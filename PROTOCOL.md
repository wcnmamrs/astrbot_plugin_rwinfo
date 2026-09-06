# 铁锈战争 (Rusted Warfare) 联机协议逆向文档

> 来源：`room_info.py`（1.15 / 协议版本 176）实测逆向。
> 用途：房间信息查询探针。**只读房间信息，不参与战斗**，查询完自动发 111 退出。

---

## 0. 总体架构

```
┌────────┐   TCP 5123   ┌──────────────┐   178跳转   ┌──────────────┐
│ 客户端  │ ───────────► │ 房间中继服务器 │ ─────────► │ 房间服务器(实际)│
│ (探针)  │  160/161    │ x.relay.cor..│  (按房间ID) │              │
└────────┘             └──────────────┘            └──────────────┘
      │                                                      │
      └────────────── 注册110 ──────────────────────────────►│
                                    106(SERVER_INFO) ◄───────┘
                                    115(TEAMS)       ◄───────┘
```

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

`parse_115(payload, stream_ver)` 按流版本解析玩家记录，`_classify_name` 区分真人/AI/观众。玩家名字符处理：过滤控制字符（`c >= " " and c != 0x7f`）。

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

```
GET http://gs1.corrodinggames.com/masterserver/1.4/interface
    ?action=list&game_version=176&game_version_beta=false
Headers: User-Agent: rw android 176 en, Language: en
```

响应：首行表头 + 每行逗号分隔 CSV：
`id,name,ver,ip,?,port,?,?,?,desc,mode,status,?,?,?,players,max,...`

---

## 14. 直连模式

- 目标格式：`IP:port` 或 `IP`（默认 5123）
- 流程：connect(160, 无room_id变体) → 161 → 110 注册 → 收 106/115 → 111 退出

---

## 15. 注意事项

- 所有 int 均为**大端有符号**
- utf 字符串长度前缀 **uint16 大端**
- 被踢 150 的 utf 是**原因字符串**，插件靠关键词翻译
- 版本号：176=1.15 正式版，174=1.15p11 测试版
- 中继挑战 v8=5/6 的 POW 穷举上限 v10 可能很大，30s 超时兜底
- **探针行为**：查询完立即发 111 离开，不占房间位置、不影响房间人数