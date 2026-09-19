#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 MiniMax Code（mcode）本地会话记录，生成接管摘要。

数据源（按优先级）：
  1. SQLite 会话库 <主目录>/v2/sqlite/runtime-state.sqlite（local_runtime_sessions /
     local_runtime_message_rows / local_runtime_messages）；
  2. 规范历史 JSONL <主目录>/v2/sessions/<日期>/<时间>-session_<id>/messages.jsonl
     （信封格式：message_id / turn_id / message{role,content,timestamp} / turn_config）；
  3. 仅有 JSONL 时（库缺失或不可读），扫描 manifest.json 列出会话。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
READ_TOOLS = {"read", "grep", "glob", "find", "ls", "list", "web_search", "tool_search", "webfetch"}
EDIT_TOOLS = {"write", "edit", "apply_patch", "applypatch", "multiedit", "notebookedit"}
SHELL_TOOLS = {"bash", "shell", "shell_command", "terminal", "run_command"}
TODO_STATUS_LABELS = {"completed": "已完成", "in_progress": "进行中", "pending": "待办"}
SKIP_KINDS = {"peek", "channel"}
TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|"
    r"cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b",
    re.IGNORECASE,
)
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|"
    r"\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE,
)


def setup_utf8_stdio():
    # newline="\n" 保证 Windows 下与 Node 版输出逐字节一致
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stderr.reconfigure(encoding="utf-8", newline="\n")


def default_minimax_dir():
    for env_name in ("MINIMAX_DATA_DIR", "MAVIS_DATA_DIR"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return Path(value.strip()).expanduser()
    home = Path.home()
    primary = home / ".minimax"
    if primary.exists():
        return primary
    legacy = home / ".mavis"
    if legacy.exists():
        return legacy
    return primary


def db_path(data_dir):
    return data_dir / "v2" / "sqlite" / "runtime-state.sqlite"


def history_root(data_dir):
    return data_dir / "v2" / "sessions"


def norm_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def parse_json(value, fallback=None):
    if fallback is None:
        fallback = {}
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def parse_json_string(value):
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    if isinstance(parsed, str):
        return parsed
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def timestamp_ms(value):
    if value is None or value == "":
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0
    if not number == number:  # NaN
        return 0
    return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)


def fmt_time(value):
    ms = timestamp_ms(value)
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def open_db(db_file):
    uri = db_file.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sessions_dir_candidates(data_dir):
    root = history_root(data_dir)
    if not root.is_dir():
        return []
    dirs = []
    for year in sorted(root.iterdir()):
        if not year.is_dir():
            continue
        for month in sorted(year.iterdir()):
            if not month.is_dir():
                continue
            for day in sorted(month.iterdir()):
                if not day.is_dir():
                    continue
                for time_dir in sorted(day.iterdir()):
                    if time_dir.is_dir():
                        dirs.append(time_dir)
    return dirs


def manifest_of(session_dir):
    file = session_dir / "manifest.json"
    if not file.is_file():
        return None
    value = parse_json(file.read_text(encoding="utf-8"), None)
    if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str) or not value.get("sessionId"):
        return None
    return value


def first_user_text(messages_file):
    """从规范历史 JSONL 提取首条真实用户消息文本（用于兜底标题）。"""
    text = ""
    try:
        for line in messages_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            envelope = parse_json(line, None)
            message = envelope.get("message") if isinstance(envelope, dict) else None
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if isinstance(message.get("archonCompaction"), dict):
                continue
            text = user_text_of(message)
            if text.strip():
                break
    except OSError:
        pass
    return text


def user_text_of(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def scan_sessions_files(data_dir):
    metas = []
    for session_dir in sessions_dir_candidates(data_dir):
        manifest = manifest_of(session_dir)
        if not manifest:
            continue
        messages_file = session_dir / "messages.jsonl"
        updated = timestamp_ms(manifest.get("updatedAtMs"))
        if not updated:
            try:
                updated = messages_file.stat().st_mtime * 1000
            except OSError:
                updated = 0
        parent_id = manifest.get("parentSessionId")
        metas.append({
            "session_id": manifest["sessionId"],
            "title": "",
            "directory": "",
            "project_dir": "",
            "agent": "",
            "model": "",
            "kind": "",
            "status": "",
            "archived": False,
            "parent_id": parent_id if isinstance(parent_id, str) else None,
            "created": timestamp_ms(manifest.get("createdAtMs")),
            "updated": updated,
            "source": "files",
            "history_dir": str(session_dir),
        })
    metas.sort(key=lambda meta: meta["updated"] or 0, reverse=True)
    return metas


def scan_sessions_db(data_dir):
    file = db_path(data_dir)
    if not file.is_file():
        return None
    try:
        with open_db(file) as conn:
            rows = conn.execute("SELECT * FROM local_runtime_sessions").fetchall()
    except sqlite3.Error:
        return None  # WAL 遗留等只读打开失败时退回文件扫描
    metas = []
    for row in rows:
        if row["visibility"] == "hidden":
            continue
        if row["session_kind"] in SKIP_KINDS:
            continue
        record = parse_json(row["record_json"], {})
        history_rel = row["history_relative_dir"]
        history_dir = str(history_root(data_dir) / history_rel) if history_rel else ""
        metas.append({
            "session_id": row["session_id"],
            "title": row["title"] or "",
            "directory": row["workspace_dir"] or row["project_workspace_dir"] or record.get("workspaceDir") or "",
            "project_dir": row["project_workspace_dir"] or "",
            "agent": row["agent_name"] or record.get("agentName") or "",
            "model": record.get("effectiveModel") or "",
            "kind": row["session_kind"] or record.get("sessionKind") or "",
            "status": row["status"] or record.get("status") or "",
            "archived": bool(row["archived"]),
            "parent_id": row["parent_session_id"] or record.get("parentSessionId"),
            "created": timestamp_ms(row["created_at_ms"] if row["created_at_ms"] is not None else record.get("createdAtMs")),
            "updated": timestamp_ms(row["updated_at_ms"] if row["updated_at_ms"] is not None else record.get("updatedAtMs")),
            "source": "sqlite",
            "history_dir": history_dir,
        })
    metas.sort(key=lambda meta: (1 if meta["archived"] else 0, -(meta["updated"] or 0)))
    return metas


def scan_sessions(data_dir):
    from_db = scan_sessions_db(data_dir)
    if from_db is not None:
        return from_db, True
    return scan_sessions_files(data_dir), False


def find_session_dir(data_dir, session_id):
    for session_dir in sessions_dir_candidates(data_dir):
        manifest = manifest_of(session_dir)
        if manifest and manifest["sessionId"] == session_id:
            return session_dir
    return None


def normalize_envelopes(envelopes):
    """信封消息 → 统一条目（user_text / assistant_text / tool_use / tool_result / compaction / attachment）。"""
    items = []
    todos = []
    model = ""
    compaction_count = 0
    latest_compaction = ""
    for envelope in envelopes:
        message = envelope.get("message") or {}
        ts = timestamp_ms(message.get("timestamp"))
        role = message.get("role") or ""
        if role == "user":
            marker = message.get("archonCompaction")
            if isinstance(marker, dict):
                compaction_count += 1
                if isinstance(marker.get("summary"), str):
                    latest_compaction = marker["summary"]
                items.append({"kind": "compaction", "timestamp": ts, "text": marker.get("summary") or "(压缩边界)"})
                if isinstance(marker.get("todoState"), list):
                    todos = []
                    for todo in marker["todoState"]:
                        if isinstance(todo, dict) and isinstance(todo.get("content"), str):
                            todos.append({"content": todo["content"], "status": str(todo.get("status") or ""), "priority": todo.get("priority")})
                continue
            content = message.get("content")
            if isinstance(content, list):
                index = 0
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        index += 1
                        items.append({"kind": "attachment", "timestamp": ts, "text": f"[图片 #{index}]"})
            text = user_text_of(message)
            if text.strip():
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
        elif role == "assistant":
            if isinstance(message.get("model"), str) and message.get("model"):
                model = message["model"]
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                        items.append({"kind": "assistant_text", "timestamp": ts, "text": block["text"]})
                    elif block.get("type") == "toolCall" and block.get("id"):
                        items.append({
                            "kind": "tool_use",
                            "timestamp": ts,
                            "name": block.get("name") or "tool",
                            "input": parse_json(block.get("arguments"), {}),
                            "tool_use_id": block["id"],
                        })
        elif role == "toolResult":
            content = message.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                )
            elif isinstance(content, str):
                text = content
            else:
                text = ""
            items.append({
                "kind": "tool_result",
                "timestamp": ts,
                "tool_use_id": message.get("toolCallId") or "",
                "content": text,
                "is_error": bool(message.get("isError")),
            })
        elif role == "compactionSummary":
            compaction_count += 1
            if isinstance(message.get("summary"), str):
                latest_compaction = message["summary"]
            items.append({"kind": "compaction", "timestamp": ts, "text": message.get("summary") or "(压缩摘要)"})
    items.sort(key=lambda item: item["timestamp"] or 0)
    return {"items": items, "model": model, "todos": todos, "compaction_count": compaction_count, "latest_compaction": latest_compaction}


def normalize_rows(rows):
    """会话库 message 行（display message）→ 统一条目。"""
    items = []
    compaction_count = 0
    latest_compaction = ""
    for row in rows:
        data = parse_json(row["data_json"], {})
        role = data.get("role") or row["role"] or ""
        ts = timestamp_ms(data.get("timestamp") if data.get("timestamp") is not None else data.get("created_at") if data.get("created_at") is not None else row["created_at_ms"])
        if role == "user":
            text = data.get("msg_content") if isinstance(data.get("msg_content"), str) else ""
            if text.strip():
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
            if isinstance(data.get("attachments"), list):
                index = 0
                for attachment in data["attachments"]:
                    index += 1
                    meta = attachment.get("meta") if isinstance(attachment, dict) else None
                    name = (meta or {}).get("fileName") if isinstance(meta, dict) else None
                    name = name or (attachment.get("fileName") if isinstance(attachment, dict) else None)
                    name = name or (attachment.get("filePath") if isinstance(attachment, dict) else None) or f"#{index}"
                    items.append({"kind": "attachment", "timestamp": ts, "text": f"[附件] {name}"})
        elif role == "assistant":
            text = data.get("msg_content") if isinstance(data.get("msg_content"), str) else ""
            if text.strip():
                items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
            if isinstance(data.get("tool_calls"), list):
                for call in data["tool_calls"]:
                    if not isinstance(call, dict) or not isinstance(call.get("tool_call_id"), str):
                        continue
                    items.append({
                        "kind": "tool_use",
                        "timestamp": ts,
                        "name": call.get("tool_name") or "tool",
                        "input": parse_json(call.get("tool_call_args"), {}),
                        "tool_use_id": call["tool_call_id"],
                    })
                    status = call.get("tool_call_status")
                    if status in (2, 3):
                        items.append({
                            "kind": "tool_result",
                            "timestamp": ts,
                            "tool_use_id": call["tool_call_id"],
                            "content": parse_json_string(call.get("tool_call_result_data")),
                            "is_error": status == 3,
                        })
        elif data.get("kind") == "compaction" and isinstance(data.get("msg_content"), str) and data["msg_content"].strip():
            compaction_count += 1
            latest_compaction = data["msg_content"]
            items.append({"kind": "compaction", "timestamp": ts, "text": data["msg_content"]})
    items.sort(key=lambda item: item["timestamp"] or 0)
    return {"items": items, "model": "", "todos": [], "compaction_count": compaction_count, "latest_compaction": latest_compaction}


def read_envelopes(messages_file):
    envelopes = []
    for line in messages_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = parse_json(line, None)
        if isinstance(envelope, dict) and isinstance(envelope.get("message"), dict):
            envelopes.append(envelope)
    return envelopes


def load_messages_jsonl(data_dir, meta):
    file = (Path(meta["history_dir"]) / "messages.jsonl") if meta["history_dir"] else None
    if file is None or not file.is_file():
        session_dir = find_session_dir(data_dir, meta["session_id"])
        file = session_dir / "messages.jsonl" if session_dir else None
    if file is None or not file.is_file():
        return None
    return normalize_envelopes(read_envelopes(file))


def load_messages_rows(data_dir, session_id):
    file = db_path(data_dir)
    if not file.is_file():
        return None
    try:
        with open_db(file) as conn:
            rows = conn.execute(
                "SELECT msg_id, role, created_at_ms, data_json FROM local_runtime_message_rows WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            normalized = normalize_rows(rows)
            if normalized["items"]:
                return normalized
            blob = conn.execute(
                "SELECT display_messages_json FROM local_runtime_messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if blob and blob["display_messages_json"]:
            display = parse_json(blob["display_messages_json"], [])
            if isinstance(display, list):
                rows_like = [
                    {
                        "msg_id": message.get("msg_id") or str(index),
                        "role": message.get("role"),
                        "created_at_ms": timestamp_ms(message.get("timestamp") if message.get("timestamp") is not None else message.get("created_at")),
                        "data_json": json.dumps(message, ensure_ascii=False),
                    }
                    for index, message in enumerate(display)
                    if isinstance(message, dict)
                ]
                return normalize_rows(rows_like)
    except sqlite3.Error:
        return None
    return normalized


def load_session(data_dir, meta):
    normalized = load_messages_jsonl(data_dir, meta)
    if normalized is None and meta["source"] == "sqlite":
        normalized = load_messages_rows(data_dir, meta["session_id"])
    if normalized is None:
        normalized = {"items": [], "model": "", "todos": [], "compaction_count": 0, "latest_compaction": ""}
    if not meta["model"] and normalized["model"]:
        meta["model"] = normalized["model"]
    if not meta["title"]:
        first_user = next((item for item in normalized["items"] if item["kind"] == "user_text"), None)
        meta["title"] = first_user["text"] if first_user else (normalized["latest_compaction"] or meta["session_id"])
    meta["title"] = meta["title"].splitlines()[0].strip() if meta["title"].splitlines() else ""
    meta["title"] = meta["title"] or meta["session_id"]
    timestamps = [item["timestamp"] for item in normalized["items"] if item["timestamp"]]
    return {
        "info": {
            "session_id": meta["session_id"],
            "title": meta["title"],
            "directory": meta["directory"],
            "agent": meta["agent"],
            "model": meta["model"],
            "kind": meta["kind"],
            "status": meta["status"],
            "archived": meta["archived"],
            "parent_id": meta["parent_id"],
            "source": meta["source"],
            "first_ts": fmt_time(timestamps[0] if timestamps else meta["created"]),
            "last_ts": fmt_time(timestamps[-1] if timestamps else meta["updated"]),
        },
        "normalized": normalized,
    }


def tool_file(name, input_data):
    for key in ("filePath", "file_path", "path", "filename"):
        if input_data.get(key):
            return str(input_data[key])
    if name in ("glob", "grep", "find"):
        return str(input_data.get("pattern") or input_data.get("query") or input_data.get("regex") or "")
    return ""


def shell_command(input_data):
    return str(input_data.get("command") or input_data.get("cmd") or "").strip()


def dedupe(values):
    seen = set()
    output = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def build_state(items, todos):
    files_read, files_edited, commands, test_results = [], [], [], []
    calls = {}
    first_user = last_user = last_assistant = ""
    for item in items:
        if item["kind"] == "user_text":
            first_user = first_user or item["text"]
            last_user = item["text"]
        elif item["kind"] == "assistant_text":
            last_assistant = item["text"]
        elif item["kind"] == "tool_use":
            name = str(item.get("name") or "").lower()
            input_data = item.get("input") or {}
            calls[item.get("tool_use_id") or f"#{len(calls)}"] = (name, input_data)
            if name in READ_TOOLS:
                files_read.append(tool_file(name, input_data))
            elif name in EDIT_TOOLS:
                files_edited.append(tool_file(name, input_data))
            elif name in SHELL_TOOLS:
                commands.append(shell_command(input_data))
        elif item["kind"] == "tool_result":
            name, input_data = calls.get(item.get("tool_use_id"), ("", {}))
            content = item.get("content") or ""
            command = shell_command(input_data) if name in SHELL_TOOLS else ""
            if content.strip() and (item.get("is_error") or TEST_CMD_RE.search(command) or TEST_RESULT_RE.search(content[:2000])):
                test_results.append({"command_hint": command or name, "is_error": bool(item.get("is_error")), "content": content})
    return {
        "goal": first_user,
        "files_read": dedupe(files_read),
        "files_edited": dedupe(files_edited),
        "commands": dedupe(commands),
        "test_results": test_results,
        "last_user": last_user,
        "last_assistant": last_assistant,
        "todos": todos,
    }


def truncate(value, limit):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def text_block(value, limit):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def tool_brief(item):
    name = item.get("name") or "tool"
    input_data = item.get("input") or {}
    detail = shell_command(input_data) or tool_file(str(name).lower(), input_data) or str(input_data.get("url") or input_data.get("query") or "")
    return f"{name}({truncate(detail, 100)})" if detail else f"{name}(...)"


def render_item(item, max_chars):
    ts = fmt_time(item["timestamp"])
    if item["kind"] == "user_text":
        return [f"### [用户] {ts}", text_block(item["text"], max_chars)]
    if item["kind"] == "assistant_text":
        return [f"### [助手] {ts}", text_block(item["text"], max_chars)]
    if item["kind"] == "tool_use":
        return [f"### [工具调用] {item.get('name') or 'tool'} {ts}", "```json", truncate(json.dumps(item.get("input") or {}, ensure_ascii=False, separators=(",", ":")), max_chars), "```"]
    if item["kind"] == "tool_result":
        return [f"### [工具结果]{' (错误)' if item.get('is_error') else ''} {ts}", text_block(item["content"], max_chars)]
    if item["kind"] == "compaction":
        return [f"### [压缩摘要] {ts}", text_block(item["text"], max_chars)]
    if item["kind"] == "attachment":
        return [f"### [附件] {ts}", item.get("text") or ""]
    return []


def todo_label(status):
    return TODO_STATUS_LABELS.get(status) or status or "未知"


def render_summary(session, state, recent_n, max_chars):
    info = session["info"]
    norm = session["normalized"]
    lines = [
        "# Resume-MiniMax 会话接管摘要", "", "## 会话信息",
        f"- 标题: {info['title']}", f"- 会话ID: {info['session_id']}",
        f"- 项目: {info['directory'] or '(未知)'}", f"- 存储: {info['source']}",
    ]
    if info["agent"]:
        lines.append(f"- Agent: {info['agent']}")
    if info["model"]:
        lines.append(f"- 模型: {info['model']}")
    if info["kind"]:
        lines.append(f"- 会话类型: {info['kind']}")
    if info["status"]:
        lines.append(f"- 状态: {info['status']}")
    if info["parent_id"]:
        lines.append(f"- 父会话: {info['parent_id']}")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(norm['items'])}")
    lines.append("")
    if norm["compaction_count"] > 0:
        lines.append(f"## 历史摘要（会话内共 {norm['compaction_count']} 次压缩，最近一次内容）")
        lines.append(text_block(norm["latest_compaction"], max_chars) or "(无)")
        lines.append("")
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    if state["todos"]:
        lines.append(f"### 任务清单（来自压缩快照，共 {len(state['todos'])} 项）")
        for todo in state["todos"]:
            lines.append(f"- [{todo_label(todo['status'])}] {truncate(todo['content'], 200)}")
        lines.append("")
    for title, key in (("已调查文件", "files_read"), ("代码修改", "files_edited"), ("执行命令", "commands")):
        if state[key]:
            lines.append(f"### {title}")
            for value in state[key]:
                lines.append(f"- {truncate(value, 200)}")
            lines.append("")
    if state["test_results"]:
        lines.append("### 测试 / 错误结果")
        for result in state["test_results"][-5:]:
            first = result["content"].strip().splitlines()[0] if result["content"].strip() else ""
            lines.append(f"-{' [错误]' if result['is_error'] else ''} {truncate(first, 200)}")
        lines.append("")
    lines.extend(["### 最近用户消息", text_block(state["last_user"], max_chars) or "(无)", "", "### 最近助手消息", text_block(state["last_assistant"], max_chars) or "(无)", ""])
    recent = norm["items"][-recent_n:] if recent_n else []
    lines.extend([f"## 近期对话（最近 {len(recent)} 条）", ""])
    for item in recent:
        lines.extend(render_item(item, max_chars))
        lines.append("")
    older_tools = [item for item in norm["items"][:-recent_n] if item["kind"] == "tool_use"] if recent_n else []
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 60 条）")
        for item in older_tools[-60:]:
            lines.append(f"- [{fmt_time(item['timestamp'])}] {tool_brief(item)}")
        lines.append("")
    lines.extend(["## 接管建议", "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。", "- 优先核对「任务清单」中未完成项，再以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。", "- 也可以直接用 `mcode --resume <会话ID>`（或当前目录下 `mcode -c`）让 MiniMax Code 原生续接本会话。", "- 不要逐字复述历史；基于现状决定下一步动作。", ""])
    return "\n".join(lines)


def project_sessions(sessions, project_path):
    target = norm_path(project_path)
    return [item for item in sessions if norm_path(item["directory"]) == target]


def pick_session(sessions, session_arg, project_path):
    if session_arg:
        return next((item for item in sessions if item["session_id"].startswith(session_arg) or session_arg in item["session_id"]), None)
    return project_sessions(sessions, project_path)[0] if project_sessions(sessions, project_path) else None


def print_list(project_path, limit, selected=None):
    if selected is None:
        selected = []
    shown = selected[:limit] if limit > 0 else selected
    print(f"当前项目: {project_path}")
    print(f"找到 {len(selected)} 个会话{f'（仅显示最近 {len(shown)} 个）' if len(shown) < len(selected) else ''}：\n")
    for index, meta in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        print(f"{mark} {fmt_time(meta['updated']) or '(无时间)'}  {meta['session_id'][:12]}  标题: {meta['title'] or '(未命名)'}")


def print_help(parser):
    parser.print_help()


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="读取 MiniMax Code（mcode）本地会话，生成结构化接管摘要。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    parser.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", metavar="ID", help="指定会话 ID 或前缀；跨项目查找")
    parser.add_argument("--project", metavar="PATH", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--minimax-dir", metavar="DIR", help="MiniMax Code 主目录（默认 ~/.minimax，兼容 ~/.mavis）")
    parser.add_argument("--recent", type=int, default=8, metavar="N", help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, metavar="N", help="单条截断长度，默认 1500")
    parser.add_argument("--limit", type=int, default=0, metavar="N", help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", metavar="FILE", help="将摘要写入文件")
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助")
    args = parser.parse_args(argv)
    if args.help:
        print_help(parser)
        sys.exit(0)
    return {
        "list": args.list,
        "latest": args.latest,
        "session": args.session,
        "project": args.project,
        "minimax_dir": args.minimax_dir,
        "recent": args.recent,
        "max_chars": args.max_chars,
        "limit": args.limit,
        "json": args.json,
        "output": args.output,
    }


def main():
    setup_utf8_stdio()
    args = parse_args(sys.argv[1:])
    data_dir = Path(args["minimax_dir"] or default_minimax_dir()).resolve()
    sessions, used_db = scan_sessions(data_dir)
    if not sessions:
        print(f"错误：在 {data_dir} 未找到 MiniMax Code 会话（已检查 v2/sqlite/runtime-state.sqlite 与 v2/sessions/）。", file=sys.stderr)
        print("若安装在自定义目录，请用 --minimax-dir 指定，或设置 MINIMAX_DATA_DIR 环境变量。", file=sys.stderr)
        sys.exit(1)
    if not used_db:
        print("提示：SQLite 会话库不可用，仅从会话 JSONL 读取（标题与项目路径信息可能不完整）。", file=sys.stderr)
    project_path = str(Path(args["project"]).resolve())
    can_filter_project = used_db
    if args["list"]:
        selected = project_sessions(sessions, project_path)
        if not selected and not can_filter_project:
            print("提示：文件模式下无法判断会话所属项目，显示全部会话。", file=sys.stderr)
            selected = sessions
        if not selected:
            print(f"错误：未找到项目 {project_path} 的 MiniMax Code 会话。可用 --session ID 跨项目查找。", file=sys.stderr)
            sys.exit(1)
        for meta in selected:
            if meta["title"]:
                continue
            normalized = load_messages_jsonl(data_dir, meta)
            if normalized is None and meta["source"] == "sqlite":
                normalized = load_messages_rows(data_dir, meta["session_id"])
            items = (normalized or {}).get("items", [])
            first_user = next((item for item in items if item["kind"] == "user_text"), None)
            if first_user:
                meta["title"] = first_user["text"].splitlines()[0].strip()
            else:
                meta["title"] = ((normalized or {}).get("latest_compaction") or "").splitlines()[0].strip() if (normalized or {}).get("latest_compaction") else ""
        print_list(project_path, args["limit"], selected)
        return
    target = pick_session(sessions, args["session"], project_path)
    if target is None and not can_filter_project and not args["session"]:
        target = sessions[0]
    if not target:
        print(f"错误：未匹配到会话 '{args['session'] or '当前项目'}'。", file=sys.stderr)
        sys.exit(1)
    try:
        session = load_session(data_dir, target)
    except (OSError, sqlite3.Error) as error:
        print(f"错误：解析会话失败：{error}", file=sys.stderr)
        sys.exit(1)
    state = build_state(session["normalized"]["items"], session["normalized"]["todos"])
    recent_count = max(args["recent"], 0)
    recent_items = session["normalized"]["items"][-recent_count:] if recent_count else []
    if args["json"]:
        output = json.dumps({"info": session["info"], "state": state, "recent_items": recent_items}, ensure_ascii=False, indent=2)
    else:
        output = render_summary(session, state, recent_count, max(args["max_chars"], 1))
    if args["output"]:
        Path(args["output"]).write_text(output, encoding="utf-8", newline="\n")
        print(f"摘要已写入：{args['output']}", file=sys.stderr)
    else:
        sys.stdout.write(output + ("" if output.endswith("\n") else "\n"))


if __name__ == "__main__":
    main()
