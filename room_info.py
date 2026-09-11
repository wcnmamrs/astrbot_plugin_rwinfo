#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
room_info.py - 铁锈战争房间信息查询工具（一体化, 1.15）
用法:
  python3 room_info.py R5132               # 中继房间
  python3 room_info.py 18.216.139.119:5125  # 直连服务器
  python3 room_info.py --list               # 官方房间列表
  python3 room_info.py R5132 --debug        # 开启调试输出
  python3 room_info.py R5132 --show-players # 显示玩家列表
"""
import gzip
import hashlib
import io
import os
import re
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
import uuid
import random
import zlib

# 全局安静模式（插件调用时设为 True，屏蔽调试输出）
QUIET = True  # 默认静默: 只输出最终结果, --debug 显示完整流程
DEBUG_MODE = False
SHOW_PLAYERS = False   # 默认不显示玩家列表

PROTO_MAGIC = "com.corrodinggames.rts"
MAGIC_INTL = "com.corrodinggames.rts"
VERSIONS = {174: "1.15p11", 176: "1.15"}
DEFAULT_VER = 176
DEFAULT_PORT = 5123
DEFAULT_NAME = "abab-探针"

MASTERSERVERS = [
    "http://gs1.corrodinggames.com/masterserver/1.4",
    "http://gs4.corrodinggames.net/masterserver/1.4",
]

CREDITS_MAP = {0: 4000, 1: 0, 2: 1000, 3: 2000, 4: 5000,
               5: 10000, 6: 50000, 7: 100000, 8: 200000}
AI_DIFF = {-2: "Very Easy", -1: "Easy", 0: "Medium", 1: "Hard", 2: "Very Hard", 3: "Impossible"}
FOG = {0: "off", 1: "basic", 2: "los"}
GAME_MODES = {0: "Skirmish Map", 1: "Custom Map", 2: "Saved Game"}
STARTING_UNITS = {1: "Normal (1 builder)", 2: "Small Army", 3: "3 Engineers",
                  4: "3 Engineers (No CC)", 5: "Experimental Spider", 9: "Custom"}

# ================================================================ 二进制协议工具

def _decode_lenient(b: bytes) -> str:
    """容错解码: 处理游戏端把 UTF-16 代理对误当 UTF-8 发送的情况.

    正常 UTF-8 解码遇到 ED A0-BF ... 会抛 UnicodeDecodeError / 替换为 U+FFFD.
    这里手动解析: 合法 UTF-8 序列照常, 代理对 (ED xx) 还原为正确 Emoji 码点.
    """
    out = []
    i = 0
    n = len(b)
    while i < n:
        c = b[i]
        # 单字节 ASCII
        if c < 0x80:
            out.append(chr(c))
            i += 1
            continue
        # 2 字节 UTF-8
        if 0xC2 <= c <= 0xDF and i + 1 < n and 0x80 <= b[i+1] <= 0xBF:
            cp = ((c & 0x1F) << 6) | (b[i+1] & 0x3F)
            out.append(chr(cp))
            i += 2
            continue
        # 3 字节 UTF-8
        if 0xE0 <= c <= 0xEF and i + 2 < n and 0x80 <= b[i+1] <= 0xBF and 0x80 <= b[i+2] <= 0xBF:
            cp = ((c & 0x0F) << 12) | ((b[i+1] & 0x3F) << 6) | (b[i+2] & 0x3F)
            # 代理区 (U+D800-DFFF): 非法单字符, 视为代理对的一部分
            if 0xD800 <= cp <= 0xDBFF and i + 3 < n and 0xED <= b[i+3] <= 0xED and 0x80 <= b[i+4] <= 0xBF and 0x80 <= b[i+5] <= 0xBF:
                # 完整代理对: 高代理 + 低代理
                lo_cp = ((b[i+3] & 0x0F) << 12) | ((b[i+4] & 0x3F) << 6) | (b[i+5] & 0x3F)
                if 0xDC00 <= lo_cp <= 0xDFFF:
                    # 组合成 Unicode 码点
                    combined = 0x10000 + ((cp - 0xD800) << 10) + (lo_cp - 0xDC00)
                    out.append(chr(combined))
                    i += 6
                    continue
            # 单独高/低代理 (无配对): 替换
            if 0xD800 <= cp <= 0xDFFF:
                out.append('\ufffd')
                i += 3
                continue
            # 普通 3 字节中文等: 正常追加
            out.append(chr(cp))
            i += 3
            continue
        # 4 字节 UTF-8 (正常 Emoji)
        if 0xF0 <= c <= 0xF4 and i + 3 < n and 0x80 <= b[i+1] <= 0xBF and 0x80 <= b[i+2] <= 0xBF and 0x80 <= b[i+3] <= 0xBF:
            cp = ((c & 0x07) << 18) | ((b[i+1] & 0x3F) << 12) | ((b[i+2] & 0x3F) << 6) | (b[i+3] & 0x3F)
            out.append(chr(cp))
            i += 4
            continue
        # 其他非法字节
        out.append('\ufffd')
        i += 1
    return ''.join(out)


class NetReader:
    def __init__(self, data: bytes):
        self.buf = io.BytesIO(data)

    def read(self, n: int) -> bytes:
        d = self.buf.read(n)
        if len(d) != n:
            raise EOFError(f"need {n}, got {len(d)}")
        return d

    def byte(self) -> int:
        return self.read(1)[0]

    def boolean(self) -> bool:
        return self.byte() != 0

    def int32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def int64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def float32(self) -> float:
        return struct.unpack(">f", self.read(4))[0]

    def utf(self) -> str:
        n = struct.unpack(">H", self.read(2))[0]
        b = self.read(n)
        # 兼容游戏端异常编码: UTF-16 代理对被当成 UTF-8 发出 (如 ED A0 BD ED B4 B4 = U+D83D U+DD34 = Emoji)
        # 正常 UTF-8 解码会得到 U+FFFD (�), 这里先把代理对还原为正确码点
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return _decode_lenient(b)

    def nullable_str(self):
        return self.utf() if self.boolean() else None

    def remaining(self) -> int:
        return self.buf.getbuffer().nbytes - self.buf.tell()


class NetWriter:
    def __init__(self):
        self.buf = bytearray()

    def byte(self, v: int):
        self.buf += struct.pack(">b", v)

    def boolean(self, v: bool):
        self.buf += b"\x01" if v else b"\x00"

    def int32(self, v: int):
        self.buf += struct.pack(">i", v)

    def int64(self, v: int):
        self.buf += struct.pack(">q", v)

    def float32(self, v: float):
        self.buf += struct.pack(">f", v)

    def utf(self, s: str):
        b = s.encode("utf-8")
        if len(b) > 65535:
            raise ValueError("string too long")
        self.buf += struct.pack(">H", len(b)) + b

    def nullable_str(self, s):
        if s is None:
            self.boolean(False)
        else:
            self.boolean(True)
            self.utf(s)

    def output(self) -> bytes:
        return bytes(self.buf)


def pack_frame(mtype: int, payload: bytes) -> bytes:
    return struct.pack(">ii", len(payload), mtype) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed by peer")
        data += chunk
    return data


def read_frame(sock: socket.socket, timeout: float = 15.0):
    sock.settimeout(timeout)
    length, mtype = struct.unpack(">ii", _recv_exact(sock, 8))
    if length < 0 or length > 50_000_000:
        raise ValueError(f"bad packet length {length} (type {mtype})")
    payload = _recv_exact(sock, length) if length > 0 else b""
    return mtype, payload

# ================================================================ 官方哈希/挑战算法

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest().upper()


def md5_lower(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def hash_f(s: str) -> str:
    return md5_lower(sha256_hex(s))


def f_c(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest().upper()[:14]


def hash_e_int(i: int) -> str:
    return "#%06X" % (i & 0xFFFFFF)


def java_int32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def java_double_str(v: float) -> str:
    if v != v:
        return "NaN"
    if v == float("inf"):
        return "Infinity"
    if v == float("-inf"):
        return "-Infinity"
    if v == 0:
        return "0.0"
    ax = abs(v)
    if ax >= 1e7 or ax < 1e-3:
        s = repr(v)
        if "e" in s or "E" in s:
            mant, exp = s.lower().split("e")
            return mant + "E" + str(int(exp))
        neg = "-" if s.startswith("-") else ""
        if neg:
            s = s[1:]
        if "." in s:
            ip, fp = s.split(".")
        else:
            ip, fp = s, ""
        ip2 = ip.lstrip("0")
        if not ip2:
            nz = 0
            for ch in fp:
                if ch == "0":
                    nz += 1
                else:
                    break
            sig = fp[nz:]
            exp = -(nz + 1)
        else:
            exp = len(ip2) - 1
            sig = (ip2 + fp).rstrip("0")
        mant = sig[0] + ("." + sig[1:] if len(sig) > 1 else "")
        return f"{neg}{mant}E{exp}"
    return repr(v)


def challenge_e(i: int) -> str:
    d = CREDITS_MAP
    return "".join([
        f"c:{i}", f"m:{java_int32((i * 87) + 24)}",
        f"0:{java_int32(d[0] * 11 * i)}", f"1:{java_int32((d[1] * 12) + i)}",
        f"2:{java_int32(d[2] * 13 * i)}", f"3:{java_int32((d[3] * 14) + i)}",
        f"4:{java_int32(d[4] * 15 * i)}", f"5:{java_int32((d[5] * 16) + i)}",
        f"6:{java_int32(d[6] * 17 * i)}", f"7:{java_int32(d[7] * 18 * i)}",
        f"8:{java_int32(d[8] * 19 * i)}", f"t1:{java_double_str(4000.0 * 11.0 * i)}",
        f"d:{java_int32(i * 5)}",
    ])


# ================================================================ 协议客户端

class RWProbe:
    def __init__(self, host, port=DEFAULT_PORT, name=DEFAULT_NAME,
                 unit_checksum=0, password=None, client_version=DEFAULT_VER,
                 verbose=False, magic=None):
        self.host = host
        self.port = port
        self.name = name
        self.unit_checksum = unit_checksum
        self.password = password
        self.client_version = client_version
        self.magic = magic or (MAGIC_INTL if client_version < 160 else PROTO_MAGIC)
        self.verbose = verbose
        self.sock = None
        self.network_client_id = str(uuid.uuid4())
        self.last_server_info = None
        self.relay_entries = []
        self._ack_sent = False
        self.t_connect = time.time()
        self.proto_ver = None
        self.server_e = None
        self.server_app_ver = None
        self.server_apk = None
        self.server_id = None
        self.conn_id = None
        self.challenge_y = None
        self.extra = None

    def log(self, *a):
        if self.verbose:
            print("[probe]", *a)

    def connect(self, proto_variant=1, room_id=None):
        self.t_connect = time.time()
        self.sock = socket.create_connection((self.host, self.port), timeout=8)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        w = NetWriter()
        w.utf(self.magic)
        if room_id:
            w.int32(4)
            w.int32(self.client_version)
            w.int32(1)
            w.nullable_str(room_id)
            w.utf(self.name)
            w.utf(self.network_client_id)
            w.utf("")
        elif proto_variant in (0, 2):
            w.int32(4)
            w.int32(self.client_version)
            w.int32(1 if proto_variant == 0 else 2)
            w.int32(random.randint(1, 1_000_000))
            w.utf(self.name)
            w.utf("en")
            w.utf("")
        else:
            w.int32(2)
            w.int32(self.client_version)
            w.int32(1 if proto_variant == 3 else 2)
            w.utf(self.name)
            w.utf("en")
        self.sock.sendall(pack_frame(160, w.output()))
        self._wait_161()

    def _wait_161(self):
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                mtype, payload = read_frame(self.sock, 5)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.verbose:
                print(f"[DBG] recv mtype={mtype} len={len(payload)}")
            if mtype == 161:
                self._parse_161(payload)
                return
            elif mtype == 163:
                r = NetReader(payload)
                relay_ver = r.int32()
                self.log(f"163 relay_ver={relay_ver}")
            elif mtype == 151:
                self._handle_relay_151(payload)
            elif mtype == 108:
                r = NetWriter(); r.int64(NetReader(payload).int64()); r.byte(1)
                self.sock.sendall(pack_frame(109, r.output()))
            elif mtype == 150:
                raise RuntimeError("kicked: " + NetReader(payload).utf())
            elif mtype == 111:
                raise RuntimeError("disconnected")
            else:
                self.log(f"got {mtype} ({len(payload)}B)")

    def _handle_relay_151(self, payload):
        _t151 = time.time()
        v7 = v8 = 0; s1 = s2 = ""; v10 = 0; bf_i = bf_j = 0
        try:
            r = NetReader(payload)
            v7 = r.int32(); v8 = r.int32()
            b1 = r.boolean()
            if b1:
                bf_i = r.int32()
            b2 = r.boolean()
            if b2:
                bf_j = r.int32()
            remaining = r.remaining()
            s1 = r.utf() if remaining >= 4 else ''
            remaining = r.remaining()
            s2 = r.utf() if remaining >= 4 else ''
            remaining = r.remaining()
            v10 = r.int32() if remaining >= 4 else 0

            if not QUIET:
                print(f"151 relay: v7={v7} v8={v8} b={b1},{b2} bf_i={bf_i} bf_j={bf_j} "
                      f"s1={s1!r} s2={s2!r} v10={v10}")
            if v8 == 5 and s1:
                self.relay_entries.append((s1, s2, v10))

            if v8 == 0:
                v0 = ''
            elif v8 == 1:
                v0 = str(bf_j)
            elif v8 == 2:
                v0 = challenge_e(bf_i)
            elif v8 in (3, 4):
                v0 = f_c(f"{bf_i}|{bf_j}")
            elif v8 in (5, 6):
                v0 = '-1'
                if s1 and s2:
                    t_pow = time.time()
                    POW_TIMEOUT = 30
                    for i in range(v10 + 1):
                        if f_c(s2 + str(i)) == s1:
                            v0 = str(i)
                            if not QUIET:
                                print(f"[151] POW solved: i={i} ({time.time()-t_pow:.1f}s)")
                            break
                        if time.time() - t_pow > POW_TIMEOUT:
                            if not QUIET:
                                print(f"[151] POW超时({POW_TIMEOUT}s), 发-1")
                            break
                        if i > 0 and i % 2_000_000 == 0 and not QUIET:
                            print(f"[151] POW进度: {i}/{v10} ({time.time()-t_pow:.0f}s)", flush=True)
                    else:
                        if not QUIET:
                            print(f"[151] POW未解出 (穷举{v10+1}次, {time.time()-t_pow:.1f}s)")
            else:
                v0 = 'max'

            ack = NetWriter()
            ack.int32(v7); ack.int32(v8); ack.utf(v0)
            ack.float32((time.time() - _t151) * 1000.0)
            self.sock.sendall(pack_frame(152, ack.output()))
            self._ack_sent = True
            self.log(f"152 sent (v8={v8}, v0={v0!r})")
        except Exception as e:
            self.log(f"151 parse err: {e}")

    def _parse_161(self, payload: bytes):
        r = NetReader(payload)
        f = {}
        try:
            f["magic"] = r.utf()
            f["proto_ver"] = r.int32()
            f["e"] = r.int32()
            f["app_ver"] = r.int32()
            f["apk"] = r.utf()
            f["server_id"] = r.utf()
            f["conn_id"] = r.int32()
            f["challenge_y"] = r.int32()
            if r.remaining() >= 4:
                f["extra"] = r.int32()
        except (EOFError, struct.error):
            pass
        self.proto_ver = f.get("proto_ver")
        self.server_e = f.get("e")
        self.server_app_ver = f.get("app_ver")
        self.server_apk = f.get("apk")
        self.server_id = f.get("server_id")
        self.conn_id = f.get("conn_id")
        self.challenge_y = f.get("challenge_y")
        self.extra = f.get("extra")

    def register(self, v_challenge, w_challenge, unit_checksum=None, wait=10):
        ver = self.server_e or self.client_version
        reg_ver = 5
        candidates = [unit_checksum] if unit_checksum is not None else [0, -1, 1]
        last = "NO_RESPONSE"
        for ck in candidates:
            if self.sock is None:
                return "CONNECTION_CLOSED"
            w = NetWriter()
            w.utf(self.magic)
            w.int32(reg_ver)
            w.int32(ver)
            w.int32(ver)
            w.utf(self.name)
            w.nullable_str(hash_f(self.password) if self.password else None)
            w.utf(self.magic)
            w.utf(sha256_hex(self.network_client_id + (self.server_id or "")))
            w.int32(ck)
            w.utf(challenge_e(v_challenge))
            if reg_ver >= 5:
                w.utf(hash_e_int(w_challenge))
            try:
                self.sock.sendall(pack_frame(110, w.output()))
            except OSError:
                return "CONNECTION_CLOSED"
            self.log(f"REGISTER(ver{reg_ver},e={ver},unit={ck})")
            result = self._read_after_register(wait)
            last = result
            if "units" not in result and "Integrity" not in result and "PASSWORD" not in result:
                return result
            if "units" in result and len(candidates) > 1:
                self.close()
                try:
                    self.connect(proto_variant=1)
                except Exception:
                    return result
        return last

    def _handle_178(self, payload):
        r = NetReader(payload)
        b0 = r.byte()
        i1 = r.int32()
        b2 = r.boolean()
        i3 = r.int32()
        addr = r.utf() if r.remaining() >= 2 else ""
        if not addr and (b0 != 0 or i1 != 3 or b2 or i3 != 1):
            r2 = NetReader(payload)
            addr = r2.utf()
            self.log(f"178 legacy parse: addr={addr!r}")
        self.log(f"178 jump addr={addr!r}")
        return addr

    def _read_after_register(self, wait=5):
        deadline = time.time() + wait
        msgs = []
        self.last_msgs = []
        while time.time() < deadline:
            try:
                mtype, payload = read_frame(self.sock, 2)
            except socket.timeout:
                break
            except OSError:
                break
            if self.verbose:
                print(f"[DBG] recv mtype={mtype} len={len(payload)} raw={payload.hex()}")
            if mtype == 106:
                info = parse_server_info(payload)
                self.last_server_info = info
                msgs.append(("SERVER_INFO", info))
                self.last_msgs.append(f"106(SERVER_INFO {len(payload)}B)")
            elif mtype == 115:
                msgs.append(("TEAMS", parse_teams(payload)))
                self.last_msgs.append(f"115(TEAMS {len(payload)}B)")
            elif mtype == 150:
                return "KICKED: " + NetReader(payload).utf()
            elif mtype == 113:
                return "PASSWORD_REQUIRED"
            elif mtype == 111:
                return "DISCONNECTED: " + NetReader(payload).utf()
            elif mtype == 141:
                sender, message = parse_141(payload)
                if not QUIET:
                    if sender:
                        print(f"[聊天] {sender}: {message}")
                    else:
                        print(f"[聊天] {message}")
                msgs.append(("CHAT", sender, message))
                self.last_msgs.append(f"141(CHAT {sender}: {message[:30]}...)")
            elif mtype == 151:
                _t151 = time.time()
                v7 = v8 = 0; s1 = s2 = ""; v10 = 0; bf_i = bf_j = 0
                try:
                    r = NetReader(payload)
                    v7 = r.int32(); v8 = r.int32()
                    b1 = r.boolean()
                    if b1:
                        bf_i = r.int32()
                    b2 = r.boolean()
                    if b2:
                        bf_j = r.int32()
                    s1 = r.utf() if r.remaining() > 2 else ""
                    s2 = r.utf() if r.remaining() > 2 else ""
                    v10 = r.int32() if r.remaining() >= 4 else 0
                    if v8 == 5 and s1:
                        self.relay_entries.append((s1, s2, v10))
                    if v8 == 0:
                        v0 = ""
                    elif v8 == 1:
                        v0 = str(bf_j)
                    elif v8 == 2:
                        v0 = challenge_e(bf_i)
                    elif v8 in (3, 4):
                        v0 = f_c(f"{bf_i}|{bf_j}")
                    elif v8 in (5, 6):
                        v0 = "-1"
                        if s1 and s2:
                            for i in range(v10 + 1):
                                if f_c(s2 + str(i)) == s1:
                                    v0 = str(i)
                                    break
                    else:
                        v0 = "max"
                    if not QUIET:
                        self.log(f"151 relay: v7={v7} v8={v8} b={b1},{b2} bf_i={bf_i} bf_j={bf_j} "
                                 f"s1={s1} s2={s2} v10={v10} -> 152v0={v0}")
                    ack = NetWriter()
                    ack.int32(v7); ack.int32(v8); ack.utf(v0)
                    ack.float32((time.time() - _t151) * 1000.0)
                    self.sock.sendall(pack_frame(152, ack.output()))
                    self._ack_sent = True
                    self.log("152 ack sent")
                except Exception as e:
                    self.log(f"151 parse err: {e}")
                msgs.append(("RELAY151", len(payload)))
                self.last_msgs.append(f"151(v8={v8},{s1}/{s2}/{v10})")
            elif mtype == 108:
                r = NetWriter(); r.int64(NetReader(payload).int64()); r.byte(1)
                self.sock.sendall(pack_frame(109, r.output()))
            else:
                msgs.append((f"MSG{mtype}", len(payload)))
                self.last_msgs.append(f"{mtype}({len(payload)}B):{payload.hex()}")
        return f"OK({len(msgs)} msgs)" if msgs else "NO_RESPONSE"

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

# ================================================================ 106/115/141 解析


MASTERSERVERS = [
    "http://gs1.corrodinggames.com/masterserver/1.4",
    "http://gs4.corrodinggames.net/masterserver/1.4",
]
ALLOCATOR = ("210.16.163.55", 5123)
REG_VER = 5
CK = 678359601
CLIENT_VER = 176
# ================================================================ 参数解析

HELP_TEXT = """\
铁锈战争 1.15 房间信息查询工具

用法:
  python3 room_info.py <房间ID>           查询中继房间 (如 R5132)
  python3 room_info.py <IP>:<端口>        直连服务器 (如 1.2.3.4:5123)
  python3 room_info.py --list             官方房间列表
  python3 room_info.py --help             显示本帮助

选项:
  --show-players        显示玩家列表 (默认显示)
  --max-players <N>     玩家列表最多显示 N 人 (默认 10)
  --name <名字>         设置探针名字 (默认 ABAB探针)
  --debug               调试输出
"""


def _find_arg(name, default=None):
    if name in sys.argv:
        try:
            return sys.argv[sys.argv.index(name) + 1]
        except IndexError:
            return default
    return default


_NAME_ARG = None
_DEBUG_ARG = False

# 收集非选项参数 (第一个非 -- 开头的即房间ID/目标)
_positional = [a for a in sys.argv[1:] if not a.startswith("--")]
_arg1 = _positional[0] if _positional else None

# 帮助: 仅命令行直接运行时生效 (import 不触发)
if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv or len(sys.argv) <= 1 or _arg1 is None:
        print(HELP_TEXT)
        sys.exit(0)

# import 场景 (如插件调用): _arg1 为 None, 置空串避免后续崩溃
if _arg1 is None:
    _arg1 = ""

_NAME_ARG = _find_arg("--name")
if "--debug" in sys.argv:
    _DEBUG_ARG = True
    QUIET = False
    DEBUG_MODE = True
    print("[DEBUG] 调试模式已启用")

_MAX_PLAYERS_DISPLAY = 10
if "--max-players" in sys.argv:
    try:
        _MAX_PLAYERS_DISPLAY = int(_find_arg("--max-players"))
    except (IndexError, ValueError, TypeError):
        pass

# 玩家列表默认显示
SHOW_PLAYERS = "--show-players" in sys.argv

LIST_MODE = _arg1 == "--list"
_DIRECT_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+(:\d+)?$")
DIRECT_TARGET = _arg1 if _DIRECT_RE.match(_arg1) else None
RID = _arg1 if DIRECT_TARGET is None else _arg1
NAME = _NAME_ARG or "ABAB探针"
NID = str(uuid.uuid4())

# ================================================================ 房间列表

def connect_and_161(host, port, rid=None):
    s = socket.create_connection((host, port), timeout=8)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    w = NetWriter()
    w.utf(PROTO_MAGIC)
    if rid:
        w.int32(4); w.int32(CLIENT_VER); w.int32(1)
        w.nullable_str(rid)
        w.utf(NAME); w.utf(NID); w.utf('')
    else:
        w.int32(2); w.int32(CLIENT_VER); w.int32(2)
        w.utf(NAME); w.utf('en')
    s.sendall(pack_frame(160, w.output()))
    probe = RWProbe('x', 0, NAME, unit_checksum=CK or 0, client_version=CLIENT_VER, verbose=False)
    probe.sock = s
    probe._wait_161()
    return s, probe


def send_110(sock, sid, ck, conn, chal=None):
    w = NetWriter()
    w.utf(PROTO_MAGIC)
    w.int32(REG_VER); w.int32(CLIENT_VER); w.int32(CLIENT_VER)
    w.utf(NAME); w.nullable_str(None); w.utf(PROTO_MAGIC)
    w.utf(sha256_hex(NID + (sid or '')))
    w.int32(ck)
    w.utf(challenge_e(conn or 0))
    w.utf(hash_e_int(chal or 0))
    sock.sendall(pack_frame(110, w.output()))


def send_leave(sock):
    try:
        w = NetWriter()
        w.utf("leaving")
        sock.sendall(pack_frame(111, w.output()))
        time.sleep(0.2)
    except OSError:
        pass


def masterserver_request(params: dict) -> list:
    for base in MASTERSERVERS:
        url = base + "/interface?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": "rw android 176 en", "Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", "replace").splitlines()
        except Exception as e:
            if not QUIET:
                print(f"[master] {base} 失败: {e}")
    return []


def _clean(s):
    return "".join(c for c in (s or "") if c >= " " and c != "\u007f").strip()


def room_list():
    if not QUIET:
        print("[list] 获取房间列表(版本 1.15)...")
    lines = masterserver_request({"action": "list", "game_version": str(CLIENT_VER),
                                  "game_version_beta": "false"})
    if not lines:
        if not QUIET:
            print("[list] 无响应(主服务器离线/被墙)")
        return []
    if not QUIET:
        print("头部:", lines[0])
        print("-" * 104)
        print(f"{'房间名':<20}{'版本':<6}{'IP':<18}{'端口':<6}{'模式':<10}{'状态':<8}{'人数':<6}{'描述'}")
        print("-" * 104)
    rooms = []
    for line in lines[1:]:
        if not line.strip():
            continue
        f = line.split(",")
        def g(i, d=""):
            return _clean(f[i]) if len(f) > i and f[i] else d
        room = {"id": g(0), "name": g(1, "?"), "ver": g(2), "ip": g(3),
                "port": g(5), "mode": g(10), "status": g(11),
                "desc": g(9, g(14, "")), "players": g(15, "0"), "max": g(16, "?")}
        rooms.append(room)
        if not QUIET:
            mode = {"skirmishMap": "遭遇战", "customMap": "自定义", "savedGame": "存档",
                    "LINK": "链接"}.get(room["mode"], room["mode"])
            status = {"ingame": "游戏中", "battleroom": "房间中", "locked": "已锁定",
                      "chat": "聊天"}.get(room["status"], room["status"])
            print(f"{room['name'][:18]:<20}{room['ver']:<6}{room['ip']:<18}{room['port']:<6}"
                  f"{mode:<10}{status:<8}{room['players']}/{room['max']:<4}{room['desc'][:40]}")
    if not QUIET:
        print("-" * 104)
        print(f"共 {len(rooms)} 个房间。直连: python3 room_info.py <IP>:<端口>")
    return rooms


def parse_106(data):
    """解析 106 SERVER_INFO 房间信息消息"""
    r = NetReader(data)
    def u16():
        return struct.unpack('>H', r.read(2))[0]
    def i32():
        return struct.unpack('>i', r.read(4))[0]
    def utf():
        return r.utf()
    def boolean():
        return r.boolean()
    def f32():
        return r.float32()
    def byte():
        return r.byte()
    def int64():
        return r.int64()
    def nullable_str():
        return r.nullable_str()
    info = {}
    # 106 消息结构依据客户端发送端 (ae.java b(c): 525-551) 与
    # as.java 字段语义 (bp.java: startingCredits=c, fogMode=d, revealedMap=e,
    #   aiDifficulty=f, startingUnits=g, incomeMultiplier=h, noNukes=i,
    #   sharedControl=l, lockedTeams=m, allowSpectators=o, roomlock=p, seed=q):
    #   utf magic + i32 app_ver + i32 mode + utf mapname
    #   + i32 credits + i32 fog + bool revealed + i32 ai_diff
    #   + byte subVer + bool H + bool I + i32 ay + i32 az
    #   + i32 units + f32 multiplier + bool noNukes + bool j
    #   + bool has_custom
    #   + [customUnits 块: utf名字 + i32块长 + 块内容]
    #   + bool shared + bool teamlock + bool n + bool spectators
    #   + bool roomlock + i32 seed
    try:
        info['magic'] = utf()
        info['app_ver'] = i32()
        info['mode'] = i32()
        info['mapname'] = utf()
        info['credits'] = i32()
        info['fog'] = i32()
        info['revealed'] = boolean()
        info['ai_diff'] = i32()
        info['sub_ver'] = byte()
        info['proxy_h'] = boolean()   # ae.H
        info['proxy_i'] = boolean()   # ae.I
        info['unit_cap'] = i32()      # ae.ay
        info['unit_cap_max'] = i32()  # ae.az
        info['units'] = i32()         # startingUnits (as.g)
        info['multiplier'] = f32()    # incomeMultiplier (as.h)
        info['no_nukes'] = boolean()  # as.i
        info['flag_j'] = boolean()    # as.j
        info['has_custom'] = boolean()

        # 初始化 Mod 相关字段
        info['mod_names'] = []
        info['custom_unit_count'] = 0
        info['custom_units'] = []
        info['custom_unit_list'] = []   # 自定义单位显示名列表, 索引0对应ID=100
        if info.get('has_custom', False):
            # customUnits 块: readUTF("customUnits") + int32块长 + 块内容
            # 块内容结构 (l.a(j) 接收端, custom/l.java 980-1010):
            #   readInt() 版本: <2 → z=false; >=2 → bool+bool+z
            #   readInt() cnt 单位数量
            #   循环 cnt: readUTF(单位ID) + readInt + readBoolean
            #             + readUTF(显示名) + readLong + readLong
            #             + (z时) readUTF(额外名)
            block_name = utf()
            block_len = i32()
            if block_name != 'customUnits':
                raise ValueError(f"bad mod block: {block_name!r}")
            block_start = r.buf.tell()
            try:
                ver = i32()
                if ver < 2:
                    z = False
                else:
                    z = boolean()
                    boolean()
                cnt = i32()
                info['custom_unit_count'] = cnt
                for _ in range(min(cnt, 5000)):
                    unit_id = utf()
                    i32()          # l.H
                    boolean()
                    dname = nullable_str()   # 显示名 l.t()
                    int64()        # checksum J.i
                    int64()
                    if z:
                        nullable_str()
                    # 客户端 ae.c(i): 100+ 显示 lVarC.e() (本地化名, 回退单位ID)
                    info['custom_unit_list'].append(unit_id if unit_id else dname)
            except (EOFError, struct.error):
                # 单位解析中途失败: 放弃块内容, 但确保跳到块尾, 后续字段不错位
                if DEBUG_MODE:
                    print(f"[DEBUG] customUnits 块解析中断: cur={r.buf.tell()} block_start={block_start} block_len={block_len}")
            # 跳过整个块内容 (单位定义列表)
            end = block_start + block_len
            if end > len(data):
                end = len(data)
            r.buf.seek(end)

        # customUnits 块之后: 房间设置布尔 + seed (as.l/m/n/o/p/q)
        info['shared'] = boolean()      # as.l 共享控制
        info['teamlock'] = boolean()    # as.m 锁定队伍
        info['flag_n'] = boolean()      # as.n
        info['spectators'] = boolean()  # as.o 允许旁观
        info['roomlock'] = boolean()    # as.p 房间锁定
        info['seed'] = i32()            # as.q

    except EOFError:
        # 确保关键字段存在
        for k in ['shared','teamlock','spectators','roomlock','seed']:
            info.setdefault(k, None)

    if DEBUG_MODE:
        print(f"[DEBUG] 106解析: app_ver={info.get('app_ver')} mode={info.get('mode')} mapname={info.get('mapname')!r}")
        print(f"[DEBUG] 106解析: credits={info.get('credits')} fog={info.get('fog')} revealed={info.get('revealed')} ai={info.get('ai_diff')} subVer={info.get('sub_ver')}")
        print(f"[DEBUG] 106解析: units={info.get('units')} multiplier={info.get('multiplier')} noNukes={info.get('no_nukes')} teamLock={info.get('teamlock')}")
        print(f"[DEBUG] 106解析: shared={info.get('shared')}, shared2={info.get('shared2')}, "
              f"spectators={info.get('spectators')}, teamlock={info.get('teamlock')}, "
              f"force2team={info.get('force2team')}, roomlock={info.get('roomlock')}, seed={info.get('seed')}")
        print(f"[DEBUG] 自定义单位数量: {info.get('custom_unit_count', 0)}")
        print(f"[DEBUG] 自定义单位列表: {info.get('custom_unit_list', [])[:10]}{'...' if len(info.get('custom_unit_list', [])) > 10 else ''}")
        print(f"[DEBUG] Mod名称列表: {info.get('mod_names', [])}")

    return info


def parse_141(payload):
    try:
        r = NetReader(payload)
        message = r.utf()
        flag = r.byte()
        sender = r.nullable_str()
        return sender, message
    except Exception:
        return None, f"(无法解析) {payload.hex()[:40]}..."


FOG_NAMES = {0: '无雾', 1: '轻雾', 2: '重雾'}
CREDITS_NAMES = {0: '默认', 1: '$0', 2: '$1000', 3: '$2000', 4: '$5000', 5: '$10000', 6: '$50000', 7: '$100000', 8: '$200000'}
UNIT_NAMES = {1: '常规模式（1个建造者）', 2: '小军队', 3: '3工程师', 4: '3工程师（无指挥中心）', 9: '自定义'}
# RWPP 可选起始单位 (isPickableStartingUnit: true, 客户端 ae.d() 下拉 5-8 项)
# units=100 起按此列表索引: 100=experimentalDropship(飞行堡垒), 101=experimentalGunship(实验悬浮型气垫船)...
PICKABLE_STARTING_UNITS = ['experimentalDropship', 'experimentalGunship', 'experimentalSpider', 'modularSpider']
MULTIPLIER_NAMES = {0: '1x', 1: '1.5x', 2: '2x', 3: '2.5x'}

UNIT_TRANSLATIONS = {
    'AntiNukeLaucher': '反核防御',
    'NukeLaucher': '核弹发射井',
    'aaBeamGunship': 'AA激光射束战机',
    'airFactory': '空军基地',
    'airShip': '拦截机',
    'amphibiousJet': '两栖喷气机',
    'antiAirTurretFlak': 'T2 - 高射炮',
    'antiAirTurretT2': 'T2 - 防空炮塔',
    'antiAirTurretT3': 'T3 - 防空炮塔',
    'artillery': '自行火炮',
    'attackSubmarine': '潜水艇',
    'battleShip': '战列舰',
    'bomber': '轰炸机',
    'builder': '建造者',
    'builderShip': '海上建造者',
    'combatEngineer': '战斗工程师',
    'creditsCrates': '资金箱子',
    'crystalResource': '水晶',
    'dropship': '运输机',
    'experiementalCarrier': '航空母舰',
    'experimentalDropship': '飞行堡垒',
    'experimentalGunship': '实验悬浮型气垫船',
    'experimentalGunshipLanded': '实验悬浮型气垫船',
    'experimentalHoverTank': '概念型悬浮坦克',
    'experimentalLandFactory': '实验工厂',
    'experimentalSpider': '实验型战斗蜘蛛',
    'experimentalTank': '实验坦克',
    'extractorT2': '资源抽取器 T2',
    'extractorT3': '资源抽取器 T3',
    'fabricator': '资源制造仪',
    'fireBee': '火蜂战机',
    'fogRevealer': 'system_fogRevealer',
    'gunBoat': '机枪艇',
    'gunShip': '武装直升机',
    'heavyAAShip': '重型防空舰',
    'heavyArtillery': '重型火炮',
    'heavyBattleship': '重型战舰',
    'heavyInterceptor': '重型拦截机',
    'heavyMissileShip': '重型导弹舰',
    'heavySub': '重型潜艇',
    'helicopter': '直升机',
    'hoverTank': '悬浮坦克',
    'hovercraft': '登陆艇',
    'ladybug': '瓢虫',
    'landFactory': '陆军工厂',
    'laserTank': '激光坦克',
    'lightGunship': '轻型武装直升机',
    'lightSub': '水下探测器',
    'mechArtillery': '火炮机甲',
    'mechBunker': '移动炮塔',
    'mechBunkerDeployed': '移动炮塔',
    'mechEngineer': '机械师',
    'mechFactory': '机械工厂',
    'mechFactoryT2': 'T2 - 机械工厂',
    'mechFlame': '喷火机甲',
    'mechGun': '基础机甲',
    'mechHeavyMissile': '重型防空机械装甲',
    'mechLaser': '等离子机甲',
    'mechLightning': '特斯拉机甲',
    'mechMinigun': '机枪机甲',
    'mechMissile': '防空机甲',
    'megaTank': '超级坦克',
    'missileAirship': '导弹飞艇',
    'missileShip': '导弹舰',
    'missileTank': '防空导弹坦克',
    'modularSpider': '模块化蜘蛛',
    'modularSpider_antiair': '萨姆防空炮T1',
    'modularSpider_antiairFlak': '高射炮T2',
    'modularSpider_antiairT2': '萨姆防空炮T2',
    'modularSpider_antinuke': '反核装置',
    'modularSpider_artillery': '火炮装置T1',
    'modularSpider_blink': '瞬移模块',
    'modularSpider_emptySlot': '闲置中...',
    'modularSpider_fabricator': '资源制造装置T1',
    'modularSpider_fabricatorT2': '资源制造装置T2',
    'modularSpider_gunturret': '机枪T1',
    'modularSpider_gunturretT2': '机枪T2',
    'modularSpider_laserdefense': '激光防御装置',
    'modularSpider_lightning': '闪电炮塔T1',
    'modularSpider_shieldGen': '护盾核心',
    'modularSpider_smallgunturret': '等离子装置T1',
    'modularSpider_smallgunturretT2': '等离子装置T2',
    'modularSpider_speed': '速度模块',
    'nautilusSubmarine': '鹦鹉螺号',
    'outpostT1': '瞭望塔',
    'outpostT2': '瞭望塔 T2',
    'plasmaTank': '等离子坦克',
    'repairbay': '修复湾',
    'scout': '侦察者',
    'seaFactory': '海军基地',
    'spreadingFire': '火',
    'spyDrone': '间谍无人机',
    'supplyDepot': '供应站',
    'tank': '坦克',
    'tankDestroyer': '坦克杀手',
    'tree': '树',
    'turretT2': '机枪 T2',
    'turretT3': '重机枪塔 T3',
    'turret_artillery': '火炮',
    'turret_artilleryT2': '火炮 T2',
    'turret_flamethrower': '火焰喷射器',
    'turret_lightning': '闪电炮塔',
    'turret_lightningT2': '闪电炮塔 T2',
    'wall_v': '墙 (V)',
}

AI_NAMES = {-2: '非常容易', -1: '容易', 0: '正常', 1: '困难', 2: '疯狂', 3: '噩梦'}

OFFICIAL_VERSIONS = {
    176: '1.15',
    174: '1.15p11',
    130: '1.13',
    100: '1.12',
}


def detect_client(info):
    ver = info.get('app_ver')
    if ver in OFFICIAL_VERSIONS:
        return OFFICIAL_VERSIONS[ver]
    if ver:
        return f'{ver / 100:.2f}' if ver < 1000 else str(ver)
    return '?'


def format_room(info, rid, players, capacity=None, players_detail=None, max_players_display=10, player_count=None, show_players=False):
    import re as _re

    lines = []
    cver = detect_client(info)
    lines.append(f"版本：{cver}")

    mapname = info['mapname']
    if mapname.endswith('.tmx'):
        mapname = mapname[:-4]
    mapname = _re.sub(r'^\[[^\]]*\]', '', mapname).strip()
    lines.append(f"地图名称：{mapname}")

    c = info['credits']
    if c == 0:
        money_str = "4000¤"
    else:
        money_str = CREDITS_NAMES.get(c, f"${c}")
    mul = MULTIPLIER_NAMES.get(int((info['multiplier'] - 1.0) * 2.0), f"{info['multiplier']}x")
    lines.append(f"初始资金：{money_str}[{mul}倍率]")

    lines.append(f"初始禁核：{'禁用核蛋蛋' if info['no_nukes'] else '未禁用核蛋蛋'}")
    lines.append("初始迷雾：" + FOG_NAMES.get(info['fog'], '未知(' + str(info['fog']) + ')'))
    mode_str = {0: '遭遇战', 1: '自定义', 2: '存档'}.get(info.get('mode'), '未知(' + str(info.get('mode')) + ')')
    lines.append(f"地图模式：{mode_str}")
    # 默认单位: 客户端下拉 ae.d() = 1..4 + 可选起始单位(100+, RWPP isPickableStartingUnit)
    # 100+ 是 g 列表索引 (客户端 ae.d 100+ 部分), 不是服务器 custom_unit_list 顺序!
    # g: 100=experimentalDropship(飞行堡垒), 101=experimentalGunship(实验悬浮型气垫船), 102=experimentalSpider(实验型战斗蜘蛛), 103=modularSpider(模块蜘蛛)
    u = info['units']
    dbg = []
    if u in UNIT_NAMES:
        unit_str = UNIT_NAMES[u]
    elif u >= 100:
        lst = info.get('custom_unit_list', [])
        idx = u - 100
        dbg.append(f'unit_raw={u} idx={idx} list_len={len(lst)}')
        if idx < len(PICKABLE_STARTING_UNITS):
            # 客户端本地 g 列表: 直接映射到单位ID
            unit_id = PICKABLE_STARTING_UNITS[idx]
            unit_str = UNIT_TRANSLATIONS.get(unit_id, unit_id)
            dbg.append(f'g_list[{idx}]={unit_id} -> {unit_str}')
        elif 0 <= idx < len(lst):
            # 超出 g 列表: 回退服务器 custom_unit_list (mod 自定义单位)
            unit_str = lst[idx]
            dbg.append(f'custom_list[{idx}]={unit_str}')
        else:
            unit_str = f"自定义单位({u})"
    else:
        unit_str = f"未知({u})"
    # 自定义单位ID: 剥 c_ 前缀后查中文翻译表 (客户端 e(): units.<id>.name)
    # aaBeamGunship -> AA激光射束战机, c_amphibiousJet -> 两栖喷气机
    unit_str = UNIT_TRANSLATIONS.get(unit_str.lstrip('c_'), unit_str)
    if DEBUG_MODE and dbg:
        dbg.append(f'final={unit_str}')
        print('[DEBUG] 默认单位: ' + ' | '.join(dbg))

    lines.append(f"默认单位：{unit_str}")
    cap = info.get('unit_cap')
    cap_max = info.get('unit_cap_max')
    if cap is not None and cap_max is not None:
        lines.append(f"单位上限：{cap}")

    # 共享控制/允许旁观: 106 消息 customUnits 块之后 (as.l / as.o)
    lines.append("共享控制：" + ("是" if info.get('shared', False) else "否"))
    spec = info.get('spectators')
    if spec is None:
        lines.append("允许旁观：未知")
    else:
        lines.append("允许旁观：" + ("是" if spec else "否"))
    lines.append("队伍锁定：" + ("是" if info.get("teamlock") else "否"))

    maxp = capacity if capacity else 0
    if not maxp:
        m = _re.search(r'\((\d+)p\)', info['mapname'])
        if m:
            maxp = int(m.group(1))
        elif '10p' in info['mapname']:
            maxp = 10
    count = player_count if player_count is not None else len(players)
    lines.append(f"当前人数：{count}/{maxp if maxp else '?'}")

    # 显示 Mod 信息
    mod_names = info.get('mod_names', [])
    unit_count = info.get('custom_unit_count', len(info.get('custom_units', [])))
    # mod_names 是自定义 mod 包名; 为空 = 原版 (custom_unit_count 只是内置单位数)
    if mod_names:
        mod_str = ', '.join(f"[{m}]" for m in mod_names)
        lines.append(f"使用Mod：{mod_str}（{unit_count}个单位）")
    else:
        lines.append(f"使用Mod：原版（{unit_count} 内置单位）")

    lines.append(f"ID：{rid}")

    if show_players:
        if players_detail:
            lines.append("")
            lines.append(" 玩家列表:")
            total = len(players_detail)
            show = players_detail[:max_players_display]
            for p in show:
                if not p.get('exists', True):
                    continue
                # 队伍字母 (客户端 p.a(I): 0=A, 1=B, 2=C ... 9=J)
                team = p.get('team', 0)
                team_letter = chr(ord('A') + team) if 0 <= team <= 25 else str(team)
                num = p.get('num', 1)
                label = f"{team_letter}{num}"
                name = p.get('name', '')
                if not name or all(not ch.isprintable() or ch.isspace() for ch in name):
                    name = "(未命名)"
                if p.get('is_ai'):
                    # 协议里 AI 名形如 "5号 - Hard"/"- Hard" (无 AI 字样), 显示时补前缀
                    if "AI" not in name:
                        name = f"AI {name}"
                    name += " (AI)"
                elif p.get('is_id_only'):
                    name += " (ID-only)"
                ping = p.get('ping', None)
                if ping is None:
                    ping_str = ""
                elif ping == -2:
                    ping_str = " (断线)"
                elif ping == -1:
                    ping_str = " (超时)"
                else:
                    ping_str = f" (ping {ping}ms)"
                lines.append(f"  {label}: {name}{ping_str}")
            if total > max_players_display:
                lines.append(f"  ... 还有 {total - max_players_display} 人")
        elif players:
            lines.append("")
            lines.append(f" 玩家: {', '.join(players)}")

    return "\n".join(lines)


# ================================================================ 115 消息解析
# 依据 1.15 官方 APK smali 逆向 (ae.smali sswitch_1ac + p.smali p.a(j,z)):
#   payload = [int32 本机玩家ID][int32 队伍数][teams块]
#   teams块 = readUTF("teams") + int32块长 + 子流(压缩标志=队伍数!=0)
#   每条队伍记录 = boolean(存在) + [空槽:A() | 非空: int32分数 + p.a()玩家结构]
#   玩家结构(non-AI) = byte槽位 + int32信用 + int32spawnId + nullable_str名字
#                    + boolean(AI) + 版本扩展字段...(>=149 含 nullable_str player_id)
#   尾部 = as.d int32 + as.c int32 + as.e boolean + as.f int32
#        + byte子版本 + ae.ay int32 + ae.az int32
#        + 子版本>=2: as.g int32 + as.h float + as.i bool + as.j bool
#        + 子版本>=3: if(bool) custom_units
#        + 子版本>=4: as.l bool
#        + 子版本>=5: ae.an bool


def parse_player_record(r, stream_ver, game_running=False):
    """解析一条玩家记录 (p.a(j, z) 逆向, z=game_running=aY).

    z=false(房间阶段): byte slot + int32 credits + int32 spawn_id
                      + nullable_str name + boolean is_ai
    z=true (游戏中):   byte+int32+int32 丢弃, nullable_str name 丢弃,
                      boolean 丢弃 (只读公共扩展字段)
    之后公共扩展字段(1.15=176 全满足):
      int32 Z, int64 aa, bool x, int32 y, int32 af, byte,
      bool L, bool M, bool H, bool F, int32 G,
      nullable_str player_id(S), int32 T, int32 A-E
    """
    p = {'slot': None, 'credits': None, 'spawn_id': None, 'name': None,
         'is_ai': None, 'player_id': None, 'ping': None, 'exists': True}
    try:
        if game_running:
            r.byte()          # slot (丢弃)
            r.int32()         # credits (丢弃)
            r.int32()         # spawn_id (丢弃)
            r.nullable_str()  # name (客户端丢弃, 我们也不取)
            r.boolean()       # is_ai (丢弃)
        else:
            p['slot'] = r.byte()
            p['credits'] = r.int32()
            r.int32()         # 跳过
            r.int32()         # 跳过
            p['spawn_id'] = r.int32()
            p['name'] = r.nullable_str()
            p['is_ai'] = r.boolean()

        # 公共扩展字段 (smali goto_32 起; c=0xf423f 恒满足, d=版本)
        if stream_ver >= 14:
            r.int32()         # Z
            r.int64()         # aa
        if stream_ver >= 34:
            r.boolean()       # x
            r.int32()         # y
        if stream_ver >= 50:
            r.int32()         # af
            r.byte()          # 跳过
        if stream_ver >= 52:
            r.boolean()       # L
            r.boolean()       # M
        if stream_ver >= 70:
            r.boolean()       # H
            r.boolean()       # F
            r.int32()         # G
        if stream_ver >= 90:
            p['player_id'] = r.nullable_str()  # S (player_id)
            r.int32()         # T
        if stream_ver >= 93:
            r.int32()         # A
            r.int32()         # B
            r.int32()         # C
            r.int32()         # D
            r.int32()         # E
    except (EOFError, struct.error):
        pass
    return p


# ================================================================ 115 消息解析
# 依据 1.15 官方 APK smali 逆向 (ae.smali sswitch_1ac + b(c)发送端):
#   外层结构 (已验证, 与 R5132 真实样本一致):
#     int32 own_player_id + boolean full_load(>=141) + int32 team_count
#     + readUTF("teams") + int32 block_len + gzip块
#
#   块内记录 (RCN服务器格式, 经真实样本验证):
#     记录之间以固定分隔符 ff ff d8 f1 切分
#     每条记录内含 nullable_str 名字: 01 + uint16长度 + UTF-8
#       - 真人名: 可打印 UTF-8, 长度<=32
#       - AI名: "AI - xxx" 难度名
#       - 未命名玩家: 64字符大写hex = player_id fallback
#
#   解析方法: 结构化扫描 (非正则), 名字长度由协议字段精确指定,
#             扫描可 100% 复现, 不依赖固定偏移 (服务器记录变长)


def _scan_nullable_names(block: bytes):
    """扫描块内所有 nullable_str 名字.
    结构化扫描: 01 + uint16长度 + UTF-8, 解码成功即候选.
    返回 [(offset, name)] 按出现顺序.
    """
    names = []
    i = 0
    n = len(block)
    while i < n - 3:
        if block[i] == 0x01:          # nullable boolean=true
            ln = struct.unpack('>H', block[i+1:i+3])[0]
            if 0 < ln <= 64 and i + 3 + ln <= n:
                try:
                    s = block[i+3:i+3+ln].decode('utf-8')
                    if s and all(c >= ' ' for c in s):
                        names.append((i, s))
                        i += 3 + ln
                        continue
                except UnicodeDecodeError:
                    pass
        i += 1
    return names


def _classify_name(name: str):
    """确定性分类玩家名. 返回 (is_real_name, is_ai, is_id_only)."""
    # 64字符全大写hex = player_id fallback
    if len(name) == 64 and all(c in '0123456789ABCDEF' for c in name):
        return False, False, True
    # AI 难度名: "AI - xxx" 或 "- xxx" (协议里 AI 名常为 "- Hard"/"5号 - Hard")
    nm = name.strip()
    if nm.startswith('AI - '):
        return True, True, False
    if nm.startswith('- ') and any(d in nm for d in ('Easy', 'Medium', 'Hard', 'Impossible')):
        return True, True, False
    return True, False, False


def parse_115(payload, stream_ver=DEFAULT_VER):
    """解析115 (teams) 消息.

    外层结构基于官方 smali (ae.smali sswitch_1ac):
      int32 own_player_id
      boolean full_load              (stream_ver >= 141)
      int32 team_count
      readUTF("teams") + int32 block_len + gzip块
    块内按分隔符 ff ff d8 f1 切分记录, 每条记录取 nullable_str 名字.

    返回 dict(players, capacity, team_count, own_player_id, ...)
    """
    result = {'players': [], 'capacity': None, 'team_count': 0,
              'own_player_id': None, 'stream_ver': stream_ver,
              'full_load': False}
    try:
        if len(payload) < 12:
            return result

        r = NetReader(payload)
        own_id = r.int32()
        result['own_player_id'] = own_id

        full_load = False
        if stream_ver >= 141:
            full_load = r.boolean()
            result['full_load'] = full_load

        team_count = r.int32()
        result['team_count'] = team_count

        # teams 块头
        block_name = r.utf()
        block_len = r.int32()
        if block_name != 'teams' or block_len < 0 or block_len > len(payload):
            raise ValueError(f"bad teams block: name={block_name!r} len={block_len}")

        block_body = r.read(block_len)
        if stream_ver >= 141:
            try:
                block_body = gzip.decompress(block_body)
            except OSError:
                if DEBUG_MODE:
                    print("[DEBUG] teams块gzip解压失败, 按明文处理")

        if DEBUG_MODE:
            print(f"[DEBUG] 115: own_id={own_id} full_load={full_load} teams={team_count} "
                  f"block='{block_name}' len={block_len} body={len(block_body)}")

        # 按分隔符切段 (RCN服务器固定分隔符)
        sep = b'\xff\xff\xd8\xf1'
        segments = []
        start = 0
        while True:
            idx = block_body.find(sep, start)
            if idx == -1:
                seg = block_body[start:]
                if seg:
                    segments.append(seg)
                break
            segments.append(block_body[start:idx])
            start = idx + len(sep)

# 每条记录: 按客户端 p.b() 精简结构连续解析 (ae.java 1900-1911 + p.java 122-128)
        #   bool exists + int32 is_ai + byte l + int32 p(credits) + int32 s(队伍)
        #   + nullable name + bool X
        # RCN 服务器: 记录间有 ff ff d8 f1 分隔符填充, 记录无扩展字段
        # 过滤: 真记录 p=credits(初始资金) 且 X=0 (false)
        players = []
        b = block_body
        o = 0
        while o + 14 <= len(b):
            if b[o] != 1:
                o += 1
                continue
            try:
                is_ai = struct.unpack('>i', b[o+1:o+5])[0]
                if is_ai not in (0, 1):
                    o += 1
                    continue
                p_l = b[o+5]
                p_cred = struct.unpack('>i', b[o+6:o+10])[0]
                p_s = struct.unpack('>i', b[o+10:o+14])[0]
                if not (0 <= p_s < team_count):
                    o += 1
                    continue
                o2 = o + 14
                # nullable name
                name = None
                if b[o2] == 1:
                    ln = struct.unpack('>H', b[o2+1:o2+3])[0]
                    if 0 < ln <= 64 and o2+3+ln <= len(b):
                        name = b[o2+3:o2+3+ln].decode('utf-8', errors='replace')
                        o2 += 3 + ln
                    else:
                        o += 1
                        continue
                else:
                    o2 += 1
                X = b[o2]
                # 真记录: is_ai 合法 + credits 合理 + 名字可读
                if p_cred <= 0 or X not in (0, 1):
                    o += 1
                    continue
                is_real, is_ai_name, is_id_only = _classify_name(name)
                players.append({
                    'slot': p_l,
                    'team': p_s,
                    'num': p_l + 1,
                    'name': name if is_real else None,
                    'raw_name': name,
                    'is_ai': is_ai != 0 or is_ai_name,
                    'is_id_only': is_id_only,
                    'player_id': name if is_id_only else None,
                    'ping': None,
                    'exists': True,
                })
                o = o2 + 1
            except (struct.error, IndexError):
                o += 1
        result['players'] = players
        # 排序: 队伍优先(A<B<C...), 编号优先(1<2<3...)
        players.sort(key=lambda p: (p['team'], p['num']))

        if DEBUG_MODE:
            print(f"[DEBUG] 解析出 {len(players)} 个玩家 (队伍+编号排序)")
            for p in players:
                print(f"  {chr(ord('A')+p['team'])}{p['num']} name={p['name']!r} "
                      f"ai={p['is_ai']} id_only={p['is_id_only']}")

        return result

    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] parse_115 异常: {e}")
            import traceback
            traceback.print_exc()
        return result


def parse_teams(payload):
    r = parse_115(payload)
    players = []
    for p in r['players']:
        if p['exists'] and p['name']:
            players.append(p['name'])
    return {'players': players, 'capacity': r['capacity'], 'detailed': r['players']}


def cleanup_old_bin_files(max_age_seconds=3600):
    try:
        current_time = time.time()
        for fname in os.listdir('.'):
            if fname.startswith('server_info_') and fname.endswith('.bin'):
                fpath = os.path.join('.', fname)
                try:
                    if current_time - os.path.getmtime(fpath) > max_age_seconds:
                        os.remove(fpath)
                        if DEBUG_MODE:
                            print(f"[清理] 删除过期文件: {fname}")
                except OSError:
                    pass
    except Exception as e:
        if DEBUG_MODE:
            print(f"[清理] 清理过程异常: {e}")


def finish_report(sock, got_106, got_115, target):
    info = parse_106(got_106)
    players_detail = []
    players_names = []
    capacity = None
    if got_115:
        t = parse_115(got_115)
        players_detail = t.get('players', [])
        players_detail = [p for p in players_detail
                          if p.get('exists') and p.get('name')
                          and not p['name'].startswith(NAME)]
        players_names = [p['name'] for p in players_detail if p.get('exists')]
        capacity = t.get('capacity') or t.get('team_count')
        current_players = len(players_detail)
    else:
        current_players = 0

    print()
    print(format_room(info, target, players_names, capacity, players_detail, _MAX_PLAYERS_DISPLAY, player_count=current_players, show_players=SHOW_PLAYERS))

    timestamp = int(time.time())
    safe_target = re.sub(r'[^\w\-]', '_', target)
    open(f"server_info_106_{safe_target}_{timestamp}.bin", "wb").write(got_106)
    if got_115:
        open(f"server_info_115_{safe_target}_{timestamp}.bin", "wb").write(got_115)
    if sock is not None:
        send_leave(sock)
        time.sleep(0.5)
        sock.close()


def direct_main():
    if not QUIET:
        ip, _, port = DIRECT_TARGET.rpartition(":")
        port = int(port or 5123)
        print(f"=== 直连 {ip}:{port} (客户端 1.15) ===")
    s, probe = connect_and_161(ip, port)
    sid = probe.server_id
    if not QUIET:
        print(f"[1] 已连, sid={sid!r} conn={probe.conn_id}")
    send_110(s, sid, CK, probe.conn_id, probe.challenge_y)
    got_106 = None
    got_115 = None
    kick_reason = None
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            mtype, payload = read_frame(s, 2)
        except (socket.timeout, OSError):
            break
        if mtype == 151:
            probe._handle_relay_151(payload)
        elif mtype == 106:
            got_106 = payload
            if not QUIET:
                print(f"[2] 收到106 SERVER_INFO ({len(payload)}B)")
        elif mtype == 115:
            got_115 = payload
            if not QUIET:
                print(f"[2] 收到115 teams ({len(payload)}B)")
        elif mtype == 150:
            kick_reason = NetReader(payload).utf()
            if not QUIET:
                print("KICKED:", kick_reason)
            break
        if got_106 and got_115:
            break
    if kick_reason:
        _handle_kicked(kick_reason, s)
    if got_106 is None:
        if not QUIET:
            print("!! 未收到106")
        s.close()
        sys.exit(1)
    finish_report(s, got_106, got_115, DIRECT_TARGET)


def relay_host(rid: str) -> str:
    return rid[0].lower() + ".relay.corrodinggames.com"


def relay_session(host, port, rid, depth=0):
    tag = f"[{depth}]" if depth else ""
    if not QUIET:
        print(f"{tag}=== 中继会话 {rid} @ {host}:{port} (depth={depth}) ===")
    s, probe = connect_and_161(host, port, rid)
    sid = probe.server_id
    if not QUIET:
        print(f"{tag}[1] 中继161: sid={sid!r} conn={probe.conn_id} chal={probe.challenge_y}")
    if not sid:
        if not QUIET:
            print(f"{tag}!! 握手失败，未收到房间服务器响应")
        s.close()
        return None

    send_110(s, sid, CK, probe.conn_id, probe.challenge_y)
    if not QUIET:
        print(f"{tag}[2] 110已发送，等待验证/跳转/房间信息...")

    got_106 = None
    got_115 = None
    kick_reason = None
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            mtype, payload = read_frame(s, 3)
        except socket.timeout:
            continue
        except OSError:
            break
        if mtype == 161:
            probe._parse_161(payload)
            if not QUIET:
                print(f"{tag}    收到游戏服务器161: sid={probe.server_id!r} conn={probe.conn_id}")
            if probe.server_id:
                send_110(s, probe.server_id, CK, probe.conn_id, probe.challenge_y)
                if not QUIET:
                    print(f"{tag}    已重新发送110到游戏服务器")
        elif mtype == 163:
            r = NetReader(payload)
            r.byte(); relay_ver = r.int32(); r.int32(); r.boolean()
            if not QUIET:
                print(f"{tag}    163 relay_ver={relay_ver}")
        elif mtype == 151:
            probe._handle_relay_151(payload)
            if not QUIET:
                print(f"{tag}    已应答151挑战")
        elif mtype == 178:
            addr = probe._handle_178(payload)
            if not QUIET:
                print(f"{tag}[3] 收到178跳转: {addr!r}")
            s.close()
            if not addr:
                if not QUIET:
                    print(f"{tag}!! 178地址为空")
                return None
            addr = re.sub(r'^\[(TCP|UDP)\]\s*', '', addr.strip())
            if not QUIET:
                print(f"{tag}[3] 清理后地址: {addr!r}")
            if "/" in addr:
                hp, _, rid2 = addr.partition("/")
                if ":" in hp:
                    h2, _, p2 = hp.rpartition(":")
                    p2 = int(p2) if p2.isdigit() else 5123
                else:
                    h2, p2 = hp, 5123
                if not QUIET:
                    print(f"{tag}[3] 跳转到中继 {h2}:{p2} 房间 {rid2}")
                return relay_session(h2, p2, rid2, depth + 1)
            else:
                if ":" in addr:
                    h2, _, p2 = addr.rpartition(":")
                    p2 = int(p2) if p2.isdigit() else 5123
                else:
                    h2, p2 = addr, 5123
                if not QUIET:
                    print(f"{tag}[3] 跳转到节点 {h2}:{p2}")
                return node_session(h2, p2)
        elif mtype == 106:
            got_106 = payload
            if not QUIET:
                print(f"{tag}[4] 收到106 SERVER_INFO ({len(payload)}B)")
        elif mtype == 115:
            got_115 = payload
            if not QUIET:
                print(f"{tag}[4] 收到115 teams ({len(payload)}B)")
        elif mtype == 108:
            r = NetWriter(); r.int64(NetReader(payload).int64()); r.byte(1)
            s.sendall(pack_frame(109, r.output()))
        elif mtype == 150:
            kick_reason = NetReader(payload).utf()
            if not QUIET:
                print(f"{tag}KICKED:", kick_reason)
            break
        elif mtype == 111:
            if not QUIET:
                print(f"{tag}DISCONNECT")
            break
        elif mtype == 141:
            sender, message = parse_141(payload)
            if not QUIET:
                if sender:
                    print(f"[聊天] {sender}: {message}")
                else:
                    print(f"[聊天] {message}")
        else:
            if not QUIET:
                print(f"{tag}    未处理消息 mtype={mtype} len={len(payload)} hex={payload.hex()[:80]}")
        if got_106 is not None:
            send_leave(s)
            s.close()
            return got_106, got_115
    s.close()
    if kick_reason:
        return ("KICKED", kick_reason)
    if got_106 is None and not QUIET:
        print(f"{tag}!! 未收到106（房间可能不存在/已关闭/需要密码）")
    return None


def node_session(host, port):
    if not QUIET:
        print(f"[4] 连接节点: {host}:{port}")
    try:
        s2 = socket.create_connection((host, port), timeout=8)
        s2.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as e:
        if not QUIET:
            print(f"!! 节点连接失败: {e}")
        return None
    w = NetWriter()
    w.utf(PROTO_MAGIC)
    w.int32(4); w.int32(CLIENT_VER); w.int32(1)
    w.nullable_str(None)
    w.utf(NAME); w.utf(NID); w.utf('')
    s2.sendall(pack_frame(160, w.output()))
    probe2 = RWProbe('x', 0, NAME, unit_checksum=CK or 0, client_version=CLIENT_VER, verbose=False)
    probe2.sock = s2
    probe2._wait_161()
    sid2 = probe2.server_id
    if not QUIET:
        print(f"[5] 节点161: sid={sid2!r} conn={probe2.conn_id}")
    if not sid2:
        if not QUIET:
            print("!! 节点握手失败")
        s2.close()
        return None
    send_110(s2, sid2, CK, probe2.conn_id, probe2.challenge_y)
    got_106 = None
    got_115 = None
    kick_reason = None
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            mtype, payload = read_frame(s2, 2)
        except socket.timeout:
            continue
        except OSError:
            break
        if mtype == 161:
            probe2._parse_161(payload)
            if not QUIET:
                print(f"[6] 收到游戏服务器161: sid={probe2.server_id!r} conn={probe2.conn_id}")
            if probe2.server_id:
                send_110(s2, probe2.server_id, CK, probe2.conn_id, probe2.challenge_y)
                if not QUIET:
                    print(f"[6] 已重新发送110到游戏服务器")
        elif mtype == 163:
            r = NetReader(payload)
            r.byte(); relay_ver = r.int32(); r.int32(); r.boolean()
            if not QUIET:
                print(f"[6] 163 relay_ver={relay_ver}")
        elif mtype == 151:
            probe2._handle_relay_151(payload)
        elif mtype == 106:
            got_106 = payload
            if not QUIET:
                print(f"[6] 收到106 SERVER_INFO ({len(payload)}B)")
        elif mtype == 115:
            got_115 = payload
            if not QUIET:
                print(f"[6] 收到115 teams ({len(payload)}B)")
        elif mtype == 108:
            r = NetWriter(); r.int64(NetReader(payload).int64()); r.byte(1)
            s2.sendall(pack_frame(109, r.output()))
        elif mtype == 150:
            kick_reason = NetReader(payload).utf()
            if not QUIET:
                print("KICKED:", kick_reason)
            break
        elif mtype == 141:
            sender, message = parse_141(payload)
            if not QUIET:
                if sender:
                    print(f"[聊天] {sender}: {message}")
                else:
                    print(f"[聊天] {message}")
        else:
            if not QUIET:
                print(f"[6] 未处理消息 mtype={mtype} len={len(payload)} hex={payload.hex()[:80]}")
        if got_106 is not None:
            send_leave(s2)
            s2.close()
            return got_106, got_115
    s2.close()
    if kick_reason:
        return ("KICKED", kick_reason)
    if got_106 is None and not QUIET:
        print("!! 未收到106")
    return None


def _handle_kicked(reason, sock=None):
    """统一处理被踢出房间的提示 (sock 非空时先关闭连接)"""
    if "No free slots" in reason:
        print("[房间查询失败] 房间已满，无法加入")
    elif "游戏已经开始" in reason or "game already started" in reason.lower():
        print("[房间查询失败] 游戏已经开始，无法加入")
    else:
        print(f"[房间查询失败] {reason}")
    if sock is not None:
        sock.close()
    sys.exit(0)

def relay_main():
    host = relay_host(RID)
    if not QUIET:
        print(f"=== 查询房间 {RID} (客户端 1.15) ===")
        print(f"[0] 中继: {host}:5123 (DNS={socket.gethostbyname(host)})")
    result = relay_session(host, 5123, RID)
    if isinstance(result, tuple) and result[0] == "KICKED":
        _handle_kicked(result[1])
    if result is None:
        if not QUIET:
            print("未获取到房间信息（房间可能不存在/已关闭）")
        sys.exit(1)
    got_106, got_115 = result
    finish_report(None, got_106, got_115, RID)


def main():
    cleanup_old_bin_files()
    if not RID:
        print(HELP_TEXT)
        return
    if LIST_MODE:
        room_list()
        return
    if DIRECT_TARGET:
        direct_main()
        return
    if RID.startswith("u_"):
        if not QUIET:
            print("!! u_房间(Auto Server)请先用 --list 找到该房的 IP:端口 再直连")
        return
    # 所有中继房间统一走 relay_main (含 h.relay.* 国际中继)
    relay_main()

if __name__ == "__main__":
    main()