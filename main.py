#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AstrBot 插件: 铁锈战争房间查询 (rwinfo)
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request

from astrbot.api.all import *
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# ================================================================ 探针脚本定位
# 只在插件自身目录查找 room_info.py。
#
# 不能向上一级目录回退：AstrBot 的标准布局是
# `<data>/plugins/astrbot_plugin_rwinfo/`，回退会指向 `<data>/plugins/`，
# 那是所有插件的公共父目录，既找不到目标，也可能误用他插件的同名文件。
ROOM_INFO_API_REQUIRED = 2          # 需要的最低接口版本（对应 room_info.py 的 ROOM_INFO_API）
ROOM_INFO_DIR = _PLUGIN_DIR
ROOM_INFO_PY = os.path.join(_PLUGIN_DIR, "room_info.py")

QUERY_TIMEOUT = 120
COOLDOWN = 10
PROBE_MIN, PROBE_MAX = 1, 99

# 插件版本（菜单与元数据展示用，与 metadata.yaml 的 version 保持一致）
PLUGIN_VERSION = "2.0.0"

# ---- 协议侧常量（与 room_info.py 保持一致）----
# 官方 masterserver 列表。插件侧解析中继房真实地址时要用，
# 与 room_info.py 的 MASTERSERVERS 是同一组服务，改动时两处同步。
MASTERSERVERS = [
    "http://gs1.corrodinggames.com/masterserver/1.4",
    "http://gs4.corrodinggames.net/masterserver/1.4",
]
# 上报给 masterserver 的客户端版本号（对应游戏 1.15）。
# 不能用 PLUGIN_VERSION 替代：这是协议协商用的游戏版本，与插件版本无关。
CLIENT_VER = 176

# 项目地址（推广用，硬编码常量，不通过配置修改）
REPO_URL = "https://github.com/wcnmamrs/astrbot_plugin_rwinfo"

# 进房推广的默认文案。
# **必须带完整 https://** —— 游戏内聊天窗口只把带协议头的串识别成链接，
# 写成 github.com/xxx 玩家点不动、也不好复制。
_PROMOTE_DEFAULT = f"本房信息由 astrbot_plugin_rwinfo 提供 · {REPO_URL}"

# ================================================================ 自动补文件（GitHub）
# 安装包里缺了 room_info.py（或它太旧、不支持插件传的新参数）时，
# 从仓库拉一份补齐。
#
# 写入范围约束：
#   1. 只写 ROOM_INFO_ALLOWED 白名单里的文件，不遍历目录、不批量同步
#   2. 目标路径必须在 _PLUGIN_DIR 之内（realpath + commonpath 双重校验，
#      挡住 `..` / 绝对路径等穿越写法）
#   3. 不删除任何已有文件 —— 旧文件改名为 .bak 保留，可手工回滚
REPO_RAW_BASE = "https://raw.githubusercontent.com/wcnmamrs/astrbot_plugin_rwinfo/main"
# 允许自动补齐的文件（相对插件目录）。
# 只放「缺了插件就跑不起来、且内容完全由仓库掌管」的运行文件。
# 不含 main.py / _conf_schema.json：这两个是用户实际安装的版本标识，
# 替换它们等同于绕过插件管理器做静默升级。
ROOM_INFO_ALLOWED = ("room_info.py",)
# 下载源（按顺序尝试，第一个成功即用）。
#
# 为什么需要多个源：实测在中国大陆网络下
#   raw.githubusercontent.com  -> Connection reset by peer
#   cdn.jsdelivr.net           -> Connection reset by peer
#   而 ghproxy 镜像和 api.github.com 是通的。
# 只写死官方 raw 地址在部分网络环境下会失败，所以做成有序回退。
_DOWNLOAD_SOURCES = (
    # ① 国内可用性最好：ghproxy 反向代理（直接拼在 raw 地址前）
    "https://ghproxy.net/" + REPO_RAW_BASE + "/{path}",
    # ② GitHub Contents API（返回 base64，需单独处理，标记 api:）
    "api:https://api.github.com/repos/wcnmamrs/astrbot_plugin_rwinfo/contents/{path}",
    # ③ 直连 raw（海外/有代理时最快）
    REPO_RAW_BASE + "/{path}",
    # ④ jsDelivr CDN（部分网络环境可用，有缓存但还算及时）
    "https://cdn.jsdelivr.net/gh/wcnmamrs/astrbot_plugin_rwinfo@main/{path}",
)
_DOWNLOAD_TIMEOUT = 15


def _fetch_once(url, timeout=_DOWNLOAD_TIMEOUT):
    """取一个 URL 的文本内容。支持 `api:` 前缀走 GitHub Contents API。

    Contents API 返回 JSON，内容是 base64 编码；其余源返回裸文本。
    """
    is_api = url.startswith("api:")
    real = url[4:] if is_api else url
    req = urllib.request.Request(
        real, headers={
            "User-Agent": f"astrbot-rwinfo/{PLUGIN_VERSION}",
            # API 要求显式 Accept；raw 源带这个也无害
            "Accept": "application/vnd.github+json" if is_api else "*/*",
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if not is_api:
        return raw.decode("utf-8")
    obj = json.loads(raw.decode("utf-8"))
    if isinstance(obj, dict) and obj.get("content"):
        import base64
        return base64.b64decode(obj["content"]).decode("utf-8")
    raise ValueError("API 返回不含 content")


def _download_text(rel_path, timeout=_DOWNLOAD_TIMEOUT):
    """按 `_DOWNLOAD_SOURCES` 顺序下载文本，全部失败返回 None。

    每个源内部再重试 2 次（应对偶发抖动）。只要有一个源成功就立刻返回，
    不会把四个源都跑满 —— 正常情况下第一个源就成功。
    """
    errors = []
    for tpl in _DOWNLOAD_SOURCES:
        url = tpl.format(path=rel_path)
        label = url.split("/")[2] if "//" in url else url[:24]
        for attempt in range(2):
            try:
                txt = _fetch_once(url, timeout)
                if txt and txt.strip():
                    logger.info(f"[补文件] 已从 {label} 获取 {rel_path}")
                    return txt
                errors.append(f"{label}: 内容为空")
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}")
                if attempt == 0:
                    time.sleep(0.6)
    logger.warning(f"[补文件] 所有源均失败: {'; '.join(errors[-6:])}")
    return None


def _room_info_relname():
    """探针脚本相对插件目录的文件名。

    集中一处，避免下载、校验、写盘三个环节各写一遍字面量而漂移。
    """
    return os.path.basename(ROOM_INFO_PY)


def _safe_target_dir(rel_path):
    """解析目标路径并校验它确实落在插件目录内。

    返回 None 表示校验不通过（路径穿越/绝对路径等），调用方必须放弃写入。
    这一步是「绝不误删/误写其他目录」的硬保障。
    """
    if not rel_path or os.path.isabs(rel_path) or ".." in rel_path.split("/"):
        return None
    root = os.path.realpath(_PLUGIN_DIR)
    target = os.path.realpath(os.path.join(root, rel_path))
    # commonpath 相等才说明 target 在 root 之内（含 root 自身）
    try:
        if os.path.commonpath([root, target]) != root:
            return None
    except ValueError:      # 不同盘符等
        return None
    return target


def _probe_api_version(py_path):
    """读本地 room_info.py 声明的接口版本。

    优先用子进程跑 `--api-version`（最准，能反映实际可执行行为）；
    老版本没有这个开关时会打印帮助文本，解析失败即视为版本 0。
    子进程失败（解释器缺失等）再退回正则扫源码里的 `ROOM_INFO_API = n`。
    """
    python = sys.executable or "python3"
    try:
        out = subprocess.run(
            [python, "-B", py_path, "--api-version"],
            capture_output=True, text=True, timeout=15, cwd=ROOM_INFO_DIR,
        )
        s = (out.stdout or "").strip().splitlines()
        if s and s[-1].strip().isdigit():
            return int(s[-1].strip())
    except Exception:
        pass
    try:
        with io.open(py_path, "r", encoding="utf-8", errors="ignore") as f:
            m = re.search(r"^ROOM_INFO_API\s*=\s*(\d+)", f.read(), re.M)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def _py_syntax_ok(py_path):
    """粗检一个 .py 文件语法是否完整（能被 compile 通过）。

    用途：`room_info.py` 没有 `ROOM_INFO_API` 标记时，用它区分
    「只是旧版」和「文件被下坏了」。旧版能编译 -> 当作可用；
    截断/乱码会抛 SyntaxError -> 判定为损坏，需要重新下载。
    """
    try:
        with io.open(py_path, "r", encoding="utf-8", errors="strict") as f:
            src = f.read()
        compile(src, py_path, "exec")
        return True
    except (OSError, SyntaxError, ValueError):
        return False


# ================================================================ 合并转发解包
# 第三方机器人（OneBot/NapCat）开启「消息压缩」后，长内容按合并转发发送
# （`send_group_forward_msg`，群里显示为一张「聊天记录」卡片）。
# 官方机器人（qq_official）收到这种消息时拿不到原始 node 结构，
# 适配器会把整张卡片渲染成一段带标记的纯文本，形如：
#
#     [铁锈查房的聊天记录]
#     === 消息 1 ===
#     [消息内容] 【房间信息】
#     [发送者] 铁锈查房
#     === 消息 2 ===
#     [消息内容] 房间版本：1.15
#     地图名称：...
#     [发送者] 铁锈查房
#
# 这层包装会让下游全部失配：
#   · 房间信息格式失配 -> 识别不到
#   · `is_room_info()` 首行不是「房间版本：」-> 房间信息被当普通聊天
#   · `extract_room_ids()` 反而可能从帮助文本里捞出假房号
#
# 因此在**所有后续判断之前**先剥掉包装，还原成发送方原本的正文。
_FORWARD_HEAD_RE = re.compile(r'^\s*\[[^\]]*的聊天记录\]\s*$')
_FORWARD_MSG_RE = re.compile(r'^\s*===\s*消息\s*\d+\s*===\s*$')
_FORWARD_FIELD_RE = re.compile(r'^\s*\[(消息内容|发送者)\]\s?(.*)$')


def extract_text_from_event(event, base: str = "") -> str:
    """尽最大努力从事件里取出「真实正文」。

    为什么需要它：AstrBot 的 `message_str` 只是适配器的**渲染结果**，
    不同适配器差异极大 —— 第三方（aiocqhttp）拿到合并转发时会渲染成
    `[xxx的聊天记录] / === 消息 N ===` 文本；官方适配器（qq_official）
    对某些元素类型可能干脆给空串，或只给一个占位标题。

    因此按「信息量优先」依次尝试：
      1. 传入的 base（调用方已取的 message_str）
      2. 事件对象里的原始消息段（message_obj / raw_message / data）
         递归找出所有可读文本，包括 forward/node 的嵌套 content
      3. 都拿不到则返回 base

    返回的文本都会过一遍 `unwrap_forward_text`。
    """
    def _walk(obj, depth=0):
        """递归收集对象里所有像「文本」的值。"""
        if depth > 6 or obj is None:
            return []
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, (int, float, bool)):
            return []
        out = []
        if isinstance(obj, dict):
            # 优先取文本字段，避免把 type/uuid 等元数据当成正文
            for key in ("content", "text", "message", "data", "messages",
                        "msg", "node", "nodes", "raw_message", "message_str"):
                if key in obj:
                    out.extend(_walk(obj[key], depth + 1))
            return out
        if isinstance(obj, (list, tuple)):
            for it in obj:
                out.extend(_walk(it, depth + 1))
            return out
        # 任意对象：扫常见属性
        for key in ("content", "text", "message", "messages", "raw_message",
                    "data", "message_str"):
            v = getattr(obj, key, None)
            if v is not None:
                out.extend(_walk(v, depth + 1))
        return out

    cands = [base or ""]
    for attr in ("message_obj", "message", "raw_message", "message_str"):
        try:
            v = getattr(event, attr, None)
        except Exception:
            v = None
        if v is not None:
            cands.extend(_walk(v))

    # 取「信息量最大」的那份：优先能识别出内容特征的，其次取最长的
    best = ""
    best_score = -1
    for c in cands:
        if not isinstance(c, str) or not c.strip():
            continue
        txt = unwrap_forward_text(c)
        score = 0
        if "房间版本：" in txt or "版本：" in txt:
            score += 100
        if "ID：" in txt:
            score += 50
        score += min(len(txt) // 20, 40)
        if score > best_score:
            best_score, best = score, txt
    return best or (base or "")


def unwrap_forward_text(text: str) -> str:
    """把「聊天记录」卡片的渲染文本还原成发送方正文。

    识别特征（全部满足才认为是卡片，避免误伤普通聊天）：
      · 首行形如 `[xxx的聊天记录]`
      · 出现 `=== 消息 N ===` 分段标记

    还原结果：丢弃 `[发送者]` 行与分段标记，只保留各段 `[消息内容]`
    之后的行，按原顺序拼回（段与段之间用换行分隔）。
    不是卡片则原样返回。
    """
    if not text:
        return text
    lines = str(text).splitlines()
    if not any(_FORWARD_HEAD_RE.match(L) for L in lines[:3]):
        return text
    if not any(_FORWARD_MSG_RE.match(L) for L in lines):
        return text
    out = []
    for L in lines:
        if _FORWARD_HEAD_RE.match(L) or _FORWARD_MSG_RE.match(L):
            continue
        m = _FORWARD_FIELD_RE.match(L)
        if m:
            field, rest = m.group(1), m.group(2)
            if field == "发送者":
                continue                 # 丢弃发送者行
            if rest:
                out.append(rest)         # [消息内容] xxx -> xxx
                continue
            continue                     # 空的 [消息内容] 行
        out.append(L)
    res = "\n".join(out).strip()
    return res or text


async def _run_in_thread(fn, *args):
    """把同步函数放到线程里跑，兼容 Python 3.8（无 asyncio.to_thread）。

    `ensure_room_info` 里会起子进程探测版本、并发网络下载，
    直接在事件循环里调会卡住其他群的消息处理。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


def ensure_room_info(auto=True):
    """确保 room_info.py 存在且接口版本够新，不够就从 GitHub 补。

    触发条件（**只有这两种**，不做任何自动更新）：
      · 文件不存在
      · 文件存在但 `--api-version` < ROOM_INFO_API_REQUIRED
        （说明插件本体更新了，旧的探针不支持新参数）

    返回 (ok, message)：
      · ok=True  文件可用（原本就在 / 已下载补齐）
      · ok=False 不可用，message 说明原因，调用方应直接告诉用户
    """
    path = ROOM_INFO_PY

    if os.path.exists(path):
        ver = _probe_api_version(path)
        if ver >= ROOM_INFO_API_REQUIRED:
            return True, ""
        if ver == 0:
            # 版本号读不到 —— 有两种可能：
            #   a) 这是本插件发布前的旧版 room_info.py（没有 ROOM_INFO_API）
            #   b) 文件损坏
            # **不能一律当成"太旧"**：仓库分支可能还没同步到带版本号的新版，
            # 那样会每次查房都重新下载一遍，白白消耗流量和时间。
            # 折中：只要文件语法能解析（能被 Python 编译），就认为可用；
            # 真的缺参数时，调用方会拿到服务的报错，用户也能看到具体原因。
            if _py_syntax_ok(path):
                logger.info(
                    "[补文件] room_info.py 无接口版本标记，按旧版兼容使用；"
                    "如需新参数请更新插件包")
                return True, ""
        # 版本太旧：需要补齐。auto=False 时不动手，只报告
        reason = (f"本地 room_info.py 接口版本 {ver} < "
                  f"需要 {ROOM_INFO_API_REQUIRED}")
    else:
        reason = "本地缺少 room_info.py"

    if not auto:
        return False, reason

    logger.info(f"[补文件] {reason}，尝试从仓库下载")
    txt = _download_text(_room_info_relname())
    if not txt:
        return False, f"{reason}，且从 GitHub 下载失败（检查网络后重试）"

    rel = _room_info_relname()
    if rel not in ROOM_INFO_ALLOWED:      # 白名单校验，防止写入范围失控
        return False, f"{reason}，但目标任务不在允许列表内，已放弃写入"

    target = _safe_target_dir(rel)
    if not target:
        return False, f"{reason}，但目标路径校验未通过，已放弃写入"

    # 写临时文件再原子替换；旧文件改名 .bak 保留（不删除）
    bak = target + ".bak"
    moved_old = False
    try:
        tmp = target + ".new"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        if os.path.exists(target):
            bak = target + ".bak"
            try:
                if os.path.exists(bak):
                    os.remove(bak)      # 只删我们自己生成的 .bak
                os.replace(target, bak)
                moved_old = True
            except OSError:
                pass
        os.replace(tmp, target)
    except Exception as e:
        logger.error(f"[补文件] 写入 {rel} 失败: {e}")
        # 清理残留临时文件（同样是插件目录内的自有文件）
        try:
            if os.path.exists(target + ".new"):
                os.remove(target + ".new")
        except OSError:
            pass
        # 回滚：旧文件已改名 .bak 但新文件没就位时，必须还原，
        # 否则插件目录里就没有 room_info.py 了
        if moved_old and os.path.exists(bak) and not os.path.exists(target):
            try:
                os.replace(bak, target)
                logger.info(f"[补文件] 已从 .bak 还原旧版 {rel}")
            except OSError as e2:
                logger.error(f"[补文件] .bak 还原失败: {e2}")
        return False, f"{reason}，且写入失败: {e}"

    # 校验下载结果确实可用
    ver = _probe_api_version(target)
    if ver < ROOM_INFO_API_REQUIRED and not (ver == 0 and _py_syntax_ok(target)):
        return False, (f"已下载 room_info.py 但接口版本仍为 {ver}，"
                       f"可能仓库分支也不是新版，请手动更新插件")
    logger.info(f"[补文件] room_info.py 已补齐（接口版本 {ver or '旧版兼容'}）")
    return True, ""

# 合并转发卡片的标题（按内容类别区分，让收件人一眼看出是什么）
_FOLD_TITLES = {
    "list": "房间列表",
    "help": "帮助菜单",
    "diag": "调试信息",
    "room": "房间信息",
}

# 合并转发不可用时附在正文末尾的提示（{reason} 填具体原因）
_FOLD_FAIL_TIP = (
    "（折叠卡片发送失败：{reason}。"
    "请让机器人管理员确认适配器支持合并转发，否则内容将以完整文本展示。）"
)

# 指令唤醒词默认值。实际使用值从配置项「指令唤醒词」读取。
# AstrBot 的唤醒词是全局配置，插件 API 读不到，所以需要在插件配置里
# 单独填一次，两者保持一致。
# 影响范围：卡片按钮 data、帮助/用法文案、未知指令兜底。
DEFAULT_CMD_PREFIX = "/"

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

# 实测确认可用的中继首字母（5123 端口开放）。用于**调度排序**，
# 不是识别白名单：未列出的字母照样识别、照样可查，只是排在后。
#   a b h   → 官方中继 (Relay US1 / US2 / ASIA2)
#   o q r s → 第三方中继 (RELAY-CN / Tiexiu.xyz / Fancy-CN)
#   c g j v → 有 DNS 但端口未开，查了也连不上，与英文词同级排最后
# 排序目的：英文聊天里会同时冒出多个纯字母候选（players / can / type …），
# 若按出现顺序截断，真房号可能被挤出 probe_max_per_msg 名额。
# 让「已知可达」的字母优先占名额，大幅降低真房号被无关词挤掉的概率。
_RELAY_REACHABLE = frozenset("abhoqrs")


def _relay_priority(rid: str) -> int:
    """房号的中继可达性优先级（越小越优先占探针名额）。

    0 = 已知可达；其余 = 不可达或非房号。
    数字房（o/q/r/s 开头）天然属于 0。
    """
    head = (rid or "")[:1].lower()
    if head in _RELAY_REACHABLE:
        return 0
    return 1


# 数字房号: 2 位以上数字 (整串 >= 3 字符)
#   o12 / q99 / r5132 / s1024
#
# **宽松匹配 + 后处理归一化**，用于兼容用户的手滑：
#   · rr5132      -> 前缀字母重复敲，归一化为 r5132
#   · r5132r5132  -> 房号连写两遍，归一化后去重 -> 一个 r5132
#   · s1024r5132  -> 两个不同房号连写（中间无空格）-> 都识别
# 因此正则不再锚定边界，改为在 extract_room_ids 里逐个检查左边界。
RELAY_NUM_LOOSE_RE = re.compile(r'[oqrsOQRS]+\d{2,}')
_RELAY_NUM_HEAD_RE = re.compile(r'^([oqrsOQRS]+)(\d+)$')
# 字母房号: 非 oqrs 的字母开头 + 纯字母 (整串 >= 3 字符)
#   hab / abc / hytvvi / aabcde
# 额外排除 URL / 域名片段: 紧邻 . / : @ = - ? # & 的纯字母串不是房号
#   如 https://example.com 里的 https / example / com 都不匹配，
#   foo-bar 也不会被拆成 foo + bar。
# 只加 URL/标点类边界，不影响纯字母房号（hello / hlbifz 照常识别）。
RELAY_ALPHA_RE = re.compile(
    r'(?<![A-Za-z0-9._:/@=\-?#&])(?![oqrsOQRS])[A-Za-z]{3,}'
    r'(?![A-Za-z0-9._:/@=\-?#&])')

# 不识别纯数字房号(如 5232): 首字符为数字的中继主机无 DNS, 无法查询。
# 不识别混合格式(如 a1b2c3): 不在实测的两种形态内, 排除以防误报。
# 不识别 IP 直连: 自动触发时不连 IP(无法判断是 Web 还是游戏服务器);
#   仅 /铁锈查房 显式指定 IP 时才允许, 由 IP_QUERY_RE 匹配。
# 不设英文单词黑名单: 字母房号随机, 可能恰好是 "hello" / "game";
#   黑名单会误杀真实房间, 合法性判定交给探针(查不到自然失败)。

# 显式查房时允许的 IP / IP:端口 形式(自动触发不使用)
IP_QUERY_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?$')


def _split_relay_run(head: str, digits: str):
    """把「前缀字母串 + 数字串」拆成若干个合法房号。

    兼容用户的手滑写法：
      · "rr"   + "5132"      -> ["r5132"]        前缀字母重复
      · "r"    + "5132"      -> ["r5132"]        正常
      · "r"    + "51325132"  -> ["r5132"]        数字重复（同一房号连写两遍）
      · "rs"   + "10245132"  -> ["r1024","s5132"] 两个房号连写（按前缀出现顺序等分）

    拆分规则：
      1. 前缀字母串逐字符剥离（每个字符是一个候选中继字母），
         **仅去掉相邻重复**（"rr" -> ["r"]；"rsr" 仍是 ["r","s","r"]）
      2. 前缀字母数 == 1 时，数字全给这一个房号
      3. 前缀字母数 > 1 时，数字串按 `len(digits)//n` **固定等分**，
         按前缀出现顺序配对；数字不够分（< 2*n）则只认第一个字母 + 全部数字

    注：`r5132r5132` 不走本函数 —— 它由 RELAY_NUM_LOOSE_RE 匹配两次
        再由 extract_room_ids 的 seen 去重，最终只留一个 r5132。
    """
    letters = []
    for ch in (head or ""):
        low = ch.lower()
        if low and (not letters or letters[-1] != low):
            letters.append(low)
    digits = digits or ""
    if not letters or len(digits) < 2:
        return []
    # 单个前缀：整段数字都归它。
    #
    # 「数字重复」手滑（r51325132 -> r5132）只在**总长 ≥8 且半段 ≥4** 时判。
    # 门槛不能再低：6 位的 r123123 / s111111 是合法房号，
    # 按 6 位判会把它们截成 r123 / s111，查错房且静默。
    # 游戏房号通常 3~5 位，8 位纯数字房几乎不存在，
    # 因此这个门槛对真实房号无风险。
    #
    # 注意 r5132r5132（同一房号连写两遍）**不走这里** ——
    # 它由 RELAY_NUM_LOOSE_RE.finditer 匹配两次 + seen 去重完成。
    if len(letters) == 1:
        d = digits
        if len(d) >= 8 and len(d) % 2 == 0:
            half = len(d) // 2
            if half >= 4 and d[:half] == d[half:]:
                return [letters[0] + d[:half]]
        return [letters[0] + d]
    # 多个前缀：需要把数字串切分成等量的几段，每段 >= 2 位
    n = len(letters)
    if len(digits) < 2 * n:
        # 数字不够分，退化：只认第一个字母 + 全部数字
        return [letters[0] + digits]
    # 贪心切分：每段至少 2 位，尽量均分（前面的段稍短，符合 r5132r5132 这类）
    seg_len = len(digits) // n
    out = []
    pos = 0
    for i, letter in enumerate(letters):
        if i == n - 1:
            seg = digits[pos:]
        else:
            seg = digits[pos:pos + seg_len]
            pos += seg_len
        if len(seg) >= 2:
            out.append(letter + seg)
    return out


def extract_room_ids(text: str):
    """从文本中提取所有房间号, 按出现顺序去重返回(小写)。

    支持的房号形式(按中继区分, 实测):
      1. 数字房: o/q/r/s + 2 位以上数字, 整串 >= 3 字符
         如 r5132 / s1024 / o9999 / q7777
      2. 字母房: 非 o/q/r/s 的字母开头 + 纯字母, 整串 >= 3 字符
         如 hytvvi / aabcde / babc / cabcde

    **兼容用户手滑的连写/重复写法**:
      · r5132r5132  -> r5132（同一房号连写两遍，去重）
      · rr5132      -> r5132（前缀字母重复）
      · s1024r5132  -> s1024、r5132（两个房号无分隔连写）

    不识别: 纯数字 / 混合格式(a1b2c3) / IP / u_ 前缀 / URL 片段。
    不做英文单词过滤: 字母房号随机, 可能恰好是 hello/game,
    合法性由探针验证(查不到即失败, 不影响正常房间)。
    """
    ids = []
    seen = set()

    def add(rid):
        rid = str(rid or "").lower()
        if rid and rid not in seen:
            seen.add(rid)
            ids.append(rid)

    rest = text or ""

    # 1) 数字房(oqrs + 数字)：宽松匹配，再归一化拆分
    for m in RELAY_NUM_LOOSE_RE.finditer(rest):
        start = m.start()
        # 左边界：紧邻「非 oqrs 字母」才说明是字母房号的一部分（如 abc123）。
        # 若紧邻数字或 oqrs 字母，那是连写的上一个房号的尾巴（s1024r5132），
        # 不能跳过 —— 这正是「连写」要能识别的关键。
        if start > 0:
            prev = rest[start - 1]
            if prev.isalpha() and prev.lower() not in "oqrs":
                continue
        # 右边界：紧邻「非 oqrs 字母」才说明是字母房号的一部分（如 r5132abc）。
        # 若紧跟 oqrs 字母，那是下一个连写房号的前缀（s1024r5132），不能跳过。
        end = m.end()
        if end < len(rest):
            nxt = rest[end]
            if nxt.isalpha() and nxt.lower() not in "oqrs":
                continue
        mm = _RELAY_NUM_HEAD_RE.match(m.group(0))
        if not mm:
            continue
        for rid in _split_relay_run(mm.group(1), mm.group(2)):
            add(rid)
    # 从文本中移除已识别的数字房，避免其数字部分干扰字母房匹配
    rest = RELAY_NUM_LOOSE_RE.sub(" ", rest)

    # 2) 字母房(非 oqrs 开头的纯字母)
    for m in RELAY_ALPHA_RE.finditer(rest):
        add(m.group(0))

    return ids


# 房间信息字段名（用于归一化匹配）。
# 只放「我们自己会输出」的字段；用于判断探针返回的是不是有效房间信息
# （is_room_info 在查房结果校验处被调用，决定回传结果还是报错）。
# 注意：不要把 `版本` 和 `房间版本` 同时放进来 ——
# `房间版本` 里含 `版本`，同一行会被计成两次命中，
# 于是用户随便打一句「房间版本是多少」就能凑够 2 个字段。
# 统一用 `版本` 覆盖（它同时匹配新旧两种标签）。
_ROOM_FIELDS = ("版本", "地图名称", "初始资金", "初始禁核",
                "初始迷雾", "地图模式", "默认单位", "单位上限",
                "共享控制", "允许旁观", "队伍锁定", "当前人数")

# 把各种平台差异抹平：
#   · markdown 修饰符  *  `  ~
#   · 表格分隔符       |
#   · 半角冒号         :  ->  ：
#   · 冒号后的空白
# 只做归一化，不改动“有多少个字段”这个事实。
_ROOM_NOISE_RE = re.compile(r'[*`~|]')

def _normalize_room_text(text: str) -> str:
    """把文本归一化成「字段名+N 个字符」的扁平串，便于找字段。

    只做与判定有关的归一化，不改变内容：
      · 删掉 markdown / 表格的装饰字符
      · 半角冒号统一成全角（表格里根本没冒号，不影响）
      · 全角空格、制表符压成普通空格
    """
    s = _ROOM_NOISE_RE.sub('', str(text or ''))
    s = s.replace(':', '：').replace('　', ' ').replace('	', ' ')
    return s

# 值起始字符：冒号、数字、字母、常见符号。
# 用来区分「字段名 + 值」与「句子里恰好提到这个词」。
_VALUE_HEAD = set('：0123456789abcdefghijklmnopqrstuvwxyz'
                  'ABCDEFGHIJKLMNOPQRSTUVWXYZ#-/¤(')

def _field_has_value(s: str, name: str) -> bool:
    """字段名是否以「键 + 值」的形式出现，而不是被当作普通词语。

    判据：字段名之后 0~3 个字符内出现冒号或值的起始字符。

    为什么需要这层：光看「包含字段名」会把闲聊误判成房间信息。
    实测：「这个版本的地图名称好听」同时包含 `版本` 和 `地图名称`，
    若只数命中个数就会被当成房间信息 —— 用户随口一句话就能污染
    名单。

    正常输出都满足这个判据：
        房间版本：1.15        -> 后面是冒号
        地图名称：Crossing    -> 后面是冒号
        | 房间版本 | 1.15 |   -> 归一化成 `房间版本  1.15`，后面是空格+数字
    """
    start = 0
    while True:
        i = s.find(name, start)
        if i < 0:
            return False
        tail = s[i + len(name): i + len(name) + 4]
        for ch in tail:
            if ch in _VALUE_HEAD:
                return True
        start = i + 1

def is_room_info(text: str) -> bool:
    """判断一段文本是不是「房间信息」（用于探针返回结果校验）。

    判定标准：**归一化后同时命中至少两个房间字段**。

    为什么是两个而不是一个：
      · 要求 1 个 -> 用户随口打一句「房间版本」就可能被误判。
      · 要求 2 个 -> 伪造成本明显提高，而正常输出通常有十几个字段，
        远远够用。

    为什么要归一化：官Q 的 Markdown 会把 `键：值` 清单渲染成表格
    （`| 房间版本 | 1.15 |`），此时**冒号消失**；第三方转发时也可能
    把全角冒号换成半角。旧实现只认原文里的 `房间版本：`，这些情况
    一律漏判。

    兼容旧版标签：`版本：1.15` 也算（老版本插件仍在用）。
    """
    if not text:
        return False
    s = _normalize_room_text(text)
    hit = 0
    for name in _ROOM_FIELDS:
        if _field_has_value(s, name):
            hit += 1
            if hit >= 2:
                return True
    return False

def fingerprint_room_id(text: str) -> str:
    """从查房结果里取房号, 仅用于日志与调试展示。"""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("ID："):
            return s[3:].strip()
    return ""

# ================================================================ 探针名前缀
# 探针在游戏内显示为「<前缀>探针NN_xxxxx」，前缀可由用户配置（如 ABAB）。
PROBE_PREFIX_DEFAULT = "ABAB"
PROBE_PREFIX_MAXLEN = 4
# 允许的字符：中英文、数字、下划线、连字符。
# **故意不放空格和标点**：探针名要经过房间列表、玩家列表、聊天显示，
# 空格/引号/控制字符在任意一环都可能被转义或截断，导致进房被服务端
# 判成非法名字（Name Check）。宁可限制严格，也不让用户配出进不去的名字。
_PROBE_PREFIX_OK = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_\-]+$")


def sanitize_probe_prefix(raw) -> str:
    """把用户配置的探针前缀清洗成安全值。

    规则（按顺序）：
      1. 转字符串、去首尾空白
      2. 丢掉所有不允许的字符（标点/空格/控制符/emoji）
      3. 截断到 PROBE_PREFIX_MAXLEN 个字符
      4. 空结果 -> 回退默认值

    **按字符而非字节截断**：中文在 UTF-8 里占 3 字节，按字节切会产生
    半个字符的乱码，直接进不了房间。
    """
    s = str(raw or "").strip()
    # 逐字符过滤，保留合法字符
    s = "".join(ch for ch in s if _PROBE_PREFIX_OK.match(ch))
    if len(s) > PROBE_PREFIX_MAXLEN:
        s = s[:PROBE_PREFIX_MAXLEN]
    return s or PROBE_PREFIX_DEFAULT


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


# 只有「房间确实存在」类的失败原因才回传;
# 其余(房间不存在 / 中继未响应 / 节点限制 等)一律静默。
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
    """规范化群号/用户号（用于名单匹配）。

    ⚠️ 必须完整保留非纯数字标识！
    官方机器人的群号/用户 openid 是 32 位十六进制哈希（如
    "3A28BF3EF5FF4F023DB209648BB7921E"）。

    早期实现用 `re.search(r"\\d+")` 抽第一个数字段，会把上面这个串
    截成 "3"，于是：
      · 白名单里手填的是完整 "3A28BF3EF5FF..."（用户看到的是对的）
      · 运行时比对用的却是 "#3"
      · 结果永远不匹配 -> 明明填了白名单，命令却回「没有权限」
    这是一个隐蔽的匹配失效 bug，只在官方机器人（哈希串群号）下暴露，
    普通数字 QQ 群完全正常，所以长时间没被发现。

    现在的规则：
      · 纯数字（"3856554669"）-> 原样返回
      · 其他（哈希串等）      -> 去空白后完整返回
    """
    if gid is None:
        return ""
    s = str(gid).strip()
    if not s:
        return ""
    # 纯数字：传统 QQ 群号 / QQ 号
    if s.isdigit():
        return s
    # 含数字但不全是数字（如 "123abc"）或纯哈希串：完整保留，绝不截断
    return s


class RWInfoPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # **独立 logger，绝不用框架共享的 `logger` 单例改级别**。
        # 早期用 `from astrbot.api import logger` 并 setLevel，
        # 会把整个 AstrBot 实例（所有插件 + 内核）的日志级别一起改掉。
        # 现在用独立命名的 logger，只影响本插件自己的输出密度。
        self.logger = logging.getLogger("astrbot_plugin_rwinfo")
        # 防止重复初始化 handler（插件重载时 logger 单例还在）
        self.logger.propagate = False
        if not self.logger.handlers:
            _h = logging.StreamHandler()
            _h.setFormatter(logging.Formatter(
                "[%(asctime)s.%(msecs)03d] [%(name)s] [%(levelname)s] "
                "%(message)s", datefmt="%H:%M:%S"))
            self.logger.addHandler(_h)
        self.config = config or self._default_config()

        # 配置读取视图：AstrBot 嵌套结构不动顶层，业务统一读 self.cfg
        self._conf_groups = self._schema_groups()
        self.cfg = self._build_flat_config()
        # 探针脚本接口是否已确认达标。
        # 初始为 False：首次查房时触发一次版本探测，
        # 版本够则 ensure_room_info 秒返回 True，开销可忽略；
        # 过旧则真正走「从仓库补齐」。
        # 若初始就置 True，接口过旧的旧脚本永远得不到补齐。
        # 启动自检：扫被截断的官方机器人哈希群号并点名
        self._migrate_broken_list_entries()

        # 配置来源自检：读不到 schema 时分组识别会失效，
        # 虽然 _build_flat_config 已能兜底解析嵌套结构，
        # 但仍记一条日志，便于排查「面板改了却不生效」这类问题。
        if not self._conf_groups:
            self.logger.warning(
                "[配置] 未能识别分组结构（_conf_schema.json 读取失败），"
                "已按嵌套/平铺自动兜底解析。若配置修改不生效请检查该文件是否存在。")
        elif self.cfg.get("debug", False):
            self.logger.debug(
                f"[配置] 识别到 {len(self._conf_groups)} 个分组，"
                f"schema 路径: {self._schema_path()}")
        # 关键配置自检：无条件打印，这是排查「明明是官Q模式却发成纯文本」
        # 最直接的入口 —— 只要看这行就知道插件当时读到的到底是什么值。
        try:
            self.logger.info(
                f"[配置] 官Q模式={bool(self.cfg.get('官Q模式'))} "
                f"压缩范围={self.cfg.get('压缩范围')!r} "
                f"唤醒词={self.cfg.get('指令唤醒词')!r} "
                f"显示项目地址={bool(self.cfg.get('显示项目地址', True))} "
                f"分组数={len(self._conf_groups)}")
        except Exception:
            pass

        # 旧平铺配置 -> 分组结构的一次性迁移。
        # 老版本配置是 {"mode": "black", "白名单": [...]} 全在顶层，
        # 换了分组 schema 后这些顶层键在面板上会被当成「额外配置项」列出，
        # 于是底部出现一堆重复的原始条目。这里把它们搬进对应分组并删掉顶层副本。
        self._migrate_to_groups()

        # 旧配置迁移: 无前缀的纯数字名单条目统一转成 #群号 (兼容 v1.2.9 及更早)
        self._migrate_list("白名单")
        self._migrate_list("黑名单")
        self._migrate_list("混合_白名单")
        self._migrate_list("混合_黑名单")
        # 撤回配置迁移: "群撤回"/"私聊撤回" 两个列表 -> 单一 "撤回" 列表,
        # 格式从 "key:开:延迟" 简化为 "key:延迟"
        self._migrate_recall()

        if self.cfg.get("debug", False):
            self.logger.setLevel(logging.DEBUG)
            self.logger.debug("Debug 模式已启用")
        else:
            self.logger.setLevel(logging.INFO)

        self.room_cooldown = {}
        # 「本机确实查到过房间信息」的记录: {"会话|房号": ts}
        # 只在 _send_room_result（即 is_room_info 为真）时写入。
        # 冷却提示只认这个 —— 英文单词 game/hello 查不到信息，
        # 永远不会被记录，因此永远不会触发提示。
        self._room_ok = {}
        self._last_cleanup = None
        # 本机 QQ 号（合并转发消息需要 uin，取不到则用占位值）
        self._self_qq = ""
        self._self_qq_warned = False
        self.probe_used = set()
        self.probe_released = []
        self.probe_next = PROBE_MIN
        self.pending_tasks = set()
        self.pending_recall_tasks = set()
        # 撤回任务明细: {id(task): {"key","mid","due","delay"}}, 供调试指令展示
        self._recall_meta = {}
        # bin 缓存定时清理任务(插件启动后开始循环)
        self._bin_cleanup_task = None
        self._start_bin_cleanup()

    # ================================================================ bin 缓存清理
    def _bin_cleanup_conf(self):
        """读取并矫正 bin 清理配置, 返回 (enabled, interval_sec, max_age_sec)."""
        enabled = bool(self.cfg.get("bin_cleanup_enabled", True))
        try:
            interval_h = float(self.cfg.get("bin_cleanup_interval_hours", 24))
        except (TypeError, ValueError):
            interval_h = 24.0
        try:
            max_age_h = float(self.cfg.get("bin_cleanup_max_age_hours", 1))
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
        if self.cfg.get("debug", False) and out:
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
            "global_enabled": True,           # 全局开关：关闭后自动查房不触发（显式指令仍可用）
            "mode": "black",                  # 模式：black=黑名单放行 / white=白名单放行 / mixed=混合模式(@优先)
            "白名单": [],                     # 白名单列表（black/white模式下使用）
            "黑名单": [],                     # 黑名单列表（black/white模式下使用）
            "混合_白名单": [],                # 混合模式专用白名单（@用户/#群 独立管理）
            "混合_黑名单": [],                # 混合模式专用黑名单（@用户/#群 独立管理）
            "混合_私聊策略": "white",         # 混合模式私聊默认策略：white=不在白名单拦 / black=在黑名单拦
            "私聊启用": True,                 # 是否处理私聊消息（关闭则私聊不发房号）
            "debug": False,
# 官Q模式：一键接管排版（Markdown 卡片 + 快捷按钮 + 换行规整）。
            # 合并了旧版的「官机适配 / 消息格式 / 快捷按钮」三个开关。
            "官Q模式": False,
            # 压缩范围：none / room / diag / all
            # 官方机器人用 Markdown 引用块视觉折叠；个人机器人改用合并转发。
            "压缩范围": "none",
            # 指令唤醒词：必须与 AstrBot 主配置的唤醒词一致。
            # 插件 API 读不到主配置，所以只能在这里单独填。
            # 影响按钮 data、帮助文案、未知指令兜底。
            "指令唤醒词": "/",
            # 是否展示项目地址：官Q=卡片底部按钮；非官Q=消息末尾虚线小字。
            "显示项目地址": True,
            # 调试指令菜单式展示（支持分类查看）
            "调试菜单": True,
            "max_players_display": 10,
            "show_players": False,
            "retry_times": 3,
            # ---- 房间列表 ----
            "list_page_size": 5,              # 列表指令每页显示数量
            "list_default_full": "",          # 列表默认人数筛选: 空=全部 / hide=隐藏人满
            "probe_max_per_msg": 5,           # 单条消息最多召唤的探针数
            # ---- bin 缓存清理 ----
            "bin_cleanup_enabled": True,      # 是否在插件启动后定时清理 bin 缓存
            "bin_cleanup_interval_hours": 24, # 清理间隔(小时), 默认一天一次
            "bin_cleanup_max_age_hours": 1,   # 只清理超过该小时数(默认1小时)的 bin
            # 撤回配置: 单一列表, 由 key 前缀区分群/用户, 写入即启用
            #   "#123456:60"      群 123456, 消息发出 60 秒后撤回
            #   "@3856554669:30"  用户 3856554669 私聊, 30 秒后撤回
            #   "*:60"            全局默认(仅群聊; 具体群配置优先)
            # 关闭 = 删掉该条(可用 /铁锈撤回 关)
            "撤回": [],
            # ---- 房号冷却 ----
            # 防止同一会话里反复发同一个房号刷请求。按「会话+房号」独立计时,
            # 不同群互不影响(早期是全局共享, A 群查过的房会让 B 群静默无响应)。
            "房号冷却秒": 10,
            # 冷却中是否回一条提示。默认关(完全静默)。
            "冷却提示": False,
        }

    def _save_config(self):
        """保存配置到磁盘。

        AstrBot 的配置对象自身持有 save_config()，而 self.config 就是它传进来的
        同一个 dict（分组结构原样保留），所以直接调用即可。
        调用前用 _sync_config_from_view() 把 self.cfg 中被原地修改过的值
        同步回去，避免 list 增删等操作只改到视图。
        """
        try:
            self._sync_config_from_view()
            if hasattr(self.config, "save_config"):
                self.config.save_config()          # AstrBot 的 AstrBotConfig
            elif hasattr(self, "_config_saver"):
                self._config_saver()
        except Exception as e:
            self.logger.warning(f"保存配置失败: {e}")

    def _sync_config_from_view(self):
        """把 self.cfg 中的值同步回 self.config（分组结构）。

        有些路径会原地修改列表（名单增删、撤回配置增删），
        虽然 list 通常是共享引用、能自动同步，但为保险起见统一回写一遍。
        """
        if not isinstance(self.config, dict):
            return
        for k, v in self.cfg.items():
            self._write_config(k, v)

    def _migrate_to_groups(self):
        """把旧的顶层平铺配置搬进分组，并删除顶层副本。

        背景：换用 object 分组 schema 后，AstrBot 会把 schema 里未定义的
        顶层键当作「额外配置项」列在面板底部。老配置恰好就是全平铺的，
        于是会看到一堆重复的原始条目。这里做一次性迁移：

            {"mode": "black", "白名单": [...]}
                ->  {"基础设置": {"mode": "black"},
                     "名单管理": {"白名单": [...]}}

        只在「该键属于某个分组，且分组里还没有它」时搬运，
        不会覆盖用户在面板上新设的值。
        """
        if not isinstance(self.config, dict) or not self._conf_groups:
            return
        # 建立 key -> group 索引
        owner = {}
        for group, keys in self._conf_groups.items():
            for k in keys:
                owner[k] = group

        moved = False
        for k in list(self.config.keys()):
            if k in self._conf_groups:        # 本身就是分组，跳过
                continue
            group = owner.get(k)
            if not group:                     # 不属于任何分组，保留原样
                continue
            body = self.config.get(group)
            if not isinstance(body, dict):
                body = {}
                self.config[group] = body
            # 分组里没有才搬（用户面板上设过的值优先）
            if k not in body:
                body[k] = self.config[k]
            del self.config[k]
            moved = True

        if moved:
            # 同步读取视图，然后把迁移结果落盘
            self.cfg = self._build_flat_config()
            self._save_config()
            self.logger.info("[配置] 已把旧平铺配置迁移到分组结构")

    def _schema_path(self) -> str:
        """定位 _conf_schema.json 的路径。

        **实测问题**：
          [WARN] [配置] 未能识别分组结构（_conf_schema.json 读取失败）
        后果：分组识别失败 -> 配置项全退回 _default_config() 的默认值
        -> 「官Q模式」读成 False -> 消息走纯文本通道（无折叠、无按钮）。

        AstrBot 把插件放在 data/plugins/ 下，但实际目录层级可能多一层
        （zip 安装解包出 `astrbot_plugin_rwinfo/astrbot_plugin_rwinfo/`），
        或者 __file__ 指向的位置与实际文件不在一处。
        `room_info.py` 那边早就踩过这个坑并加了父目录后备（见模块头部），
        schema 这里却只试了单层 —— 这就是不一致的地方。

        现在按可靠性排序，并为每一层都补上「父目录 / 子目录」兜底。
        """
        cands = self._schema_candidates()
        for p in cands:
            try:
                if p and os.path.exists(p):
                    return p
            except Exception:
                continue
        try:
            self.logger.warning(
                f"[配置] _conf_schema.json 未找到，已尝试: {cands[:8]}"
            )
        except Exception:
            pass
        return cands[0] if cands else "_conf_schema.json"

    def _schema_candidates(self):
        """返回 _conf_schema.json 的全部候选路径（供诊断日志用）。"""
        cands = []

        def add(d):
            if not d:
                return
            try:
                p = os.path.join(d, "_conf_schema.json")
                if p not in cands:
                    cands.append(p)
            except Exception:
                pass

        here = ""
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            add(here)
        except Exception:
            pass
        try:
            add(_PLUGIN_DIR)
        except Exception:
            pass
        try:
            add(os.path.dirname(ROOM_INFO_PY))
        except Exception:
            pass
        bases = []
        for b in (here, globals().get("_PLUGIN_DIR", ""),
                  os.path.dirname(globals().get("ROOM_INFO_PY", ""))):
            if b and b not in bases:
                bases.append(b)
        for base in bases:
            try:
                add(os.path.dirname(base))
            except Exception:
                pass
            try:
                for name in os.listdir(base):
                    sub = os.path.join(base, name)
                    if os.path.isdir(sub):
                        add(sub)
            except Exception:
                pass
        try:
            add(os.getcwd())
        except Exception:
            pass
        return cands

    def _load_schema(self, cache=True):
        """读取 schema（带内存缓存，只读一次）。读不到返回 {}。"""
        if cache and getattr(self, "_schema_cache", None) is not None:
            return self._schema_cache
        data = {}
        path = self._schema_path()
        try:
            with io.open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                data = d
        except Exception as e:
            # 原来是 logger.debug —— AstrBot 默认日志级别 INFO，
            # 于是「文件不存在 / JSON 坏了 / 编码异常」全部静默，
            # 只剩下一句「未能识别分组结构」，完全无法定位。
            # 这里必须打出来，否则线上永远查不出原因。
            try:
                import traceback
                self.logger.warning(
                    f"[配置] 读取 _conf_schema.json 失败: {path} | "
                    f"{type(e).__name__}: {e} | 存在={os.path.exists(path)}")
                self.logger.warning(f"[配置] 候选路径: {self._schema_candidates()[:8]}")
                self.logger.debug(traceback.format_exc())
            except Exception:
                pass
        if cache:
            self._schema_cache = data
        return data

    def _schema_groups(self) -> dict:
        """返回 {分组名: [组内键, ...]}。

        分组结构有两个来源，优先用磁盘 schema，读不到就从配置自身推断：

        **来源 ①（磁盘 schema）**：顶层 `type == object` 的项。
        **来源 ②（配置自身，兜底）**：AstrBot 传进来的 `config` 本身
        就是嵌套结构（`{"基础设置": {...}, "名单管理": {...}}`），
        只要某个顶层键的值是 dict、且键名不在默认配置里，
        它就是分组。这条兜底很关键 —— 实测线上 `_conf_schema.json`
        读不到（`分组数=0`）时，`官Q模式` 仍然能正确读出 True，
        正是因为 `_build_flat_config` 的嵌套兜底在起作用。
        分组识别也该有同样的韧性，否则 `_write_config` 会把值写到
        顶层去，面板上改配置就不生效了。
        """
        schema = self._load_schema()
        groups = {}
        for name, body in schema.items():
            if isinstance(body, dict) and body.get("type") == "object":
                items = body.get("items")
                if isinstance(items, dict) and items:
                    groups[name] = list(items.keys())
        if groups:
            return groups
        # ---- 来源 ②：从配置自身推断分组 ----
        try:
            if isinstance(self.config, dict):
                known = set(self._default_config().keys())
                for k, v in self.config.items():
                    if isinstance(v, dict) and k not in known and v:
                        groups[k] = list(v.keys())
        except Exception:
            pass
        return groups

    def _build_flat_config(self) -> dict:
        """构造平铺的配置读取视图（业务代码统一用 self.cfg）。

        优先级（后者覆盖前者）：
          1. _default_config() 的默认值
          2. schema 声明的 default（保证与面板显示一致）
          3. 磁盘配置的平铺键（旧版本配置，直接就在顶层）
          4. 磁盘配置的分组内键（新版本 object 分组）

        self.config 本身始终保持 AstrBot 的原样结构，不做任何提升，
        这样配置面板不会多出未定义的「额外配置项」。
        """
        flat = dict(self._default_config())

        # 2. schema 的 default（用缓存，避免每次读盘）
        schema = self._load_schema()
        for name, body in schema.items():
            if isinstance(body, dict) and body.get("type") == "object":
                items = body.get("items") or {}
            elif isinstance(body, dict):
                items = {name: body}      # 未分组时 schema 是平铺的
            else:
                continue
            for k, v in items.items():
                if isinstance(v, dict) and "default" in v:
                    flat[k] = v["default"]

        if not isinstance(self.config, dict):
            return flat

        # 3. 平铺键（旧配置）：跳过已知分组名和「值为 dict 的非配置项」
        known_keys = set(flat.keys())
        for k, v in self.config.items():
            if k in self._conf_groups:
                continue
            if isinstance(v, dict) and k not in known_keys:
                continue
            flat[k] = v

        # 4. 分组内的键（新配置）：值为 dict 的分组把键值提升上来
        groups = dict(self._conf_groups)
        if not groups:
            for k, v in self.config.items():
                if isinstance(v, dict) and k not in known_keys:
                    groups[k] = list(v.keys())
        for group, keys in groups.items():
            body = self.config.get(group)
            if isinstance(body, dict):
                for k in keys:
                    if k in body:
                        flat[k] = body[k]
        return flat

    def _write_config(self, key: str, value):
        """写入配置：同时更新读取视图和 AstrBot 的原始配置结构。

        业务代码通过 self.cfg 读取，通过本方法写入。
        写入会落到分组内（若该键属于某个分组），保证面板能正确显示。
        注意：本方法内部只能操作 self.config，不能操作 self.cfg 的嵌套结构。
        """
        self.cfg[key] = value
        if not isinstance(self.config, dict):
            return
        for group, keys in self._conf_groups.items():
            if key in keys:
                body = self.config.get(group)
                if not isinstance(body, dict):
                    body = {}
                    self.config[group] = body
                body[key] = value
                return
        # 不属于任何分组 -> 直接写顶层（旧平铺结构）
        self.config[key] = value

    def _migrate_list(self, key: str):
        """旧配置迁移: 无前缀的纯数字条目转成 #群号; 清理 @@/##/混合前缀的脏数据."""
        lst = self.cfg.get(key, [])
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

    def _plausible_ident(self, v) -> str:
        """过滤非身份值（qq_official 等平台名），返回规范化字符串，否则 ''。"""
        if v is None:
            return ""
        s = str(v).strip()
        if not s or s == "0":
            return ""
        low = s.lower()
        # 平台名 / 适配器名 / 明显的非身份单词
        bad = ("qq_official", "qqofficial", "official", "qq", "adapter",
               "aiocqhttp", "napcat", "unknown", "none", "null")
        if low in bad:
            return ""
        if "official" in low or "adapter" in low:
            return ""
        # 统一小写：不同适配器对 openid 的大小写可能不一致，
        # 归一化后便于稳定比较。
        return s.lower()

    def _resolve_self_qq(self, event=None) -> str:
        """取合并转发/调试展示用的本机标识（官方机器人常为 openid 哈希）。

        依次读事件 self_id → 配置「机器人QQ号」→ 本机占位标识，
        全部经 _plausible_ident 过滤（去掉 qq_official 等平台名）。
        """
        sid = ""
        if event is not None:
            sid = self._plausible_ident(getattr(event, "self_id", None))
        if sid:
            self._self_qq = sid
            return sid

        cfg_qq = self._plausible_ident(
            str(self.cfg.get("机器人QQ号", "") or "").strip())
        if cfg_qq:
            self._self_qq = cfg_qq
            return cfg_qq

        cached = self._plausible_ident(self._self_qq)
        if cached:
            return cached

        placeholder = self._self_placeholder()
        if not self._self_qq_warned:
            self._self_qq_warned = True
            self.logger.info(
                f"未取到真实 QQ 号, 使用占位标识 {placeholder} "
                f"(仅影响合并转发的显示名)")
        return placeholder

    def _self_placeholder(self) -> str:
        """本机占位标识：仅在拿不到真实身份时用于合并转发显示名。"""
        tag = getattr(self, "_self_tag", "")
        if not tag:
            tag = "self-" + format(int(time.time()) & 0xFFFFFF, "06x")
            self._self_tag = tag
        return tag

    def _is_plugin_ui_text(self, text: str) -> bool:
        """判断这条消息是不是插件自己发出的「界面文本」。

        特征：文本里出现插件固定的标题行，例如
          · 【铁锈查房调试】
          · 【铁锈查房插件指令】
          · 【铁锈查房调试菜单】
          · 【铁锈查房调试 · xxx】

        这类内容含真实房号和会误判成字母房号的英文单词
        （如 s5132 / black），绝不能被当成用户的房号请求。

        **不用 startswith 而用「标题行出现」**：跨框架收消息时，正文
        可能被包进引用前缀（如 `[引用消息(...)] 正文`）或 Markdown，
        开头不一定就是标题。
        """
        t = (text or "").strip()
        if not t:
            return False
        for head in ("【铁锈查房调试】", "【铁锈查房调试 ·",
                     "【铁锈查房插件指令】", "【铁锈查房调试菜单】"):
            if head in t:
                return True
        return False

    def _is_admin(self, event) -> bool:
        """判断当前发送者是否管理员（同步，异常一律判否）。

        **必须带 self**：调用点是 ``self._is_admin(event)``，定义为实例方法后
        Python 会把 self 绑到第一个形参上，少写 self 就等于少一个形参 ——
        调用时直接 TypeError: _is_admin() takes 1 positional argument but 2
        were given。/铁锈查房调试 的「当前会话」节会因此整条崩掉。
        """
        try:
            val = getattr(event, "is_admin", None)
            if val is None:
                return False
            got = val() if callable(val) else val
            if asyncio.iscoroutine(got):
                got.close()
                return False
            return bool(got)
        except Exception:
            return False

    # ---- 撤回配置: 单一列表, 格式 "#群号:延迟" / "@用户ID:延迟" / "*:延迟" ----
    # 设计说明(相对旧版的简化):
    #   · 旧版拆成 "群撤回" / "私聊撤回" 两个列表 —— 但 key 本身带 #/@ 前缀
    #     已经能区分群与用户, 拆两列纯属冗余。
    #       · 旧版每条是 "key:开:延迟", 而"写进配置"本身就意味着"要开启",
    #     那个 开 字段永远是 1, 纯冗余。现在只剩 key 和延迟两段。
    #   · 想要"关闭", 直接删掉该条即可 —— 列表里没有 = 不启用。

    RECALL_DELAY_DEFAULT = 60
    RECALL_DELAY_MAX = 300
    # 合法的会话 key: #群号(数字或官方机器人哈希) / @用户 / *(全局默认)
    _RECALL_KEY_RE = re.compile(r"^(?:#[0-9A-Za-z]+|@\w+|\*)$")

    def _recall_store(self) -> list:
        """取出撤回配置列表(容器). 单一列表, 群/私聊混合存放, 由 key 前缀区分。"""
        store = self.cfg.get("撤回", [])
        return store if isinstance(store, list) else []

    def _recall_parse(self, entry):
        """解析单条撤回配置。

        新格式: "key:延迟"         例 "#123456:60" / "@3856554669:30" / "*:60"
        显式关闭: "key:off"        例 "#123456:off"(延迟记为 -1，覆盖 "*" 继承)
        旧格式: "key:开:延迟"      例 "#123456:1:60"(兼容迁移, 开=0 视为已关闭返回 None)

        返回 {"key": str, "延迟": int} 或 None(格式非法 / 旧格式里标记为关闭)。
        延迟为 -1 表示「显式关闭」，只由 key:off 产生。

        严格校验:
          · key 必须匹配 #数字 / @标识 / *
          · 延迟必须是 0-300 的整数, 非整数一律视为非法(不再静默取默认值)
        """
        s = str(entry or "").strip()
        if not s:
            return None
        parts = [p.strip() for p in s.split(":")]
        if len(parts) < 2:
            return None

        key = parts[0]
        if not self._RECALL_KEY_RE.match(key):
            return None

        # 兼容旧格式 "key:开:延迟"
        if len(parts) >= 3:
            flag = parts[1].lower()
            on = flag in ("1", "开", "true", "on", "yes", "y")
            if not on:
                # 旧格式里显式关闭的条目: 视为"不存在", 调用方当它没配置
                return None
            delay_raw = parts[2]
        else:
            delay_raw = parts[1]

        # 显式关闭条目: "key:off"(也接受「关」)。用途只有一个 —— 群聊里存在
        # "*" 默认时，用它在单个群上盖掉继承，否则「关」在继承场景下无处落笔。
        if delay_raw.lower() in ("off", "关", "关闭"):
            return {"key": key, "延迟": -1}

        try:
            delay = int(delay_raw)
        except (TypeError, ValueError):
            return None
        if delay < 0 or delay > self.RECALL_DELAY_MAX:
            return None
        return {"key": key, "延迟": delay}

    def _recall_entries(self) -> list:
        """返回所有合法配置 [(key, 延迟), ...]。非法条目自动跳过。

        延迟为 -1 的条目是「显式关闭」，也一并返回，由调用方决定怎么用。
        """
        out = []
        for entry in self._recall_store():
            cfg = self._recall_parse(entry)
            if cfg:
                out.append((cfg["key"], cfg["延迟"]))
        return out

    def _get_recall_cfg(self, event, want_wildcard=False) -> dict:
        """读取当前会话生效的撤回配置, 返回 {"开": bool, "延迟": int}。

        匹配顺序: 精确 key 优先 -> 群聊回退通配符 "*"。
        私聊不使用通配符(防止一条 "*" 影响所有私聊用户)。
        列表里查不到 = 未启用。
        精确 key 命中"显式关闭"(-1)时直接返回未启用，**不再回退**通配符。
        """
        key, is_private = self._recall_session_key(event)
        wildcard_delay = None
        for k, delay in self._recall_entries():
            if want_wildcard:
                if k == "*":
                    if delay < 0:
                        return {"开": False, "延迟": self.RECALL_DELAY_DEFAULT}
                    return {"开": True, "延迟": delay}
                continue
            if k == key:
                # -1 = 显式关闭，命中即结束，不再回退到 "*"
                if delay < 0:
                    return {"开": False, "延迟": self.RECALL_DELAY_DEFAULT}
                return {"开": True, "延迟": delay}
            if k == "*" and not is_private:
                wildcard_delay = delay
        if not want_wildcard and wildcard_delay is not None and wildcard_delay >= 0:
            return {"开": True, "延迟": wildcard_delay}
        return {"开": False, "延迟": self.RECALL_DELAY_DEFAULT}

    def _set_recall_cfg(self, event, delay=None, wildcard=False, remove=False):
        """写入/删除撤回配置。

        · delay=None 且 remove=False: 无操作
        · remove=True: 删掉当前会话(或 '*')对应的条目；
          本会话没有自己的条目、但正继承着 "*" 时，改写一条 "key:off"
          显式关闭记录（否则「关」在继承场景下写不下去）
        · delay 有值: 覆盖写入(已存在则替换延迟, 不存在则新增)

        写入即启用; 关闭请用 remove。
        """
        key, is_private = self._recall_session_key(event)
        if wildcard and not is_private:
            key = "*"
        if not key:
            return False

        store = self.cfg.get("撤回")
        if not isinstance(store, list):
            store = []
            self._write_config("撤回", store)

        # 找到该 key 的现有条目(按原始字符串位置)
        idx = None
        for i, entry in enumerate(store):
            cfg = self._recall_parse(entry)
            # 旧格式标记关闭的条目也算"占位", 一并清理
            raw_key = str(entry or "").split(":")[0].strip()
            if (cfg and cfg["key"] == key) or raw_key == key:
                idx = i
                break

        if remove:
            if idx is not None:
                store.pop(idx)
                self._save_config()
            # 删掉精确条目后，如果本群还会继承 "*" 默认，就补一条显式关闭记录。
            # 否则「关」只是把延迟退回默认值：用户看到的是"关完又自己开了"。
            # 读路径认 "*"、写路径只认精确 key —— 不对称就在这里。
            if (not wildcard and not is_private
                    and self._get_recall_cfg(event)["开"]):
                store.append(f"{key}:off")
                self._save_config()
                return True
            return idx is not None

        if delay is None:
            return False
        try:
            delay = max(0, min(self.RECALL_DELAY_MAX, int(delay)))
        except (TypeError, ValueError):
            return False

        new_entry = f"{key}:{delay}"
        if idx is None:
            store.append(new_entry)
        else:
            store[idx] = new_entry
        self._save_config()
        return True

    def _migrate_recall(self):
        """把旧的 "群撤回"/"私聊撤回" 两个列表合并成新的 "撤回" 单列表。

        旧格式 "#123:1:60" -> 新格式 "#123:60"
        旧格式 "#123:0:60" -> 丢弃(那是显式关闭, 新语义下"不在列表里"即关闭)
        迁移后删掉旧键, 避免配置里留一堆无效字段。
        """
        old_keys = ("群撤回", "私聊撤回")
        existing = self.cfg.get("撤回")
        if not isinstance(existing, list):
            existing = []
        merged = list(existing)
        changed = False

        for old_key in old_keys:
            entries = self.cfg.get(old_key)
            if not isinstance(entries, list) or not entries:
                if old_key in self.config:
                    self.config.pop(old_key, None)
                    changed = True
                continue
            for entry in entries:
                cfg = self._recall_parse(entry)
                if not cfg:
                    changed = True       # 旧格式里开=0 或非法条目, 丢弃
                    continue
                new_entry = f"{cfg['key']}:{cfg['延迟']}"
                if new_entry not in merged:
                    merged.append(new_entry)
                changed = True
            self.config.pop(old_key, None)
            changed = True

        if changed:
            self._write_config("撤回", merged)
            self._save_config()

    # ---- 通配符展开 ----
    async def _fetch_all_groups(self, event) -> list:
        """向平台主动拉取机器人加入的**全部**群号。
        只靠运行时状态推断只能拿到「见过」的群，这里主动拉全量群号。


        兼容多种适配器形态, 按可用性依次尝试:
          · OneBot v11:  bot.api.call_action("get_group_list", {"no_cache": True})
                        或 bot.call_api("get_group_list", ...)
          · 通用:        bot.get_group_list()
          · 部分适配器:  bot.api.get_group_list()
        返回 [群号字符串, ...], 失败返回 []。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            self.logger.debug("[撤回] 拉取群列表失败: event 里没有 bot 对象")
            return []

        raw = None
        tried = []

        # ---- ① OneBot v11 标准: call_action ----
        for holder_name, holder in (("bot.api", getattr(bot, "api", None)),
                                    ("bot", bot)):
            if holder is None:
                continue
            for attr in ("call_action", "call_api"):
                fn = getattr(holder, attr, None)
                if fn is None:
                    continue
                tried.append(f"{holder_name}.{attr}")
                try:
                    raw = fn("get_group_list", {"no_cache": True})
                    if asyncio.iscoroutine(raw):
                        # 加超时：适配器卡住时不能让 /铁锈撤回 + * 永久挂起
                        raw = await asyncio.wait_for(raw, timeout=10)
                    if raw:
                        break
                except Exception as e:
                    self.logger.debug(
                        f"[撤回] {holder_name}.{attr}('get_group_list') 失败: {e}")
                    raw = None
            if raw:
                break

        # ---- ② 直接方法: bot.get_group_list() ----
        if not raw:
            fn = getattr(bot, "get_group_list", None)
            if fn is not None:
                tried.append("bot.get_group_list")
                try:
                    raw = fn()
                    if asyncio.iscoroutine(raw):
                        raw = await asyncio.wait_for(raw, timeout=10)
                except Exception as e:
                    self.logger.debug(f"[撤回] bot.get_group_list() 失败: {e}")
                    raw = None

        if not raw:
            self.logger.debug(
                f"[撤回] 未能拉取群列表, 已尝试: {tried or '(无可用方法)'}")
            return []

        # ---- 归一化各种返回格式 ----
        items = raw
        if isinstance(raw, dict):
            # {"status":"ok","data":[{...}]} 或 {"data":{"group_list":[...]}}
            for k in ("data", "groups", "group_list", "result"):
                v = raw.get(k)
                if isinstance(v, list):
                    items = v
                    break
                if isinstance(v, dict):
                    for k2 in ("group_list", "groups", "list"):
                        if isinstance(v.get(k2), list):
                            items = v[k2]
                            break
                    if isinstance(items, list) and items is not v:
                        break
            else:
                items = []

        if not isinstance(items, list):
            self.logger.debug(f"[撤回] 群列表返回格式不认识: {type(raw).__name__}")
            return []

        out = []
        for it in items:
            gid = None
            if isinstance(it, dict):
                for k in ("group_id", "group", "id", "gid"):
                    if it.get(k) is not None:
                        gid = it[k]
                        break
            elif isinstance(it, (str, int)):
                gid = it
            if gid is None:
                continue
            s = normalize_gid(gid)
            if s:
                out.append(s)
        self.logger.debug(f"[撤回] 拉到 {len(out)} 个群: {out[:10]}")
        return sorted(set(out))

    async def _recall_all_groups(self, event) -> list:
        """展开用的完整群集合 = 平台拉取的全部群 ∪ 本地已知群。

        平台拉取失败(如适配器不支持)时退回本地已知群, 功能不至于完全不可用。
        """
        local = self._recall_known_groups(event)
        remote = []
        try:
            remote = await self._fetch_all_groups(event)
        except Exception as e:
            self.logger.debug(f"[撤回] 拉取群列表异常: {e}")
        merged = set(local) | set(remote)
        return sorted(g for g in merged if g)

    def _recall_known_groups(self, event=None) -> list:
        """已知群号集合, 供通配符 '*' 展开。

        来源(按可靠性排序):
          1. 当前会话的群号 —— 最先, 因为用户此刻就在这个群里发指令,
             这个群一定是"已知"的, 不该出现"0 个已知群"这种自相矛盾的提示
          2. 配置里的白名单/黑名单群条目(#群号) —— 用户明确列过的群
          3. 运行时状态 —— 房号冷却("g#群号|房号")、防重复指纹("g#群号")、
             列表翻页状态("g#群号@QQ号")
        """
        groups = set()

        def _take(s):
            s = str(s)
            if not s.startswith("g#"):
                return
            gid = s[2:].split("|")[0].split("@")[0]
            if gid:
                groups.add(gid)

        # 1. 当前会话的群号(最可靠)
        if event is not None:
            try:
                gid = normalize_gid(event.get_group_id())
                if gid:
                    groups.add(gid)
            except Exception:
                pass

        # 2. 配置里明确写过的群
        for ckey in ("白名单", "黑名单", "混合_白名单", "混合_黑名单"):
            for item in (self.cfg.get(ckey) or []):
                s = str(item).strip()
                if s.startswith("#") and len(s) > 1:
                    for part in s[1:].split("@")[:1]:
                        if part.strip():
                            groups.add(part.strip())

        # 3. 运行时状态
        for k in self.room_cooldown:
            _take(k)
        for k in self._list_page_state():
            _take(k)

        # 过滤掉空值
        return sorted(g for g in groups if g)

    def _recall_expand_wildcard(self, delay: int, event=None, groups=None) -> int:
        """把 '*' 展开成具体群条目的配置写入, 返回新增数量。

        groups: 要展开的群号列表。为 None 时退回本地已知群(不含平台拉取,
                因为本方法同步, 无法 await; 调用方应先用 _recall_all_groups
                拿到完整集合再传进来)。

        已存在同群条目 -> 更新延迟, 不重复添加。
        """
        store = self.cfg.get("撤回")
        if not isinstance(store, list):
            store = []
            self._write_config("撤回", store)

        if groups is None:
            groups = self._recall_known_groups(event)

        # 已有具体群条目: {群号: 列表索引}
        existing = {}
        for i, entry in enumerate(store):
            cfg = self._recall_parse(entry)
            if cfg and cfg["key"].startswith("#"):
                existing[cfg["key"][1:]] = i

        added = 0
        touched = False
        for gid in groups:
            new_entry = f"#{gid}:{delay}"
            if gid in existing:
                i = existing[gid]
                if store[i] != new_entry:
                    store[i] = new_entry
                    touched = True
            elif new_entry not in store:
                store.append(new_entry)
                added += 1
                touched = True

        # 保留 "*" 作为兜底: 展开出的具体条目只覆盖"现在已知"的群,
        # 将来新加入的群仍然靠 "*" 生效。两者同时存在时, 具体群条目优先
        # (_get_recall_cfg 的匹配顺序就是精确 key 优先)。
        if not any((self._recall_parse(e) or {}).get("key") == "*" for e in store):
            store.append(f"*:{delay}")
            touched = True

        if touched:
            self._save_config()
        return added

    def _recall_apply_batch(self, spec: str, add: bool) -> tuple:
        """处理 "/铁锈撤回 +|- key:延迟 ..." 批量操作。

        spec 里可以有多条(空格分隔); 每条必须是 "key:延迟" 或裸 "key"。
        add=True  新增/覆盖; add=False 删除(裸 key 也按删除处理)。
        返回 (成功数, 失败列表[(条目, 原因)])。
        """
        store = self.cfg.get("撤回")
        if not isinstance(store, list):
            store = []
            self._write_config("撤回", store)

        ok_n = 0
        fails = []
        for token in spec.split():
            token = token.strip()
            if not token:
                continue
            # 裸 key(无冒号) : 删除时合法, 新增时补默认延迟
            if ":" not in token:
                key_only = token
                if not self._RECALL_KEY_RE.match(key_only):
                    fails.append((token, "key 格式不对(需 #群号/@用户/*)"))
                    continue
                if add:
                    token = f"{key_only}:{self.RECALL_DELAY_DEFAULT}"
                else:
                    cfg = {"key": key_only}
                    idx = None
                    for i, entry in enumerate(store):
                        p = self._recall_parse(entry)
                        raw_key = str(entry or "").split(":")[0].strip()
                        if (p and p["key"] == key_only) or raw_key == key_only:
                            idx = i
                            break
                    if idx is None:
                        fails.append((token, "不存在"))
                    else:
                        store.pop(idx)
                        ok_n += 1
                    continue

            cfg = self._recall_parse(token)
            if cfg is None:
                fails.append((token, "格式不对(需 key:延迟, 延迟 0-300)"))
                continue

            key = cfg["key"]
            idx = None
            for i, entry in enumerate(store):
                p = self._recall_parse(entry)
                raw_key = str(entry or "").split(":")[0].strip()
                if (p and p["key"] == key) or raw_key == key:
                    idx = i
                    break

            if add:
                new_entry = f"{key}:{cfg['延迟']}"
                if idx is None:
                    store.append(new_entry)
                else:
                    store[idx] = new_entry
                ok_n += 1
            else:
                if idx is None:
                    fails.append((token, "不存在"))
                else:
                    store.pop(idx)
                    ok_n += 1

        if ok_n:
            self._save_config()
        return ok_n, fails

    def _dedup_key(self, event) -> str:
        """会话标识: 群聊按群号, 私聊按用户ID。

        用途有两个: 防重复指纹的归属、房号冷却的分组。
        同一会话内共享, 跨会话互不影响。
        """
        if event is None:
            # 全量清理时允许不传 event(all_sessions=True 分支不会用到 key)
            return ""
        try:
            if event.is_private_chat():
                return "p@" + normalize_gid(event.get_sender_id())
        except Exception:
            pass
        gid = normalize_gid(event.get_group_id())
        if gid:
            return "g#" + gid
        return "p@" + normalize_gid(event.get_sender_id())

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
                # 允许 /铁锈列表、/铁锈列表、@机器人 /铁锈列表 等前缀
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
        """发送房间查询结果。自动触发与显式 /铁锈查房 共用此路径。"""
        if len(result) > 3500:
            result = result[:3500] + "\n...(已截断)"

        rid = fingerprint_room_id(result) or "?"

        mid = await self._send_recallable(event, result)

        # 记录「本机确实查到过这个房号」，冷却提示只认这个标记。
        # 本方法只在 is_room_info(result) 为真时被调用，也就是探针真的
        # 拿到了房间信息；英文单词 game / hello 查不到信息，不会走到这里。
        try:
            ck = f"{self._dedup_key(event)}|{str(rid).lower()}"
            self._room_ok[ck] = time.time()
        except Exception as e:
            self.logger.debug(f"[查房] 结果记录失败(忽略): {e}")

        return mid

    def _official_channel_desc(self) -> str:
        """调试菜单里显示「官方 Markdown 通道是否可用」。

        只做静态探测：看当前保存的 bot 引用上有没有可用的 API 方法。
        探测不到时把具体原因写进返回值，避免「选了 markdown 却没效果」无从排查。
        """
        if not self.cfg.get("官Q模式", False):
            return "未启用（官Q模式=关）"
        bot = getattr(self, "_last_bot", None)
        if not getattr(self, "_bot_probed", False):
            return "待首条消息探测"
        if bot is None:
            # 探测过但拿不到 bot：适配器不通过 event 暴露 bot，
            # 这种情况靠猜方法名是无解的，必须靠 /铁锈官机探测 看现场。
            return "无 bot（用 /铁锈官机探测 查看）"
        fn, style, _ = self._official_api(bot)
        if fn is None:
            return "不可用（bot 上未找到官方 API）"
        return f"可用（{getattr(fn, '__name__', 'api')} / {style}）"

    def _as_int(self, v):
        """把群号/用户号转成 int；不是纯数字（openid 哈希串）时返回 None。

        OneBot 的 send_msg 只接受整数 ID，而官方机器人的 ID 是
        `A1B2C3...` 这种 openid 字符串，绝不能硬转。
        """
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        # 只接受纯数字（允许前导 + 号，防御性处理）
        if s.startswith("+"):
            s = s[1:]
        if not s.isdigit():
            return None
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    def _compress_scope(self) -> str:
        """当前压缩范围：none / room / diag / list / help / all。非法值按 none。"""
        try:
            v = str(self.cfg.get("压缩范围", "none") or "none").strip().lower()
        except Exception:
            v = "none"
        return v if v in ("none", "room", "diag", "list", "help", "all") else "none"

    def _compress_label(self) -> str:
        """压缩范围的中文标签，供调试菜单展示。"""
        return {
            "none": "不压缩",
            "room": "仅查房结果",
            "diag": "仅调试输出",
            "list": "仅房间列表",
            "help": "仅帮助信息",
            "all": "全部（覆盖前四项）",
        }.get(self._compress_scope(), "不压缩")

    def _should_compress(self, kind: str) -> bool:
        """判断某一类内容是否该压缩。

        kind:
          "room" 查房结果（单房详情，键值对）
          "list" 房间列表（分页清单，多条房间）
          "diag" 调试输出
          "help" 帮助与菜单

        all 覆盖全部四类，选了「全部压缩」即不再分别设置。

        注意 help 这一档容易被漏：帮助文本很长（40+ 行），
        是最需要折叠的内容之一。早期版本没接进来，
        导致选了 all 之后帮助依旧是一大坨。

        但 help 在官Q模式下又**不能折叠**（见 _compress_text 的例外），
        因为 QQ 官方 Markdown 不支持代码块，折叠会退化成纯文本。
        所以 help 的实际折叠只在个人机器人下发生。
        """
        scope = self._compress_scope()
        if scope == "all":
            return True
        return scope == kind

    def _fold_all(self, text: str) -> str:
        """整段包进 ```txt 代码块（不分段）。用于帮助菜单。"""
        if not text:
            return text
        lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        if len(lines) <= 6:
            return "\n".join(lines)
        body = "\n".join(ln.replace("```", "'''") for ln in lines)
        return f"```txt\n{body}\n```"

    def _fold_markdown(self, text: str) -> str:
        """把长文本做成「分段折叠」：前半留块外，后半进 ```txt 代码块。

        按空行分块，整块要么全在外、要么全进块，绝不从表格/列表中间切开。
        不足 6 行不折。
        """
        if not text:
            return text
        lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        if len(lines) <= 6:
            return "\n".join(lines)

        blocks = []
        cur = []
        for ln in lines:
            if ln.strip():
                cur.append(ln)
            else:
                if cur:
                    blocks.append(cur)
                    cur = []
        if cur:
            blocks.append(cur)
        if len(blocks) <= 1:
            return "\n".join(lines)

        keep = max(1, len(blocks) // 2)
        head_blocks = blocks[:keep]
        rest_blocks = blocks[keep:]

        head = []
        for b in head_blocks:
            head.extend(b)
        body_lines = []
        for b in rest_blocks:
            body_lines.extend(b)

        if not body_lines:
            return "\n".join(lines)
        body = "\n".join(ln.replace("```", "'''") for ln in body_lines)
        return "\n".join(head + ["", "```txt", body, "```"])

    def _strip_markdown(self, text: str, prefix: str = "") -> str:
        """把 Markdown 触发写法换成不会被渲染的等价写法。

        第三方机器人（OneBot / NapCat 等）的 QQ 客户端仍会解析消息里的
        Markdown 语法，即使插件只发纯文本。会被自动渲染的写法：

          · `|` 多次出现            -> 表格
          · `**x**`                 -> 粗体
          · ``` ``` ```             -> 代码块
          · 行首 4 个以上空格       -> 缩进式代码块
          · 行首 `> ` / `>`         -> 引用块
          · 行首 `- ` / `* ` / `+ `  -> 无序列表
          · 行首 `1. `              -> 有序列表
          · 行首 `# `               -> 标题
          · 连续 `---`              -> 分割线

        **行首缩进是最容易漏的一条**：room_info.py 输出的玩家列表是
        ` 玩家列表:` + `  A1: xxx`，正好在空行之后，QQ 会把它整体识别成
        缩进式代码块，消息里凭空多出一个「代码 / 复制 / 全屏」的框。
        这里统一把前导空格换成全角空格：视觉缩进保留，但不再触发解析。

        `prefix` 是当前唤醒词。它本身可能就是 Markdown 特殊字符（最常见的是
        `>`），这时行首的 `>铁锈列表` 会被 QQ 整行渲染成引用块、把 `>` 吃掉，
        用户看不见指令前缀。所以对行首唤醒词做全角替换：

          `>` -> `＞`   `!` -> `！`   `#` -> `＃`
          `-` -> `－`   `*` -> `＊`   `_` -> `＿`

        全角字符视觉几乎一致，但不触发任何 Markdown 规则。
        `/` 等非特殊字符保持原样（不影响复制粘贴）。
        """
        if not text:
            return text
        s = str(text)
        # 1. 竖线 -> 全角（表格触发）；粗体星号去掉
        s = s.replace("|", "｜")
        s = s.replace("**", "")
        # 2. 代码块围栏整行去掉
        out = []
        for ln in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            t = ln.strip()
            if t.startswith("```"):
                continue
            out.append(ln)
        s = "\n".join(out)
        # 3. 行首：缩进转全角；唤醒词 -> 全角；其余 Markdown 行首符 -> 等价字符
        full = {"!": "！", "#": "＃", "-": "－",
                "*": "＊", "_": "＿", ">": "＞", "+": "＋"}
        fixed = []
        for ln in s.split("\n"):
            # 3a. 前导缩进 -> 全角空格（保住缩进观感，不触发代码块）
            # 半角空格与制表符都会被 Markdown 当成缩进（\t 按 4 空格算），
            # 一律换成全角空格。
            lead = len(ln) - len(ln.lstrip(" \t"))
            if lead:
                raw = ln[:lead]
                n = sum(4 if ch == "\t" else 1 for ch in raw)
                ln = "\u3000" * n + ln[lead:]
            stripped = ln.lstrip()
            indent = ln[:len(ln) - len(stripped)]
            # 3b. 唤醒词本身是 Markdown 特殊字符：换成全角，保住指令可见性
            if prefix and prefix in full and stripped.startswith(prefix):
                ln = indent + full[prefix] + stripped[len(prefix):]
                stripped = ln[len(indent):]
            # 3c. 引用块
            if stripped.startswith(">"):
                ln = indent + "」 " + stripped[1:].lstrip()
                stripped = ln[len(indent):]
            # 3d. 有序列表 `1. ` -> `1．`
            # 房间列表里的 `1. 房间名　19/19` 是真实内容（含全角空格分栏），
            # QQ 不会把它当 Markdown 列表，保持原样即可。
            m = re.match(r"(\d+)\.\s+(.*)$", stripped)
            if m and "\u3000" not in stripped:
                ln = indent + m.group(1) + "．" + m.group(2)
            # 3e. 无序列表
            elif stripped[:2] in ("- ", "* ", "+ "):
                ln = indent + "· " + stripped[2:]
            # 3f. 标题 `# `
            elif stripped.startswith("# "):
                ln = indent + "＃ " + stripped[2:]
            # 3g. 分割线：连续 3+ 个短横线压到 3 个
            elif stripped and set(stripped) == {"-"} and len(stripped) > 3:
                ln = indent + "-" * 3
            fixed.append(ln)
        return "\n".join(fixed)

    def _compress_text(self, text: str, kind: str) -> str:
        """按当前配置压缩文本。

        只有官Q（有 Markdown 通道）才真正折叠：收进 ```txt 代码块。
        第三方机器人没有 Markdown，早期用点号 + 缩进假装折叠 —— 那既不
        减少长度，又把内容割开，已停用。第三方的折叠由 `_send_forward_card`
        （合并转发卡片）承担，不是改文本。
        """
        if not text or not self._should_compress(kind):
            return text
        try:
            if self.cfg.get("官Q模式", False):
                return self._fold_markdown(text)
        except Exception:
            pass
        return text

    async def _send_forward_card(self, event, text: str, title: str = "房间信息"):
        """非官方平台：把长内容发成「聊天记录」合并转发卡片。

        返回 (ok, mid, reason)：
          ok=True  —— 已发出（或异常但可能已送达），调用方不要再发纯文本
          ok=False —— 无法合并转发，reason 说明原因，调用方降级并提示

        为什么用合并转发：
          个人机器人（aiocqhttp / NapCat 等）走 OneBot v11，支持
          `send_group_forward_msg` 发「聊天记录」卡片，群里只占一条消息，
          点开才展开全部内容 —— 这是个人机器人唯一能做到真折叠的方式。
          官方机器人没有这个接口，那边用 Markdown 引用块代替（见 _fold_markdown）。

        实现细节：
          OneBot 的 node 有两种格式，不同实现支持的不一样，按顺序试：
            ① {"type":"node","data":{"name":..,"uin":..,"content":..}}  标准
            ② {"name":..,"uin":..,"content":..}                          简写
          都不行再放弃，交给调用方降级。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, None, "拿不到 bot 对象"
        fn = getattr(bot, "send_group_forward_msg", None)
        if not callable(fn):
            fn = getattr(bot, "send_forward_msg", None)
        if not callable(fn):
            return False, None, "当前适配器不支持合并转发"
        gid = self._as_int(event.get_group_id())
        if gid is None:
            return False, None, "私聊无法合并转发"
        # 拆成多条 node：标题 + 正文分段，让卡片里有多条记录可翻
        body = str(text).replace("\r\n", "\n").replace("\r", "\n")
        chunks = []
        cur = []
        size = 0
        for ln in body.split("\n"):
            cur.append(ln)
            size += len(ln) + 1
            if size >= 400:            # 单条 node 不超过约 400 字，避免超限
                chunks.append("\n".join(cur))
                cur, size = [], 0
        if cur:
            chunks.append("\n".join(cur))
        if not chunks:
            return False, None, "内容为空"
        nodes = []
        me = self._as_int(self._resolve_self_qq(event)) or 10000
        nodes.append({
            "type": "node",
            "data": {"name": "铁锈查房", "uin": me, "content": f"【{title}】"},
        })
        for ch in chunks:
            nodes.append({
                "type": "node",
                "data": {"name": "铁锈查房", "uin": me, "content": ch},
            })
        try:
            result = await fn(group_id=gid, messages=nodes)
            mid = self._extract_message_id(result)
            self.logger.info(f"[合并转发] 已发送 {len(nodes)} 条 node, mid={mid}")
            return True, mid, ""
        except Exception as e:
            # 先用简写格式再试一次（部分 OneBot 实现只认这个）
            try:
                simple = [{"name": n["data"]["name"], "uin": n["data"]["uin"],
                           "content": n["data"]["content"]} for n in nodes]
                result = await fn(group_id=gid, messages=simple)
                mid = self._extract_message_id(result)
                self.logger.info(f"[合并转发] 简写格式成功, mid={mid}")
                return True, mid, ""
            except Exception as e2:
                reason = f"{type(e).__name__}: {e} / {type(e2).__name__}: {e2}"
                self.logger.warning(
                    f"[合并转发] 两种格式都失败，降级纯文本: {reason}"
                )
                return False, None, reason

    def _want_markdown(self, event=None) -> bool:
        """本条消息是否该走官方 Markdown 通道。

        条件：配置「官Q模式」= True。
        通道探测放在发送时做（`_send_official_markdown` 内部），
        这里只做配置判断，避免每条消息都反射一遍 bot。
        """
        try:
            return bool(self.cfg.get("官Q模式", False))
        except Exception:
            return False

    def _official_api(self, bot, event=None, topic: str = "group"):
        """在 bot / api 上找官方发送入口。返回 (方法, 风格, holder)。

        按会话类型优先匹配接口：群 post_group_message，私 post_c2c_message。
        """
        if topic == "c2c":
            api_names = ("post_c2c_message", "post_dms_message",
                         "send_c2c_message", "post_group_message",
                         "send_group_message")
        else:
            api_names = ("post_group_message", "send_group_message",
                         "post_c2c_message", "post_dms_message")
        api = getattr(bot, "api", None) if bot is not None else None
        if api is not None:
            for name in api_names:
                fn = getattr(api, name, None)
                if callable(fn):
                    return fn, "api", api
        bot_names = tuple(api_names) + ("send_group_markdown", "send_markdown",
                                        "send_group_msg", "send_private_msg")
        for name in bot_names:
            fn = getattr(bot, name, None) if bot is not None else None
            if callable(fn):
                return fn, "post", bot
        for name in ("post_group_message", "post_c2c_message"):
            fn = getattr(event, name, None) if event is not None else None
            if callable(fn):
                return fn, "event", event
        return None, None, None

    async def _send_official_markdown(self, event, text: str, keyboard=None):
        """通过官方机器人通道发送 Markdown。返回 (ok, mid)。

        官方机器人群号/用户号是 openid 字符串，绝不做 int() 转换。
        """
        bot = getattr(event, "bot", None)
        # 先判定群聊/私聊，再探测接口（群 post_group_message，私 post_c2c_message）
        gid = None
        uid = None
        try:
            gid = event.get_group_id()
        except Exception:
            gid = None
        try:
            uid = event.get_sender_id()
        except Exception:
            uid = None
        gid_s = str(gid).strip() if gid not in (None, "") else ""
        uid_s = str(uid).strip() if uid not in (None, "") else ""
        topic = "group" if gid_s else "c2c"
        target = gid_s or uid_s

        fn, style, holder = self._official_api(bot, event, topic=topic)
        self._last_bot = holder if holder is not None else bot
        self._bot_probed = True
        if fn is None:
            return False, None
        # 房间列表已是最终结构，跳过二次转换
        if str(text).lstrip().startswith("【房间列表】"):
            md = str(text)
        # 查房结果与已折叠内容不转 Markdown 表格：
        # 键值对转成 `| 项目 | 值 |` 后每个字段占两行，比原文长一倍，
        # 而且玩家列表会变成 `- A1: xxx` 无序列表，官Q 反而比纯文本更长。
        # 这类内容在 _render_text 里已经包进 ```txt 代码块，直接原样发送。
        elif self._guess_kind(text) == "room" or str(text).lstrip().startswith("```"):
            md = str(text)
        else:
            md = self._to_markdown(text)
        if not md:
            return False, None
        if not target:
            return False, None
        msg_id = self._passive_msg_id(event)
        payload = {"msg_type": 2, "markdown": {"content": md}}
        if keyboard:
            payload["keyboard"] = keyboard
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = self._next_msg_seq(str(msg_id))
        try:
            call_desc = ""
            if style == "api":
                # botpy 风格：按会话类型给参数名，并用签名兜底自动改名
                kw = dict(payload)
                if topic == "group":
                    kw["group_openid"] = target
                else:
                    kw["openid"] = target
                try:
                    import inspect as _insp
                    params = _insp.signature(fn).parameters
                    if topic == "group" and "group_openid" not in params:
                        for alt in ("group_id", "open_id", "gid"):
                            if alt in params:
                                kw[alt] = kw.pop("group_openid")
                                break
                    if topic == "c2c" and "openid" not in params:
                        for alt in ("user_openid", "open_id", "uid", "user_id"):
                            if alt in params:
                                kw[alt] = kw.pop("openid")
                                break
                    if params and not any(
                            p.kind == _insp.Parameter.VAR_KEYWORD
                            for p in params.values()):
                        kw = {k: v for k, v in kw.items() if k in params}
                except Exception:
                    pass
                call_desc = (f"{getattr(fn, '__name__', 'api')}("
                             f"{'group_openid' if topic == 'group' else 'openid'}"
                             f"={target[:8]}..., msg_type=2, ...)")
                result = await fn(**kw)
            else:
                # 直接 POST 风格：先试 (target, payload)，失败再试 payload 单参
                try:
                    if topic == "group":
                        result = await fn(group_openid=target, **payload)
                    else:
                        result = await fn(openid=target, **payload)
                    call_desc = "kw风格"
                except TypeError as te1:
                    self.logger.debug(f"[官机] kw 风格失败({te1}), 改试单参 payload")
                    result = await fn(payload)
                    call_desc = "单参payload风格"
            mid = self._extract_message_id(result)
            self.logger.info(
                f"[官机] markdown 已发送 [{call_desc}] mid={mid} "
                f"返回类型={type(result).__name__}"
            )
            return True, mid
        except (TypeError, ValueError, AttributeError) as e:
            # **签名/参数类错误 = 确定没发出去**（不是网络问题）。
            # 这时必须降级为纯文本，否则这条消息一条都发不出去 ——
            # 对应症状：「菜单显示 markdown 但完全没效果」。
            # 记 error 级别并带完整参数清单，方便定位到底哪个参数不被接受。
            self.logger.error(
                f"[官机] markdown 参数错误, 降级纯文本: {type(e).__name__}: {e} | "
                f"fn={getattr(fn, '__name__', '?')} style={style} "
                f"topic={topic} target={target[:12]}... "
                f"payload_keys={sorted(payload.keys())}"
            )
            self._last_md_error = f"{type(e).__name__}: {e}"
            return False, None
        except Exception as e:
            # 其它异常（网络超时、回包解析失败）可能发生在消息**已送达之后**，
            # 此时再发纯文本群里就有两条。保守处理：不重发，只记录。
            self.logger.error(
                f"[官机] markdown 发送异常(不重发, 可能已送达): "
                f"{type(e).__name__}: {e}"
            )
            self._last_md_error = f"{type(e).__name__}: {e}"
            return True, None

    def _passive_msg_id(self, event):
        """取本条消息的 msg_id，用于官方被动回复。取不到返回 None。"""
        for attr in ("message_obj", "message"):
            obj = getattr(event, attr, None)
            if obj is None:
                continue
            for k in ("message_id", "msg_id", "id"):
                v = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
                if v not in (None, "", 0):
                    return str(v)
            d = getattr(obj, "raw_message", None) or getattr(obj, "data", None)
            if isinstance(d, dict):
                for k in ("message_id", "msg_id", "id"):
                    v = d.get(k)
                    if v not in (None, "", 0):
                        return str(v)
        return None

    def _next_msg_seq(self, msg_id: str) -> int:
        """同一 msg_id 的多次被动回复要递增 msg_seq，否则官方会拒重。"""
        seq = getattr(self, "_msg_seq_map", None)
        if seq is None:
            seq = {}
            self._msg_seq_map = seq
        cur = int(seq.get(msg_id, 0)) + 1
        seq[msg_id] = cur
        # 简单清理，避免长期运行无限增长
        if len(seq) > 200:
            for k in list(seq.keys())[:100]:
                seq.pop(k, None)
        return cur

    @staticmethod
    def _md_neutralize(s) -> str:
        """断开 Markdown 链接/图片语法，防止远端可控文本变成可点击链接。

        房名、房主名、地图名都来自房间本身（谁能开房谁就能写），
        官Q模式会把整段结果渲染成 Markdown，`[点我](http://...)`
        就会被 QQ 渲染成超链接 —— 这是插件替陌生人发的消息。

        只断开 `](` 这个**必要组合**，不转义普通方括号：
        房名里的 `[CN]` / `[1v1]` 很常见，全转义会把正常文本弄脏
        （部分渲染端还会把反斜杠原样显示出来）。
        """
        return str(s).replace("](", "]\\(")

    def _to_markdown(self, text: str) -> str:
        """把 `键：值` 形式的纯文本结果转成 Markdown。

        只做**结构无损**的转换，不猜测内容：
          · 首行的 `版本：1.15` 之类 -> 保留为普通行
          · 连续的 `键：值` -> 转成两列表格
          · ` 玩家列表:` 之后缩进的条目 -> 转成无序列表
          · 其它不符合规则的行 -> 原样保留
        这样即使 format_room 以后加字段，也不会漏掉。

        **前置守卫**：如果文本里已经含 Markdown 结构（`***` 分割线、
        `> ` 引用块、`| ... |` 表格行、``` 代码块），直接原样返回。
        否则会把折叠后的内容二次加工成畸形表格 —— 实测过：
        `> 筛选:` 会被转成 `| > 筛选 | |`，整个排版崩掉。

        **帮助文本特判**：首行是「【铁锈查房插件指令】」这类帮助内容时，
        转成「粗体分类标题 + 指令列表」，不套键值表格。
        原因：帮助里的 `|` 是参数分隔符（如 `/铁锈列表 房间中|游戏中`），
        不是键值对；套表格会把整行塞进单元格，QQ 里排版全乱。
        """
        if not text:
            return ""
        lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # ---- 已经是 Markdown 了，别再动 ----
        # 代码块折叠后含 ``` 围栏，必须原样返回，否则会被二次加工成畸形表格。
        # `> ` 引用块 / `***` 分割线保留判定，防御旧格式文本直接传入。
        for ln in lines:
            s = ln.strip()
            # ```` ``` ```` 或 ```` ```txt ```` 都算代码块围栏
            if s.startswith("```"):
                return "\n".join(lines)
            if s == "***" or s == "---":
                return "\n".join(lines)
            if s.startswith("> ") or s == ">":
                return "\n".join(lines)
            if s.startswith("| ") and s.endswith(" |"):
                return "\n".join(lines)
            # 已经由 `_help_to_markdown` 转过的结构化 Markdown：
            # 粗体行、二级列表（`  - ` 两空格缩进）。
            # 必须原样返回，否则第二次转换会把 `  - xxx` 的缩进吃掉
            # （raw.startswith(" ") 分支只看 lstrip 后的内容），
            # 筛选项那一层的层级就丢了 —— 这是幂等性的关键一环。
            if s.startswith("**") and s.count("**") >= 2:
                return "\n".join(lines)
            if ln.startswith("  - ") and not ln.startswith("   -"):
                return "\n".join(lines)
        # ---- 帮助文本特判 ----
        # 只认帮助菜单（首行「【铁锈查房插件指令】」）。
        # 房间列表不做这个转换：它要靠 _fold_markdown 收进代码块，
        # 转换会把它拆成列表项，反而增加行数、更难折叠。
        first = lines[0].strip() if lines else ""
        if first.startswith("【铁锈查房插件指令】"):
            return self._help_to_markdown(lines)
        kv_re = re.compile(r"^([^：:]{1,12})[：:]\s*(.*)$")
        # 先切块：kv 块 / 列表块 / 普通块
        out = []
        kv_buf = []
        list_buf = []

        def flush_kv():
            if not kv_buf:
                return
            out.append("| 项目 | 值 |")
            out.append("| --- | --- |")
            for k, v in kv_buf:
                # 表格里 | 必须转义，否则会截断单元格；
                # `](` 另需断开，否则远端文本能拼出可点击链接
                k2 = self._md_neutralize(str(k).replace("|", "\\|"))
                v2 = self._md_neutralize(str(v).replace("|", "\\|"))
                out.append(f"| {k2} | {v2} |")
            kv_buf.clear()

        def flush_list():
            if not list_buf:
                return
            for it in list_buf:
                out.append(f"- {self._md_neutralize(it)}")
            list_buf.clear()

        for raw in lines:
            ln = raw.rstrip()
            if not ln.strip():
                flush_kv()
                flush_list()
                if out and out[-1] != "":
                    out.append("")
                continue
            # 玩家列表块的标题行：` 玩家列表:` / ` 玩家: ...`
            if ln.lstrip().startswith("玩家列表"):
                flush_kv()
                flush_list()
                out.append("**玩家列表**")
                continue
            # 缩进条目 -> 列表项
            if raw.startswith(" ") or raw.startswith("\t"):
                flush_kv()
                list_buf.append(ln.strip())
                continue
            m = kv_re.match(ln.strip())
            if m:
                flush_list()
                kv_buf.append((m.group(1).strip(), m.group(2).strip()))
                continue
            flush_kv()
            flush_list()
            out.append(self._md_neutralize(ln.strip()))
        flush_kv()
        flush_list()
        # 收尾：去掉首尾空行，压缩连续空行
        res = []
        for ln in out:
            if ln == "" and (not res or res[-1] == ""):
                continue
            res.append(ln)
        return "\n".join(res).strip("\n")

    def _help_to_markdown(self, lines):
        """把帮助文本转成 QQ 官方可渲染的 Markdown。

        QQ 官方机器人的 Markdown 只认少数语法，未识别的行按纯文本渲染。
        只转换部分行会导致同一条消息里出现「粗体标题 + 纯文本 + 列表项」
        的混排，因此这里对每一类行都给出确定的目标格式：

          · `【xxx】v1.5.7`        -> `**粗体**`
          · `—— 分类 ——`          -> `**粗体**`
          · 指令行（`/` `>` `!`）  -> `- ` 列表项，`|` 换全角 `｜`
          · 缩进说明行             -> `  - ` 二级列表项（保住层级，不塌陷）
          · `键: 值` 说明行        -> `- **键**：值`（粗体键，和列表同层级）
          · 纯说明行               -> `- ` 列表项
        这样整条消息只有「粗体 + 无序列表」两种结构，不会再半 Markdown 半纯文本。
        """
        out = []
        for raw in lines:
            ln = raw.rstrip()
            s = ln.strip()
            if not s:
                if out and out[-1] != "":
                    out.append("")
                continue
            # 首行标题：`【xxx】v1.5.7`
            if s.startswith("【") and "】" in s:
                out.append(f"**{s}**")
                continue
            # 分类标题：`—— 房间列表 ——`
            if s.startswith("——") and s.endswith("——"):
                # 破折号保留：官Q 下粗体 + 破折号与纯文本排版一一对应
                out.append(f"**{s}**")
                continue
            indented = raw[:1] in (" ", "\t")
            # 指令行：转列表项，`|` 换全角防表格误判
            if s[:1] in ("/", ">", "!"):
                out.append("- " + s.replace("|", "｜"))
                continue
            # 缩进说明行（筛选项、别名等）：二级列表，保住缩进层级。
            # 直接用 4 空格会被 Markdown 当代码块，所以用 `  - ` 两空格缩进。
            if indented:
                out.append("  - " + s.replace("|", "｜"))
                continue
            # `键: 值` 说明行：键加粗，值保留
            m = re.match(r"^([^：:]{1,10})[：:]\s*(.+)$", s)
            if m:
                out.append(f"- **{m.group(1).strip()}**："
                           f"{m.group(2).strip().replace('|', '｜')}")
                continue
            # 以冒号结尾的「引导行」（如 `筛选:`），后面跟着缩进子项：
            # 只把键加粗，不加 `- ` 前缀，避免和子项看起来同级。
            m2 = re.match(r"^([^：:]{1,10})[：:]$", s)
            if m2:
                out.append(f"**{m2.group(1).strip()}**")
                continue
            # 其余说明行：统一成列表项，避免纯文本混排
            out.append("- " + s.replace("|", "｜"))
        # 收尾：去连续空行
        res = []
        for ln in out:
            if ln == "" and (not res or res[-1] == ""):
                continue
            res.append(ln)
        return "\n".join(res).strip("\n")

    def _pr(self, event, text, kind: str = "auto"):
        """构造面向用户的纯文本结果：替换指令前缀 + 按需压缩折叠。

        **重要限制**：本方法返回的是 AstrBot 的纯文本组件，无法发送
_ag = self._send_text(...)
async for _r in _ag:
    yield _r`。
_ag = self._send_text(...)
async for _r in _ag:
    yield _r` 保持零改动。

        参数 kind：
          "auto"  —— 自动判断。有房号信息 -> room，有调试标记 -> diag，
                     其余按 help 处理。
          "room" / "diag" / "help" —— 显式指定分类。

        只替换 `/铁锈`、`/rwinfo` 这类**指令名**，不会碰文件路径和 URL。
        """
        if not text:
            return event.plain_result(text)
        s = text if isinstance(text, str) else str(text)
        s = self._render_text(s, event, kind)
        return event.plain_result(s)

    def _render_text(self, text: str, event=None, kind: str = "auto") -> str:
        """把原始文案处理成「最终要发的文本」：前缀替换 + 折叠。

        抽出来是为了两处共用：
          · `_pr()`      —— 纯文本平台，结果包成 plain_result
          · `_send_text()` —— 官Q模式，结果塞进 Markdown 卡片
        两边必须用同一份处理逻辑，否则会出现「官Q折叠了、纯文本没折」
        或「前缀替换漏了一边」这类分裂行为。
        """
        if not text:
            return text
        s = text if isinstance(text, str) else str(text)
        # 先判定内容类别（必须用原始纯文本判，转换/折叠后会判不准）
        try:
            k = self._guess_kind(s) if kind == "auto" else kind
        except Exception:
            k = "help"
        try:
            official = bool(self.cfg.get("官Q模式", False))
        except Exception:
            official = False
        # 区分「插件内容」和「指令回执」：
        #   内容型 —— 房间信息、房间列表、帮助、调试菜单（一律以 `【` 开头
        #            或由 kind 判定），有成段的结构，值得转 Markdown / 折叠 / 挂页脚；
        #   回执型 —— 「已加入白名单：#111」「本群自动撤回已关闭」「配置错误」，
        #            都是散文短句。给它们套表格会把一句话拆成三行，
        #            折叠进代码块更是无意义。
        # _guess_kind 对认不出来的文本默认回 "help"，所以这里必须再用
        # 首行特征判一次，不能只看 k。
        _head = str(text).lstrip()
        is_content = _head.startswith("【") or k in ("room", "list", "diag")
        # ---- 0. 官Q模式：先转 Markdown 结构（顺序必须在折叠之前） ----
        # list 不转（序号+地图清单）；help 折叠时不转（代码块内不解析 Markdown）
        _help_will_fold = False
        if k == "help" and is_content:
            try:
                _help_will_fold = (official and self._should_compress("help"))
            except Exception:
                _help_will_fold = False
        if official and is_content and k not in ("list", "room", "diag") \
                and not (k == "help" and _help_will_fold):
            try:
                s = self._to_markdown(s)
            except Exception:
                pass
        # ---- 1. 压缩折叠 ----
        # list 自带分页（每页 5 条），单条消息长度已经固定，再折叠只会
        # 把第 1 条和第 2 条割开，反而更难读 —— 任何平台都不折。
        # help 在官Q走整段代码块；
        # room 在官Q也走代码块：表格会把每个键值拆成 2 行 + 分隔线，
        # 比原文长得多，代码块才是最短且保留对齐的形式。
        try:
            if k == "list" or not is_content:
                # 回执型文本任何平台都不折：它本来就只有一两行
                pass
            elif official:
                if k in ("help", "diag"):
                    if self._should_compress(k) or k == "diag":
                        s = self._fold_all(s)
                elif k == "room":
                    # 官Q：整段进代码块（不做 Markdown 转换）
                    s = self._fold_all(s)
                elif not self._should_compress(k):
                    pass
                else:
                    s = self._compress_text(s, k)
            elif not self._should_compress(k):
                pass
            else:
                s = self._compress_text(s, k)
        except Exception:
            pass
        # ---- 2. 指令前缀替换（引用/列表项内的指令行去掉行首符号） ----
        try:
            p = self._prefix_of(event)
        except Exception:
            p = DEFAULT_CMD_PREFIX
        NAMES = ("铁锈", "rwinfo", "rwlist", "rwhelp", "rw查房", "房间列表")
        out_lines = []
        for ln in s.split("\n"):
            stripped = ln.lstrip()
            if stripped.startswith("> /"):
                after = stripped[2:].lstrip()
                if any(after.startswith("/" + n) for n in NAMES):
                    for name in NAMES:
                        after = after.replace("/" + name, p + name)
                    out_lines.append(after)
                    continue
            # 列表项里的指令行（官Q模式帮助转换后是 "- /铁锈xxx"）
            if stripped.startswith("- /"):
                after = stripped[2:].lstrip()
                if any(after.startswith("/" + n) for n in NAMES):
                    for name in NAMES:
                        after = after.replace("/" + name, p + name)
                    out_lines.append("- " + after)
                    continue
            out_lines.append(ln)
        s = "\n".join(out_lines)
        # 剩下的非引用块指令行，正常替换
        if p != "/":
            for name in NAMES:
                s = s.replace("/" + name, p + name)
        # ---- 3. 底部项目地址 ----
        # 放在最后一步：前缀已替换完，不会被误当成指令去改写。
        #
        # 只挂在「真有一段内容」的消息上：房间信息、房间列表、帮助正文。
        # 指令回执（已关闭 / 已加入白名单 / 配置已更新）、调试输出、报错
        # 一律不带尾巴 —— 一条「已关闭」后面跟个仓库地址既没用，又像广告。
        # 注意不能直接用 k == "help"：_guess_kind 对认不出来的文本也回
        # "help"，所以这里再用首行特征确认一次。
        # 调试输出虽然是内容，但那是排障用的，末尾跟仓库地址没意义
        _content = is_content and k != "diag"
        if _content:
            try:
                s = self._with_repo_footer(s, event)
            except Exception:
                pass
        # ---- 4. 非官Q：净化 Markdown 触发字符 ----
        # 关闭官Q模式后要发的是真正的纯文本。第三方机器人（OneBot / NapCat
        # 等）的 QQ 客户端仍会解析 Markdown：帮助里的 `开|关` 会变成表格、
        # `---` 会变分割线、唤醒词为 `>` 时整个指令行会变成引用块。
        # 这一步放在最末，确保前缀替换和 footer 都已处理完。
        if not official:
            try:
                s = self._strip_markdown(s, prefix=p)
            except Exception:
                pass
        # ---- 5. 空行与行尾空白兜底 ----
        # 各大构建器各自 append 空行，拼起来常出现「连空两行」「段尾一大片空」。
        # 放在最末统一收口，前面任何一步都不用再关心空行排布。
        try:
            s = self._tidy_text(s)
        except Exception:
            pass
        return s

    @staticmethod
    def _tidy_text(text: str) -> str:
        """最后一道排版兜底：抹平空行与行尾空白。

        只做无损整理，不改任何文字：
          · 逐行去掉行尾空格 / 制表符
          · 连续空行压成一个
          · 去掉首尾空行
        代码块（``` 围栏）内部同样适用 —— 多余空行在代码块里更刺眼。
        """
        if not text:
            return text
        lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out = []
        for ln in lines:
            ln = ln.rstrip()
            if not ln and out and not out[-1]:
                continue
            out.append(ln)
        while out and not out[0]:
            out.pop(0)
        while out and not out[-1]:
            out.pop()
        return "\n".join(out)

    def _repo_footer(self, event=None) -> str:
        """底部项目地址：官Q走按钮返回空，非官Q追加虚线小字地址。"""
        try:
            if not self.cfg.get("显示项目地址", True):
                return ""
            if self.cfg.get("官Q模式", False):
                return ""
        except Exception:
            return ""
        return f"⋯⋯ {REPO_URL.lower()}"

    def _with_repo_footer(self, text: str, event=None) -> str:
        """在文本末尾追加项目地址（已含则不重复）。"""
        if not text:
            return text
        footer = self._repo_footer(event)
        if not footer:
            return text
        if REPO_URL in text:
            return text
        body = str(text).rstrip()
        return f"{body}\n\n{footer}"

    async def _as_async_iter(self, obj):
        """把异步生成器/协程统一适配成异步迭代器。"""
        if obj is None:
            return
        if hasattr(obj, "__aiter__"):
            async for item in obj:
                yield item
            return
        if asyncio.iscoroutine(obj) or hasattr(obj, "__await__"):
            try:
                await obj
            except Exception as e:
                self.logger.warning(f"[适配] 协程执行异常: {type(e).__name__}: {e}")
            return
        try:
            for item in obj:
                yield item
        except TypeError:
            self.logger.warning(
                f"[适配] 无法迭代的对象: {type(obj).__name__}"
            )

    async def _send_text(self, event, text, kind: str = "auto",
                         keyboard_text: str = None):
        """按平台发送文本（异步生成器）。

        官Q模式走 Markdown 通道；第三方机器人没有 Markdown，需要折叠时
        改走合并转发卡片（真正的一条消息折叠，点开才展开）。
        合并转发不可用时降级为完整纯文本，并附一句简短提示。
        """
        s = self._render_text(text, event, kind)
        if not s:
            return
        # 官Q模式：走官方 Markdown 通道
        if self._want_markdown(event):
            kb = None
            try:
                if self.cfg.get("官Q模式", False):
                    kb = self._keyboard_of(text if keyboard_text is None
                                          else keyboard_text, event)
            except Exception as e:
                self.logger.debug(f"[官机] 按钮构造失败(忽略): {e}")
            ok, _mid = await self._send_official_markdown(event, s, kb)
            if ok:
                return
            self.logger.warning(
                f"[官机] markdown 未发出, 降级纯文本 | 原因="
                f"{getattr(self, '_last_md_error', None) or '通道未找到/目标为空'} "
                f"| 文本长度={len(s)} 行数={s.count(chr(10)) + 1}"
            )
            yield event.plain_result(s)
            return
        # 第三方机器人 + 需要折叠：走合并转发卡片
        # room / diag 不折：
        #   · room 是查房结果，需要一眼看全，折叠反而多一次点击
        #   · diag 是调试状态速览，本身只有 20-30 行，折叠更没意义，
        #     而且折叠失败还会多附一句「不支持合并转发」的噪音
        try:
            k = self._guess_kind(s) if kind == "auto" else kind
        except Exception:
            k = "help"
        if self._should_compress(k) and k not in ("room", "diag"):
            ok_fw, _mid, reason = await self._send_forward_card(event, s, _FOLD_TITLES.get(k, "房间信息"))
            if ok_fw:
                return
            self.logger.info(f"[合并转发] 不可用({reason}), 降级完整纯文本")
            tip = _FOLD_FAIL_TIP.format(reason=reason)
            if tip not in s:
                s = f"{s}\n\n{tip}"
        yield event.plain_result(s)

    def _guess_kind(self, text: str) -> str:
        """按内容特征判断输出类别（list/room/diag/help），用于压缩判定。"""
        if not text:
            return "help"
        t = str(text)
        if t.lstrip().startswith("【房间列表】") or t.lstrip().startswith("[房间列表]"):
            return "list"
        room_hits = sum(1 for k in ("地图名称", "初始资金", "初始禁核",
                                    "当前人数", "初始迷雾", "默认单位") if k in t)
        if room_hits >= 2 or ("ID：" in t and "当前人数" in t):
            return "room"
        # 调试类内容全部以 `-- 分类 --` 分段 + `键: 值` 平铺，
# 特征是「【铁锈查房调试 或 【官机 开头的标题」和成段的 `-- xxx --`。
        # 之前只认 6 个固定标题，漏了「-- 撤回配置 --」「-- 房间列表 --」
        # 「-- 主要配置 --」等 —— 单独查某一分类时判不出 diag，
        # 会被当成 help 去做 Markdown 转换，排版立刻崩掉。
        if t.lstrip().startswith(("【铁锈查房调试", "【官机")):
            return "diag"
        diag_hits = sum(1 for k in ("-- 当前会话", "-- 撤回配置", "-- 待撤回",
                                    "-- 探针池", "-- 房间列表", "-- 主要配置",
                                    "-- 查看分类") if k in t)
        if diag_hits >= 1:
            return "diag"
        return "help"

    def _prefix_of(self, event=None) -> str:
        """当前生效的指令唤醒词（来自配置项「指令唤醒词」）。

        AstrBot 的唤醒词是全局配置，插件 API 读不到，所以只能让用户在
        插件配置里单独填一次。取值规则：
          · 配置非空 → 取第一个字符（防止用户误填多个字符）
          · 配置为空 → 用默认 `/`
        写错前缀的后果很隐蔽：按钮点下去什么都不发生，且没有报错。
        """
        try:
            raw = str(self.cfg.get("指令唤醒词", "") or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return DEFAULT_CMD_PREFIX
        return raw[0]

    def _list_page_info(self, event=None):
        """取房间列表的当前页码与总页数，用于决定翻页按钮的可用性。

        返回 (cur_page, total_pages)。
        取不到时退化为 (1, 1) —— 两个按钮都不显示，比显示一个
        点了报错的按钮好。
        """
        try:
            st = self._state_get(event) or {}
        except Exception:
            return 1, 1
        try:
            cur = int(st.get("page", 1) or 1)
        except Exception:
            cur = 1
        try:
            total = int(st.get("total", 1) or 1)
        except Exception:
            total = 1
        if cur < 1:
            cur = 1
        if total < 1:
            total = 1
        return cur, total

    def _keyboard_of(self, text: str, event=None):
        """根据结果内容生成快捷指令按钮（官方 Markdown 专用）。

        action.type=2 = 指令按钮：点击后把 data 填进输入框。
        enter=False：按钮不自动回车，避免误触直接刷屏。

        按钮里的 data 必须是真能执行的指令。早期实现写了
        `/铁锈查房 {rid} 玩家`，但 cmd_query_room 的 arg 只接受
        序号/房号/IP:端口，「玩家」不会被解析，点下去整串会被当房号查，
        会提示「房号不存在」。这里只放确实支持的指令：
          · 刷新     —— /铁锈查房 <房号>        （重新查同一个房）
          · 玩家列表 —— /铁锈玩家列表 开         （这是全局开关，不是按房查）
          · 房间列表 —— /铁锈列表
          · 帮助     —— /铁锈帮助

        **只给「房间信息」和「房间列表」挂按钮**：按钮是为「看完这条
        房间信息，想马上再点一下」设计的；挂在指令回执上（保存成功、
        已关闭、名单已更新、调试输出、帮助、报错）纯属噪音，还会把消息
        撑高一大截。其余情况返回 None，由发送端跳过 keyboard 字段。

        返回 None 时调用方不要传 keyboard，否则 QQ 会渲染出一个空键盘。
        """
        raw = str(text or "")
        rid = fingerprint_room_id(raw)
        p = self._prefix_of(event)
        is_list = raw.lstrip().startswith(("【房间列表】", "[房间列表]"))
        is_room = bool(rid and rid != "?") and is_room_info(raw)
        if not is_list and not is_room:
            # 指令回执 / 帮助 / 调试 / 错误提示：不挂按钮
            return None

        rows = []
        if is_list:
            nav = []
            try:
                cur_page, total_pages = self._list_page_info(event)
            except Exception:
                cur_page, total_pages = 1, 1
            if cur_page > 1:
                nav.append({
                    "render_data": {"label": "上一页",
                                    "visited_label": "上一页", "style": 0},
                    "action": {"type": 2, "permission": {"type": 2},
                               "data": f"{p}铁锈上一页", "enter": False}})
            if cur_page < total_pages:
                nav.append({
                    "render_data": {"label": "下一页",
                                    "visited_label": "下一页", "style": 1},
                    "action": {"type": 2, "permission": {"type": 2},
                               "data": f"{p}铁锈下一页", "enter": False}})
            if nav:
                rows.append({"buttons": nav})

        if is_room:
            rows.append({"buttons": [
                {"render_data": {"label": "刷新", "visited_label": "刷新中", "style": 1},
                 "action": {"type": 2, "permission": {"type": 2},
                            "data": f"{p}铁锈查房 {rid}", "enter": False}},
                {"render_data": {"label": "房间列表", "visited_label": "加载中", "style": 0},
                 "action": {"type": 2, "permission": {"type": 2},
                            "data": f"{p}铁锈列表", "enter": False}},
            ]})
            cur = bool(self.cfg.get("show_players", False))
            nxt = "关" if cur else "开"
            rows.append({"buttons": [
                {"render_data": {"label": "玩家列表" + nxt,
                                 "visited_label": "已切换", "style": 0},
                 "action": {"type": 2, "permission": {"type": 2},
                            "data": f"{p}铁锈玩家列表 {nxt}", "enter": False}},
                {"render_data": {"label": "帮助", "visited_label": "打开中", "style": 0},
                 "action": {"type": 2, "permission": {"type": 2},
                            "data": f"{p}铁锈帮助", "enter": False}},
            ]})

        # 项目主页按钮：type=0 URL 跳转（不受 Markdown 域名白名单限制）
        # 房间信息/房间列表都挂（单页列表没有翻页行时也要有）
        try:
            if self.cfg.get("显示项目地址", True):
                rows.append({"buttons": [
                    {"render_data": {"label": "项目主页",
                                     "visited_label": "感谢支持", "style": 0},
                     "action": {"type": 0, "permission": {"type": 2},
                                "data": REPO_URL}},
                ]})
        except Exception:
            pass
        return {"content": {"rows": rows}} if rows else None

    def _adapt_text(self, text: str, event=None) -> str:

        """按平台适配消息文本。

        官方机器人对纯文本的支持较弱（有些接口会把长文本截断、
        或对连续空白/特殊符号处理异常）。规整动作：
          · 统一换行符，去掉行尾空白
          · 压缩连续空行（最多保留一个）
          · 过长的行按字符折行，避免单行超长被截断

        **开启「官Q模式」后生效**，不再强制要求判定为官方平台。
        原因：平台判定依赖 event 上的 platform 属性 / self_id 形态，
        不同适配器差异很大，判不出来时用户开了却完全没效果。
        开着就等于「我要这套排版」，判定仅用于日志提示。
        未开启时原样返回，零影响。
        """
        if not text:
            return text
        try:
            if not self.cfg.get("官Q模式", False):
                return text
        except Exception:
            return text

        try:
            lines = []
            blank = 0
            for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                ln = raw.rstrip()
                if not ln.strip():
                    blank += 1
                    if blank > 1:
                        continue
                else:
                    blank = 0
                # 超长行折行（每段最多 60 字符，续行缩进）
                if len(ln) > 60:
                    indent = "  "
                    rest = ln
                    first = True
                    while rest:
                        piece = rest[:60]
                        rest = rest[60:]
                        if first:
                            lines.append(piece)
                            first = False
                        else:
                            lines.append(indent + piece)
                else:
                    lines.append(ln)
            return "\n".join(lines).strip("\n")
        except Exception:
            return text

    async def _send_recallable(self, event, text: str):
        """发送一条消息, 返回 message_id; 拿不到则返回 None。

        自动撤回需要 message_id。取 mid 的策略:

          开启撤回时:
            ① `bot.send_msg(...)` —— OneBot v11 风格, 稳定返回 {"message_id": ...}
            ② 若 bot 没这个方法 / 调用抛异常 → 退回 `event.send()` 保证消息能发出去,
               但此时大概率拿不到 mid, 该条无法撤回(会记 info 日志说明原因)

          未开启撤回:
            直接 `event.send()`, 不关心 mid

        **关键: 绝不重复发送。** 只要 `bot.send_msg` 被调用过(哪怕抛异常),
        就不再走 `event.send` —— 因为异常也可能发生在消息已经送达之后
        (网络超时、解析回包失败等), 再发一次群里就有两条了。
        """
        text = text.strip()
        if not text:
            return None

        # ---- 官Q：查房结果整段进代码块 ----
        # 这条路径（_send_room_result -> _send_recallable）不经过 _render_text，
        # 所以折叠必须在这里做。查房结果是「键：值」清单，官Q 若走
        # _to_markdown 会变成 `| 项目 | 值 |` 表格，每个字段占两行，
        # 比原文长一倍 —— 代码块才是最短且保留对齐的形式。
        if self._want_markdown(event):
            try:
                if self._guess_kind(text) == "room":
                    text = self._fold_all(text)
            except Exception:
                pass

        # ---- 官方机器人：优先走 msg_type=2 的 Markdown 通道 ----
        # 必须在 _adapt_text 之前判断：文本规整会压掉空行、按 60 字折行,
        # 把 markdown 的结构(表格分隔行/列表)破坏掉。
        # 通道不可用时返回 (False, None)，继续往下走纯文本，绝不重复发送。
        #
        # ElementType=17 的元素，其他适配器（OneBot/NapCat）接到会报
        # 「未实现 (ElementType = 17)」，解析不出正文。
        # 必须走最朴素的纯文本，才能跨框架互通。
        if self._want_markdown(event):
            kb = None
            if self.cfg.get("官Q模式", False):
                try:
                    kb = self._keyboard_of(text, event)
                except Exception as e:
                    self.logger.debug(f"[官机] 按钮构造失败(忽略): {e}")
            ok, oid = await self._send_official_markdown(event, text, kb)
            if ok:
                return oid
            self.logger.info("[官机] markdown 通道不可用, 本条降级为纯文本")

        # ---- 非官方平台 + 需要折叠：走合并转发（聊天记录卡片） ----
        # 官方机器人没有这个接口（那边用 Markdown 代码块），
        # 个人机器人（OneBot v11）支持，群里只占一条消息、点开才展开。
        # 折叠没意义，失败提示更是纯噪音。
        if (not self._want_markdown(event)
                and self._should_compress("room")):
            ok_fw, mid_fw, fw_reason = await self._send_forward_card(
                event, text, _FOLD_TITLES["room"])
            if ok_fw:
                return mid_fw
            self.logger.info(f"[合并转发] 不可用({fw_reason}), 降级为普通文本")
            tip = _FOLD_FAIL_TIP.format(reason=fw_reason)
            if tip not in text:
                text = f"{text}\n\n{tip}"

        # 官方机器人平台适配（官Q模式开启时生效）。
        text = self._adapt_text(text, event)
        rcfg = self._get_recall_cfg(event)
        recall_on = bool(rcfg["开"])
        if not recall_on:
            # 未开启自动撤回: 用原生发送, 不关心 message_id
            try:
                await event.send(self._pr(event, text))
            except Exception as e:
                self.logger.warning(f"发送消息失败: {e}")
            return None
        mid = None
        bot = getattr(event, "bot", None)
        can_send_directly = bot is not None and hasattr(bot, "send_msg")
        # **安全转换**：OneBot 的 group_id/user_id 必须是整数，
        # 但官方机器人的群号/用户号是 openid 字符串（A1B2...）。
        # 直接 int() 会抛 ValueError，而异常分支设计为「不重发」，
        # 结果就是官方机器人一条消息都发不出去 —— 这是实测到的真实缺陷。
        # 取不到整数时**不能抛异常**（会被下面 except 吞掉并 return，
        # 消息彻底丢失），而是标记为「这个 bot 走不了 OneBot 路径」，
        # 静默降级到原生发送。
        gid_num = self._as_int(event.get_group_id())
        uid_num = self._as_int(event.get_sender_id())
        if can_send_directly and gid_num is None and uid_num is None:
            can_send_directly = False

        if can_send_directly:
            # ---- 路径 ①: 显式 bot.send_msg(可拿 message_id) ----
            #
            # **CQ 码注入防护**：OneBot 会把字符串 message 里的
            # [CQ:at,qq=all] / [CQ:image,...] 当指令解析。而查房结果里的
            # 地图名 / 玩家名 / 房主名完全由远端房主可控，恶意房主可借此
            # 让机器人 @全体 / 发图 / 触发任意 CQ 能力。
            # 发送前对 CQ 敏感字符做 OneBot 标准转义，让它们变成纯文本。
            #
            try:
                safe_text = self._escape_cq(text)
            except Exception:
                safe_text = text
            try:
                if gid_num is not None:
                    result = await bot.send_msg(
                        message_type="group",
                        group_id=gid_num,
                        message=safe_text,
                    )
                else:
                    result = await bot.send_msg(
                        message_type="private",
                        user_id=uid_num,
                        message=safe_text,
                    )
                mid = self._extract_message_id(result)
                if mid is None:
                    self.logger.debug(
                        f"[撤回] bot.send_msg 返回体里没有 message_id: "
                        f"{type(result).__name__} {str(result)[:120]}"
                    )
            except Exception as e:
                # 发送可能已成功(异常在回包解析阶段), 因此**不重发**,
                # 只记日志, 本条放弃撤回。
                self.logger.warning(
                    f"[撤回] bot.send_msg 调用异常, 本条不再重发(避免重复): {e}"
                )
                return None
        else:
            # ---- 路径 ②: bot 不支持显式发送, 只能原生发送 ----
            try:
                send_result = await event.send(self._pr(event, text))
                mid = self._extract_message_id(send_result)
            except Exception as e:
                self.logger.warning(f"发送消息失败: {e}")
                return None

        if mid is None:
            # 消息已发出, 但拿不到 message_id → 无法撤回。
            # 这不是静默失败, 而是适配器能力限制, 明确记日志方便排查。
            # 只在首次遇到时提示一次: 若适配器就是不支持, 每条消息都记会刷屏。
            if not getattr(self, "_mid_warned", False):
                self._mid_warned = True
                self.logger.warning(
                    "[撤回] 消息已发送但未取得 message_id, 本条无法自动撤回。"
                    "当前适配器不返回 message_id, 自动撤回对它无效 —— "
                    "此提示只出现一次, 后续同类情况不再重复记录。"
                )
            return None

        # 只有本群真开了自动撤回才安排定时撤回。
        if not recall_on:
            return mid

        delay = int(rcfg["延迟"])
        # 捕获 bot 引用而非 event: 延迟期间 event 可能已随会话结束而失效,
        # bot 是长生命周期的客户端对象, 更稳。
        # 登记明细(会话/计划撤回时间), 供 /铁锈查房调试 展示真实进度。
        due = time.time() + max(0, delay)
        rec_key = self._dedup_key(event)
        task = asyncio.create_task(
            self._delayed_recall(event, mid, max(0, delay), bot=bot)
        )
        meta = {"key": rec_key, "mid": mid, "due": due, "delay": delay}
        self._recall_meta[id(task)] = meta

        def _done(t, _meta_id=id(task)):
            self.pending_recall_tasks.discard(t)
            self._recall_meta.pop(_meta_id, None)

        self.pending_recall_tasks.add(task)
        task.add_done_callback(_done)
        self.logger.debug(f"[撤回] 已安排撤回 mid={mid} delay={delay}s")
        return mid

    @staticmethod
    def _escape_cq(text: str) -> str:
        """OneBot CQ 码转义：把 [ ] & 换成 HTML 实体，防止注入。

        只处理「能构成 CQ 码」的字符。普通文本里的方括号（如
        【房间列表】用的全角括号）不受影响，半角 [] 仅当内容可能被
        OneBot 解析成 CQ 码时才会被触发，转义后仍是可读文本。
        """
        if not text:
            return text
        return (str(text)
                .replace("&", "&amp;")
                .replace("[", "&#91;")
                .replace("]", "&#93;"))

    @staticmethod
    def _extract_message_id(result):
        """从各种适配器返回体里尽力取出 message_id。

        见过的形态:
          · dict: {"message_id": 123} 或 {"data": {"message_id": 123}}
          · 对象: result.message_id
          · 对象: result.data.message_id
        """
        if result is None:
            return None
        if isinstance(result, dict):
            for k in ("message_id", "messageId", "msg_id", "id"):
                v = result.get(k)
                if v not in (None, "", 0):
                    return v
            data = result.get("data")
            if isinstance(data, dict):
                for k in ("message_id", "messageId", "msg_id", "id"):
                    v = data.get(k)
                    if v not in (None, "", 0):
                        return v
            return None
        for attr in ("message_id", "messageId", "msg_id"):
            v = getattr(result, attr, None)
            if v not in (None, "", 0):
                return v
        data = getattr(result, "data", None)
        if data is not None:
            if isinstance(data, dict):
                for k in ("message_id", "messageId", "msg_id", "id"):
                    v = data.get(k)
                    if v not in (None, "", 0):
                        return v
            else:
                for attr in ("message_id", "messageId", "msg_id"):
                    v = getattr(data, attr, None)
                    if v not in (None, "", 0):
                        return v
        return None

    async def _delayed_recall(self, event, message_id, delay: int, bot=None):
        """延迟 delay 秒后撤回指定消息。

        撤回失败的原因分三类, 分别处理:
          · 超时间窗口(QQ 约 2 分钟) / 无权限(非管理员撤他人) —— 不可恢复, 记日志
          · 平台连接抖动 / 临时错误 —— 重试一次
          · bot 对象为空 —— 退回用 event.bot
        """
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.logger.debug(f"[撤回] 等待期间被取消 mid={message_id}")
            return

        target = bot if bot is not None else getattr(event, "bot", None)
        if target is None or not hasattr(target, "delete_msg"):
            self.logger.info(
                f"[撤回] 无法撤回 mid={message_id}: 当前 bot 对象不支持 delete_msg"
            )
            return

        # message_id 可能是 int 或 str (不同适配器不一致), 原样传入, 不要强转
        last_err = None
        for attempt in (1, 2):
            try:
                await target.delete_msg(message_id=message_id)
                self.logger.debug(f"[撤回] 撤回成功 mid={message_id} delay={delay}s")
                return
            except asyncio.CancelledError:
                self.logger.debug(f"[撤回] 任务被取消 mid={message_id}")
                raise
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                # 不可恢复的错误直接放弃, 不重试
                if any(k in msg for k in ("time", "expire", "permission", "forbidden",
                                          "not found", "不存在", "无权限", "超时")):
                    break
                if attempt == 1:
                    self.logger.debug(f"[撤回] 第1次失败, 1s 后重试 mid={message_id}: {e}")
                    try:
                        await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        raise

        # 用 warning 而不是 debug: 默认日志级别是 INFO, debug 会被吞掉,
        # 表现为"开了撤回却从没撤过"且毫无线索。这里只在失败时记一条,
        # 正常运行不会刷屏。
        self.logger.warning(
            f"[撤回] 撤回失败 mid={message_id} delay={delay}s: {last_err} "
            f"(常见原因: 超过 QQ 2分钟撤回窗口 / 非管理员无权撤回他人消息)"
        )

    def _acquire_probe(self) -> int:
        """分配一个探针编号。无空闲探针时返回 None。

        策略: 优先复用已释放的最小编号(probe_released), 没有再按 probe_next 递增。
        probe_next 是"历史上分配到的最大编号+1", 不会因为释放而回退 ——
        这样同一个编号不会被两条并发任务同时用到。
        """
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

    def _probe_stats(self) -> dict:
        """探针池快照, 供调试展示。语义明确化, 避免"下一个编号"造成误解。"""
        used = sorted(self.probe_used)
        released = sorted(self.probe_released)
        # 该编号是否曾经分配过(用于解释 probe_next 的含义)
        ever_allocated_max = self.probe_next - 1
        return {
            "范围": f"{PROBE_MIN}-{PROBE_MAX}",
            "占用中": used,
            "可复用": released,
            "历史最大编号": ever_allocated_max if ever_allocated_max >= PROBE_MIN else None,
            "容量": PROBE_MAX - PROBE_MIN + 1,
            "空闲容量": (PROBE_MAX - PROBE_MIN + 1) - len(used),
        }

    def _probe_prefix(self) -> str:
        """当前生效的探针名前缀（已清洗）。

        配置项「探针名前缀」可能为空/超长/含特殊字符，
        `sanitize_probe_prefix` 统一兜底：
          · 非法字符直接丢弃
          · 超过 4 个字符截断
          · 清洗后为空则回退 `ABAB`

        **每次调用都重新清洗**（不做缓存）：配置面板改了立刻生效，
        不必重启插件。清洗本身只是几次字符判断，开销可忽略。
        """
        return sanitize_probe_prefix(self.cfg.get("探针名前缀", PROBE_PREFIX_DEFAULT))

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
    def _ts_encode(month, day, hour, minute, sec) -> str:
        """_ts_decode 的逆运算, 用于回环校验(确认后缀确实是编码产物)。"""
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        v = ((((month - 1) * 31 + (day - 1)) * 24 + hour) * 60 + minute) * 60 + sec
        if v < 0:
            return ""
        out = ""
        while v:
            out = chars[v % 36] + out
            v //= 36
        return out.zfill(5)

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
        for base in MASTERSERVERS:
            url = base + "/interface?" + urllib.parse.urlencode(
                {"action": "get", "game_id": room_uuid, "c": c})
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": f"rw android {CLIENT_VER} en", "Language": "en"})
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

    async def _query_room(self, rid: str, probe_name: str, gid: str = "") -> str:
        # 探针脚本缺失 / 接口过旧 -> 从仓库补齐（仅这两种情况，不做自动更新）。
        # 放到线程里跑：里面会起子进程探测版本 + 网络下载，不能阻塞事件循环。
        if not os.path.exists(ROOM_INFO_PY) or not self._room_info_ok:
            ok, err = await _run_in_thread(ensure_room_info, True)
            self._room_info_ok = ok
            if not ok:
                return f"[rwinfo] 探针脚本不可用：{err}"
        python = sys.executable
        if not python or not os.path.exists(python):
            python = "python3"
        cmd_args = [
            python,
            ROOM_INFO_PY,
            rid,
            "--name", probe_name,
            "--max-players", str(self.cfg.get("max_players_display", 10))
        ]
        if self.cfg.get("show_players", False):
            cmd_args.append("--show-players")
        # 进房推广：文案固定为项目地址，不开放配置 ——
        # 文案写死为 REPO_URL，保证来源标识不会被配置覆盖。
        if self.cfg.get("推广开关", False):
            cmd_args += ["--promote", _PROMOTE_DEFAULT,
                         "--promote-cooldown",
                         str(self.cfg.get("推广冷却秒", 180))]
        if self.cfg.get("debug", False):
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
        # 房间信息正文以 "房间版本：" 开头（新版）或 "版本："（旧版）。
        # 只保留正文, 避免 debug 模式下调试输出刷屏群里.
        mark = out.find("房间版本：")
        if mark == -1:
            mark = out.find("版本：")
        if mark != -1:
            return out[mark:]
        return out

    def _list_hit(self, lst, kind: str, ident: str) -> bool:
        """判断 ident 是否命中名单 lst（kind 为 "#" 群或 "@" 用户）。

        **严格匹配前缀**：条目必须带正确前缀，标识部分完全相等。
          · "#3A28BF..." 配 "#" + "3A28BF..."  -> 命中
          · "3A28BF..."  裸值（缺前缀）        -> 不命中
          · "@3A28BF..." 前缀类型不符          -> 不命中

        **标识部分忽略大小写**：官方机器人的 openid 是十六进制哈希，
        不同适配器/事件给出的字母大小写可能不一致（有时全大写、有时全小写）。
        对十六进制串来说大小写是同一个 ID，比对必须归一化，
        否则会随机出现「填对了却提示没权限」。
        数字 QQ 不含字母，归一化无副作用。
        """
        if not ident:
            return False
        want = (kind + str(ident).strip()).lower()
        for raw in (lst or []):
            if isinstance(raw, str) and raw.strip().lower() == want:
                return True
        return False

    def _near_miss(self, lst, kind: str, ident: str):
        """找出「标识相同、但前缀缺失或写错」的条目。

        用于把配置错误讲清楚：条目内容其实是对的，只是少了 `#` 或 `@`。
        返回原始条目字符串列表（可能为空）。
        """
        if not ident:
            return []
        target = str(ident).strip().lower()
        want = (kind + target).lower()
        out = []
        for raw in (lst or []):
            if not isinstance(raw, str):
                continue
            s = raw.strip()
            if not s or s.lower() == want:
                continue
            body = s[1:].strip() if s[:1] in ("#", "@") else s
            if body.lower() == target:
                out.append(s)
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
        mode = self.cfg.get("mode", "black")

        if mode == "black":
            bl = self.cfg.get("黑名单", [])
            if is_private:
                return not self._list_hit(bl, "@", uid)
            # 群聊: #gid 或 @uid 任一在黑名单 -> 拦
            return not (self._list_hit(bl, "#", gid) or self._list_hit(bl, "@", uid))

        if mode == "white":
            wl = self.cfg.get("白名单", [])
            if is_private:
                return self._list_hit(wl, "@", uid)
            # 群聊: #gid 或 @uid 任一在白名单 -> 放
            return self._list_hit(wl, "#", gid) or self._list_hit(wl, "@", uid)

        # ---- mixed 混合模式 ----
        mwl = self.cfg.get("混合_白名单", [])
        mbl = self.cfg.get("混合_黑名单", [])
        if is_private:
            if self._list_hit(mbl, "@", uid):
                return False  # 用户在混合黑名单 -> 拦
            if self._list_hit(mwl, "@", uid):
                return True   # 用户在混合白名单 -> 放
            # 用户不在两名单: 跟随配置的私聊默认策略
            policy = self.cfg.get("混合_私聊策略", "white")
            return policy != "white"  # white=不在白->拦(False); black=在黑才拦->放(True)
        # 群聊
        if self._list_hit(mbl, "@", uid):
            return False  # 用户黑名单优先(即使群在白名单) -> 拦
        if self._list_hit(mwl, "@", uid):
            return True   # 用户白名单优先(即使群在黑名单) -> 放
        # 用户不在两名单: 看群级
        if self._list_hit(mbl, "#", gid):
            return False  # 群在黑名单 -> 拦
        if self._list_hit(mwl, "#", gid):
            return True   # 群在白名单 -> 放
        # 群级都不在：**只要配置了混合白名单就默认拦截**。
        #
        # 旧实现判的是「白名单里有没有群条目」，没有就全放行 —— 这等于：
        # 用户写了「只让 @张三 用」（混合白名单 = ['@张三']），
        # 结果因为没有任何群条目，**所有群的所有人都能用了**。
        # 白名单反而变成了一种更宽的放行，与预期完全相反。
        #
        # 正确语义：白名单一旦非空，就代表「只有名单内的能用」，
        # 名单里只写了用户，就只放行这个用户，群级一律不额外放行。
        # 混合白名单为空时才回退到「不限制」（与黑名单模式的默认行为一致）。
        return not mwl

    def _migrate_broken_list_entries(self):
        """启动时检查四个名单里的写法问题，只告警、不改数据。

        检查两类：
          ① 缺前缀：条目是不带 `#` / `@` 的裸标识。
             名单前缀是有语义的（# 群、@ 用户），代码严格按前缀匹配，
             裸值永远匹配不上。这类必须由配置者自己补上。
          ② 截断残留：形如 `#3` 这种超短纯数字条目。
             v1.5.7 及更早用 re.search(r"\\d+") 抽数字段，把官方机器人的
             32 位哈希 openid 截成了 "#3"，属于死数据。

        只告警不自动改，理由：自动补前缀会把「用户本来想加群却写成用户」
        这类真实语义错误一起糊过去，问题只是被推迟到下次。
        """
        try:
            groups = ("白名单", "黑名单", "混合_白名单", "混合_黑名单")
            no_prefix = []
            truncated = []
            for g in groups:
                for x in (self.cfg.get(g) or []):
                    if not isinstance(x, str):
                        continue
                    s = x.strip()
                    if not s:
                        continue
                    if s[:1] not in ("#", "@"):
                        no_prefix.append((g, s))
                        continue
                    body = s[1:].strip()
                    # 纯数字但极短：真实 QQ 群号/QQ 号不可能只有 1~2 位
                    if body.isdigit() and len(body) <= 2:
                        truncated.append((g, s))
            if no_prefix:
                detail = ", ".join(f"{g}:{x!r}" for g, x in no_prefix)
                self.logger.warning(
                    f"[名单] {len(no_prefix)} 条配置缺少前缀（群用 #、用户用 @），"
                    f"严格匹配下永远不会命中：{detail}。"
                    f"请在配置面板里给它们补上正确前缀。")
            if truncated:
                detail = ", ".join(f"{g}:{x}" for g, x in truncated)
                self.logger.warning(
                    f"[名单] {len(truncated)} 条疑似旧版截断产生的无效条目：{detail}。"
                    f"它们在官方机器人（哈希群号）下永远无法匹配，"
                    f"请删除后重新添加完整标识。")
        except Exception as e:
            self.logger.warning(f"[名单] 自检跳过: {type(e).__name__}: {e}")

    def _session_blocked(self, event):
        """当前会话是否整体禁止使用本插件（管理/辅助指令的准入闸门）。

        **调用时机**：本方法在 AstrBot 的权限校验之后执行，构成两级串联：

            事件 → [AstrBot] command 匹配
                 → [AstrBot] permission_type(ADMIN)  ← 非管理员在此被框架拦下
                 → [插件]   _session_blocked()       ← 名单外的管理员在此被拦下
                 → 业务逻辑

        也就是说：**名单的约束力高于 AstrBot 管理员身份**。
        管理员先过框架那关，再过名单这关，任一不过都无法执行。

        与 `_is_allowed` 的分工：
          · `_is_allowed`      —— 能不能查房（黑名单模式下默认放行）
          · `_session_blocked` —— 能不能用插件（名单说不让用就是不让用）

        返回值：None 表示放行；返回字符串表示应直接回这句拒绝理由。
        """
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if self._is_allowed(is_private, normalize_gid(event.get_sender_id()),
                            normalize_gid(event.get_group_id())):
            return None
        # 说明清楚是「名单没通过」而非「权限不足」：
        # 这两者在管理员眼里很容易混淆 —— 明明自己是管理员却被拒，
        # 若不点明原因，会误以为是插件坏了。
        return ("当前会话未授权使用本插件（不在名单内）。\n"
                "名单的优先级高于机器人管理员：管理员也需在名单内才能操作。\n"
                "请先在已授权的会话里用 /铁锈白名 添加本群/本用户。")

    def _allowed_detail(self, is_private, uid, gid) -> str:
        """给「没有权限」的回复附加可诊断的现场信息。

        白名单里明明填了却一直提示没权限，最常见的原因是标识形态对不上
        （官方机器人群号是 32 位哈希，或名单里存的是裸值而比对带了 # 前缀）。
        这里把两边实际用于比较的字符串打出来，便于看出差异。
        """
        try:
            mode = self.cfg.get("mode", "black")
            if mode == "mixed":
                key = "混合_白名单"
            elif mode == "white":
                key = "白名单"
            else:
                key = "黑名单"
            lst = list(self.cfg.get(key) or [])
            if is_private:
                ident = str(uid or "")
                kind = "@"
            else:
                ident = str(gid or "")
                kind = "#"
            want = (kind + ident) if ident else "(未知会话)"
            seen = lst[:8]
            more = "" if len(lst) <= 8 else f" …共 {len(lst)} 条"
            lines = [
                f"\n当前比对值：{want}",
                f"{key}：{'、'.join(map(str, seen)) or '(空)'}{more}",
            ]
            near = self._near_miss(lst, kind, ident) if ident else []
            if near:
                # 内容对、前缀错：这是配置写法问题，明确提示配置者补上
                fixed = kind + ident
                lines.append(
                    f"⚠ 配置错误：名单里有 {'、'.join(repr(x) for x in near)} "
                    f"缺少 {kind} 前缀\n"
                    f"请把里面那条改成：{fixed}"
                )
            elif lst:
                lines.append(
                    "提示：名单里没有与比对值相同的条目，"
                    "请检查是否漏了字符、或前后带空格"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    # ---- 指令（管理类在 handler 内再判 ADMIN 名单） ----
    @filter.command("铁锈查房帮助")
    async def cmd_help(self, event: AstrMessageEvent, arg: str = ""):
        """显示本插件全部指令与用法。

        把房间列表、查房、插件设置、其他、自动触发五类指令一次列全，
        适合第一次使用、或忘记某个指令怎么拼的时候查看。

        输出内容：
          · 房间列表：/铁锈列表 及其筛选词（房间中 / 不满 / 真人 / 官方）、
            页码、刷新、搜索的组合用法，以及翻页指令与全部别名
          · 查房：/铁锈查房 的三种目标形式（列表序号 / 房号 / IP:端口）
          · 插件设置：全局解析、名单模式、白名黑名、私聊策略与开关、
            玩家列表、重试次数、自动撤回
          · 其他：运行时调试、探针时间反解
          · 自动触发：什么样的房号会被自动识别、多长的房号才有效、
            为什么查房结果本身不会再次触发查房

        别名：铁锈查房菜单 / 铁锈帮助 / rwhelp 四个指令输出完全相同。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        _ag = self._send_text(event, "\n".join(self._help_text_lines()))
        async for _r in _ag:
            yield _r

    # ---- 帮助别名 ----
    # 三个别名一律转发到 cmd_help：会话拦截、拼文本、发送这套逻辑抄四份的
    # 后果是改一处漏三处（历史上就漏过一次会话拦截）。
    @filter.command("铁锈查房菜单")
    async def cmd_help_alias1(self, event: AstrMessageEvent, arg: str = ""):
        """显示本插件全部指令与用法（铁锈查房帮助 的别名）。

        与 /铁锈查房帮助 输出完全一致，只是指令名更短更好记。
        """
        async for _r in self.cmd_help(event, arg):
            yield _r

    @filter.command("铁锈帮助")
    async def cmd_help_alias2(self, event: AstrMessageEvent, arg: str = ""):
        """显示本插件全部指令与用法（铁锈查房帮助 的别名）。

        与 /铁锈查房帮助 输出完全一致，适合不习惯长指令名的场景。
        """
        async for _r in self.cmd_help(event, arg):
            yield _r

    @filter.command("rwhelp")
    async def cmd_help_alias3(self, event: AstrMessageEvent, arg: str = ""):
        """显示本插件全部指令与用法（铁锈查房帮助 的别名）。

        英文短名，与 /铁锈查房帮助 输出完全一致。
        """
        async for _r in self.cmd_help(event, arg):
            yield _r

    def _help_text_lines(self):
        """帮助文本(供主指令与别名共用).

        排版约定：分类标题用 `—— xxx ——`，指令行以 `/` 开头，
        说明行不缩进（缩进会被 QQ 当代码块），同类项用全角空格分组。
        """
        return [
            f"【铁锈查房插件指令】v{PLUGIN_VERSION}　群聊/私聊通用",
            "",
            "—— 查房 ——",
            "/铁锈查房 <序号|房号>　群里直接发房号也行",
            "",
            "—— 房间列表 ——",
            "/铁锈列表 [页码] [筛选] [搜索 关键词] [刷新]",
            "筛选：房间中/游戏中、满/不满、真人/官方（可组合、空格分隔）",
            "例：/铁锈列表 房间中 2　/铁锈列表 搜索 我的 世界",
            "别名：/房间列表　/铁锈房列表　/rwlist　/铁锈下一页　/铁锈上一页",
            "",
            "—— 插件设置（管理员）——",
            "/铁锈全局解析 开|关　/铁锈模式 白|黑|混合",
            "/铁锈白名 +@用户ID|+#群号　可连写多条",
            "/铁锈黑名 +@用户ID|+#群号　可连写多条",
            "/铁锈私聊策略 白|黑　/铁锈私聊开关 开|关",
            "/铁锈玩家列表 开|关　/铁锈重试 [0-10]",
            "/铁锈撤回 列出|+|-|重置　* 表示所有群",
            "",
            "—— 调试 ——",
            "/铁锈查房调试 [会话|撤回|探针|列表|配置|官机]",
            "/铁锈查房调试菜单 - 查看/修改内部数值",
            "/铁锈探针时解 <5位后缀> - 还原探针进房时间",
            "",
            "—— 自动触发 ——",
            "群聊/私聊发房号自动查房，无需指令",
            "支持 o/q/r/s+数字 与 纯字母两类，均需 3 位以上",
            "官方机器人需群主开启「允许获取群消息」与「自动回复」",
            "别名：/铁锈查房菜单　/铁锈帮助　/rwhelp",
        ]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_unmatched(self, event: AstrMessageEvent):
        """未识别的 铁锈 指令自动路由到帮助菜单.

        前缀不能写死 `/` —— 唤醒词可以是 `>`、`!` 等任意字符。
        写死会让打错的指令既没有功能、也没有帮助兜底，完全静默。
        这里同时接受「配置的唤醒词」和 `/`：
        前者是实际在用的，后者是 AstrBot 部分版本仍会触发默认前缀。
        """
        text = ""
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return
        if not text:
            return
        # 剥掉唤醒词：命中配置的前缀 或 `/` 都算
        prefix = self._prefix_of(event)
        if prefix and text.startswith(prefix):
            cmd = text[len(prefix):].split()[0] if len(text) > len(prefix) else ""
        elif text.startswith("/"):
            cmd = text[1:].split()[0] if len(text) > 1 else ""
        else:
            return
        # 只接管 "铁锈"/"rwinfo" 开头但非已知指令的情况
        if not (cmd.startswith("铁锈") or cmd.startswith("rwinfo")):
            return
        # 会话准入：未授权会话连帮助菜单都不返回。
        # 帮助内容里列出了全部管理指令名，对名单外的群属于信息泄露；
        # 且过期插件不该在无关群里刷屏。
        if self._session_blocked(event):
            return
        known = {
            "铁锈查房帮助", "铁锈查房菜单", "铁锈帮助", "rwhelp",
            "铁锈列表", "房间列表", "铁锈房列表", "rwlist",
            "铁锈下一页", "铁锈上一页", "铁锈查房",
            "铁锈全局解析", "铁锈模式", "铁锈白名", "铁锈黑名",
            "铁锈私聊策略", "铁锈私聊开关", "铁锈玩家列表",
            "铁锈重试", "铁锈撤回", "铁锈查房调试",
            "铁锈查房调试菜单",
            "铁锈探针时解",
            "铁锈官机探测",
            "铁锈官机测试",
            "铁锈压缩",
        }
        if cmd in known:
            return
        _ag = self._send_text(event, 
            f"未知指令：/{cmd}\n\n" + "\n".join(self._help_text_lines())
        )
        async for _r in _ag:
            yield _r

    # ---- 房间列表 (分页 + 按 QQ 号缓存) ----
    # 滑动过期: 每次"有动作"(翻页/切筛选/刷新)都会把过期时间往后推
    LIST_CACHE_TTL = 60   # 秒: 房间数据缓存有效期(全局共享一份, 滑动续期)
    # 秒: 「第 N 条 -> 哪个房」映射的有效期。
    # 比列表数据缓存长得多 —— 房号不会变，过期重建会导致
    # 「序号突然失效」，体验很差。10 分钟足够覆盖一次浏览会话。
    ADDRS_TTL = 600

    def _list_cache(self):
        """房间数据缓存容器(全局共享一份, 所有 QQ/群共用)。"""
        if not hasattr(self, "_room_list_cache"):
            self._room_list_cache = {"ts": 0.0, "rooms": []}
        return self._room_list_cache

    def _list_page_state(self):
        """按 QQ 号隔离的翻页/筛选状态。key=QQ号, value=状态字典。

        内存字典是**主存储**（每次翻页都要读写，必须同步、零 await）。
        AstrBot 的 KV 存储（`self.put_kv_data` / `self.get_kv_data`）
        是**可选镜像**：插件重启后内存丢失，KV 能让翻页状态续上。

        为什么不直接用 KV 当主存储：`_state_get` / `_state_put` 被
        `_cmd_list_impl` 等**同步**路径调用，改成 await 要传染整条调用链，
        风险远大于收益。镜像写入失败一律静默忽略 —— 这只是"重启后还能
        翻页"的锦上添花，不能影响正常流程。
        """
        if not hasattr(self, "_room_page_state"):
            self._room_page_state = {}
        return self._room_page_state

    # ---- KV 镜像（AstrBot v4.9+ 提供，缺失时全部降级为 no-op）----
    def _kv_ok(self) -> bool:
        """当前 Star 实例是否真的提供 KV 接口（不同版本差异大，探测一次）。"""
        if getattr(self, "_kv_probed", False):
            return bool(getattr(self, "_kv_usable", False))
        self._kv_probed = True
        ok = False
        for name in ("put_kv_data", "get_kv_data"):
            fn = getattr(self, name, None)
            if not callable(fn):
                ok = False
                break
            ok = True
        self._kv_usable = ok
        try:
            self.logger.debug(f"[列表状态] KV 可用={ok}")
        except Exception:
            pass
        return ok

    async def _kv_mirror(self, key: str, data):
        """把状态写进 KV（静默失败）。"""
        if not self._kv_ok():
            return
        try:
            await self.put_kv_data(key, data)
        except Exception as e:
            try:
                self.logger.debug(f"[列表状态] KV 写入跳过: {type(e).__name__}: {e}")
            except Exception:
                pass

    async def _kv_restore(self, key: str):
        """从 KV 读回状态（失败返回 None）。"""
        if not self._kv_ok():
            return None
        try:
            v = await self.get_kv_data(key, None)
            return v if isinstance(v, dict) else None
        except Exception:
            return None

    def _list_uid(self, event):
        """列表状态 key: 群聊 = `g#群号@QQ号`, 私聊 = `p@QQ号`。

        此方法曾只用 sender QQ 号做 key、不区分群，会导致跨群串房：
        同一个用户在 A 群查过 `/铁锈列表`，筛选条件、页码、以及**该页每个序号的
        真实地址列表(addrs)** 都被存在 `_room_page_state[QQ号]` 里；
        该用户跑到 B 群发 `/铁锈查房 2`，取到的就是 A 群的 `addrs[1]`
        —— 查到的是 A 群的房，表现就是「串房」。

        现在按「会话 + 用户」隔离：群内各用户互不干扰，跨群彻底不可见。
        私聊仍按用户，彼此独立。
        """
        try:
            uid = normalize_gid(event.get_sender_id()) or "unknown"
            try:
                is_private = bool(event.is_private_chat())
            except Exception:
                is_private = False
            gid = normalize_gid(event.get_group_id())
            if is_private or not gid:
                return f"p@{uid}"
            return f"g#{gid}@{uid}"
        except Exception:
            return "unknown"

    def _state_get(self, event):
        """取翻页/筛选状态。页码不过期，addrs 单独判新鲜度。"""
        uid = self._list_uid(event)
        return self._list_page_state().get(uid)

    def _state_addrs(self, event):
        """取该页直连地址列表；超过 ADDRS_TTL 返回空（调用方就地重建）。"""
        uid = self._list_uid(event)
        st = self._list_page_state().get(uid) or {}
        try:
            age = time.time() - float(st.get("data_ts") or 0)
        except (TypeError, ValueError):
            age = 1e9
        if age > self.ADDRS_TTL:
            return []
        return list(st.get("addrs") or [])

    def _state_put(self, event, page, keyword, status, full, source,
                   total=0, addrs=None):
        """保存翻页/筛选状态到内存（ts 只用于容量控制，data_ts 用于 addrs 新鲜度）。"""
        uid = self._list_uid(event)
        now = time.time()
        st = {
            "page": page, "keyword": keyword, "status": status,
            "full": full, "source": source,
            "total": total,
            "addrs": list(addrs or []),
            "ts": now,
            "data_ts": now,
        }
        self._list_page_state()[uid] = st
        return st

    async def _state_save_kv(self, event):
        """把当前状态落盘到 KV（await 版）。KV 不可用时静默跳过。"""
        if not self._kv_ok():
            return
        uid = self._list_uid(event)
        st = self._list_page_state().get(uid)
        if not st:
            return
        await self._kv_mirror(f"liststate:{uid}", st)

    async def _state_load(self, event):
        """从 KV 恢复该 QQ 的状态到内存（若内存里没有）。

        用于两条路径：
          · 插件刚重启，内存字典是空的；
          · 用户隔了很久才来点「下一页」。
        只在内存缺失时读盘，命中就返回内存里的（避免每次翻页都 IO）。

        恢复出来的状态**不校验 ts** —— 页码本来就不该过期；
        `addrs` 是否可用由 `_state_addrs` 按 `data_ts` 单独判断。
        """
        uid = self._list_uid(event)
        states = self._list_page_state()
        if uid in states:
            return states[uid]
        st = await self._kv_restore(f"liststate:{uid}")
        if not st:
            return None
        # 补齐旧数据可能缺的字段，避免上层 KeyError
        st.setdefault("page", 1)
        st.setdefault("total", 0)
        st.setdefault("addrs", [])
        st.setdefault("data_ts", 0)
        st.setdefault("ts", 0)
        states[uid] = st
        try:
            self.logger.info(
                f"[列表状态] 已从 KV 恢复: {uid} 第 {st.get('page')}/"
                f"{st.get('total')} 页")
        except Exception:
            pass
        return st

    def _room_state(self):
        """`{会话key: {"target","desc","ride","ts"}}` 的内存容器。"""
        if not hasattr(self, "_last_room_state"):
            self._last_room_state = {}
        return self._last_room_state

    def _remember_room(self, event, target, desc, result):
        """记录本会话最近一次成功查到房间的信息。

        `target` 是查房实际用的目标（房号 / IP:端口 / 解析出的真实地址），
        `desc` 是给用户看的简短描述，`result` 原样留用来做字段回溯。

        键用 `_list_uid`（会话+用户），与翻页状态同一套隔离规则：
        跨群不可见，群内互不干扰。
        """
        uid = self._list_uid(event)
        try:
            rid = fingerprint_room_id(result or "")
        except Exception:
            rid = "?"
        self._room_state()[uid] = {
            "target": str(target or ""),
            "desc": str(desc or ""),
            "room_id": str(rid or "?"),
            "ts": time.time(),
        }

    async def _room_save_kv(self, event):
        """把「上次查的房间」落盘到 KV（await 版）。"""
        if not self._kv_ok():
            return
        uid = self._list_uid(event)
        st = self._room_state().get(uid)
        if not st:
            return
        await self._kv_mirror(f"lastroom:{uid}", st)

    async def _refill_addrs(self, event):
        """静默重建当前页 addrs（不发消息）。按原页码+筛选重新抓取。"""
        st = self._state_get(event)
        if not st:
            st = await self._state_load(event)
        if not st:
            return False
        page = int(st.get("page", 1) or 1)
        keyword = st.get("keyword", "") or ""
        status = st.get("status", "") or ""
        full = st.get("full", "") or ""
        source = st.get("source", "all") or "all"

        try:
            rooms_all, _cached = await self._list_fetch(force=False)
        except Exception as e:
            self.logger.warning(f"[查房] 重建 addrs 失败: {type(e).__name__}: {e}")
            return False
        if not rooms_all:
            return False

        def match(r):
            rst = (r.get("status") or "").lower()
            if status and rst != status:
                return False
            isfull = ((r.get("max") or 0) > 0
                      and (r.get("players") or 0) >= (r.get("max") or 0))
            if full == "only" and not isfull:
                return False
            if full == "hide" and isfull:
                return False
            if source != "all" and (r.get("source") or "") != source:
                return False
            if keyword:
                blob = " ".join([str(r.get("name") or ""),
                                 str(r.get("map") or ""),
                                 str(r.get("desc") or "")]).lower()
                if keyword.lower() not in blob:
                    return False
            return True

        rooms = [r for r in rooms_all if match(r)]
        if not rooms:
            return False

        try:
            page_size = max(1, int(self.cfg.get("list_page_size", 5)))
        except (TypeError, ValueError):
            page_size = 5
        total_pages = max(1, (len(rooms) + page_size - 1) // page_size)
        page = min(max(1, page), total_pages)
        start = (page - 1) * page_size
        page_rooms = rooms[start:start + page_size]

        self._state_put(event, page, keyword, status, full, source,
                        total_pages,
                        [{
                            "addr": str(r.get("addr") or ""),
                            "room_id": str(r.get("room_id") or ""),
                            "name": str(r.get("name") or ""),
                            "uuid": str(r.get("id") or ""),
                            "join_a": str(r.get("join_a") or "0"),
                        } for r in page_rooms])
        await self._state_save_kv(event)
        self.logger.info(
            f"[查房] 已就地重建第 {page}/{total_pages} 页 addrs "
            f"（{len(page_rooms)} 条，未发送列表消息）")
        return True

    def _cleanup_states(self):
        """内存字典容量控制：超过 200 个会话时按 LRU 裁一半。"""
        states = self._list_page_state()
        if len(states) <= 200:
            return
        items = sorted(states.items(), key=lambda kv: kv[1].get("ts", 0))
        for k, _v in items[:len(items) // 2]:
            states.pop(k, None)

    async def _list_fetch(self, force: bool = False):
        """拉取房间列表(带滑动过期缓存)。force=True 强制刷新。"""
        cache = self._list_cache()
        now = time.time()
        if not force and cache["rooms"] and (now - cache["ts"]) < self.LIST_CACHE_TTL:
            cache["ts"] = now   # 滑动续期: 有人用就续命
            return cache["rooms"], True   # 命中缓存

        cmd_args = [sys.executable, "-u", ROOM_INFO_PY, "--list-json"]
        # 房间列表也依赖 room_info.py，同样做一次可用性保障
        # 与 _query_room 同一套门控：缺失或接口过旧都触发补齐，
        # 失败时直接返回错误信息，不能落进 create_subprocess_exec
        # （文件不存在会抛 FileNotFoundError，具体原因就被吞掉了）
        if not os.path.exists(ROOM_INFO_PY) or not self._room_info_ok:
            ok, err = await _run_in_thread(ensure_room_info, True)
            self._room_info_ok = ok
            if not ok:
                self.logger.error(f"[列表] 探针脚本不可用: {err}")
                return None, False
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
        """查看铁锈战争房间列表（分页显示）。

        用法：
          /铁锈列表                查看第一页（有缓存则直接用缓存，不重新抓取）
          /铁锈列表 2              直接跳到第 2 页
          /铁锈列表 刷新           忽略缓存重新抓取
          /铁锈列表 房间中         只看已开始的房间
          /铁锈列表 不满           只看未满员的房间
          /铁锈列表 真人           只看真人房
          /铁锈列表 官方           只看官方 Auto Server
          /铁锈列表 搜索 关键词    按房名或房号搜索
          /铁锈列表 房间中 2       筛选条件与页码可组合

        筛选规则：给新的筛选词会整体替换旧筛选；只给页码则沿用旧筛选；
        不带任何参数则重新查全部。

        数据来源有两路：
          · 真人房：RTSBox 第三方聚合接口（不是官方接口），房多但字段有限
          · 官方 Auto Server：官方列表，带区域标记，但只有官方开的房

        每行会标出序号，之后可用 /铁锈查房 <序号> 直接查该房；
        序号对应的真实地址会随本次列表一起缓存（默认 10 分钟有效），
        过期后需要重新发一次列表。
        翻页也可用 /铁锈下一页 与 /铁锈上一页。
        别名：房间列表 / 铁锈房列表 / rwlist。
        """
        _src = self._cmd_list_impl(event, arg)
        async for _r in self._as_async_iter(_src):
            yield _r

    # ---- 列表指令别名 ----
    @filter.command("房间列表")
    async def cmd_list_alias1(self, event: AstrMessageEvent, arg: str = ""):
        """查看铁锈战争房间列表（铁锈列表 的别名）。

        支持页码、刷新、筛选、搜索，用法与 /铁锈列表 完全一致。
        """
        _src = self._cmd_list_impl(event, arg)
        async for _r in self._as_async_iter(_src):
            yield _r

    @filter.command("铁锈房列表")
    async def cmd_list_alias2(self, event: AstrMessageEvent, arg: str = ""):
        """查看铁锈战争房间列表（铁锈列表 的别名）。

        支持页码、刷新、筛选、搜索，用法与 /铁锈列表 完全一致。
        """
        _src = self._cmd_list_impl(event, arg)
        async for _r in self._as_async_iter(_src):
            yield _r

    @filter.command("rwlist")
    async def cmd_list_alias3(self, event: AstrMessageEvent, arg: str = ""):
        """查看铁锈战争房间列表（铁锈列表 的别名）。

        英文短名，支持页码、刷新、筛选、搜索，用法与 /铁锈列表 完全一致。
        """
        _src = self._cmd_list_impl(event, arg)
        async for _r in self._as_async_iter(_src):
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
            _ag = self._send_text(event, 
                "当前会话没有房间列表权限。\n"
                "管理员可用 /铁锈模式 与 /铁锈白名 调整放行范围。"
                + self._allowed_detail(is_private, uid, gid)
            )
            async for _r in _ag:
                yield _r
            return

        # 取该 QQ 的历史状态(已过期则视为无)
        st = self._state_get(event)
        # 顺手清理其它已过期的 QQ 状态
        if len(self._list_page_state()) > 50:
            self._cleanup_states()

        # 默认筛选条件(可由配置指定)
        keyword = ""
        status = ""
        full = self.cfg.get("list_default_full", "")  # ""/hide
        source = "all"

        page = 1
        force = False
        # 本次是否显式给了筛选条件:
        #   给了 -> 整体丢弃旧筛选, 只用本次的
        #   没给 -> 沿用该 QQ 上次的条件
        has_filter_arg = False

        # ---- 参数解析表: 单遍扫描, 支持 "房间中 2" / "搜索 岛屿 2" / "刷新" ----
        FILTER_WORDS = {
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
                # "搜索 关键词" 或 "搜索关键词"；关键词可含空格，取到末尾全部
                kw = ""
                if idx + 1 < len(toks):
                    idx += 1
                    kw = " ".join(toks[idx:])
                    idx = len(toks)          # 剩余 token 已并入关键词
                if not kw:
                    _ag = self._send_text(event, "用法：/铁锈列表 搜索 <关键词>")
                    async for _r in _ag:
                        yield _r
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
                if field == "status":
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
                _ag = self._send_text(event, 
                    "已清空本账号的房间列表筛选状态，下一条 /铁锈列表 将从全部房间重新开始"
                )
                async for _r in _ag:
                    yield _r
                return
            else:
                bad = t
                break
            idx += 1

        if bad:
            _ag = self._send_text(event, 
                f"无法识别的参数：{bad}\n\n"
                "【房间列表用法】\n"
                "/铁锈列表 [页码] [筛选] [搜索 关键词] [刷新]\n"
                "筛选：房间中/游戏中、满/不满、真人/官方（空格分隔，可组合）\n"
                "页码：纯数字，如 2　刷新：忽略缓存重抓\n"
                "例：/铁锈列表 房间中 2　/铁锈列表 搜索 我的 世界\n"
                "/铁锈下一页　/铁锈上一页"
            )
            async for _r in _ag:
                yield _r
            return

        # 无参数 = 全新查询, 不继承任何筛选
        # 只在「给了参数但没给筛选词」时沿用该 QQ 上次的条件(如 /铁锈列表 2、/铁锈下一页)
        if st and toks and not has_filter_arg:
            keyword = st.get("keyword", "")
            status = st.get("status", "")
            full = st.get("full", "")
            source = st.get("source", "all")

        rooms_all, cached = await self._list_fetch(force=force)
        if rooms_all is None:
            _ag = self._send_text(event, "房间列表获取超时，请稍后重试")
            async for _r in _ag:
                yield _r
            return
        if not rooms_all:
            _ag = self._send_text(event, "没有获取到房间数据，请稍后重试")
            async for _r in _ag:
                yield _r
            return

        # 本地筛选
        def match(r):
            rst = (r.get("status") or "").lower()
            if status and rst != status:
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
            _ag = self._send_text(event, "没有符合条件的房间，试试 /铁锈列表（不带参数）")
            async for _r in _ag:
                yield _r
            return

        # 只按每页条数分页，不限制总房间数
        matched_total = len(rooms)
        page_size = 5
        try:
            page_size = max(1, int(self.cfg.get("list_page_size", 5)))
        except (TypeError, ValueError):
            page_size = 5
        total_pages = max(1, (len(rooms) + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_rooms = rooms[start:start + page_size]

        # 保存该 QQ 的翻页/筛选状态
        # items 只存当前页, 与列表展示的序号一一对应; 查房时直接用 addr 或 room_id
        # join_a 非 0 表示该条是中继房, 地址需另行解析(仅内部使用, 不展示)
        self._state_put(event, page, keyword, status, full, source,
                        total_pages,
                        [{
                            "addr": str(r.get("addr") or ""),
                            "room_id": str(r.get("room_id") or ""),
                            "name": str(r.get("name") or ""),
                            "uuid": str(r.get("id") or ""),
                            "join_a": str(r.get("join_a") or "0"),
                        } for r in page_rooms])
        # 落盘到 KV：翻页状态要能撑过插件重启 / 长时间不操作。
        # 这里 await 而不是 create_task —— 列表指令本身是异步的，
        # 顺手写完更确定（见 _state_save_kv 的说明）。
        await self._state_save_kv(event)

        # ---- 组装消息 ----
        conds = []
        if source == "qroom":
            conds.append("真人房")
        elif source == "official":
            conds.append("官方房")
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
        if conds:
            header += f"（{'/'.join(conds)}）"
        header += f"　第 {page}/{total_pages} 页"
        if cached and not force:
            header += "　[缓存]"

        lines = [header, ""]
        status_cn = {"battleroom": "房间中", "ingame": "游戏中",
                     "locked": "已锁定", "chat": "聊天", "unknown": "-"}
        # 官Q模式：标题留块外，房间正文整体进一个代码块（可滚动文本框）
        room_lines = []
        for idx, rm in enumerate(page_rooms, 1):
            host = str(rm.get("name") or "?")[:16]
            mapname = str(rm.get("map") or "-")[:28]
            pl = rm.get("players", 0)
            mx = rm.get("max", 0)
            sts = status_cn.get((rm.get("status") or "").lower(), "-")
            # 一条房间两行：首行序号/房名/人数/状态，次行地名与房主
            # 合成一行 —— 旧版把地图、房主、官方自动房各占一行，列表一长就散。
            detail = [f"地图：{mapname}"]
            hostname = str(rm.get("host") or "").strip()
            if hostname and hostname not in host:
                detail.append(f"房主：{hostname[:24]}")
            if rm.get("is_auto_server"):
                detail.append("官方自动房")
            room_lines.append(f"{idx}. {host}　{pl}/{mx}　{sts}")
            room_lines.append("  " + "　".join(detail))
        _official = False
        try:
            _official = bool(self.cfg.get("官Q模式", False))
        except Exception:
            _official = False
        if _official and room_lines:
            # ```txt：纯文本代码块，QQ 自动收成可滚动区域（就是折叠）
            lines.append("```txt")
            # 代码块内不能出现 ``` 本身，防御性替换
            lines.extend(ln.replace("```", "'''") for ln in room_lines)
            lines.append("```")
        else:
            lines.extend(room_lines)
        lines.append("")
        lines.append(f"/铁锈查房 <序号> 查详情（本条 1-{len(page_rooms)}）")
        nav = []
        if page > 1:
            nav.append("/铁锈上一页")
        if page < total_pages:
            nav.append("/铁锈下一页")
        if total_pages > 1:
            nav.append(f"/铁锈列表 <页码> 直达 1-{total_pages}")
        if nav:
            # 翻页相关指令并到一行，别占三四行结尾
            lines.append("　".join(nav))
        _ag = self._send_text(event, "\n".join(lines))
        async for _r in _ag:
            yield _r

    @filter.command("铁锈下一页")
    async def cmd_list_next(self, event: AstrMessageEvent, arg: str = ""):
        """房间列表翻到下一页。

        沿用上一次的筛选条件（房间中 / 不满 / 真人 / 官方 / 搜索关键词）
        与已抓取的数据缓存，只改变页码。

        **页码跨会话持久保存（内存 + KV）**：
          缓存过期后点「下一页」又回到第 1/4 页，根因是旧版把整套状态
          一起过期、页码跟着一起丢了。
          现在页码不过期：内存没有就从 KV 读（`_state_load`），
          真的从未翻过页才从第 1 页开始。

        第 1 次点「下一页」（还没有任何基准）→ 展示第 1 页，
        用户看到列表后再点一次才是第 2 页 —— 与「翻到下一页」的直觉一致。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        st = self._state_get(event)
        if not st:
            # 内存里没有：先试 KV（插件重启 / 隔了很久的场景）
            st = await self._state_load(event)
        if not st or not st.get("page"):
            # 确实从没查过：展示第 1 页，用户再点一次就是第 2 页
            self.logger.info("[列表] 下一页：无任何历史状态，展示第 1 页")
            _src = self._cmd_list_impl(event, "1")
            async for r in self._as_async_iter(_src):
                yield r
            return
        cur = int(st.get("page", 1) or 1)
        total = int(st.get("total", 0) or 0)
        if total and cur >= total:
            _ag = self._send_text(event, f"已经是最后一页了（第 {cur}/{total} 页）")
            async for _r in _ag:
                yield _r
            return
        nxt = cur + 1
        self.logger.info(f"[列表] 下一页：{cur} -> {nxt}")
        _src = self._cmd_list_impl(event, str(nxt))
        async for r in self._as_async_iter(_src):
            yield r

    @filter.command("铁锈上一页")
    async def cmd_list_prev(self, event: AstrMessageEvent, arg: str = ""):
        """房间列表翻到上一页。

        与 /铁锈下一页 对称，沿用上一次的筛选条件与数据缓存，
        只改变页码。页码同样跨会话持久保存（内存 + KV）。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        st = self._state_get(event)
        if not st:
            st = await self._state_load(event)
        if not st or not st.get("page"):
            self.logger.info("[列表] 上一页：无任何历史状态，展示第 1 页")
            _src = self._cmd_list_impl(event, "1")
            async for r in self._as_async_iter(_src):
                yield r
            return
        cur = int(st.get("page", 1) or 1)
        if cur <= 1:
            _ag = self._send_text(event, "已经是第一页了")
            async for _r in _ag:
                yield _r
            return
        prv = max(1, cur - 1)
        self.logger.info(f"[列表] 上一页：{cur} -> {prv}")
        _src = self._cmd_list_impl(event, str(prv))
        async for r in self._as_async_iter(_src):
            yield r

    @filter.command("铁锈查房")
    async def cmd_query_room(self, event: AstrMessageEvent, arg: str = ""):
        """查询单个铁锈战争房间的详细信息（显式查房）。

        用法：
          /铁锈查房 2              查列表里第 2 号房间（需先发 /铁锈列表）
          /铁锈查房 r5132          直接按房号查询
          /铁锈查房 1.2.3.4:5123   按 IP:端口 直连查询

        输出包含：房名、房号、地图、当前/最大人数、房主、收入倍率、
        初始资金、迷雾、游戏模式、初始单位、是否禁核、共享控制、
        允许旁观、队伍锁定、玩家列表（需开启玩家列表显示）等。

        与自动识别不同，这里是用户明确指定目标，所以一定会给出回复：
        成功返回房间详情，失败也明确说明原因（房号不存在 / 连接超时 /
        验证码房等），不参与防重复发送的去重，也不会被静默吞掉。

        遇到验证码房会提示「验证码房无法继续」。这是官方中继的反机器人
        验证机制，硬闯会导致 IP 被封，因此不做重试。
        使用「序号」形式时，若列表缓存已过期，会提示先重新发一次列表。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return
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
            _ag = self._send_text(event, 
                "当前会话没有查房权限。\n"
                "管理员可用 /铁锈模式 与 /铁锈白名 调整放行范围。"
                + self._allowed_detail(is_private, uid, gid)
            )
            async for _r in _ag:
                yield _r
            return

        USAGE = (
            "用法：/铁锈查房 <序号|房号>\n"
            "序号 - 先发 /铁锈列表，再填列表里的序号（如 /铁锈查房 2）\n"
            "房号 - r5132 / s1024 / Q12345 / hytvvi"
        )
        if not arg:
            # 不带参数只给用法
            _ag = self._send_text(event, USAGE)
            async for _r in _ag:
                yield _r
            return

        # ---- 解析目标 ----
        target = ""
        desc = ""
        if IP_QUERY_RE.match(arg):
            target = arg
            desc = arg
        elif arg.isdigit():
            # 序号 = 当前页第 N 条；addrs 过期则按原页码就地重建，不拉第 1 页
            st = self._state_get(event)
            if not st:
                st = await self._state_load(event)
            items = self._state_addrs(event)
            if not items:
                if await self._refill_addrs(event):
                    items = self._state_addrs(event)
                if not items:
                    _ag = self._send_text(event,
                        "取不到列表序号对应的房间（列表数据暂时不可用）。\n"
                        "可直接用房号查询：{0}铁锈查房 r5132".format(self._prefix_of(event) or "/"))
                    async for _r in _ag:
                        yield _r
                    return
            n = int(arg)
            if n < 1 or n > len(items):
                _ag = self._send_text(event, 
                    f"序号超出范围：当前页共 {len(items)} 条"
                )
                async for _r in _ag:
                    yield _r
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
                        # 解析失败: 不能用 :5158 入口直连(会被踢或串到别的房),
                        # 明确报错而不是静默退回入口地址。
                        _ag = self._send_text(event, 
                            f"第 {n} 条 {label} 无法解析真实地址，暂时无法查询"
                        )
                        async for _r in _ag:
                            yield _r
                        return
            else:
                label = ""
                target = str(item or "").strip()
            if not target:
                _ag = self._send_text(event, 
                    f"第 {n} 条没有可用的地址或房号，无法查询"
                )
                async for _r in _ag:
                    yield _r
                return
            desc = f"第 {n} 条 {label}".strip()
        else:
            rids = extract_room_ids(arg)
            if not rids:
                _ag = self._send_text(event, 
                    f"无法识别的房号：{arg}\n\n" + USAGE
                )
                async for _r in _ag:
                    yield _r
                return
            target = rids[0]
            desc = target

        # ---- 分配探针并查询 ----
        idx = self._acquire_probe()
        if idx is None:
            _ag = self._send_text(event, "探针全部被占用，请稍后再试")
            async for _r in _ag:
                yield _r
            return

        result = ""
        try:
            max_retry = 1
            try:
                max_retry = max(1, min(10, int(self.cfg.get("retry_times", 3))))
            except (TypeError, ValueError):
                max_retry = 1
            attempt = 0
            while attempt < max_retry:
                attempt += 1
                # 第一次带时间后缀; 后续缩短名字, 降低被过滤概率
                if attempt == 1:
                    probe_name = f"{self._probe_prefix()}探针{idx:02d}_{self._short_ts()}"
                else:
                    probe_name = f"{self._probe_prefix()}探针{idx:02d}_{attempt}"
                try:
                    result = await asyncio.wait_for(
                        self._query_room(target, probe_name, gid),
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
                    f"{desc} 探针名被过滤，重试中（{attempt}/{max_retry}）"
                )
        finally:
            self._release_probe(idx)

        # ---- 回传(显式查房不静默, 必须给出原因) ----
        if is_room_info(result):
            # 记住「这个会话上次查的是哪个房」，供刷新/复述使用。
            # 与翻页状态同源（内存 + KV），重启后仍然有效。
            try:
                self._remember_room(event, target, desc, result)
                await self._room_save_kv(event)
            except Exception as e:
                self.logger.debug(f"[查房] 记录上次房间失败(忽略): {e}")
            # 用户主动 /铁锈查房, 必须给回答
            await self._send_room_result(event, result)
            return
        if not result:
            _ag = self._send_text(event, 
                f"{desc} 查房无响应（房间不存在、中继繁忙或地址不可达）"
            )
            async for _r in _ag:
                yield _r
        elif is_error_info(result):
            _ag = self._send_text(event, translate_error(result))
            async for _r in _ag:
                yield _r
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
            msg = f"{desc} 未获取到房间信息"
            if tail:
                msg += f"\n{tail[:200]}"
            _ag = self._send_text(event, msg)
            async for _r in _ag:
                yield _r

    @filter.command("铁锈全局解析")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_global(self, event: AstrMessageEvent, arg: str = ""):
        """开关全局自动解析（是否响应群里的房号并自动查房）。

        用法：
          /铁锈全局解析          查看当前状态
          /铁锈全局解析 开       允许自动查房
          /铁锈全局解析 关       完全关闭自动查房（显式 /铁锈查房 仍可用）

        关闭后本插件不再自动识别消息中的房号，适合临时静默的场景。
        该开关与白/黑名单是「与」的关系：全局关掉则任何群都不会自动触发。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = (arg or "").strip()
        if not arg:
            state = "开启" if self.cfg.get("global_enabled", True) else "关闭"
            _ag = self._send_text(event, f"全局解析当前状态：{state}")
            async for _r in _ag:
                yield _r
            return
        if arg not in ("开", "关"):
            _ag = self._send_text(event, "用法：/铁锈全局解析 开|关")
            async for _r in _ag:
                yield _r
            return
        self._write_config("global_enabled", (arg == "开"))
        self._save_config()
        _ag = self._send_text(event, f"全局解析已{'开启' if self.cfg['global_enabled'] else '关闭'}")
        async for _r in _ag:
            yield _r

    @filter.command("铁锈模式")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_mode(self, event: AstrMessageEvent, arg: str = ""):
        """切换名单模式（决定自动查房在哪些群/用户生效）。

        用法：
          /铁锈模式              查看当前模式
          /铁锈模式 白           白名单模式：只有名单内的群/用户才响应
          /铁锈模式 黑           黑名单模式：名单内的群/用户不响应，其余都响应
          /铁锈模式 混合         混合模式：#群号 与 @用户 分开管理，@用户优先

        混合模式下：用户黑名单优先于群白名单，用户白名单优先于群黑名单；
        群里两者都不在时，看混合白名单里是否存在群条目决定默认放行还是拦截。
        切换模式后，/铁锈白名 与 /铁锈黑名 会自动指向当前模式对应的名单。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = (arg or "").strip()
        MODE_MAP = {"白": "white", "黑": "black", "混合": "mixed"}
        NAME = {"white": "白名单(仅名单内放行)", "black": "黑名单(名单内禁用)", "mixed": "混合模式(@用户优先于#群)"}
        if not arg:
            mode = self.cfg.get("mode", "black")
            _ag = self._send_text(event, f"当前模式：{NAME[mode]}")
            async for _r in _ag:
                yield _r
            return
        if arg not in MODE_MAP:
            _ag = self._send_text(event, "用法：/铁锈模式 白|黑|混合")
            async for _r in _ag:
                yield _r
            return
        new_mode = MODE_MAP[arg]
        old_mode = self.cfg.get("mode", "black")
        if new_mode != old_mode:
            if new_mode == "mixed":
                # 切到混合: 若混合名单为空, 用当前白/黑名单作为初始(保留上次混合配置)
                if not self.cfg.get("混合_白名单"):
                    self._write_config("混合_白名单", list(self.cfg.get("白名单", [])))
                if not self.cfg.get("混合_黑名单"):
                    self._write_config("混合_黑名单", list(self.cfg.get("黑名单", [])))
            # 从混合切回白/黑: 白/黑名单保持切换前状态(独立保存), 混合名单保留供下次切回时恢复
            self._write_config("mode", new_mode)
            self._save_config()
        _ag = self._send_text(event, f"当前模式已切换为：{NAME[new_mode]}")
        async for _r in _ag:
            yield _r

    @filter.command("铁锈玩家列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_show_players(self, event: AstrMessageEvent, arg: str = ""):
        """开关查房结果里是否显示玩家列表。

        用法：
          /铁锈玩家列表          查看当前状态
          /铁锈玩家列表 开       显示玩家名与队伍
          /铁锈玩家列表 关       只显示人数，不列玩家名

        玩家列表较长，房间人多时建议关闭以保持消息简洁。
        关闭后仍会显示当前人数与最大人数。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = (arg or "").strip()
        if not arg:
            state = "开启" if self.cfg.get("show_players", False) else "关闭"
            _ag = self._send_text(event, f"玩家列表显示当前状态：{state}")
            async for _r in _ag:
                yield _r
            return
        if arg not in ("开", "关"):
            _ag = self._send_text(event, "用法：/铁锈玩家列表 开|关")
            async for _r in _ag:
                yield _r
            return
        self._write_config("show_players", (arg == "开"))
        self._save_config()
        _ag = self._send_text(event, f"玩家列表显示已{'开启' if self.cfg['show_players'] else '关闭'}")
        async for _r in _ag:
            yield _r

    @filter.command("铁锈白名")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_white(self, event: AstrMessageEvent, arg: str = ""):
        """维护白名单（名单模式为「白」或「混合」时生效）。

        用法：
          /铁锈白名                      查看当前名单
          /铁锈白名 +#群号               把群加入白名单
          /铁锈白名 +@QQ号               把用户加入白名单
          /铁锈白名 -#群号               从白名单移除
          /铁锈白名 -@QQ号               从白名单移除

        白名单模式下：只有名单内的群/用户才会自动查房，其余一律不响应。
        混合模式下会写入「混合白名单」，与黑名单分开管理。
        加号与减号可连写多条，用空格分隔。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        mixed = self.cfg.get("mode") == "mixed"
        key = "混合_白名单" if mixed else "白名单"
        opp = "混合_黑名单" if mixed else None
        label = "混合白名单" if mixed else "白名单"
        # AstrBot 只把第一个空格前的参数传给 arg，`+ #xxx` 这种写法会丢后半截，
        # 所以先从原始消息里补全完整参数。
        arg = self._raw_args(event, arg, "铁锈白名", "白名单")
        text = self._cmd_list_text(arg, key, label, opp, cmd_name="铁锈白名")
        # 落盘在 _cmd_list_text 内部按需完成，这里不再重复写一次
        _ag = self._send_text(event, text)
        async for _r in _ag:
            yield _r

    @filter.command("铁锈黑名")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_black(self, event: AstrMessageEvent, arg: str = ""):
        """维护黑名单（名单模式为「黑」或「混合」时生效）。

        用法：
          /铁锈黑名                      查看当前名单
          /铁锈黑名 +#群号               把群加入黑名单
          /铁锈黑名 +@QQ号               把用户加入黑名单
          /铁锈黑名 -#群号               从黑名单移除
          /铁锈黑名 -@QQ号               从黑名单移除

        黑名单模式下：名单内的群/用户不响应，其余都自动查房。
        混合模式下会写入「混合黑名单」，其中 @用户 优先级高于 #群号。
        加号与减号可连写多条，用空格分隔。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        mixed = self.cfg.get("mode") == "mixed"
        key = "混合_黑名单" if mixed else "黑名单"
        opp = "混合_白名单" if mixed else None
        label = "混合黑名单" if mixed else "黑名单"
        # 同 cmd_white：从原始消息补全完整参数，避免 `+ #xxx` 被空格切断
        arg = self._raw_args(event, arg, "铁锈黑名", "黑名单")
        text = self._cmd_list_text(arg, key, label, opp, cmd_name="铁锈黑名")
        # 落盘在 _cmd_list_text 内部按需完成，这里不再重复写一次
        _ag = self._send_text(event, text)
        async for _r in _ag:
            yield _r
    @filter.command("铁锈重试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_retry(self, event: AstrMessageEvent, arg: str = ""):
        """设置探针名被过滤时的自动重试次数。

        用法：
          /铁锈重试                查看当前次数
          /铁锈重试 5              设为重试 5 次（范围 0-10）

        查房时探针会以「ABAB探针NN_后缀」的形式进入房间。部分房主会
        设置名字过滤规则，导致进房被拒；此时插件会换更短的名字重试。
        次数设太小可能因偶发过滤而查房失败，设太大则会拖慢响应，
        默认 3 次是较平衡的选择。设为 0 表示不重试。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = (arg or "").strip()
        if not arg:
            n = self.cfg.get("retry_times", 3)
            _ag = self._send_text(event, f"探针名被过滤时自动重试次数：{n}")
            async for _r in _ag:
                yield _r
            return
        if not arg.isdigit() or int(arg) < 0 or int(arg) > 10:
            _ag = self._send_text(event, "用法：/铁锈重试 [0-10]")
            async for _r in _ag:
                yield _r
            return
        self._write_config("retry_times", int(arg))
        self._save_config()
        _ag = self._send_text(event, f"重试次数已设置为：{self.cfg['retry_times']}")
        async for _r in _ag:
            yield _r

    def _diag_text(self, event, section: str = "") -> str:
        """生成运行时诊断信息. 不修改任何状态, 只读取。

        section 为空 -> 输出全部（沿用旧行为）。
        section 非空 -> 只输出对应分类，便于长输出时按需查看：
            session  当前会话
            recall   撤回配置 + 待撤回
            probe    探针池
            list     房间列表
            config   主要配置
        展示原则: 说人话。内部 key 一律翻译成「群 123456 / 用户 3856554669」,
        时间一律换算成「还剩 1分23秒」, 不出现 probe_next 这类实现细节。
        """
        L = ["【铁锈查房调试】"] if not section else []

        def want(name: str) -> bool:
            """这一节要不要输出。"""
            return not section or section == name

        def safe(fn, default="<异常>"):
            try:
                return fn()
            except Exception as e:
                return f"<{type(e).__name__}: {e}>"

        def _human_key(k: str) -> str:
            """内部 key -> 人话。"""
            k = str(k or "")
            if k.startswith("g#"):
                rest = k[2:]
                gid, _, uid = rest.partition("@")
                if uid:
                    return f"群 {gid} / 用户 {uid}"
                return f"群 {gid}"
            if k.startswith("p@"):
                return f"用户 {k[2:]}（私聊）"
            if k == "*":
                return "所有群（默认）"
            return k or "(空)"

        def _human_secs(sec: float) -> str:
            sec = int(max(0, sec))
            if sec < 60:
                return f"{sec}秒"
            m, s = divmod(sec, 60)
            if m < 60:
                return f"{m}分{s}秒" if s else f"{m}分"
            h, m = divmod(m, 60)
            return f"{h}时{m}分" if m else f"{h}时"

        gid_raw = safe(event.get_group_id)
        uid_raw = safe(event.get_sender_id)
        is_priv = safe(event.is_private_chat)
        is_admin = safe(event.is_admin)
        key, is_private = self._recall_session_key(event)

        # ---- 1. 当前会话 ----
        if want("session"):
            L.append("")
            L.append("-- 当前会话 --")
            if is_priv is True:
                L.append(f"类型：私聊　用户：{normalize_gid(uid_raw) or '?'}")
            else:
                L.append(f"类型：群聊　群号：{normalize_gid(gid_raw) or '?'}　"
                         f"发送者：{normalize_gid(uid_raw) or '?'}")
            L.append(f"管理员：{'是' if is_admin is True else '否'}　"
                     f"（本插件判定：{'是' if self._is_admin(event) else '否'}）")
            L.append(f"撤回配置匹配键：{_human_key(key) if key else '(未识别)'}")

        # ---- 2. 撤回配置 ----
        cfg = self._get_recall_cfg(event)
        store = self._recall_store()
        if want("recall"):
            L.append("")
            L.append("-- 撤回配置 --")
            if cfg["开"]:
                L.append(f"本会话生效：已开启，延迟 {_human_secs(cfg['延迟'])}")
            else:
                L.append("本会话生效：未开启")
            L.append(f"配置总条目：{len(store)}")
            if store:
                valid = 0
                for e in store:
                    p = self._recall_parse(e)
                    if p is None:
                        L.append(f"  ! {e!r}　格式非法，已忽略")
                    else:
                        valid += 1
                        if p["延迟"] < 0:
                            L.append(f"  {_human_key(p['key'])}  关闭（覆盖默认）")
                        else:
                            L.append(f"  {_human_key(p['key'])}  延迟 {_human_secs(p['延迟'])}")
                if valid != len(store):
                    L.append(f"  合法 {valid} 条、非法 {len(store)-valid} 条")
            else:
                L.append("  (空：全部关闭)")
            L.append("改配置：/铁锈撤回 列出｜+｜-｜重置")

            # ---- 3. 待撤回任务（与撤回配置同属一节）----
            L.append("")
            L.append("-- 待撤回消息 --")
            now = time.time()
            rows = []
            for tid, m in list(self._recall_meta.items()):
                remain = m.get("due", now) - now
                rows.append((m.get("key", ""), m.get("mid"), remain, m.get("delay", 0)))
            L.append(f"任务数：{len(rows)}")
            if rows:
                rows.sort(key=lambda x: x[2])     # 最快到期的在前
                for k, mid, remain, delay in rows:
                    state = "即将撤回" if remain <= 0 else f"还剩 {_human_secs(remain)}"
                    L.append(f"  {_human_key(k)}  {state}（设定 {_human_secs(delay)}）")
            else:
                L.append("  (无)")

        # ---- 4. 探针池 ----
        if want("probe"):
            L.append("")
            L.append("-- 探针池 --")
            st = self._probe_stats()
            L.append(f"占用中：{len(st['占用中'])} 个　可复用：{len(st['可复用'])} 个　"
                     f"空闲容量：{st['空闲容量']}/{st['容量']}")
            if st["占用中"]:
                L.append("  正在跑：" + "、".join(f"{i:02d}" for i in st["占用中"]))
            if st["可复用"]:
                L.append("  待复用：" + "、".join(f"{i:02d}" for i in st["可复用"]))
            if st["历史最大编号"] is None:
                L.append("  尚未分配过探针")
            else:
                L.append(f"  历史最大编号：{st['历史最大编号']:02d}（只增不减，避免并发撞号）")
            L.append(f"在途查询任务：{len(self.pending_tasks)}")

        # ---- 5. 房间列表 ----
        if want("list"):
            L.append("")
            L.append("-- 房间列表 --")
            cache = self._list_cache()
            if cache.get("rooms"):
                age = time.time() - cache.get("ts", 0)
                left = self.LIST_CACHE_TTL - age
                if left > 0:
                    L.append(f"数据缓存：{len(cache['rooms'])} 条，还能用 {_human_secs(left)}")
                else:
                    L.append(f"数据缓存：{len(cache['rooms'])} 条，已过期（下次查询会重新拉取）")
            else:
                L.append("数据缓存：空")
            states = self._list_page_state()
            uid = self._list_uid(event)
            L.append(f"翻页状态条目：{len(states)}")
            mine = states.get(uid)
            if mine:
                L.append(
                    f"  本会话：第 {mine.get('page', 1)} 页　"
                    f"筛选={mine.get('status') or '全部'}　"
                    f"来源={mine.get('source') or 'all'}　"
                    f"序号缓存 {len(mine.get('addrs') or [])} 条"
                )
                try:
                    addr_age = time.time() - float(mine.get("data_ts") or 0)
                except (TypeError, ValueError):
                    addr_age = 1e9
                addr_left = self.ADDRS_TTL - addr_age
                if addr_left > 0:
                    L.append(f"  序号缓存还能用 {_human_secs(addr_left)}")
                else:
                    L.append("  序号缓存已过期（下次按序号查房会自动重建）")
            else:
                L.append("  本会话：无（先发 /铁锈列表 才有序号可用）")
            if len(states) > 1:
                others = [k for k in states if k != uid][:5]
                L.append("  其他会话有状态的：" + "、".join(_human_key(k) for k in others))

        # ---- 6. 其他配置 ----
        if want("config"):
            L.append("")
            L.append("-- 主要配置 --")
            L.append(f"全局解析：{'开' if self.cfg.get('global_enabled') else '关'}　"
                     f"名单模式：{self.cfg.get('mode')}")
            L.append(f"私聊查房：{'开' if self.cfg.get('私聊启用') else '关'}　"
                     f"显示玩家：{'开' if self.cfg.get('show_players') else '关'}　"
                     f"重试：{self.cfg.get('retry_times')}")
            L.append(f"每页 {self.cfg.get('list_page_size', 5)} 条（总数不限）")
            L.append(
                f"白名单 {len(self.cfg.get('白名单', []))}、"
                f"黑名单 {len(self.cfg.get('黑名单', []))}、"
                f"混合白 {len(self.cfg.get('混合_白名单', []))}、"
                f"混合黑 {len(self.cfg.get('混合_黑名单', []))}"
            )
            L.append(f"房号冷却：{_human_secs(self._cooldown_secs())}")
            L.append(f"冷却提示：{'开' if self.cfg.get('冷却提示') else '关'}　"
                     f"官Q模式：{'开' if self.cfg.get('官Q模式') else '关'}　"
                     f"debug：{'开' if self.cfg.get('debug') else '关'}")

        # 统一收口：分类标题前保证一个空行、去掉开头的空行
        # （section 指定某一节时 L 为空列表，首行就是标题）
        out = []
        for ln in L:
            if ln.startswith("-- ") and ln.endswith(" --") \
                    and out and out[-1].strip():
                out.append("")
            out.append(ln)
        return "\n".join(out).lstrip("\n")

    def _diag_text_safe(self, event, section: str = "") -> str:
        """生成诊断文本；任何一节崩掉都不让整条命令失败。

        诊断命令是排障入口 —— 用户正是遇到问题时才敲它，如果它自己抛异常，
        就什么都拿不到了。所以这里做两级兜底：
          1. 正常路径：直接返回 _diag_text 的结果，零额外开销；
          2. 抛异常时：不再整体失败，改为逐分类重跑，把成功的节拼起来，
             崩掉的那一节标注异常类型、消息和出错位置（文件名:行号）。
             全部都是只读操作，重跑没有任何副作用。

        之所以要标注「文件名:行号」：_diag_text 里手写了大量取值表达式，
        漏一个 safe() 守卫就会在某一节炸掉，只给异常消息很难定位到具体行。
        """
        try:
            return self._diag_text(event, section)
        except Exception as err:  # noqa: BLE001 —— 诊断命令必须兜住一切
            where = ""
            tb = err.__traceback__
            while tb is not None and tb.tb_next is not None:
                tb = tb.tb_next
            if tb is not None:
                where = f" @ {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
            self.logger.warning(
                f"诊断输出异常(已降级为部分输出): {type(err).__name__}: {err}{where}",
                exc_info=True)
            if section:
                # 用户只要了这一分类 -> 直接说明它挂了，别假装成功
                return f"（该分类生成时抛出 {type(err).__name__}：{err}{where}）"
            # 全量输出时中途崩: 逐分类重跑，能出的都出
            parts = []
            for key, name, _desc in self.DIAG_SECTIONS:
                if key == "off":
                    continue      # off 分类由 cmd_diag 转发给官机探测，不在这里生成
                try:
                    parts.append(self._diag_text(event, key))
                except Exception as sub:  # noqa: BLE001
                    parts.append(f"-- {name} --\n"
                                 f"（该分类生成时抛出 {type(sub).__name__}：{sub}）")
            parts.append(f"（注：全量输出曾在 {type(err).__name__}：{err}{where} 处中断，"
                         f"以上为逐分类重跑结果）")
            # 每节自成一段：节与节之间留一个空行，和正常路径的排版一致
            return "\n\n".join(p.strip("\n") for p in parts if p.strip("\n"))

    def _cooldown_secs(self) -> int:
        """读取房号冷却秒数(带兜底)。"""
        try:
            return max(0, min(3600, int(self.cfg.get("房号冷却秒", COOLDOWN))))
        except (TypeError, ValueError):
            return COOLDOWN

    DIAG_SECTIONS = (
        ("session", "会话", "当前会话、群号、管理员判定"),
        ("recall", "撤回", "撤回配置与待撤回任务"),
        ("probe", "探针", "探针池占用与在途任务"),
        ("list", "列表", "房间列表缓存与翻页状态"),
        ("config", "配置", "主要配置项一览"),
        ("off", "官机", "官方通道探测与发送测试（官机 | 官机测试 | 官机 e）"),
    )

    def _diag_resolve(self, arg: str) -> str:
        """把用户输入的分类（中文/英文/简写）解析成内部 section。

        返回 "" 表示要全部；返回 "?" 表示无法识别（应展示菜单）。
        """
        a = str(arg or "").strip().lower()
        if not a or a in ("全部", "all", "full", "*"):
            return ""
        # 支持「官机测试」「官机 e」这类带子参数的写法：
        # 只有首词命中分类时才认，剩余部分留给调用方（见 _diag_sub）
        # 注意：用户很可能不打空格（「官机测试」），所以还要做前缀匹配，
        # 否则整个词匹配不上、被判成「未知分类」。
        head = a.split()[0]
        prefixes = (("官机", "off"), ("off", "off"), ("official", "off"))
        for pfx, key in prefixes:
            if a.startswith(pfx):
                return key
        alias = {
            "会话": "session", "session": "session", "s": "session",
            "撤回": "recall", "recall": "recall", "r": "recall",
            "探针": "probe", "probe": "probe", "p": "probe",
            "列表": "list", "list": "list", "l": "list",
            "配置": "config", "config": "config", "cfg": "config", "c": "config",
            "官机": "off", "off": "off", "o": "off", "official": "off",
        }
        if a in alias:
            return alias[a]
        # 首词命中分类（支持「官机测试」「官机 e」）
        if head in alias:
            return alias[head]
        for key, name, _ in self.DIAG_SECTIONS:
            if a == name.lower() or a == key or head == name.lower() or head == key:
                return key
        return "?"

    def _diag_sub(self, arg: str) -> str:
        """取分类之后的子参数。

        `/铁锈查房调试 官机测试` -> "测试"
        `/铁锈查房调试 官机 e`    -> "e"
        `/铁锈查房调试 官机`      -> ""
        首词是分类本身时，剩余部分整体返回（小写）。
        """
        parts = str(arg or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            # 无空格情况：「官机测试」 -> 返回 "测试"
            # 注意顺序：长前缀优先，否则 "official" 会被 "off" 截成 "icial"
            a = str(arg or "").strip()
            for pfx in ("官机", "official", "off"):
                if a == pfx:
                    return ""          # 本身就是分类名，没有子参数
                if a.startswith(pfx):
                    return a[len(pfx):].strip().lower()
            return ""
        return parts[1].strip().lower()

    @filter.command("铁锈查房调试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_diag(self, event: AstrMessageEvent, arg: str = ""):
        """打印运行时诊断信息，用于排查配置与状态问题。

        输出内容：
          · 当前会话：群聊/私聊、群号、发送者、是否管理员、
            撤回配置实际匹配到的键
          · 撤回配置：本会话是否生效、延迟多少、全部配置条目
            （格式非法的条目会单独标出来并注明已忽略）
          · 待撤回消息：逐条显示属于哪个会话、还剩多久撤回、设定延迟多少
          · 探针池：占用中的编号、可复用编号、空闲容量、在途查询任务数
          · 房间列表：数据缓存是否还有效、本会话的页码/筛选/序号缓存情况
          · 主要配置：全局解析、名单模式、私聊查房、显示玩家、重试次数、
            每页条数、四个名单的条目数、房号冷却与冷却提示、官Q模式、debug

        为了便于直接判断问题，内部标识已翻译成人话：
          · 会话键 g#群号@QQ号 显示为「群 123456 / 用户 789」
          · 时间统一换算成「还剩 1分23秒」这类可读形式
          · 探针编号不再显示内部递增指针，改为「占用中 / 可复用 / 空闲容量」

        权限：仅管理员（群聊/私聊一致，由 AstrBot 权限系统拦截）
             —— 私聊用户排查自己会话的问题不需要群主授权。
        本指令只读不写，不会改动任何配置或状态，可随时执行。

        用法：
          /铁锈查房调试            输出全部运行时状态
          /铁锈查房调试 探针       只看某一分类（会话|撤回|探针|列表|配置）
          /铁锈查房调试菜单        查看「可修改的内部数值」

        关于「调试菜单」配置：它现在只影响本命令的展示方式
        （关闭后 /铁锈查房调试 <分类> 会被忽略，总是输出全部），
        不再影响「/铁锈查房调试菜单」这条独立命令。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        use_menu = bool(self.cfg.get("调试菜单", True))
        if not use_menu:
            # 关闭分类功能：总是输出全部
            _ag = self._send_text(event, self._diag_text_safe(event), kind="diag")
            async for _r in _ag:
                yield _r
            return

        section = self._diag_resolve(arg)
        if section == "?":
            # 无法识别的分类 -> 提示 + 全量（不让人卡住）
            tip = str(arg or "").strip()
            body = self._diag_text_safe(event)
            body += f"\n\n（未识别的分类：{tip}，已显示全部）"
            _ag = self._send_text(event, body, kind="diag")
            async for _r in _ag:
                yield _r
            return
        if not section:
            # 未给分类 -> 全部（保持「调试」这条命令的本意）
            _ag = self._send_text(event, self._diag_text_safe(event), kind="diag")
            async for _r in _ag:
                yield _r
            return

        # ---- 官机分类：转发到官机探测 / 官机测试 ----
        # 合并了原本的 /铁锈官机探测 与 /铁锈官机测试 两条独立命令，
        # 避免指令表里堆一堆「看起来像测试版」的条目。
        if section == "off":
            sub = self._diag_sub(arg)
            if sub.startswith(("测试", "test", "t")):
                # 「官机测试 e」-> 详细模式
                detail = sub[len("测试"):].strip() if sub.startswith("测试") else \
                         sub[len("test"):].strip() if sub.startswith("test") else \
                         sub[1:].strip()
                _src = self._do_official_test(event, detail)
                async for r in self._as_async_iter(_src):
                    yield r
                return
            # 默认走探测；「官机 e」也走探测（探测本身已足够详细）
            _src = self._do_official_probe(event, sub)
            async for r in self._as_async_iter(_src):
                yield r
            return

        text = self._diag_text_safe(event, section)
        name = next((n for k, n, _ in self.DIAG_SECTIONS if k == section),
                    section)
        if not text.strip():
            text = f"【铁锈查房调试 · {name}】\n\n(无内容)"
        else:
            text = f"【铁锈查房调试 · {name}】\n{text}"
        _ag = self._send_text(event, text, kind="diag")
        async for _r in _ag:
            yield _r

    # ---------------------------------------------------------- 调试菜单
    def _menu_groups(self):
        """菜单分组定义：(分组名, [(显示名, 当前值, 修改方式), ...])。

        只列**能在聊天里改**的项，避免菜单变成第二份配置说明。
        """
        onoff = lambda v: "开" if v else "关"
        cd = self._cooldown_secs()
        prefix = self._prefix_of()
        return [
            ("查房开关", [
                ("全局解析",
                 onoff(self.cfg.get("global_enabled", True)),
                 "/铁锈全局解析 开|关"),
                ("名单模式",
                 str(self.cfg.get("mode", "black")),
                 "/铁锈模式 白|黑|混合"),
                ("私聊查房",
                 onoff(self.cfg.get("私聊启用", True)),
                 "/铁锈私聊开关 开|关"),
                ("混合私聊策略",
                 str(self.cfg.get("混合_私聊策略", "white")),
                 "/铁锈私聊策略 白|黑"),
                ("显示玩家",
                 onoff(self.cfg.get("show_players", False)),
                 "/铁锈玩家列表 开|关"),
                ("探针重试次数",
                 str(self.cfg.get("retry_times", 3)),
                 "/铁锈重试 <0-10>"),
            ]),
            ("冷却与撤回", [
                ("房号冷却", f"{cd} 秒", "配置文件：查房行为 → 房号冷却秒"),
                ("冷却提示", onoff(self.cfg.get("冷却提示", False)),
                 "配置文件：查房行为 → 冷却提示"),
                ("自动撤回配置",
                 f"{len(self._recall_store())} 条",
                 "/铁锈撤回 列出 | + key:秒 | - key"),
            ]),
            ("排版与通道", [
                ("官Q模式", onoff(self.cfg.get("官Q模式", False)),
                 "配置文件：基础设置 → 官Q模式（含 Markdown + 按钮 + 换行规整）"),
                ("压缩范围", self._compress_label(),
                 f"配置文件：基础设置 → 压缩范围 | {prefix}铁锈压缩 <none|room|diag|all>"),
                ("指令唤醒词", self._prefix_of(),
                 "配置文件：基础设置 → 指令唤醒词（须与 AstrBot 主配置一致）"),
                ("官方通道", self._official_channel_desc(),
                 f"{prefix}铁锈查房调试 官机 查看通道详情"),
                ("上次发送异常", (getattr(self, "_last_md_error", "") or "无")[:120],
                 f"{prefix}铁锈查房调试 官机 e 查看详情"),
                ("调试分类", onoff(self.cfg.get("调试菜单", True)),
                 "配置文件：基础设置 → 调试菜单"),
                ("调试日志", onoff(self.cfg.get("debug", False)),
                 "配置文件：基础设置 → 调试日志"),
             ]),
        ]
    def _menu_text(self, event) -> str:
        """生成调试菜单正文（含可修改项与当前值）。"""
        try:
            gid = normalize_gid(event.get_group_id()) if event is not None else ""
            self._menu_gid = gid if not (
                event is not None and event.is_private_chat()) else ""
        except Exception:
            self._menu_gid = ""

        L = ["【铁锈查房调试菜单】"]
        L.append("下面这些数值可以直接改")
        for title, rows in self._menu_groups():
            L.append("")
            L.append(f"-- {title} --")
            for name, value, how in rows:
                L.append(f"  {name}：{value}")
                L.append(f"　　改法：{how}")
        L.append("")
        L.append("看详细运行状态：/铁锈查房调试")
        L.append("看单个分类：/铁锈查房调试 探针|会话|撤回|列表|配置")
        return "\n".join(L)

    @filter.command("铁锈查房调试菜单")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_diag_menu(self, event: AstrMessageEvent, arg: str = ""):
        """查看「可修改的内部数值」及其当前值。

        只列出能在聊天里改、或需要知道当前值的项，并给出修改方式。
        与 /铁锈查房调试 的分工：
          · /铁锈查房调试菜单 —— 我有哪些开关、现在是什么值、怎么改
          · /铁锈查房调试     —— 现在运行得怎么样（状态与诊断）

        权限与调试一致：仅管理员。
        本指令只读不写。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        _ag = self._send_text(event, self._menu_text(event), kind="diag")
        async for _r in _ag:
            yield _r

    @filter.command("铁锈压缩")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_compress(self, event: AstrMessageEvent, arg: str = ""):
        """切换消息压缩范围（视觉折叠）。

        压缩范围统一改一个配置项，按钮不再占用卡片位置，
        改由本命令与配置面板调整。

        用法：
          /铁锈压缩                查看当前值
          /铁锈压缩 关             关闭压缩（= none）
          /铁锈压缩 查房           只压缩查房结果（= room）
          /铁锈压缩 列表           只压缩房间列表（= list）
          /铁锈压缩 调试           只压缩调试输出（= diag）
          /铁锈压缩 帮助           只压缩帮助与菜单（= help）
          /铁锈压缩 全部           全部压缩（= all，覆盖前四项）

        权限：仅管理员（改的是全局配置）。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = self._raw_args(event, arg, "铁锈压缩").strip().lower()
        if not arg:
            _ag = self._send_text(event,
                f"当前压缩范围：{self._compress_label()}\n"
                f"用法：/铁锈压缩 <范围>\n"
                f"  关 - 不压缩\n"
                f"  查房 - 只压缩查房结果\n"
                f"  列表 - 只压缩房间列表\n"
                f"  调试 - 只压缩调试输出\n"
                f"  帮助 - 只压缩帮助与菜单\n"
                f"  全部 - 全部压缩（覆盖前四项）\n"
                f"说明：官方机器人（官Q模式）用 ``` 代码块折叠，个人机器人改用合并转发卡片"
            )
            async for _r in _ag:
                yield _r
            return

        table = {
            "关": "none", "不压缩": "none", "none": "none", "off": "none",
            "查房": "room", "查房信息": "room", "room": "room",
            "列表": "list", "房间列表": "list", "list": "list",
            "调试": "diag", "调试信息": "diag", "diag": "diag",
            "帮助": "help", "帮助信息": "help", "help": "help",
            "全部": "all", "所有": "all", "all": "all",
        }
        val = table.get(arg)
        if val is None:
            _ag = self._send_text(event,
                f"无法识别的压缩范围：{arg}\n"
                f"可用值：关 / 查房 / 列表 / 调试 / 帮助 / 全部")
            async for _r in _ag:
                yield _r
            return
        self._write_config("压缩范围", val)
        self._save_config()
        note = ""
        if val == "all":
            note = "\n（「全部」已覆盖「查房」「列表」「调试」「帮助」四项）"
        _ag = self._send_text(event, f"压缩范围已设为：{self._compress_label()}{note}")
        async for _r in _ag:
            yield _r

    @filter.command("铁锈官机探测")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_official_probe_legacy(self, event: AstrMessageEvent, arg: str = ""):
        """旧入口（保留兼容）：等价于 /铁锈查房调试 官机。

        新版本已把它合并进 /铁锈查房调试 的「官机」分类。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        _src = self._do_official_probe(event, arg)
        async for r in self._as_async_iter(_src):
            yield r

    async def _do_official_probe(self, event, arg: str = ""):
        """官方通道探测（只读，不发送）。

        不同 AstrBot 版本、不同适配器把官方 API 挂在完全不同的位置
        （`event.bot.api` / `event.bot` 自己 / `event._bot` / 根本没有 bot）。
        靠猜方法名永远猜不全，「选了 markdown 没效果」就会卡在一个
        无法自证的死循环里。这条命令不走任何猜测，直接把现场结构输出，
        一次就能定位该用哪个入口。

        只读，不改任何状态。权限：仅管理员。
        """
        lines = ["【官机通道探测】"]
        # ---- 1. event 上的候选属性 ----
        lines.append("")
        lines.append("-- event 属性 --")
        for attr in ("bot", "_bot", "client", "api", "adapter", "platform",
                     "self_id", "message_obj"):
            try:
                v = getattr(event, attr, None)
            except Exception as e:
                lines.append(f"{attr}：<读取异常 {type(e).__name__}>")
                continue
            if v is None:
                lines.append(f"{attr}：None")
            else:
                lines.append(f"{attr}：{type(v).__name__} = {self._short_repr(v)}")

        # ---- 2. event 上所有含 bot/api/client/send 的方法 ----
        lines.append("")
        lines.append("-- event 可用方法 --")
        try:
            names = [n for n in dir(event) if not n.startswith("__")]
        except Exception:
            names = []
        hits = [n for n in names
                if any(k in n.lower() for k in ("bot", "api", "client", "send", "post"))]
        lines.append(", ".join(hits[:40]) if hits else "(无匹配)")

        # ---- 3. bot 对象的方法清单（关键） ----
        bot = getattr(event, "bot", None)
        if bot is None:
            bot = getattr(event, "_bot", None)
        lines.append("")
        lines.append("-- bot 对象 --")
        if bot is None:
            lines.append("取不到 bot（event.bot 与 event._bot 都是 None）")
            lines.append("=> 说明本适配器不通过 event 暴露 bot。")
            lines.append("请把下面这行贴给我：")
            lines.append("平台适配器名：" + str(self._platform_name_of(event)))
        else:
            lines.append(f"类型：{type(bot).__module__}.{type(bot).__name__}")
            try:
                meths = [n for n in dir(bot)
                         if not n.startswith("__") and callable(getattr(bot, n, None))]
            except Exception:
                meths = []
            # 优先展示可能是发送口的
            pri = [n for n in meths if any(k in n.lower() for k in
                   ("send", "post", "message", "markdown", "keyboard", "group", "c2c"))]
            lines.append("疑似发送方法：")
            lines.append("  " + (", ".join(pri[:25]) if pri else "(无)"))
            other = [n for n in meths if n not in pri]
            lines.append("其它方法（前 20）：")
            lines.append("  " + (", ".join(other[:20]) if other else "(无)"))
            # ---- 4. bot.api 子对象 ----
            api = getattr(bot, "api", None)
            lines.append("")
            lines.append("-- bot.api --")
            if api is None:
                lines.append("None（bot 上没有 api 属性）")
            else:
                lines.append(f"类型：{type(api).__module__}.{type(api).__name__}")
                try:
                    ameths = [n for n in dir(api)
                              if not n.startswith("__") and callable(getattr(api, n, None))]
                except Exception:
                    ameths = []
                pri2 = [n for n in ameths if any(k in n.lower() for k in
                        ("post", "send", "message", "markdown"))]
                lines.append("发送相关：")
                lines.append("  " + (", ".join(pri2[:25]) if pri2 else "(无)"))

        # ---- 5. 当前插件会怎么选 ----
        lines.append("")
        lines.append("-- 本插件的判断结果 --")
        fn, style, holder = self._official_api(bot, event)
        if fn is None:
            lines.append("官方通道：未找到（将降级为纯文本）")
        else:
            lines.append(f"官方通道：{getattr(fn, '__name__', '?')}｜{style}")
            lines.append(f"所在对象：{type(holder).__name__ if holder is not None else '?'}")
        lines.append(f"官Q模式：{'开' if self.cfg.get('官Q模式') else '关'}")
        lines.append(f"压缩范围：{self._compress_label()}")
        lines.append(f"平台名：{self._platform_name_of(event)}")
        mid = self._passive_msg_id(event)
        lines.append(f"被动回复 msg_id：{mid if mid else '取不到'}")
        try:
            lines.append(f"群号：{event.get_group_id()}（类型 {type(event.get_group_id()).__name__}）")
        except Exception as e:
            lines.append(f"群号读取失败：{type(e).__name__}")

        _ag = self._send_text(event, "\n".join(lines))
        async for _r in _ag:
            yield _r

    def _short_repr(self, v, limit: int = 60) -> str:
        """安全地取对象的短表示，异常时返回类型名。"""
        try:
            s = repr(v)
        except Exception:
            return "<repr 失败>"
        s = s.replace("\n", " ")
        return s if len(s) <= limit else s[:limit] + "..."

    def _platform_name_of(self, event) -> str:
        """尽量取出平台名/适配器名，用于诊断输出。"""
        for attr in ("get_platform_name", "platform", "get_platform", "adapter"):
            try:
                v = getattr(event, attr, None)
                if v is None:
                    continue
                if callable(v):
                    v = v()
                s = str(v or "")
                if s:
                    return s[:80]
            except Exception:
                continue
        return "(未知)"

    @filter.command("铁锈官机测试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_official_test_legacy(self, event: AstrMessageEvent, arg: str = ""):
        """旧入口（保留兼容）：等价于 /铁锈查房调试 官机测试。

        新版本已把「官机探测 / 官机测试」合并进 /铁锈查房调试 的「官机」分类，
        这条命令仅为兼容旧用法而保留，内部直接转发。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        _src = self._do_official_test(event, arg)
        async for r in self._as_async_iter(_src):
            yield r

    async def _do_official_test(self, event, arg: str = ""):
        """官机发送测试的实际实现（被合并命令与 /铁锈查房调试 官机测试 共用）。

        `铁锈官机测试 e` —— 额外打印异常堆栈，用于深挖。
        权限：仅管理员。
        """
        verbose = arg.strip().lower() in ("e", "error", "详情", "堆栈")

        # 用固定的假数据，不受真实房间状态影响
        sample = ("房间版本：1.15\n"
                  "地图名称：测试地图 (10p)\n"
                  "初始资金：4000¤[1.0倍率]\n"
                  "当前人数：7/10\n"
                  "ID：test01")
        lines = ["【官机发送测试】"]

        md = self._to_markdown(sample)
        lines.append("")
        lines.append("-- 1. Markdown 转换 --")
        lines.append("成功，%d 行" % len(md.splitlines()))
        lines.append("预览：")
        for ln in md.splitlines()[:6]:
            lines.append("  " + ln)

        bot = getattr(event, "bot", None)
        fn, style, holder = self._official_api(bot, event)
        lines.append("")
        lines.append("-- 2. 通道 --")
        if fn is None:
            lines.append("未找到官方发送方法")
        else:
            lines.append(f"方法：{getattr(fn, '__name__', '?')}")
            lines.append(f"风格：{style}")
            lines.append(f"所在对象：{type(holder).__name__ if holder is not None else '?'}")

        # ---- 3. 真实发送 ----
        lines.append("")
        lines.append("-- 3. 实际发送 --")
        kb = None
        if self.cfg.get("官Q模式", False):
            try:
                kb = self._keyboard_of(sample, event)
            except Exception as e:
                lines.append(f"按钮构造失败：{e}")
        before = getattr(self, "_last_md_error", None)
        try:
            ok, mid = await self._send_official_markdown(event, sample, kb)
            lines.append(f"返回：ok={ok} mid={mid}")
            if ok:
                lines.append("=> 已发出，请查看上一条消息的实际效果")
            else:
                lines.append("=> 未发出（或已异常），已降级为纯文本")
        except Exception as e:
            lines.append(f"调用抛异常：{type(e).__name__}：{e}")
            if verbose:
                import traceback
                lines.append(traceback.format_exc()[:1200])

        err = getattr(self, "_last_md_error", None)
        if err and err != before:
            lines.append("")
            lines.append("-- 4. 上次异常 --")
            lines.append(err[:500])

        # ---- 5. 备选路径：event 自带的 markdown fallback ----
        lines.append("")
        lines.append("-- 5. event 自带能力 --")
        sfm = getattr(event, "send_with_markdown_fallback", None)
        if callable(sfm):
            lines.append("send_with_markdown_fallback：存在（AstrBot 内置降级接口）")
            lines.append("  若上面发送失败，可改走这个接口")
        else:
            lines.append("send_with_markdown_fallback：不存在")

        if verbose:
            lines.append("")
            lines.append("-- 6. bot.api 签名 --")
            api = getattr(bot, "api", None) if bot is not None else None
            if api is not None:
                pgm = getattr(api, "post_group_message", None)
                if pgm is not None:
                    try:
                        import inspect
                        lines.append("post_group_message 签名：")
                        lines.append("  " + str(inspect.signature(pgm))[:400])
                        pk = getattr(api, "post_keyboard_message", None)
                        if pk is not None:
                            lines.append("post_keyboard_message 签名：")
                            lines.append("  " + str(inspect.signature(pk))[:400])
                    except Exception as e:
                        lines.append(f"读签名失败：{e}")

        _ag = self._send_text(event, "\n".join(lines))
        async for _r in _ag:
            yield _r

    @filter.command("铁锈探针时解")
    async def cmd_probe_ts(self, event: AstrMessageEvent, arg: str = ""):
        """解析探针名后缀，还原探针的进房时间。

        探针在游戏内显示为「ABAB探针NN_xxxxx」，下划线后的 5 位是用
        base36 编码的「月日时分秒」压缩值，可以无损反解回具体时间。

        用法：
          /铁锈探针时解 fkhk3              只给 5 位后缀
          /铁锈探针时解 ABAB探针01_fkhk3   直接贴完整探针名

        输出：探针的进房时间（年月日时分秒），以及距今已经过去多久。
        用途：在游戏里看到某个探针还占着房间，可反查它是哪一次查房留下的、
        已经待了多长时间，用来判断是不是陈旧的残留连接。

        三层校验，避免给出错误时间：
          · 长度必须正好 5 位
          · 字符必须在 0-9 / a-z 的 base36 表内
          · 回环校验：解出的时间重新编码后必须与原串完全一致
        因此随手编一串字母会被明确拒绝，而不会返回一个看起来合理、
        实际错误的进房时间。

        年份按当前年推断；若解出的日期比现在晚超过一天，则判定为去年。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = self._raw_args(event, arg, "铁锈探针时解")
        arg = (arg or "").strip()
        if not arg:
            _ag = self._send_text(event, 
                "用法：/铁锈探针时解 <5位后缀>\n"
                "例：/铁锈探针时解 fkhk3　/铁锈探针时解 ABAB探针01_fkhk3\n"
                "后缀是玩家列表里探针名的下划线后半部分"
            )
            async for _r in _ag:
                yield _r
            return

        # 从完整探针名里截取后缀, 否则原样当作后缀
        raw = arg
        suffix = ""
        if "_" in raw:
            suffix = raw.rsplit("_", 1)[-1].strip()
        else:
            suffix = raw

        suffix = suffix.lower()
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"

        # 校验: 长度 5, 字符都在 base36 字母表内
        if len(suffix) != 5:
            _ag = self._send_text(event, 
                f"后缀应为 5 位，你给的是 {len(suffix)} 位：{suffix!r}\n"
                f"探针名形如 ABAB探针01_fkhk3，后缀取 _ 之后的 5 位"
            )
            async for _r in _ag:
                yield _r
            return
        bad = [c for c in suffix if c not in chars]
        if bad:
            _ag = self._send_text(event, 
                f"后缀含非法字符：{''.join(bad)!r}\n"
                f"只允许 0-9 和 a-z（base36）"
            )
            async for _r in _ag:
                yield _r
            return

        try:
            month, day, hour, minute, sec = self._ts_decode(suffix)
        except Exception:
            _ag = self._send_text(event, f"无法解析后缀：{suffix!r}")
            async for _r in _ag:
                yield _r
            return

        # 合理性校验: 月份 1-12, 日 1-31, 时 0-23, 分/秒 0-59
        if not (1 <= month <= 12 and 1 <= day <= 31
                and 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= sec <= 59):
            _ag = self._send_text(event, 
                f"后缀 {suffix!r} 解出的时间不合理（{month}月{day}日 "
                f"{hour:02d}:{minute:02d}:{sec:02d}），可能不是本插件生成的探针名"
            )
            async for _r in _ag:
                yield _r
            return

        # 回环校验: 把解出的时间重新编码, 必须与原后缀完全一致。
        # 这一层能挡住"碰巧落在合法范围内、但其实不是编码产物"的字符串
        # (如 fffff 会解出 12月32日 这种越界值; 而 aaaaa 之类可能解出
        #  看似合法、但重新编码对不上的时间)。
        re_enc = self._ts_encode(month, day, hour, minute, sec)
        if re_enc != suffix:
            _ag = self._send_text(event, 
                f"后缀 {suffix!r} 不是有效的探针时间编码：解出 {month}月{day}日 "
                f"{hour:02d}:{minute:02d}:{sec:02d}，重新编码却得到 {re_enc!r}\n"
                f"请确认复制的是 ABAB探针NN_xxxxx 里下划线后的 5 位"
            )
            async for _r in _ag:
                yield _r
            return

        _ag = self._send_text(event,
            f"探针进房时间：{month}月{day}日 "
            f"{hour:02d}:{minute:02d}:{sec:02d}"
        )
        async for _r in _ag:
            yield _r


    @filter.command("铁锈撤回")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_recall(self, event: AstrMessageEvent, arg: str = ""):
        """自动撤回配置：按会话独立管理消息自动撤回。

        用法：
          /铁锈撤回                  查看本会话生效配置
          /铁锈撤回 列出             列出全部配置（群 / 私聊 / 默认）
          /铁锈撤回 + #群号:60       添加或修改（可跟多条）
          /铁锈撤回 - #群号          删除（可跟多条）
          /铁锈撤回 重置             清空全部配置
          /铁锈撤回 开|关|秒数       快捷设置本会话
          /铁锈撤回 + *:60           展开到机器人加入的所有群

        权限：仅管理员。由 @filter.permission_type(ADMIN) 统一拦截，
              群聊与私聊一致，函数内不再做任何权限判断。
        说明：配置格式为 key:延迟，key 前缀 # 表示群、@ 表示用户、* 表示所有群。
             写进配置即启用，删掉即关闭。参数末尾加 * 表示对所有群生效。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = self._recall_full_args(event, arg)
        key, is_private = self._recall_session_key(event)
        if self.cfg.get("debug", False):
            self.logger.debug(
                f"[撤回] 收到指令 raw_arg={arg!r} key={key!r} is_private={is_private}"
            )
        # 权限由 @filter.permission_type(ADMIN) 统一拦截（群聊/私聊都需管理员）
        # 解析末尾 '*' 通配符
        wildcard = False
        parts = arg.split()
        if parts and parts[-1] == "*":
            wildcard = True
            parts = parts[:-1]
            arg = " ".join(parts)
        if not key and not wildcard:
            # 拿不到会话标识(平台差异): 明确告知, 不再静默吞掉
            _ag = self._send_text(event, 
                "无法识别当前会话，撤回配置未生效。\n"
                "请在群聊或私聊中直接使用，或用 /铁锈查房调试 查看详情"
            )
            async for _r in _ag:
                yield _r
            return
        scope = ("全部群" if wildcard else (f"本群({key})" if not is_private else "本私聊"))

        # ---- 列出 ----
        if arg in ("列出", "列表", "list", "ls"):
            store = self._recall_store()
            L = [f"【撤回配置】共 {len(store)} 条"]
            if not store:
                L.append("(空)")
            else:
                for e in store:
                    p = self._recall_parse(e)
                    if p is None:
                        L.append(f"  ! {e}  （格式非法，已忽略）")
                    else:
                        k = p["key"]
                        if k == "*":
                            label = "所有群（默认）"
                        elif k.startswith("#"):
                            label = f"群 {k[1:]}"
                        else:
                            label = f"用户 {k[1:]}（私聊）"
                        if p["延迟"] < 0:
                            L.append(f"  {label}  →  关闭（覆盖默认）")
                        else:
                            L.append(f"  {label}  →  {p['延迟']} 秒")
            L.append("")
            L.append("增删：/铁锈撤回 + #群号:60 @qq:30　/铁锈撤回 - #群号")
            L.append("清空：/铁锈撤回 重置")
            _ag = self._send_text(event, "\n".join(L))
            async for _r in _ag:
                yield _r
            return

        # ---- 重置 ----
        if arg in ("重置", "清空", "reset", "clear"):
            store = self._recall_store()
            n = len(store)
            self._write_config("撤回", [])
            self._save_config()
            self.logger.debug(f"[撤回] 重置, 清空 {n} 条")
            _ag = self._send_text(event, f"已清空撤回配置（原 {n} 条）")
            async for _r in _ag:
                yield _r
            return

        # ---- 批量增删: + / - ----
        if arg[:1] in ("+", "-") and len(arg) > 1:
            add = arg[0] == "+"
            spec = arg[1:].strip()
            # 支持 " - *" 这种把 '*' 附带在末尾的写法
            if not spec:
                _ag = self._send_text(event, 
                    "用法：/铁锈撤回 + #群号:60 @qq:30\n"
                    "　　　/铁锈撤回 - #群号"
                )
                async for _r in _ag:
                    yield _r
                return

            # 处理单独的通配符: '*' 需要展开成所有已知群
            raw_tokens = spec.split()
            wc_delay = None
            rest_tokens = []
            for t in raw_tokens:
                if t == "*":
                    wc_delay = self.RECALL_DELAY_DEFAULT
                elif t.startswith("*:"):
                    p = self._recall_parse(t)
                    if p:
                        wc_delay = p["延迟"]
                    else:
                        _ag = self._send_text(event, f"{t} 格式不对（应为 *:延迟）")
                        async for _r in _ag:
                            yield _r
                        return
                else:
                    rest_tokens.append(t)

            if wc_delay is not None:
                if add:
                    # 主动向平台拉取机器人加入的全部群, 不只是"见过"的群
                    all_groups = await self._recall_all_groups(event)
                    added = self._recall_expand_wildcard(
                        wc_delay, event, groups=all_groups)
                    known = len(all_groups)
                    extra = ""
                    if known <= 1:
                        extra = (
                            "\n（平台没有返回群列表；不同适配器能力不同，"
                            "如 OneBot v11 才支持 get_group_list）"
                        )
                    _ag = self._send_text(event, 
                        f"已把 * 展开到 {known} 个群：新增 {added} 条，延迟 {wc_delay} 秒"
                        f"{extra}\n用 /铁锈撤回 列出 查看具体群号"
                    )
                    async for _r in _ag:
                        yield _r
                else:
                    store = self._recall_store()
                    keep = []
                    removed = 0
                    for e in store:
                        p = self._recall_parse(e)
                        if p and p["key"].startswith("#"):
                            removed += 1
                            continue
                        keep.append(e)
                    self._write_config("撤回", keep)
                    self._save_config()
                    _ag = self._send_text(event, f"已删除 {removed} 条群配置")
                    async for _r in _ag:
                        yield _r
                if not rest_tokens:
                    return
                spec = " ".join(rest_tokens)

            ok_n, fails = self._recall_apply_batch(spec, add)
            L = []
            if ok_n:
                L.append(f"已{'添加/更新' if add else '删除'} {ok_n} 条")
            if fails:
                L.append("失败：")
                for tok, why in fails:
                    L.append(f"  {tok}  →  {why}")
            if not L:
                L.append("没有可处理的内容")
            _ag = self._send_text(event, "\n".join(L))
            async for _r in _ag:
                yield _r
            return

        # ---- 旧用法兼容: 裸数字/开/关, 作用于当前会话 ----
        if not arg:
            cfg = self._get_recall_cfg(event, want_wildcard=wildcard)
            state = "开启" if cfg["开"] else "关闭"
            _ag = self._send_text(event, 
                f"{scope}自动撤回：{state}，延迟 {cfg['延迟']} 秒\n"
                f"改配置：/铁锈撤回 列出｜+｜-｜重置"
            )
            async for _r in _ag:
                yield _r
            return

        if arg in ("关", "关闭", "off"):
            # 先看"现在是否生效"再看能不能删：群聊里可能没有自己的条目，
            # 只是继承了 "*" 默认，那种情况下 _set_recall_cfg 会写显式关闭记录。
            if not self._get_recall_cfg(event, want_wildcard=wildcard)["开"]:
                _ag = self._send_text(event, f"{scope}本来就没有开启自动撤回")
                async for _r in _ag:
                    yield _r
                return
            self._set_recall_cfg(event, wildcard=wildcard, remove=True)
            _ag = self._send_text(event, f"{scope}自动撤回已关闭")
            async for _r in _ag:
                yield _r
            return

        if arg in ("开", "开启", "on"):
            self._set_recall_cfg(event, delay=self.RECALL_DELAY_DEFAULT, wildcard=wildcard)
            _ag = self._send_text(event, 
                f"{scope}自动撤回已开启，延迟 {self.RECALL_DELAY_DEFAULT} 秒"
            )
            async for _r in _ag:
                yield _r
            return

        if arg.isdigit() and 0 <= int(arg) <= self.RECALL_DELAY_MAX:
            self._set_recall_cfg(event, delay=int(arg), wildcard=wildcard)
            _ag = self._send_text(event, f"{scope}自动撤回延迟已设置为：{int(arg)} 秒")
            async for _r in _ag:
                yield _r
            return

        _ag = self._send_text(event, 
            "用法：/铁锈撤回 <子命令>\n"
            "  列出 - 列出全部配置\n"
            "  + #群号:60 - 添加/修改（可跟多条）\n"
            "  - #群号 - 删除（可跟多条）\n"
            "  重置 - 清空全部配置\n"
            "  开|关|秒数 - 快捷设置本会话\n"
            "  + *:60 - 展开到所有已知群\n"
            "不带参数 = 查看本会话配置；群聊末尾加 * 表示对全部群生效"
        )
        async for _r in _ag:
            yield _r

    @filter.command("铁锈私聊策略")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_private_policy(self, event: AstrMessageEvent, arg: str = ""):
        """设置混合模式下私聊的默认策略。

        用法：
          /铁锈私聊策略           查看当前策略
          /铁锈私聊策略 白        跟随白名单：不在白名单里的私聊不响应
          /铁锈私聊策略 黑        跟随黑名单：只拦黑名单里的私聊，其余响应

        作用范围：仅在 /铁锈模式 为「混合」时生效。
        用来说明「既不在用户白名单、也不在用户黑名单」的私聊用户该
        如何处理——默认放行还是默认拦截。

        白名单 / 黑名单模式下不使用此项，直接按对应名单判断即可。
        选择白（跟随白名单）更保守，能避免陌生人私聊消耗资源；
        选择黑（跟随黑名单）更开放，只拦黑名单里的人。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = (arg or "").strip()
        if not arg:
            p = self.cfg.get("混合_私聊策略", "white")
            name = "跟随白名单(不在白名单拦截)" if p == "white" else "跟随黑名单(在黑名单拦截)"
            _ag = self._send_text(event, f"混合模式私聊默认策略：{name}")
            async for _r in _ag:
                yield _r
            return
        if arg not in ("白", "黑"):
            _ag = self._send_text(event, "用法：/铁锈私聊策略 白|黑")
            async for _r in _ag:
                yield _r
            return
        self._write_config("混合_私聊策略", "white" if arg == "白" else "black")
        self._save_config()
        name = "跟随白名单(不在白名单拦截)" if self.cfg["混合_私聊策略"] == "white" else "跟随黑名单(在黑名单拦截)"
        _ag = self._send_text(event, f"混合模式私聊默认策略已设为：{name}")
        async for _r in _ag:
            yield _r

    @filter.command("铁锈私聊开关")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_private_switch(self, event: AstrMessageEvent, arg: str = ""):
        """开关私聊查房功能。

        用法：
          /铁锈私聊开关           查看当前状态
          /铁锈私聊开关 开        允许在私聊中查房
          /铁锈私聊开关 关        只允许群聊查房，私聊不响应

        关闭后私聊发房号不会触发自动查房，显式 /铁锈查房 也不会响应，
        可减少陌生人私聊带来的资源消耗与骚扰。

        注意：该开关只管「查房」，不影响自动撤回配置——撤回配置在私聊
        中依然按 @QQ号 独立生效，不受此开关控制。
        启用私聊查房时，建议同时用 /铁锈私聊策略 明确陌生人的默认策略。
        """
        _why = self._session_blocked(event)
        if _why:
            _ag = self._send_text(event, _why)
            async for _r in _ag:
                yield _r
            return

        arg = (arg or "").strip()
        if not arg:
            state = "开启" if self.cfg.get("私聊启用", True) else "关闭"
            _ag = self._send_text(event, f"私聊查房处理当前状态：{state}")
            async for _r in _ag:
                yield _r
            return
        if arg not in ("开", "关"):
            _ag = self._send_text(event, "用法：/铁锈私聊开关 开|关")
            async for _r in _ag:
                yield _r
            return
        self._write_config("私聊启用", (arg == "开"))
        self._save_config()
        _ag = self._send_text(event, f"私聊查房处理已{'开启' if self.cfg['私聊启用'] else '关闭'}")
        async for _r in _ag:
            yield _r

    def _cmd_list_text(self, arg, key, label, opposite_key=None, cmd_name=None) -> str:
        arg = (arg or "").strip()
        # ⚠️ 不能写成 self.cfg.get(key, [])：键缺失、或面板里被改成标量时，
        #    拿到的是临时列表，append/remove 只改到临时对象，_save_config
        #    根本存不回去 —— 表现为"提示已加入，重启后名单又空了"。
        lst = self.cfg.get(key)
        if not isinstance(lst, list):
            lst = []
            self.cfg[key] = lst
        if not arg:
            if not lst:
                return f"{label}为空"
            # 如实展示，不再偷偷补前缀。
            # 早期这里会给裸值自动加 "#" 再打印，结果配置里明明是错的
            # （缺前缀，永远不会命中），显示出来却像是对的，
            # 排查时被彻底误导。现在缺前缀的条目会明确标出来。
            shown = []
            bad = 0
            for x in lst:
                if isinstance(x, str) and x.strip()[:1] in ("#", "@"):
                    shown.append(x.strip())
                else:
                    shown.append(f"{x}  ← 缺前缀")
                    bad += 1
            text = f"{label}内：{'、'.join(shown)}"
            if bad:
                text += (
                    f"\n⚠ 其中 {bad} 条缺少前缀，严格匹配下不会命中。"
                    f"群请写成 #群号，用户请写成 @QQ号。"
                )
            return text
        # 解析 +条目 / -条目，支持一条指令连写多条：
        #   /铁锈白名 +#111 +#222     每段可自带 +/-，缺省沿用开头那个符号
        #   /铁锈白名 - #111 #222
        #   /铁锈白名 +111 222        无前缀时整批按群号处理
        # 早期实现把整串当一个标识丢给 fullmatch，所以 `+#111 +#222`
        # 必然报"无效标识" —— 文档承诺了连写，代码并不支持。
        op = arg[0] if arg[0] in "+-" else ""
        tokens = [t for t in arg[1 if op else 0:].split() if t]
        if not tokens:
            return f"用法：/{cmd_name or label} [+@用户ID|+#群号]（可连写多条）"
        results = []
        changed = False
        for tok in tokens:
            sign = tok[0] if tok[:1] in ("+", "-") else op
            body = tok[1:].strip() if tok[:1] in ("+", "-") else tok
            if not body:
                continue
            if not body.startswith(("@", "#")):
                body = "#" + body  # 无前缀默认当群号
            # ⚠️ 这里绝不能像早期那样用 re.search(r"\d+") 抽数字段：
            #    官方机器人的群号 openid 是 32 位哈希（如 3A28BF3EF5FF4F02...），
            #    抽数字会得到 "3"，写进名单的是 "#3"，
            #    而运行时拿完整哈希去比对，永远匹配不上 —— 白名单形同虚设。
            #    规则改为：纯数字群号原样用；否则完整保留（含哈希串）。
            ident = body[1:].strip()
            if not ident or not re.fullmatch(r"[0-9A-Za-z_\-]{3,64}", ident):
                results.append(
                    f"无效标识：{tok}\n"
                    f"可用形式：#群号、#官方群openid、@QQ号、@用户openid"
                )
                continue
            key_val = body[0] + ident  # 前缀 + 完整标识（不截断）
            if sign == "+":
                if key_val in lst:
                    results.append(f"{label}中已存在：{key_val}")
                    continue
                lst.append(key_val)
                changed = True
                # 同名互斥(混合模式): 加入白名单时从黑名单移除同实体, 反之亦然
                opp = self.cfg.get(opposite_key) if opposite_key else None
                if isinstance(opp, list) and key_val in opp:
                    opp.remove(key_val)
                results.append(f"已加入{label}：{key_val}")
            elif sign == "-":
                if key_val not in lst:
                    results.append(f"{label}中不存在：{key_val}")
                    continue
                lst.remove(key_val)
                changed = True
                results.append(f"已移出{label}：{key_val}")
            else:
                results.append(f"用法：/{cmd_name or label} [+@用户ID|+#群号]")
        if changed:
            # 只在真的改动过时落盘；调用方(cmd_white/cmd_black)不再重复保存
            self._save_config()
        return "\n".join(results)

    # ---- 消息监听 ----
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        try:
            # 兜底: 若 __init__ 时事件循环未就绪, 在首条消息补启 bin 清理循环
            self._ensure_bin_cleanup()
            # 取真实正文：适配器的 message_str 只是渲染结果，
            # 对方开了消息压缩时正文被包进「聊天记录」卡片，甚至会整个丢失。
            # 这里会同时尝试 message_str 和原始消息段，取信息量最大的那份。
            text = extract_text_from_event(event, event.message_str or "")
            if not text.strip():
                return
            # 指令消息不参与自动查房：按配置的唤醒词判断，不能用硬编码 "/"。
            # 唤醒词可改成 > ! 等，写死 "/" 会让 `>铁锈查房 r5132` 绕过这里，
            # 于是 extract_room_ids 把 r5132 抓出来再自动查一遍（重复回消息 + 多占探针）。
            _pfx = ""
            try:
                _pfx = self._prefix_of(event)
            except Exception:
                _pfx = ""
            if _pfx and text.strip().startswith(_pfx):
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
            if is_private and not self.cfg.get("私聊启用", True):
                return

            # ---- 检查 0: 权限名单（**必须最先执行**）----
            # 早期版本把权限检查放在查房判定之后，导致：
            #   群不在白名单里，但查房照做，只是最后发结果时才拦截 ——
            #   表现就是"白名单失效"：不在名单的群里照样看到房间信息，
            #   甚至探针已经进房。
            # 现在把名单判定提到最前面：不在名单内的会话，连查房都不做，
            # 消息直接丢弃，零副作用。
            if not self._is_allowed(is_private, uid, gid):
                self.logger.debug(
                    f"[权限] 会话={'私聊' if is_private else '群聊'} "
                    f"群号={gid or '-'} 发送者={uid or '-'} 不在名单内, 整条忽略")
                return

            # ---- 检查 0.5: 全局解析开关（只影响自动触发）----
            # 显式 /铁锈查房、/铁锈列表 等指令不走这里，仍可正常使用；
            # 只有「从消息里自动提取房号」这条路径受全局开关约束。
            if not self.cfg.get("global_enabled", True):
                return

            # ---- 检查 1.5: 机器人自己发的界面文本，绝不提取房号 ----
            # 调试信息 / 帮助菜单 / 调试菜单里含有真实房号（如
            # 「本机待撤回候选: 1 条（s5132）」）和会误判成字母房号的单词
            # （如「名单模式: black」）。若不拦下，别的机器人会把这段
            # 界面文本当作用户查房请求，提取出 ['s5132','black'] 后去查房
            # —— 实测：第三方发调试信息，官方机器人立刻去查 s5132 并回传。
            if self._is_plugin_ui_text(text):
                return

            rids = extract_room_ids(text)
            if not rids:
                return

            # 权限判定已在「检查 0」做过（那时就 return 了），这里不再重复判定。
            if is_private:
                gid = uid  # 后续日志/回传沿用 gid 变量(此处为用户ID)

            self.logger.debug(f"群 {gid} 提取到房号: {rids}")

            # 冷却判定 + 探针分配 (全部同步代码, 单线程 asyncio 下天然原子, 无需加锁) ----
            # 冷却按「会话 + 房号」而非全局房号:
            # 早期是全局 room_cooldown[rid], 导致 A 群刚查过 r5132, B 群再查同一个房
            # 会被静默吞掉 —— 两个群的人互相看不见对方的查询结果。
            # 现在 key = "会话key|房号", 各群独立计时。
            #
            now = time.time()
            tasks = []  # (rid, idx, task)
            sess = self._dedup_key(event)
            try:
                cooldown = max(0, min(3600, int(self.cfg.get("房号冷却秒", COOLDOWN))))
            except (TypeError, ValueError):
                cooldown = COOLDOWN
            # 定期清理冷却字典, 防止长期运行无限膨胀
            if (not self.room_cooldown or self._last_cleanup is None
                    or now - self._last_cleanup > 300):
                stale = [k for k, ts in self.room_cooldown.items() if now - ts > cooldown * 2]
                for k in stale:
                    del self.room_cooldown[k]
                # 清理过期的"本机确实查到过房间"记录（只需覆盖冷却窗口）
                for k in [k for k, ts in self._room_ok.items()
                          if now - ts > max(cooldown * 2, 60)]:
                    del self._room_ok[k]
                self._last_cleanup = now
            to_query = []
            try:
                max_per_msg = max(1, int(self.cfg.get("probe_max_per_msg", 5)))
            except (TypeError, ValueError):
                max_per_msg = 5
            # 按中继可达性优先排序（稳定排序，同优先级保持原出现顺序）。
            # 英文聊天会产出多个纯字母候选，若不排序，真房号可能被
            # 挤出 probe_max_per_msg 名额 —— 自动查房就漏报了。
            rids = sorted(rids, key=_relay_priority)
            cooling = []          # 被冷却挡下、且**确实查到过房间**的房号（用于提示）
            for rid in rids:
                ck = f"{sess}|{rid}"
                last = self.room_cooldown.get(ck, 0)
                hit = False
                if now - last < cooldown:
                    hit = True                       # 本机刚查过
                if hit:
                    # **只有「确实查到过有效房间」的房号才纳入提示候选。**
                    #
                    # 为什么需要这一步：探针是 100% 查房的，
                    # 英文聊天里的 game / hello 也会被当房号送进探针，
                    # 但它们查不到任何信息。这类无效词即使命中了冷却，
                    # 也绝不能提示 —— 否则英文聊天会冒出中文回复。
                    #
                    # 判定依据：本机的 _room_ok（_send_room_result 写入），
                    # 只在 is_room_info 为真时才被写入。
                    if self._room_ok.get(ck, 0):
                        cooling.append(rid)
                    continue
                # 本机查房前先占位，防止同一房号在查询期间被重复触发
                # （这个占位只用于「防重复查」，不用于「提示」）
                self.room_cooldown[ck] = now
                to_query.append(rid)
                if len(to_query) >= max_per_msg:
                    break
            if not to_query:
                self.logger.debug("所有房号均在冷却中")
                # ---- 可选的冷却提示 ----
                # cooling 里只包含「确实查到过房间」的房号
                # （_room_ok 只在本机成功查到房间时写入），
                # 英文聊天里的 game / hello 查不到信息，不会进入提示。
                # 默认关闭（"冷却提示"=False），保持完全静默。
                if cooling and self.cfg.get("冷却提示", False):
                    try:
                        left = 0
                        for rid in cooling:
                            ts = self.room_cooldown.get(f"{sess}|{rid}", 0)
                            if ts:
                                left = max(left, int(cooldown - (now - ts)))
                        left = max(0, left)
                        await self._send_recallable(
                            event,
                            f"房号 {'、'.join(cooling)} 刚查过，"
                            f"请 {left} 秒后再试")
                    except Exception as e:
                        self.logger.debug(f"冷却提示发送失败(忽略): {e}")
                return

            self.logger.debug(f"待查询房号: {to_query}")

            for rid in to_query:
                idx = self._acquire_probe()
                if idx is None:
                    # 所有探针均被占用: 跳过该房号本轮查询(保持冷却中, 下轮消息再触发)
                    self.logger.debug(f"探针池已满, 跳过房号 {rid}")
                    continue
                base_name = f"{self._probe_prefix()}探针{idx:02d}"
                # 短时间标识: 编码"月日时分秒"为5位base36, 可逆解回, 避免过长的数字后缀被过滤
                probe_name = f"{base_name}_{self._short_ts()}"
                task = asyncio.create_task(self._query_room(rid, probe_name, gid))
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
                                # 探针名被过滤: 自动改名重试。
                                # 与显式查房同口径夹取 [1,10]：retry_times=0 时
                                # 两处都应至少试 1 次，否则群里自动识别会一次都不查。
                                try:
                                    max_retry = max(1, min(10, int(
                                        self.cfg.get("retry_times", 3))))
                                except (TypeError, ValueError):
                                    max_retry = 1
                                retried = 0
                                while retried < max_retry:
                                    retried += 1
                                    # 重试过程静默, 只在 debug 日志留痕(避免刷屏)
                                    self.logger.debug(
                                        f"房间 {rid} 探针名被过滤, "
                                        f"重试({retried}/{max_retry})")
                                    # 重试时去掉时间后缀, 直接 _序号 最短, 最不容易被过滤
                                    # 用当前任务的 idx 构造, 避免 for 循环残留的 base_name 串号
                                    new_name = f"{self._probe_prefix()}探针{idx:02d}_{retried}"
                                    retry_result = ""
                                    retry_task = None
                                    try:
                                        retry_task = asyncio.create_task(self._query_room(rid, new_name, gid))
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
                                            _ag = self._send_text(event, 
                                                translate_error(retry_result))
                                            async for _r in _ag:
                                                yield _r
                                        else:
                                            self.logger.debug(
                                                f"房间 {rid} 重试失败(静默): "
                                                f"{retry_result[:120]}")
                                        break
                                else:
                                    # 重试次数用尽: 仅当失败原因值得回传才发
                                    if is_reportable_error(result):
                                        _ag = self._send_text(event, 
                                            translate_error(result))
                                        async for _r in _ag:
                                            yield _r
                                    else:
                                        self.logger.debug(
                                            f"房间 {rid} 重试耗尽(静默): "
                                            f"{result[:120]}")
                            elif is_error_info(result):
                                # 仅"房间确实存在"类错误回传, 其余静默
                                if is_reportable_error(result):
                                    self.logger.info(
                                        f"房间 {rid} 查询失败，回传原因")
                                    _ag = self._send_text(event, 
                                        translate_error(result))
                                    async for _r in _ag:
                                        yield _r
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
