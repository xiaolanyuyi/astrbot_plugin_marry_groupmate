"""marry_groupmate 插件的离线自测。

不连接 QQ、不启动 AstrBot，只用真实依赖跑插件内的判定/抽签/好感度逻辑。

两种用法（脚本会自动识别所在位置）：
    # 在插件仓库根目录（需要能 import astrbot 的环境）
    python tests/test_marry_groupmate.py
    # 在工作区里（测已安装的插件）
    "AstrBot\\.venv\\Scripts\\python.exe" scripts\\test_marry_groupmate.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 仓库布局下直接测根目录的 main.py；工作区布局下测已安装的插件。
_LOCAL_MAIN = ROOT / "main.py"
_INSTALLED_MAIN = ROOT / "AstrBot" / "data" / "plugins" / "marry_groupmate" / "main.py"
PLUGIN_MAIN = _LOCAL_MAIN if _LOCAL_MAIN.exists() else _INSTALLED_MAIN

# 以下 QQ 号 / 昵称全部虚构，仅用于自测。
GROUP_ID = "123456789"
BOT_ID = "10000001"
SENDER_ID = "20000002"

MEMBERS = [
    {"user_id": 30000003, "card": "群友A", "nickname": "群友A"},
    {"user_id": 40000004, "card": "", "nickname": "群友B"},
    {"user_id": 50000005, "card": "群友C", "nickname": "c"},
    {"user_id": int(BOT_ID), "card": "机器人", "nickname": "bot"},
    {"user_id": 60000006, "card": "", "nickname": "群友D"},
    {"user_id": 70000007, "card": "", "nickname": "官方机器人", "is_robot": True},
]

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def _yesterday() -> str:
    """返回北京时间的昨天日期字符串。"""
    return (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).strftime(
        "%Y-%m-%d",
    )


def load_plugin():
    """以独立模块名加载插件 main.py（需要已安装 AstrBot）。"""
    try:
        import astrbot  # noqa: F401
    except ImportError:
        # 工作区布局下把 AstrBot 目录加进 path 再试一次。
        workspace_astrbot = ROOT / "AstrBot"
        if not workspace_astrbot.is_dir():
            print("SKIP: 未检测到 astrbot，请在 AstrBot 的虚拟环境中运行本自测。")
            raise SystemExit(0) from None
        sys.path.insert(0, str(workspace_astrbot))
    spec = importlib.util.spec_from_file_location("marry_groupmate_main", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBot:
    """模拟 OneBot 客户端，只实现 get_group_member_list。"""

    def __init__(self) -> None:
        self.calls = 0

    async def call_action(self, action: str, **kwargs):
        """返回固定的群成员列表。"""
        self.calls += 1
        assert action == "get_group_member_list", action
        return MEMBERS


class FakeEvent:
    """模拟 AstrMessageEvent 的最小可用子集。"""

    def __init__(
        self,
        *,
        group_id: str = GROUP_ID,
        sender_id: str = SENDER_ID,
        self_id: str = BOT_ID,
        message_str: str = "娶群友",
        bot: FakeBot | None = None,
    ) -> None:
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self.message_str = message_str
        self.bot = bot if bot is not None else FakeBot()
        self.chains: list[list] = []
        self._extras: dict = {}
        self._result = None

    def get_group_id(self) -> str:
        """群号。"""
        return self._group_id

    def get_sender_id(self) -> str:
        """发送者 QQ。"""
        return self._sender_id

    def get_self_id(self) -> str:
        """机器人 QQ。"""
        return self._self_id

    def chain_result(self, chain: list):
        """记录消息链并返回自身，便于断言。"""
        self.chains.append(chain)
        return chain

    def set_extra(self, key, value) -> None:
        """设置事件附加信息。"""
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        """读取事件附加信息。"""
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_result(self, result) -> None:
        """设置待发送结果。"""
        self._result = result

    def get_result(self):
        """读取待发送结果。"""
        return self._result

    def clear_result(self) -> None:
        """清除待发送结果。"""
        self._result = None


class FakeResult:
    """模拟 MessageEventResult：只需要 chain 属性。"""

    def __init__(self, chain: list) -> None:
        self.chain = chain


def make_plugin(module, tmp_dir: Path, config: dict | None = None, case: str = "base"):
    """绕过 Star.__init__ 构造插件实例（不需要真实 Context）。

    每个 case 使用独立的 data 目录，避免状态文件互相污染。
    """
    plugin = object.__new__(module.MarryGroupmate)
    plugin.config = config if config is not None else {}
    plugin._lock = asyncio.Lock()
    plugin._members_cache = None
    data_dir = tmp_dir / "plugin_data" / case / module.PLUGIN_NAME
    plugin._state_path = data_dir / "state.json"
    return plugin


async def run_handler(plugin, event) -> list[list]:
    """执行插件 handler，收集所有 yield 出来的消息链。"""
    results = []
    async for item in plugin.on_marry_groupmate(event):
        results.append(item)
    return results


def chain_text(chain: list) -> str:
    """把消息链里的文本拼起来，便于断言。"""
    parts = []
    for comp in chain:
        text = getattr(comp, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def comp_types(chain: list) -> list[str]:
    """取消息链的类型序列，便于断言顺序。"""
    return [getattr(c, "type", type(c).__name__) for c in chain]


async def main() -> int:
    """跑完全部断言。"""
    # 系统临时目录在沙箱下不可写，改用工作区内的临时目录。
    tmp_dir = ROOT / ".pytest_tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 先切到临时目录再 import astrbot，否则 astrbot 会在当前目录下创建 data/。
    cwd = Path.cwd()
    os.chdir(tmp_dir)
    try:
        module = load_plugin()
        # 1) 纯函数
        today = module._today()
        check("_today 返回 YYYY-MM-DD", len(today) == 10 and today[4] == "-", today)

        state_path = tmp_dir / "state.json"
        state = module._load_state(state_path)
        check("缺失状态文件返回空状态", state == module._new_state())
        state["pairs"]["a:b"] = 12
        state["last_draw"]["a"] = today
        module._save_state(state_path, state)
        check(
            "状态落盘后可原样读回",
            module._load_state(state_path)["pairs"]["a:b"] == 12,
        )

        state_path.write_text("{broken json", encoding="utf-8")
        check(
            "损坏状态文件回退为空状态",
            module._load_state(state_path) == module._new_state(),
        )

        view = module._member_view(MEMBERS[1])
        check(
            "群名片为空时回退昵称",
            view == {"user_id": "40000004", "name": "群友B", "is_robot": False},
            str(view),
        )
        check("非法 QQ 号返回 None", module._member_view({"user_id": "abc"}) is None)

        # 2) 首次抽取
        plugin = make_plugin(module, tmp_dir, {"group_ids": [GROUP_ID]})
        event = FakeEvent()
        results = await run_handler(plugin, event)
        check("首次触发只回一条消息", len(results) == 1, str(results))
        chain = results[0]
        types = comp_types(chain)
        check(
            "消息链为 At+Plain+Image+Plain",
            types == ["At", "Plain", "Image", "Plain"],
            str(types),
        )
        text = chain_text(chain)
        check("包含“今天你的群老婆是”", "今天你的群老婆是" in text, text)
        check("包含好感度展示", "当前你们好感度为" in text, text)
        image = chain[2]
        check(
            "图片为 qlogo 头像 URL",
            str(image.file).startswith("https://q1.qlogo.cn/g?b=qq&nk=")
            and image.file.endswith("&s=640"),
            str(image.file),
        )

        state = module._load_state(plugin._state_path)
        check(
            "已记录当天抽取",
            state["last_draw"].get(SENDER_ID) == today,
            str(state["last_draw"]),
        )
        pair = next(iter(state["pairs"]))
        score = state["pairs"][pair]
        check("首次好感度落在 1~5", 1 <= score <= 5, f"{pair}={score}")

        # 3) 同日重复
        event2 = FakeEvent()
        results2 = await run_handler(plugin, event2)
        check("同日重复只回一条", len(results2) == 1)
        text2 = chain_text(results2[0])
        check("同日重复提示“你已经娶过了”", "你已经娶过了" in text2, text2)
        check(
            "同日重复不再带图片",
            "Image" not in comp_types(results2[0]),
            str(comp_types(results2[0])),
        )
        check(
            "同日重复后好感度不变",
            module._load_state(plugin._state_path)["pairs"][pair] == score,
        )

        # 4) 次日再抽：同一对好感度累积（锁定候选池，确保抽到同一人）
        acc_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID], "gain_min": 2, "gain_max": 2},
            case="acc",
        )
        forced_member = [module._member_view(MEMBERS[0])]
        acc_plugin._members_cache = (
            time.monotonic() + 300,
            GROUP_ID,
            forced_member,
        )
        await run_handler(acc_plugin, FakeEvent())
        first = module._load_state(acc_plugin._state_path)["pairs"][
            f"{SENDER_ID}:30000003"
        ]
        state = module._load_state(acc_plugin._state_path)
        state["last_draw"][SENDER_ID] = _yesterday()
        module._save_state(acc_plugin._state_path, state)
        results3 = await run_handler(acc_plugin, FakeEvent())
        check(
            "次日可以再抽（回一条带图消息）",
            len(results3) == 1 and "Image" in comp_types(results3[0]),
        )
        second = module._load_state(acc_plugin._state_path)["pairs"][
            f"{SENDER_ID}:30000003"
        ]
        check(
            "同一对好感度按增量累加",
            first == 2 and second == 4,
            f"第一次={first} 第二次={second}",
        )

        # 5) 群与身份过滤
        other_group = FakeEvent(group_id="1031338497")
        check("非白名单群不响应", await run_handler(plugin, other_group) == [])
        self_msg = FakeEvent(sender_id=BOT_ID)
        check("机器人自己发的消息不响应", await run_handler(plugin, self_msg) == [])

        other_bot_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID]},
            case="mismatch",
        )
        not_match = FakeEvent(message_str="我想娶群友")
        check("非精确匹配不响应", await run_handler(other_bot_plugin, not_match) == [])

        robot_sender = FakeEvent(sender_id="70000007")
        check(
            "其他机器人发的触发词不响应", await run_handler(plugin, robot_sender) == []
        )

        blocked_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID], "blocked_sender_ids": [SENDER_ID]},
            case="blocked",
        )
        check(
            "黑名单发送者不响应",
            await run_handler(blocked_plugin, FakeEvent()) == [],
        )

        # 6) 结果守卫：拦截大模型模仿模板编造的假消息
        guard_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID]},
            case="guard",
        )
        fake_result = FakeResult(
            [
                module.Plain(
                    "@群友A 今天你的群老婆是群友B哒 当前你们好感度为77",
                ),
            ],
        )
        guard_event = FakeEvent()
        guard_event.set_result(fake_result)
        await guard_plugin.guard_imitation(guard_event)
        check("拦截无标记的伪造模板消息", guard_event.get_result() is None)

        own_event = FakeEvent()
        own_event.set_result(FakeResult([module.Plain("当前你们好感度为1")]))
        own_event.set_extra(module.OWN_RESULT_KEY, True)
        await guard_plugin.guard_imitation(own_event)
        check("插件自己的消息不被拦截", own_event.get_result() is not None)

        plain_event = FakeEvent()
        plain_event.set_result(FakeResult([module.Plain("今天天气不错")]))
        await guard_plugin.guard_imitation(plain_event)
        check("普通闲聊消息不被拦截", plain_event.get_result() is not None)

        off_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID], "block_imitation": False},
            case="guard_off",
        )
        off_event = FakeEvent()
        off_event.set_result(FakeResult([module.Plain("当前你们好感度为77")]))
        await off_plugin.guard_imitation(off_event)
        check("关闭 block_imitation 后不再拦截", off_event.get_result() is not None)

        other_group_event = FakeEvent(group_id="1031338497")
        other_group_event.set_result(FakeResult([module.Plain("当前你们好感度为77")]))
        await guard_plugin.guard_imitation(other_group_event)
        check(
            "非白名单群的消息不在守卫范围", other_group_event.get_result() is not None
        )

        # 7) 配置生效
        cfg_plugin = make_plugin(
            module,
            tmp_dir,
            {
                "enabled": True,
                "group_ids": [GROUP_ID],
                "trigger_text": "娶群友",
                "gain_min": 7,
                "gain_max": 7,
                "at_sender_on_repeat": False,
                "repeat_lines": ["测试文案"],
            },
            case="cfg",
        )
        cfg_event = FakeEvent()
        await run_handler(cfg_plugin, cfg_event)
        cfg_state = module._load_state(cfg_plugin._state_path)
        check(
            "gain_min=gain_max 时增量固定",
            list(cfg_state["pairs"].values()) == [7],
            str(cfg_state["pairs"]),
        )
        cfg_event2 = FakeEvent()
        results_cfg = await run_handler(cfg_plugin, cfg_event2)
        chain_cfg = results_cfg[0]
        check(
            "关闭 @ 后重复消息不带 At",
            "At" not in comp_types(chain_cfg),
            str(comp_types(chain_cfg)),
        )
        check(
            "自定义调侃文案生效",
            "测试文案" in chain_text(chain_cfg),
            chain_text(chain_cfg),
        )

        # 7) 接口失败兜底
        class BrokenBot:
            """模拟 OneBot API 异常。"""

            async def call_action(self, action: str, **kwargs):
                """总是抛错。"""
                raise RuntimeError("boom")

        broken_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID]},
            case="broken",
        )
        broken_event = FakeEvent(bot=BrokenBot())
        results_broken = await run_handler(broken_plugin, broken_event)
        check(
            "成员接口失败时给出友好提示",
            len(results_broken) == 1
            and "月老今天不在线" in chain_text(results_broken[0]),
            chain_text(results_broken[0]) if results_broken else "",
        )
        check(
            "接口失败时不记录抽取",
            module._load_state(broken_plugin._state_path)["last_draw"] == {},
        )

        # 8) 随机性：池子里每个成员都应有机会出现（不排除任何人的一视同仁）
        seen = set()
        rand_plugin = make_plugin(
            module,
            tmp_dir,
            {"group_ids": [GROUP_ID]},
            case="rand",
        )
        for i in range(300):
            state = module._load_state(rand_plugin._state_path)
            state["last_draw"].pop(SENDER_ID, None)
            module._save_state(rand_plugin._state_path, state)
            results_rand = await run_handler(rand_plugin, FakeEvent())
            image = [c for c in results_rand[0] if getattr(c, "file", None)][0]
            seen.add(image.file.split("nk=")[1].split("&")[0])
        check(
            "候选池包含全部成员（含机器人/发起人自己）",
            seen == {str(m["user_id"]) for m in MEMBERS},
            str(sorted(seen)),
        )
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {', '.join(failures)}")
        return 1
    print("全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    random.seed()
    raise SystemExit(asyncio.run(main()))
