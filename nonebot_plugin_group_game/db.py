"""群互动游戏 - SQLite 存储层。

数据文件默认 <运行目录>/data/group_game.db，可用环境变量 GROUP_GAME_DB 覆盖。
单进程 + 事件循环内同步调用，毫秒级操作；写路径统一加锁。
"""
import sqlite3
import threading
import time
from pathlib import Path

# 数据文件：优先环境变量 GROUP_GAME_DB；默认 <运行目录>/data/group_game.db
# （NoneBot 项目惯例为项目根/data，与本插件开发时的挂载路径一致）
import os

DB_PATH = Path(os.environ.get("GROUP_GAME_DB")) if os.environ.get("GROUP_GAME_DB") \
    else Path.cwd() / "data" / "group_game.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS players(
  group_id     TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  coins        INTEGER NOT NULL DEFAULT 100,
  last_sign_date TEXT NOT NULL DEFAULT '',
  sign_streak  INTEGER NOT NULL DEFAULT 0,
  total_sign   INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS ledger(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  user_id  TEXT NOT NULL,
  delta    INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  reason   TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_gu ON ledger(group_id, user_id, id);
CREATE TABLE IF NOT EXISTS action_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  action     TEXT NOT NULL,
  target_id  TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action ON action_log(group_id, user_id, action, created_at);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id   TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- recruit | marriage
  from_user  TEXT NOT NULL,
  to_user    TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending'  -- pending/accepted/rejected/expired
);
CREATE INDEX IF NOT EXISTS idx_req_to ON requests(group_id, to_user, status);
CREATE TABLE IF NOT EXISTS followers(
  group_id    TEXT NOT NULL,
  member_user TEXT NOT NULL,
  owner_user  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'idle',   -- idle/working
  started_at  INTEGER NOT NULL DEFAULT 0,
  joined_at   INTEGER NOT NULL,
  PRIMARY KEY (group_id, member_user)
);
DROP TABLE IF EXISTS work_daily;
CREATE TABLE IF NOT EXISTS idiom_games(
  group_id  TEXT PRIMARY KEY,
  starter   TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  last_at   INTEGER NOT NULL,
  chain     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lottery_rounds(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id   TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ends_at    INTEGER NOT NULL,
  status     TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_lot_round ON lottery_rounds(group_id, status);
CREATE TABLE IF NOT EXISTS lottery_tickets(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id   INTEGER NOT NULL,
  group_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  tier       TEXT NOT NULL DEFAULT '',
  prize      INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lot_tk ON lottery_tickets(round_id);
CREATE TABLE IF NOT EXISTS bank(
  group_id         TEXT NOT NULL,
  user_id          TEXT NOT NULL,
  balance          INTEGER NOT NULL DEFAULT 0,
  last_interest_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS vip_state(
  group_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  last_greet TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS buffs(
  group_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  buff       TEXT NOT NULL,
  count      INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_id, user_id, buff)
);
CREATE TABLE IF NOT EXISTS marriages(
  group_id TEXT NOT NULL,
  user_a   TEXT NOT NULL,
  user_b   TEXT NOT NULL,
  since    INTEGER NOT NULL,
  PRIMARY KEY (group_id, user_a)
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=3000")
        _conn.executescript(SCHEMA)
        # 老库迁移：玩家魅力值列
        cols = {row[1] for row in _conn.execute("PRAGMA table_info(players)")}
        if "charm" not in cols:
            _conn.execute("ALTER TABLE players ADD COLUMN charm INTEGER NOT NULL DEFAULT 0")
        _conn.commit()
    return _conn


def now() -> int:
    return int(time.time())


# ---------- 玩家 ----------

def ensure_player(group_id: str, user_id: str, default_coins: int = 100) -> sqlite3.Row:
    with _lock:
        c = conn()
        c.execute(
            "INSERT OR IGNORE INTO players(group_id, user_id, coins, created_at) VALUES(?,?,?,?)",
            (group_id, user_id, default_coins, now()),
        )
        c.commit()
        return c.execute(
            "SELECT * FROM players WHERE group_id=? AND user_id=?", (group_id, user_id)
        ).fetchone()


def update_player(group_id: str, user_id: str, **fields) -> None:
    keys = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        c = conn()
        c.execute(
            f"UPDATE players SET {keys} WHERE group_id=? AND user_id=?",
            (*fields.values(), group_id, user_id),
        )
        c.commit()


def transfer(group_id: str, src: str, dst: str, amount: int, reason: str) -> None:
    """src -> dst 金币转移，双侧写流水；行不存在时以 0 余额补建（防事务悬挂）。"""
    if amount <= 0:
        return
    with _lock:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            for uid in (src, dst):
                c.execute(
                    "INSERT OR IGNORE INTO players(group_id, user_id, coins, created_at)"
                    " VALUES(?,?,0,?)",
                    (group_id, uid, now()),
                )
            cur = c.execute(
                "UPDATE players SET coins = coins - ? WHERE group_id=? AND user_id=? AND coins >= ?",
                (amount, group_id, src, amount),
            )
            if cur.rowcount != 1:
                c.execute("ROLLBACK")
                return
            c.execute(
                "UPDATE players SET coins = coins + ? WHERE group_id=? AND user_id=?",
                (amount, group_id, dst),
            )
            ts = now()
            for uid, delta in ((src, -amount), (dst, amount)):
                bal = c.execute(
                    "SELECT coins FROM players WHERE group_id=? AND user_id=?",
                    (group_id, uid),
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO ledger(group_id,user_id,delta,balance_after,reason,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (group_id, uid, delta, bal, reason, ts),
                )
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def mint(group_id: str, user_id: str, amount: int, reason: str) -> None:
    """系统发放（签到/打工收益），写流水；行不存在时以 0 余额补建。"""
    if amount <= 0:
        return
    with _lock:
        c = conn()
        c.execute(
            "INSERT OR IGNORE INTO players(group_id, user_id, coins, created_at) VALUES(?,?,0,?)",
            (group_id, user_id, now()),
        )
        c.execute(
            "UPDATE players SET coins = coins + ? WHERE group_id=? AND user_id=?",
            (amount, group_id, user_id),
        )
        bal = c.execute(
            "SELECT coins FROM players WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO ledger(group_id,user_id,delta,balance_after,reason,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (group_id, user_id, amount, bal, reason, now()),
        )
        c.commit()


def ledger_tail(group_id: str, user_id: str, n: int = 5) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(
            "SELECT delta, balance_after, reason, created_at FROM ledger"
            " WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (group_id, user_id, n),
        ).fetchall()


# ---------- 冷却 / 限额 ----------

def last_action_ts(group_id: str, user_id: str, action: str) -> int:
    row = conn().execute(
        "SELECT created_at FROM action_log WHERE group_id=? AND user_id=? AND action=?"
        " ORDER BY created_at DESC LIMIT 1",
        (group_id, user_id, action),
    ).fetchone()
    return row[0] if row else 0


def today_count(group_id: str, user_id: str, action: str, day_start: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM action_log WHERE group_id=? AND user_id=? AND action=?"
        " AND created_at >= ?",
        (group_id, user_id, action, day_start),
    ).fetchone()[0]


def pair_attack_ts(group_id: str, attacker: str) -> int:
    """攻击者最近一次出手时间（打劫/劫色共享）。"""
    row = conn().execute(
        "SELECT created_at FROM action_log WHERE group_id=? AND user_id=?"
        " AND action IN ('打劫','劫色') ORDER BY created_at DESC LIMIT 1",
        (group_id, attacker),
    ).fetchone()
    return row[0] if row else 0


def log_action(group_id: str, user_id: str, action: str, target_id: str = "") -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO action_log(group_id,user_id,action,target_id,created_at)"
            " VALUES(?,?,?,?,?)",
            (group_id, user_id, action, target_id, now()),
        )
        c.commit()


# ---------- 请求（收小弟/求婚的两步确认） ----------

def create_request(group_id: str, kind: str, from_user: str, to_user: str) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO requests(group_id,kind,from_user,to_user,created_at) VALUES(?,?,?,?,?)",
            (group_id, kind, from_user, to_user, now()),
        )
        c.commit()


def latest_pending_to(group_id: str, to_user: str, expire_sec: int) -> sqlite3.Row | None:
    row = conn().execute(
        "SELECT * FROM requests WHERE group_id=? AND to_user=? AND status='pending'"
        " ORDER BY id DESC LIMIT 1",
        (group_id, to_user),
    ).fetchone()
    if row is None:
        return None
    if now() - row["created_at"] > expire_sec:
        set_request_status(row["id"], "expired")
        return None
    return row


def overdue_recruits(group_id: str, cutoff_ts: int) -> list[sqlite3.Row]:
    """创建时间早于 cutoff_ts 且仍未处理的收小弟请求（用于超时自动同意）。"""
    return conn().execute(
        "SELECT * FROM requests WHERE group_id=? AND kind='recruit'"
        " AND status='pending' AND created_at <= ?",
        (group_id, cutoff_ts),
    ).fetchall()


def set_request_status(req_id: int, status: str) -> None:
    with _lock:
        c = conn()
        c.execute("UPDATE requests SET status=? WHERE id=?", (status, req_id))
        c.commit()


def has_pending_from(group_id: str, from_user: str, kind: str, expire_sec: int) -> bool:
    row = conn().execute(
        "SELECT created_at FROM requests WHERE group_id=? AND from_user=? AND kind=?"
        " AND status='pending' ORDER BY id DESC LIMIT 1",
        (group_id, from_user, kind),
    ).fetchone()
    if row is None:
        return False
    if now() - row[0] > expire_sec:
        return False
    return True


# ---------- 小弟 ----------

def get_follower(group_id: str, member_user: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM followers WHERE group_id=? AND member_user=?",
        (group_id, member_user),
    ).fetchone()


def followers_of(group_id: str, owner_user: str) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM followers WHERE group_id=? AND owner_user=?",
        (group_id, owner_user),
    ).fetchall()


def add_follower(group_id: str, member_user: str, owner_user: str) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT OR REPLACE INTO followers(group_id,member_user,owner_user,status,started_at,joined_at)"
            " VALUES(?,?,?,'idle',0,?)",
            (group_id, member_user, owner_user, now()),
        )
        c.commit()


def set_follower_status(group_id: str, member_user: str, status: str, started_at: int) -> None:
    with _lock:
        c = conn()
        c.execute(
            "UPDATE followers SET status=?, started_at=? WHERE group_id=? AND member_user=?",
            (status, started_at, group_id, member_user),
        )
        c.commit()


def remove_follower(group_id: str, member_user: str) -> None:
    with _lock:
        c = conn()
        c.execute(
            "DELETE FROM followers WHERE group_id=? AND member_user=?",
            (group_id, member_user),
        )
        c.commit()


# ---------- 婚姻 ----------

def get_marriage(group_id: str, user_id: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM marriages WHERE group_id=? AND (user_a=? OR user_b=?)",
        (group_id, user_id, user_id),
    ).fetchone()


def marry(group_id: str, a: str, b: str) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO marriages(group_id,user_a,user_b,since) VALUES(?,?,?,?)",
            (group_id, a, b, now()),
        )
        c.commit()


def divorce(group_id: str, user_id: str) -> bool:
    with _lock:
        c = conn()
        cur = c.execute(
            "DELETE FROM marriages WHERE group_id=? AND (user_a=? OR user_b=?)",
            (group_id, user_id, user_id),
        )
        c.commit()
        return cur.rowcount > 0


# ---------- 商店 buff ----------

def lottery_open_round(group_id: str, started_at: int, ends_at: int) -> int:
    with _lock:
        c = conn()
        cur = c.execute(
            "INSERT INTO lottery_rounds(group_id,started_at,ends_at) VALUES(?,?,?)",
            (group_id, started_at, ends_at),
        )
        c.commit()
        return cur.lastrowid


def lottery_active_round(group_id: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM lottery_rounds WHERE group_id=? AND status='open'"
        " ORDER BY id DESC LIMIT 1",
        (group_id,),
    ).fetchone()


def lottery_close_round(round_id: int) -> None:
    with _lock:
        c = conn()
        c.execute(
            "UPDATE lottery_rounds SET status='closed' WHERE id=? AND status='open'",
            (round_id,),
        )
        c.commit()


def lottery_add_ticket(round_id: int, group_id: str, user_id: str, tier: str, prize: int) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO lottery_tickets(round_id,group_id,user_id,tier,prize,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (round_id, group_id, user_id, tier, prize, now()),
        )
        c.commit()


def lottery_stats(round_id: dict | object) -> tuple:
    """返回 (售出张数, 购买人数, 中奖人数)。"""
    row = conn().execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS u FROM lottery_tickets WHERE round_id=?",
        (round_id,),
    ).fetchone()
    win = conn().execute(
        "SELECT COUNT(DISTINCT user_id) AS w FROM lottery_tickets WHERE round_id=? AND prize>0",
        (round_id,),
    ).fetchone()
    return row["n"], row["u"], win["w"]


def get_bank(group_id: str, user_id: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM bank WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()


def bank_upsert(group_id: str, user_id: str, balance: int, last_interest_at: int) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO bank(group_id,user_id,balance,last_interest_at) VALUES(?,?,?,?)"
            " ON CONFLICT(group_id,user_id) DO UPDATE SET"
            " balance=excluded.balance, last_interest_at=excluded.last_interest_at",
            (group_id, user_id, balance, last_interest_at),
        )
        c.commit()


def add_charm(group_id: str, user_id: str, n: int) -> int:
    """魅力值累加（不走金币流水），返回新总值。"""
    with _lock:
        c = conn()
        c.execute(
            "INSERT OR IGNORE INTO players(group_id,user_id,coins,charm,created_at) VALUES(?,?,0,?,?)",
            (group_id, user_id, n, now()),
        )
        c.execute(
            "UPDATE players SET charm = charm + ? WHERE group_id=? AND user_id=?",
            (n, group_id, user_id),
        )
        total = c.execute(
            "SELECT charm FROM players WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()[0]
        c.commit()
        return total


def wallet_delta(group_id: str, user_id: str, delta: int, reason: str) -> bool:
    """直接增减余额并写流水；扣成负数则整体回滚返回 False。"""
    if delta == 0:
        return True
    with _lock:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT OR IGNORE INTO players(group_id,user_id,coins,created_at) VALUES(?,?,0,?)",
                (group_id, user_id, now()),
            )
            cur = c.execute(
                "UPDATE players SET coins = coins + ? WHERE group_id=? AND user_id=? AND coins + ? >= 0",
                (delta, group_id, user_id, delta),
            )
            if cur.rowcount != 1:
                c.execute("ROLLBACK")
                return False
            bal = c.execute(
                "SELECT coins FROM players WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            ).fetchone()[0]
            c.execute(
                "INSERT INTO ledger(group_id,user_id,delta,balance_after,reason,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (group_id, user_id, delta, bal, reason, now()),
            )
            c.execute("COMMIT")
            return True
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def idiom_get(group_id: str) -> dict | None:
    row = conn().execute(
        "SELECT starter, started_at, last_at, chain FROM idiom_games WHERE group_id=?",
        (group_id,),
    ).fetchone()
    if not row:
        return None
    import json as _json
    return {
        "starter": row["starter"],
        "started_at": row["started_at"],
        "last_at": row["last_at"],
        "chain": _json.loads(row["chain"]),
    }


def idiom_put(group_id: str, st: dict) -> None:
    import json as _json
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO idiom_games(group_id,starter,started_at,last_at,chain) VALUES(?,?,?,?,?)"
            " ON CONFLICT(group_id) DO UPDATE SET starter=excluded.starter,"
            " started_at=excluded.started_at, last_at=excluded.last_at, chain=excluded.chain",
            (group_id, st["starter"], st["started_at"], st["last_at"],
             _json.dumps(st["chain"], ensure_ascii=False)),
        )
        c.commit()


def idiom_delete(group_id: str) -> None:
    with _lock:
        c = conn()
        c.execute("DELETE FROM idiom_games WHERE group_id=?", (group_id,))
        c.commit()


def idiom_expired(cutoff_ts: int) -> list:
    rows = conn().execute(
        "SELECT group_id, starter, started_at, last_at, chain FROM idiom_games WHERE last_at < ?",
        (cutoff_ts,),
    ).fetchall()
    import json as _json
    return [
        {"group_id": r["group_id"], "starter": r["starter"],
         "started_at": r["started_at"], "last_at": r["last_at"],
         "chain": _json.loads(r["chain"])}
        for r in rows
    ]


def vip_last_greet(group_id: str, user_id: str) -> str:
    row = conn().execute(
        "SELECT last_greet FROM vip_state WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()
    return row[0] if row else ""


def set_vip_greet(group_id: str, user_id: str, date_str: str) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO vip_state(group_id,user_id,last_greet) VALUES(?,?,?)"
            " ON CONFLICT(group_id,user_id) DO UPDATE SET last_greet=excluded.last_greet",
            (group_id, user_id, date_str),
        )
        c.commit()


def get_buff(group_id: str, user_id: str, buff: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM buffs WHERE group_id=? AND user_id=? AND buff=?",
        (group_id, user_id, buff),
    ).fetchone()


def buff_count(group_id: str, user_id: str, buff: str) -> int:
    row = get_buff(group_id, user_id, buff)
    return row["count"] if row else 0


def buff_active(group_id: str, user_id: str, buff: str) -> bool:
    """count>0 且未过期（expires_at=0 表示不过期）。"""
    row = get_buff(group_id, user_id, buff)
    if not row or row["count"] <= 0:
        return False
    return row["expires_at"] == 0 or row["expires_at"] > now()


def set_buff(group_id: str, user_id: str, buff: str, count: int, expires_at: int = 0) -> None:
    with _lock:
        c = conn()
        if count <= 0:
            c.execute(
                "DELETE FROM buffs WHERE group_id=? AND user_id=? AND buff=?",
                (group_id, user_id, buff),
            )
        else:
            c.execute(
                "INSERT INTO buffs(group_id,user_id,buff,count,expires_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(group_id,user_id,buff) DO UPDATE SET count=excluded.count, expires_at=excluded.expires_at",
                (group_id, user_id, buff, count, expires_at),
            )
        c.commit()


def consume_buff(group_id: str, user_id: str, buff: str) -> bool:
    """用掉一层；有可用层则扣减并返回 True。"""
    if not buff_active(group_id, user_id, buff):
        return False
    with _lock:
        c = conn()
        row = c.execute(
            "SELECT count FROM buffs WHERE group_id=? AND user_id=? AND buff=?",
            (group_id, user_id, buff),
        ).fetchone()
        if not row or row[0] <= 0:
            return False
        if row[0] - 1 <= 0:
            c.execute(
                "DELETE FROM buffs WHERE group_id=? AND user_id=? AND buff=?",
                (group_id, user_id, buff),
            )
        else:
            c.execute(
                "UPDATE buffs SET count=count-1 WHERE group_id=? AND user_id=? AND buff=?",
                (group_id, user_id, buff),
            )
        c.commit()
        return True
