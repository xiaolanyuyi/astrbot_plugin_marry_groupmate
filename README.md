# astrbot_plugin_marry_groupmate

AstrBot 插件：在指定 QQ 群里发送「娶群友」，随机抽一位群友当你的"群老婆"，展示对方头像与昵称，并按「发起人 + 被抽人」持久累积好感度。

> 纯规则实现，**不调用大模型**：0 token、毫秒级响应。

## 效果

首次抽取：

```
@你 今天你的群老婆是
<对方 QQ 头像>
[示例群友](12345678)哒
当前你们好感度为 58
```

同一天再抽：

```
@你 你已经娶过了，一天想娶几个？QQ 群不是后宫。
```

## 功能特性

- **按群号锁定**：只在配置的群内触发（群改名不影响）
- **精确触发**：消息必须正好是触发词，`我想娶群友` 不触发
- **每天一次**：按北京时间（UTC+8）0 点重置，每人每天只能抽一次
- **一视同仁的候选池**：全群成员随机，不排除机器人、不排除发起人自己
- **好感度持久累积**：按「发起人 + 被抽人」落盘，每次随机 `+gain_min~gain_max`，无上限、不衰减
- **重复抽取只调侃**：不再给好感度，随机抽一条调侃文案，可在配置里改
- **不响应机器人**：机器人自己（含其他 bot，按 `is_robot` 与黑名单判定）发的触发词不响应
- **防伪造**：群里模板消息多了以后，大模型会自己套句式编造「群老婆」和假好感度；插件在发送前拦截这类非插件产出的消息
- **健壮性**：状态文件原子写入、损坏自动重置、群成员接口失败时给友好提示
- **不响应**：机器人自己与其他 bot 的消息

## 安装

要求：AstrBot `>= 4.17.0`，平台适配器为 `aiocqhttp`（OneBot v11）。

### 方式一：WebUI 从 GitHub 安装（推荐）

AstrBot WebUI → 插件管理 → 从 GitHub 安装，填入本仓库地址：

```
https://github.com/xiaolanyuyi/astrbot_plugin_marry_groupmate
```

### 方式二：手动放入

```bash
git clone https://github.com/xiaolanyuyi/astrbot_plugin_marry_groupmate.git
```

把整个目录放到 AstrBot 的 `data/plugins/` 下，目录名建议为 `marry_groupmate`：

```
<AstrBot>/data/plugins/marry_groupmate/
├── main.py
├── metadata.yaml
└── _conf_schema.json
```

> 无第三方依赖，因此不需要 `requirements.txt`。

放好后在 WebUI → 插件管理点「重载」，或重启 AstrBot。**新建插件目录必须重载/重启一次才会被发现。**

## 配置

WebUI → 插件 → 娶群友，或直接改 `data/config/marry_groupmate_config.json`。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关 |
| `group_ids` | list | `[]` | **必填**：生效的群号列表，留空则插件完全不响应 |
| `trigger_text` | string | `"娶群友"` | 触发词，精确匹配（忽略首尾空格） |
| `gain_min` | int | `1` | 单次好感度增量下限 |
| `gain_max` | int | `5` | 单次好感度增量上限 |
| `at_sender_on_repeat` | bool | `true` | 重复抽取时是否 @ 发起人 |
| `repeat_lines` | list | 15 条 | 重复抽取时的调侃文案池，随机抽一条 |
| `ignore_robot_senders` | bool | `true` | 发送者资料 `is_robot=true` 时不触发 |
| `blocked_sender_ids` | list | `[]` | 黑名单 QQ，填写其他机器人号可彻底屏蔽 |
| `block_imitation` | bool | `true` | 拦截大模型模仿模板发出的假消息 |

> 想让人物数字涨得快一点，把 `gain_min` / `gain_max` 调大即可（例如 `10` / `30`）。
> 配置改完保存即生效，不需要重启。

## 数据存储

好感度与每日次数保存在：

```
<AstrBot>/data/plugin_data/marry_groupmate/state.json
```

结构：

```json
{
  "version": 1,
  "last_draw": { "12345678": "2026-10-25" },
  "pairs": { "12345678:87654321": 58 }
}
```

- `last_draw`：发起人 → 最近一次抽取日期（判断"每天一次"）
- `pairs`：`发起人:被抽人` → 好感度

**重置**：停止 AstrBot，删除该文件，再启动即可。

## 自测

仓库自带离线自测（不连接 QQ、不启动机器人，只验证抽签/好感度/每日限制/过滤逻辑）：

```bash
# 需要可 import astrbot 的 Python 环境（即 AstrBot 的虚拟环境）
python tests/test_marry_groupmate.py
```

全部通过时会输出 `全部断言通过 ✅`。

## 实现要点

- 触发用 `@filter.regex(r"^娶群友$")`：正则过滤器不受 `wake_prefix` 约束，群里**不 @ 机器人**也能触发；且不设置 `is_at_or_wake_command`，所以不会额外触发默认的大模型回复（不会双回复）。
- 群号判定放在 handler 内做，避免在别的群"唤醒"机器人却什么都不回。
- 头像使用 QQ 官方头像 CDN：`https://q1.qlogo.cn/g?b=qq&nk=<QQ>&s=640`。
- 消息用 `event.chain_result([At, Plain, Image, Plain])` 一次发出，@、图、文本在同一条消息里。
- 好感度用 `asyncio.Lock` + 临时文件 `os.replace` 原子写入，避免并发/中断写坏 JSON。

## 为什么需要 `block_imitation`

群里如果有人（或别的机器人）经常发这类模板，**大模型的群聊上下文里就全是这个句式**。
当模型被 @ 或主动插话时，它会自己套用模板编一条，例如：

```
@某人 今天你的群老婆是某某哒 当前你们好感度为77
```

这条**不是插件发的**，77 是模型编的，头像和 QQ 号也会缺失。

`block_imitation` 通过 `@filter.on_decorating_result()` 钩子在**发送前**检查消息文本：
命中 `今天你的群老婆是` / `当前你们好感度为` / `你已经娶过了` 且不是插件自己产出的结果，就直接丢弃并记 WARN 日志。
插件自己的回复通过 `event.set_extra()` 打了标记，不会被误拦。

如果你希望模型能自由讨论这个梗，可以把它关掉（`block_imitation: false`）。

## 开发

```bash
# 代码格式化与静态检查（AstrBot 官方插件的提交要求）
ruff format .
ruff check .

# 离线自测：35 项断言，不连 QQ、不启动机器人
python tests/test_marry_groupmate.py
```

- 无第三方运行时依赖，因此没有 `requirements.txt`。
- 调试时改完代码在 AstrBot WebUI → 插件 → 刷新图标（重载插件）即可，无需重启 AstrBot。
- 持久化数据写在 AstrBot 的 `data/plugin_data/marry_groupmate/`，**不要**写进插件目录，避免更新插件时被覆盖。

## 灵感来源

「娶群友 / 今天你的群老婆是」是 QQ 群里流行的娱乐玩法，本插件按该玩法独立实现（未使用其他项目代码）。
文案、好感度规则与实现均为本项目原创。

## 许可

[MIT](LICENSE)
