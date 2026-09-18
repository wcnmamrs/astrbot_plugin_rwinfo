# Rusted Warfare Room Lookup (astrbot_plugin_rwinfo)

> AstrBot plugin · Auto-detect Rusted Warfare (铁锈战争) room IDs in group messages and dispatch probe bots to query and report room info

[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-wcnmamrs%2Fastrbot_plugin_rwinfo-black)](https://github.com/wcnmamrs/astrbot_plugin_rwinfo)

Automatically detects Rusted Warfare room IDs in group messages and dispatches probe bots (`ABAB探针01~99`) to join rooms and report back: **version / map / credits / fog / no-nukes / multiplier / mode / unit cap / default unit / shared control / spectators / team lock / player count / Mod / player list**.

> 📖 Protocol reverse-engineering notes in [PROTOCOL.md](PROTOCOL.md): frame format, message types, hash algorithms, 106 room-info parsing, 115 player-list parsing, 151 POW challenge, etc.

---

## ✨ Features

- **Auto room-ID detection** (try everything, send only on success):
  - **CN relay rooms**: `r1234` / `s1234` (`r/s` + 3+ digits, no length cap)
  - **International rooms**: `h/H` + English letters (e.g. `HLBIFZ`, `hhello`), length 3~10; `h` must **not** be preceded by a letter (space, colon, quote, digit, punctuation, line start all OK); greedy match (multiple `h` in one room ID are not split)
  - Sends only when a valid room is found; silently drops on failure (room not found / English-word false positive) — no filter wordlist needed
- **Localized failure reasons**: full room / game started / room locked etc. return clear Chinese hints (e.g. `[房间查询失败] 房间已满，无法加入`)
- **Auto-retry on probe-name filter**: automatically renames and retries when the server blocks the probe name, sending progress `[房间查询] 重试中(1/3)`; retry count configurable (default 3)
- **Unique retry names**: with multiple rooms per message, retry names use each probe's own index (`ABAB探针01_1`, `02_1`...) to avoid cross-room collisions
- **Direct-connect default port**: `room_info.py 18.216.139.119` defaults to port 5123 when omitted
- **Auto-recall room info**: successfully reported room info and retry progress are auto-recalled after `recall_delay` sec (error info & command replies not recalled); off by default, toggle/set via `/铁锈撤回`
- **Probe pool**: `ABAB探针01` ~ `ABAB探针99` concurrent reuse, queue waiting beyond 99; max 5 room IDs per message
- **Room cooldown**: same room ID not re-parsed within 10s, anti-spam
- **Permission control**: global switch + whitelist/blacklist modes
- **Player-list folding**: folds when over `max_players_display` (default 10) to avoid spam
- **Special character support**: any UTF-8 player name (Chinese/symbols/whitespace)
- **Fully self-contained**: built-in protocol probe `room_info.py`, no third-party deps, no hardcoded absolute paths, upload-and-go on any device

---

## 📦 Install

Install from the AstrBot plugin store, or via the **WebUI**:

1. Open AstrBot admin panel → **Plugin Management**
2. Option 1: paste the GitHub repo URL `https://github.com/wcnmamrs/astrbot_plugin_rwinfo` to auto-fetch and install
3. Option 2: upload the zipped repo directly
4. Enable the plugin after install — no command-line needed

> Compatibility mode: if `room_info.py` is absent in the plugin dir, it auto-falls back to a `room_info.py` in the parent dir.

---

## 🎮 Commands

> All commands except help default to **admin-only** (AstrBot `PermissionType.ADMIN`) to prevent abuse by ordinary members.

| Command | Permission | Description |
|---------|-----------|-------------|
| `/铁锈查房帮助` | Everyone | Show all plugin commands and usage |
| `/铁锈全局解析 开\|关` | 🔒 Admin | Global on/off switch (default on); no arg shows current state |
| `/铁锈模式 白\|黑` | 🔒 Admin | List mode: white=only listed items pass; black=listed items disabled (default black); no arg shows mode |
| `/铁锈玩家列表 开\|关` | 🔒 Admin | Whether to show player list (default off); no arg shows state |
| `/铁锈白名 +@3245987504` | 🔒 Admin | Add user to whitelist (private chat) |
| `/铁锈白名 +#123456` | 🔒 Admin | Add group to whitelist |
| `/铁锈白名` | 🔒 Admin | Print whitelist |
| `/铁锈黑名 +@3245987504` | 🔒 Admin | Add user to blacklist (private chat) |
| `/铁锈黑名 +#123456` | 🔒 Admin | Add group to blacklist |
| `/铁锈黑名` | 🔒 Admin | Print blacklist |
| `/铁锈重试 [0-10]` | 🔒 Admin | Set auto-retry count when probe name is filtered; no arg shows config |
| `/铁锈撤回 开\|关\|秒数(0-300)` | 🔒 Admin | Auto-recall on/off / delay seconds; no arg shows config |

---

## ⚙️ Configuration

Config is auto-persisted by the AstrBot framework, editable via WebUI or commands. Defaults:

```json
{
  "global_enabled": true,
  "mode": "black",
  "white_list": [],
  "black_list": [],
  "debug": false,
  "max_players_display": 10,
  "show_players": false,
  "retry_times": 3,
  "auto_recall": false,
  "recall_delay": 60
}
```

| Key | Description |
|-----|-------------|
| `global_enabled` | Global parsing switch |
| `mode` | List mode (`white` or `black`) |
| `white_list` / `black_list` | Group(`#`)/UserID(`@`) lists |
| `debug` | Enable debug logging |
| `max_players_display` | Max players shown in list, fold beyond |
| `show_players` | Show player list (default off) |
| `retry_times` | Auto-retry count when probe name filtered (0-10, default 3) |
| `auto_recall` | Auto-recall room info after `recall_delay` sec (default off) |
| `recall_delay` | Auto-recall delay in seconds (0-300, default 60) |

---

## 📝 Message Example

Someone in the group posts:

```
ADMIN: Other players can type HLBIFZ in 'direct join' to connect to this room.
```

The bot replies:

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
  B2 (AI): AI - Hard
```

If the probe name is filtered, it replies progress then retries:

```
[房间查询] 重试中(1/3)
[房间查询] 重试中(2/3)
[房间查询] 重试中(3/3)
(room info returned when the 3rd attempt succeeds)
```

If the room is full / game started / locked:

```
[房间查询失败] 房间已满，无法加入
[房间查询失败] 游戏已经开始
[房间查询失败] 房间已锁定
```

---

## 🧪 Command-line Tool (room_info.py)

The built-in protocol probe also runs standalone:

```bash
python3 room_info.py R5132                # query relay room r5132
python3 room_info.py HLBIFZ               # query international room hlbifz
python3 room_info.py 18.216.139.119:5125  # direct-connect server query
python3 room_info.py --list               # fetch official room list
python3 room_info.py R5132 --show-players # show player list
python3 room_info.py R5132 --debug        # debug output
```

Uses only the Python standard library (gzip/hashlib/io/os/re/socket/struct/sys/time/urllib/uuid/random/zlib), no third-party deps.

---

## 📄 Protocol Reverse-engineering

See [PROTOCOL.md](PROTOCOL.md): Rusted Warfare online protocol reverse-engineering notes (relay/direct architecture, frame format, message type table, 160/161 handshake, 151 POW challenge, 110 registration, 106 room-info parsing, 115 player-list parsing, 178 jump, etc.).

---

## 📁 Directory Structure

```
astrbot_plugin_rwinfo/
├── main.py            # AstrBot plugin main logic (message listen / room-ID extract / query schedule / retry / translate)
├── room_info.py       # protocol probe (room-info query core, runs standalone)
├── PROTOCOL.md        # protocol reverse-engineering notes
├── README.md          # this doc (Chinese)
├── README_EN.md       # this doc (English)
├── metadata.yaml      # plugin metadata
├── _conf_schema.json  # config schema
└── LICENSE            # MIT license
```

---

## 📌 Notes

- Queries run via subprocess calling `room_info.py`, isolated; auto-sends 111 to leave after query, never occupies a room slot
- Same room ID has a 10s cooldown to avoid repeated parsing
- Private messages: processed as group if the unified session id contains a group id passing whitelist/blacklist; pure private (no group id) is ignored
- Player list shown by slot order, empty names shown as `(空)`
- This plugin is for learning & entertainment; please do not use it to spam rooms or disturb other players

---

## 📜 License

[MIT](LICENSE)

> **Disclaimer**: This plugin is independently developed and not affiliated with Corroding Games. If you object to the protocol reverse-engineering, please contact the author.