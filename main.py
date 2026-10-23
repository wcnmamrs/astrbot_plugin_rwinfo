#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AstrBot 插件: 铁锈战争房间查询 (rwinfo)
"""
import asyncio
import hashlib
import json
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

# 插件版本(菜单显示与 @register 共用, 避免两处不一致)
PLUGIN_VERSION = "1.5.7"

# ================================================================ 房号识别
# 铁锈客户端构造中继连接串的逻辑 (ae.smali line 7107-7180):
#   若连接串不含 ':' '.' '/' '\' 且 != "localhost" 且长度>0
#   → 视为纯房号, 转换成 "<首字母>.relay.corrodinggames.com/<房号>"
#   即客户端只取首字符当主机名, 对房号字符本身无限制。
#
# 实测可用的中继首字母 (5123 端口开放):
#   a b h  → 官方中继 (Relay US1 / US2 / ASIA2)
#   o q r s→ 第三方中继 (RELAY-CN / Tiexiu.xyz / Fancy-CN)
#   c g j v→ 有 DNS 但端口未开放; 其余字母无 DNS
#
# 房号形态按中继区分(实测):
#   o q r s   → 数字房, 如 r5132 / s1024 / o9999 / q7777
#   其他字母   → 字母房, 如 hytvvi / aabcde / babc
# 两种形态统一限制: 整串长度 >= 3。其他形式(纯数字 / 混合 / IP)一律不识别。
# 注意: 不把首字母写死成白名单, 因为官方随时可能开通新中继;
#       未开通的字母查询时会快速失败(连接错误), 不影响正常房间。
NUM_RELAY_LETTERS = "oqrs"

# 数字房号: oqrs 开头 + 2 位以上数字 (整串 >= 3 字符)
#   o12 / q99 / r5132 / s1024
RELAY_NUM_RE = re.compile(r'(?<![A-Za-z0-9])[oqrsOQRS]\d{2,}(?![A-Za-z0-9])')
# 字母房号: 非 oqrs 的字母开头 + 纯字母 (整串 >= 3 字符)
#   hab / abc / hytvvi / aabcde
# 额外排除 URL / 域名片段: 紧邻 . / : @ = 的纯字母串不是房号
#   如 https://example.com 里的 https / example / com 都不匹配
RELAY_ALPHA_RE = re.compile(
    r'(?<![A-Za-z0-9._:/@=])(?![oqrsOQRS])[A-Za-z]{3,}(?![A-Za-z0-9._:/@=])')

# 不识别纯数字房号(如 5232): 首字符为数字的中继主机无 DNS, 无法查询。
# 不识别混合格式(如 a1b2c3): 不在实测的两种形态内, 排除以防误报。
# 不识别 IP 直连: 自动触发时不连 IP(无法判断是 Web 还是游戏服务器);
#   仅 /铁锈查房 显式指定 IP 时才允许, 由 IP_QUERY_RE 匹配。
# 不设英文单词黑名单: 字母房号随机, 可能恰好是 "hello" / "game";
#   黑名单会误杀真实房间, 合法性判定交给探针(查不到自然失败)。

# 显式查房时允许的 IP / IP:端口 形式(自动触发不使用)
IP_QUERY_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?$')


def extract_room_ids(text: str):
    """从文本中提取所有房间号, 按出现顺序去重返回(小写)。

    支持的房号形式(按中继区分, 实测):
      1. 数字房: o/q/r/s + 2 位以上数字, 整串 >= 3 字符
         如 r5132 / s1024 / o9999 / q7777
      2. 字母房: 非 o/q/r/s 的字母开头 + 纯字母, 整串 >= 3 字符
         如 hytvvi / aabcde / babc / cabcde

    不识别: 纯数字 / 混合格式(a1b2c3) / IP / u_ 前缀 / URL 片段。
    不做英文单词过滤: 字母房号随机, 可能恰好是 hello/game,
    合法性由探针验证(查不到即失败, 不影响正常房间)。
    """
    ids = []
    seen = set()
    rest = text
    # 1) 先匹配数字房(oqrs + 数字, 结构最明确)
    for m in RELAY_NUM_RE.finditer(rest):
        rid = m.group(0).lower()
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    rest = RELAY_NUM_RE.sub(" ", rest)
    # 2) 再匹配字母房(非 oqrs 开头的纯字母)
    for m in RELAY_ALPHA_RE.finditer(rest):
        rid = m.group(0).lower()
        if rid in seen:
            continue
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


# 只有"房间确实存在、用户会关心"的失败原因才回传;
# 其余(房间不存在 / 中继未响应 / 握手失败 / 节点限制 等)一律静默。
# 设计核心: 自动触发时房号可能是英文单词误报, 大量失败绝不能刷屏。
REPORTABLE_ERRORS = (
    "房间已满", "房间已锁定", "游戏已经开始",
    "已被封禁", "被移出房间",
)

def is_reportable_error(text: str) -> bool:
    """该失败原因是否值得回传到群里(仅房间确实存在时)。"""
    if not is_error_info(text):
        return False
    msg = translate_error(text)
    return any(k in msg for k in REPORTABLE_ERRORS)


def normalize_gid(gid) -> str:
    if gid is None:
        return ""
    s = str(gid)
    m = re.search(r"\d+", s)
    return m.group(0) if m else s


@register("rwinfo", "Operit", "铁锈战争房间查询: 自动识别房号并安排探针查房", PLUGIN_VERSION)
class RWInfoPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.logger = logger
        self.config = config or self._default_config()
        for k, v in self._default_config().items():
            if k not in self.config:
                self.config[k] = v
        # 旧配置迁移: 无前缀的纯数字名单条目统一转成 #群号 (兼容 v1.2.9 及更早)
        self._migrate_list("白名单")
        self._migrate_list("黑名单")
        self._migrate_list("混合_白名单")
        self._migrate_list("混合_黑名单")

        if self.config.get("debug", False):
            self.logger.setLevel(logging.DEBUG)
            self.logger.debug("Debug 模式已启用")
        else:
            self.logger.setLevel(logging.INFO)

        self.room_cooldown = {}
        self._last_cleanup = None
        self.probe_used = set()
        self.probe_released = []
        self.probe_next = PROBE_MIN
        self.pending_tasks = set()
        self.pending_recall_tasks = set()
        # bin 缓存定时清理任务(插件启动后开始循环)
        self._bin_cleanup_task = None
        self._start_bin_cleanup()

    # ================================================================ bin 缓存清理
    def _bin_cleanup_conf(self):
        """读取并矫正 bin 清理配置, 返回 (enabled, interval_sec, max_age_sec)."""
        enabled = bool(self.config.get("bin_cleanup_enabled", True))
        try:
            interval_h = float(self.config.get("bin_cleanup_interval_hours", 24))
        except (TypeError, ValueError):
            interval_h = 24.0
        try:
            max_age_h = float(self.config.get("bin_cleanup_max_age_hours", 1))
        except (TypeError, ValueError):
            max_age_h = 1.0
        # 下限保护: 间隔至少 60s(避免配置成 0 后疯狂跑), 缓存年龄至少 60s
        interval_sec = max(60.0, interval_h * 3600.0)
        max_age_sec = max(60.0, max_age_h * 3600.0)
        return enabled, interval_sec, max_age_sec

    def _start_bin_cleanup(self):
        """插件启动时启动 bin 清理循环(需在事件循环内创建任务)."""
        enabled, interval_sec, _ = self._bin_cleanup_conf()
        if not enabled:
            self.logger.info("[清理] bin 定时清理已在配置中关闭")
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("event loop closed")
            self._bin_cleanup_task = asyncio.create_task(self._bin_cleanup_loop())
        except RuntimeError as e:
            # __init__ 可能早于事件循环启动; 退化为首次消息时惰性启动
            self.logger.debug(f"[清理] 事件循环不可用, 改为惰性启动: {e}")
            self._bin_cleanup_task = None
            return
        self.logger.info(f"[清理] bin 缓存定时清理已启动: 每 {interval_sec / 3600:.2f} 小时检查一次")

    async def _bin_cleanup_loop(self):
        """循环: 从插件启动起计时, 每隔 interval 清理一次(启动瞬间不清理)。"""
        while True:
            try:
                _, interval_sec, _ = self._bin_cleanup_conf()
                await asyncio.sleep(interval_sec)
                enabled, _, max_age_sec = self._bin_cleanup_conf()
                if not enabled:
                    self.logger.debug("[清理] 配置已关闭, 清理循环退出")
                    return
                await self._run_bin_cleanup(max_age_sec)
            except asyncio.CancelledError:
                self.logger.debug("[清理] 定时任务被取消")
                raise
            except Exception as e:
                # 单次失败不应终止循环, 等下一轮
                self.logger.warning(f"[清理] 本轮清理异常, 将在下轮重试: {e}")
                await asyncio.sleep(60)

    async def _run_bin_cleanup(self, max_age_sec: float):
        """调用 room_info.py --cleanup-bins 执行实际清理(独立进程, 不阻塞事件循环)."""
        if not os.path.exists(ROOM_INFO_PY):
            return
        python = sys.executable
        if not python or not os.path.exists(python):
            python = "python3"
        cmd = [python, "-B", ROOM_INFO_PY, "--cleanup-bins",
               "--max-age", str(int(max_age_sec))]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ROOM_INFO_DIR,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
                self.logger.warning("[清理] 清理子进程超时(>60s), 已终止")
                return
        except Exception as e:
            self.logger.warning(f"[清理] 启动清理子进程失败: {e}")
            return
        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()
        # 只把「删除/完成」这类结果行写进日志, 避免 --cleanup 帮助文本刷屏
        for line in out.splitlines():
            if line.startswith("[清理]"):
                self.logger.info(line)
        if proc.returncode not in (0, None):
            self.logger.warning(f"[清理] 子进程退出码={proc.returncode} stderr={err[:300]}")
        if self.config.get("debug", False) and out:
            self.logger.debug(f"[清理] 完整输出: {out[:800]}")

    def _ensure_bin_cleanup(self):
        """惰性启动: __init__ 时事件循环不可用则在首条消息补启。"""
        if self._bin_cleanup_task is not None:
            return
        enabled, _, _ = self._bin_cleanup_conf()
        if enabled:
            self._start_bin_cleanup()

    def _default_config(self) -> dict:
        return {
            "global_enabled": True,           # 全局开关：关闭后所有查房都被拦截
            "mode": "black",                  # 模式：black=黑名单放行 / white=白名单放行 / mixed=混合模式(@优先)
            "白名单": [],                     # 白名单列表（black/white模式下使用）
            "黑名单": [],                     # 黑名单列表（black/white模式下使用）
            "混合_白名单": [],                # 混合模式专用白名单（@用户/#群 独立管理）
            "混合_黑名单": [],                # 混合模式专用黑名单（@用户/#群 独立管理）
            "混合_私聊策略": "white",         # 混合模式私聊默认策略：white=不在白名单拦 / black=在黑名单拦
            "私聊启用": True,                 # 是否处理私聊消息（关闭则私聊不发房号）
            "debug": False,
            "max_players_display": 10,
            "show_players": False,
            "retry_times": 3,
            # ---- 房间列表 ----
            "list_max_display": 20,           # 列表指令最多展示的房间数(0=不限)
            "list_page_size": 5,              # 列表指令每页显示数量
            "list_default_full": "",          # 列表默认人数筛选: 空=全部 / hide=隐藏人满
            "probe_max_per_msg": 5,           # 单条消息最多召唤的探针数
            # ---- bin 缓存清理 ----
            "bin_cleanup_enabled": True,      # 是否在插件启动后定时清理 bin 缓存
            "bin_cleanup_interval_hours": 24, # 清理间隔(小时), 默认一天一次
            "bin_cleanup_max_age_hours": 1,   # 只清理超过该小时数(默认1小时)的 bin
            "群撤回": [],                     # 按群独立的撤回配置: 每条 "#群号:开:延迟" 例 "#123456:1:60"
            "私聊撤回": [],                   # 按用户独立的撤回配置: 每条 "@用户ID:开:延迟" 例 "@3856554669:1:60"
        }

    def _save_config(self):
        try:
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            else:
                self.logger.warning("配置对象不支持 save_config 方法，请检查 AstrBot 版本")
        except Exception as e:
            self.logger.warning(f"保存配置失败: {e}")

    def _migrate_list(self, key: str):
        """旧配置迁移: 无前缀的纯数字条目转成 #群号; 清理 @@/##/混合前缀的脏数据."""
        lst = self.config.get(key, [])
        changed = False
        for i, item in enumerate(lst):
            if isinstance(item, (int, float)):
                lst[i] = f"#{int(item)}"
                changed = True
            elif isinstance(item, str):
                s = item
                # 清理重复/混合前缀: @@3245 -> @3245, ##123 -> #123, #@/ @# -> 去重为单个前缀
                while len(s) >= 2 and s[0] in ("@", "#") and s[1] in ("@", "#"):
                    s = s[0] + s[1:].lstrip("@#")
                if not s:
                    continue
                if s[0] not in ("@", "#"):
                    if s.isdigit():
                        s = "#" + s
                        changed = True
                if s != item:
                    lst[i] = s
                    changed = True
        if changed:
            self._save_config()

    def _recall_session_key(self, event):
        """返回 (会话key, 是否私聊). 群聊=#群号, 私聊=@用户ID(与黑白名单前缀一致)."""
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        gid = normalize_gid(event.get_group_id())
        uid = normalize_gid(event.get_sender_id())
        if is_private or not gid:
            return (f"@{uid}" if uid else "", True)
        return (f"#{gid}", False)

    def _recall_store(self, is_private: bool) -> list:
        """取出群/私聊撤回配置列表(容器)."""
        store = self.config.get("私聊撤回" if is_private else "群撤回", [])
        return store if isinstance(store, list) else []

    def _recall_parse(self, entry) -> dict:
        """解析单条 'key:开:延迟' -> {key, 开, 延迟}. 容错: 缺省 开=False, 延迟=60."""
        s = str(entry).strip()
        parts = s.split(":")
        key = parts[0].strip() if parts else ""
        on = False
        delay = 60
        if len(parts) >= 2:
            # 布尔值大小写不敏感: 1/开/true/on 任意大小写都算开启
            on = parts[1].strip().lower() in ("1", "开", "true", "on", "yes", "y")
        if len(parts) >= 3:
            raw = parts[2].strip()
            try:
                # 允许负数/超界输入, 统一夹到 0-300(负数此前会被静默丢弃)
                delay = max(0, min(300, int(raw)))
            except (TypeError, ValueError):
                delay = 60
        return {"key": key, "开": on, "延迟": delay}

    def _get_recall_cfg(self, event, want_wildcard=False) -> dict:
        """读取当前会话的撤回配置. want_wildcard=True 时只读 '*' 那条(用于 /铁锈撤回 * 查询).
        否则: 精确匹配优先, 群聊其次回退通配符 '*', 私聊不使用通配符.
        缺省 {开: False, 延迟: 60}."""
        key, is_private = self._recall_session_key(event)
        if want_wildcard:
            # 显式要求读通配符: 直接取 '*', 找不到返回缺省
            for entry in self._recall_store(is_private):
                cfg = self._recall_parse(entry)
                if cfg["key"] == "*":
                    return {"开": cfg["开"], "延迟": cfg["延迟"]}
            return {"开": False, "延迟": 60}
        wildcard = None
        for entry in self._recall_store(is_private):
            cfg = self._recall_parse(entry)
            if cfg["key"] == key:
                return {"开": cfg["开"], "延迟": cfg["延迟"]}
            # 通配符仅对群聊生效, 私聊忽略(防止普通用户设 '*' 越权影响他人)
            if cfg["key"] == "*" and not is_private:
                wildcard = {"开": cfg["开"], "延迟": cfg["延迟"]}
        if wildcard is not None:
            return wildcard
        return {"开": False, "延迟": 60}

    def _set_recall_cfg(self, event, on=None, delay=None, wildcard=False):
        """写入撤回配置并持久化. wildcard=True 时写入 '*' (仅群聊生效).
        私聊强制忽略通配符(防止普通用户越权). 仅更新传入字段, 另一字段沿用现有值."""
        key, is_private = self._recall_session_key(event)
        if wildcard and not is_private:
            key = "*"
        if not key:
            return
        store_key = "私聊撤回" if is_private else "群撤回"
        store = self.config.get(store_key)
        if not isinstance(store, list):
            store = []
            self.config[store_key] = store
        # 读取现有值
        cur = {"开": False, "延迟": 60}
        idx = None
        for i, entry in enumerate(store):
            cfg = self._recall_parse(entry)
            if cfg["key"] == key:
                cur = {"开": cfg["开"], "延迟": cfg["延迟"]}
                idx = i
                break
        if on is not None:
            cur["开"] = bool(on)
        if delay is not None:
            cur["延迟"] = max(0, min(300, int(delay)))
        new_entry = f"{key}:{1 if cur['开'] else 0}:{cur['延迟']}"
        if idx is None:
            store.append(new_entry)
        else:
            store[idx] = new_entry
        self._save_config()

    def _raw_args(self, event, arg: str, *cmd_names: str) -> str:
        """从原始消息中取出完整参数.

        AstrBot 只把第一个空格分隔的参数传给 arg, 后续会丢失
        (如 "/铁锈列表 搜索 无常人" 的 arg 只有 "搜索")。
        因此这里按命令名在原始文本里定位，取其后全部内容。取不到时回退 arg。
        """
        try:
            text = (event.get_message_str() or "").strip()
        except Exception:
            text = ""
        if text:
            for name in cmd_names:
                if not name:
                    continue
                # 允许 /铁锈列表、>铁锈列表、@机器人 /铁锈列表 等前缀
                m = re.search(re.escape(name), text)
                if m:
                    rest = text[m.end():].strip()
                    # 去掉可能残留的前导分隔符
                    rest = rest.lstrip(":：,，")
                    if rest:
                        return rest.strip()
            # 命令名后没有参数, 但 arg 有值(少见), 回退
        return (arg or "").strip()

    def _recall_full_args(self, event, arg: str) -> str:
        """撤回指令的完整参数 (兼容 /铁锈撤回 与 /rwinfo撤回)."""
        return self._raw_args(event, arg, "铁锈撤回", "rwinfo撤回", "撤回")

    async def _send_room_result(self, event, result: str):
        """发送房间查询结果(可撤回). 处理超长截断."""
        if len(result) > 3500:
            result = result[:3500] + "\n...(已截断)"
        await self._send_recallable(event, result)

    async def _send_recallable(self, event, text: str):
        """发送一条"可撤回"的消息. 返回 message_id; 若平台不支持则返回 None.
        发送后若开启自动撤回, 则安排延迟撤回任务."""
        text = text.strip()
        if not text:
            return None
        rcfg = self._get_recall_cfg(event)
        if not rcfg["开"]:
            # 未开启自动撤回: 用原生发送, 不关心 message_id
            try:
                await event.send(event.plain_result(text))
            except Exception as e:
                self.logger.warning(f"发送消息失败: {e}")
            return None
        # 开启自动撤回: 用 bot.send_msg 发送以拿 message_id (wait_recall 是特定适配器扩展, 非核心 API)
        try:
            gid = event.get_group_id()
            if gid:
                result = await event.bot.send_msg(
                    message_type="group",
                    group_id=int(gid),
                    message=text,
                )
            else:
                # 私聊/直聊: 用 user_id
                result = await event.bot.send_msg(
                    message_type="private",
                    user_id=int(event.get_sender_id()),
                    message=text,
                )
            mid = None
            if isinstance(result, dict):
                mid = result.get("message_id")
            elif result is not None:
                mid = getattr(result, "message_id", None)
        except Exception as e:
            self.logger.warning(f"发送可撤回消息失败: {e}")
            # 发送失败: 回退原生发送, 保证消息能发出
            try:
                await event.send(event.plain_result(text))
            except Exception:
                pass
            return None
        if not mid:
            self.logger.debug("未获取到 message_id, 不安排撤回")
            return None
        delay = int(rcfg["延迟"])
        # delay=0 表示立即撤回; 其余为延迟秒数。两者都要安排任务, 否则设 0 会永不撤回。
        task = asyncio.create_task(self._delayed_recall(event, mid, max(0, delay)))
        self.pending_recall_tasks.add(task)
        task.add_done_callback(self.pending_recall_tasks.discard)
        self.logger.debug(f"[撤回] 已安排撤回 mid={mid} delay={delay}s")
        return mid

    async def _delayed_recall(self, event, message_id, delay: int):
        """延迟 delay 秒后撤回指定消息 (仅撤回房间解析信息/重试进度)."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            # message_id 可能是 int 或 str (不同适配器不一致), 原样传入, 不要强转
            await event.bot.delete_msg(message_id=message_id)
            self.logger.debug(f"[撤回] 撤回成功 mid={message_id} delay={delay}s")
        except asyncio.CancelledError:
            # 插件卸载/任务清理时被取消, 属正常退出, 不向上抛
            self.logger.debug(f"[撤回] 任务被取消 mid={message_id}")
        except Exception as e:
            # QQ 普通成员有时间窗口限制(约2分钟), 超时/无权限撤回失败属正常
            self.logger.debug(f"[撤回] 失败(可能超时间窗口或无权限) mid={message_id}: {e}")

    def _acquire_probe(self) -> int:
        """分配一个探针编号 (同步). 无空闲探针时返回 None (调用方跳过该房号本轮查询).
        "判定冷却→分配探针→建任务"整段无 await, 单线程 asyncio 下天然原子, 无需加锁."""
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
        return None

    def _release_probe(self, idx: int):
        """释放探针编号 (同步). 无 await, 天然原子."""
        self.probe_used.discard(idx)
        if idx not in self.probe_released:
            self.probe_released.append(idx)

    @staticmethod
    def _short_ts() -> str:
        """短时间标识: 编码当前'月日时分秒'为5位base36, 可逆解回.
        相比 int(time.time()) 的10位纯数字, 更短且含字母, 降低被服务器过滤概率."""
        t = time.localtime()
        # 月日时分秒压成整数: (月-1)*31+日-1 保证唯一可逆, 最大 32140799, base36 5位足够
        v = ((((t.tm_mon - 1) * 31 + (t.tm_mday - 1)) * 24 + t.tm_hour) * 60 + t.tm_min) * 60 + t.tm_sec
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        s = ""
        while v:
            s = chars[v % 36] + s
            v //= 36
        return s.zfill(5)

    @staticmethod
    def _ts_decode(s: str):
        """反解 _short_ts 生成的标识, 返回 (月, 日, 时, 分, 秒)."""
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        v = 0
        for c in s:
            v = v * 36 + chars.find(c)
        sec = v % 60; v //= 60
        minute = v % 60; v //= 60
        hour = v % 24; v //= 24
        day = v % 31 + 1; v //= 31
        month = v + 1
        return month, day, hour, minute, sec

    @staticmethod
    def _relay_challenge(a) -> str:
        """由列表第 21 列(A 字段)推导 masterserver get 的挑战码。

        反推自客户端 gameFramework/j/m.java 的 a(int i):
            i < 100000  -> MD5("x"  + i)[:10]
            i < 200000  -> MD5("y"  + i)[:11]
            i < 300000  -> MD5("z"  + i)[:12]
            i < 1000000 -> MD5("xx" + i)[:13]
            i < 2000000 -> MD5("yy" + i)[:14]
        实测 A=10605 -> "da7821ccf7"
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

    def _resolve_relay_addr(self, room_uuid: str, join_a) -> str:
        """把中继房在列表里的入口地址解析成真实游戏地址(同步, 供 to_thread 调用)。

        中继房在列表里的 ip:port 全是同一个注册入口(如 18.216.139.119:5158),
        直连会被踢 "Your client is newer then the server"。真实地址需向
        masterserver 发 action=get, 参数 c 由 A 字段推导。

        仅内部连接使用, 结果不对外展示。
        """
        c = self._relay_challenge(join_a)
        if not c or c == "NA":
            return ""
        import urllib.parse
        import urllib.request
        import gzip as _gzip
        for base in ("http://gs1.corrodinggames.com/masterserver/1.4",
                     "http://gs4.corrodinggames.net/masterserver/1.4"):
            url = base + "/interface?" + urllib.parse.urlencode(
                {"action": "get", "game_id": room_uuid, "c": c})
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "rw android 176 en", "Language": "en"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        try:
                            data = _gzip.decompress(data)
                        except OSError:
                            pass
                text = data.decode("utf-8", "replace")
            except Exception as e:
                self.logger.debug(f"[查房] get 请求失败 {base}: {e}")
                continue
            rows = [x for x in text.splitlines() if x.strip()]
            if len(rows) < 4 or rows[1].strip().upper() != "OK":
                self.logger.debug(f"[查房] get 响应异常: {rows[:2]}")
                continue
            f = rows[3].split(",")
            if len(f) > 5:
                ip, port = f[3].strip(), f[5].strip()
                if ip and port.isdigit():
                    return f"{ip}:{port}"
        return ""

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
            # 回收子进程, 避免僵尸进程窗口
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            return f"[rwinfo] 查询超时(>{QUERY_TIMEOUT}s): {rid}"
        except asyncio.CancelledError:
            # 协程被取消(插件卸载/任务清理): 必须终止并回收子进程, 避免留下孤儿进程
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise
        except Exception as e:
            # 通信阶段其他异常: 同样回收子进程后透传
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise e
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
        # 过滤调试行: room_info.py 的流程日志以 [DEBUG]/[n] 中继/===/151 relay 等开头,
        # 房间信息正文以 "版本：" 开头. 只保留正文, 避免 debug 模式下调试输出刷屏群里.
        mark = out.find("版本：")
        if mark != -1:
            return out[mark:]
        return out

    def _is_allowed(self, is_private, uid, gid) -> bool:
        """名单判定. 依据当前 mode 及会话类型(is_private)判定是否放行查房.
        入参: is_private 是否私聊, uid 用户ID, gid 群号(私聊为 None).
        返回 True=放行, False=拦截.
        mode 三种:
          black  黑名单: 私聊@uid/群聊#gid或@uid 命中黑名单即拦, 都不在->放
          white  白名单: 私聊@uid/群聊#gid或@uid 命中白名单即放, 都不在->拦
          mixed  混合:   @uid优先(黑拦/白放), 不在再看#gid, 群级都不在按白名单是否有群条目
        """
        if not self.config.get("global_enabled", True):
            return False  # 全局关闭, 一律拦截
        mode = self.config.get("mode", "black")

        if mode == "black":
            bl = self.config.get("黑名单", [])
            if is_private:
                return not (uid and f"@{uid}" in bl)
            # 群聊: #gid 或 @uid 任一在黑名单 -> 拦
            return not ((gid and f"#{gid}" in bl) or (uid and f"@{uid}" in bl))

        if mode == "white":
            wl = self.config.get("白名单", [])
            if is_private:
                return bool(uid and f"@{uid}" in wl)
            # 群聊: #gid 或 @uid 任一在白名单 -> 放
            return bool((gid and f"#{gid}" in wl) or (uid and f"@{uid}" in wl))

        # ---- mixed 混合模式 ----
        mwl = self.config.get("混合_白名单", [])
        mbl = self.config.get("混合_黑名单", [])
        if is_private:
            if uid and f"@{uid}" in mbl:
                return False  # 用户在混合黑名单 -> 拦
            if uid and f"@{uid}" in mwl:
                return True   # 用户在混合白名单 -> 放
            # 用户不在两名单: 跟随配置的私聊默认策略
            policy = self.config.get("混合_私聊策略", "white")
            return policy != "white"  # white=不在白->拦(False); black=在黑才拦->放(True)
        # 群聊
        if uid and f"@{uid}" in mbl:
            return False  # 用户黑名单优先(即使群在白名单) -> 拦
        if uid and f"@{uid}" in mwl:
            return True   # 用户白名单优先(即使群在黑名单) -> 放
        # 用户不在两名单: 看群级
        if gid and f"#{gid}" in mbl:
            return False  # 群在黑名单 -> 拦
        if gid and f"#{gid}" in mwl:
            return True   # 群在白名单 -> 放
        # 群级都不在: 混合白名单里有没有群条目(#) -> 有则拦, 无则放
        has_group_whitelist = any(k.startswith("#") for k in mwl)
        return not has_group_whitelist

    # ---- 指令 (除帮助外均要求管理员权限) ----
    @filter.command("铁锈查房帮助")
    async def cmd_help(self, event: AstrMessageEvent, arg: str = ""):
        """显示本插件所有指令及用法."""
        yield event.plain_result("\n".join(self._help_text_lines()))

    # ---- 帮助别名 ----
    @filter.command("铁锈查房菜单")
    async def cmd_help_alias1(self, event: AstrMessageEvent, arg: str = ""):
        yield event.plain_result("\n".join(self._help_text_lines()))

    @filter.command("铁锈帮助")
    async def cmd_help_alias2(self, event: AstrMessageEvent, arg: str = ""):
        yield event.plain_result("\n".join(self._help_text_lines()))

    @filter.command("rwhelp")
    async def cmd_help_alias3(self, event: AstrMessageEvent, arg: str = ""):
        yield event.plain_result("\n".join(self._help_text_lines()))

    def _help_text_lines(self):
        """帮助文本(供主指令与别名共用)."""
        return [
            f"【铁锈查房插件指令】v{PLUGIN_VERSION}",
            "",
            "—— 房间列表 ——",
            "/铁锈列表 [页码]|[筛选]|[搜索 关键词]|[刷新]",
            "",
            "筛选:",
            "  模组|原版        是否使用 Mod",
            "  房间中|游戏中    房间状态",
            "  满|不满          人数是否已满",
            "  真人|官方        数据来源",
            "",
            "页码: 纯数字, 如 2 表示第 2 页",
            "刷新: 忽略缓存重新抓取",
            "组合: /铁锈列表 模组 房间中 2",
            "别名: /房间列表 /铁锈房列表 /rwlist",
            "/铁锈下一页 / 铁锈上一页",
            "",
            "—— 查房 ——",
            "/铁锈查房 <序号|房号>",
            "  序号: 先发 /铁锈列表, 再填表里的序号, 如 /铁锈查房 2",
            "  房号: r5132 / s1024 / Q12345 / hytvvi",
            "",
            "给筛选词就整体替换旧筛选; 只给页码或翻页则沿用;",
            "只发 /铁锈列表 (不带任何参数) 则重新查全部。",
            "",
            "—— 插件设置 ——",
            "/铁锈全局解析 开|关",
            "/铁锈模式 白|黑|混合",
            "/铁锈白名 +@用户ID|+#群号",
            "/铁锈黑名 +@用户ID|+#群号",
            "/铁锈私聊策略 白|黑",
            "/铁锈私聊开关 开|关",
            "/铁锈玩家列表 开|关",
            "/铁锈重试 [0-10]",
            "/铁锈撤回 开|关|[秒数0-300] [*]",
            "别名: /铁锈查房菜单 /铁锈帮助 /rwhelp",
            "",
            "—— 其他 ——",
            "/铁锈查房调试 - 查看运行时状态(撤回/探针/缓存)",
            "",
            "—— 自动触发 ——",
            "群聊/私聊发房号自动查房",
            "数字房: oqrs+数字(r5132 s1024 o9999 q7777)",
            "字母房: 其他字母+纯字母(hytvvi aabcde babc)",
            "两种房号均需 3 位以上",
        ]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_unmatched(self, event: AstrMessageEvent):
        """未识别的 /铁锈 指令自动路由到帮助菜单."""
        text = ""
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return
        if not text.startswith("/"):
            return
        cmd = text[1:].split()[0] if len(text) > 1 else ""
        # 只接管 "铁锈"/"rwinfo" 开头但非已知指令的情况
        if not (cmd.startswith("铁锈") or cmd.startswith("rwinfo")):
            return
        known = {
            "铁锈查房帮助", "铁锈查房菜单", "铁锈帮助", "rwhelp",
            "铁锈列表", "房间列表", "铁锈房列表", "rwlist",
            "铁锈下一页", "铁锈上一页", "铁锈查房",
            "铁锈全局解析", "铁锈模式", "铁锈白名", "铁锈黑名",
            "铁锈私聊策略", "铁锈私聊开关", "铁锈玩家列表",
            "铁锈重试", "铁锈撤回", "铁锈查房调试",
        }
        if cmd in known:
            return
        yield event.plain_result(
            f"未知指令：/{cmd}\n\n" + "\n".join(self._help_text_lines())
        )

    # ---- 房间列表 (分页 + 按 QQ 号缓存) ----
    # 滑动过期: 每次"有动作"(翻页/切筛选/刷新)都会把过期时间往后推
    LIST_CACHE_TTL = 60   # 秒: 房间数据缓存有效期(全局共享一份, 滑动续期)
    LIST_STATE_TTL = 60   # 秒: 每个 QQ 的翻页/筛选状态有效期(滑动续期)

    def _list_cache(self):
        """房间数据缓存容器(全局共享一份, 所有 QQ/群共用)。"""
        if not hasattr(self, "_room_list_cache"):
            self._room_list_cache = {"ts": 0.0, "rooms": []}
        return self._room_list_cache

    def _list_page_state(self):
        """按 QQ 号隔离的翻页/筛选状态。key=QQ号, value=状态字典。"""
        if not hasattr(self, "_room_page_state"):
            self._room_page_state = {}
        return self._room_page_state

    def _list_uid(self, event):
        """取发送者 QQ 号(统一按 QQ 号隔离, 不区分群聊/私聊)。"""
        try:
            uid = normalize_gid(event.get_sender_id())
            return uid or "unknown"
        except Exception:
            return "unknown"

    def _state_get(self, event):
        """取出该 QQ 的状态; 已过期则视为无状态(惰性清理)。"""
        uid = self._list_uid(event)
        states = self._list_page_state()
        st = states.get(uid)
        if not st:
            return None
        # 滑动过期: 超过 LIST_STATE_TTL 未动作则作废
        if (time.time() - st.get("ts", 0)) > self.LIST_STATE_TTL:
            states.pop(uid, None)
            return None
        return st

    def _state_put(self, event, page, keyword, status, mod_filter, full, source,
                   total=0, addrs=None):
        """保存该 QQ 的状态并刷新过期时间(滑动续期)。"""
        uid = self._list_uid(event)
        self._list_page_state()[uid] = {
            "page": page, "keyword": keyword, "status": status,
            "mod": mod_filter, "full": full, "source": source,
            "total": total,      # 总页数, 供翻页指令判断边界
            # 本次筛选结果的直连地址(顺序与列表序号一致), 供 /铁锈查房 <序号> 使用
            "addrs": list(addrs or []),
            "ts": time.time(),   # 每次动作都刷新 → 滑动过期
        }

    def _state_touch(self, event):
        """仅刷新过期时间(命中缓存时也要续期)。"""
        uid = self._list_uid(event)
        st = self._list_page_state().get(uid)
        if st:
            st["ts"] = time.time()

    def _cleanup_states(self):
        """清理已过期的 QQ 状态, 避免字典无限增长。"""
        now = time.time()
        states = self._list_page_state()
        dead = [k for k, v in states.items()
                if (now - v.get("ts", 0)) > self.LIST_STATE_TTL]
        for k in dead:
            states.pop(k, None)

    async def _list_fetch(self, force: bool = False):
        """拉取房间列表(带滑动过期缓存)。force=True 强制刷新。"""
        cache = self._list_cache()
        now = time.time()
        if not force and cache["rooms"] and (now - cache["ts"]) < self.LIST_CACHE_TTL:
            cache["ts"] = now   # 滑动续期: 有人用就续命
            return cache["rooms"], True   # 命中缓存

        cmd_args = [sys.executable, "-u", ROOM_INFO_PY, "--list-json"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ROOM_INFO_DIR,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return None, False
        except Exception as e:
            self.logger.error(f"房间列表获取失败: {e}")
            return None, False

        out = (stdout or b"").decode("utf-8", "replace").strip()
        rooms = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("["):
                try:
                    rooms = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if rooms is None:
            rooms = []
        cache["rooms"] = rooms
        cache["ts"] = time.time()
        return rooms, False

    @filter.command("铁锈列表")
    async def cmd_list(self, event: AstrMessageEvent, arg: str = ""):
        """房间列表(分页). 数据源为真人房(Q Room List) + 官方 Auto Server."""
        async for _r in self._cmd_list_impl(event, arg):
            yield _r

    # ---- 列表指令别名 ----
    @filter.command("房间列表")
    async def cmd_list_alias1(self, event: AstrMessageEvent, arg: str = ""):
        async for _r in self._cmd_list_impl(event, arg):
            yield _r

    @filter.command("铁锈房列表")
    async def cmd_list_alias2(self, event: AstrMessageEvent, arg: str = ""):
        async for _r in self._cmd_list_impl(event, arg):
            yield _r

    @filter.command("rwlist")
    async def cmd_list_alias3(self, event: AstrMessageEvent, arg: str = ""):
        async for _r in self._cmd_list_impl(event, arg):
            yield _r

    async def _cmd_list_impl(self, event: AstrMessageEvent, arg: str = ""):
        # AstrBot 只把第一个空格参数传给 arg, 这里从原始消息补全全部参数
        arg = self._raw_args(
            event, arg, "铁锈房列表", "房间列表", "铁锈列表", "rwlist"
        )
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        # 列表是只读查询, 走黑白名单而不是硬性要求管理员
        # (早期版本非管理员直接静默 return, 群里普通成员完全看不到任何反应)
        uid = normalize_gid(event.get_sender_id())
        gid = normalize_gid(event.get_group_id())
        if not self._is_allowed(is_private, uid, gid):
            yield event.plain_result(
                "当前会话没有房间列表权限。\n"
                "管理员可用 /铁锈模式 与 /铁锈白名 调整放行范围。"
            )
            return

        # 取该 QQ 的历史状态(已过期则视为无)
        st = self._state_get(event)
        # 顺手清理其它已过期的 QQ 状态
        if len(self._list_page_state()) > 50:
            self._cleanup_states()

        # 默认筛选条件(可由配置指定)
        keyword = ""
        status = ""
        mod_filter = ""
        full = self.config.get("list_default_full", "")  # ""/hide
        source = "all"

        page = 1
        force = False
        # 本次是否显式给了筛选条件:
        #   给了 -> 整体丢弃旧筛选, 只用本次的
        #   没给 -> 沿用该 QQ 上次的条件
        has_filter_arg = False

        # ---- 参数解析表: 单遍扫描, 支持 "模组 2" / "搜索 岛屿 2" / "刷新" ----
        FILTER_WORDS = {
            "模组": ("mod", "mod"), "mod": ("mod", "mod"),
            "原版": ("mod", "vanilla"), "vanilla": ("mod", "vanilla"),
            "房间中": ("status", "battleroom"), "可加入": ("status", "battleroom"),
            "joinable": ("status", "battleroom"), "battleroom": ("status", "battleroom"),
            "游戏中": ("status", "ingame"), "ingame": ("status", "ingame"),
            "满": ("full", "only"), "人满": ("full", "only"), "full": ("full", "only"),
            "不满": ("full", "hide"), "有空": ("full", "hide"), "notfull": ("full", "hide"),
            "真人": ("source", "qroom"), "qroom": ("source", "qroom"), "q": ("source", "qroom"),
            "官方": ("source", "official"), "official": ("source", "official"),
            "auto": ("source", "official"),
        }
        RETAIN = ("搜索", "search")
        RESET_STATE = ("清空", "重置", "清除", "reset", "clear")
        REFRESH = ("刷新", "重载", "refresh")

        toks = arg.split()
        idx = 0
        bad = ""
        while idx < len(toks):
            t = toks[idx]
            if t.isdigit():
                page = max(1, int(t))
            elif t in REFRESH:
                force = True
            elif t in RETAIN:
                # "搜索 关键词" 或 "搜索关键词"
                kw = ""
                if idx + 1 < len(toks):
                    idx += 1
                    kw = toks[idx]
                if not kw:
                    yield event.plain_result("用法: /铁锈列表 搜索 <关键词>")
                    return
                keyword = kw
                has_filter_arg = True
            elif t.startswith("搜索"):
                keyword = t[2:].strip()
                has_filter_arg = True
            elif t.startswith("search"):
                keyword = t[6:].strip()
                has_filter_arg = True
            elif t in FILTER_WORDS:
                field, val = FILTER_WORDS[t]
                if field == "mod":
                    mod_filter = val
                elif field == "status":
                    status = val
                elif field == "full":
                    full = val
                elif field == "source":
                    source = val
                has_filter_arg = True
            elif t in RESET_STATE:
                # 彻底清空该 QQ 的筛选/翻页状态, 立即生效
                uid = self._list_uid(event)
                self._list_page_state().pop(uid, None)
                yield event.plain_result(
                    "已清空本账号的房间列表筛选状态\n"
                    "下一条 /铁锈列表 将从全部房间重新开始"
                )
                return
            else:
                bad = t
                break
            idx += 1

        if bad:
            yield event.plain_result(
                f"无法识别的参数：{bad}\n\n"
                "【房间列表用法】\n"
                "/铁锈列表 [页码]|[筛选]|[搜索 关键词]|[刷新]\n"
                "\n"
                "筛选: 模组|原版  房间中|游戏中  满|不满  真人|官方\n"
                "页码: 纯数字, 如 2 表示第 2 页\n"
                "刷新: 忽略缓存重新抓取\n"
                "给筛选词就整体替换旧筛选\n"
                "组合: /铁锈列表 模组 房间中 2\n"
                "/铁锈下一页 / 铁锈上一页"
            )
            return

        # 无参数 = 全新查询, 不继承任何筛选
        # 只在「给了参数但没给筛选词」时沿用该 QQ 上次的条件(如 /铁锈列表 2、/铁锈下一页)
        if st and toks and not has_filter_arg:
            keyword = st.get("keyword", "")
            status = st.get("status", "")
            mod_filter = st.get("mod", "")
            full = st.get("full", "")
            source = st.get("source", "all")

        rooms_all, cached = await self._list_fetch(force=force)
        if rooms_all is None:
            yield event.plain_result("[房间列表] 获取超时，请稍后重试")
            return
        if not rooms_all:
            yield event.plain_result("[房间列表] 没有获取到房间数据")
            return

        # 本地筛选
        def match(r):
            rst = (r.get("status") or "").lower()
            if status and rst != status:
                return False
            if mod_filter == "mod" and not r.get("is_mod"):
                return False
            if mod_filter == "vanilla" and r.get("is_mod"):
                return False
            isfull = (r.get("max") or 0) > 0 and (r.get("players") or 0) >= (r.get("max") or 0)
            if full == "only" and not isfull:
                return False
            if full == "hide" and isfull:
                return False
            if source != "all" and (r.get("source") or "") != source:
                return False
            if keyword:
                blob = " ".join([str(r.get("name") or ""), str(r.get("map") or ""),
                                 str(r.get("desc") or "")]).lower()
                if keyword.lower() not in blob:
                    return False
            return True

        rooms = [r for r in rooms_all if match(r)]
        if not rooms:
            yield event.plain_result("[房间列表] 没有符合条件的房间，试试 /铁锈列表（不带参数）")
            return

        # 总条数上限(避免房间数暴涨时分页过多/消息过长), 0 或非法值视为不限
        matched_total = len(rooms)
        max_display = 20
        try:
            max_display = int(self.config.get("list_max_display", 20))
        except (TypeError, ValueError):
            max_display = 20
        if max_display > 0:
            rooms = rooms[:max_display]

        page_size = 5
        try:
            page_size = max(1, int(self.config.get("list_page_size", 5)))
        except (TypeError, ValueError):
            page_size = 5
        total_pages = max(1, (len(rooms) + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_rooms = rooms[start:start + page_size]

        # 保存该 QQ 的翻页/筛选状态(滑动续期 60 秒)
        # items 只存当前页, 与列表展示的序号一一对应; 查房时直接用 addr 或 room_id
        # join_a 非 0 表示该条是中继房, 地址需另行解析(仅内部使用, 不展示)
        self._state_put(event, page, keyword, status, mod_filter, full, source,
                        total_pages,
                        [{
                            "addr": str(r.get("addr") or ""),
                            "room_id": str(r.get("room_id") or ""),
                            "name": str(r.get("name") or ""),
                            "uuid": str(r.get("id") or ""),
                            "join_a": str(r.get("join_a") or "0"),
                        } for r in page_rooms])

        # ---- 组装消息 ----
        conds = []
        if source == "qroom":
            conds.append("真人房")
        elif source == "official":
            conds.append("官方房")
        if mod_filter == "mod":
            conds.append("模组")
        elif mod_filter == "vanilla":
            conds.append("原版")
        if status == "battleroom":
            conds.append("房间中")
        elif status == "ingame":
            conds.append("游戏中")
        if full == "only":
            conds.append("人满")
        elif full == "hide":
            conds.append("未满")
        if keyword:
            conds.append(f"搜索「{keyword}」")

        header = f"【房间列表】{len(rooms)} 个"
        # 命中数超过上限时明确提示, 避免用户误以为只有这么多
        if matched_total > len(rooms):
            header += f"（共 {matched_total} 个，已限 {len(rooms)} 个）"
        if conds:
            header += f"（{'/'.join(conds)}）"
        header += f"　第 {page}/{total_pages} 页"
        if cached and not force:
            header += "　[缓存]"

        lines = [header, ""]
        status_cn = {"battleroom": "房间中", "ingame": "游戏中",
                     "locked": "已锁定", "chat": "聊天", "unknown": "-"}
        for idx, rm in enumerate(page_rooms, start + 1):
            host = str(rm.get("name") or "?")[:16]
            mapname = str(rm.get("map") or "-")[:28]
            pl = rm.get("players", 0)
            mx = rm.get("max", 0)
            sts = status_cn.get((rm.get("status") or "").lower(), "-")
            mod = "模组" if rm.get("is_mod") else "原版"
            lines.append(f"{idx}. {host}　{pl}/{mx}　{sts}　{mod}")
            lines.append(f"   地图：{mapname}")
            hostname = str(rm.get("host") or "").strip()
            if hostname and hostname not in host:
                lines.append(f"   房主：{hostname[:24]}")
            if rm.get("source") == "official":
                lines.append("   （官方自动房）")
        lines.append("")
        lines.append(f"/铁锈查房 <序号> 查详情（本条 1-{len(page_rooms)}）")
        nav = []
        if page > 1:
            nav.append("/铁锈上一页")
        if page < total_pages:
            nav.append("/铁锈下一页")
        if nav:
            lines.append("　".join(nav))
        if total_pages > 1:
            lines.append(f"/铁锈列表 <页码> 直达 1-{total_pages}")
        yield event.plain_result("\n".join(lines))

    @filter.command("铁锈下一页")
    async def cmd_list_next(self, event: AstrMessageEvent, arg: str = ""):
        """房间列表下一页"""
        st = self._state_get(event)
        if not st:
            yield event.plain_result("请先发送 /铁锈列表（翻页状态按 QQ 号记住 60 秒）")
            return
        cur = st.get("page", 1)
        total = st.get("total", 0)
        if total and cur >= total:
            yield event.plain_result(f"已经是最后一页了（第 {cur}/{total} 页）")
            return
        nxt = cur + 1
        async for r in self._cmd_list_impl(event, str(nxt)):
            yield r

    @filter.command("铁锈上一页")
    async def cmd_list_prev(self, event: AstrMessageEvent, arg: str = ""):
        """房间列表上一页"""
        st = self._state_get(event)
        if not st:
            yield event.plain_result("请先发送 /铁锈列表（翻页状态按 QQ 号记住 60 秒）")
            return
        cur = st.get("page", 1)
        if cur <= 1:
            yield event.plain_result("已经是第一页了")
            return
        prv = max(1, cur - 1)
        async for r in self._cmd_list_impl(event, str(prv)):
            yield r

    @filter.command("铁锈查房")
    async def cmd_query_room(self, event: AstrMessageEvent, arg: str = ""):
        """显式查房: 按列表序号 / 房号 / IP:端口 查询.

        与自动识别不同, 这里是用户明确指定目标, 因此失败原因必须回传(不静默)。
        """
        # AstrBot 只把第一个空格参数传给 arg, 这里从原始消息补全
        arg = self._raw_args(event, arg, "铁锈查房")
        arg = (arg or "").strip()
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        # 显式查房同样走黑白名单, 不再硬性要求管理员:
        # 普通群成员发 /铁锈查房 之前会被静默 return, 表现为"指令完全没反应"
        uid = normalize_gid(event.get_sender_id())
        gid = normalize_gid(event.get_group_id())
        if not self._is_allowed(is_private, uid, gid):
            yield event.plain_result(
                "当前会话没有查房权限。\n"
                "管理员可用 /铁锈模式 与 /铁锈白名 调整放行范围。"
            )
            return

        USAGE = (
            "用法: /铁锈查房 <序号|房号>\n"
            "  序号: 先发 /铁锈列表, 再填列表里的序号, 如 /铁锈查房 2\n"
            "  房号: r5132 / s1024 / Q12345 / hytvvi"
        )
        if not arg:
            yield event.plain_result(USAGE)
            return

        # ---- 解析目标 ----
        target = ""
        desc = ""
        if IP_QUERY_RE.match(arg):
            # IP / IP:端口 原样交给 room_info.py (其内部识别为直连目标)
            target = arg
            desc = arg
        elif arg.isdigit():
            # 纯数字 = 列表序号(纯数字本身不是房号)
            st = self._state_get(event)
            items = (st or {}).get("addrs") or []
            if not items:
                yield event.plain_result(
                    "请先发送 /铁锈列表（序号取自最近一次列表结果，60 秒内有效）"
                )
                return
            n = int(arg)
            if n < 1 or n > len(items):
                yield event.plain_result(
                    f"序号超出范围：当前页共 {len(items)} 条"
                )
                return
            item = items[n - 1]
            if isinstance(item, dict):
                label = str(item.get("name") or "").strip()
                # qroom 房只有房号, official 房只有地址
                target = (str(item.get("addr") or "").strip()
                          or str(item.get("room_id") or "").strip())
                # 中继房: 列表里是注册入口地址, 直连会被踢。这里静默换成真实
                # 地址再连。解析过程不产生任何面向用户的消息。
                try:
                    join_a = int(str(item.get("join_a") or "0"))
                except ValueError:
                    join_a = 0
                uuid_ = str(item.get("uuid") or "").strip()
                if join_a and uuid_ and not str(item.get("room_id") or "").strip():
                    real = await asyncio.to_thread(
                        self._resolve_relay_addr, uuid_, join_a)
                    if real:
                        target = real
            else:
                label = ""
                target = str(item or "").strip()
            if not target:
                yield event.plain_result(
                    f"第 {n} 条没有可用的地址或房号，无法查询"
                )
                return
            desc = f"第 {n} 条 {label}".strip()
        else:
            rids = extract_room_ids(arg)
            if not rids:
                yield event.plain_result(
                    f"无法识别的房号：{arg}\n\n" + USAGE
                )
                return
            target = rids[0]
            desc = target

        # ---- 分配探针并查询 ----
        idx = self._acquire_probe()
        if idx is None:
            yield event.plain_result("探针全部被占用，请稍后再试")
            return

        result = ""
        try:
            max_retry = 1
            try:
                max_retry = max(1, int(self.config.get("retry_times", 3)))
            except (TypeError, ValueError):
                max_retry = 1
            attempt = 0
            while attempt < max_retry:
                attempt += 1
                # 第一次带时间后缀; 后续缩短名字, 降低被过滤概率
                if attempt == 1:
                    probe_name = f"ABAB探针{idx:02d}_{self._short_ts()}"
                else:
                    probe_name = f"ABAB探针{idx:02d}_{attempt}"
                try:
                    result = await asyncio.wait_for(
                        self._query_room(target, probe_name),
                        timeout=QUERY_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    result = ""
                except Exception as e:
                    self.logger.error(f"[查房] {target} 查询异常: {e}")
                    result = ""
                if is_room_info(result):
                    break
                if not is_name_check_error(result):
                    break
                self.logger.debug(
                    f"[查房] {desc} 探针名被过滤, 重试({attempt}/{max_retry})"
                )
        finally:
            self._release_probe(idx)

        # ---- 回传(显式查房不静默, 必须给出原因) ----
        if is_room_info(result):
            await self._send_room_result(event, result)
            return
        if not result:
            yield event.plain_result(
                f"[查房] {desc} 无响应（房间不存在 / 中继繁忙 / 地址不可达）"
            )
        elif is_error_info(result):
            yield event.plain_result(translate_error(result))
        else:
            # 既不是房间信息也不是已知错误: 多为中继链路调试输出,
            # 不把原始日志贴到群里, 只取第一条像样的说明文字
            noise = ("[", "===", "!!", "151 relay", "relay:", "DEBUG", "sid=")
            tail = ""
            for line in result.splitlines():
                s = line.strip()
                if not s or s.startswith(noise):
                    continue
                # 跳过纯 kv 调试行 (如 v7=1 v8=5 ...)
                if "=" in s and " " in s and not any(c in s for c in "，。：！？"):
                    continue
                tail = s
                break
            msg = f"[查房] {desc} 未获取到房间信息"
            if tail:
                msg += f"\n{tail[:200]}"
            yield event.plain_result(msg)

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
        MODE_MAP = {"白": "white", "黑": "black", "混合": "mixed"}
        NAME = {"white": "白名单(仅名单内放行)", "black": "黑名单(名单内禁用)", "mixed": "混合模式(@用户优先于#群)"}
        if not arg:
            mode = self.config.get("mode", "black")
            yield event.plain_result(f"当前模式：{NAME[mode]}")
            return
        if arg not in MODE_MAP:
            yield event.plain_result("用法: /铁锈模式 白|黑|混合")
            return
        new_mode = MODE_MAP[arg]
        old_mode = self.config.get("mode", "black")
        if new_mode != old_mode:
            if new_mode == "mixed":
                # 切到混合: 若混合名单为空, 用当前白/黑名单作为初始(保留上次混合配置)
                if not self.config.get("混合_白名单"):
                    self.config["混合_白名单"] = list(self.config.get("白名单", []))
                if not self.config.get("混合_黑名单"):
                    self.config["混合_黑名单"] = list(self.config.get("黑名单", []))
            # 从混合切回白/黑: 白/黑名单保持切换前状态(独立保存), 混合名单保留供下次切回时恢复
            self.config["mode"] = new_mode
            self._save_config()
        yield event.plain_result(f"当前模式已切换为：{NAME[new_mode]}")

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
        mixed = self.config.get("mode") == "mixed"
        key = "混合_白名单" if mixed else "白名单"
        opp = "混合_黑名单" if mixed else None
        label = "混合白名单" if mixed else "白名单"
        text = self._cmd_list_text(arg, key, label, opp, cmd_name="铁锈白名")
        yield event.plain_result(text)
        self._save_config()

    @filter.command("铁锈黑名")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_black(self, event: AstrMessageEvent, arg: str = ""):
        mixed = self.config.get("mode") == "mixed"
        key = "混合_黑名单" if mixed else "黑名单"
        opp = "混合_白名单" if mixed else None
        label = "混合黑名单" if mixed else "黑名单"
        text = self._cmd_list_text(arg, key, label, opp, cmd_name="铁锈黑名")
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
            yield event.plain_result("用法: /铁锈重试 [0-10]")
            return
        self.config["retry_times"] = int(arg)
        self._save_config()
        yield event.plain_result(f"重试次数已设置为：{self.config['retry_times']}")

    def _diag_text(self, event) -> str:
        """生成运行时诊断信息. 不修改任何状态, 只读取."""
        L = ["【铁锈查房调试】"]

        # ---- 1. 平台/会话识别 ----
        def safe(fn, default="<异常>"):
            try:
                return fn()
            except Exception as e:
                return f"<{type(e).__name__}: {e}>"

        gid_raw = safe(event.get_group_id)
        uid_raw = safe(event.get_sender_id)
        is_priv = safe(event.is_private_chat)
        is_admin = safe(event.is_admin)
        key, is_private = self._recall_session_key(event)

        L.append("")
        L.append("-- 会话 --")
        L.append(f"群号原始: {gid_raw!r} -> 归一: {normalize_gid(gid_raw)!r}")
        L.append(f"发送者原始: {uid_raw!r} -> 归一: {normalize_gid(uid_raw)!r}")
        L.append(f"私聊: {is_priv}  管理员: {is_admin}")
        L.append(f"撤回会话key: {key!r} (is_private={is_private})")

        # ---- 2. 撤回配置 ----
        L.append("")
        L.append("-- 撤回配置 --")
        cfg = self._get_recall_cfg(event)
        L.append(f"生效配置: 开={cfg['开']} 延迟={cfg['延迟']}s")
        grp = self._recall_store(False)
        prv = self._recall_store(True)
        L.append(f"群撤回条目({len(grp)}):")
        if grp:
            for e in grp:
                p = self._recall_parse(e)
                L.append(f"  {p['key']}: 开={p['开']} 延迟={p['延迟']}s")
        else:
            L.append("  (空)")
        L.append(f"私聊撤回条目({len(prv)}):")
        if prv:
            for e in prv:
                p = self._recall_parse(e)
                L.append(f"  {p['key']}: 开={p['开']} 延迟={p['延迟']}s")
        else:
            L.append("  (空)")
        L.append(f"待执行撤回任务: {len(self.pending_recall_tasks)}")

        # ---- 3. 探针池 ----
        L.append("")
        L.append("-- 探针池 --")
        L.append(
            f"已用 {len(self.probe_used)} / 空闲队列 {len(self.probe_released)} / "
            f"下一个编号 {self.probe_next} (范围 {PROBE_MIN}-{PROBE_MAX})"
        )
        if self.probe_used:
            used = sorted(self.probe_used)
            L.append("占用: " + ",".join(f"{i:02d}" for i in used[:20]) + ("..." if len(used) > 20 else ""))
        L.append(f"在途任务: {len(self.pending_tasks)}")

        # ---- 4. 房间列表缓存 + 状态 ----
        L.append("")
        L.append("-- 房间列表 --")
        uid = self._list_uid(event)
        cache = self._list_cache()
        if cache.get("rooms"):
            age = time.time() - cache.get("ts", 0)
            flag = "" if age <= self.LIST_CACHE_TTL else " (已过期)"
            L.append(
                f"数据缓存: {len(cache['rooms'])} 条, 存活 {age:.1f}s / "
                f"{self.LIST_CACHE_TTL}s{flag}"
            )
        else:
            L.append("数据缓存: 空")
        states = self._list_page_state()
        L.append(f"列表状态条目: {len(states)}  (当前 QQ={uid})")
        st = states.get(uid)
        if st:
            age = time.time() - st.get("ts", 0)
            L.append(
                f"  当前QQ状态: 第{st.get('page', 1)}页 模组={st.get('mod') or '-'} "
                f"状态={st.get('status') or '-'} 满={st.get('full') or '-'} "
                f"来源={st.get('source') or 'all'} 搜索={st.get('keyword') or '-'} "
                f"存活 {age:.1f}s"
            )
        else:
            L.append("  当前QQ状态: 无")
        if len(states) > 1:
            L.append("  其他QQ: " + ", ".join(k for k in list(states)[:5] if k != uid))

        # ---- 5. 其他配置 ----
        L.append("")
        L.append("-- 主要配置 --")
        L.append(f"全局解析: {self.config.get('global_enabled')}  模式: {self.config.get('mode')}")
        L.append(
            f"私聊启用: {self.config.get('私聊启用')}  显示玩家: {self.config.get('show_players')} "
            f"重试次数: {self.config.get('retry_times')}"
        )
        L.append(
            f"每页条数: {self.config.get('list_page_size', 5)}  "
            f"默认满房筛选: {self.config.get('list_default_full') or '(空)'}  "
            f"debug: {self.config.get('debug')}"
        )
        L.append(
            f"白名单 {len(self.config.get('白名单', []))} / "
            f"黑名单 {len(self.config.get('黑名单', []))} / "
            f"混合白 {len(self.config.get('混合_白名单', []))} / "
            f"混合黑 {len(self.config.get('混合_黑名单', []))}"
        )
        L.append(f"房号冷却记录: {len(self.room_cooldown)} 条")
        return "\n".join(L)

    @filter.command("铁锈查房调试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_diag(self, event: AstrMessageEvent, arg: str = ""):
        """打印运行时诊断信息(会话识别/撤回配置/探针池/列表缓存)."""
        yield event.plain_result(self._diag_text(event))


    @filter.command("铁锈撤回")
    async def cmd_recall(self, event: AstrMessageEvent, arg: str = ""):
        """自动撤回开关/延迟(按会话独立): 群聊需管理员, 私聊任何人可用.
        群聊参数末尾加 '*' 表示对所有群生效(通配符, 可被具体群配置覆盖); 私聊不支持通配符."""
        arg = self._recall_full_args(event, arg)
        key, is_private = self._recall_session_key(event)
        if self.config.get("debug", False):
            self.logger.debug(
                f"[撤回] 收到指令 raw_arg={arg!r} key={key!r} is_private={is_private} "
                f"is_admin={event.is_admin()}"
            )
        if not is_private and not event.is_admin():
            self.logger.debug("[撤回] 群聊非管理员, 忽略")
            return
        # 解析末尾 '*' 通配符(仅群聊支持)
        wildcard = False
        parts = arg.split()
        if parts and parts[-1] == "*":
            if is_private:
                # 私聊不支持通配符, 直接拒绝(防止越权影响他人)
                yield event.plain_result("私聊不支持通配符 *（仅群聊可用），已忽略")
                return
            wildcard = True
            parts = parts[:-1]
            arg = " ".join(parts)
        if not key and not wildcard:
            # 拿不到会话标识(平台差异): 明确告知, 不再静默吞掉
            yield event.plain_result(
                "无法识别当前会话，撤回配置未生效。\n"
                "请在群聊或私聊中直接使用，或用 /铁锈查房调试 查看详情"
            )
            return
        scope = ("全部群" if wildcard else (f"本群({key})" if not is_private else "本私聊"))
        if not arg:
            # 查询: 带 * 时读通配符配置, 否则读本会话生效配置
            cfg = self._get_recall_cfg(event, want_wildcard=wildcard)
            state = "开启" if cfg["开"] else "关闭"
            yield event.plain_result(f"{scope}自动撤回：{state}；延迟秒数：{cfg['延迟']} 秒")
            return
        if arg in ("开", "关"):
            self._set_recall_cfg(event, on=(arg == "开"), wildcard=wildcard)
            self.logger.debug(f"[撤回] 写入 on={arg == '开'} wildcard={wildcard} key={key!r}")
            yield event.plain_result(f"{scope}自动撤回已{'开启' if arg == '开' else '关闭'}")
            return
        if arg.isdigit() and 0 <= int(arg) <= 300:
            self._set_recall_cfg(event, delay=int(arg), wildcard=wildcard)
            self.logger.debug(f"[撤回] 写入 delay={int(arg)} wildcard={wildcard} key={key!r}")
            yield event.plain_result(f"{scope}自动撤回延迟已设置为：{int(arg)} 秒")
            return
        yield event.plain_result("用法: /铁锈撤回 开|关|[秒数0-300] [*]")

    @filter.command("铁锈私聊策略")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_private_policy(self, event: AstrMessageEvent, arg: str = ""):
        """混合模式下私聊默认策略: white=不在白名单拦 / black=在黑名单拦."""
        arg = (arg or "").strip()
        if not arg:
            p = self.config.get("混合_私聊策略", "white")
            name = "跟随白名单(不在白名单拦截)" if p == "white" else "跟随黑名单(在黑名单拦截)"
            yield event.plain_result(f"混合模式私聊默认策略：{name}")
            return
        if arg not in ("白", "黑"):
            yield event.plain_result("用法: /铁锈私聊策略 白|黑")
            return
        self.config["混合_私聊策略"] = "white" if arg == "白" else "black"
        self._save_config()
        name = "跟随白名单(不在白名单拦截)" if self.config["混合_私聊策略"] == "white" else "跟随黑名单(在黑名单拦截)"
        yield event.plain_result(f"混合模式私聊默认策略已设为：{name}")

    @filter.command("铁锈私聊开关")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_private_switch(self, event: AstrMessageEvent, arg: str = ""):
        """是否启用私聊查房处理."""
        arg = (arg or "").strip()
        if not arg:
            state = "开启" if self.config.get("私聊启用", True) else "关闭"
            yield event.plain_result(f"私聊查房处理当前状态：{state}")
            return
        if arg not in ("开", "关"):
            yield event.plain_result("用法: /铁锈私聊开关 开|关")
            return
        self.config["私聊启用"] = (arg == "开")
        self._save_config()
        yield event.plain_result(f"私聊查房处理已{'开启' if self.config['私聊启用'] else '关闭'}")

    def _cmd_list_text(self, arg, key, label, opposite_key=None, cmd_name=None) -> str:
        arg = (arg or "").strip()
        lst = self.config.get(key, [])
        if not arg:
            if not lst:
                return f"{label}为空"
            # 兼容迁移: 旧配置纯数字条目视为 #群号; 已有 @/# 前缀的不重复加
            lst2 = []
            for x in lst:
                if x.startswith(("@", "#")):
                    lst2.append(x)
                else:
                    lst2.append("#" + x)
            return f"{label}内: {', '.join(lst2)}"
        # 解析 +条目 / -条目
        op = arg[0] if arg[0] in "+-" else ""
        body = arg[1:] if op else arg
        if not body.startswith(("@", "#")):
            body = "#" + body  # 无前缀默认当群号
        digits = re.search(r"\d+", body[1:])
        if not digits:
            return f"无效标识: {arg}"
        key_val = body[0] + digits.group(0)  # 规范化 @数字 / #数字
        if op == "+":
            if key_val in lst:
                return f"{label}中已存在: {key_val}"
            lst.append(key_val)
            # 同名互斥(混合模式): 加入白名单时从黑名单移除同实体, 反之亦然
            if opposite_key and key_val in self.config.get(opposite_key, []):
                self.config[opposite_key].remove(key_val)
            self._save_config()
            return f"已加入{label}: {key_val}"
        elif op == "-":
            if key_val not in lst:
                return f"{label}中不存在: {key_val}"
            lst.remove(key_val)
            self._save_config()
            return f"已移出{label}: {key_val}"
        return f"用法: /{cmd_name or label} [+@用户ID|+#群号]"

    # ---- 消息监听 ----
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        try:
            # 兜底: 若 __init__ 时事件循环未就绪, 在首条消息补启 bin 清理循环
            self._ensure_bin_cleanup()
            text = event.message_str or ""
            if not text.strip():
                return
            if text.strip().startswith("/"):
                return

            # 群聊按 #群号, 私聊按 @用户ID, 各自进黑/白名单判定
            # 优先用 event.is_private_chat() 判断私聊 (更可靠, 参考 astrbot_plugin_GUGUblack)
            is_private = False
            try:
                is_private = bool(event.is_private_chat())
            except Exception:
                is_private = False
            gid = normalize_gid(event.get_group_id())
            uid = normalize_gid(event.get_sender_id())

            # 私聊处理开关: 关闭私聊时, 私聊消息直接不处理
            if is_private and not self.config.get("私聊启用", True):
                return

            rids = extract_room_ids(text)
            if not rids:
                return

            # 只有真正发房号的消息才做权限判定与日志(避免无关消息刷屏)
            self.logger.debug(f"[权限] 会话={'私聊' if is_private else '群聊'} 群号={gid or '-'} 发送者={uid or '-'} 模式={self.config.get('mode')}")
            if not self._is_allowed(is_private, uid, gid):
                self.logger.debug(f"[权限] 发送者={uid} 群号={gid or '-'} 命中名单, 已拦截查房")
                return
            if is_private:
                gid = uid  # 后续日志/回传沿用 gid 变量(此处为用户ID)

            self.logger.debug(f"群 {gid} 提取到房号: {rids}")

            # ---- 冷却判定 + 探针分配 (全部同步代码, 单线程 asyncio 下天然原子, 无需加锁) ----
            now = time.time()
            tasks = []  # (rid, idx, task)
            # 定期清理冷却字典, 防止长期运行无限膨胀
            if not self.room_cooldown or self._last_cleanup is None or now - self._last_cleanup > 300:
                stale = [rid for rid, ts in self.room_cooldown.items() if now - ts > COOLDOWN * 2]
                for rid in stale:
                    del self.room_cooldown[rid]
                self._last_cleanup = now
            to_query = []
            try:
                max_per_msg = max(1, int(self.config.get("probe_max_per_msg", 5)))
            except (TypeError, ValueError):
                max_per_msg = 5
            for rid in rids:
                last = self.room_cooldown.get(rid, 0)
                if now - last >= COOLDOWN:
                    self.room_cooldown[rid] = now
                    to_query.append(rid)
                if len(to_query) >= max_per_msg:
                    break
            if not to_query:
                self.logger.debug("所有房号均在冷却中")
                return

            self.logger.debug(f"待查询房号: {to_query}")

            for rid in to_query:
                idx = self._acquire_probe()
                if idx is None:
                    # 所有探针均被占用: 跳过该房号本轮查询(保持冷却中, 下轮消息再触发)
                    self.logger.debug(f"探针池已满, 跳过房号 {rid}")
                    continue
                base_name = f"ABAB探针{idx:02d}"
                # 短时间标识: 编码"月日时分秒"为5位base36, 可逆解回, 避免过长的数字后缀被过滤
                probe_name = f"{base_name}_{self._short_ts()}"
                task = asyncio.create_task(self._query_room(rid, probe_name))
                self.pending_tasks.add(task)
                task.add_done_callback(self.pending_tasks.discard)
                tasks.append((rid, idx, task))
            if not tasks:
                return

            released = set()  # 已释放的探针编号, 避免 finally 重复释放
            try:
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
                                await self._send_room_result(event, result)
                            elif is_name_check_error(result):
                                # 探针名被过滤: 自动改名重试
                                max_retry = int(self.config.get("retry_times", 3))
                                retried = 0
                                while retried < max_retry:
                                    retried += 1
                                    # 重试过程静默, 只在 debug 日志留痕(避免刷屏)
                                    self.logger.debug(
                                        f"房间 {rid} 探针名被过滤, "
                                        f"重试({retried}/{max_retry})")
                                    # 重试时去掉时间后缀, 直接 _序号 最短, 最不容易被过滤
                                    # 用当前任务的 idx 构造, 避免 for 循环残留的 base_name 串号
                                    new_name = f"ABAB探针{idx:02d}_{retried}"
                                    retry_result = ""
                                    retry_task = None
                                    try:
                                        retry_task = asyncio.create_task(self._query_room(rid, new_name))
                                        self.pending_tasks.add(retry_task)
                                        retry_task.add_done_callback(self.pending_tasks.discard)
                                        retry_result = await asyncio.wait_for(retry_task, timeout=QUERY_TIMEOUT)
                                    except asyncio.TimeoutError:
                                        retry_result = ""
                                    finally:
                                        # 确保重试任务被取消并回收, 避免协程泄漏
                                        if retry_task is not None and not retry_task.done():
                                            retry_task.cancel()
                                            try:
                                                await retry_task
                                            except (asyncio.CancelledError, Exception):
                                                pass
                                    if is_room_info(retry_result):
                                        await self._send_room_result(event, retry_result)
                                        break
                                    if not is_name_check_error(retry_result):
                                        if is_reportable_error(retry_result):
                                            yield event.plain_result(
                                                translate_error(retry_result))
                                        else:
                                            self.logger.debug(
                                                f"房间 {rid} 重试失败(静默): "
                                                f"{retry_result[:120]}")
                                        break
                                else:
                                    # 重试次数用尽: 仅当失败原因值得回传才发
                                    if is_reportable_error(result):
                                        yield event.plain_result(
                                            translate_error(result))
                                    else:
                                        self.logger.debug(
                                            f"房间 {rid} 重试耗尽(静默): "
                                            f"{result[:120]}")
                            elif is_error_info(result):
                                # 仅"房间确实存在"类错误回传, 其余静默
                                if is_reportable_error(result):
                                    self.logger.info(
                                        f"房间 {rid} 查询失败，回传原因")
                                    yield event.plain_result(
                                        translate_error(result))
                                else:
                                    self.logger.debug(
                                        f"房间 {rid} 查询失败(静默): "
                                        f"{result[:120]}")
                            else:
                                self.logger.debug(f"房间 {rid} 无有效信息, 静默丢弃")
                            released.add(idx)  # 先标记再释放, 确保 finally 不重复释放
                            self._release_probe(idx)
                            remaining.remove(t)
                            break
            finally:
                # 取消所有尚未完成的查询任务并释放对应探针 (跳过已释放的, 避免重复)
                for rid, idx, task in tasks:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    if idx not in released:
                        self._release_probe(idx)
                        released.add(idx)
        except Exception as e:
            self.logger.error(f"rwinfo on_all_message error: {e}\n{traceback.format_exc()}")

    async def terminate(self):
        """插件卸载时取消所有待执行的查询任务与延迟撤回任务，避免资源泄漏。"""
        # bin 清理循环是长驻任务, 必须先取消, 否则卸载后仍在后台运行
        if self._bin_cleanup_task is not None:
            self._bin_cleanup_task.cancel()
            try:
                await self._bin_cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._bin_cleanup_task = None
        for task in list(self.pending_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self.pending_tasks.clear()
        for task in list(self.pending_recall_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self.pending_recall_tasks.clear()
        self.pending_recall_tasks.clear()