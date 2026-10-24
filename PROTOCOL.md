# 铁锈战争 (Rusted Warfare) 联机协议逆向文档

> 来源：`room_info.py`（1.15 / 协议版本 176）实测逆向。
> 用途：房间信息查询探针。**只读房间信息，不参与战斗**，查询完自动发 111 退出。

---

## 0. 总体架构

探针流程：**TCP 连接中继服务器 `x.relay.corrodinggames.com:5123`（x=房号首字母小写）→ 发 160 握手 → 收 161 → 发 110 注册 → 收 106(房间信息)+115(玩家列表) → 发 111 退出**。若收到 178 则跳转到实际房间服务器（或直连）。

- **官方主服务器**：`http://gs1.corrodinggames.com/masterserver/1.4/interface`（备：`gs4.corrodinggames.net`），用于 `--list` 拉房间列表
- **中继域名规律**：`<房号首字母小写>.relay.corrodinggames.com`，默认端口 **5123**
- **直连**：`IP:5123`（默认端口 5123，旧版 5125）

---

## 1. 二进制基础类型（大端序）

| 类型 | 字节数 | 说明 |
|------|-------|------|
| `byte` | 1 | 有符号字节 |
| `boolean` | 1 | `0x01`=true / `0x00`=false |
| `int32` | 4 | 有符号大端 `>i` |
| `int64` | 8 | 有符号大端 `>q` |
| `float32` | 4 | 大端 `>f` |
| `utf` | 2+N | `uint16` 长度 + UTF-8 字节（长度上限 65535） |
| `nullable_str` | 1+2+N | `boolean`(有值?) + `utf` |

---

## 2. 帧格式

```
pack_frame(mtype, payload) = int32(payload_len) + int32(mtype) + payload
read_frame  = 读 8 字节头 → int32 长度 + int32 类型 → 读 payload
```

- 长度非法（<0 或 >50000000）直接丢弃
- 所有消息都走这个壳，**mtype 是消息路由键**

---

## 3. 关键消息类型表

| mtype | 方向 | 名称 | 说明 |
|-------|------|------|------|
| **160** | C→S | HELLO | 客户端握手（携带 magic/版本/名字/room_id） |
| **161** | S→C | HELLO_ACK | 服务器握手响应（magic/proto_ver/e/app_ver/server_id/conn_id/challenge_y/extra） |
| **163** | S→C | RELAY_VERSION | 中继版本号（int32） |
| **110** | C→S | REGISTER | 注册进房间（关键：带哈希挑战应答） |
| **106** | S→C | SERVER_INFO | **房间信息**（地图/资金/迷雾/禁核/单位/人数...） |
| **115** | S→C | TEAMS | 队伍/玩家列表 |
| **141** | S→C | CHAT | 房间聊天 |
| **150** | S→C | KICK | 被踢（utf 原因） |
| **151** | S→C | RELAY_CHALLENGE | 中继挑战（防滥用） |
| **152** | C→S | RELAY_ACK | 中继挑战应答 |
| **113** | S→C | PASSWORD_REQ | 需要密码 |
| **111** | 双向 | LEAVE | 离开（utf 原因） |
| **108/109** | S→C/C→S | PING | 心跳：108(int64 id) → 109(int64 id + byte 1) |
| **178** | S→C | JUMP | 跳转指令（utf 地址 → 换服务器连） |

---

## 4. 握手流程（160 → 161）

### 4.1 客户端发 160

**带 room_id（中继入房）**：
```
utf  magic = "com.corrodinggames.rts"
int32 4
int32 client_version (176)
int32 1
nullable_str room_id   ← 要查的房间号，如 "HLBIFZ"
utf  name              ← 探针名
utf  network_client_id ← uuid4 字符串
utf  ""
```

**无 room_id（直连 / 列表）**：变体 proto_variant 0/2/3，结构略有差异（版本+随机种子或版本+语言）。

### 4.2 服务器回 161

```
utf  magic
int32 proto_ver
int32 e            ← 服务器协议版本(注册用)
int32 app_ver
utf  apk
utf  server_id
int32 conn_id
int32 challenge_y   ← 注册挑战值
int32 extra         ← 可选
```

161 之前可能收到 **163**（中继版本）、**151**（中继挑战）、**108**（心跳，须回 109）。**收到 150 = 被踢，111 = 断开**。

---

## 5. 中继挑战 151 → 152（防滥用工作证明）

服务器发 **151**，结构：

```
int32 v7       ← 挑战ID(回显)
int32 v8       ← 挑战类型
boolean b1; if b1: int32 bf_i
boolean b2; if b2: int32 bf_j
utf s1         ← 目标哈希(可选)
utf s2         ← 盐(可选)
int32 v10      ← 穷举上限(可选)
```

按 `v8` 类型计算应答 `v0`：

| v8 | 含义 | v0 计算 |
|----|------|---------|
| 0 | 空挑战 | `""` |
| 1 | 数字 | `str(bf_j)` |
| 2 | 信用挑战 | `challenge_e(bf_i)`（见 §7） |
| 3/4 | 哈希挑战 | `f_c(f"{bf_i}|{bf_j}")` = sha256 前14位大写 |
| 5/6 | **POW 穷举** | 找 `i ∈ [0, v10]` 使 `f_c(s2 + str(i)) == s1`，超时 30s 发 `-1` |
| 其他 | | `"max"` |

> v8=5 且 s1 非空时，`(s1, s2, v10)` 会加入 `relay_entries`（可能是房号跳转线索）。

应答 **152**：

```
int32 v7           ← 回显挑战ID
int32 v8           ← 回显类型
utf   v0           ← 计算结果
float32 耗时(ms)   ← 计算耗时毫秒
```

---

## 6. 注册进房 110（核心）

```
utf  magic = "com.corrodinggames.rts"
int32 reg_ver = 5
int32 ver      ← 服务器 e 或客户端版本
int32 ver
utf  name
nullable_str password_hash  ← hash_f(password) = md5(sha256(pass))，无密码传 null
utf  magic
utf  sha256_hex(network_client_id + server_id)   ← 身份校验
int32 unit_checksum       ← 单位校验和，失败换 0/-1/1 重试
utf  challenge_e(v_challenge)   ← 信用挑战应答
utf  hash_e_int(w_challenge)    ← 颜色挑战应答 = "#%06X" & 0xFFFFFF
```

**单位校验和错误**（返回含 "units"/"Integrity"）会自动断线重连换 checksum 重试（`[0, -1, 1]`）。

---

## 7. 哈希算法库

```python
sha256_hex(s)  = sha256(s).hexdigest().upper()        # 64位大写hex
md5_lower(s)   = md5(s).hexdigest()                   # 32位小写hex
hash_f(s)      = md5_lower(sha256_hex(s))             # 密码哈希
f_c(s)         = sha256(s).hexdigest().upper()[:14]   # 前14位大写hex
hash_e_int(i)  = "#%06X" % (i & 0xFFFFFF)             # 颜色挑战

challenge_e(i):  # 信用挑战 → 拼接13段
  c:{i}, m:{java_int32(i*87+24)},
  0..8: {java_int32(CREDITS_MAP[k] * (11+k) * i + (i%2==1)*0)}  # 每段系数 11..19
  t1:{java_double_str(4000.0*11.0*i)}, d:{java_int32(i*5)}
```

> CREDITS_MAP = {0:4000, 1:0, 2:1000, 3:2000, 4:5000, 5:10000, 6:50000, 7:100000, 8:200000}
> `java_int32` 模拟 Java int 溢出（>0x7FFFFFFF 减 0x100000000）。
> 注意：上面 `challenge_e` 中 `0..8` 段实际代码为 `d[k]* (11+k) * i` 或 `d[k]*(11+k)+i`（奇偶不同），实现以 room_info.py 第 215-225 行为准。

---

## 8. 房间信息 106（SERVER_INFO）解析

106 是**查询的核心**。结构（依据客户端 ae.java 发送端 525-551 / as.java 字段语义）：

```
utf   magic
int32 app_ver        ← 版本 (176=1.15, 174=1.15p11)
int32 mode           ← 0遭遇战 1自定义 2存档
utf   mapname        ← 地图名(可能带.tmx)
int32 credits        ← 资金索引 (CREDITS_MAP)
int32 fog            ← 0无雾 1轻雾 2重雾
bool  revealed
int32 ai_diff
byte  sub_ver
bool  proxy_h
bool  proxy_i
int32 unit_cap       ← 单位上限
int32 unit_cap_max
int32 units          ← 默认单位(1常规 2小军队 3三工程师 4三工程师无CC 9自定义 100+可选单位)
float32 multiplier   ← 收入倍率(1.0=1x, 1.5=1.5x...)
bool  no_nukes       ← 禁核
bool  flag_j
bool  has_custom     ← 是否有Mod
[if has_custom] customUnits块:
    utf "customUnits" + int32 块长 + 块内容
    块内: int32 ver(<2无扩展) + [bool z + bool] + int32 cnt
          cnt个: utf单位ID + int32 + bool + nullable_str显示名 + int64 checksum + int64 [+ nullable_str]
bool  shared          ← 共享控制
bool  teamlock        ← 锁定队伍
bool  flag_n
bool  spectators      ← 允许旁观
bool  roomlock        ← 房间锁定
int32 seed
```

> 块解析容错：customUnits 中途失败则 seek 到块尾，后续字段不错位。

---

## 9. 队伍/玩家 115（TEAMS）

服务器按**队伍槽位顺序**广播玩家记录（官方客户端 `ae.java` 发送端 + `p.b()` 写入）。

```
外层: int32 own_player_id + [版本>=141] bool full_load + int32 team_count
      + readUTF("teams") + int32 block_len + gzip块
块内: 每条玩家记录以分隔符 ff ff d8 f1 切分, 每条记录格式:
      boolean exists + int32 is_ai + byte slot + int32 credits
      + int32 team + nullable_str name + boolean X
```

解析要点：
- **`own_player_id`**：服务器告诉本探针的槽位号，用于过滤自己的探针记录。
- **记录格式**（7 字段）：`exists(1) + is_ai(4) + slot(1) + credits(4) + team(4) = 14 字节头 + nullable name + bool X`。
- **AI 识别**：`_classify_name` 区分真人/AI/ID-only。AI 名形如 `"- Hard"`/`"AI - Impossible"`。
- **Emoji 容错**：玩家名解码用 `_decode_lenient`，处理游戏端 UTF-16 代理对误当 UTF-8 的乱码。
- **坑（r12345 实测）**：段内开头可能含不定长 `00` 填充。若整块逐字节滑动扫描会误匹配填充里的"假记录头"（exists=1 但 name=None、team 错位），导致**真人房主丢失**（只剩 AI）。修复：**按分隔符切段，每段只取名字非空且字段合法的真实记录**。

**显示**：槽位标签 `队伍字母(A-J)+编号`；AI/ID-only 标记显示在槽位后、名字前（如 `B2 (AI): AI - Impossible`）。

---

## 10. 跳转 178（中继→实际服务器）

服务器在中继阶段发 **178**，内容含地址字符串（可能带 `[TCP]`/`[UDP]` 前缀）：

```
解析: byte b0 + int32 i1 + bool b2 + int32 i3 + utf addr
     （legacy 回退: 直接读 utf addr）
```

收到后：
1. 关掉当前连接
2. `addr = re.sub(r'^\[(TCP|UDP)\]\s*', '', addr)`
3. 若 `addr` 是 `host:port` 或 `host` → 直连目标（或**递归 relay_session 下一跳**）
4. 若格式为 `xxx/rid` 之类 → 拆分 host/端口/新房间号继续中继

**深度限制**：递归 depth+1，防环。

---

## 11. 查询流程（relay_session 完整时序）

```
1. TCP连接 x.relay.corrodinggames.com:5123 (x=房号首字母小写)
2. 发 160 (带 room_id)
3. 循环收包:
   - 161 → 记录 server_id/conn_id/challenge_y，继续
   - 163 → 记 relay_ver，继续
   - 151 → 计算应答发 152（可能多次）
   - 108 → 回 109
   - 178 → 跳转（递归/直连）
   - 111/150 → 退出（150=被踢，原因utf）
4. 注册: 发 110 (挑战应答+身份哈希+单位校验和)
5. 收包:
   - 106 → SERVER_INFO (解析§8) + 记 last_server_info
   - 115 → TEAMS (解析§9)
   - 141 → 聊天（忽略）
   - 113 → 需要密码
   - 150 → KICKED: 原因
   - 151 → 继续应答152
   - 111 → 被断开
6. 超时(默认5s)或收到关键消息后: 发 111 退出 → 输出格式化结果
```

**输出格式化**（format_room）：版本/地图/资金+倍率/禁核/迷雾/模式/默认单位/单位上限/共享控制/旁观/队伍锁定/人数/Mod/ID + 可选玩家列表（折叠超过 max_players_display）。

### 11.1 从列表序号进入查询

`/铁锈列表` 的每条都会带一个**可查目标**，`/铁锈查房 N` 直接拿它去查：

| 来源 | 目标 | 说明 |
|---|---|---|
| Q Room List（qroom） | 房号 `Q<internal_id>` | 走 `q.relay` 三跳，见 §13.4 |
| Official（masterserver） | `ip:port` | 直接 `direct_main` 连 |

**中继入口地址不可直连**（实测）：`18.216.139.119:5158` 等入口只做 UPList 注册，
带任何 room_id 都只回 161 握手，不给房间信息；直连则被踢
`Your client is newer then the server`。这类地址在列表里不视为可查目标，
排序时也会排到后面，避免占满首页。

---

## 12. 错误与状态字符串

| 返回 | 含义 | 插件翻译 |
|------|------|---------|
| `KICKED: ...` | 被踢 | 按原因翻译：No free slots→房间已满 / Room is locked→房间已锁定 / game already started→游戏已经开始 / Name Check→探针名被过滤 / banned→被封禁 / kicked→被移出 |
| `PASSWORD_REQUIRED` | 房间需密码 | — |
| `DISCONNECTED: ...` | 被断开 | — |
| 无输出 | 房间不存在/英文误报 | **静默丢弃**（插件不回复） |
| `NO_RESPONSE` | 注册无响应 | — |

---

## 13. 官方房间列表（masterserver）

### 13.1 拉取列表

```
GET http://gs1.corrodinggames.com/masterserver/1.4/interface
    ?action=list&game_version=176&game_version_beta=false
Headers: User-Agent: rw android 176 en, Language: en
```

备用：`http://gs4.corrodinggames.net/masterserver/1.4/interface`（参数相同）。
两个服务器返回的列表**内容不同**，客户端会并行请求；插件只用第一个成功的。

响应：首行表头 `CORRODINGGAMES[1.0]` + 每行 22 列逗号分隔 CSV。

### 13.2 CSV 字段（逐列核实）

按客户端 `gameFramework/j/r.smali` 的赋值顺序反推，实测 340 行样本验证：

| 列 | 字段 | 说明 |
|---|---|---|
| 0 | `id` | 房间 UUID，`u_...` 形式；中继房也用它 |
| 1 | `name` | 房名，绝大多数是 `Unnamed` |
| 2 | `ver` | 版本号 int（176 / 174） |
| 3 | `ip` | 对外 IP；LINK 条目为 `url:...` |
| 4 | `lan_ip` | 内网 IP；中继房常见 `127.0.0.1` / `10.0.0.1` |
| 5 | `port` | 端口 |
| 6 | ? | bool |
| 7 | **`host`** | **房主名**（`created_by`）。真正的标识在这里，不在第 1 列 |
| 8 | ? | bool |
| 9 | `desc` | 描述 / 地图名 |
| 10 | `mode` | `skirmishMap` / `customMap` / `LINK` |
| 11 | `status` | `battleroom` / `ingame` / `LINK` |
| 12 | `game_ver` | 版本字符串（`1.15` / `1.15-RN` / `ANY`） |
| 13 | ? | bool |
| 14 | `desc2` | 描述备用列 |
| 15 | `players` | 当前人数（**房主自报，可伪造**） |
| 16 | `max` | 人数上限（同上） |
| 17 | ? | bool |
| 18 | `uuid2` | UUID 副本 |
| 19 | ? | bool |
| 20 | ? | 常为空 |
| 21 | **`A`** | **加入方式字段**，见 13.3 |

> 注意：早期文档把第 7 列标成 `SERVER/CLIENT`，是错的。它是房主名，
> 中继入口标识 `RELAY-CN 入口` 就出现在这一列。

### 13.3 A 字段（协议层，插件不使用）

CSV 第 21 列 `A` 决定客户端如何加入，见 `MultiplayerLobbyActivity/gl.java`：

```java
if (fVar2.A == 0) str = fVar2.c + ":" + fVar2.g;                    // 直接连 ip:port
else              str = "get|" + fVar2.b + "|" + fVar2.A + "|" + fVar2.m + "|" + fVar2.g;
```

`A != 0` 时客户端会走 `action=get&game_id=<UUID>&c=<挑战码>` 向 masterserver
换取真实地址，挑战码由 `A` 经 `gameFramework/j/m.java` 的 `a(int i)` 推导
（`MD5("x"+A)[:10]` 起，按 `A` 的量级切换前缀与长度）。

**插件实现了这条链路。** 官方列表里 `A != 0` 的房间绝大多数共用同一个
UPList 注册入口（实测 `18.216.139.119:5158`，占比约 2/3），那只是注册端口，
直连会被踢 `Your client is newer then the server`。

处理方式：

- 列表展示**不含地址**，用户只看到序号、房主、人数、状态、地图
- `/铁锈查房 <序号>` 命中这类条目时，插件先向 masterserver 发 `action=get`
  换出真实地址，再连真实地址；**整个过程不产生任何面向用户的消息**，
  解析结果只在内部用于建连
- 解析失败（`A` 超出推导范围、`get` 返回非 OK、地址为空）则回退到列表地址，
  连接失败时只回传业务原因

> `action=get` 返回的是房主自报的公网地址。房主若在自己电脑上开房且没做
> 端口映射（实测多为 `:5123` 住宅宽带 IP），TCP 层直接超时，客户端同样连不上。
> 实测真人房 16/32 可查，其余多为这种不可达地址。

### 13.4 Q Room List（qroom）—— 房号是 `Q<internal_id>`

数据源 `https://www.rtsbox.cn/api/paiwei/room_last.php`（RW++ 启动器）。

**接口返回 10 个字段，但客户端只用 6 个**：`internal_id` / `map_name` /
`current_players` / `max_players` / `is_started` / `host_name`。

`host_ip` / `host_port` / `myhost_ip` **客户端完全不读**（实测 53 条 `host_ip:5129`
的 TCP/UDP 全部超时，是房主公网出口 IP，不是游戏端口）。误把它当游戏地址是本插件
早期版本查不到 qroom 房的原因。

房号规则（`BaseNetImpl$init$6$1` 构造 `RoomDescription`）：

```
customIp = "Q" + internal_id       // 大写 Q
roomId   = 0
```

`RoomDescription.addressProvider()`：

```java
if (roomId == 0) return customIp + ":" + port;             // 这里返回 "Q<id>:0"
else             return "get|" + uuid2 + "|" + roomId + "|" + pwd + "|" + port;
```

再经 `ae.java` 的房号转换（不含 `:` `.` `/` `\`，长度 > 4）得到：

```
Q.relay.corrodinggames.com/Q<internal_id>
```

**三跳链路**（实测 `Q30456`）：

```
1) q.relay.corrodinggames.com:5123  带 room_id=Q30456 发 160
   ← 178 跳转，地址 "114.66.9.225:5129/Q30456"
   （房号不存在时反而回 161 + 117「房间ID不存在」）

2) 连 114.66.9.225:5129，room_id 用 '/' 之后的 "Q30456"
   ← 161 → 151 挑战 → 110 → 141 房间聊天 / 115

3) ← 又一个 161，server_id 形如 "<uuid>"，magic 是 com.corrodinggames.rts.zx
   （铁锈盒子层）→ 用新 sid/conn/chal 重发 110
   ← 106 SERVER_INFO（完整房间信息）
```

`178` 地址解析对齐客户端 `ae.java`：

```java
if (str.contains("/")) {
    int i = str.indexOf("/");
    roomId = str.substring(i + 1).trim();   // '/' 之后 = 新房间号
    str    = str.substring(0, i);           // '/' 之前 = host:port
}
```

**`_wait_161` 必须处理 178**：中继在房号存在时直接回 178 而不发 161。
早期版本没有这个分支，178 落到 `else` 只打印一行，然后继续死等 161 直到超时，
表现为「握手失败/未收到房间服务器响应」——这会让真实存在的房间被误判为不存在。

---

## 14. 直连模式

- 目标格式：`IP:port` 或 `IP`（默认 5123）
- 流程：connect(160, 无room_id变体) → 161 → 110 注册 → 收 106/115 → 111 退出
- 连接失败必须输出 `[房间查询失败]`，不能抛 traceback：
  直连地址未必是游戏服务器（可能是 Web / 其他服务），连接失败属正常情况，
  而插件要靠 `[房间查询失败]` 前缀归类失败原因。

---

## 15. 注意事项

- 所有 int 均为**大端有符号**
- utf 字符串长度前缀 **uint16 大端**
- 被踢 150 的 utf 是**原因字符串**，插件靠关键词翻译
- 版本号：176=1.15 正式版，174=1.15p11 测试版
- 中继挑战 v8=5/6 的 POW 穷举上限 v10 可能很大，30s 超时兜底
- 客户端判定房号只看 6 个条件（非空、不含 `:` `.` `/` `\`、非 `localhost`），
  然后取**首字符**拼 `<首字母>.relay.corrodinggames.com/<房号>`。
  实测形态由**中继服务端**决定：`o` `q` `r` `s` 只收数字房，其他字母只收纯字母房
- 中继入口（`18.216.139.119:5158` 等）只接受 UPList 注册，不接受加入请求；
  带任何 room_id 握手都只返回 161，不会给房间信息
- **bin 缓存**：每次查到房间信息后，原始 106/115 报文写到
  `server_info_<版本>_<目标>_<时间戳>.bin`（位于 `room_info.py` 同目录，即 `BIN_DIR`）。
  这些是调试产物，不参与查询逻辑。
  - 清理入口：`python3 room_info.py --cleanup-bins [--max-age 秒]`
  - 插件侧从启动起每隔 `bin_cleanup_interval_hours`（默认 24h）触发一次，
    删除 mtime 超过 `bin_cleanup_max_age_hours`（默认 1h）的文件
  - 读写都用 `BIN_DIR` 绝对路径，不依赖 cwd：早期版本用 `os.listdir('.')`，
    换工作目录调用时会把别的目录当目标，实际一个也删不掉
  - 间隔/阈值均有 60s 下限，防止配成 0 后反复执行
- **探针行为**：查询完立即发 111 离开，不占房间位置、不影响房间人数