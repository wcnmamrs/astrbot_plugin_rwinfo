# 铁锈查房 (astrbot_plugin_rwinfo)

> AstrBot 插件 · 群内自动识别铁锈战争房间号并安排探针机器人查房回传

[![AstrBot](https://img.shields.io/badge/AstrBot-%E6%8F%92%E4%BB%B6-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-wcnmamrs%2Fastrbot_plugin_rw_roominfo-black)](https://github.com/wcnmamrs/astrbot_plugin_rw_roominfo)

群内自动识别铁锈战争（Rusted Warfare）房间号，安排探针机器人（ABAB探针01~99）进入房间查询信息并回传：**版本 / 地图 / 资金 / 迷雾 / 禁核 / 倍率 / 模式 / 单位上限 / 默认单位 / 共享控制 / 旁观 / 队伍锁定 / 人数 / Mod / 玩家列表**。

> 📖 联机协议逆向细节见 [PROTOCOL.md](PROTOCOL.md)：帧格式、消息类型、哈希算法、106 房间信息解析、151 POW 挑战等。

---

## ✨ 功能特性

- **自动识别房号**（策略：一律尝试，成功才发）：
  - **CN 中继房**：`r1234` / `s1234`（r/s + 3~7 位数字）
  - **国际房**：`h/H` 开头的英文字母串（如 `HLBIFZ`、`hhello`），长度 3~10 位，h 前必须**不是字母**（空格、冒号、引号、数字、标点、行首均可），整串贪婪匹配（一个房号内多个 h 不会拆开）
  - 查询到有效房间信息才发送，查不到（房间不存在/英文单词误报）**自动静默丢弃**，无需任何过滤词表
- **失败原因中文翻译**：房间已满、游戏已开始、房间已锁定等无法加入时，返回明确中文提示（如 `[房间查询失败] 房间已满，无法加入`）
- **探针名被过滤自动重试**：被服务器前置过滤拦截时自动换名重试，发送进度 `[房间查询] 重试中(1/3)`，次数可配置（默认 3 次）
- **探针池**：ABAB探针01 ~ ABAB探针99 并发复用，超 99 并发排队等待；单条消息最多处理 5 个房号
- **房号冷却**：同一房号 10 秒内不重复解析，防刷屏
- **权限控制**：全局开关 + 白名单/黑名单模式
- **玩家列表折叠**：超过 `max_players_display`（默认 10）人时折叠显示，避免刷屏
- **特殊字符支持**：任意 UTF-8 玩家名（中文/特殊符号/空白）
- **完全自包含**：内置协议探针 `room_info.py`，无第三方依赖、无绝对路径硬编码，换设备上传即用

---

## 📦 安装

1. 将 `astrbot_plugin_rwinfo/` 整个目录（或 zip）复制到 AstrBot 的 `addons/` 目录下
2. 重启 AstrBot（或热加载插件）

> 兼容模式：若插件目录内没有 `room_info.py`，会自动回退查找上级目录的 `room_info.py`。

---

## 🎮 指令

| 指令 | 说明 |
|------|------|
| `/铁锈全局解析 开\|关` | 全局总开关（默认开）；无参数时显示当前状态 |
| `/铁锈模式 白\|黑` | 名单模式：白=仅白名单群可用；黑=黑名单群禁用（默认黑）；无参数时显示当前模式 |
| `/铁锈白名 +123456` | 加入白名单 |
| `/铁锈白名 -123456` | 移出白名单 |
| `/铁锈白名` | 打印白名单内群聊 |
| `/铁锈黑名 +123456` | 加入黑名单 |
| `/铁锈黑名 -123456` | 移出黑名单 |
| `/铁锈黑名` | 打印黑名单内群聊 |
| `/铁锈重试 [0-10]` | 设置探针名被过滤时的自动重试次数；无参数显示当前配置 |

---

## ⚙️ 配置

配置由 AstrBot 框架自动持久化，可通过 WebUI 修改，也支持指令修改后即时保存。默认配置：

```json
{
  "global_enabled": true,
  "mode": "black",
  "white_list": [],
  "black_list": [],
  "debug": false,
  "max_players_display": 10,
  "show_players": false,
  "retry_times": 3
}
```

| 配置项 | 说明 |
|--------|------|
| `global_enabled` | 全局解析开关 |
| `mode` | 名单模式（white 或 black） |
| `white_list` / `black_list` | 群号列表 |
| `debug` | 是否开启调试日志 |
| `max_players_display` | 玩家列表最多显示人数，超过则折叠 |
| `show_players` | 是否显示玩家列表（默认关闭） |
| `retry_times` | 探针名被过滤时自动重试次数（0-10，默认3） |

---

## 📝 消息示例

群内有人发：

```
ADMIN: Other players can type HLBIFZ in 'direct join' to connect to this room.
```

机器人自动回复：

```
版本：1.15
地图名称：Crossing Large (10p)
初始资金：4000¤[1x倍率]
初始禁核：未禁用核蛋蛋
初始迷雾：重雾
模式：遭遇战
默认单位：常规模式（1个建造者）
单位上限：1100
共享控制：否
允许旁观：是
队伍锁定：否
当前人数：2/10
使用Mod：原版（121 内置单位）
ID：hlbifz
 玩家列表:
  A1: abab
  A3: AI - Hard (AI)
```

如果探针名被过滤，会先回复进度再重试：

```
[房间查询] 重试中(1/3)
[房间查询] 重试中(2/3)
[房间查询] 重试中(3/3)
（第3次成功时返回房间信息）
```

如果房间已满/游戏已开始/已锁定，会回复（已翻译）：

```
[房间查询失败] 房间已满，无法加入
[房间查询失败] 游戏已经开始
[房间查询失败] 房间已锁定
```

---

## 🧪 命令行工具（room_info.py）

插件内置的协议探针也可独立使用：

```bash
python3 room_info.py R5132                # 查询中继房间 r5132
python3 room_info.py HLBIFZ               # 查询国际房 hlbifz
python3 room_info.py 18.216.139.119:5125  # 直连服务器查询
python3 room_info.py --list               # 拉取官方房间列表
python3 room_info.py R5132 --show-players # 显示玩家列表
python3 room_info.py R5132 --debug        # 调试输出
```

仅使用 Python 标准库（gzip/hashlib/io/os/re/socket/struct/sys/time/urllib/uuid/random/zlib），无第三方依赖。

---

## 📄 协议逆向

见 [PROTOCOL.md](PROTOCOL.md)：铁锈战争联机协议逆向笔记（中继/直连架构、帧格式、消息类型表、160/161 握手、151 POW 挑战、110 注册、106 房间信息解析、178 跳转等）。

---

## 📁 目录结构

```
astrbot_plugin_rwinfo/
├── main.py            # AstrBot 插件主逻辑（消息监听/房号提取/查询调度/重试/翻译）
├── room_info.py       # 协议探针（房间信息查询核心，可独立命令行运行）
├── PROTOCOL.md        # 联机协议逆向笔记
├── README.md          # 本文档
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # 配置 schema
└── LICENSE            # MIT 许可证
```

---

## 📌 说明

- 查询通过子进程调用 `room_info.py` 完成，互不干扰；查询完自动发 111 退出，不占房间位置
- 同一房号 10 秒冷却，避免重复解析
- 私聊消息不处理（仅群聊）
- 玩家列表按槽位顺序显示，空名字显示为 `(空)`
- 本插件仅供学习与娱乐，请勿用于恶意刷房/干扰他人游戏

---

## 📜 许可证

[MIT](LICENSE)

> **免责声明**：本插件为独立开发，与 Corroding Games 无任何关联。协议逆向仅用于互操作与学习目的。如游戏官方对协议逆向有异议，请联系作者。