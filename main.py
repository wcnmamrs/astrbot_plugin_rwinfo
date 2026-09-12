#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AstrBot 插件: 铁锈战争房间查询 (rwinfo)
"""
import asyncio
import logging
import os
import re
import sys
import time
import traceback

from astrbot.api.all import *
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOM_INFO_PY = os.path.join(_PLUGIN_DIR, "room_info.py")
if not os.path.exists(ROOM_INFO_PY):
    ROOM_INFO_PY = os.path.join(os.path.dirname(_PLUGIN_DIR), "room_info.py")
ROOM_INFO_DIR = os.path.dirname(ROOM_INFO_PY)

QUERY_TIMEOUT = 120
COOLDOWN = 10
PROBE_MIN, PROBE_MAX = 1, 99

CN_RE = re.compile(r'(?<![A-Za-z0-9])[rRsS]\d{3,}(?![A-Za-z0-9])')
INTL_RE = re.compile(r'(?<![A-Za-z])[hH][A-Za-z]{2,9}(?![A-Za-z])')


def extract_room_ids(text: str):
    ids = []
    seen = set()
    for m in CN_RE.finditer(text):
        rid = m.group(0).lower()
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    rest = CN_RE.sub(" ", text)
    for m in INTL_RE.finditer(rest):
        rid = m.group(0).lower()
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    return ids


def is_room_info(text: str) -> bool:
    return ("版本：" in text and "地图名称：" in text) or "SERVER_INFO" in text

def is_error_info(text: str) -> bool:
    return text.startswith("[房间查询失败]")

# 服务器错误信息 -> 中文翻译
ERROR_TRANSLATIONS = [
    ("No free slots", "房间已满，无法加入"),
    ("Room is locked", "房间已锁定"),
    ("游戏已经开始", "游戏已经开始"),
    ("game already started", "游戏已经开始"),
    ("Name Check", "探针名字被过滤"),
    ("您的名字被前置过滤拦截", "探针名字被前置过滤拦截"),
    ("banned", "已被封禁"),
    ("kicked", "被移出房间"),
]

def translate_error(text: str) -> str:
    """翻译服务器错误信息为中文 (匹配到关键词时替换整条错误句)"""
    for eng, zh in ERROR_TRANSLATIONS:
        if eng in text:
            # 替换从 [房间查询失败] 后的整段英文错误描述
            prefix = "[房间查询失败]"
            if text.startswith(prefix):
                rest = text[len(prefix):].strip()
                # 截取到句号/换行前 (只保留第一个错误句)
                for sep in (".", "\n"):
                    idx = rest.find(sep)
                    if idx != -1:
                        rest = rest[:idx]
                        break
                return f"{prefix} {zh}"
            return text.replace(eng, zh)
    return text

def is_name_check_error(text: str) -> bool:
    """判断是否为探针名字被过滤错误 (可重试)"""
    return "Name Check" in text or "前置过滤" in text or "名字被" in text


def normalize_gid(gid) -> str:
    if gid is None:
        return ""
    s = str(gid)
    m = re.search(r"\d+", s)
    return m.group(0) if m else s


@register("rwinfo", "Operit", "铁锈战争房间查询: 自动识别房号并安排探针查房", "1.0.0")
class RWInfoPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.logger = logger
        self.config = config or self._default_config()
        for k, v in self._default_config().items():
            if k not in self.config:
                self.config[k] = v

        if self.config.get("debug", False):
            self.logger.setLevel(logging.DEBUG)
            self.logger.debug("Debug 模式已启用")
        else:
            self.logger.setLevel(logging.INFO)

        self.room_cooldown = {}
        self.lock = asyncio.Lock()
        self.probe_used = set()
        self.probe_released = []
        self.probe_next = PROBE_MIN
        self.pending_tasks = set()

    def _default_config(self) -> dict:
        return {
            "global_enabled": True,
            "mode": "black",
            "white_list": [],
            "black_list": [],
            "debug": False,
            "max_players_display": 10,
            "show_players": False,
            "retry_times": 3,
        }

    def _save_config(self):
        try:
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            else:
                self.logger.warning("配置对象不支持 save_config 方法，请检查 AstrBot 版本")
        except Exception as e:
            self.logger.warning(f"保存配置失败: {e}")

    async def _acquire_probe(self) -> int:
        while True:
            async with self.lock:
                if self.probe_released:
                    idx = min(self.probe_released)
                    self.probe_released.remove(idx)
                    self.probe_used.add(idx)
                    return idx
                if self.probe_next <= PROBE_MAX:
                    idx = self.probe_next
                    self.probe_next += 1
                    self.probe_used.add(idx)
                    return idx
            await asyncio.sleep(1)

    async def _release_probe(self, idx: int):
        async with self.lock:
            self.probe_used.discard(idx)
            if idx not in self.probe_released:
                self.probe_released.append(idx)

    async def _query_room(self, rid: str, probe_name: str) -> str:
        if not os.path.exists(ROOM_INFO_PY):
            return f"[rwinfo] 找不到探针脚本: {ROOM_INFO_PY}"
        python = sys.executable
        if not python or not os.path.exists(python):
            python = "python3"
        cmd_args = [
            python,
            ROOM_INFO_PY,
            rid,
            "--name", probe_name,
            "--max-players", str(self.config.get("max_players_display", 10))
        ]
        if self.config.get("show_players", False):
            cmd_args.append("--show-players")
        if self.config.get("debug", False):
            cmd_args.append("--debug")
        self.logger.debug(f"[DBG] _query_room 调用: rid={rid} name={probe_name}")
        self.logger.debug(f"[DBG] python={python} exists={os.path.exists(python) if python else False}")
        self.logger.debug(f"[DBG] ROOM_INFO_PY={ROOM_INFO_PY} exists={os.path.exists(ROOM_INFO_PY)} cwd={ROOM_INFO_DIR}")
        self.logger.debug(f"[DBG] cmd_args={cmd_args}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ROOM_INFO_DIR,
            )
        except Exception as e:
            self.logger.error(f"启动探针失败 (python={python}): {e}")
            return f"[房间查询失败] 探针启动失败: {e}"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return f"[rwinfo] 查询超时(>{QUERY_TIMEOUT}s): {rid}"
        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()
        rc = proc.returncode
        self.logger.debug(f"[DBG] 子进程退出码={rc} stdout长度={len(out)} stderr长度={len(err)}")
        if out:
            self.logger.debug(f"[DBG] stdout 前200字: {out[:200]!r}")
        if err:
            self.logger.debug(f"[DBG] stderr 前500字: {err[:500]!r}")
        if not out:
            # 无输出 = 房间不存在/英文单词误报, 静默丢弃 (避免国际房批量尝试刷屏)
            self.logger.debug(f"查询 {rid} 无输出, 静默 (python={python} rc={rc}) stderr: {err[:300]}")
            return ""
        return out

    def _is_allowed(self, gid: str) -> bool:
        if not self.config.get("global_enabled", True):
            return False
        mode = self.config.get("mode", "black")
        if mode == "white":
            return gid in self.config.get("white_list", [])
        elif mode == "black":
            return gid not in self.config.get("black_list", [])
        return True

    # ---- 指令 (全部要求管理员权限) ----
    @filter.command("铁锈全局解析")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_global(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        if not arg:
            state = "开启" if self.config.get("global_enabled", True) else "关闭"
            yield event.plain_result(f"全局解析当前状态：{state}")
            return
        if arg not in ("开", "关"):
            yield event.plain_result("用法: /铁锈全局解析 开|关")
            return
        self.config["global_enabled"] = (arg == "开")
        self._save_config()
        yield event.plain_result(f"全局解析已{'开启' if self.config['global_enabled'] else '关闭'}")

    @filter.command("铁锈模式")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_mode(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        if not arg:
            mode = self.config.get("mode", "black")
            mode_name = "白名单(仅白名单群可用)" if mode == "white" else "黑名单(黑名单群禁用)"
            yield event.plain_result(f"当前模式：{mode_name}")
            return
        if arg not in ("白", "黑"):
            yield event.plain_result("用法: /铁锈模式 白|黑")
            return
        self.config["mode"] = "white" if arg == "白" else "black"
        self._save_config()
        mode_name = "白名单(仅白名单群可用)" if self.config["mode"] == "white" else "黑名单(黑名单群禁用)"
        yield event.plain_result(f"当前模式已切换为：{mode_name}")

    @filter.command("铁锈玩家列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_show_players(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        if not arg:
            state = "开启" if self.config.get("show_players", False) else "关闭"
            yield event.plain_result(f"玩家列表显示当前状态：{state}")
            return
        if arg not in ("开", "关"):
            yield event.plain_result("用法: /铁锈玩家列表 开|关")
            return
        self.config["show_players"] = (arg == "开")
        self._save_config()
        yield event.plain_result(f"玩家列表显示已{'开启' if self.config['show_players'] else '关闭'}")

    @filter.command("铁锈白名")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_white(self, event: AstrMessageEvent, arg: str = ""):
        text = self._cmd_list_text(arg, "white_list", "白名单")
        yield event.plain_result(text)
        self._save_config()

    @filter.command("铁锈黑名")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_black(self, event: AstrMessageEvent, arg: str = ""):
        text = self._cmd_list_text(arg, "black_list", "黑名单")
        yield event.plain_result(text)
        self._save_config()

    @filter.command("铁锈重试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_retry(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        if not arg:
            n = self.config.get("retry_times", 3)
            yield event.plain_result(f"探针名被过滤时自动重试次数：{n}")
            return
        if not arg.isdigit() or int(arg) < 0 or int(arg) > 10:
            yield event.plain_result("用法: /铁锈重试 [0-10]  (无参数查看当前配置)")
            return
        self.config["retry_times"] = int(arg)
        self._save_config()
        yield event.plain_result(f"重试次数已设置为：{self.config['retry_times']}")

    def _cmd_list_text(self, arg, key, label) -> str:
        arg = (arg or "").strip()
        lst = self.config.get(key, [])
        if not arg:
            if not lst:
                return f"{label}为空"
            return f"{label}内群聊: {', '.join(lst)}"
        if arg.startswith("+"):
            gid = normalize_gid(arg[1:])
            if gid and gid not in lst:
                lst.append(gid)
                self._save_config()
                return f"已加入{label}: {gid}"
            return f"{label}中已存在: {gid}"
        elif arg.startswith("-"):
            gid = normalize_gid(arg[1:])
            if gid in lst:
                lst.remove(gid)
                self._save_config()
                return f"已移出{label}: {gid}"
            return f"{label}中不存在: {gid}"
        return f"用法: /{label} [+群号|-群号] (无参数列出)"

    # ---- 消息监听 ----
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        try:
            text = event.message_str or ""
            if not text.strip():
                return
            if text.strip().startswith("/"):
                return

            gid = normalize_gid(event.get_group_id())
            if not gid:
                alt = getattr(event, 'unified_msg_origin', None) or getattr(event, 'session_id', '')
                gid = normalize_gid(alt)
                if not gid:
                    return

            if not self._is_allowed(gid):
                return

            rids = extract_room_ids(text)
            if not rids:
                return

            self.logger.debug(f"群 {gid} 提取到房号: {rids}")

            now = time.time()
            to_query = []
            for rid in rids:
                last = self.room_cooldown.get(rid, 0)
                if now - last >= COOLDOWN:
                    self.room_cooldown[rid] = now
                    to_query.append(rid)
                if len(to_query) >= 5:
                    break
            if not to_query:
                self.logger.debug("所有房号均在冷却中")
                return

            self.logger.debug(f"待查询房号: {to_query}")

            tasks = []
            try:
                for rid in to_query:
                    idx = await self._acquire_probe()
                    base_name = f"ABAB探针{idx:02d}"
                    probe_name = f"{base_name}_{int(time.time())}"
                    task = asyncio.create_task(self._query_room(rid, probe_name))
                    tasks.append((rid, idx, task))

                remaining = tasks.copy()
                while remaining:
                    done, _ = await asyncio.wait(
                        [t[2] for t in remaining],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in remaining[:]:
                        if t[2] in done:
                            rid, idx, task = t
                            try:
                                result = task.result()
                            except Exception as e:
                                self.logger.error(f"查询 {rid} 异常: {e}")
                                result = ""
                            if is_room_info(result):
                                self.logger.info(f"房间 {rid} 查询成功, 回传")
                                if len(result) > 3500:
                                    result = result[:3500] + "\n...(已截断)"
                                yield event.plain_result(result)
                            elif is_name_check_error(result):
                                # 探针名被过滤: 自动改名重试
                                max_retry = int(self.config.get("retry_times", 3))
                                retried = 0
                                while retried < max_retry:
                                    retried += 1
                                    yield event.plain_result(f"[房间查询] 重试中({retried}/{max_retry})")
                                    new_name = f"{base_name}_{int(time.time())}_{retried}"
                                    retry_task = asyncio.create_task(self._query_room(rid, new_name))
                                    try:
                                        retry_result = await asyncio.wait_for(retry_task, timeout=QUERY_TIMEOUT)
                                    except asyncio.TimeoutError:
                                        retry_task.cancel()
                                        retry_result = ""
                                    if is_room_info(retry_result):
                                        if len(retry_result) > 3500:
                                            retry_result = retry_result[:3500] + "\n...(已截断)"
                                        yield event.plain_result(retry_result)
                                        break
                                    if not is_name_check_error(retry_result):
                                        if is_error_info(retry_result):
                                            yield event.plain_result(translate_error(retry_result))
                                        break
                                else:
                                    yield event.plain_result(translate_error(result))
                            elif is_error_info(result):
                                self.logger.info(f"房间 {rid} 查询失败，回传原因")
                                yield event.plain_result(translate_error(result))
                            else:
                                self.logger.debug(f"房间 {rid} 无有效信息, 静默丢弃")
                            await self._release_probe(idx)
                            remaining.remove(t)
                            break
            finally:
                pending_idx = {t[1] for t in remaining}
                for rid, idx, task in tasks:
                    if idx in pending_idx:
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass
                        await self._release_probe(idx)
        except Exception as e:
            self.logger.error(f"rwinfo on_all_message error: {e}\n{traceback.format_exc()}")