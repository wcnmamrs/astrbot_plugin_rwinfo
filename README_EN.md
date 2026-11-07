# Rusted Warfare Room Lookup (astrbot_plugin_rwinfo)

> AstrBot plugin · Auto-detect Rusted Warfare room IDs in group messages and dispatch probe bots to query and report room info

[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

Detects Rusted Warfare room IDs in messages and dispatches probe bots (`<prefix>探针01~99`, prefix configurable) to join rooms and report back: room version / map / credits / fog / no-nukes / multiplier / mode / unit cap / default unit / shared control / spectators / team lock / player count / player list.

Protocol notes in [PROTOCOL.md](PROTOCOL.md), version history in [CHANGELOG.md](CHANGELOG.md).

---

## Features

- **Auto room-ID detection**, try everything and send only on success, silently drop on failure, no filter wordlist needed
  - Numeric rooms: `o` `q` `r` `s` + digits, e.g. `r5132`, `s1024`, `o9999`, `q7777`
  - Alphabetic rooms: other leading letters + pure letters, e.g. `hytvvi`, `aabcde`, `babc`
  - Both forms require the whole token to be ≥ 3 chars; bare digits, mixed forms (`a1b2c3`), URL fragments and `u_` prefixes are rejected
- **Room list**: `/铁锈列表` paged browser
- **Look up straight from the list**: `/铁锈查房 <index>` reuses the last list result
- **Localized failure reasons**: full room / game started / room locked return clear hints
- **Auto-retry on probe-name filter**: configurable count (default 3), retry names carry each probe index to avoid collisions
- **Auto-recall**: room info and retry progress can be auto-recalled, per-group / per-private config, off by default
- **Probe pool**: `<prefix>探针01`~`<prefix>探针99` concurrent reuse (prefix configurable, default `ABAB`), max 5 room IDs per message (configurable via `probe_max_per_msg`)
- **Room cooldown**: same room ID not re-parsed within 10 seconds
- **Permission control**: global switch plus white/black lists, per-user (`@userID`) and per-group (`#groupID`)
- **Fully self-contained**: built-in protocol probe, no third-party deps, no hardcoded absolute paths

### Detection basis

The client (`ae.smali` lines 7129~7139) only checks six conditions before treating the input as a
room ID: non-empty, no `:` `.` `/` `\`, and not `localhost`. It then takes the **first character**
and builds `<first letter>.relay.corrodinggames.com/<room ID>`, so the client itself imposes no
character restrictions.

The two accepted forms are therefore determined by the **relay servers**, verified on-device:

| Leading letters | Form | Example |
|---|---|---|
| `o` `q` `r` `s` | numeric only | `r5132`, `s1024`, `o9999`, `q7777` |
| other letters (`a` `b` `h` …) | pure alphabetic only | `hytvvi`, `aabcde`, `babc` |

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

If `room_info.py` is missing or too old, it is downloaded from the repository.

---

## Commands

Two permission tiers:

- **Query** (help / list / lookup / paging): available to everyone, gated by the plugin's own
  allow/deny lists. With no lists configured, everyone in the group can use them.
- **Config & diagnostics**: AstrBot admin only (`PermissionType.ADMIN`).

| Command | Permission | Description |
|---------|-----------|-------------|
| `/铁锈查房帮助` | Everyone | Show command menu. Aliases `/铁锈查房菜单` `/铁锈帮助` `/rwhelp` |
| `/铁锈列表 [args]` | Allowlist | Paged room list. Aliases `/房间列表` `/铁锈房列表` `/rwlist` |
| `/铁锈下一页` `/铁锈上一页` | Allowlist | Page navigation |
| `/铁锈查房 <index\|roomID\|IP:port>` | Allowlist | Explicit lookup. Index comes from the latest list, room ID like `r5132` / `hytvvi`, or a raw `IP` / `IP:port` |
| `/铁锈全局解析 开\|关` | Admin | Global parsing switch, default on |
| `/铁锈模式 白\|黑\|混合` | Admin | List mode, default black. In mixed mode `@` takes precedence over `#`, same name is exclusive |
| `/铁锈白名 +@userID` `/铁锈白名 +#groupID` | Admin | Whitelist add/remove, prints list with no args |
| `/铁锈黑名 +@userID` `/铁锈黑名 +#groupID` | Admin | Blacklist add/remove, prints list with no args |
| `/铁锈私聊策略 白\|黑` | Admin | Mixed-mode private default policy |
| `/铁锈私聊开关 开\|关` | Admin | Enable private-chat lookup, default on |
| `/铁锈玩家列表 开\|关` | Admin | Show player list, default off |
| `/铁锈重试 [0-10]` | Admin | Retry count when probe name is filtered |
| `/铁锈查房调试 [category]` | Admin | Runtime diagnostics (session / recall / probe / list / config / official) |
| `/铁锈查房调试菜单` | Admin | List adjustable settings and current values |
| `/铁锈压缩 <scope>` | Admin | Fold scope: `none` / `room` / `list` / `diag` / `help` / `all` |
| `/铁锈官机探测` | Admin | Detect the official-bot channel |
| `/铁锈官机测试` | Admin | Send a test message through the official-bot channel |
| `/铁锈探针时解 <suffix>` | Everyone | Decode the timestamp embedded in a probe name |
| `/铁锈撤回 列出\|+\|-\|重置` | Admin | Auto-recall config, `key:delay` entries, `*` expands to all known groups. See the Chinese README for the full syntax |

Unrecognized `/铁锈xxx` commands automatically return the help menu.

### Room list

```
/铁锈列表 [page]|[waiting|started]
         |[full|notfull]|[player|official]|[search keyword]|[refresh]
/铁锈下一页 / 铁锈上一页
e.g. /铁锈列表 房间中 真人 2
```

| Argument | Description |
|----------|-------------|
| page | Number, jump to that page |
| `房间中` / `游戏中` | Filter by room status |
| `满` / `不满` | Filter by whether the room is full |
| `真人` / `官方` | Filter by data source |
| `搜索 keyword` | Fuzzy search by host name / map name |
| `刷新` | Ignore cache and refetch |

**Filter inheritance**

| Usage | Behavior |
|-------|----------|
| `/铁锈列表` (no arguments) | Fresh query, all filters cleared |
| `/铁锈列表 2` (page only) | Reuse last filters, jump to page 2 |
| `/铁锈下一页` / `/铁锈上一页` | Reuse last filters when paging |
| `/铁锈列表 模组` (with filters) | Replace all old filters with this query's |

Filter state is isolated per "group + user"; **page number never expires**
and survives a plugin restart via AstrBot KV persistence.

Display format (5 entries per page by default):
```
1. 萧逸  1/10  房间中
   map: Crossing Large (10p)
2. SERVER  2/10  房间中
   map: Two Sides Remake (10p)
```

When the room name is `Unnamed`, the host name is shown instead (CSV column 1 is the room name,
column 7 is the host name).

### Room lookup

```
/铁锈查房 <index|roomID|IP:port>
```

| Form | Description | Example |
|---|---|---|
| index | Number from the latest `/铁锈列表` result (per current page; mapping valid 10 min, auto-rebuilt silently when stale) | `/铁锈查房 2` |
| numeric room | `o` `q` `r` `s` + digits, at least 3 chars | `/铁锈查房 r5132` |
| alphabetic room | Other letters, pure alphabetic, at least 3 chars | `/铁锈查房 hytvvi` |
| direct | `IP` or `IP:port` (port defaults to 5123) | `/铁锈查房 45.76.27.30:5125` |

**Permissions**

Both `/铁锈列表` and `/铁锈查房` follow the white/black lists: with the default `black`
mode and empty lists, everyone in the group can use them. Admins can narrow access with
`/铁锈模式`, `/铁锈白名` and `/铁锈黑名`. When blocked, the reason is reported instead of
being silently dropped.

**Automatic detection vs explicit lookup**

| | Auto-detect (room ID posted in chat) | Explicit lookup |
|---|---|---|
| Allow raw IP | No | Yes |
| On failure | **Silent**, nothing posted | **Reason reported** |
| Purpose | Avoid spam from English words misread as room IDs | User asked explicitly, must answer |

In auto-detect mode only failures that imply "the room really exists" are reported
(full / locked / game already started / banned); "room not found", "relay not responding"
and "address unreachable" are silently dropped.

Two sources merged, player rooms first, then waiting rooms, then by player count descending:

| Source | Description |
|--------|-------------|
| Q Room List | `https://www.rtsbox.cn/api/paiwei/room_last.php`, player-hosted rooms with host / map / players / status |
| Official | Full official `masterserver` list (Auto Server + relay rooms), placed after player rooms |

Only queryable player rooms are kept: link entries, malformed fields, empty relay
placeholders, promotional wording, and entries whose player cap exceeds the game
limit (100 max) are skipped.

Cache: room data is shared globally with a 60-second sliding expiry; paging and filter state are isolated per "group + user", kept in memory (optionally mirrored to KV) and never expire. The index-to-room mapping lasts 10 minutes.

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
| `官Q模式` | Use QQ official-bot layout (Markdown cards + buttons), default false. Keep off for third-party bots |
| `压缩范围` | Fold scope: `none` / `room` / `list` / `diag` / `help` / `all`, default none. Third-party bots fold via a forward card; official bots fold via a Markdown code block |
| `指令唤醒词` | Must match AstrBot's global wake word, default `/` |
| `显示项目地址` | Show the project address, default true. Official bot = button at the card bottom (room info / room list only); third-party bot = small dotted line at the end of content messages (no footer on command replies or debug output) |
| `调试菜单` | Enable categorized debug output, default true |
| `max_players_display` | Max players shown, folds beyond, default 10 |
| `show_players` | Show player list, default false |
| `retry_times` | Retry count when probe name filtered, 0-10, default 3 |
| `list_page_size` | Room list page size, default 5 (controls message length only, no total room limit) |
| `list_default_full` | Default full-room filter, `hide` hides full rooms |
| `probe_max_per_msg` | Max probes per message, default 5 (1-20) |
| `探针名前缀` | Probe name prefix, default `ABAB`. 1-4 chars, CJK/letters/digits/`_`/`-` only |
| `推广开关` | Send project URL when a probe enters a room, default false |
| `推广冷却秒` | Per-room promo cooldown, default 180 |
| `房号冷却秒` | Per-room lookup cooldown, default 10 |
| `bin_cleanup_enabled` | Enable periodic bin cache cleanup while the plugin runs, default true |
| `bin_cleanup_interval_hours` | Cleanup check interval in hours, default 24 (once a day), min 1 |
| `bin_cleanup_max_age_hours` | Only delete bin files older than N hours, default 1 |
| `机器人QQ号` | Bot QQ, only used as sender uin for forward cards. Empty = auto-detect |
| `冷却提示` | Reply when a room is still cooling down, default false |
| `撤回` | Auto-recall config, `key:delaySeconds`; `#groupID` / `@userID` / `*`. An explicit `#groupID:off` entry means "closed here" and overrides an inherited `*` default |

### Project address display

`显示项目地址` is on by default, with different behaviour per platform:

| Platform | How it shows |
|---|---|
| Official bot (`官Q模式` on) | An extra "项目主页" jump button under room info and room list cards; no address text in the body |
| Third-party bot (`官Q模式` off) | **One extra dotted line at the end of every message** `⋯⋯ https://github.com/...` |

The address is a hardcoded constant and cannot be changed. Turning `显示项目地址` off hides both.

### bin cache cleanup

After each successful lookup, `room_info.py` dumps the raw 106/115 payloads to
`server_info_<version>_<target>_<timestamp>.bin` for protocol debugging. They are only
useful while debugging and pile up during long-running deployments.

Policy: **starting from plugin load, every `bin_cleanup_interval_hours` (default 24h)
it deletes bin files whose mtime is older than `bin_cleanup_max_age_hours` (default 1h).**

Implementation notes:

- Cleanup always targets the directory containing `room_info.py` (`BIN_DIR`), independent
  of the current working directory. Writes use the same path, so writer and cleaner can
  never disagree.
- The actual deletion runs in a separate subprocess (`room_info.py --cleanup-bins`) so it
  never blocks the event loop.
- A failed run does not kill the loop; it retries on the next tick.
- `terminate()` cancels the loop so nothing keeps running after unload.
- If the event loop is not ready at construction time, it is lazily started on the first
  incoming message.
- Interval and age threshold both have a 60-second floor to prevent a misconfigured 0
  from hammering the disk.

Manual cleanup:

```bash
python3 room_info.py --cleanup-bins               # delete files older than 1 hour
python3 room_info.py --cleanup-bins --max-age 600 # delete files older than 10 minutes
```

---

## Message example

Someone posts:

```
ADMIN: Other players can type HLBIFZ in 'direct join' to connect to this room.
```

The bot replies:

```
房间版本：1.15
地图名称：Crossing Large (10p)
初始资金：4000¤[1x倍率]
初始禁核：未禁用核蛋蛋
初始迷雾：重雾
地图模式：遭遇战
默认单位：常规模式（1个建造者）
单位上限：1100
共享控制：否
允许旁观：是
队伍锁定：否
当前人数：2/10
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