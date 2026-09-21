#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/agy: Antigravity CLI（~/.gemini/antigravity）-> 归一化会话。

格式依据（与 resume-agy 一致）：
  <agy_dir>/brain/<session_id>/.system_generated/logs/transcript.jsonl
  每行一个事件：{created_at, source, type, status, content, error, tool_calls,
  step_index}；created_at 为秒/毫秒时间戳或 ISO 字符串（数值绝对值 < 1e10 视为秒）。
  - 用户真实输入：source=USER_EXPLICIT 且 type=USER_INPUT；content 常包裹
    <USER_REQUEST>...</USER_REQUEST>（取内部文本），<ADDITIONAL_METADATA> 块剔除
  - 模型真实输出：source=MODEL 的 PLANNER_RESPONSE 及其余内容事件
    （EPHEMERAL_MESSAGE 为内部瞬时内容，跳过）
  - 工具结果：type ∈ RESULT_TYPES（LIST_DIRECTORY/RUN_COMMAND/VIEW_FILE 等），
    status=ERROR 视为出错；事件 error 字段单独产出一条错误结果
  - 工具调用：事件 tool_calls 列表；源无独立调用 id，用 step_index:序号 标识
  - 结果事件不携带调用 id，按源时间顺序与最近的未配对调用 FIFO 配对；无配对传 ""
  - CHECKPOINT / CONVERSATION_HISTORY 历史摘要 -> 转换注记
标题：第一条用户消息的首行（截取 60 字符）。
cwd：从工具入参 Cwd / 绝对路径推断（与 resume-agy infer_directory 一致）。
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

META = {"tool": "agy", "storage": "JSONL", "desc": "Antigravity CLI（~/.gemini/antigravity）"}

RESULT_TYPES = {
    "LIST_DIRECTORY", "GREP_SEARCH", "VIEW_FILE", "RUN_COMMAND", "CODE_ACTION",
    "INVOKE_SUBAGENT", "SEARCH_WEB", "ASK_QUESTION", "READ_URL_CONTENT",
}
TYPE_TOOL = {
    "LIST_DIRECTORY": "list_dir", "GREP_SEARCH": "grep_search", "VIEW_FILE": "view_file",
    "RUN_COMMAND": "run_command", "CODE_ACTION": "code_action",
    "INVOKE_SUBAGENT": "invoke_subagent", "SEARCH_WEB": "search_web",
    "ASK_QUESTION": "ask_question", "READ_URL_CONTENT": "read_url_content",
}
SUMMARY_TYPES = {"CHECKPOINT", "CONVERSATION_HISTORY"}


def default_dir():
    return os.environ.get("ANTIGRAVITY_HOME") or os.path.expanduser(
        os.path.join("~", ".gemini", "antigravity"))


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p)))) if p else ""


def parse_jsonl(path):
    """读 transcript JSONL，返回事件列表（坏行跳过）。"""
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        raise cs.ConvertError(f"无法读取 transcript 文件：{e}")
    return events


def value_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # 紧凑分隔符与 JS JSON.stringify 输出一致，保证双语言指纹相同
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def clean_visible_text(value):
    """提取 USER_REQUEST 内文，否则剔除 ADDITIONAL_METADATA 块（与 resume-agy 一致）。"""
    text = str(value or "").strip()
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text,
                  flags=re.DOTALL).strip()


def clean_path(value):
    """工具入参里的路径可能是 JSON 字符串字面量（带引号），先解包。"""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                text = decoded
        except json.JSONDecodeError:
            text = text[1:-1]
    return text


def timestamp_ms(value):
    """秒/毫秒时间戳或 ISO 字符串 -> 毫秒；无法解析返回 0（与 resume-agy 一致）。"""
    if value is None or value == "":
        return 0
    try:
        number = float(value)
        return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0


def ms_to_iso(ms):
    """毫秒时间戳 -> 毫秒精度 UTC 'Z' 结尾格式；0 视为无时间。"""
    if not ms:
        return ""
    sec, msec = divmod(int(ms), 1000)
    dt = datetime.fromtimestamp(sec, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{msec:03d}Z"


def tool_path(name, inputs):
    """按工具名挑选代表路径的入参键（与 resume-agy 一致）。"""
    keys = {
        "list_dir": ("DirectoryPath",), "grep_search": ("SearchPath", "Query"),
        "view_file": ("AbsolutePath",), "write_to_file": ("TargetFile",),
        "replace_file_content": ("TargetFile",), "multi_replace_file_content": ("TargetFile",),
    }.get(name, ("TargetFile", "AbsolutePath", "DirectoryPath", "SearchPath"))
    for key in keys:
        if inputs.get(key):
            return clean_path(inputs[key])
    return ""


def git_root(value):
    """向上找 .git 目录，找不到则返回路径本身（规范化小写，与 resume-agy 一致）。"""
    candidate = os.path.abspath(str(value))
    if os.path.splitext(candidate)[1] and not os.path.isdir(candidate):
        candidate = os.path.dirname(candidate)
    current = candidate
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return norm_path(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return norm_path(candidate)


def infer_directory(msgs):
    """从工具入参的 Cwd / 绝对路径推断项目目录（与 resume-agy 一致）。"""
    cwds, paths = [], []
    for m in msgs:
        if m["kind"] != "tool_use":
            continue
        inputs = m.get("input") or {}
        cwd = clean_path(inputs.get("Cwd"))
        if cwd and os.path.isabs(cwd):
            cwds.append(cwd)
        candidate = tool_path(str(m.get("name") or ""), inputs)
        if (candidate and os.path.isabs(candidate)
                and ".gemini\\antigravity\\brain" not in candidate.lower().replace("/", "\\")):
            paths.append(candidate)
    if cwds:
        return git_root(Counter(norm_path(v) for v in cwds).most_common(1)[0][0])
    if not paths:
        return ""
    try:
        common = os.path.commonpath([norm_path(v) for v in paths])
    except ValueError:
        return ""
    return git_root(common)


def title_for(msgs, session_id):
    """标题 = 第一条用户消息的首行，截取 60 字符；无用户消息用会话 ID。"""
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user":
            first = (m["text"].strip().splitlines() or [""])[0]
            return first[:60] + ("…" if len(first) > 60 else "") or session_id
    return session_id


def normalize(events):
    """事件流 -> msgs；工具结果按源顺序与未配对调用 FIFO 配对。"""
    msgs = []
    pending = []  # 尚未等到结果的 tool_use call_id（源结果事件不携带 id）
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = ms_to_iso(timestamp_ms(ev.get("created_at")))
        source = str(ev.get("source") or "")
        event_type = str(ev.get("type") or "")
        status = str(ev.get("status") or "")
        content = clean_visible_text(value_text(ev.get("content")))
        error = value_text(ev.get("error")).strip()

        if source == "USER_EXPLICIT" and event_type == "USER_INPUT" and content:
            msgs.append(cs.m_message("user", content, ts))
        elif event_type in SUMMARY_TYPES and content:
            msgs.append(cs.m_note(f"历史摘要（{event_type}）：\n{content}", ts))
        elif source == "MODEL" and event_type == "PLANNER_RESPONSE" and content:
            msgs.append(cs.m_message("assistant", content, ts))
        elif event_type in RESULT_TYPES and content:
            call_id = pending.pop(0) if pending else ""
            msgs.append(cs.m_tool_result(call_id, content, status == "ERROR", ts))
        elif source == "MODEL" and content and event_type != "EPHEMERAL_MESSAGE":
            msgs.append(cs.m_message("assistant", content, ts))

        for index, call in enumerate(ev.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            call_id = f"{ev.get('step_index', '')}:{index}"
            msgs.append(cs.m_tool_use(
                call_id, str(call.get("name") or "tool"),
                call.get("args") if isinstance(call.get("args"), dict) else {}, ts))
            pending.append(call_id)
        if error:
            call_id = pending.pop(0) if pending else ""
            msgs.append(cs.m_tool_result(call_id, error, True, ts))
    return msgs


def session_updated_ms(msgs, mtime):
    """会话最后活动时间：末条消息的毫秒时间戳，兜底文件 mtime。"""
    for m in reversed(msgs):
        ms = timestamp_ms(m.get("ts"))
        if ms:
            return ms
    return int(mtime * 1000)


def scan_sessions(agy_dir):
    """扫描 brain/*/​.system_generated/logs/transcript.jsonl，按最后活动倒序。"""
    brain = os.path.join(agy_dir, "brain")
    if not os.path.isdir(brain):
        return []
    sessions = []
    try:
        entries = os.listdir(brain)
    except OSError:
        return []
    for name in entries:
        transcript = os.path.join(brain, name, ".system_generated", "logs",
                                  "transcript.jsonl")
        if not os.path.isfile(transcript):
            continue
        try:
            mtime = os.path.getmtime(transcript)
            msgs = normalize(parse_jsonl(transcript))
        except (OSError, cs.ConvertError):
            continue
        sessions.append({
            "session_id": name, "path": transcript,
            "cwd": infer_directory(msgs), "mtime": mtime,
            "updated": session_updated_ms(msgs, mtime),
            "title": title_for(msgs, name), "count": len(msgs),
            "last_ts": cs.fmt_cst(msgs[-1]["ts"]) if msgs else "",
        })
    sessions.sort(key=lambda s: (s["updated"], s["mtime"]), reverse=True)
    return sessions


def list_sessions(dir_=None, project=None):
    agy_dir = dir_ or default_dir()
    sessions = scan_sessions(agy_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if s["cwd"] and norm_path(s["cwd"]) == norm]
    return [{
        "session_id": s["session_id"], "title": s["title"], "cwd": s["cwd"],
        "mtime": s["mtime"], "path": s["path"], "count": s["count"],
        "last_ts": s["last_ts"],
    } for s in sessions[:50]]


def load_session(dir_=None, project=None, session_id=None):
    agy_dir = dir_ or default_dir()
    sessions = scan_sessions(agy_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if s["cwd"] and norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Antigravity 会话（dir={agy_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    msgs = normalize(parse_jsonl(target["path"]))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    sid = target["session_id"]
    started = next((m["ts"] for m in msgs if m.get("ts")), "")
    ended = next((m["ts"] for m in reversed(msgs) if m.get("ts")), "")
    ref = cs.make_ref("agy", sid, title_for(msgs, sid), target["cwd"],
                      started_at=started, ended_at=ended, source=target["path"])
    return ref, msgs
