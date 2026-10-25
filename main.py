"""娶群友：在指定群内随机抽取一位群友作为“群老婆”，并持久累积好感度。

触发与行为：
- 只在白名单群内、消息精确等于触发词（默认「娶群友」）时触发；
- 机器人自己与其他 bot 的消息不触发；
- 每人每天只能抽一次，同一天重复触发只返回「你已经娶过了」并随机调侃一句；
- 好感度按「发起人:被抽人」持久化，每次随机增加 gain_min~gain_max，无上限、不衰减。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "marry_groupmate"
DEFAULT_TRIGGER = "娶群友"
DEFAULT_GROUP_IDS: list[int] = []
DEFAULT_GAIN_MIN = 1
DEFAULT_GAIN_MAX = 5
AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"
MEMBER_CACHE_TTL = 300.0
CHINA_TZ = timezone(timedelta(hours=8))

# 标记「这条结果由本插件产生」，避免结果守卫误拦自己的消息。
OWN_RESULT_KEY = "marry_groupmate:own_result"
# 大模型模仿模板后会吐出这些特征串，命中即判定为伪造消息。
IMITATION_PATTERN = re.compile(r"今天你的群老婆是|当前你们好感度为|你已经娶过了")

DEFAULT_REPEAT_LINES = [
    "一天想娶几个？QQ 群不是后宫。",
    "急什么，明天再来排队。",
    "你这不是娶群友，是批发。",
    "月老说你今天的额度用完了。",
    "贪心的人好感度不涨哦。",
    "重婚罪了解一下。",
    "建议先照照镜子，冷静一下。",
    "明天同一时间，准时开奖。",
    "你这属于把群当民政局了。",
    "今天这位还热乎着，别惦记别人。",
    "再抽也还是同一位，别试了。",
    "你的群老婆正在看着你，注意言行。",
    "地主家也没余粮了，明天请早。",
    "月老已下班，明早准时上班。",
    "这么着急，是怕人跑了吗？",
]


def _today() -> str:
    """返回北京时间（UTC+8）的日期字符串，用于“每人每天一次”判定。"""
    return datetime.now(CHINA_TZ).strftime("%Y-%m-%d")


def _new_state() -> dict:
    """构造一份空状态。"""
    return {"version": 1, "last_draw": {}, "pairs": {}}


def _load_state(path: Path) -> dict:
    """读取状态文件；文件缺失或损坏时返回空状态，不抛异常。

    Args:
        path: 状态文件路径。

    Returns:
        包含 last_draw（发起人 -> 日期）与 pairs（"发起人:被抽人" -> 好感度）的字典。
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _new_state()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[{PLUGIN_NAME}] 状态文件读取失败，已重置: {e}")
        return _new_state()

    if not isinstance(raw, dict):
        return _new_state()

    state = _new_state()
    if isinstance(raw.get("last_draw"), dict):
        state["last_draw"] = {
            str(k): str(v) for k, v in raw["last_draw"].items() if k and v
        }
    if isinstance(raw.get("pairs"), dict):
        pairs = {}
        for k, v in raw["pairs"].items():
            try:
                pairs[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        state["pairs"] = pairs
    return state


def _save_state(path: Path, state: dict) -> None:
    """原子写入状态文件，避免进程中断写坏 JSON。

    Args:
        path: 状态文件路径。
        state: 待写入的状态字典。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _member_view(raw: dict) -> dict | None:
    """把 OneBot 群成员对象裁剪成只含 QQ 号和展示昵称的视图。

    Args:
        raw: get_group_member_list 返回的单个成员对象。

    Returns:
        {"user_id": str, "name": str}；QQ 号非法时返回 None。
    """
    uid = raw.get("user_id")
    if uid is None:
        return None
    uid = str(uid).strip()
    if not uid.isdigit():
        return None
    name = (raw.get("card") or raw.get("nickname") or "").strip()
    return {
        "user_id": uid,
        "name": name or uid,
        "is_robot": bool(raw.get("is_robot", False)),
    }


class MarryGroupmate(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._lock = asyncio.Lock()
        self._members_cache: tuple[float, str, list[dict]] | None = None
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self._state_path = data_dir / "state.json"

    def _cfg(self, key: str, default):
        """读取插件配置，缺失或异常时回退默认值。"""
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _group_ids(self) -> set[str]:
        """返回生效群号集合（统一转成字符串比较）。"""
        raw = self._cfg("group_ids", DEFAULT_GROUP_IDS)
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _gain_range(self) -> tuple[int, int]:
        """返回好感度单次增量区间，配置非法时回退 1~5。"""
        try:
            lo = int(self._cfg("gain_min", DEFAULT_GAIN_MIN))
            hi = int(self._cfg("gain_max", DEFAULT_GAIN_MAX))
        except (TypeError, ValueError):
            lo, hi = DEFAULT_GAIN_MIN, DEFAULT_GAIN_MAX
        if lo > hi:
            lo, hi = hi, lo
        return max(0, lo), max(0, hi)

    def _pick_repeat_line(self) -> str:
        """从调侃文案池随机取一条，池为空时返回兜底文案。"""
        lines = self._cfg("repeat_lines", DEFAULT_REPEAT_LINES)
        if not isinstance(lines, (list, tuple)) or not lines:
            lines = DEFAULT_REPEAT_LINES
        candidates = [str(x).strip() for x in lines if str(x).strip()]
        return random.choice(candidates) if candidates else "明天再来吧。"

    def _repeat_chain(self, sender_id: str, line: str) -> list:
        """构造“你已经娶过了”的消息链。"""
        chain = [Plain(f"你已经娶过了，{line}")]
        if self._cfg("at_sender_on_repeat", True):
            chain.insert(0, At(qq=sender_id))
            chain.insert(1, Plain(" "))
        return chain

    async def _group_members(
        self,
        event: AstrMessageEvent,
        group_id: str,
    ) -> list[dict]:
        """拉取群成员列表（5 分钟缓存），失败时返回空列表。

        Args:
            event: 消息事件，用于取得 OneBot 客户端。
            group_id: 群号。

        Returns:
            成员视图列表；失败时为空列表。
        """
        now = time.monotonic()
        cached = self._members_cache
        if cached and cached[1] == group_id and cached[0] > now:
            return cached[2]

        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning(f"[{PLUGIN_NAME}] 当前平台不支持调用 OneBot API")
            return []
        try:
            raw = await bot.call_action(
                "get_group_member_list",
                group_id=int(group_id),
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 获取群成员失败: {e}")
            return []

        if isinstance(raw, dict):
            raw = raw.get("data") or []
        if not isinstance(raw, list):
            logger.warning(f"[{PLUGIN_NAME}] 群成员返回格式异常: {type(raw)}")
            return []

        members = [
            view
            for view in (_member_view(item) for item in raw if isinstance(item, dict))
            if view
        ]
        self._members_cache = (now + MEMBER_CACHE_TTL, group_id, members)
        return members

    async def _already_drawn(self, sender_id: str, today: str) -> bool:
        """判断发起人今天是否已经抽过。"""
        async with self._lock:
            state = _load_state(self._state_path)
            return state["last_draw"].get(sender_id) == today

    @filter.regex(r"^娶群友$")
    async def on_marry_groupmate(self, event: AstrMessageEvent):
        """处理「娶群友」触发词。"""
        if not self._cfg("enabled", True):
            return

        group_id = str(event.get_group_id() or "")
        if not group_id or group_id not in self._group_ids():
            return

        trigger = str(self._cfg("trigger_text", DEFAULT_TRIGGER)).strip()
        if not trigger or event.message_str.strip() != trigger:
            return

        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return

        blocked = {
            str(x).strip()
            for x in (self._cfg("blocked_sender_ids", None) or [])
            if str(x).strip()
        }
        if sender_id in blocked:
            return

        members = await self._group_members(event, group_id)
        if self._cfg("ignore_robot_senders", True):
            sender_view = next(
                (m for m in members if m["user_id"] == sender_id),
                None,
            )
            if sender_view and sender_view.get("is_robot"):
                logger.info(f"[{PLUGIN_NAME}] 忽略机器人发送者 {sender_id}")
                return

        today = _today()
        if await self._already_drawn(sender_id, today):
            event.set_extra(OWN_RESULT_KEY, True)
            yield event.chain_result(
                self._repeat_chain(sender_id, self._pick_repeat_line()),
            )
            return

        if not members:
            event.set_extra(OWN_RESULT_KEY, True)
            yield event.chain_result(
                [
                    At(qq=sender_id),
                    Plain(" 月老今天不在线，抽不了，稍后再试试。"),
                ],
            )
            return

        target = random.choice(members)
        gain_lo, gain_hi = self._gain_range()
        gain = random.randint(gain_lo, gain_hi)
        pair_key = f"{sender_id}:{target['user_id']}"

        # 在锁内重新判定并落盘，缩小并发窗口。
        score = None
        async with self._lock:
            state = _load_state(self._state_path)
            if state["last_draw"].get(sender_id) == today:
                repeat = True
            else:
                repeat = False
                score = state["pairs"].get(pair_key, 0) + gain
                state["pairs"][pair_key] = score
                state["last_draw"][sender_id] = today
                _save_state(self._state_path, state)

        event.set_extra(OWN_RESULT_KEY, True)
        if repeat or score is None:
            yield event.chain_result(
                self._repeat_chain(sender_id, self._pick_repeat_line()),
            )
            return

        logger.info(
            f"[{PLUGIN_NAME}] {sender_id} -> {target['user_id']} 好感度 {score}（+{gain}）",
        )
        yield event.chain_result(
            [
                At(qq=sender_id),
                Plain(" 今天你的群老婆是"),
                Image.fromURL(AVATAR_URL_TEMPLATE.format(uin=target["user_id"])),
                Plain(
                    f"\n[{target['name']}]({target['user_id']})哒\n"
                    f"当前你们好感度为{score}",
                ),
            ],
        )

    @filter.on_decorating_result()
    async def guard_imitation(self, event: AstrMessageEvent) -> None:
        """拦截大模型模仿模板编造的假「群老婆」消息。

        群里长期存在同类模板（包括其他 bot 的输出）时，大模型会在闲聊中
        自己套用这个句式并编出随机好感度；本钩子在发送前把这类非插件产出
        的消息直接丢弃，避免发出假数据。

        Args:
            event: 即将发送结果的消息事件。
        """
        if not self._cfg("block_imitation", True):
            return
        if event.get_extra(OWN_RESULT_KEY):
            return

        group_id = str(event.get_group_id() or "")
        if not group_id or group_id not in self._group_ids():
            return

        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return

        text = "".join(
            comp.text for comp in chain if isinstance(comp, Plain) and comp.text
        )
        if not text or not IMITATION_PATTERN.search(text):
            return

        logger.warning(f"[{PLUGIN_NAME}] 已拦截疑似模仿模板的伪造消息: {text[:80]}")
        event.clear_result()
