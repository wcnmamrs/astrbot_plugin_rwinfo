# Rusted Warfare Room Lookup (astrbot_plugin_rwinfo)

> AstrBot plugin · Auto-detect Rusted Warfare room IDs in group messages and dispatch probe bots to query and report room info

[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

Detects Rusted Warfare room IDs in messages and dispatches probe bots (`<prefix>探针01~99`) to join rooms and report back: room version / map / credits / fog / no-nukes / multiplier / mode / unit cap / default unit / shared control / spectators / team lock / player count / player list / room short ID.

Protocol notes in [PROTOCOL.md](PROTOCOL.md), version history in [CHANGELOG.md](CHANGELOG.md).

## Features

- **Auto room-ID detection**: numeric rooms (`o`/`q`/`r`/`s` + digits) and alphabetic rooms, ≥3 chars; failures are dropped silently
- Paged browser with `/铁锈列表`; `/铁锈查房 <index>` looks up a room from the last list
- Configurable probe names with auto-retry on name filtering; auto-recall, room cooldown, concurrent probe pool (`探针01~99`)
- In-room promotion (off by default), white/black list permission control per user and per group, built-in protocol probe with no third-party dependencies

## Install

Install from the AstrBot plugin store, or via WebUI:

1. Open the AstrBot admin panel and go to Plugin Management
2. Paste `https://github.com/wcnmamrs/astrbot_plugin_rwinfo` to auto-fetch, or upload a zipped package
3. Enable the plugin after install

Requires AstrBot and Python 3.9+, no third-party packages. If `room_info.py` is missing or too old, it is downloaded from the repository.

## Commands

Two permission tiers that do not overlap: **query** commands (help / list / lookup / paging) are available to everyone and are gated by the plugin's own white/black lists, mixed mode and private-chat policy — with no lists configured everyone in the group can use them; **config and diagnostics** commands are AstrBot admin only (`PermissionType.ADMIN`). An unrecognized `/铁锈xxx` command automatically returns the help menu.

| Command | Permission | Description |
|---------|-----------|-------------|
| `/铁锈查房帮助` | Everyone | Show command menu. Aliases `/铁锈查房菜单` `/铁锈帮助` `/rwhelp` |
| `/铁锈列表 [args]` | Allowlist | Paged room list. Aliases `/房间列表` `/铁锈房列表` `/rwlist` |
| `/铁锈下一页` `/铁锈上一页` | Allowlist | Room list paging |
| `/铁锈查房 <index\|roomID\|IP:port>` | Allowlist | Explicit lookup, including CNKD K rooms such as `RKC497` and raw `IP:port` |
| `/铁锈查房 状态` | Everyone | Today's query count, success rate, failure categories and latency |
| `/铁锈探针时解 <5-char suffix>` | Everyone | Decode the join time embedded in a probe name, e.g. `/铁锈探针时解 fkhk3`; a full probe name `<prefix>探针01_fkhk3` also works |
| `/铁锈全局解析 开\|关` | Admin | Global parsing switch, default on |
| `/铁锈模式 白\|黑\|混合` | Admin | List mode, default black; in mixed mode `@` takes precedence over `#`, names are exclusive |
| `/铁锈白名 +@userID` `/铁锈白名 +#groupID` | Admin | Whitelist add/remove, prints the list with no args |
| `/铁锈黑名 +@userID` `/铁锈黑名 +#groupID` | Admin | Blacklist add/remove, prints the list with no args |
| `/铁锈私聊策略 白\|黑` | Admin | Mixed-mode private-chat default policy |
| `/铁锈私聊开关 开\|关` | Admin | Enable private-chat lookup, default on |
| `/铁锈玩家列表 开\|关` | Admin | Show player list, default off |
| `/铁锈重试 [0-10]` | Admin | Retry count when a probe name is filtered |
| `/铁锈压缩 [scope]` | Admin | Message fold scope |
| `/铁锈撤回 [列出\|+\|-\|重置]` | Admin | Auto-recall config. `列出` shows all, `+`/`-` add or remove, `重置` clears; no args shows this session; `+ *:60` expands to every known group |
| `/铁锈忽略 [列出\|+\|-\|测试\|开\|关]` | Admin | Ignore regexes: matching messages are never parsed. 9 built-in rules block this plugin's and common lookup bots' output; add your own with `+ <regex>` |
| `/铁锈查房调试 [category]` | Admin | Print runtime state: sessions, recall, probe pool, list cache, config, official bot |
| `/铁锈查房调试菜单` | Admin | Show adjustable internal values and how to change them |
| `/铁锈官机探测` | Admin | Dump the official-bot channel structure (read-only) |
| `/铁锈官机测试` | Admin | Send a Markdown sample to verify the channel |

### Room list

```
/铁锈列表 [page]|[filters]|[搜索 keyword]|[刷新]
/铁锈下一页 / 铁锈上一页
```

| Argument | Description |
|----------|-------------|
| page | Number, `2` means page 2 |
| `房间中` / `游戏中` | Room status |
| `满` / `不满` | Whether the room is full |
| `真人` / `官方` | Data source |
| `搜索 keyword` | Fuzzy search by host name / map name |
| `刷新` | Ignore cache and refetch |

Arguments combine freely, e.g. `/铁锈列表 房间中 真人 2` is page 2 of the "waiting + player-hosted" result.

| Usage | Filter inheritance |
|-------|----------|
| `/铁锈列表` (no arguments) | Fresh query, all filters cleared |
| `/铁锈列表 2` (page only) | Reuse last filters, jump to page 2 |
| `/铁锈下一页` `/铁锈上一页` | Reuse last filters (same as passing only a page) |
| `/铁锈列表 房间中` (with filters) | Replace all old filters with this query's |

Filter and page state is isolated per "group + user" and persisted through AstrBot KV; page numbers never expire, so paging can continue after a plugin restart. Display format (5 entries per page by default):

```
1. 萧逸  1/10  房间中
   map: Crossing Large (10p)
2. SERVER  2/10  房间中
   map: Two Sides Remake (10p)
```

When the room name is `Unnamed` the host name is shown instead (CSV column 1 is the room name, column 7 is the host name). Two sources are merged: player rooms first, then waiting rooms, then by player count descending:

| Source | Description |
|--------|-------------|
| Q Room List | `https://www.rtsbox.cn/api/paiwei/room_last.php`, player-hosted rooms with host / map / players / status |
| Official | Full official `masterserver` list (Auto Server and relay rooms), placed after player rooms |

Only queryable player rooms are kept: link entries, malformed fields, empty relay placeholders, promotional wording, and entries whose player cap exceeds the game limit (100 max) are skipped. Room data is shared globally (60-second sliding expiry); the index-to-room mapping lasts 10 minutes and is rebuilt silently from the same page afterwards.

### Room lookup

```
/铁锈查房 <index|roomID|IP:port>
```

| Form | Description | Example |
|---|---|---|
| index | Number from the latest `/铁锈列表` result (per current page, mapping valid 10 min, silently rebuilt when stale) | `/铁锈查房 2` |
| numeric room | `o` `q` `r` `s` + digits, at least 3 chars | `/铁锈查房 r5132` |
| alphabetic room | Other leading letters, pure alphabetic, at least 3 chars | `/铁锈查房 hytvvi` |
| custom room (explicit only) | Relay letter + CJK characters (letters/digits allowed), 3~24 chars | `/铁锈查房 s九一大魔王` |
| any custom shape (explicit only) | A single word starting with a letter (`rsh`, `abc123`, `s4932XYZ`, …) | `/铁锈查房 rsh` |
| K room | CNKD share ID `RKC` / `KC` + 2~8 digits, or a full share link | `/铁锈查房 RKC497` |
| direct | `IP` or `IP:port` (port defaults to 5123) | `/铁锈查房 45.76.27.30:5125` |

The room ID is extracted as its core, so surrounding text does not matter: `R95898缺一` / `q12345来人` / `r5132缺一` → `r95898` / `q12345` / `r5132`; trailing letters are dropped `r5132abc` → `r5132`; a repeated prefix is collapsed `rr5132` → `r5132`; two IDs written back to back are split `s1024r5132` → `s1024`, `r5132` (`r5132r5132` is deduplicated).

Auto-detection and the command follow different rules: auto-parsing only accepts the fixed regexes above (numeric / alphabetic / K rooms), because room-ID shapes cannot be enumerated and mixing them into auto-parsing would misread ordinary sentences and waste probe slots — better to miss than to misfire. `/铁锈查房` force-parses: any single letter-leading word (CJK, kana, emoji and hyphens included, e.g. `h阿巴阿巴` / `rsh` / `s4932XYZ`) is queried as-is instead of being split by regex; failures are explained by the probe (no such room / already started / no response), and an argument that yields no room ID produces an explicit error rather than silence.

| | Auto-detect (room ID posted in chat) | Explicit lookup (`/铁锈查房`) |
|---|---|---|
| Allow raw IP | No | Yes |
| On failure | Silent, nothing posted | Reason reported |
| Purpose | Avoid spam from English words misread as room IDs | User asked explicitly, must answer |

Auto-detection only reports failures that imply the room really exists (full / locked / game already started / banned); "room not found", "relay not responding" and "address unreachable" are dropped silently. `房号冷却秒` (default 10) is isolated per session and stops the same room ID from being queried repeatedly in one conversation.

**Daily query statistics**: `/铁锈查房 状态` (also `统计` or `status`) reports today's total, successes, failures, success rate, average latency, success latency, failure categories and current probe usage; explicit and automatic lookups are both counted, and probe-name retries count as actual attempts. Data lives in `data/plugin_data/astrbot_plugin_rwinfo/stats.json` (never in the plugin directory), survives reloads and resets on the next local day; a legacy `.rwinfo_stats.json` is migrated read-only on first load (the old file is kept).

### Ignore regex

Auto-detection only asks "does this message contain a room ID", but room reports, room lists and error dumps posted by other bots (or by another instance of this plugin) are full of room IDs; without a guard they cascade into bots querying each other. Two layers, and a hit on either means the whole message is not parsed:

| Layer | Content | Editable |
|---|---|---|
| 9 built-in rules | Room info blocks (fields with a colon such as `房间版本：` `地图名称：`), field lines (`ID：` `当前人数：`), room-ID labels (`房号：r5132`), room lists, error reports (`[房间查询失败]` `[插件错误]` `查房无响应`), probe output, relay diagnostics, recall receipts, plugin UI | No |
| Custom | Config key `忽略正则`, one Python regex per entry, multi-line matching | Yes |

```
/铁锈忽略                     show built-in/custom rules
/铁锈忽略 + <regex>           add (several lines at once allowed, compile is validated first)
/铁锈忽略 - <index|regex>     remove
/铁锈忽略 测试 <text>         show which rule this text would hit
/铁锈忽略 开|关               master switch (on by default)
```

It only affects auto-detection; an explicit `/铁锈查房 s5132` still runs. Built-in rules require the field name to be followed by a colon (or a table separator), so `s5132 地图名称是什么` still triggers while `地图名称：Crossing 10p` is blocked.

## Configuration

Auto-persisted by the AstrBot framework, editable via WebUI, and saved immediately when changed by command.

| Key | Default | Description |
|-----|---------|-------------|
| `global_enabled` | true | Global parsing switch |
| `mode` | black | List mode: `white` / `black` / `mixed` |
| `白名单` / `黑名单` | empty | White/black lists (users / groups) |
| `混合_白名单` / `混合_黑名单` | empty | Mixed-mode independent lists; the plain lists stay intact when switching back |
| `混合_私聊策略` | white | Mixed-mode private policy: `white` block if not whitelisted / `black` block if blacklisted |
| `私聊启用` | true | Enable private-chat lookup handling |
| `debug` | false | Enable debug logging |
| `官Q模式` | false | Use the QQ official-bot layout (Markdown cards + buttons); keep off for third-party bots. Buttons appear only on lookup results, room lists and the help menu |
| `压缩范围` | none | Fold scope: `none` / `room` / `list` / `diag` / `help` / `all`; third-party bots fold via a forward card, official bots via a Markdown code block |
| `指令唤醒词` | `/` | Must match AstrBot's global wake word |
| `显示项目地址` | true | Official bot = button at the card bottom; third-party bot = small dotted line at the end of content messages (not on command replies or debug output) |
| `调试菜单` | true | Show debug commands in categorized form |
| `max_players_display` | 10 | Max players shown, folded beyond |
| `show_players` | false | Show the player list |
| `retry_times` | 3 | Retry count when a probe name is filtered (0-10) |
| `房号冷却秒` | 10 | Minimum interval for the same room ID in one session |
| `冷却提示` | false | Reply while a room ID is still cooling down (fully silent by default) |
| `probe_max_per_msg` | 5 | Max probes summoned per message (1-20) |
| `探针名前缀` | ABAB | Prefix of `<prefix>探针01`, 1-4 chars, CJK/letters/digits/`_`/`-` only; invalid chars dropped, over-long values truncated, empty result falls back to the default |
| `list_page_size` | 5 | Room list page size (controls message length only, no total room limit) |
| `list_default_full` | empty | Default player-count filter; `hide` hides full rooms |
| `推广开关` | false | Send the project URL after a probe joins a room |
| `推广冷却秒` | 180 | Per-room promotion cooldown; switching rooms allows it immediately |
| `机器人QQ号` | empty | Only used as the forward-card sender uin; empty = auto-detect |
| `bin_cleanup_enabled` | true | Periodically clean the bin cache while the plugin runs |
| `bin_cleanup_interval_hours` / `bin_cleanup_max_age_hours` | 24 / 1 | Cleanup check interval (hours, min 1) / only delete caches older than this many hours |
| `忽略开关` | true | Enable ignore rules; when off only the "plugin's own UI text" layer remains |
| `忽略正则` | empty | Custom ignore regexes (one Python regex per entry); matching messages are not parsed, maintained with `/铁锈忽略` |
| `撤回` | empty | Auto-recall config, `key:delaySeconds`, enabled when written and disabled when deleted; e.g. `#123456:60` / `@3856554669:30` / `*:60` |

Recall maintenance commands:

| Command | Effect |
|---|---|
| `/铁锈撤回` / `/铁锈撤回 列出` | Show this session's config / list all entries |
| `/铁锈撤回 + #group:60 @qq:30` | Add or modify (several entries allowed) |
| `/铁锈撤回 - #group @qq` | Remove (several entries allowed) |
| `/铁锈撤回 重置` | Clear everything |
| `/铁锈撤回 + *:60` | Expand to every known group; `列出` shows the resulting coverage |
| `/铁锈撤回 开\|关\|30` | Quick setting for this session |

`关` also works in a group that merely inherits the `*` default: it leaves `*` untouched and writes an explicit `#groupID:off` entry for that group (shown as "closed (overrides default)" in `列出`); send `开` or a number of seconds to restore it.

All of the following state is isolated per "session + user" and never leaks across groups:

| State | Key format | Description |
|---|---|---|
| `/铁锈列表` paging and filters | `g#groupID@userID` (group) / `p@userID` (private) | Holds the real address cached for that page index; a leak would query the wrong room |
| Room cooldown | `session_key\|roomID` | A room queried in group A does not affect group B |
| Auto-recall config | `#groupID` / `@userID` / `*` | One list, prefix distinguishes groups and users, `*` acts as the global default in groups |

### Project address display

`显示项目地址` is on by default and behaves differently per platform:

| Platform | How it shows |
|---|---|
| Official bot (`官Q模式` on) | An extra "项目主页" jump button at the card bottom of lookup results, room lists and the help menu; no address text in the body |
| Third-party bot (`官Q模式` off) | One extra dotted line with the address `⋯⋯ https://github.com/...` at the end of lookup results, room lists and help; not appended to command replies, debug output or errors |

The address is a hardcoded constant and cannot be changed; turning `显示项目地址` off hides both.

### bin cache cleanup

After each successful lookup `room_info.py` dumps the raw 106/115 payloads to `server_info_<version>_<target>_<timestamp>.bin`, useful only for protocol debugging. Policy: starting from plugin load, every `bin_cleanup_interval_hours` (default 24h) it deletes bin files whose mtime is older than `bin_cleanup_max_age_hours` (default 1h).

- The cache never lives in the plugin directory: `--bin-dir` first, then `--state-dir/bin_cache` (the plugin passes `data/plugin_data/astrbot_plugin_rwinfo/`); with neither nothing is written, so a standalone probe run must pass `--state-dir`. Writer and cleaner share one path
- If the directory is not writable the dump is skipped with a single stderr note; room output and the leave packet are unaffected. Deletion runs in a separate subprocess (`room_info.py --cleanup-bins`), never blocks the event loop, and a failed run retries next tick
- `terminate()` cancels the loop, and if the event loop is not ready at construction time it starts lazily on the first message; interval and age threshold both have a 60-second floor

```bash
python3 room_info.py --cleanup-bins               # delete files older than 1 hour
python3 room_info.py --cleanup-bins --max-age 600 # delete files older than 10 minutes
```

## Message example

Someone posts `ADMIN: Other players can type HLBIFZ in 'direct join' to connect to this room.` and the bot replies automatically:

```
房间版本：1.15
地图名称：Crossing Large (10p)
初始资金：4000¤[1x倍率]
地图模式：遭遇战
初始禁核：未禁用核蛋蛋
初始迷雾：重雾
默认单位：常规模式（1个建造者）
单位上限：1100
共享控制：否
允许旁观：是
队伍锁定：否
使用模组：原版
当前人数：2/10
ID：hlbifz
 玩家列表：
  A1：abab
  A3 (AI)：AI - Hard
```

`使用模组` has four shapes, all derived from the room info packet itself: the display name reported by the mod (`使用模组：炮塔科技1.4`, up to 3 names, then "等 N 个"), `使用模组：有（自定义单位 1277 个，未上报名称）` (unit count far above vanilla 121 but the mod named nothing), `使用模组：有（地图来自模组）` (count still 121 but the map starts with `MOD|`), and `使用模组：原版`. Mods that only change numeric values leave no trace in the packet and still show `原版` — a limit of the protocol, not a parsing failure; column 21 of the room list (the mod-name column) is ignored because the host's client fills it in and such mods leave it empty.

Map names are not always plain names: values seen in the wild include `NEW_PATH|maps2/10p西班牙混战by-MP97.tmx`, `MOD|36976564B8…6BBB5B6AC//maps/ai-弱智AI试炼场(2p)`, `/SD/rusted_warfare_maps/MOD|sw3058511896//maps/[p9]Coliding Powers (4v5).tmx`, `0    1941欧洲二战：至暗黄昏   40人  by哟西` and `[z;p10]Crossing Large (10p).tmx`; the protocol/path junk in front is stripped and only the map name remains (e.g. `ai-弱智AI试炼场(2p)`). Cleaning order is fixed: protocol prefix → mod marker (hash or `sw` workshop ID) → last path segment → repeated bracket-parameter and numeric-prefix stripping → collapse spaces → drop extension. Slashes inside names such as `1v1/2v2 混战地图` are not paths and are kept; an empty result shows "未知地图". The room list's map and description columns use the same cleaning.

When a probe name is filtered the plugin reports progress from `[房间查询] 重试中(1/3)` through `(3/3)` before retrying; when the room cannot be joined the reason is translated:

```
[房间查询失败] 房间已满，无法加入
[房间查询失败] 游戏已经开始
[房间查询失败] 房间已锁定
```

## Command-line tool

The built-in probe runs standalone with the standard library only:

```bash
python3 room_info.py                      # no args in a terminal: interactive mode
python3 room_info.py R5132                # query a relay room
python3 room_info.py HLBIFZ               # query an international room
python3 room_info.py 18.216.139.119:5125  # direct-connect query
python3 room_info.py --list               # fetch the room list
python3 room_info.py R5132 --show-players # show the player list, add --debug for debug output
```

### Interactive mode

A resident process taking one line at a time, handy for trying several room IDs, switching filters or toggling the player list. Start it with `python3 room_info.py -i` (or `--interactive` / `--shell`; no args in a terminal also enters it):

```
rwinfo> r5132                  # a bare room ID queries it
rwinfo> q7765 --show-players   # per-command options, same as one-shot mode
rwinfo> list --joinable --search survival
rwinfo> list --addr             # room ID / direct address / host (needed for official rooms' IP:port/UUID)
rwinfo> json --status battleroom / debug / status / exit
```

Commands: `<room id>`, `query`, `list`, `json`, `cleanup`, `name`, `players`, `limit`, `promote`, `cooldown`, `debug`, `status`, `help`, `exit`. `Ctrl-C` discards the current line (or aborts a running query); leave with `exit` or `Ctrl-D`. Promotion, probe name and player-list settings stay in the session, so they need not be repeated. Results go to stdout while prompts and status lines go to stderr, so `python3 room_info.py -i < cmds.txt > out.txt` still yields clean results; the session supports line editing and arrow-key history (via `readline`) and `# comments` at line end. `-i` is mutually exclusive with one-shot flags: `-i --list-json` or `-i R5132` run in one-shot mode with a notice, while `-i --help` always prints help. The plugin always passes a room ID, so it never enters interactive mode.

Colors are enabled on a terminal: report field names bold, the `ID` line highlighted, the list status cell colored by state, failures in red, the closing line dimmed with a colored exit code, and a cyan prompt. Colors affect terminal output only: `--color` takes `auto` (default, on only when stdout is a terminal), `always`, or `never`, and `RWINFO_COLOR` works as an env fallback. With `NO_COLOR` set, `auto` turns colors off while `--color=always` still honors the explicit request. Pipes and files get no escape sequences.

## Directory structure

```
astrbot_plugin_rwinfo/
├── main.py            # plugin logic (message listen / ID extract / query schedule / retry / translate)
├── room_info.py       # protocol probe (room-info core, runs standalone)
├── PROTOCOL.md        # protocol reverse-engineering notes
├── CHANGELOG.md       # version history
├── README.md / README_EN.md   # Chinese / English docs
├── metadata.yaml / _conf_schema.json   # plugin metadata / config schema
└── LICENSE / .gitignore
```

Runtime data is not written here (plugin updates replace this directory) but in `data/plugin_data/astrbot_plugin_rwinfo/`:

| File | Content |
| --- | --- |
| `stats.json` | Today's counters behind `/铁锈查房 状态` (resets daily) |
| `promote_state.json` | Per-room cooldown for the in-room promotion |
| `bin_cache/` | Raw 106/115 packets saved during lookups (protocol debugging, cleaned periodically) |

A leftover `.rwinfo_stats.json` in the plugin directory is migrated read-only on first load (the old file is kept). The data directory is only `StarTools.get_data_dir()` (or `RWINFO_PLUGIN_DATA` when set, mainly for tests); if neither is available nothing is persisted — stats stay in memory and the probe runs without `--state-dir`, never falling back to `~/.rwinfo`.

## Notes

- Queries run in a subprocess calling `room_info.py` and are isolated; the probe sends 111 to leave afterwards and never occupies a room slot
- Private messages are checked against `@userID`; group messages check both `#groupID` and the sender's `@userID` (the user blacklist applies in groups too)
- The player list follows slot order, empty names show as `(空)`; when a room is in the "returned to battleroom after start" state the server does not update its status, so humans can still join while probes are rejected — game behaviour
- Some servers (e.g. 青阳云服务) require a human check after the handshake: the player must type an access code within 15 seconds. The probe never answers it. It exits immediately and reports two lines — `[房间查询失败] 该服务器启用了人机接入验证（或客户端白名单），探针无法自动完成，需玩家本人在客户端接入` plus `服务端提示：<server text>` — with exit code `6` (manual verification required). Those queries count as failures in the daily stats, without a separate category
- This plugin is for learning and entertainment; do not use it to spam rooms or disturb other players

## License

[MIT](LICENSE)

This plugin is independently developed and not affiliated with Corroding Games. The protocol reverse engineering is for interoperability and learning only. If the game's developers object, please contact the author.
