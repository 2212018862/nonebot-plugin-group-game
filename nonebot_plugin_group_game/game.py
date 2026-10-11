"""群互动游戏 - 业务逻辑。

四个模块共享一套经济：
  水龙头: 签到、小弟打工（系统发放）
  群内再分配: 打劫（零和转移）；劫色（无金币，赢魅力值）
  沉淀: 求婚戒指（200 里 100 直接销毁）
时间统一用东八区自然日（容器是 UTC，签到日界必须显式 +8）。
"""
import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import db

CST = timezone(timedelta(hours=8))

# ---------- 数值配置 ----------
NEWBIE_COINS = 100          # 新玩家启动金
SINK_USER = "___system_sink___"   # 销毁账户（求婚戒指等），等效移出流通
SIGN_BASE = 50              # 签到基础
SIGN_STREAK_STEP = 5        # 连签每日加成
SIGN_STREAK_MAX = 50        # 连签加成上限
QUERY_CD = 5                # 查询类冷却(秒)

# 战斗三合一：cd=个人冷却 daily=每日限额 win=胜率
# lo=固定抢夺区间 pct=按目标余额比例 lose=失败赔偿区间
ATTACKS = {
    "打劫": dict(cd=10, daily=30, win=0.45, lo=(20, 60), pct=0.15, lose=(20, 50)),
}
CHARM_ROB = dict(cd=10, daily=30, win=0.50, gain=(1, 3))   # 劫色：成功加魅力值
TRANSFER_CD = 5                                            # 转账冷却(秒)
PAIR_CD = 10                # 打劫/劫色共享出手间隔（与个人 CD 同步）
MIN_VICTIM_COINS = 10       # 目标低于此不值得动手

RECRUIT_CD = 60
RECRUIT_DAILY = 10
MAX_FOLLOWERS = 5           # 每人基础上限 5 个小弟
RELEASE_CD = 60             # 放生冷却(秒)
REDEEM_CD = 300             # 赎身冷却(秒)
REDEEM_FEE = 50             # 赎身费（销毁账户沉淀）
REQUEST_EXPIRE = 600        # 收小弟/求婚请求 10 分钟过期

WORK_RATE = 1               # 小弟打工 1 金币/分钟/人
WORK_SESSION_MAX = 480      # 单次打工最多 8 小时（分钟），超时无收益，需收工重上
WORK_FOLLOWER_SHARE = 10    # 小弟分成 10%（其余归老大）


def transfer_coins(group_id: str, sender: str, target: str, text: str) -> str:
    """转账赠送金币（零和转移，双侧写流水）。"""
    if sender == target:
        return "❌ 不能转给自己。"
    n = _parse_amount(text)
    if n is None:
        return "用法：转账 100 @收款人"
    now_ts = db.now()
    if now_ts - db.last_action_ts(group_id, sender, "转账") < TRANSFER_CD:
        return "⏳ 转账冷却中，5 秒后再来。"
    # 群成员校验：昵称映射非空时才校验（桥不可用/测试群放行）
    nicks = nick_map(group_id)
    if nicks and target not in nicks:
        return "❌ 对方不在本群成员列表，检查是否 @ 错人。"
    p = db.ensure_player(group_id, sender)
    if p["coins"] < n:
        return f"❌ 余额不足：你要转 {n}，钱包只有 {p['coins']}。"
    db.log_action(group_id, sender, "转账", target)
    db.transfer(group_id, sender, target, n, "转账赠送")
    s = db.ensure_player(group_id, sender)["coins"]
    t = db.ensure_player(group_id, target)["coins"]
    return (
        f"🎁 {name(group_id, sender)} 转账 {n} 金币给 {name(group_id, target)}！\n"
        f"🏦 {name(group_id, sender)} 余额 {s}\n"
        f"🏦 {name(group_id, target)} 余额 {t}"
    )


# ---------- 银行 ----------

def _parse_amount(text: str) -> int | None:
    t = (text or "").strip().replace(",", "")
    if not t:
        return None
    try:
        n = int(t.split()[0])
    except ValueError:
        return None
    return n if n > 0 else None


def _settle_bank(group_id: str, user_id: str) -> int:
    """懒结息：按整天数复利入存款余额（钱在 bank 表，不进钱包）。返回本次结得利息。"""
    row = db.get_bank(group_id, user_id)
    if not row or row["balance"] <= 0 or row["last_interest_at"] <= 0:
        return 0
    days = (db.now() - row["last_interest_at"]) // 86400
    if days < 1:
        return 0
    interest = int(row["balance"] * ((1 + BANK_DAILY_RATE) ** days - 1))
    db.bank_upsert(
        group_id, user_id,
        row["balance"] + interest,
        row["last_interest_at"] + days * 86400,
    )
    return interest


def bank_info(group_id: str, user_id: str) -> str:
    settled = _settle_bank(group_id, user_id)
    row = db.get_bank(group_id, user_id)
    bal = row["balance"] if row else 0
    vault = db.ensure_player(group_id, VAULT_USER, default_coins=VAULT_SEED)["coins"]
    lines = [f"🏦 群银行（日息 {int(BANK_DAILY_RATE * 100)}%，复利）", "━━━━━━━━━━"]
    if settled:
        lines.append(f"💰 刚结算利息 +{settled}")
    lines += [
        f"我的存款：{bal}",
        (f"明日利息：约 +{int(bal * BANK_DAILY_RATE)}" if bal else "发「存款 N」把闲钱放进来"),
        f"存款上限：{BANK_MAX}",
        f"金库储备：{vault}" + ("  ⚠️ 见底" if vault < 200 else ""),
        "━━━━━━━━━━",
        "指令：存款 N ／ 取款 N（或 取款 全部）／ 抢银行",
    ]
    return "\n".join(lines)


def deposit(group_id: str, user_id: str, text: str) -> str:
    n = _parse_amount(text)
    if n is None:
        return "用法：存款 500"
    _settle_bank(group_id, user_id)
    row = db.get_bank(group_id, user_id)
    current = row["balance"] if row else 0
    if current >= BANK_MAX:
        return f"⛔ 存款已到上限（{BANK_MAX}）。"
    if n > BANK_MAX - current:
        return f"⛔ 超出上限：最多还能存 {BANK_MAX - current}（上限 {BANK_MAX}）。"
    if not db.wallet_delta(group_id, user_id, -n, "银行存款"):
        return f"❌ 余额不足：钱包只有 {db.ensure_player(group_id, user_id)['coins']}。"
    last = row["last_interest_at"] if row and row["last_interest_at"] > 0 else db.now()
    db.bank_upsert(group_id, user_id, current + n, last if current > 0 else db.now())
    p = db.ensure_player(group_id, user_id)
    return (
        f"🏦 存入 {n} 金币（存款余额 {current + n}）\n"
        f"📅 明天此时利息约 +{int((current + n) * BANK_DAILY_RATE)}\n"
        f"🏦 钱包余额 {p['coins']}"
    )


def withdraw(group_id: str, user_id: str, text: str) -> str:
    _settle_bank(group_id, user_id)
    row = db.get_bank(group_id, user_id)
    bal = row["balance"] if row else 0
    if bal <= 0:
        return "你没有存款，先「存款 N」。"
    t = (text or "").strip()
    first = t.split()[0] if t else ""
    if first in ("全部", "all", "ALL"):
        n = bal
    else:
        n = _parse_amount(text)
        if n is None:
            return "用法：取款 500 ／ 取款 全部"
        if n > bal:
            return f"❌ 存款只有 {bal}。"
    if not db.wallet_delta(group_id, user_id, n, "银行取款"):
        return "系统忙，稍后再试。"
    db.bank_upsert(group_id, user_id, bal - n, db.now())
    p = db.ensure_player(group_id, user_id)
    return (
        f"🏦 取出 {n} 金币（含已结利息）\n"
        f"💰 钱包余额 {p['coins']}，存款余额 {bal - n}"
    )


def rob_bank(group_id: str, user_id: str) -> str:
    now_ts = db.now()
    if now_ts - db.last_action_ts(group_id, user_id, "抢银行") < BANK_ROB["cd"]:
        return "⏳ 抢银行冷却中，5 分钟后再来。"
    if db.today_count(group_id, user_id, "抢银行", day_start_ts()) >= BANK_ROB["daily"]:
        return "⛔ 今日抢银行次数已用完（10 次）。"
    vault = db.ensure_player(group_id, VAULT_USER, default_coins=VAULT_SEED)["coins"]
    if vault < 20:
        return "🪙 金库见底了，抢无可抢（等别人存钱或交罚款回血）。"

    db.log_action(group_id, user_id, "抢银行")
    if random.random() < BANK_ROB["win"]:
        loot = min(vault, random.randint(*BANK_ROB["loot"]))
        db.transfer(group_id, VAULT_USER, user_id, loot, "抢银行")
        left = db.ensure_player(group_id, VAULT_USER, default_coins=VAULT_SEED)["coins"]
        bal = db.ensure_player(group_id, user_id)["coins"]
        return (
            f"🚨 【抢银行成功】{name(group_id, user_id)} 卷走 {loot} 金币！\n"
            f"🪙 金库剩余 {left}\n"
            f"🏦 余额 {bal}"
        )
    fine = min(db.ensure_player(group_id, user_id)["coins"], random.randint(*BANK_ROB["fine"]))
    if fine <= 0:
        return f"🚨 【抢银行失败】{name(group_id, user_id)} 被逮个正着，\n可你身无分文，警察都懒得罚……"
    db.transfer(group_id, user_id, VAULT_USER, fine, "抢银行罚款")
    bal = db.ensure_player(group_id, user_id)["coins"]
    return (
        f"🚨 【抢银行失败】{name(group_id, user_id)} 被保安按倒在地！\n"
        f"💸 罚款 {fine} 金币（进了金库）\n"
        f"🏦 余额 {bal}"
    )


# ---------- 刮刮乐 ----------

def _lottery_status(group_id: str, rnd) -> str:
    pool = db.ensure_player(group_id, POOL_USER, default_coins=0)["coins"]
    n, buyers, winners = db.lottery_stats(rnd["id"])
    left = max(0, rnd["ends_at"] - db.now())
    return (
        f"🎰 刮刮乐进行中\n"
        f"━━━━━━━━━━\n"
        f"💰 奖池：{pool}\n"
        f"🎫 购买：{buyers} 人 / {n} 张\n"
        f"🎉 中奖：{winners} 人\n"
        f"⏳ 剩余：{left // 60}:{left % 60:02d}\n"
        f"━━━━━━━━━━\n"
        f"发「购买刮刮乐 3」买 3 张即刮即开"
    )


def lottery_announce(group_id: str, rnd) -> str:
    """生成本轮结束公告（只读，不结算）。"""
    pool = db.ensure_player(group_id, POOL_USER, default_coins=0)["coins"]
    n, buyers, winners = db.lottery_stats(rnd["id"])
    return (
        f"🎰 刮刮乐本轮结束！\n"
        f"💥 奖池 {pool} 金币已销毁\n"
        f"🎫 售出 {n} 张 · 购买 {buyers} 人 · 中奖 {winners} 人\n"
        f"发「刮刮乐」开启新一轮"
    )


def lottery_finalize(group_id: str, rnd) -> None:
    """销毁奖池并关闭轮次（幂等：仅对仍 open 的轮次生效）。"""
    pool = db.ensure_player(group_id, POOL_USER, default_coins=0)["coins"]
    if pool > 0:
        db.wallet_delta(group_id, POOL_USER, -pool, "刮刮乐奖池销毁")
    db.lottery_close_round(rnd["id"])


def _lottery_close(group_id: str, rnd) -> str:
    text = lottery_announce(group_id, rnd)
    lottery_finalize(group_id, rnd)
    return text


def lottery_expired_rounds() -> list:
    """巡检只读：返回已到期但未结算的 [(group_id, round_row), ...]。"""
    rows = db.conn().execute(
        "SELECT * FROM lottery_rounds WHERE status='open'"
    ).fetchall()
    return [(row["group_id"], row) for row in rows if row["ends_at"] <= db.now()]


def lottery_command(group_id: str) -> list:
    """刮刮乐指令：进行中→状态；已到期→先结算公告再开新轮；无轮→开场。返回要逐条发送的消息。"""
    rnd = db.lottery_active_round(group_id)
    if rnd is not None and rnd["ends_at"] > db.now():
        return [_lottery_status(group_id, rnd)]
    msgs = []
    if rnd is not None:  # 到期未结算（巡检 30 秒窗口内）：本指令顺手结算
        msgs.append(_lottery_close(group_id, rnd))
    # 开新轮：清残留池（崩溃遗留）后铸入初始奖池
    stale = db.ensure_player(group_id, POOL_USER, default_coins=0)["coins"]
    if stale > 0:
        db.wallet_delta(group_id, POOL_USER, -stale, "刮刮乐奖池销毁")
    db.wallet_delta(group_id, POOL_USER, LOTTERY_SEED, "刮刮乐奖池开启")
    db.lottery_open_round(group_id, db.now(), db.now() + LOTTERY_ROUND)
    msgs.append(
        f"🎰 刮刮乐开场！初始奖池 {LOTTERY_SEED} 金币\n"
        f"🎫 票价 {LOTTERY_TICKET} 金/张（全额进池），{LOTTERY_ROUND // 60} 分钟后整池销毁\n"
        f"📖「购买刮刮乐 3」买 3 张即刮即开；「刮刮乐」看本轮状态"
    )
    return msgs


def lottery_buy(group_id: str, user_id: str, text: str) -> str:
    rnd = db.lottery_active_round(group_id)
    if rnd is None:
        return "🎰 还没开场，先发「刮刮乐」开启本轮。"
    if rnd["ends_at"] <= db.now():
        return "⌛ 本轮已到期，系统结算中——稍后先发「刮刮乐」开新一轮。"
    t = (text or "").strip()
    if not t:
        n = 1
    else:
        try:
            n = int(t.split()[0])
        except ValueError:
            return f"用法：购买刮刮乐 3（每次 1-{LOTTERY_MAX_BUY} 张）"
        if n < 1 or n > LOTTERY_MAX_BUY:
            return f"每次最多 {LOTTERY_MAX_BUY} 张，用法：购买刮刮乐 3"
    now_ts = db.now()
    if now_ts - db.last_action_ts(group_id, user_id, "刮刮乐") < LOTTERY_BUY_CD:
        return "⏳ 手速太快，3 秒后再刮。"
    cost = n * LOTTERY_TICKET
    p = db.ensure_player(group_id, user_id)
    if p["coins"] < cost:
        return f"❌ 余额不足：{n} 张要 {cost} 金，你只有 {p['coins']}。"
    db.log_action(group_id, user_id, "刮刮乐")
    db.transfer(group_id, user_id, POOL_USER, cost, f"购买刮刮乐×{n}")
    pool = db.ensure_player(group_id, POOL_USER, default_coins=0)["coins"]
    misses = 0
    wins = []
    for _ in range(n):
        tier, prize = "", 0
        for pct, one_in in LOTTERY_PRIZES:
            if random.randint(1, one_in) == 1:
                tier = f"{int(pct * 100)}%"
                prize = max(1, int(pool * pct))
                prize = min(prize, pool)
                db.transfer(group_id, POOL_USER, user_id, prize, f"刮刮乐中奖({tier})")
                pool -= prize
                break
        db.lottery_add_ticket(rnd["id"], group_id, user_id, tier, prize)
        if prize > 0:
            wins.append((tier, prize))
        else:
            misses += 1
    bal = db.ensure_player(group_id, user_id)["coins"]
    lines = [f"🎰 刮刮乐 ×{n}（-{cost} 金）"]
    if wins:
        for tier, prize in wins:
            lines.append(f"🎉 中奖！{tier} 池 → +{prize} 金")
    if misses:
        lines.append(f"❌ 未中 ×{misses}")
    lines.append(f"🏦 余额 {bal} ｜ 💰 奖池 {pool}")
    return "\n".join(lines)


def _idiom_shape(t: str) -> str | None:
    """结构性校验：必须恰好 4 个字（是不是真成语由群里的人肉鉴定）。"""
    if len(t) != 4:
        return "成语必须是 4 个字，用法：成语接龙 光明正大"
    return None


def _idiom_status_text(group_id: str, st: dict) -> str:
    last = st["chain"][-1]
    left = max(0, st["started_at"] + 0, st["last_at"] + IDIOM_TIMEOUT - db.now())
    return (
        f"🧩 成语接龙进行中（{len(st['chain'])} 条）\n"
        f"━━━━━━━━━━\n"
        f"上一条：{last['t']} — {name(group_id, last['u'])}\n"
        f"👉 下一位接「{last['t'][-1]}」开头的 4 字成语：成语接龙 你的成语\n"
        f"⏰ 满 {IDIOM_WIN_LEN} 条返还发起者押金；{left // 60}:{left % 60:02d} 无人接则结束（押金不退）"
    )


def idiom(group_id: str, user_id: str, text: str) -> str:
    t = (text or "").strip()
    st = db.idiom_get(group_id)
    stale_note = ""

    # 惰性过期处理：超时但巡检还没清（≤30s 窗口）——本指令顺手收尾
    if st is not None and db.now() - st["last_at"] >= IDIOM_TIMEOUT:
        db.idiom_delete(group_id)
        stale_note = "（上一局已超时结束，押金不退）\n"
        st = None

    if st is None:
        # —— 开局 ——
        seed = t if t else random.choice(IDIOM_SEED)
        err = _idiom_shape(seed)
        if err:
            return stale_note + err
        p = db.ensure_player(group_id, user_id)
        if p["coins"] < IDIOM_START_COST:
            return stale_note + f"开局押金 {IDIOM_START_COST} 金，你只有 {p['coins']}。"
        db.transfer(group_id, user_id, SINK_USER, IDIOM_START_COST, "成语接龙押金")
        now_ts = db.now()
        db.idiom_put(group_id, {
            "starter": user_id, "started_at": now_ts, "last_at": now_ts,
            "chain": [{"u": user_id, "t": seed}],
        })
        return (
            stale_note
            + f"🧩 成语接龙开局！押金 -{IDIOM_START_COST} 金\n"
            + f"首句：{seed}\n"
            + f"👉 接「{seed[-1]}」开头的 4 字成语：成语接龙 你的成语\n"
            + f"⏰ 满 {IDIOM_WIN_LEN} 条返还发起者押金；{IDIOM_TIMEOUT // 60} 分钟没人接则结束（押金不退）"
        )

    # —— 本局进行中：查状态 ——
    if not t:
        return stale_note + _idiom_status_text(group_id, st)

    # —— 尝试接龙 ——
    err = _idiom_shape(t)
    if err:
        return err
    last = st["chain"][-1]
    if any(c["t"] == t for c in st["chain"]):
        return "⛔ 这条成语已经在链里了，换一个。"
    if t[0] != last["t"][-1]:
        return f"❌ 要接「{last['t'][-1]}」开头的成语（上一条是「{last['t']}」）。"

    reward = random.randint(*IDIOM_REWARD)
    db.mint(group_id, user_id, reward, "成语接龙成功")
    st["chain"].append({"u": user_id, "t": t})
    st["last_at"] = db.now()

    # 超过 IDIOM_WIN_LEN → 返还发起者押金并结束
    if len(st["chain"]) > IDIOM_WIN_LEN:
        db.idiom_delete(group_id)
        db.mint(group_id, st["starter"], IDIOM_START_COST, "成语接龙押金返还")
        bal = db.ensure_player(group_id, user_id)["coins"]
        return (
            f"✅ {t} — +{reward} 金！\n"
            f"🎉 接龙达成（{len(st['chain'])} 条）！返还发起者 "
            f"{name(group_id, st['starter'])} 押金 {IDIOM_START_COST} 金，本局结束。"
        )

    db.idiom_put(group_id, st)
    next_c = t[-1]
    return (
        f"✅ {t} — +{reward} 金！\n"
        f"👉 下一位接「{next_c}」开头（{len(st['chain'])}/{IDIOM_WIN_LEN}），"
        f"发：成语接龙 你的成语"
    )


def idiom_broadcast_tasks(now_ts: int) -> list:
    """巡检用：超时局 → [(group_id, 公告文案, starter)]，只读不清，发送成功后调 idiom_delete。"""
    out = []
    for g in db.idiom_expired(now_ts - IDIOM_TIMEOUT):
        out.append((
            g["group_id"],
            f"🧩 成语接龙结束（{IDIOM_TIMEOUT // 60} 分钟无人接），发起者 "
            f"{name(g['group_id'], g['starter'])} 的 {IDIOM_START_COST} 金押金不予返还。",
            g["starter"],
        ))
    return out


def leaderboard(group_id: str, user_id: str) -> str:
    """金币 TOP10；CD 通过的看榜者随机奖励少量金币。"""
    now_ts = db.now()
    last = db.last_action_ts(group_id, user_id, "排行榜")
    if now_ts - last < LEADERBOARD_CD:
        return f"⏳ 排行榜冷却中，{LEADERBOARD_CD - (now_ts - last)} 秒后再来。"
    db.log_action(group_id, user_id, "排行榜")
    reward = random.randint(*LEADERBOARD_REWARD)
    db.mint(group_id, user_id, reward, "看排行榜")

    sys_ph = ",".join("?" for _ in SYSTEM_USERS)
    # 财富 = 现金(players.coins) + 存款(bank.balance)
    join = (" LEFT JOIN bank b ON b.group_id = p.group_id AND b.user_id = p.user_id")
    rows = db.conn().execute(
        f"SELECT p.user_id, p.coins + COALESCE(b.balance, 0) AS total"
        f" FROM players p{join}"
        f" WHERE p.group_id=? AND p.user_id NOT IN ({sys_ph})"
        " ORDER BY total DESC LIMIT 10",
        (group_id, *SYSTEM_USERS),
    ).fetchall()
    lines = ["🏆 财富排行榜 TOP10（现金+存款）", "━━━━━━━━━━"]
    in_top = False
    for i, row in enumerate(rows, 1):
        is_me = row["user_id"] == user_id
        in_top = in_top or is_me
        lines.append(f"{i}. {name(group_id, row['user_id'])} — {row['total']}" + (" 👈是你" if is_me else ""))
    if not in_top:
        my = db.ensure_player(group_id, user_id)
        rank = db.conn().execute(
            f"SELECT COUNT(*) + 1 FROM players p{join}"
            f" WHERE p.group_id=? AND p.user_id NOT IN ({sys_ph})"
            " AND p.coins + COALESCE(b.balance, 0) > ?",
            (group_id, *SYSTEM_USERS, my["coins"]),
        ).fetchone()[0]
        my_total = my["coins"] + (
            db.get_bank(group_id, user_id)["balance"] if db.get_bank(group_id, user_id) else 0
        )
        lines.append(f"你：第 {rank} 名 · {my_total} 金")
    lines.append(f"🎁 看榜奖励 +{reward} 金（冷却 {LEADERBOARD_CD // 60} 分钟）")
    return "\n".join(lines)


# ---------- 贵族 ----------
VIP_CARD = """🔮·┈✨贵族驾到✨┈·🔮
   👑本群大佬身份象征👑
╭┈┈┈👸✨🤴┈┈┈╮
王子殿下驾到大家通通闪开
🚀昵称:{nick}
👑身份:{role}
🕐时间:{time}
  🌸🌸欢迎本群贵族🌸🌸
。 🦄🔮🦄｡    🦄🔮🦄*｡
｡ 🔮          🔮        🔮。
 🦄         身份象征      🦄
 *🔮        至高无上       🔮*
　. 🦄     💗   💗    🦄 .
　　 *🔮               🔮
　　 　🦄     🦄
                    🔮
╰┈┈┈🧝🏻‍♀✨🧝🏻┈┈┈╯"""


def _vip_card(group_id: str, user_id: str) -> str:
    ts = datetime.now(CST).strftime("%H:%M:%S")
    return VIP_CARD.format(nick=name(group_id, user_id), role=VIP_NAME, time=ts)


def vip_greeting(group_id: str, user_id: str) -> str | None:
    """贵族每日首次发言返回驾到卡；非贵族/今天已发过返回 None。"""
    if not db.buff_active(group_id, user_id, "贵族"):
        return None
    today = today_str()
    if db.vip_last_greet(group_id, user_id) == today:
        return None
    db.set_vip_greet(group_id, user_id, today)
    return _vip_card(group_id, user_id)


def vip_card_now(group_id: str, user_id: str) -> str | None:
    """购买贵族成功后立即出一次驾到卡；记为今日已发，避免当天发言再重复一张。"""
    if not db.buff_active(group_id, user_id, "贵族"):
        return None
    db.set_vip_greet(group_id, user_id, today_str())
    return _vip_card(group_id, user_id)


# ---------- 商店 ----------
SHOP = {
    "免战牌": dict(price=150, desc="30 分钟内免疫被攻击"),
    "必胜符": dict(price=200, desc="下一次攻击必定成功"),
    "双倍袋": dict(price=150, desc="下一次攻击收益翻倍"),
    "金钟罩": dict(price=120, desc="下一次攻击失手时赔付减半"),
    "小弟令牌": dict(price=400, desc="永久小弟上限+1（最多买 3 枚）"),
    "贵族": dict(price=300, desc="7 天 VIP：每日首次发言触发贵族驾到卡（可购买续期）"),
}
VIP_DUR = 7 * 86400                      # 贵族身份 7 天

# ---------- 银行 ----------
BANK_DAILY_RATE = 0.10                   # 每日利息 10%，复利（激进！要刹车改这里）
BANK_MAX = 10000                         # 单人存款上限（防通胀刹车）
VAULT_USER = "___vault___"               # 每群金库账号（抢银行的靶子）
VAULT_SEED = 10000                       # 金库初始储备
BANK_ROB = dict(cd=300, daily=10, win=0.40, loot=(20, 80), fine=(30, 100))
VIP_NAME = "王子殿下"                     # 驾到卡身份称谓
ATTACK_BUFFS = ("必胜符", "双倍袋")   # 同时只允许装一个
BUFF_DUR = 1800                       # 免战牌 30 分钟
MAX_TOKENS = 3                        # 小弟令牌持有上限

# ---------- 刮刮乐 ----------
LOTTERY_TICKET = 1                   # 票价（全额进池）
LOTTERY_SEED = 10000                 # 开轮初始奖池（铸入）
LOTTERY_ROUND = 3600                 # 一轮 1 小时
LOTTERY_MAX_BUY = 100                # 单次最多张
LOTTERY_BUY_CD = 3                   # 购买冷却(秒)
POOL_USER = "___lottery_pool___"     # 每群奖池账号
# (池占比, 概率分母 1/N) —— 定标期望返奖≈1.95/张@池10000（池自然回落至~5100平衡）
LOTTERY_PRIZES = (
    (0.50, 50000),
    (0.30, 25000),
    (0.20, 10000),
    (0.10, 3000),
    (0.05, 1250),
    (0.01, 125),
)

# ---------- 成语接龙 ----------
IDIOM_START_COST = 10       # 开局押金（进销毁账户）
IDIOM_REWARD = (1, 3)       # 每次成功接龙的随机奖励
IDIOM_TIMEOUT = 600         # 10 分钟无人接 → 结束（押金不退）
IDIOM_WIN_LEN = 10          # 链长超过 10（第 11 条）→ 返还押金并结束
# 机器人兜底开局用的 4 字成语池
IDIOM_SEED = [
    "画蛇添足", "一见如故", "故弄玄虚", "虚张声势", "势如破竹",
    "竹报平安", "安居乐业", "业精于勤", "勤能补拙", "前所未有",
    "有备无患", "患得患失", "失魂落魄", "魄散魂飞", "飞黄腾达",
    "达官贵人", "人山人海", "海阔天空", "空穴来风", "风花雪月",
    "月明星稀", "稀奇古怪", "神气活现", "现身说法", "贵不可言",
    "言而有信", "信口开河", "河清海晏", "乌合之众", "众志成城",
]

# ---------- 排行榜 ----------
LEADERBOARD_CD = 60                   # 看榜冷却(秒)
LEADERBOARD_REWARD = (1, 5)           # 看榜随机奖励（少量）
SYSTEM_USERS = (SINK_USER, VAULT_USER, POOL_USER)   # 系统账号不参与排行

MARRIAGE_CD = 1800
MARRIAGE_DAILY = 5
RING_COST = 200             # 戒指钱（求婚方出）
RING_GIFT = 100             # 其中 100 转给对象当红包，100 销毁（sink）

HELP_TEXT = """🎮 群互动游戏 指令
💰 签到（每日打卡，连签加成）
💰 我的金币 — 余额/连签/小弟/婚姻
⚔️ 打劫@某人 — 抢金币（10秒/日30次）
💘 劫色@某人 — 成功加魅力值（10秒/日30次）
👨‍👩‍👦 收小弟@某人 — 对方回「同意」才算
👨‍👦 小弟打工 / 小弟收工 — 1金/分钟，单次8小时封顶，分成9:1
🐟 放生@小弟 — 老大开除（工钱结清）
🕊️ 赎身 — 花50金币重获自由
🛒 商店 — 查看货架；购买道具名 购买
🔮 贵族 — 7天VIP，每日首条消息贵族驾到卡
🏦 银行 — 存款N/取款N/抢银行，日息10%复利
🎁 转账 N @某人 — 赠送金币
🎰 刮刮乐 — 开轮/看状态；购买刮刮乐 3（1金/张，1小时销毁）
🏆 排行榜 — 财富TOP10（现金+存款），看榜得1~5金（CD 1分钟）
🧩 成语接龙 — 押金10金，接龙得1~3金；满10条退押金，10分钟超时押金不退
💍 求婚@某人 — 对象回「同意」
💔 离婚
✅ 同意 / ❌ 拒绝 — 确认请求"""


def today_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def day_start_ts() -> int:
    """今天 00:00（东八区）的 epoch 秒。"""
    now_cst = datetime.now(CST)
    midnight = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def short(uid: str) -> str:
    """wxid 太长，展示时截短。"""
    return uid if len(uid) <= 12 else uid[:10] + "…"


BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:9527")
_nick_mem: dict[str, tuple[float, dict[str, str]]] = {}


def nick_map(group_id: str) -> dict[str, str]:
    """经桥 /nicks 取群成员 wxid->昵称，本地缓存 60s；失败空表缓存 30s（回退显示 wxid）。"""
    now = time.time()
    hit = _nick_mem.get(group_id)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        q = urllib.parse.quote(group_id, safe="")
        with urllib.request.urlopen(f"{BRIDGE_URL}/nicks?group_id={q}", timeout=2) as r:
            nicks = (json.loads(r.read().decode()) or {}).get("nicks") or {}
        _nick_mem[group_id] = (now, nicks)
        return nicks
    except Exception:
        _nick_mem[group_id] = (now - 30, hit[1] if hit else {})
        return hit[1] if hit else {}


def name(group_id: str, uid: str) -> str:
    """显示名：优先群昵称，查不到回退截短 wxid。"""
    return nick_map(group_id).get(uid) or short(uid)


# ---------- 签到 / 查询 ----------

def do_sign(group_id: str, user_id: str) -> str:
    today = today_str()
    p = db.ensure_player(group_id, user_id)
    if p["last_sign_date"] == today:
        return f"⏳ {name(group_id, user_id)} 今天已经签到过了，明天再来吧！"

    if p["last_sign_date"] == (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d"):
        streak = p["sign_streak"] + 1
    else:
        streak = 1

    reward = SIGN_BASE + min((streak - 1) * SIGN_STREAK_STEP, SIGN_STREAK_MAX)
    db.mint(group_id, user_id, reward, "签到")
    db.update_player(
        group_id, user_id,
        last_sign_date=today, sign_streak=streak, total_sign=p["total_sign"] + 1,
    )
    db.log_action(group_id, user_id, "签到")
    bal = db.ensure_player(group_id, user_id)["coins"]
    return (
        f"✅ {name(group_id, user_id)} 签到成功！\n"
        f"━━━━━━━━━━\n"
        f"💰 金币 +{reward}（连签 {streak} 天）\n"
        f"🏦 余额 {bal}"
    )


def wallet(group_id: str, user_id: str) -> str:
    last = db.last_action_ts(group_id, user_id, "_查询")
    if time.time() - last < QUERY_CD:
        return "⏳ 查得太快了，歇 5 秒～"
    db.log_action(group_id, user_id, "_查询")

    swept = auto_accept_recruits(group_id)
    p = db.ensure_player(group_id, user_id)
    followers = db.followers_of(group_id, user_id)
    working = sum(1 for f in followers if f["status"] == "working")
    m = db.get_marriage(group_id, user_id)
    if m:
        partner = m["user_b"] if m["user_a"] == user_id else m["user_a"]
        marry_text = f"💍 与 {name(group_id, partner)}"
    else:
        marry_text = "💔 未婚"
    lines = [
        f"💰 {name(group_id, user_id)} 的钱包",
        f"━━━━━━━━━━",
        f"金币：{p['coins']}",
        f"签到：连签 {p['sign_streak']} 天 / 累计 {p['total_sign']} 天",
        f"小弟：{len(followers)} 人" + (f"（{working} 人打工中）" if followers else ""),
    ]
    for f in followers:
        status = "打工中" if f["status"] == "working" else "空闲"
        lines.append(f"  · {name(group_id, f['member_user'])}（{status}）")
    lines.append(marry_text)
    brow = db.get_bank(group_id, user_id)
    if brow and brow["balance"] > 0:
        lines.append(f"🏦 存款：{brow['balance']}")
    lines.append(f"✨ 魅力值：{p['charm']}")
    if swept:
        lines.append(f"📨 超时自动加入 {swept} 人")
    tail = db.ledger_tail(group_id, user_id, 3)
    if tail:
        lines.append("最近流水：")
        for r in tail:
            sign = "+" if r["delta"] >= 0 else ""
            lines.append(f"  {r['reason']} {sign}{r['delta']} → {r['balance_after']}")
    return "\n".join(lines)


# ---------- 战斗 ----------

def attack(group_id: str, attacker: str, victim: str, kind: str) -> str:
    cfg = ATTACKS[kind]
    if attacker == victim:
        return "❌ 不能对自己动手。"

    now_ts = int(time.time())
    last = db.last_action_ts(group_id, attacker, kind)
    if now_ts - last < cfg["cd"]:
        wait_txt = f"{cfg['cd']} 秒" if cfg["cd"] < 60 else f"{cfg['cd'] // 60} 分钟"
        return f"⏳ {kind}冷却中，{wait_txt}后再来。"
    daily = db.today_count(group_id, attacker, kind, day_start_ts())
    if daily >= cfg["daily"]:
        return f"⛔ 今日{kind}次数已用完（{cfg['daily']} 次）。"
    pair_last = db.pair_attack_ts(group_id, attacker)
    if now_ts - pair_last < PAIR_CD:
        wait = PAIR_CD - (now_ts - pair_last)
        wait_txt = f"{wait} 秒" if wait < 60 else f"{wait // 60 + 1} 分钟"
        return f"⏳ 刚动过手，{wait_txt}后才能再攻击任何人。"

    if db.buff_active(group_id, victim, "免战牌"):
        vrow = db.get_buff(group_id, victim, "免战牌")
        remain = max(1, (vrow["expires_at"] - now_ts + 59) // 60)
        return f"🛡 {name(group_id, victim)} 挂着免战牌（剩 {remain} 分钟），无从下手。"

    v = db.ensure_player(group_id, victim)
    if v["coins"] < MIN_VICTIM_COINS:
        return f"😅 {name(group_id, victim)} 只有 {v['coins']} 金币，下不去手……"

    a = db.ensure_player(group_id, attacker)
    db.log_action(group_id, attacker, kind, victim)  # 够格动手才占冷却/限额

    notes = ""
    must_win = db.consume_buff(group_id, attacker, "必胜符")
    if must_win:
        notes += "\n📜 必胜符生效！"

    if must_win or random.random() < cfg["win"]:
        gain = min(v["coins"], random.randint(*cfg["lo"]) + int(v["coins"] * cfg["pct"]))
        if db.consume_buff(group_id, attacker, "双倍袋"):
            gain = min(v["coins"], gain * 2)
            notes += "\n🎁 双倍袋生效！"
        db.transfer(group_id, victim, attacker, gain, kind)
        return (
            f"⚔️ 【{kind}成功】{name(group_id, attacker)} → {name(group_id, victim)}\n"
            f"💰 抢到 {gain} 金币{notes}\n"
            f"🏦 余额 {a['coins'] + gain}"
        )

    penalty = min(a["coins"], random.randint(*cfg["lose"]))
    if db.consume_buff(group_id, attacker, "金钟罩") and penalty > 0:
        penalty = (penalty + 1) // 2
        notes += "\n🛡 金钟罩生效！"
    if penalty <= 0:
        return (
            f"💥 【{kind}失败】{name(group_id, attacker)} 扑了个空，\n"
            f"而且身无分文，{name(group_id, victim)} 也拿你没办法……"
        )
    db.transfer(group_id, attacker, victim, penalty, f"{kind}失败赔偿")
    return (
        f"💥 【{kind}失败】{name(group_id, attacker)} 反被{name(group_id, victim)}制服！\n"
        f"💸 赔了 {penalty} 金币{notes}\n"
        f"🏦 余额 {a['coins'] - penalty}"
    )


def charm_rob(group_id: str, attacker: str, victim: str) -> str:
    """劫色：无金币转移，成功给自己加魅力值。"""
    if attacker == victim:
        return "❌ 不能对自己下手。"
    now_ts = db.now()
    last = db.last_action_ts(group_id, attacker, "劫色")
    if now_ts - last < CHARM_ROB["cd"]:
        return f"⏳ 劫色冷却中，{CHARM_ROB['cd']} 秒后再来。"
    if db.today_count(group_id, attacker, "劫色", day_start_ts()) >= CHARM_ROB["daily"]:
        return f"⛔ 今日劫色次数已用完（{CHARM_ROB['daily']} 次）。"
    pair_last = db.pair_attack_ts(group_id, attacker)
    if now_ts - pair_last < PAIR_CD:
        wait = PAIR_CD - (now_ts - pair_last)
        wait_txt = f"{wait} 秒" if wait < 60 else f"{wait // 60 + 1} 分钟"
        return f"⏳ 刚动过手，{wait_txt}后才能再出手。"
    if db.buff_active(group_id, victim, "免战牌"):
        vrow = db.get_buff(group_id, victim, "免战牌")
        remain = max(1, (vrow["expires_at"] - now_ts + 59) // 60)
        return f"🛡 {name(group_id, victim)} 挂着免战牌（剩 {remain} 分钟），无从下手。"

    db.log_action(group_id, attacker, "劫色", victim)
    notes = ""
    must_win = db.consume_buff(group_id, attacker, "必胜符")
    if must_win:
        notes += "\n📜 必胜符生效！"

    if must_win or random.random() < CHARM_ROB["win"]:
        gain = random.randint(*CHARM_ROB["gain"])
        if db.consume_buff(group_id, attacker, "双倍袋"):
            gain *= 2
            notes += "\n🎁 双倍袋生效！"
        total = db.add_charm(group_id, attacker, gain)
        return (
            f"💘 【劫色成功】{name(group_id, attacker)} 对 {name(group_id, victim)} 下手了！\n"
            f"✨ 魅力值 +{gain}（当前 {total}）{notes}"
        )
    return (
        f"💔 【劫色失败】{name(group_id, attacker)} 被 {name(group_id, victim)} 当变态打了……\n"
        f"✨ 魅力值没有变化，整理仪容再来（无需疗伤）"
    )


# ---------- 小弟 ----------

def auto_accept_recruits(group_id: str) -> int:
    """收小弟请求超 10 分钟未回应自动视为同意（懒清算：随读随结）。返回自动加入数。"""
    cutoff = db.now() - REQUEST_EXPIRE
    accepted = 0
    for req in db.overdue_recruits(group_id, cutoff):
        owner, member = req["from_user"], req["to_user"]
        # 与手动同意同样的守卫：目标已有大哥 / 原主名额已满 → 作罢
        if db.get_follower(group_id, member) or len(db.followers_of(group_id, owner)) >= follower_cap(group_id, owner):
            db.set_request_status(req["id"], "rejected")
            continue
        db.add_follower(group_id, member, owner)
        db.set_request_status(req["id"], "accepted")
        accepted += 1
    return accepted


def follower_cap(group_id: str, owner: str) -> int:
    """小弟上限 = 基础 5 + 小弟令牌（每枚 +1，最多 3 枚）→ 5~8。"""
    return MAX_FOLLOWERS + min(db.buff_count(group_id, owner, "小弟令牌"), MAX_TOKENS)


def recruit(group_id: str, owner: str, member: str) -> str:
    auto_accept_recruits(group_id)  # 顺带清算旧请求，名额判断才准
    if owner == member:
        return "❌ 不能收自己当小弟。"
    now_ts = int(time.time())
    last = db.last_action_ts(group_id, owner, "收小弟")
    if now_ts - last < RECRUIT_CD:
        return f"⏳ 收小弟冷却中，{RECRUIT_CD // 60} 分钟后再来。"
    if db.today_count(group_id, owner, "收小弟", day_start_ts()) >= RECRUIT_DAILY:
        return "⛔ 今日收小弟次数已用完。"
    if db.has_pending_from(group_id, owner, "recruit", REQUEST_EXPIRE):
        return "⏳ 你已有一个待确认的收徒请求。"
    mine = db.followers_of(group_id, owner)
    cap = follower_cap(group_id, owner)
    if len(mine) >= cap:
        return f"⛔ 小弟已满（{cap} 个），先开除一个或买小弟令牌。"
    existing = db.get_follower(group_id, member)
    if existing:
        who = "你的" if existing["owner_user"] == owner else f"{name(group_id, existing['owner_user'])} 的"
        return f"❌ {name(group_id, member)} 已经是 {who}小弟了。"

    db.log_action(group_id, owner, "收小弟", member)
    db.create_request(group_id, "recruit", owner, member)
    return (
        f"👨‍👦 {name(group_id, owner)} 想收 {name(group_id, member)} 为小弟！\n"
        f"{name(group_id, member)} 回复「同意」加入，或「拒绝」。\n"
        f"⏰ 超过 10 分钟未回复，视为同意"
    )


def _work_income(start_ts: int, end_ts: int) -> int:
    """单次打工收益：按分钟计，封顶 WORK_SESSION_MAX 分钟（8 小时）。"""
    minutes = min(int((end_ts - start_ts) // 60), WORK_SESSION_MAX)
    return minutes * WORK_RATE


def work(group_id: str, owner: str) -> str:
    swept = auto_accept_recruits(group_id)
    followers = db.followers_of(group_id, owner)
    if not followers:
        return "你还没有小弟，先 收小弟@某人 吧。"
    idle = [f for f in followers if f["status"] == "idle"]
    if not idle:
        return f"你的 {len(followers)} 个小弟都在干活，回复「小弟收工」结账。"
    ts = db.now()
    for f in idle:
        db.set_follower_status(group_id, f["member_user"], "working", ts)
    working = sum(1 for f in followers if f["status"] == "working") + len(idle)
    note = f"📨 {swept} 个新人超时自动加入\n" if swept else ""
    return (
        note
        + f"👨‍🏭 {len(idle)} 个小弟上工了！（共 {working} 人打工）\n"
        + f"💵 {WORK_RATE} 金币/分钟/人，单次最多 {WORK_SESSION_MAX // 60} 小时（{WORK_SESSION_MAX} 分钟）\n"
        + "超时部分无收益，收工重新上工即可继续赚。\n"
        + "收工时小弟分 10%、你分 90%，发「小弟收工」结账。"
    )


def settle(group_id: str, owner: str) -> str:
    swept = auto_accept_recruits(group_id)
    followers = db.followers_of(group_id, owner)
    working = [f for f in followers if f["status"] == "working"]
    if not working:
        return "当前没有打工中的小弟，先「小弟打工」。"
    ts = db.now()
    lines = []
    total = 0
    follower_total = 0
    for f in working:
        income = _work_income(f["started_at"], ts)
        total += income
        share = income // WORK_FOLLOWER_SHARE
        follower_total += share
        if share > 0:
            db.mint(group_id, f["member_user"], share, "小弟打工分成")
        mins = min(int((ts - f["started_at"]) // 60), WORK_SESSION_MAX)
        lines.append(f"  {name(group_id, f['member_user'])}：+{income} 金（{mins} 分钟）")
        db.set_follower_status(group_id, f["member_user"], "idle", 0)
    owner_total = total - follower_total
    if owner_total > 0:
        db.mint(group_id, owner, owner_total, "小弟打工收益")
    bal = db.ensure_player(group_id, owner)["coins"]
    return (
        (f"📨 {swept} 个新人超时自动加入\n" if swept else "")
        + f"🏗️ 小弟收工（{len(working)} 人）\n"
        + "\n".join(lines)
        + f"\n💰 总收益 {total}：小弟分 {follower_total}、你分 {owner_total}"
        + f"\n🏦 余额 {bal}"
        + ("\n⚠️ 不足 1 分钟没有收益，让他们多干会儿。" if total == 0 else "")
    )


def _depart_settle(group_id: str, follower) -> tuple:
    """成员离开时结清工钱（同样受日上限、10/90 分成）；返回 (总收益, 老大份)。"""
    if follower["status"] != "working":
        return (0, 0)
    total = _work_income(follower["started_at"], db.now())
    if total <= 0:
        return (0, 0)
    share = total // WORK_FOLLOWER_SHARE
    owner_part = total - share
    if share > 0:
        db.mint(group_id, follower["member_user"], share, "小弟打工分成")
    db.mint(group_id, follower["owner_user"], owner_part, "小弟离开结算")
    return (total, owner_part)


def release(group_id: str, owner: str, member: str) -> str:
    """老大放生（开除）小弟。"""
    f = db.get_follower(group_id, member)
    if not f or f["owner_user"] != owner:
        return f"❌ {name(group_id, member)} 不是你的小弟。"
    now_ts = db.now()
    if now_ts - db.last_action_ts(group_id, owner, "放生") < RELEASE_CD:
        return "⏳ 放生冷却中，1 分钟后再来。"
    total, owner_part = _depart_settle(group_id, f)
    db.remove_follower(group_id, member)
    db.log_action(group_id, owner, "放生", member)
    tail = f"\n💰 已结清工钱：你 +{owner_part}、小弟 +{total - owner_part}" if total > 0 else ""
    return (
        f"🐟 {name(group_id, owner)} 放生了小弟 {name(group_id, member)}！\n"
        f"{name(group_id, member)} 重获自由 🌊" + tail
    )


def redeem(group_id: str, user_id: str) -> str:
    """小弟花钱赎身，赎身费进销毁账户。"""
    f = db.get_follower(group_id, user_id)
    if not f:
        return "🕊️ 你本来就是自由身。"
    now_ts = db.now()
    if now_ts - db.last_action_ts(group_id, user_id, "赎身") < REDEEM_CD:
        return "⏳ 赎身冷静期，5 分钟后再来（好马不吃回头草）。"
    total, owner_part = _depart_settle(group_id, f)
    fee = min(REDEEM_FEE, db.ensure_player(group_id, user_id)["coins"])
    if fee > 0:
        db.transfer(group_id, user_id, SINK_USER, fee, "赎身费(销毁)")
    db.remove_follower(group_id, user_id)
    db.log_action(group_id, user_id, "赎身")
    owner_note = (f"\n（工钱已结：老大 +{owner_part}、你 +{total - owner_part}）"
                  if total > 0 else "")
    return (
        f"🕊️ {name(group_id, user_id)} 赎身成功，重获自由！\n"
        f"💸 赎身费 {fee} 金币化作烟火 ✨" + owner_note
    )


def shop_list(group_id: str, user_id: str) -> str:
    p = db.ensure_player(group_id, user_id)
    lines = [f"🛒 群商店（你有 {p['coins']} 金币）", "━━━━━━━━━━"]
    for item, cfg in SHOP.items():
        lines.append(f"· {item} {cfg['price']}金 — {cfg['desc']}")
    lines.append("━━━━━━━━━━")
    lines.append("购买：购买道具名（例：购买 免战牌）")
    lines.append("👑 贵族专享：购买其他道具一律 8 折")
    status = []
    row = db.get_buff(group_id, user_id, "免战牌")
    if row and row["count"] > 0 and row["expires_at"] > db.now():
        mins = max(1, (row["expires_at"] - db.now() + 59) // 60)
        status.append(f"🛡 免战中（剩 {mins} 分钟）")
    armed = [b for b in ATTACK_BUFFS if db.buff_count(group_id, user_id, b) > 0]
    if armed:
        status.append(f"⚔️ 已装备：{'/'.join(armed)}")
    if db.buff_count(group_id, user_id, "金钟罩") > 0:
        status.append("🛡 金钟罩护体")
    tok = db.buff_count(group_id, user_id, "小弟令牌")
    if tok:
        status.append(f"👨‍👦 小弟令牌 {tok}/{MAX_TOKENS}（上限 {follower_cap(group_id, user_id)} 人）")
    vrow = db.get_buff(group_id, user_id, "贵族")
    if vrow and vrow["count"] > 0 and vrow["expires_at"] > db.now():
        days = max(1, (vrow["expires_at"] - db.now() + 43200) // 86400)
        status.append(f"🔮 贵族剩余 {days} 天")
    if status:
        lines.append("你的状态：" + "；".join(status))
    return "\n".join(lines)


def buy(group_id: str, user_id: str, item_raw: str) -> str:
    item = (item_raw or "").strip().replace(" ", "")
    if item.startswith("购买"):
        item = item[2:]
    if item.lower() == "vip":
        item = "贵族"
    cfg = SHOP.get(item)
    if cfg is None:
        shelf = "\n".join(f"· {k} {v['price']}金" for k, v in SHOP.items())
        return f"🛒 没有这个道具，货架如下：\n{shelf}\n用法：购买道具名"
    p = db.ensure_player(group_id, user_id)
    base_price = cfg["price"]
    vip_now = db.buff_active(group_id, user_id, "贵族")
    price = int(base_price * 0.8) if (vip_now and item != "贵族") else base_price
    if p["coins"] < price:
        return f"❌ 余额不足：{item} 要 {price} 金" + ("（贵族8折）" if price < base_price else "") + f"，你只有 {p['coins']}。"
    # 占用校验（先验占用再扣钱，避免白扣）
    if item == "免战牌":
        row = db.get_buff(group_id, user_id, "免战牌")
        if row and row["count"] > 0 and row["expires_at"] > db.now():
            return "🛡 免战牌生效中，到期再买。"
    elif item in ATTACK_BUFFS:
        armed = [b for b in ATTACK_BUFFS if db.buff_count(group_id, user_id, b) > 0]
        if armed:
            return f"⚠️ 已装备 {'/'.join(armed)}，用掉后再买（攻击增益同时只留一个）。"
    elif item == "金钟罩":
        if db.buff_count(group_id, user_id, "金钟罩") > 0:
            return "🛡 金钟罩护体中，用掉后再买。"
    elif item == "小弟令牌":
        if db.buff_count(group_id, user_id, "小弟令牌") >= MAX_TOKENS:
            return f"⛔ 令牌已买满（{MAX_TOKENS} 枚）。"
    # 贵族：可叠加续期，不拦

    reason = f"购买{item}" + ("(贵族8折)" if price < base_price else "")
    db.transfer(group_id, user_id, SINK_USER, price, reason)
    if item == "免战牌":
        db.set_buff(group_id, user_id, "免战牌", 1, db.now() + BUFF_DUR)
    elif item == "贵族":
        row = db.get_buff(group_id, user_id, "贵族")
        base = max(db.now(), row["expires_at"] if row else 0)
        db.set_buff(group_id, user_id, "贵族", 1, base + VIP_DUR)
    elif item == "小弟令牌":
        db.set_buff(group_id, user_id, "小弟令牌",
                    db.buff_count(group_id, user_id, "小弟令牌") + 1, 0)
    else:
        db.set_buff(group_id, user_id, item, 1, 0)

    effect = {
        "免战牌": f"🛡 免战 {BUFF_DUR // 60} 分钟已生效",
        "必胜符": "⚔️ 已装备，下次攻击必胜",
        "双倍袋": "💰 已装备，下次攻击收益翻倍",
        "金钟罩": "🛡 已装备，下次失手赔付减半",
        "小弟令牌": (f"👨‍👦 令牌 {db.buff_count(group_id, user_id, '小弟令牌')}/{MAX_TOKENS}，"
                    f"小弟上限 → {follower_cap(group_id, user_id)} 人"),
        "贵族": "🔮 贵族身份 +7 天生效，每天首条消息触发驾到卡（再买可续期）",
    }[item]
    bal = db.ensure_player(group_id, user_id)["coins"]
    if price < base_price:
        cost = f"-{price} 金，贵族8折省 {base_price - price}"
    else:
        cost = f"-{price} 金币"
    return f"🛒 购买成功：{item}（{cost}）\n{effect}\n🏦 余额 {bal}"


# ---------- 婚姻 ----------

def propose(group_id: str, proposer: str, target: str) -> str:
    if proposer == target:
        return "❌ 不能和自己求婚。"
    now_ts = int(time.time())
    last = db.last_action_ts(group_id, proposer, "求婚")
    if now_ts - last < MARRIAGE_CD:
        return "⏳ 求婚冷却中，30 分钟后再来。"
    if db.today_count(group_id, proposer, "求婚", day_start_ts()) >= MARRIAGE_DAILY:
        return "⛔ 今日求婚次数已用完。"
    if db.has_pending_from(group_id, proposer, "marriage", REQUEST_EXPIRE):
        return "⏳ 你已有一个待确认的求婚。"
    if db.get_marriage(group_id, proposer):
        return "❌ 你已经结婚了，先「离婚」。"
    if db.get_marriage(group_id, target):
        return f"❌ {name(group_id, target)} 已经名花有主了。"
    p = db.ensure_player(group_id, proposer)
    if p["coins"] < RING_COST:
        return f"❌ 求婚需要 {RING_COST} 金币买戒指，你只有 {p['coins']}。"

    db.log_action(group_id, proposer, "求婚", target)
    db.create_request(group_id, "marriage", proposer, target)
    return (
        f"💍 {name(group_id, proposer)} 向 {name(group_id, target)} 求婚！\n"
        f"🎩 戒指钱 {RING_COST} 金币（对象收 {RING_GIFT} 红包）\n"
        f"{name(group_id, target)} 回复「同意」或「拒绝」。"
    )


# ---------- 请求确认（同意/拒绝 统一入口） ----------

def resolve(group_id: str, responder: str, accept: bool) -> str:
    auto_accept_recruits(group_id)  # 超 10 分钟未回的请求先按自动同意落账
    req = db.latest_pending_to(group_id, responder, REQUEST_EXPIRE)
    if req is None:
        return "没有待你确认的请求。"

    if req["kind"] == "recruit":
        owner = req["from_user"]
        if accept:
            if len(db.followers_of(group_id, owner)) >= follower_cap(group_id, owner):
                db.set_request_status(req["id"], "rejected")
                return f"❌ {name(group_id, owner)} 的小弟名额刚刚满了，作罢。"
            if db.get_follower(group_id, responder):
                db.set_request_status(req["id"], "rejected")
                return "❌ 你已经有大哥了。"
            db.add_follower(group_id, responder, owner)
            db.set_request_status(req["id"], "accepted")
            return f"🎉 {name(group_id, responder)} 拜入 {name(group_id, owner)} 门下，小弟+1！\n发「小弟打工」安排活儿。"
        db.set_request_status(req["id"], "rejected")
        return f"🙅 {name(group_id, responder)} 拒绝了 {name(group_id, owner)} 的收徒请求。"

    if req["kind"] == "marriage":
        proposer = req["from_user"]
        if accept:
            if db.get_marriage(group_id, proposer) or db.get_marriage(group_id, responder):
                db.set_request_status(req["id"], "rejected")
                return "❌ 一方已另嫁/另娶，求婚作罢。"
            p = db.ensure_player(group_id, proposer)
            if p["coins"] < RING_COST:
                db.set_request_status(req["id"], "rejected")
                return f"❌ {name(group_id, proposer)} 的金币不够买戒指了，求婚作罢。"
            db.transfer(group_id, proposer, responder, RING_GIFT, "新婚红包")
            db.transfer(group_id, proposer, SINK_USER, RING_COST - RING_GIFT, "求婚戒指(销毁)")
            db.marry(group_id, proposer, responder)
            db.set_request_status(req["id"], "accepted")
            return (
                f"🎉 {name(group_id, proposer)} 和 {name(group_id, responder)} 结为伴侣！\n"
                f"💸 {name(group_id, responder)} 收到 {RING_GIFT} 金币红包，{RING_COST - RING_GIFT} 金币化作戒指烟火 ✨"
            )
        db.set_request_status(req["id"], "rejected")
        return f"💔 {name(group_id, responder)} 婉拒了 {name(group_id, proposer)} 的求婚……"


def divorce(group_id: str, user_id: str) -> str:
    if db.divorce(group_id, user_id):
        return f"💔 {name(group_id, user_id)} 解除婚约，恢复自由身。"
    return "你目前未婚。"
