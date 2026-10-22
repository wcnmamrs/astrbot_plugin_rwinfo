# Rusted Warfare Room Lookup (astrbot_plugin_rwinfo)

> AstrBot plugin · Auto-detect Rusted Warfare room IDs in group messages and dispatch probe bots to query and report room info

[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

Detects Rusted Warfare room IDs in messages and dispatches probe bots (`ABAB探针01~99`) to join rooms and report back: version / map / credits / fog / no-nukes / multiplier / mode / unit cap / default unit / shared control / spectators / team lock / player count / Mod / player list.

Protocol notes in [PROTOCOL.md](PROTOCOL.md), version history in [CHANGELOG.md](CHANGELOG.md).

---

## Features

- **Auto room-ID detection**, try everything and send only on success, silently drop on failure, no filter wordlist needed
  - Relay rooms (any leading letter + digits): `r5132` `s1024` `a5132` `b5132` `h1234` `o9999`
  - International rooms: `h` + English letters, e.g. `HLBIFZ`, `hytvvi`
  - Direct connect: `IP` or `IP:port`, port defaults to 5123 when omitted
- **Room list**: `/铁锈列表` paged browser with automatic ad filtering
- **Localized failure reasons**: full room / game started / room locked return clear hints
- **Auto-retry on probe-name filter**: configurable count (default 3), retry names carry each probe index to avoid collisions
- **Auto-recall**: room info and retry progress can be auto-recalled, per-group / per-private config, off by default
- **Probe pool**: `ABAB探针01`~`ABAB探针99` concurrent reuse, max 5 room IDs per message
- **Room cooldown**: same room ID not re-parsed within 10 seconds
- **Permission control**: global switch plus white/black lists, per-user (`@userID`) and per-group (`#groupID`)
- **Fully self-contained**: built-in protocol probe, no third-party deps, no hardcoded absolute paths

### Detection basis

The client (`ae.smali` lines 7107~7180) converts any room ID without `:` `.` `/` `\` into `<first letter>.relay.corrodinggames.com/<room ID>`, so the leading letter is not limited to r/s/h.

Measured relay availability:

| Letter | Relay | Status |
|--------|-------|--------|
| a / b / h | Official Relay US1 / US2 / ASIA2 | reachable |
| o / q / r / s | RELAY-CN / Fancy-CN / Tiexiu.xyz | reachable |
| c / g / j / v | DNS exists, port closed | connect fails |
| others | no DNS record | resolve fails |

---

## Install

Install from the AstrBot plugin store, or via WebUI:

1. Open the AstrBot admin panel and go to Plugin Management
2. Paste `https://github.com/wcnmamrs/astrbot_plugin_rwinfo` to auto-fetch, or upload a zipped package
3. Enable the plugin after install

If `room_info.py` is absent in the plugin directory, it falls back to the parent directory.

---

## Commands

All commands except help default to admin-only (`PermissionType.ADMIN`).

| Command | Permission | Description |
|---------|-----------|-------------|
| `/铁锈查房帮助` | Everyone | Show command menu. Aliases `/铁锈查房菜单` `/铁锈帮助` `/rwhelp` |
| `/铁锈列表 [args]` | Admin | Paged room list. Aliases `/房间列表` `/铁锈房列表` `/rwlist` |
| `/铁锈下一页` `/铁锈上一页` | Admin | Page navigation |
| `/铁锈全局解析 开\|关` | Admin | Global parsing switch, default on |
| `/铁锈模式 白\|黑\|混合` | Admin | List mode, default black. In mixed mode `@` takes precedence over `#`, same name is exclusive |
| `/铁锈白名 +@userID` `/铁锈白名 +#groupID` | Admin | Whitelist add/remove, prints list with no args |
| `/铁锈黑名 +@userID` `/铁锈黑名 +#groupID` | Admin | Blacklist add/remove, prints list with no args |
| `/铁锈私聊策略 白\|黑` | Admin | Mixed-mode private default policy |
| `/铁锈私聊开关 开\|关` | Admin | Enable private-chat lookup, default on |
| `/铁锈玩家列表 开\|关` | Admin | Show player list, default off |
| `/铁锈重试 [0-10]` | Admin | Retry count when probe name is filtered |
| `/铁锈撤回 开\|关\|[seconds 0-300] [*]` | Group admin / everyone in private | Auto-recall config, saved per group / per private. Append `*` in group to apply to all groups, overridable by a specific group; wildcard not supported in private |

Unrecognized `/铁锈xxx` commands automatically return the help menu.

### Room list

```
/铁锈列表 [page]|[mod|vanilla]|[waiting|started]
         |[full|notfull]|[player|official]|[search keyword]
         |[all]|[refresh]
/铁锈下一页 / 铁锈上一页
e.g. /铁锈列表 模组 房间中 2
```

| Argument | Description |
|----------|-------------|
| page | Number, jump to that page |
| `模组` / `原版` | Filter by Mod usage |
| `房间中` / `游戏中` | Filter by room status |
| `满` / `不满` | Filter by whether the room is full |
| `真人` / `官方` | Filter by data source |
| `搜索 keyword` | Fuzzy search by host name / map name |
| `全部` | Clear filters |
| `刷新` | Ignore cache and refetch |

Display format (5 entries per page by default):

```
1. hostname  1/10  房间中  原版
   map: Crossing Large (10p)
```

Two sources merged, player rooms first, then waiting rooms, then by player count descending:

| Source | Description |
|--------|-------------|
| Q Room List | `https://www.rtsbox.cn/api/paiwei/room_last.php`, player-hosted rooms with host / map / players / status / mod flag |
| Official | Auto Server rooms from the official `masterserver`, placed after player rooms |

Room ID (internal QN protocol number) and host IP:port are not displayed: the former cannot be used to join, the latter is the host's real address and also cannot be used to join directly.

Ad filtering uses structural criteria only, never player counts (counts are self-reported and can be forged):

- `LINK` / `url:` entries
- Control characters in name or description (injected by ad bots, humans cannot type them)
- Invalid IPv4 / port
- Duplicate `ip:port`

Cache: room data is shared globally, paging and filter state are isolated per QQ number, both with a 60-second sliding expiry renewed on activity.

---

## Configuration

Auto-persisted by the AstrBot framework, editable via WebUI or commands.

| Key | Description |
|-----|-------------|
| `global_enabled` | Global parsing switch, default true |
| `mode` | List mode: `white` / `black` / `mixed`, default black |
| `白名单` / `黑名单` | White/black lists (user/group) |
| `混合_白名单` / `混合_黑名单` | Mixed-mode independent lists; the plain lists stay intact when switching back |
| `混合_私聊策略` | Mixed-mode private policy: `white`=block if not whitelisted / `black`=block if blacklisted |
| `私聊启用` | Enable private-chat lookup, default true |
| `debug` | Enable debug logging |
| `max_players_display` | Max players shown, folds beyond, default 10 |
| `show_players` | Show player list, default false |
| `retry_times` | Retry count when probe name filtered, 0-10, default 3 |
| `list_page_size` | Room list page size, default 5 |
| `list_default_full` | Default full-room filter, `hide` hides full rooms |
| `群撤回` | Per-group auto-recall config, `#groupID:on:delay`, e.g. `#123456:1:60` |
| `私聊撤回` | Per-user auto-recall config, `@userID:on:delay`, e.g. `@3856554669:1:60` |

---

## Message example

Someone posts:

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

If the probe name is filtered, progress is sent before each retry:

```
[房间查询] 重试中(1/3)
[房间查询] 重试中(2/3)
[房间查询] 重试中(3/3)
```

When the room cannot be joined, the reason is translated:

```
[房间查询失败] 房间已满，无法加入
[房间查询失败] 游戏已经开始
[房间查询失败] 房间已锁定
```

---

## Command-line tool

The built-in probe runs standalone, standard library only:

```bash
python3 room_info.py R5132                # query a relay room
python3 room_info.py HLBIFZ               # query an international room
python3 room_info.py 18.216.139.119:5125  # direct-connect query
python3 room_info.py --list               # fetch the room list
python3 room_info.py R5132 --show-players # show the player list
python3 room_info.py R5132 --debug        # debug output
```

---

## Directory structure

```
astrbot_plugin_rwinfo/
├── main.py            # plugin logic (message listen / room-ID extract / query schedule / retry / translate)
├── room_info.py       # protocol probe (room-info query core, runs standalone)
├── PROTOCOL.md        # protocol reverse-engineering notes
├── CHANGELOG.md       # version history
├── README.md          # Chinese doc
├── README_EN.md       # this doc
├── metadata.yaml      # plugin metadata
├── _conf_schema.json  # config schema
├── LICENSE            # MIT license
└── .gitignore
```

---

## Notes

- Queries run in a subprocess calling `room_info.py`, isolated; sends 111 to leave afterwards, never occupies a room slot
- Private messages are checked against `@userID`; group messages check both `#groupID` and the sender's `@userID` (user blacklist also applies in groups)
- Player list is shown by slot order, empty names shown as `(空)`
- When a room is in the "returned to battleroom after start" state the server does not update its status; humans can still join but probes are rejected, this is game behaviour
- This plugin is for learning and entertainment; do not use it to spam rooms or disturb other players

---

## License

[MIT](LICENSE)

This plugin is independently developed and not affiliated with Corroding Games. The protocol reverse engineering is for interoperability and learning only. If the game's developers object, please contact the author.