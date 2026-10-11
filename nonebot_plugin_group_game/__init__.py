"""群互动游戏 - NoneBot2 插件入口（OneBot v12 原生）。

命令文本依赖桥侧把 "@昵称\\u2005" 标记从文本里剥掉（只留 mention 段），
因此这里拿到的 plaintext 天然以命令词开头；配合 COMMAND_START 含空串，
群里裸发「签到」即可触发。
"""
import asyncio

import nonebot
from nonebot import on_command, on_message, logger, get_driver
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v12 import GroupMessageEvent, Message, MessageEvent

from nonebot.plugin import PluginMetadata

from . import db, game

__plugin_meta__ = PluginMetadata(
    name="群互动游戏",
    description="微信群互动小游戏合集：签到、打劫、劫色、收小弟打工、求婚、商店、贵族、银行、转账、刮刮乐、成语接龙、排行榜",
    usage="发送「游戏帮助」查看全部指令；裸发指令即可触发（COMMAND_START 需含空串）",
    type="application",
    homepage="https://github.com/2212018862/nonebot-plugin-group-game",
    supported_adapters={"onebot.v12"},
    extra={"author": "yz", "priority": 5},
)

# ---------- helpers ----------


def mention_target(event: GroupMessageEvent) -> str | None:
    """取消息里被 @ 的目标 wxid（排除机器人自己）。"""
    bot_id = ""
    try:
        bot_id = (event.self.user_id if event.self else "") or ""
    except Exception:
        pass
    for seg in event.get_message():
        if seg.type == "mention":
            uid = str(seg.data.get("user_id") or "")
            if uid and uid != bot_id:
                return uid
    return None


def group_only(event: MessageEvent) -> bool:
    """非群聊时先回一句并终止（返回 False 表示已处理）。"""
    return isinstance(event, GroupMessageEvent)


# ---------- matchers ----------

signin = on_command("签到", aliases={"每日签到", "打卡"}, priority=5, block=True)
wallet_cmd = on_command("我的金币", aliases={"我的信息", "钱包"}, priority=5, block=True)
help_cmd = on_command("游戏帮助", aliases={"帮助", "菜单"}, priority=5, block=True)

rob = on_command("打劫", aliases={"抢劫"}, priority=5, block=True)
charm_cmd = on_command("劫色", priority=5, block=True)

recruit_cmd = on_command("收小弟", aliases={"招小弟"}, priority=5, block=True)
work_cmd = on_command("小弟打工", aliases={"打工"}, priority=5, block=True)
settle_cmd = on_command("小弟收工", aliases={"收工"}, priority=5, block=True)

transfer_cmd = on_command("转账", aliases={"打钱"}, priority=5, block=True)

bank_cmd = on_command("银行", aliases={"我的存款"}, priority=5, block=True)
deposit_cmd = on_command("存款", priority=5, block=True)
withdraw_cmd = on_command("取款", priority=5, block=True)
rob_bank_cmd = on_command("抢银行", aliases={"抢劫银行"}, priority=5, block=True)

idiom_cmd = on_command("成语接龙", aliases={"接龙"}, priority=5, block=True)
leaderboard_cmd = on_command("排行榜", aliases={"排行", "金币排行"}, priority=5, block=True)

lottery_cmd = on_command("刮刮乐", aliases={"彩票"}, priority=5, block=True)
lottery_buy_cmd = on_command("购买刮刮乐", aliases={"买彩票", "刮一张"}, priority=5, block=True)

shop_cmd = on_command("商店", aliases={"购物", "商城"}, priority=5, block=True)
buy_cmd = on_command("购买", priority=5, block=True)

release_cmd = on_command("放生", aliases={"放生小弟"}, priority=5, block=True)
redeem_cmd = on_command("赎身", aliases={"脱离", "退出帮派"}, priority=5, block=True)

propose_cmd = on_command("求婚", priority=5, block=True)
divorce_cmd = on_command("离婚", priority=5, block=True)

accept_cmd = on_command("同意", aliases={"接受", "答应"}, priority=5, block=True)
reject_cmd = on_command("拒绝", aliases={"不了"}, priority=5, block=True)


# ---------- handlers ----------

@signin.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await signin.finish("该指令仅群聊可用")
    await signin.finish(game.do_sign(event.group_id, event.user_id))


@wallet_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await wallet_cmd.finish("该指令仅群聊可用")
    await wallet_cmd.finish(game.wallet(event.group_id, event.user_id))


@help_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await help_cmd.finish("该指令仅群聊可用")
    await help_cmd.finish(game.HELP_TEXT)


def _atk_handler(matcher, kind: str):
    """打劫：校验 @ 目标后交给战斗结算器。"""

    async def _(event: MessageEvent):
        if not group_only(event):
            await matcher.finish("该指令仅群聊可用")
        target = mention_target(event)
        if not target:
            await matcher.finish(f"请@要{kind}的对象，如：{kind}@张三")
        await matcher.finish(game.attack(event.group_id, event.user_id, target, kind))

    matcher.handle()(_)


_ = _atk_handler(rob, "打劫")


@charm_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await charm_cmd.finish("该指令仅群聊可用")
    target = mention_target(event)
    if not target:
        await charm_cmd.finish("请@要劫色的对象，如：劫色@小红")
    await charm_cmd.finish(game.charm_rob(event.group_id, event.user_id, target))


@recruit_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await recruit_cmd.finish("该指令仅群聊可用")
    target = mention_target(event)
    if not target:
        await recruit_cmd.finish("请@要收的小弟，如：收小弟@张三")
    await recruit_cmd.finish(game.recruit(event.group_id, event.user_id, target))


@work_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await work_cmd.finish("该指令仅群聊可用")
    await work_cmd.finish(game.work(event.group_id, event.user_id))


@settle_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await settle_cmd.finish("该指令仅群聊可用")
    await settle_cmd.finish(game.settle(event.group_id, event.user_id))


@transfer_cmd.handle()
async def _(event: MessageEvent, cmd_arg: Message = CommandArg()):
    if not group_only(event):
        await transfer_cmd.finish("该指令仅群聊可用")
    target = mention_target(event)
    if not target:
        await transfer_cmd.finish("请@收款人，如：转账 100 @张三")
    await transfer_cmd.finish(
        game.transfer_coins(event.group_id, event.user_id, target, cmd_arg.extract_plain_text())
    )


@bank_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await bank_cmd.finish("该指令仅群聊可用")
    await bank_cmd.finish(game.bank_info(event.group_id, event.user_id))


@deposit_cmd.handle()
async def _(event: MessageEvent, cmd_arg: Message = CommandArg()):
    if not group_only(event):
        await deposit_cmd.finish("该指令仅群聊可用")
    await deposit_cmd.finish(game.deposit(event.group_id, event.user_id, cmd_arg.extract_plain_text()))


@withdraw_cmd.handle()
async def _(event: MessageEvent, cmd_arg: Message = CommandArg()):
    if not group_only(event):
        await withdraw_cmd.finish("该指令仅群聊可用")
    await withdraw_cmd.finish(game.withdraw(event.group_id, event.user_id, cmd_arg.extract_plain_text()))


@rob_bank_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await rob_bank_cmd.finish("该指令仅群聊可用")
    await rob_bank_cmd.finish(game.rob_bank(event.group_id, event.user_id))


@idiom_cmd.handle()
async def _(event: MessageEvent, cmd_arg: Message = CommandArg()):
    if not group_only(event):
        await idiom_cmd.finish("该指令仅群聊可用")
    await idiom_cmd.finish(
        game.idiom(event.group_id, event.user_id, cmd_arg.extract_plain_text())
    )


@leaderboard_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await leaderboard_cmd.finish("该指令仅群聊可用")
    await leaderboard_cmd.finish(game.leaderboard(event.group_id, event.user_id))


@lottery_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await lottery_cmd.finish("该指令仅群聊可用")
    msgs = game.lottery_command(event.group_id)
    for m in msgs[:-1]:
        await lottery_cmd.send(m)
    await lottery_cmd.finish(msgs[-1])


@lottery_buy_cmd.handle()
async def _(event: MessageEvent, cmd_arg: Message = CommandArg()):
    if not group_only(event):
        await lottery_buy_cmd.finish("该指令仅群聊可用")
    await lottery_buy_cmd.finish(
        game.lottery_buy(event.group_id, event.user_id, cmd_arg.extract_plain_text())
    )


@shop_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await shop_cmd.finish("该指令仅群聊可用")
    await shop_cmd.finish(game.shop_list(event.group_id, event.user_id))


@buy_cmd.handle()
async def _(event: MessageEvent, cmd_arg: Message = CommandArg()):
    if not group_only(event):
        await buy_cmd.finish("该指令仅群聊可用")
    reply = game.buy(event.group_id, event.user_id, cmd_arg.extract_plain_text())
    if reply.startswith("🛒 购买成功：贵族"):
        # 购买成功先回购买结果，紧接着立即补发一次驾到卡
        await buy_cmd.send(reply)
        card = game.vip_card_now(event.group_id, event.user_id)
        if card:
            await buy_cmd.finish(card)
    await buy_cmd.finish(reply)


# 贵族：每日首次发言（任意群消息，非命令也触发）→ 驾到卡；
# 优先级低于命令、不阻塞，命令回复与驾到卡互不影响
vip_greet = on_message(priority=10, block=False)


@vip_greet.handle()
async def _(event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    card = game.vip_greeting(event.group_id, event.user_id)
    if card:
        await vip_greet.send(card)


@release_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await release_cmd.finish("该指令仅群聊可用")
    target = mention_target(event)
    if not target:
        await release_cmd.finish("请@要放生的小弟，如：放生@张三")
    await release_cmd.finish(game.release(event.group_id, event.user_id, target))


@redeem_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await redeem_cmd.finish("该指令仅群聊可用")
    await redeem_cmd.finish(game.redeem(event.group_id, event.user_id))


@propose_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await propose_cmd.finish("该指令仅群聊可用")
    target = mention_target(event)
    if not target:
        await propose_cmd.finish("请@求婚对象，如：求婚@小红")
    await propose_cmd.finish(game.propose(event.group_id, event.user_id, target))


@divorce_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await divorce_cmd.finish("该指令仅群聊可用")
    await divorce_cmd.finish(game.divorce(event.group_id, event.user_id))


@accept_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await accept_cmd.finish("该指令仅群聊可用")
    await accept_cmd.finish(game.resolve(event.group_id, event.user_id, accept=True))


@reject_cmd.handle()
async def _(event: MessageEvent):
    if not group_only(event):
        await reject_cmd.finish("该指令仅群聊可用")
    await reject_cmd.finish(game.resolve(event.group_id, event.user_id, accept=False))


# ---------- 巡检（唯一的定时逻辑：30s 自愈循环 + 开机补跑）—— 刮刮乐结算 + 成语接龙超时 ----------

async def _broadcast(group_id: str, text: str) -> bool:
    """向群发公告；任一 bot 发送成功即算成功。"""
    sent = False
    for bot in nonebot.get_bots().values():
        try:
            await bot.call_api(
                "send_message",
                detail_type="group",
                group_id=group_id,
                message=[{"type": "text", "data": {"text": text}}],
            )
            sent = True
        except Exception as e:
            logger.opt(exception=e).warning(f"[group_game] 公告发送失败 group={group_id}")
    return sent


async def _patrol_loop():
    await asyncio.sleep(5)  # 等 bot 连接就绪
    while True:
        try:
            # 先公告、后烧池：发送失败（掉线）则本轮下个周期重试，公告绝不丢
            for gid, rnd in game.lottery_expired_rounds():
                if await _broadcast(gid, game.lottery_announce(gid, rnd)):
                    game.lottery_finalize(gid, rnd)
                    logger.info(f"[group_game] 刮刮乐轮次结算 group={gid}")
        except Exception as e:
            logger.opt(exception=e).error("[group_game] 刮刮乐巡检异常")
        try:
            # 接龙超时：先公告、后清局（同重试语义）
            for gid, msg, _starter in game.idiom_broadcast_tasks(db.now()):
                if await _broadcast(gid, msg):
                    db.idiom_delete(gid)
                    logger.info(f"[group_game] 成语接龙超时结束 group={gid}")
        except Exception as e:
            logger.opt(exception=e).error("[group_game] 接龙巡检异常")
        await asyncio.sleep(30)


async def _start_patrol():
    asyncio.create_task(_patrol_loop())


get_driver().on_startup(_start_patrol)


# 初始化建库（数据文件在挂载卷 /app/data 下）
try:
    db.conn()
    logger.opt(colors=True).info(f"<y>[group_game]</y> 数据库就绪: {db.DB_PATH}")
except Exception as e:
    logger.opt(exception=e).error("[group_game] 数据库初始化失败")
