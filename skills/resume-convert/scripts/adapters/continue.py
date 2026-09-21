#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/continue: Continue（VS Code/JetBrains 扩展与 cn CLI 共用）-> 归一化会话。

格式依据（与 resume-continue 一致）：
  ~/.continue/sessions/<会话>.json（sessions.json 为索引文件，扫描时跳过；
  目录受 CONTINUE_DATA_DIR / CONTINUE_GLOBAL_DIR 环境变量影响）
  单文件 JSON：{sessionId|session_id, title, workspaceDirectory|workspace,
  chatModelTitle, usage, history}
  history 条目：{message: {role, content, toolCalls?}, contextItems,
  toolCallStates, conversationSummary?}
  - 用户/助手文本：message.content（字符串，或 text/imageUrl 片段列表）
  - 工具调用：toolCallStates（新，toolCall.function + output）与
    message.toolCalls（旧，仅调用无结果）；结果来自 state.output 或独立
    role:"tool" 消息（后者已给出时跳过 state.output，避免重复）
  - 推理/思考（thinking/system 角色）不可迁移，跳过
  - conversationSummary（原会话 compact）-> 转换注记
  - contextItems 附件折叠进对应用户消息文本（[附件] 前缀）
标题：title 为空或占位（New Session 等）时回退首条用户消息首行。
时间：条目本身无时间戳，会话级时间取会话文件的创建/修改时间。
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

# 占位标题（与 resume-continue 的 UNTITLED_TITLES 一致，比较时不区分大小写）
UNTITLED_TITLES = {"", "untitled session", "new session", "新会话", "未命名会话"}
TOOL_STATUS_ERROR = {"errored", "error", "failed"}


def default_dir():
    """~/.continue/sessions；受 CONTINUE_DATA_DIR（直接指定 sessions 目录）
    与 CONTINUE_GLOBAL_DIR（主目录）影响，与 resume-continue 一致。"""
    override = os.environ.get("CONTINUE_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get("CONTINUE_GLOBAL_DIR") or os.path.join(
        os.path.expanduser("~"), ".continue")
    return os.path.join(os.path.abspath(os.path.expanduser(base)), "sessions")


def resolve_sessions_dir(configured):
    """dir_ 兼容多种形态：sessions 目录本身、Continue 主目录（含 sessions/）、空（默认）。"""
    if not configured:
        return default_dir()
    resolved = os.path.abspath(os.path.expanduser(str(configured)))
    if os.path.basename(resolved) == "sessions" and os.path.isdir(resolved):
        return resolved
    nested = os.path.join(resolved, "sessions")
    return nested if os.path.isdir(nested) else resolved


def norm_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def file_times(path):
    """会话文件（创建时间, 修改时间），epoch 毫秒。"""
    stat = os.stat(path)
    created = int(getattr(stat, "st_birthtime", 0) or 0) * 1000 or int(stat.st_ctime * 1000)
    return created, int(stat.st_mtime * 1000)


def ms_to_iso(ms):
    """epoch 毫秒 -> UTC ISO（毫秒精度、Z 结尾）；0 返回空。"""
    ms = int(ms or 0)
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def read_session(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def parse_json(value):
    """宽松 JSON 解析：对象/数组原样返回，空值与坏串返回 {}。"""
    if isinstance(value, (dict, list)):
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def first_defined(*values):
    for value in values:
        if value is not None:
            return value
    return None


def message_content_text(content):
    """message.content -> 纯文本：text 片段拼接，imageUrl 记为 [图片] 占位。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "imageUrl":
                parts.append("[图片]")
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(part for part in parts if part)


def attachment_lines(context_items):
    """contextItems -> [附件] 标签行（名称 + uri.value）。"""
    lines = []
    for context in context_items or []:
        if not isinstance(context, dict):
            continue
        label = str(context.get("name") or "").strip()
        uri = context.get("uri") if isinstance(context.get("uri"), dict) else {}
        value = str(uri.get("value") or "").strip()
        if value and value != label:
            label = f"{label} ({value})" if label else value
        if label:
            lines.append(f"[附件] {label}")
    return lines


def context_items_text(output):
    """toolCallStates[].output（contextItems 形态）-> 纯文本。"""
    if output is None:
        return ""
    if isinstance(output, list):
        parts = []
        for entry in output:
            if not isinstance(entry, dict):
                parts.append("" if entry is None else str(entry))
            elif entry.get("content"):
                parts.append(str(entry["content"]))
            elif entry.get("description"):
                parts.append(str(entry["description"]))
        return "\n".join(part for part in parts if part)
    if isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return str(output)


def tool_input(value):
    """parsedArgs / processedArgs / arguments 统一为 dict；非 dict 以 {"raw": ...} 包装。"""
    if isinstance(value, dict):
        return value
    return {"raw": value} if value else {}


def tool_status_is_error(status):
    return str(status or "").lower() in TOOL_STATUS_ERROR


def first_user_text(history):
    """首条用户消息的首行，作为无标题会话的展示标题。"""
    for entry in history:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if message.get("role") != "user":
            continue
        text = message_content_text(message.get("content")).strip()
        if not text:
            continue
        return text.splitlines()[0][:120]
    return ""


def resolve_title(meta):
    title = (meta.get("title") or "").strip()
    if title.lower() not in UNTITLED_TITLES:
        return title
    return meta.get("first_user") or meta["session_id"]


def normalize_history(history):
    """history 条目 -> 归一化消息列表（分支与 resume-continue 的 normalize_history 一致）。"""
    msgs = []
    state_outputs = set()  # 已由 role:"tool" 消息给出结果，避免与 toolCallStates.output 重复
    for entry in history:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        role = message.get("role") or ""
        summary = entry.get("conversationSummary")
        if isinstance(summary, str) and summary.strip():
            msgs.append(cs.m_note(f"原会话压缩摘要：{summary.strip()}", ""))
        if role == "user":
            parts = [message_content_text(message.get("content")).strip()]
            parts.extend(attachment_lines(entry.get("contextItems")))
            text = "\n\n".join(part for part in parts if part)
            if text:
                msgs.append(cs.m_message("user", text, ""))
        elif role == "assistant":
            text = message_content_text(message.get("content")).strip()
            if text:
                msgs.append(cs.m_message("assistant", text, ""))
            states = entry.get("toolCallStates") if isinstance(entry.get("toolCallStates"), list) else []
            state_ids = set()
            for state in states:
                if not isinstance(state, dict):
                    continue
                call = state.get("toolCall") if isinstance(state.get("toolCall"), dict) else {}
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or state.get("toolCallId") or "tool")
                call_id = str(call.get("id") or state.get("toolCallId") or "")
                state_ids.add(call_id)
                input_obj = tool_input(first_defined(
                    state.get("parsedArgs"),
                    state.get("processedArgs"),
                    parse_json(function.get("arguments")),
                ))
                msgs.append(cs.m_tool_use(call_id, name, input_obj, ""))
                status = state.get("status")
                if call_id not in state_outputs and (
                        state.get("output") is not None or tool_status_is_error(status)):
                    msgs.append(cs.m_tool_result(
                        call_id, context_items_text(state.get("output")),
                        tool_status_is_error(status), ""))
            # 旧版本仅在 message.toolCalls 上记录调用（无状态对象）
            for call in message.get("toolCalls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_id = str(call.get("id") or "")
                if call_id and call_id in state_ids:
                    continue
                msgs.append(cs.m_tool_use(
                    call_id, str(function.get("name") or "tool"),
                    tool_input(parse_json(function.get("arguments"))), ""))
        elif role == "tool":
            call_id = str(message.get("toolCallId") or message.get("tool_call_id") or "")
            if call_id:
                state_outputs.add(call_id)
            msgs.append(cs.m_tool_result(
                call_id, message_content_text(message.get("content")), False, ""))
        # thinking / system 角色不迁移，跳过
    return msgs


def scan_sessions(sessions_dir):
    """扫描 sessions 目录（跳过 sessions.json 索引），按修改时间倒序。"""
    sessions = []
    entries = []
    try:
        for name in os.listdir(sessions_dir):
            if not name.endswith(".json") or name == "sessions.json":
                continue
            full = os.path.join(sessions_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                entries.append((os.path.getmtime(full), full))
            except OSError:
                continue
    except OSError:
        return sessions
    entries.sort(key=lambda item: item[0])
    for _, full in entries:
        data = read_session(full)
        if not isinstance(data, dict) or not isinstance(data.get("history"), list):
            continue
        try:
            created, updated = file_times(full)
        except OSError:
            created = updated = 0
        sessions.append({
            "session_id": str(data.get("sessionId") or data.get("session_id")
                              or os.path.splitext(os.path.basename(full))[0]),
            "title": str(data.get("title") or "").strip(),
            "first_user": first_user_text(data["history"]),
            "directory": str(data.get("workspaceDirectory") or data.get("workspace") or "").strip(),
            "chat_model_title": str(data.get("chatModelTitle") or "").strip(),
            "created": created,
            "updated": updated,
            "path": full,
            "_history": data["history"],
        })
    sessions.sort(key=lambda meta: -(meta["updated"] or meta["created"]))
    return sessions


def list_sessions(dir_=None, project=None):
    sessions_dir = resolve_sessions_dir(dir_)
    sessions = scan_sessions(sessions_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    out = []
    for s in sessions[:50]:
        msgs = normalize_history(s["_history"])
        # Continue 条目本身无时间戳，last_ts 以会话文件修改时间为准
        out.append({
            "session_id": s["session_id"],
            "title": resolve_title(s),
            "cwd": s["directory"],
            "mtime": s["updated"],
            "path": s["path"],
            "count": len(msgs),
            "last_ts": cs.fmt_cst(ms_to_iso(s["updated"])),
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    sessions_dir = resolve_sessions_dir(dir_)
    sessions = scan_sessions(sessions_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Continue 会话（dir={sessions_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    msgs = normalize_history(target["_history"])
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    ref = cs.make_ref(
        "continue", target["session_id"], resolve_title(target), target["directory"],
        model=target["chat_model_title"],
        started_at=ms_to_iso(target["created"]),
        ended_at=ms_to_iso(target["updated"]),
        source=target["path"])
    return ref, msgs
