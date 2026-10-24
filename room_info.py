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
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request
import uuid
import random
import zlib

# 全局安静模式（插件调用时设为 True，屏蔽调试输出）
# 命令行直接运行时自动关闭静默（否则 --list 等模式看不到任何输出）
QUIET = True if __name__ != "__main__" else False
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
        # 178 跳转地址(中继在房号存在时可能直接下发, 而不先发 161)
        self.jump_addr = None

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
        """等待 161 握手。

        注意: 中继(尤其 q/s 这类 RWHPS 中继)在房号存在时可能直接回 178 跳转,
        而不是先回 161。早期版本没有 178 分支, 会落到 else 分支只打印一行,
        然后继续死等 161 直到超时, 表现为 "握手失败/未收到房间服务器响应"。
        这里把 178 也捕获下来, 交给调用方(relay_session)处理跳转。
        """
        self.jump_addr = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                mtype, payload = read_frame(self.sock, 5)
            except socket.timeout:
                # 已经拿到跳转地址就不用再等了
                if self.jump_addr:
                    return
                continue
            except OSError:
                break
            if self.verbose:
                print(f"[DBG] recv mtype={mtype} len={len(payload)}")
            if mtype == 161:
                self._parse_161(payload)
                return
            elif mtype == 178:
                self.jump_addr = self._handle_178(payload)
                self.log(f"178 jump: {self.jump_addr!r}")
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
        """中继挑战 151 → 应答 152。

        字段顺序严格对齐客户端 ae.smali (line 50561-50600):
          int32 v7 / int32 v8 / bool b1[+int32 bf_i] / bool b2[+int32 bf_j]
          然后 v8 in (5,6): utf s1 / utf s2 / int32 v10
               v8 == 7:     utf s2 / int32 v6
        应答 v0 的分支(客户端 line 5553-5620):
          v8==0 → str(bf_i)          ← 早期版本发空串, 导致 q 中继 152 被忽略
          v8==1 → str(bf_j)
          v8==2 → challenge_e(bf_i)
          v8==3/4 → sha256("bf_i|bf_j") 前 14 位
          v8==5/6 → POW: f_c(s2 [+bf_i] + str(i)) == s1, 上限 10000000 否则 "max"
          v8==7 → POW: 上限 10000
        """
        _t151 = time.time()
        v7 = v8 = bf_i = bf_j = 0
        v10 = 0
        s1 = s2 = ""
        try:
            r = NetReader(payload)
            v7 = r.int32()
            v8 = r.int32()
            if r.boolean():
                bf_i = r.int32()
            if r.boolean():
                bf_j = r.int32()
            # 5/6/7 才有后续字符串与上限; 其他类型读不到就保持默认
            if v8 in (5, 6):
                s1 = r.utf()
                s2 = r.utf()
                v10 = r.int32()
            elif v8 == 7:
                s2 = r.utf()
                v10 = r.int32()

            if not QUIET:
                print(f"151 relay: v7={v7} v8={v8} bf_i={bf_i} bf_j={bf_j} "
                      f"s1={s1!r} s2={s2!r} v10={v10}")

            v0 = ""
            if v8 == 0:
                v0 = str(bf_i)
            elif v8 == 1:
                v0 = str(bf_j)
            elif v8 == 2:
                v0 = challenge_e(bf_i)
            elif v8 in (3, 4):
                v0 = f_c(f"{bf_i}|{bf_j}")
            elif v8 in (5, 6, 7):
                # v8==6 用 s2+bf_i 作为前缀; 5 和 7 只有 s2
                prefix = s2 if v8 != 6 else (s2 + str(bf_i))
                limit = 10000000 if v8 in (5, 6) else 10000
                if v10 > limit:
                    v0 = "max"
                else:
                    v0 = "-1"
                    if s1 and s2:
                        t_pow = time.time()
                        POW_TIMEOUT = 30
                        for i in range(v10 + 1):
                            if f_c(prefix + str(i)) == s1:
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
                v0 = "max"

            ack = NetWriter()
            ack.int32(v7)
            ack.int32(v8)
            ack.utf(v0)
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
        """解析 178 PACKET_RECONNECT_TO。
        客户端 ae.smali sswitch_72f 结构: byte / int32 / boolean / int32(N) / N x utf 地址
        客户端取 list.get(0) 作为连接目标。
        """
        addr = ""
        try:
            r = NetReader(payload)
            b0 = r.byte()
            i1 = r.int32()
            b2 = r.boolean()
            n = r.int32()          # 服务器数量
            addrs = []
            for _ in range(max(0, n)):
                if r.remaining() < 2:
                    break
                addrs.append(r.utf())
            if addrs:
                addr = addrs[0]
            self.log(f"178 jump n={n} addrs={addrs!r} b0={b0} i1={i1} b2={b2}")
        except Exception:
            try:
                r2 = NetReader(payload)
                addr = r2.utf()
                self.log(f"178 legacy parse: addr={addr!r}")
            except Exception:
                addr = ""
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
  python3 room_info.py <房间ID>           查询中继房间 (如 r5132 / a5132 / h1234 / hytvvi)
  python3 room_info.py <IP>:<端口>        直连服务器 (如 1.2.3.4:5123)
  python3 room_info.py --list             房间列表(真人房优先)
  python3 room_info.py --list-json        房间列表(JSON 输出, 供插件调用)
  python3 room_info.py --cleanup-bins     清理过期 bin 缓存(默认1小时前, 可用 --max-age 覆盖)
  python3 room_info.py --help             显示本帮助

房间列表选项 (配合 --list / --list-json):
  --source <源>         all(默认) / qroom(真人房) / official(官方Auto Server)
  --joinable            只列可加入的房间(状态=房间中)
  --status <状态>       battleroom / ingame
  --search <关键词>     按 房主名/地图名/描述 搜索(不区分大小写)
  --mod <类型>          mod(只看模组房) / vanilla(只看原版房)
  --full <模式>         hide(隐藏人满) / only(只看人满)
  --all                 official 源保留全部条目(仅调试用)

查询选项:
  --name <名字>         设置探针名字 (默认 ABAB探针)
  --debug               调试输出

支持的中继房号:
  首字母+数字    r5132 / s1024 / a5132 / b5132 / h1234 / c8888 / o9999 / q7777
  国际房(纯字母) h 开头, 如 hytvvi / hlbifz

可用中继首字母 (实测 5123 端口开放):
  a/b/h  官方中继 (Relay US1 / US2 / ASIA2)
  o/q/r/s 第三方中继 (RELAY-CN / Fancy-CN / Tiexiu.xyz)
  c/g/j/v 有域名但端口未开放

房间列表数据源:
  Q Room List    https://www.rtsbox.cn/api/paiwei/room_last.php  (真人房, 有房主/地图/人数)
  Official       gs1.corrodinggames.com (官方 Auto Server)
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
# 注意: --list / --list-json 是"模式开关"而非带值选项, 需保留下来供模式判定
_MODE_SWITCHES = ("--list", "--list-json", "--cleanup-bins")
_positional = [a for a in sys.argv[1:]
               if not a.startswith("--") or a in _MODE_SWITCHES]
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
LIST_JSON = _arg1 == "--list-json"
# 独立清理 bin 缓存: 供插件定时任务调用, 也可手动执行
CLEANUP_MODE = _arg1 == "--cleanup-bins"
# 列表筛选开关
LIST_ALL = "--all" in sys.argv              # official 源保留全部条目(调试用)
LIST_JOINABLE = "--joinable" in sys.argv    # 只保留可加入(battleroom)
LIST_KEYWORD = _find_arg("--search") or ""  # 按 房主/地图/描述 搜索
LIST_STATUS = _find_arg("--status") or ""   # 按 status 过滤
LIST_SOURCE = _find_arg("--source") or "all"  # all / qroom / official
LIST_MOD = _find_arg("--mod") or ""         # mod / vanilla
LIST_FULL = _find_arg("--full") or ""       # hide / only
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
    """注册包 110。字段顺序严格对齐客户端 ae.f(c) / sendRegisterConnection:
       magic / regVer=5 / clientVer / verAdjust / name / password(nullable) /
       networkClientId / 身份哈希 / unitsChecksum / challenge_e(conn) / hash_e_int(chal)
    """
    w = NetWriter()
    w.utf(PROTO_MAGIC)
    w.int32(REG_VER); w.int32(CLIENT_VER); w.int32(CLIENT_VER)
    w.utf(NAME); w.nullable_str(None)
    w.utf(NID)                               # network_client_id (此前误发 PROTO_MAGIC)
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


# ================================================================ Q Room List (真人房)
# 来源: RW++ 启动器 io.github.rwpp.impl.BaseNetImpl$init$6$1
#   接口: https://www.rtsbox.cn/api/paiwei/room_last.php
#   返回: {"status":"success","data":[ {...} ],"page":"1","page_size":"1000"}
#   字段: internal_id(房号) / map_name(地图) / current_players / max_players
#         host_name(房主) / is_started(0=房间中,1=游戏中) / is_mod(是否模组)
#         host_ip / host_port / myhost_ip
# 这是真人玩家开的房; Official Room List 里几乎全是 Auto Server。
QROOM_URL = "https://www.rtsbox.cn/api/paiwei/room_last.php"
QROOM_TIMEOUT = 15


def _as_int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def fetch_qroom_list(timeout=None) -> list:
    """拉取 Q Room List (真人房), 返回统一结构的房间字典列表。

    统一结构:
      source      "qroom"
      id          平台内部编号(internal_id, 纯数字, 不能用于查房)
      addr        直连地址 ip:port, 可直接用于查房(可能为空)
      name        房主名
      map         地图名(已去掉 .tmx 后缀)
      players     当前人数
      max         人数上限
      status      "battleroom" / "ingame"
      is_mod      是否模组房
      is_started  原始开局标记
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(QROOM_URL, headers={
        "User-Agent": "okhttp/4.12.0",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    })
    data = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout or QROOM_TIMEOUT, context=ctx) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    pass
    except Exception as e:
        if not QUIET:
            print(f"[qroom] 请求失败: {e}")
        return []

    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        if not QUIET:
            print("[qroom] JSON 解析失败")
        return []

    if not isinstance(obj, dict) or obj.get("status") != "success":
        if not QUIET:
            print(f"[qroom] 状态异常: {str(obj)[:80]}")
        return []

    rooms = []
    for r in obj.get("data") or []:
        if not isinstance(r, dict):
            continue
        mapname = str(r.get("map_name") or "").strip()
        # 去掉 .tmx 后缀, 便于展示与搜索
        if mapname.lower().endswith(".tmx"):
            mapname = mapname[:-4]
        started = _as_int(r.get("is_started"), 0)
        # RW++ 客户端并不使用 host_ip / host_port:
        # qRoomListProvider (BaseNetImpl$init$6$1) 只读取 internal_id / map_name /
        # current_players / max_players / is_started / host_name,
        # 然后拼出房号 "Q" + internal_id, 走 q.relay.corrodinggames.com 进房。
        # host_ip:5129 是房主公网出口 IP, TCP/UDP 一律超时, 不可用。
        internal_id = str(r.get("internal_id") or "").strip()
        rooms.append({
            "source": "qroom",
            "id": internal_id,
            "addr": "",
            # 房号 = 大写 Q + internal_id; q 中继会回 178 跳转到真实节点
            "room_id": f"Q{internal_id}" if internal_id else "",
            "name": str(r.get("host_name") or "").strip() or "?",
            "map": mapname or "?",
            "players": _as_int(r.get("current_players"), 0),
            "max": _as_int(r.get("max_players"), 0),
            "status": "ingame" if started else "battleroom",
            "is_mod": _as_int(r.get("is_mod"), 0) == 1,
            "is_started": started,
        })
    return rooms


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


# ================================================================ 房间列表
# 官方 masterserver 返回的列表里混有推广条目与伪造人数的展示位。
# 判据分散在 classify_room(结构) 与 fetch_official_rooms(内容/人数)。

_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def classify_room(room: dict) -> tuple:
    """对房间做结构分类, 返回 (类别, 原因)。

    类别:
      "link"    —— 链接条目(Discord/QQ 群), 不是房间
      "invalid" —— 字段无效(ip/port 不合法)
      "ok"      —— 结构正常的房间(仍需按 status 判断是否可加入)

    注意: 这里不把"含控制字符"当作剔除依据 —— RELAY-CN 给所有开放到列表的
    真人房都加 \\x01 前缀(如房主名 "\\x01测试房"), 据此删会误杀真人房。
    """
    ip = (room.get("ip") or "").strip()
    port = (room.get("port") or "").strip()
    mode = (room.get("mode") or "").strip()
    status = (room.get("status") or "").strip()

    # 1. 链接条目: mode/status 为 LINK, 或 ip 形如 url:xxx
    if mode.upper() == "LINK" or status.upper() == "LINK":
        return "link", "链接条目"
    if ip.lower().startswith("url:"):
        return "link", "网页链接"

    # 2. 字段无效
    if not ip:
        return "invalid", "缺少IP"
    if not _IPV4_RE.fullmatch(ip):
        return "invalid", "IP格式非法"
    if not port.isdigit():
        return "invalid", "端口非法"

    return "ok", ""


def is_joinable(room: dict) -> bool:
    """是否为可加入的房间(status=battleroom)"""
    return (room.get("status") or "").strip().lower() == "battleroom"


def _parse_room_line(line: str, index: int) -> dict:
    """把一行 masterserver 数据解析成房间字典。

    注意: 展示用字段经过 _clean() 清洗(去控制字符), 但内容判定必须看
    清洗前的原文, 因此额外保留 name_raw / desc_raw。
    """
    f = line.split(",")
    def g(i, d=""):
        return _clean(f[i]) if len(f) > i and f[i] else d
    def raw(i):
        return f[i] if len(f) > i else ""
    # 22 字段含义(按客户端 j/r.smali 反编译逐列核实):
    #   0  房间UUID        1  房名            2  版本号
    #   3  对外IP          4  内网IP          5  端口
    #   6  ?               7  房主名(created_by)
    #   8  ?               9  描述
    #   10 模式(skirmishMap/customMap/LINK)   11 状态(battleroom/ingame/LINK)
    #   12 游戏版本        13 ?               14 描述(备用)
    #   15 当前人数        16 人数上限        17 ?
    #   18 UUID副本        19 ?               20 ?
#   21 A 字段(仅协议层使用, 插件不再解析)
    return {
        "idx": index,
        "id": g(0),
        "name": g(1, "?"),
        "ver": g(2),
        "ip": g(3),
        "lan_ip": g(4),
        "port": g(5),
        "host": g(7),          # 房主名 created_by
        "type": g(7),          # 兼容旧字段名
        "mode": g(10),
        "status": g(11),
        "game_ver": g(12),
        "desc": g(9) or g(14, ""),
        "players": g(15, "0"),
        "max": g(16, "?"),
        # 第 21 列: 加入方式。0 = 列表里的 ip:port 就是真实地址;
        # 非 0 = 中继房, 需要向 masterserver 发 action=get 换真实地址。
        "join_a": g(21),
        # 原始未清洗字段: 仅用于内容判定, 不对外展示
        "name_raw": raw(1),
        "host_raw": raw(7),
        "desc_raw": raw(9) or raw(14),
        "raw": line,
    }

# 列表条目筛选。只回答一个问题: 这个房间值不值得探针去抓。
#   · 超员房间(max 超出游戏真实范围)是刷出来的展示位, 抓它没有意义
#   · 空置入口、链接条目、纯推广话术同理
MAX_PLAYERS_LIMIT = 100

# 中继自身入口: 房主名或描述含 RELAY / 中继 / 入口 等字样。
# 这类条目是 0/1000~0/10 的入口占位, 不是可玩的房间。
_RELAY_ENTRY_RE = re.compile(
    r"(?:^|[^A-Za-z])(?:RELAY|Relay)(?:[^A-Za-z]|$)"
    r"|中继|入口|节[点點]",
    re.IGNORECASE,
)


def _is_relay_entry(host: str, desc: str) -> bool:
    """房主名或描述是否为中继自身的入口占位条目。

    只判断标识本身; 房间是否为空置由调用方结合人数决定(空置入口才剔除,
    有人的入口可能正是用户自己的房)。
    """
    blob = f"{host or ''} {desc or ''}"
    return bool(_RELAY_ENTRY_RE.search(blob))


# 推广话术的正则判据(实测出来的)。
#
# 单靠"群"字会误杀: 「群雄单混」「[群雄单混]攻城掠地匹配」是正常玩法房名。
# 也单靠人数不行: max 字段被大面积伪造(max=2000 有 54 条, 3334 有 34 条,
# 9999 有 19 条, 89842 有 8 条), 真实上限一般是 10/20。
#
# 实测结论 —— 推广房名是"社会关系/拉人话术", 正常房名是"玩法+地图":
#   推广: 𒀱EK群内有可爱男娘ABCD / 飞艇时代模组群 / 开学季高活跃铁锈群
#         银杏社②群687958465 / 𒀱四军对峙群94600232 / 红温…来群338254
#   正常: 群雄单混·多人排位赛 / [官方6v6]公平竞技赛 / 综合排位大厅|对战与生存
_GROUP_AD_RE = re.compile(
    # —— 明确的群号: 群在数字前 / 数字在群前 两种顺序都要覆盖 ——
    r"群号\s*\d{6,}"
    r"|群\s*\d{6,}"            # 群94600232 / 群687958465
    r"|\d{6,}\s*群"            # 762601886群
    r"|\d{6,}\s*裙"
    # —— 拉人动作 ——
    r"|加群|进群|入群|进裙|加裙|来群|进群聊|群聊"
    r"|一键加群|点击加群|扫码加"
    # —— 明确指代 QQ 群的复合词 ——
    r"|QQ\s*群|企鹅群|交流群|玩家群|开黑群|粉丝群|水友群"
    r"|铁锈群|模组群|联机群|游戏群|活跃群"
    r"|群里有|群内|群里"          # 群里有猫娘 / 群内有可爱男娘
    # —— 社交/主播导向(正常房名不会写) ——
    r"|直播间|直播服务器|主播|关注|点赞"
    r"|男娘|女装|变装|性感|萝莉|正太|猫娘|猫耳",
    re.IGNORECASE,
)

# "XX社" 型群名(银杏社②群 / LSZ银杏社公开服): 社 前是社团名, 后接群号/公开服。
# 单独用 "社" 会误杀"社区服"这类, 所以要求 社 后紧跟群/公开服/序号 等。
_COMMUNITY_RE = re.compile(
    r"社\s*[①②③④⑤⑥⑦⑧⑨\d]"
    r"|社\s*(?:公开服|公益服|专服|主服|群)"
    r"|[A-Za-z]{2,}社",
    re.IGNORECASE,
)

# 正常玩法房名里会出现的词。含这些词时不按"群"字剔除,
# 用于避免 "群雄单混" / "群战" 这类误杀。
_PLAY_STYLE_RE = re.compile(
    r"群雄|群战|群殴|群英"
    r"|排位|匹配|对战|生存|团混|守家|入侵|攻防|攻城"
    r"|v\d+|\d+v\d+|vs|ranked|fair|ffa"
    r"|单挑|多人|pvp|pve",
    re.IGNORECASE,
)


def _is_group_ad(name: str, host: str, desc: str) -> bool:
    """房名/房主名/描述是否为拉群推广。

    只按文本内容判断, 与 ip:port、人数无关。

    误杀保护: 房名里出现明确玩法词(群雄/排位/6v6/生存…)时,
    不把其中的"群"字当作推广 —— 否则会删掉「群雄单混·多人排位赛」这类正常房。
    但群号、加群、男娘等强特征不受此豁免: 即使带玩法词也照样剔除。
    """
    blob = f"{name or ''} {host or ''} {desc or ''}"
    # "XX社公开服" 型社团名(银杏社②群 / LSZ银杏社公开服)
    if _COMMUNITY_RE.search(blob):
        return True
    m = _GROUP_AD_RE.search(blob)
    if not m:
        return False
    # 命中的是"弱"特征(只因为出现"群"字)且房名有玩法词 -> 不算推广
    hit = m.group(0)
    if "群" in hit or "裙" in hit:
        strong = re.search(r"\d{6,}|加群|进群|入群|进裙|加裙|来群|群聊"
                           r"|QQ|企鹅|交流群|玩家群|开黑群|粉丝群|水友群"
                           r"|铁锈群|模组群|联机群|游戏群|活跃群", hit)
        if not strong and _PLAY_STYLE_RE.search(blob):
            return False
    return True


def _challenge_code(a) -> str:
    """根据列表第 21 列(A 字段)生成 masterserver get 的挑战码。

    反推自客户端 gameFramework/j/m.java 的 a(int i):
        i == 0        -> ""
        i < 100000    -> MD5("x"  + i)[:10]
        i < 200000    -> MD5("y"  + i)[:11]
        i < 300000    -> MD5("z"  + i)[:12]
        i < 1000000   -> MD5("xx" + i)[:13]
        i < 2000000   -> MD5("yy" + i)[:14]
        其它          -> "NA"

    实测 A=10605 -> "da7821ccf7", action=get 返回 OK。
    """
    try:
        a = int(a)
    except (TypeError, ValueError):
        return "NA"
    if a <= 0:
        return ""
    if a < 100000:
        pre, n = "x", 10
    elif a < 200000:
        pre, n = "y", 11
    elif a < 300000:
        pre, n = "z", 12
    elif a < 1000000:
        pre, n = "xx", 13
    elif a < 2000000:
        pre, n = "yy", 14
    else:
        return "NA"
    return hashlib.md5((pre + str(a)).encode("utf-8")).hexdigest()[:n]


def resolve_official_addr(room: dict, timeout: int = 15) -> str:
    """把中继房在列表里的入口地址解析成真实游戏地址。

    中继房(RELAY-CN 等通过 UPList 开放到官方列表)在列表里的 ip:port 全是
    同一个入口(如 18.216.139.119:5158), 直连会被踢
    "Your client is newer then the server"。真实地址需向 masterserver 发
    action=get, 参数 c 由 A 字段推导。

    仅内部使用; 结果只用于连接, 不对外展示。
    返回 "ip:port", 失败返回空串。
    """
    uuid_ = (room.get("id") or "").strip()
    raw = room.get("raw") or {}
    a = room.get("join_a") or raw.get("join_a") or "0"
    # A == 0 说明列表里的 ip:port 就是真实地址, 无需解析
    try:
        if int(str(a).strip() or "0") == 0:
            return ""
    except ValueError:
        return ""
    c = _challenge_code(a)
    if not c or c == "NA":
        return ""
    for base in MASTERSERVERS:
        params = {"action": "get", "game_id": uuid_, "c": c}
        url = base + "/interface?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"rw android {CLIENT_VER} en", "Language": "en"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    try:
                        data = gzip.decompress(data)
                    except OSError:
                        pass
            text = data.decode("utf-8", "replace")
        except Exception:
            continue
        rows = [x for x in text.splitlines() if x.strip()]
        # 响应: CORRODINGGAMES[1.0] / OK / <哈希> / <房间CSV>
        if len(rows) < 4 or rows[1].strip().upper() != "OK":
            continue
        f = rows[3].split(",")
        if len(f) > 5:
            ip, port = f[3].strip(), f[5].strip()
            if ip and port.isdigit():
                return f"{ip}:{port}"
    return ""


def _normalize_official(room: dict) -> dict:
    """把 Official Room List 的一条记录转成统一结构。"""
    oip = str(room.get("ip") or "").strip()
    oport = str(room.get("port") or "").strip()
    oaddr = f"{oip}:{oport}" if oip and oport else oip

    # 房名(第1列)常是 "Unnamed", 真正的标识在房主名(第7列)。
    # 客户端列表也同时展示这两列(gi.java: fVar.n 与 fVar.t\fVar.u),
    # 因此这里两者都保留, 展示时优先用房主名。
    name = (room.get("name") or "").strip()
    host = (room.get("host") or "").strip()
    if not name or name.lower() == "unnamed":
        display = host or name or "?"
    else:
        display = name if not host or host == name else f"{name}（{host}）"

    # 地图名: 描述列常见 "Crossing Large (10p)" / "Auto Server 4+ [US-E #3]"
    desc = (room.get("desc") or "").strip()
    status = (room.get("status") or "").strip().lower() or "unknown"

    return {
        "source": "official",
        "id": room.get("id", ""),
        "addr": oaddr,
        "name": display,
        "host": host,
        "map": desc or "-",
        "players": _as_int(room.get("players"), 0),
        "max": _as_int(room.get("max"), 0),
        "status": status,
        "is_mod": False,
        "is_started": 1 if status == "ingame" else 0,
        "desc": desc,
        # 中继房判定所需(仅内部使用, 不展示): 非 0 表示列表地址是注册入口
        "join_a": str(room.get("join_a") or "0").strip(),
        # 筛选结果(只在 include_all=True 时有意义, 便于调试)
        "kind": room.get("kind") or "ok",
        "kind_reason": room.get("kind_reason") or "",
        "raw": room,
    }


def fetch_official_rooms(include_all: bool = False) -> list:
    """拉取 Official Room List (官方 masterserver), 筛掉不可信条目后返回统一结构。

    注意: 内容判定不做 ip:port 聚合, 也不按控制字符判定。
    RELAY-CN 把真人房开放到列表(UPList)时:
      · 所有房间共用同一个入口 ip:port (18.216.139.119:5158)
      · 所有房间的房名/房主名/描述都带 \\x01 前缀
    这两种特征对真人房与推广房完全一样, 因此都不能作为判据。

    真正的区别在文本内容:
      1. classify_room   —— 链接条目 / 字段非法
      2. _is_relay_entry —— 房主名或描述是中继自身入口(且房间空置)
      3. _is_group_ad    —— 房主名或描述带群号/加群/直播等推广话术
    """
    lines = masterserver_request({"action": "list", "game_version": str(CLIENT_VER),
                                  "game_version_beta": "false"})
    if not lines:
        return []

    rooms = []
    seen_id = set()
    for i, line in enumerate(lines[1:]):
        if not line.strip():
            continue
        room = _parse_room_line(line, i)
        kind, why = classify_room(room)
        if not include_all and kind != "ok":
            continue

        name = room.get("name") or ""
        host = room.get("host") or ""
        desc = (room.get("desc") or "").strip()
        try:
            players = int(str(room.get("players") or "0").strip() or "0")
        except ValueError:
            players = 0
        try:
            max_players = int(str(room.get("max") or "0").strip() or "0")
        except ValueError:
            max_players = 0

        # 人数上限超出游戏真实范围 —— 展示位, 数值不可信, 探针跳过
        if max_players > MAX_PLAYERS_LIMIT:
            kind, why = "skip", f"人数上限超出范围({max_players})"
        # 在线人数超过上限本身 —— 逻辑上不可能, 只能伪造(如 943438591/10)
        if kind == "ok" and max_players > 0 and players > max_players:
            kind, why = "skip", f"人数越界({players}/{max_players})"
        # 中继自身入口占位: "RELAY-CN 入口" / "Relay ASIA2" 之类, 且没人
        # (有人的入口条目不排除 —— 那可能就是某个玩家自己的房间)
        if kind == "ok" and _is_relay_entry(host, desc) and players <= 0:
            kind, why = "skip", "空置入口"
        # 拉群推广
        if kind == "ok" and _is_group_ad(name, host, desc):
            kind, why = "skip", "推广话术"

        room["kind"] = kind
        room["kind_reason"] = why
        if not include_all and kind != "ok":
            continue

        # 按房间 UUID 去重(真房 UUID 唯一; 重复上报的会被去重)
        rid = room.get("id") or ""
        if rid and rid in seen_id:
            continue
        if rid:
            seen_id.add(rid)
        rooms.append(_normalize_official(room))
    return rooms


def fetch_rooms(source: str = "all", include_all: bool = False,
                only_joinable: bool = False, keyword: str = "",
                status: str = "", mod_filter: str = "", full: str = "") -> list:
    """拉取并合并房间列表, 返回统一结构。

    source:        "all"(默认, Q优先+Official补充) / "qroom" / "official"
    include_all:   True 时保留全部条目(仅对 official 生效, 调试用)
    only_joinable: True 时只保留 status=battleroom(可加入)的房间
    keyword:       按 房主名/地图名/描述 模糊搜索(不区分大小写)
    status:        只保留指定 status(battleroom/ingame), 空表示不限
    mod_filter:    "mod"=只看模组房 / "vanilla"=只看原版房 / 空=不限
    full:          "hide"=隐藏人满 / "only"=只看人满 / 空=不限
    """
    rooms = []
    if source in ("all", "qroom"):
        rooms.extend(fetch_qroom_list())
    if source in ("all", "official"):
        rooms.extend(fetch_official_rooms(include_all=include_all))

    out = []
    for room in rooms:
        st = (room.get("status") or "").strip().lower()
        # 状态过滤
        if only_joinable and st != "battleroom":
            continue
        if status and st != status.lower():
            continue
        # 模组过滤
        if mod_filter == "mod" and not room.get("is_mod"):
            continue
        if mod_filter == "vanilla" and room.get("is_mod"):
            continue
        # 人满过滤
        is_full = (room.get("max") or 0) > 0 and (room.get("players") or 0) >= (room.get("max") or 0)
        if full == "hide" and is_full:
            continue
        if full == "only" and not is_full:
            continue
        # 关键词搜索
        if keyword:
            kw = keyword.lower()
            blob = " ".join([
                str(room.get("name") or ""),
                str(room.get("map") or ""),
                str(room.get("desc") or ""),
            ]).lower()
            if kw not in blob:
                continue
        out.append(room)

    # 排序: 有可查目标的排前面, 其次 房间中 > 游戏中, 再按人数降序
    # 注意: RELAY-CN 的 :5158 是 UPList 注册入口, 不是游戏端口, 直连查不到,
    # 这类地址不算"可查目标", 否则会把一屏假人数条目顶到最前面。
    def _has_target(r):
        addr = str(r.get("addr") or "")
        if r.get("room_id"):
            return True
        return bool(addr) and not addr.endswith(":5158")

    def _sort_key(r):
        return (0 if _has_target(r) else 1,
                0 if (r.get("status") or "").lower() == "battleroom" else 1,
                -(r.get("players") or 0))
    out.sort(key=_sort_key)
    return out


STATUS_CN = {"battleroom": "房间中", "ingame": "游戏中", "locked": "已锁定",
             "chat": "聊天", "unknown": "-"}


def _fmt_room_line(room: dict) -> str:
    """单行房间展示: 房主 | 地图 | 人数 | 状态 | 模组"""
    st = STATUS_CN.get((room.get("status") or "").lower(), room.get("status") or "-")
    pl = room.get("players", 0)
    mx = room.get("max", 0)
    mod = "模组" if room.get("is_mod") else "原版"
    desc = room.get("desc") or ""
    line = (f"{str(room.get('name') or '?')[:14]:<16}"
            f"{str(room.get('map') or '-')[:26]:<28}"
            f"{pl}/{mx:<7}{st:<7}{mod:<5}")
    if desc and desc != room.get("map"):
        line += desc[:24]
    return line


def room_list(source: str = "all", include_all: bool = False,
              only_joinable: bool = False, keyword: str = "",
              status: str = "", mod_filter: str = "", full: str = "") -> list:
    if not QUIET:
        print("[list] 获取房间列表...")
    rooms = fetch_rooms(source=source, include_all=include_all,
                        only_joinable=only_joinable, keyword=keyword,
                        status=status, mod_filter=mod_filter, full=full)
    if not QUIET:
        conds = []
        if only_joinable:
            conds.append("仅可加入")
        if status:
            conds.append("状态=" + status)
        if mod_filter:
            conds.append("模组=" + mod_filter)
        if full:
            conds.append("人满=" + full)
        if keyword:
            conds.append("关键词=" + keyword)
        print("筛选: " + (" / ".join(conds) if conds else "无"))
        print("-" * 100)
        print(f"{'房主':<16}{'地图':<28}{'人数':<7}{'状态':<7}{'类型':<5}描述")
        print("-" * 100)
        for room in rooms:
            print(_fmt_room_line(room))
        print("-" * 100)
        print(f"共 {len(rooms)} 个房间")
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
                    # 收集 mod 引入单位的显示名 (如"炮塔科技1.4") → mod 名来源
                    if dname:
                        d = dname.strip()
                        if d and d not in info['mod_names'] and d != unit_id:
                            info['mod_names'].append(d)
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

    mapname = info.get('mapname') or "(未知地图)"
    if mapname.endswith('.tmx'):
        mapname = mapname[:-4]
    mapname = _re.sub(r'^\[[^\]]*\]', '', mapname).strip()
    lines.append(f"地图名称：{mapname}")

    c = info.get('credits', 0)
    if c == 0:
        money_str = "4000¤"
    else:
        money_str = CREDITS_NAMES.get(c, f"${c}")
    mul = MULTIPLIER_NAMES.get(int((info.get('multiplier', 1.0) - 1.0) * 2.0), f"{info.get('multiplier', 1.0)}x")
    lines.append(f"初始资金：{money_str}[{mul}倍率]")

    lines.append(f"初始禁核：{'禁用核蛋蛋' if info.get('no_nukes', False) else '未禁用核蛋蛋'}")
    _fog = info.get('fog')
    lines.append("初始迷雾：" + (FOG_NAMES.get(_fog, '未知(' + str(_fog) + ')') if _fog is not None else "未知"))
    mode_str = {0: '遭遇战', 1: '自定义', 2: '存档'}.get(info.get('mode'), '未知(' + str(info.get('mode')) + ')')
    lines.append(f"地图模式：{mode_str}")
    # 默认单位: 客户端下拉 ae.d() = 1..4 + 可选起始单位(100+, RWPP isPickableStartingUnit)
    # 100+ 是 g 列表索引 (客户端 ae.d 100+ 部分), 不是服务器 custom_unit_list 顺序!
    # g: 100=experimentalDropship(飞行堡垒), 101=experimentalGunship(实验悬浮型气垫船), 102=experimentalSpider(实验型战斗蜘蛛), 103=modularSpider(模块蜘蛛)
    u = info.get('units', 0)
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
        m = _re.search(r'\((\d+)p\)', info.get('mapname', ''))
        if m:
            maxp = int(m.group(1))
        elif '10p' in info.get('mapname', ''):
            maxp = 10
    count = player_count if player_count is not None else len(players)
    lines.append(f"当前人数：{count}/{maxp if maxp else '?'}")

    # 显示 Mod 信息
    # 坑: customUnits 块与 mod 无关 —— 发送端 l.a(bg) 是**无条件**调用
    # (aj.smali:746), 遍历的是全局单位表 l.d, 任何房间(含原版)都会带
    # 121 个内置单位。所以:
    #   · cnt=121 不代表任何 mod
    #   · has_custom 也不代表有 mod
    # 只能靠单位显示名判断: mod 会带来"与单位ID不同"的显示名(如"炮塔科技1.4")。
    mod_names = info.get('mod_names', [])
    unit_count = info.get('custom_unit_count') or 0
    if mod_names:
        mod_str = ', '.join(f"[{m}]" for m in mod_names[:5])
        if len(mod_names) > 5:
            mod_str += f" 等{len(mod_names)}个"
        lines.append(f"使用Mod：{mod_str}（{unit_count} 个单位）")
    else:
        lines.append("使用Mod：原版")

    # 只展示房号类标识(Q57390 / r5132 / u_xxx), 不展示 IP 形式的目标地址
    _rid_s = str(rid or "").strip()
    if _rid_s and not re.fullmatch(r"[\d.]+:\d+", _rid_s):
        lines.append(f"ID：{_rid_s}")

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
                # AI/ID-only 标记移到槽位后、名字前: "B2 (AI): AI - Impossible"
                mark = ""
                if p.get('is_ai'):
                    mark = " (AI)"
                elif p.get('is_id_only'):
                    mark = " (ID-only)"
                name = p.get('name', '')
                if not name or all(not ch.isprintable() or ch.isspace() for ch in name):
                    name = "(未命名)"
                if p.get('is_ai'):
                    # 协议里 AI 名形如 "5号 - Hard"/"- Hard" (无 AI 字样), 显示时补前缀
                    # 只有纯 "- Hard" 补 AI, 带槽位前缀 "5号 - Hard" 保持原名
                    if name.startswith('- '):
                        name = f"AI {name}"
                ping = p.get('ping', None)
                if ping is None:
                    ping_str = ""
                elif ping == -2:
                    ping_str = " (断线)"
                elif ping == -1:
                    ping_str = " (超时)"
                else:
                    ping_str = f" (ping {ping}ms)"
                lines.append(f"  {label}{mark}: {name}{ping_str}")
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
    if not name:
        return False, False, False
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
                # 限制解压后大小, 防 gzip 炸弹 (用 zlib 底层, 兼容 3.11/3.12 及魔改环境)
                import zlib as _zlib
                _d = _zlib.decompressobj(16 + _zlib.MAX_WBITS)
                block_body = _d.decompress(block_body, 512 * 1024)
                block_body += _d.flush()
            except (OSError, EOFError, zlib.error):
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
        # RCN 服务器: 玩家记录之间以 ff ff d8 f1 分隔, 每条记录一段。
        # 段内可能含不定长 00 填充 + 真实记录。若整块逐字节滑动扫描,
        # 会误匹配填充里的"假记录头"(exists=1 但 name=None、team 错位),
        # 并因 o=o2+1 跳过真实记录 → 真人房主丢失(如 r12345 "一个无常人")。
        # 修复: 按分隔符切段, 每段只取"名字非空且字段合法"的真实记录
        # (填充误报的假记录 name=None 自动跳过), 每段至多一条。
        for seg in b.split(b'\xff\xff\xd8\xf1'):
            if not seg:
                continue
            n = len(seg)
            o = 0
            while o + 14 <= n:
                if seg[o] != 1:
                    o += 1
                    continue
                try:
                    is_ai = struct.unpack('>i', seg[o+1:o+5])[0]
                    if is_ai not in (0, 1):
                        o += 1
                        continue
                    p_l = seg[o+5]
                    p_cred = struct.unpack('>i', seg[o+6:o+10])[0]
                    p_s = struct.unpack('>i', seg[o+10:o+14])[0]
                    if not (0 <= p_s < team_count):
                        o += 1
                        continue
                    o2 = o + 14
                    name = None
                    if o2 < n and seg[o2] == 1:
                        ln = struct.unpack('>H', seg[o2+1:o2+3])[0]
                        if 0 < ln <= 64 and o2+3+ln <= n:
                            _nb = seg[o2+3:o2+3+ln]
                            # 与 NetReader.utf() 一致: 处理游戏端 UTF-16 代理对误当 UTF-8 的 Emoji
                            try:
                                name = _nb.decode('utf-8')
                            except UnicodeDecodeError:
                                name = _decode_lenient(_nb)
                            o2 += 3 + ln
                        else:
                            o += 1
                            continue
                    else:
                        o2 += 1
                    if o2 >= n:
                        o += 1
                        continue
                    X = seg[o2]
                    # 真记录: is_ai 合法 + credits 合理 + 名字可读
                    if p_cred <= 0 or X not in (0, 1):
                        o += 1
                        continue
                    is_real, is_ai_name, is_id_only = _classify_name(name or "")
                    # 只收名字非空的真实记录 (填充误报 name=None 被跳过)
                    if not is_real:
                        o += 1
                        continue
                    players.append({
                        'slot': p_l,
                        'team': p_s,
                        'num': p_l + 1,
                        'name': name,
                        'raw_name': name,
                        'is_ai': is_ai != 0 or is_ai_name,
                        'is_id_only': is_id_only,
                        'player_id': name if is_id_only else None,
                        'ping': None,
                        'exists': True,
                    })
                    break  # 一段只取一条记录
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


# bin 缓存目录: 固定为本脚本所在目录, 不依赖调用方的 cwd
# (插件用 cwd=ROOM_INFO_DIR 启动子进程, 但 --list 等模式可能在其他 cwd 下被调用)
BIN_DIR = os.path.dirname(os.path.abspath(__file__))


def cleanup_old_bin_files(max_age_seconds=3600, verbose=False):
    """删除 BIN_DIR 下超过 max_age_seconds 的 server_info_*.bin 缓存。

    返回 (deleted, kept) 两个计数，便于调用方记录日志或做自检。
    verbose=True 时无条件打印清理明细(命令行/定时任务用)。
    """
    deleted = kept = 0
    current_time = time.time()
    try:
        names = os.listdir(BIN_DIR)
    except OSError as e:
        if verbose or DEBUG_MODE:
            print(f"[清理] 无法列出目录 {BIN_DIR}: {e}")
        return deleted, kept
    for fname in names:
        if not (fname.startswith('server_info_') and fname.endswith('.bin')):
            continue
        fpath = os.path.join(BIN_DIR, fname)
        try:
            if current_time - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
                deleted += 1
                if verbose or DEBUG_MODE:
                    print(f"[清理] 删除过期文件: {fname}")
            else:
                kept += 1
        except OSError as e:
            # 单文件失败(被占用/权限)不影响其他文件
            if verbose or DEBUG_MODE:
                print(f"[清理] 跳过 {fname}: {e}")
    if verbose or DEBUG_MODE:
        print(f"[清理] 完成: 删除 {deleted} 个, 保留 {kept} 个 (阈值 {max_age_seconds}s)")
    return deleted, kept


def finish_report(sock, got_106, got_115, target):
    info = parse_106(got_106)
    players_detail = []
    players_names = []
    capacity = None
    if got_115:
        t = parse_115(got_115)
        players_detail = t.get('players', [])
        own_slot = t.get('own_player_id')
        players_detail = [p for p in players_detail
                          if p.get('exists') and p.get('name')
                          and (own_slot is None or p.get('slot') != own_slot)
                          and NAME not in p['name']]
        players_names = [p['name'] for p in players_detail if p.get('exists')]
        capacity = t.get('capacity') or t.get('team_count')
        current_players = len(players_detail)
    else:
        current_players = 0

    print()
    print(format_room(info, target, players_names, capacity, players_detail, _MAX_PLAYERS_DISPLAY, player_count=current_players, show_players=SHOW_PLAYERS))

    timestamp = int(time.time())
    safe_target = re.sub(r'[^\w\-]', '_', target)
    # 写入路径与 cleanup_old_bin_files 保持一致(固定 BIN_DIR), 避免 cwd 不同导致清理不到
    with open(os.path.join(BIN_DIR, f"server_info_106_{safe_target}_{timestamp}.bin"), "wb") as f:
        f.write(got_106)
    if got_115:
        with open(os.path.join(BIN_DIR, f"server_info_115_{safe_target}_{timestamp}.bin"), "wb") as f:
            f.write(got_115)
    if sock is not None:
        send_leave(sock)
        time.sleep(0.5)
        sock.close()


def direct_main():
    # 无端口时直接默认 5123, 避免 rpartition 把整个地址当端口
    if ":" in DIRECT_TARGET:
        ip, _, port = DIRECT_TARGET.rpartition(":")
        try:
            port = int(port or 5123)
        except ValueError:
            print(f"[房间查询失败] 端口无效: {DIRECT_TARGET}")
            sys.exit(0)
    else:
        ip, port = DIRECT_TARGET, 5123
    if not QUIET:
        print(f"=== 直连 {ip}:{port} (客户端 1.15) ===")

    # 统一走 node_session: 它会处理 161 之前的 178 跳转、151 挑战、106/115。
    # 早期这里另写了一套 connect_and_161 流程, 只认 161, 遇到节点先回 178
    # 就报 sid=None / 未收到106 —— 而同样的地址走 node_session 是能查到的。
    result = node_session(ip, port, None)
    if isinstance(result, tuple) and result and result[0] in ("KICKED", "FAILED", "LOOP"):
        kind, msg = result[0], str(result[1])
        if kind == "KICKED":
            _handle_kicked(msg)
        if not QUIET:
            print(f"!! {msg}")
        print(f"[房间查询失败] {msg}")
        sys.exit(1 if kind == "LOOP" else 0)
    if result is None:
        print("[房间查询失败] 房间不存在或已关闭")
        sys.exit(1)
    got_106, got_115 = result
    finish_report(None, got_106, got_115, DIRECT_TARGET)


def relay_host(rid: str) -> str:
    return rid[0].lower() + ".relay.corrodinggames.com"


def _follow_jump(addr, rid, depth, seen, tag=""):
    """处理 178 跳转地址: host:port/path 或 host:port。

    客户端 ae.java line 2155-2166: 含 '/' 时取 '/' 之后的部分作为**新房间号**,
    '/' 之前的部分作为 host:port 继续连接。
    """
    if not addr:
        if not QUIET:
            print(f"{tag}!! 178地址为空")
        return ("FAILED", "中继跳转地址为空，房间可能刚关闭")
    addr = re.sub(r'^\[(TCP|UDP)\]\s*', '', addr.strip())
    if not QUIET:
        print(f"{tag}[3] 收到178跳转: {addr!r}")
    if "/" in addr:
        hp, _, rid2 = addr.partition("/")
        rid2 = rid2.strip()
        if ":" in hp:
            h2, _, p2 = hp.rpartition(":")
            p2 = int(p2) if p2.isdigit() else 5123
        else:
            h2, p2 = hp, 5123
        if not QUIET:
            print(f"{tag}[3] 跳转到中继 {h2}:{p2} 房间 {rid2}")
        return relay_session(h2, p2, rid2, depth + 1, seen)
    if ":" in addr:
        h2, _, p2 = addr.rpartition(":")
        p2 = int(p2) if p2.isdigit() else 5123
    else:
        h2, p2 = addr, 5123
    if not QUIET:
        print(f"{tag}[3] 跳转到节点 {h2}:{p2}")
    return node_session(h2, p2, rid)


def relay_session(host, port, rid, depth=0, seen=None):
    tag = f"[{depth}]" if depth else ""
    # 环检测: 防止 178 跳转在 中继<->节点 之间来回死循环
    if seen is None:
        seen = set()
    node_key = (str(host).lower(), int(port), str(rid).lower())
    if node_key in seen:
        if not QUIET:
            print(f"{tag}!! 检测到跳转环路 {host}:{port} rid={rid}，已中止")
        return ("LOOP", f"跳转环路: {host}:{port}/{rid}")
    seen.add(node_key)
    if depth > 6:
        if not QUIET:
            print(f"{tag}!! 跳转层级过深({depth})，已中止")
        return ("LOOP", "跳转层级过深")
    if not QUIET:
        print(f"{tag}=== 中继会话 {rid} @ {host}:{port} (depth={depth}) ===")
    # 连接失败必须显式报告: 该首字母的中继未开通端口(c/g/j/v)时会在此抛出
    try:
        s, probe = connect_and_161(host, port, rid)
    except OSError as e:
        if not QUIET:
            print(f"{tag}!! 连接中继失败: {e}")
        return ("FAILED", f"无法连接中继 {host}:{port}（该首字母的中继未开放或网络不通）")
    except Exception as e:
        if not QUIET:
            print(f"{tag}!! 握手异常: {e}")
        return ("FAILED", f"中继握手异常: {type(e).__name__}")
    sid = probe.server_id
    if not QUIET:
        print(f"{tag}[1] 中继161: sid={sid!r} conn={probe.conn_id} chal={probe.challenge_y}")
    # 中继可能直接回 178 跳转(而不是先回 161): 房号存在时 q/s 这类 RWHPS 中继就是如此
    jump_now = getattr(probe, "jump_addr", None)
    if jump_now:
        s.close()
        return _follow_jump(jump_now, rid, depth, seen, tag)
    if not sid:
        if not QUIET:
            print(f"{tag}!! 握手失败，未收到房间服务器响应")
        s.close()
        return ("FAILED", f"中继 {host}:{port} 未响应握手（房间不存在或中继繁忙）")

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
            s.close()
            return _follow_jump(addr, rid, depth, seen, tag)
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
        elif mtype == 117:
            # 服务器提示消息: byte 1 + int32 类型 + utf(文本)
            hint = ""
            try:
                _r = NetReader(payload)
                _r.byte(); _r.int32()
                hint = _r.utf()
            except Exception:
                hint = ""
            if not QUIET:
                print(f"{tag}[117] {hint!r}")
            if hint:
                return ("KICKED", hint)
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
    # 静默模式也需要让调用方知道失败原因, 不能返回 None 让插件"无输出"
    return ("FAILED", f"未收到房间信息（中继 {host}:{port} 无响应）")


def _follow_node_jump(addr, rid, depth=0, seen=None):
    """跟随节点下发的 178 跳转(节点侧, 不带 161)。

    节点回包形如:
        [TCP]h1.example.com:5123/RA      -> host:port + 新房间号, 继续中继
        [TCP]1.2.3.4:5123                -> 纯地址, 继续连节点

    客户端(ae.java)收到 178 后一律关闭当前连接再连新地址, 不把它当错误。
    这里统一处理, 并做环检测与深度限制, 防止节点互相推诿造成死循环。
    """
    if seen is None:
        seen = set()
    if depth > 6:
        if not QUIET:
            print(f"!! 节点跳转层级过深({depth})")
        return ("LOOP", "节点跳转层级过深")
    raw = (addr or "").strip()
    if not raw:
        return ("FAILED", "节点跳转地址为空")
    raw = re.sub(r'^\[(TCP|UDP)\]\s*', '', raw)
    if not QUIET:
        print(f"[*] 跟随节点178: {raw!r} (depth={depth})")

    # 客户端 ae.smali 的地址归一化(必须照做, 否则短代号全部连不上):
    #   · 以 ".relay" 结尾            -> 补 ".corrodinggames.com"
    #   · 不含 : . / \ 且非 localhost -> "首字母.relay.corrodinggames.com/<原串>"
    # 例: "[TCP]RRELAY-CN" -> "R.relay.corrodinggames.com/RRELAY-CN"
    if raw.lower().endswith(".relay"):
        raw = raw + ".corrodinggames.com"
    elif (len(raw) > 1 and ":" not in raw and "." not in raw
          and "/" not in raw and "\\" not in raw and raw != "localhost"):
        raw = f"{raw[0]}.relay.corrodinggames.com/{raw}"
        if not QUIET:
            print(f"[*] 短代号转域名: {raw!r}")

    key = raw.lower()
    if key in seen:
        if not QUIET:
            print(f"!! 节点跳转环路 {raw}")
        return ("LOOP", f"节点跳转环路: {raw}")
    seen.add(key)

    if "/" in raw:
        hp, _, rid2 = raw.partition("/")
        rid2 = rid2.strip()
        if ":" in hp:
            h, _, p = hp.rpartition(":")
            p = int(p) if p.isdigit() else 5123
        else:
            h, p = hp, 5123
        return relay_session(h, p, rid2, depth + 1, set(seen))
    if ":" in raw:
        h, _, p = raw.rpartition(":")
        p = int(p) if p.isdigit() else 5123
    else:
        h, p = raw, 5123
    return node_session(h, p, rid, depth + 1, seen)


def node_session(host, port, rid=None, depth=0, seen=None):
    if not QUIET:
        print(f"[4] 连接节点: {host}:{port} (房间 {rid or '-'})")
    try:
        s2 = socket.create_connection((host, port), timeout=8)
        s2.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as e:
        if not QUIET:
            print(f"!! 节点连接失败: {e}")
        return ("FAILED", f"无法连接游戏节点 {host}:{port}")
    w = NetWriter()
    w.utf(PROTO_MAGIC)
    w.int32(4); w.int32(CLIENT_VER); w.int32(1)
    # 注意: 此处必须不带房间号。节点是按 178 分配到的专用节点,
    # 带 room_id 会触发 "绕过ID分配中心" 拒绝, 并导致 178 环路。
    w.nullable_str(None)
    w.utf(NAME); w.utf(NID); w.utf('')
    s2.sendall(pack_frame(160, w.output()))
    probe2 = RWProbe('x', 0, NAME, unit_checksum=CK or 0, client_version=CLIENT_VER, verbose=False)
    probe2.sock = s2
    probe2._wait_161()
    sid2 = probe2.server_id
    if not QUIET:
        print(f"[5] 节点161: sid={sid2!r} conn={probe2.conn_id}")
    # 有些节点在握手阶段就直接回 178(连 161 都不发): RELAY-CN / Tiexiu 的
    # 分配节点就是这样, 回包形如 "[TCP]h1.example:5123/RA"。客户端收到后
    # 会关掉连接继续跟随, 早期这里只认 161, 于是全部报"未响应握手"。
    jump_now = getattr(probe2, "jump_addr", None)
    if jump_now:
        s2.close()
        if not QUIET:
            print(f"[5] 节点握手即178: {jump_now!r}")
        return _follow_node_jump(jump_now, rid, 0)
    if not sid2:
        if not QUIET:
            print("!! 节点握手失败")
        s2.close()
        return ("FAILED", f"游戏节点 {host}:{port} 未响应握手")
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
        elif mtype == 117:
            # 节点拒绝: byte 1 + int32 类型 + utf(文本)
            hint = ""
            try:
                _r = NetReader(payload)
                _r.byte(); _r.int32()
                hint = _r.utf()
            except Exception:
                hint = ""
            if not QUIET:
                print(f"[7] 节点提示: {hint!r}")
            if hint:
                kick_reason = hint
                break
        elif mtype == 178:
            # 178 是跳转指令, 不是拒绝。节点把房间转挂到别处时会再下发一跳
            # (如 62.234.56.66:5126/RB), 客户端收到后关掉当前连接继续跟随。
            addr = probe2._handle_178(payload)
            if not QUIET:
                print(f"[6] 节点178跳转: {addr!r}")
            s2.close()
            return _follow_node_jump(addr, rid, depth + 1, seen)
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
    return ("FAILED", f"游戏节点 {host}:{port} 未返回房间信息")


def _handle_kicked(reason, sock=None):
    """统一处理被踢出房间的提示 (sock 非空时先关闭连接)"""
    reason = (reason or "").strip()
    if "No free slots" in reason:
        print("[房间查询失败] 房间已满，无法加入")
    elif "游戏已经开始" in reason or "game already started" in reason.lower():
        # RELAY-CN 走错房间(节点默认房)时也会返回这句, 需加提示
        print("[房间查询失败] 游戏已经开始，无法加入")
    elif "绕过ID分配中心" in reason:
        # 178 跳到节点后节点拒绝: 房间已迁移/需走分配中心, 探针无法直连
        print("[房间查询失败] 该房间当前无法直接查询（RELAY-CN 节点限制，请稍后重试或联系房主）")
    elif "找不到这个服务器" in reason or "cant find this server" in reason.lower():
        print("[房间查询失败] 房间不存在或已关闭")
    else:
        print(f"[房间查询失败] {reason}")
    if sock is not None:
        sock.close()
    sys.exit(0)

def relay_main():
    host = relay_host(RID)
    if not QUIET:
        print(f"=== 查询房间 {RID} (客户端 1.15) ===")
        try:
            print(f"[0] 中继: {host}:5123 (DNS={socket.gethostbyname(host)})")
        except OSError as e:
            print(f"[0] 中继: {host}:5123 (DNS解析失败: {e})")
    # 中继服务器不可达: 该首字母的中继未开通 (如 c/g/j/v 有域名但端口关闭)
    try:
        socket.gethostbyname(host)
    except OSError:
        print(f"[房间查询失败] 房号 {RID.upper()} 所属中继({host})不存在，请检查房号首字母")
        sys.exit(0)
    result = relay_session(host, 5123, RID)
    if isinstance(result, tuple) and result[0] == "KICKED":
        _handle_kicked(result[1])
    if isinstance(result, tuple) and result[0] == "LOOP":
        if not QUIET:
            print(f"!! {result[1]}")
        print("[房间查询失败] 房间查询链路异常（跳转环路），该房间当前无法查询")
        sys.exit(1)
    if isinstance(result, tuple) and result[0] == "FAILED":
        print(f"[房间查询失败] {result[1]}")
        sys.exit(1)
    if result is None:
        print("[房间查询失败] 房间不存在或已关闭")
        sys.exit(1)
    got_106, got_115 = result
    finish_report(None, got_106, got_115, RID)


def main():
    if CLEANUP_MODE:
        # 独立清理: 阈值可用 --max-age 秒数覆盖(默认 3600)
        max_age = 3600
        try:
            v = _find_arg("--max-age")
            if v:
                max_age = max(0, int(v))
        except (TypeError, ValueError):
            pass
        deleted, kept = cleanup_old_bin_files(max_age, verbose=True)
        print(f"[清理] 目录: {BIN_DIR}")
        sys.exit(0)
    cleanup_old_bin_files()
    if LIST_JSON:
        rooms = fetch_rooms(source=LIST_SOURCE, include_all=LIST_ALL,
                            only_joinable=LIST_JOINABLE, keyword=LIST_KEYWORD,
                            status=LIST_STATUS, mod_filter=LIST_MOD,
                            full=LIST_FULL)
        print(json.dumps(rooms, ensure_ascii=False, default=str))
        return
    if LIST_MODE:
        room_list(source=LIST_SOURCE, include_all=LIST_ALL,
                  only_joinable=LIST_JOINABLE, keyword=LIST_KEYWORD,
                  status=LIST_STATUS, mod_filter=LIST_MOD, full=LIST_FULL)
        return
    if not RID:
        print(HELP_TEXT)
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