#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 Kilo Code 本地会话（kilo.db SQLite 或旧版 VS Code 扩展任务目录），生成接管摘要。"""

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
READ_TOOLS = {"read", "glob", "grep", "list", "codesearch"}
EDIT_TOOLS = {"write", "edit", "patch", "apply_patch", "multiedit"}
SHELL_TOOLS = {"bash", "shell", "shell_command", "terminal", "execute_command"}
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
# Kilo/Cline 旧版任务在用户消息外包裹的环境与系统块，不属于任务内容本身
LEGACY_BLOCK_RE = re.compile(
    r"<(environment_details|system-reminder|fetch_instructions|notice|custom_instructions|rules)[\s\S]*?</\1>"
)
LEGACY_TASK_RE = re.compile(r"<task>([\s\S]*?)</task>")
LEGACY_TASK_RE_G = re.compile(r"<task>([\s\S]*?)</task>")


def setup_utf8_stdio():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="")
        sys.stderr.reconfigure(encoding="utf-8", newline="")


def default_data_dir_candidates():
    candidates = []
    override = os.environ.get("KILO_DATA_DIR")
    if override:
        candidates.append(Path(override).expanduser())
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidates.append(Path(xdg).expanduser() / "kilo")
    candidates.append(Path.home() / ".local" / "share" / "kilo")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "kilo")
    return candidates


def editor_global_storage_roots():
    editors = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Trae"]
    roots = []
    if platform.system() == "Darwin":
        for editor in editors:
            roots.append(Path.home() / "Library" / "Application Support" / editor / "User" / "globalStorage")
    elif platform.system() == "Windows":
        app_data = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        for editor in editors:
            roots.append(Path(app_data) / editor / "User" / "globalStorage")
    else:
        for editor in editors:
            roots.append(Path.home() / ".config" / editor / "User" / "globalStorage")
    return roots


def default_tasks_roots():
    roots = []
    override = os.environ.get("KILO_TASKS_DIR")
    if override:
        roots.append(Path(override).expanduser())
    for root in editor_global_storage_roots():
        roots.append(root / "kilocode.kilo-code" / "tasks")
    return roots


def norm_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def parse_json(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    if default is None and isinstance(value, str) and not value.strip():
        return {}
    if value is None or value == "":
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def timestamp_ms(value):
    if value is None or value == "":
        return 0
    try:
        number = float(value)
        return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0


def fmt_time(value):
    ms = timestamp_ms(value)
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def open_db(db_path):
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def find_databases(data_dir):
    override = os.environ.get("KILO_DB")
    if override:
        configured = Path(override)
        if not configured.is_absolute():
            configured = data_dir / configured
        return [configured] if configured.is_file() else []
    if not data_dir.is_dir():
        return []
    candidates = []
    for entry in data_dir.iterdir():
        if entry.is_file() and re.fullmatch(r"(kilo|opencode)[^/\\]*\.db", entry.name, re.IGNORECASE):
            try:
                candidates.append((entry, entry.stat().st_mtime))
            except OSError:
                continue
    candidates.sort(key=lambda item: -item[1])
    return [entry for entry, _ in candidates]


def db_sessions(db_path):
    if not db_path.is_file():
        return []
    sessions = []
    try:
        with open_db(db_path) as conn:
            tables = table_names(conn)
            if "session" not in tables:
                return []
            workspace_join = ", w.directory AS workspace_directory" if "workspace" in tables else ""
            workspace_left = "LEFT JOIN workspace w ON w.id = s.workspace_id" if "workspace" in tables else ""
            rows = conn.execute(
                f"""
                SELECT s.*, p.worktree AS project_worktree, p.name AS project_name{workspace_join}
                FROM session s
                LEFT JOIN project p ON p.id = s.project_id
                {workspace_left}
                ORDER BY s.time_updated DESC
                """
            ).fetchall()
            source = f"sqlite:{db_path.name}"
            for row in rows:
                sessions.append(
                    {
                        "session_id": row["id"],
                        "title": row["title"] or row["slug"] or row["id"],
                        "directory": row["directory"] or row["workspace_directory"] or row["project_worktree"] or "",
                        "project_id": row["project_id"],
                        "project_name": row["project_name"] or "",
                        "parent_id": row["parent_id"],
                        "version": row["version"] or "",
                        "agent": row["agent"] or "",
                        "model": row["model"] or "",
                        "created": row["time_created"],
                        "updated": row["time_updated"],
                        "archived": row["time_archived"],
                        "cost": row["cost"],
                        "summary_additions": row["summary_additions"],
                        "summary_deletions": row["summary_deletions"],
                        "summary_files": row["summary_files"],
                        "tokens": {
                            "input": row["tokens_input"],
                            "output": row["tokens_output"],
                            "reasoning": row["tokens_reasoning"],
                            "cache_read": row["tokens_cache_read"],
                            "cache_write": row["tokens_cache_write"],
                        },
                        "source": source,
                        "path": db_path,
                        "tables": tables,
                        "message_count": 0,
                    }
                )
            return sessions
    except sqlite3.Error:
        return []


def legacy_task_roots(configured):
    if configured:
        resolved = Path(configured).expanduser()
        if resolved.name == "tasks":
            return [resolved]
        nested = resolved / "kilocode.kilo-code" / "tasks"
        if nested.is_dir():
            return [nested]
        return [resolved / "tasks"]
    return default_tasks_roots()


def migrated_session_id(task_id):
    digest = hashlib.sha1(str(task_id).encode("utf-8")).hexdigest()
    return "ses_migrated_" + digest[:26]


def legacy_task_date(task_id):
    try:
        value = float(task_id)
    except (TypeError, ValueError):
        return 0
    return int(value) if value > 1_000_000_000_000 else 0


def read_index_entries(tasks_root):
    entries = parse_json(read_text(tasks_root / "_index.json"), {})
    by_id = {}
    for entry in entries.get("entries") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            by_id[entry["id"]] = entry
    return by_id


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_json_array(path):
    text = read_text(path)
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def legacy_sessions(task_roots, known_ids):
    sessions = []
    for tasks_root in task_roots:
        if not tasks_root.is_dir():
            continue
        try:
            dirs = list(tasks_root.iterdir())
        except OSError:
            continue
        index_by_id = read_index_entries(tasks_root)
        for task_dir in dirs:
            if not task_dir.is_dir():
                continue
            api_file = task_dir / "api_conversation_history.json"
            history = read_json_array(api_file)
            if history is None:
                continue
            if task_dir.name in known_ids or migrated_session_id(task_dir.name) in known_ids:
                continue
            stored = parse_json(read_text(task_dir / "history_item.json"), None)
            indexed = index_by_id.get(task_dir.name, {})
            messages = [entry for entry in history if isinstance(entry, dict)]
            directory = str((stored or {}).get("workspace") or indexed.get("workspace") or "").strip()
            created = (
                timestamp_ms((stored or {}).get("ts", indexed.get("ts")))
                or legacy_task_date(task_dir.name)
            )
            updated = created
            try:
                updated = max(created, int(task_dir.stat().st_mtime * 1000))
            except OSError:
                pass
            source_label = tasks_root.parent.parent.name
            sessions.append(
                {
                    "session_id": task_dir.name,
                    "title": str((stored or {}).get("task") or indexed.get("task") or "").strip()[:120],
                    "directory": directory,
                    "project_id": "",
                    "project_name": "",
                    "parent_id": None,
                    "version": "",
                    "agent": "code",
                    "model": "",
                    "created": created,
                    "updated": updated,
                    "archived": 0,
                    "cost": 0,
                    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0},
                    "source": f"legacy-tasks:{source_label}",
                    "path": api_file,
                    "tables": None,
                    "message_count": len(messages),
                    "_history": messages,
                }
            )
    sessions.sort(key=lambda meta: -timestamp_ms(meta["updated"]))
    return sessions


def scan_sessions(data_dirs, tasks_dir):
    seen = set()
    sessions = []
    for data_dir in data_dirs:
        for db_path in find_databases(data_dir):
            for meta in db_sessions(db_path):
                if meta["session_id"] in seen:
                    continue
                seen.add(meta["session_id"])
                sessions.append(meta)
    sessions.extend(legacy_sessions(legacy_task_roots(tasks_dir), seen))
    sessions.sort(key=lambda meta: -timestamp_ms(meta["updated"]))
    return sessions


def message_time(data, fallback):
    return timestamp_ms((data.get("time") or {}).get("created")) or timestamp_ms(fallback)


def part_time(data, fallback):
    return timestamp_ms((data.get("time") or {}).get("start")) or timestamp_ms(fallback)


def normalize_v1(messages, parts_by_message):
    items, summaries = [], []
    model = provider = agent = ""
    for message in messages:
        data = message["data"]
        role = data.get("role") or ""
        ts = message_time(data, message.get("time_created"))
        model_info = data.get("model") if isinstance(data.get("model"), dict) else {}
        if data.get("modelID") or model_info.get("modelID"):
            model = data.get("modelID") or model_info.get("modelID")
        elif isinstance(data.get("model"), str) and data.get("model"):
            model = data["model"]
        if data.get("providerID") or model_info.get("providerID"):
            provider = data.get("providerID") or model_info.get("providerID")
        if data.get("agent"):
            agent = data["agent"]
        for part in parts_by_message.get(message["id"], []):
            pdata = part["data"]
            ptype = pdata.get("type") or ""
            pts = part_time(pdata, part.get("time_created")) or ts
            if ptype == "text":
                text = pdata.get("text") or ""
                if not text.strip():
                    continue
                if pdata.get("synthetic") or data.get("summary") is True:
                    summaries.append(text)
                else:
                    items.append({"kind": f"{role}_text", "timestamp": pts, "text": text})
            elif ptype == "reasoning":
                continue
            elif ptype == "tool":
                state = pdata.get("state") or {}
                name = pdata.get("tool") or "tool"
                call_id = pdata.get("callID") or ""
                items.append({"kind": "tool_use", "timestamp": pts, "name": name, "input": state.get("input") or {}, "tool_use_id": call_id})
                status = str(state.get("status") or "")
                output_value = state.get("output")
                error = state.get("error")
                if output_value is not None or error is not None or status in ("completed", "error"):
                    output = output_value if isinstance(output_value, str) else ("" if output_value is None else json.dumps(output_value, ensure_ascii=False, separators=(",", ":")))
                    items.append(
                        {
                            "kind": "tool_result",
                            "timestamp": timestamp_ms((state.get("time") or {}).get("end")) or pts,
                            "tool_use_id": call_id,
                            "content": str(error) if error is not None else output,
                            "is_error": status == "error" or error is not None,
                        }
                    )
            elif ptype == "patch":
                items.append({"kind": "patch", "timestamp": pts, "files": pdata.get("files") or []})
            elif ptype == "file":
                source = pdata.get("source") or {}
                label = pdata.get("filename") or source.get("path") or source.get("uri") or "附件"
                items.append({"kind": "attachment", "timestamp": pts, "text": str(label)})
    items.sort(key=lambda item: item.get("timestamp") or 0)
    return {"items": items, "summaries": summaries, "model": model, "provider": provider, "agent": agent}


def tool_result_text(state):
    if state.get("error") is not None:
        error = state["error"]
        return error if isinstance(error, str) else json.dumps(error, ensure_ascii=False, separators=(",", ":"))
    output = state.get("output")
    if output is not None and not isinstance(output, (dict, list)):
        return str(output)
    content = state.get("content")
    if not isinstance(content, list):
        return "" if output is None else json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    lines = []
    for entry in content:
        if not isinstance(entry, dict):
            lines.append("" if entry is None else str(entry))
        elif entry.get("type") == "text":
            lines.append(str(entry.get("text") or ""))
        else:
            lines.append(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(line for line in lines if line)


def normalize_v2(rows):
    items, summaries = [], []
    model = provider = agent = ""

    def sort_key(row):
        return (row["seq"] if row["seq"] is not None else timestamp_ms(row["time_created"]), str(row["id"]))

    for row in sorted(rows, key=sort_key):
        data = row["data"] or {}
        ts = timestamp_ms((data.get("time") or {}).get("created")) or timestamp_ms(row["time_created"])
        rtype = row["type"]
        if rtype == "user":
            text = str(data.get("text") or "").strip()
            if text:
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
            for file in data.get("files") or []:
                if isinstance(file, str):
                    label = file
                elif isinstance(file, dict):
                    label = file.get("name") or file.get("uri") or "附件"
                else:
                    label = "附件"
                items.append({"kind": "attachment", "timestamp": ts, "text": str(label)})
        elif rtype in ("synthetic", "compaction"):
            text = str(data.get("summary") or data.get("kilo_summary") or data.get("text") or "").strip()
            if text:
                summaries.append(text)
        elif rtype == "shell":
            command = str(data.get("command") or "").strip()
            call_id = data.get("callID") or f"shell-{row['id']}"
            items.append({"kind": "tool_use", "timestamp": ts, "name": "bash", "input": {"command": command}, "tool_use_id": call_id})
            items.append(
                {
                    "kind": "tool_result",
                    "timestamp": timestamp_ms((data.get("time") or {}).get("completed")) or ts,
                    "tool_use_id": call_id,
                    "content": str(data.get("output") or ""),
                    "is_error": False,
                }
            )
        elif rtype == "assistant":
            for entry in data.get("content") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "text":
                    text = str(entry.get("text") or "")
                    if text.strip():
                        items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
                elif entry.get("type") == "tool":
                    state = entry.get("state") or {}
                    call_id = entry.get("id") or ""
                    items.append({"kind": "tool_use", "timestamp": ts, "name": entry.get("name") or "tool", "input": state.get("input") or {}, "tool_use_id": call_id})
                    status = str(state.get("status") or "")
                    if status in ("completed", "error"):
                        items.append(
                            {
                                "kind": "tool_result",
                                "timestamp": timestamp_ms((entry.get("time") or {}).get("completed")) or ts,
                                "tool_use_id": call_id,
                                "content": tool_result_text(state),
                                "is_error": status == "error",
                            }
                        )
        elif rtype == "model-switched":
            ref = data.get("model") or {}
            if ref.get("modelID"):
                model = ref["modelID"]
            if ref.get("providerID"):
                provider = ref["providerID"]
        elif rtype == "agent-switched":
            if data.get("agent"):
                agent = data["agent"]
    items.sort(key=lambda item: item.get("timestamp") or 0)
    return {"items": items, "summaries": summaries, "model": model, "provider": provider, "agent": agent}


def db_todos(conn, tables, session_id):
    if "todo" not in tables:
        return []
    try:
        return [
            {"content": row["content"], "status": row["status"], "priority": row["priority"]}
            for row in conn.execute(
                "SELECT content, status, priority FROM todo WHERE session_id=? ORDER BY position", (session_id,)
            ).fetchall()
        ]
    except sqlite3.Error:
        return []


def load_db_session(meta):
    with open_db(meta["path"]) as conn:
        tables = meta["tables"] or table_names(conn)
        normalized = {"items": [], "summaries": [], "model": meta.get("model") or "", "provider": "", "agent": meta.get("agent") or ""}
        if "message" in tables and "part" in tables:
            messages = [
                {"id": row["id"], "time_created": row["time_created"], "data": parse_json(row["data"])}
                for row in conn.execute(
                    "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id",
                    (meta["session_id"],),
                ).fetchall()
            ]
            parts_by_message = {}
            for row in conn.execute(
                "SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created, id",
                (meta["session_id"],),
            ).fetchall():
                parts_by_message.setdefault(row["message_id"], []).append(
                    {"id": row["id"], "time_created": row["time_created"], "data": parse_json(row["data"])}
                )
            if messages:
                normalized = normalize_v1(messages, parts_by_message)
        if not normalized["items"] and "session_message" in tables:
            rows = [
                {
                    "id": row["id"],
                    "type": row["type"],
                    "seq": row["seq"],
                    "time_created": row["time_created"],
                    "data": parse_json(row["data"]),
                }
                for row in conn.execute(
                    "SELECT id, type, seq, time_created, data FROM session_message WHERE session_id=?",
                    (meta["session_id"],),
                ).fetchall()
            ]
            if rows:
                normalized = normalize_v2(rows)
        if not normalized["model"] and meta.get("model"):
            normalized["model"] = meta["model"]
        if not normalized["agent"] and meta.get("agent"):
            normalized["agent"] = meta["agent"]
        return normalized, db_todos(conn, tables, meta["session_id"])


def strip_legacy_noise(text):
    return LEGACY_BLOCK_RE.sub("", str(text or "")).strip()


def legacy_text_from_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def legacy_first_task(messages):
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = legacy_text_from_content(message.get("content"))
        if not text.strip():
            continue
        task = LEGACY_TASK_RE.search(text)
        if task:
            text = task.group(1)
        text = strip_legacy_noise(text)
        if not text:
            continue
        first = text.splitlines()[0] if text.splitlines() else ""
        return first[:120]
    return ""


def normalize_legacy(messages):
    items = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role") or ""
        content = message.get("content")
        if role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        block_content = block.get("content")
                        if isinstance(block_content, str):
                            text = block_content
                        elif isinstance(block_content, list):
                            parts = []
                            for entry in block_content:
                                if isinstance(entry, dict):
                                    parts.append(str(entry.get("text") or json.dumps(entry, ensure_ascii=False, separators=(",", ":"))))
                                else:
                                    parts.append("" if entry is None else str(entry))
                            text = "\n".join(part for part in parts if part)
                        else:
                            text = "" if block_content is None else str(block_content)
                        items.append(
                            {
                                "kind": "tool_result",
                                "timestamp": 0,
                                "tool_use_id": block.get("tool_use_id") or "",
                                "content": text,
                                "is_error": bool(block.get("is_error")),
                            }
                        )
            text = strip_legacy_noise(LEGACY_TASK_RE_G.sub(lambda m: "\n" + m.group(1) + "\n", legacy_text_from_content(content)))
            if text:
                items.append({"kind": "user_text", "timestamp": 0, "text": text})
        elif role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = str(block.get("text") or "")
                        if text.strip():
                            items.append({"kind": "assistant_text", "timestamp": 0, "text": text})
                    elif block.get("type") == "tool_use":
                        items.append(
                            {
                                "kind": "tool_use",
                                "timestamp": 0,
                                "name": block.get("name") or "tool",
                                "input": block.get("input") or {},
                                "tool_use_id": block.get("id") or "",
                            }
                        )
    return {"items": items, "summaries": [], "model": "", "provider": "", "agent": "code"}


def load_session(meta):
    if meta["source"].startswith("sqlite:"):
        normalized, todos = load_db_session(meta)
    else:
        normalized = normalize_legacy(meta.get("_history") or [])
        todos = []
    if not normalized["model"] and meta.get("model"):
        normalized["model"] = meta["model"]
    if not normalized["agent"] and meta.get("agent"):
        normalized["agent"] = meta["agent"]
    timestamps = [item["timestamp"] for item in normalized["items"] if item.get("timestamp")]
    return {
        "info": {
            "session_id": meta["session_id"],
            "title": meta["title"],
            "directory": meta["directory"],
            "project_id": meta["project_id"],
            "project_name": meta["project_name"],
            "parent_id": meta["parent_id"],
            "version": meta["version"],
            "model": normalized["model"],
            "provider": normalized["provider"],
            "agent": normalized["agent"],
            "source": meta["source"],
            "first_ts": fmt_time(timestamps[0] if timestamps else meta["created"]),
            "last_ts": fmt_time(timestamps[-1] if timestamps else meta["updated"]),
            "archived": bool(meta["archived"]),
            "cost": meta.get("cost") or 0,
            "tokens": meta.get("tokens") or {},
            "summary": {
                "additions": meta.get("summary_additions"),
                "deletions": meta.get("summary_deletions"),
                "files": meta.get("summary_files"),
            },
        },
        "normalized": normalized,
        "todos": todos,
    }


def tool_file(name, input_data):
    for key in ("filePath", "file_path", "path", "filename"):
        if input_data.get(key):
            return str(input_data[key])
    if name in ("glob", "grep", "codesearch"):
        return str(input_data.get("pattern") or input_data.get("query") or "")
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
        kind = item["kind"]
        if kind == "user_text":
            first_user = first_user or item["text"]
            last_user = item["text"]
        elif kind == "assistant_text":
            last_assistant = item["text"]
        elif kind == "patch":
            files_edited.extend(str(name) for name in item.get("files") or [])
        elif kind == "tool_use":
            name = str(item.get("name") or "").lower()
            input_data = item.get("input") or {}
            calls[item.get("tool_use_id") or f"#{len(calls)}"] = (name, input_data)
            if name in READ_TOOLS:
                files_read.append(tool_file(name, input_data))
            elif name in EDIT_TOOLS:
                files_edited.append(tool_file(name, input_data))
            elif name in SHELL_TOOLS:
                commands.append(shell_command(input_data))
        elif kind == "tool_result":
            name, input_data = calls.get(item.get("tool_use_id"), ("", {}))
            content = item.get("content") or ""
            command = shell_command(input_data) if name in SHELL_TOOLS else ""
            if content.strip() and (
                item.get("is_error") or TEST_CMD_RE.search(command) or TEST_RESULT_RE.search(content[:2000])
            ):
                test_results.append(
                    {"command_hint": command or name, "is_error": bool(item.get("is_error")), "content": content}
                )
    return {
        "goal": first_user,
        "files_read": dedupe(files_read),
        "files_edited": dedupe(files_edited),
        "commands": dedupe(commands),
        "test_results": test_results,
        "todos": todos or [],
        "last_user": last_user,
        "last_assistant": last_assistant,
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
    detail = shell_command(input_data) or tool_file(str(name).lower(), input_data)
    if detail:
        return f"{name}({truncate(detail, 100)})"
    return f"{name}(...)"


def render_item(item, max_chars):
    ts = fmt_time(item.get("timestamp"))
    time = f" {ts}" if ts else ""
    kind = item["kind"]
    if kind == "user_text":
        return [f"### [用户]{time}", text_block(item["text"], max_chars)]
    if kind == "assistant_text":
        return [f"### [助手]{time}", text_block(item["text"], max_chars)]
    if kind == "tool_use":
        return [
            f"### [工具调用] {item.get('name') or 'tool'}{time}",
            "```json",
            truncate(json.dumps(item.get("input") or {}, ensure_ascii=False, separators=(",", ":")), max_chars),
            "```",
        ]
    if kind == "tool_result":
        error = " (错误)" if item.get("is_error") else ""
        return [f"### [工具结果]{error}{time}", text_block(item.get("content"), max_chars)]
    if kind == "patch":
        return [f"### [代码补丁]{time}", "\n".join(f"- {name}" for name in item.get("files") or [])]
    if kind == "attachment":
        return [f"### [附件]{time}", item.get("text") or ""]
    return []


def format_tokens(tokens):
    parts = []
    for key, label in (("input", "输入"), ("output", "输出"), ("reasoning", "推理"), ("cache_read", "缓存读")):
        if tokens and tokens.get(key):
            parts.append(f"{label} {tokens[key]}")
    return " / ".join(parts)


def render_summary(session, state, recent_n, max_chars):
    info = session["info"]
    norm = session["normalized"]
    lines = [
        "# Resume-Kilo 会话接管摘要",
        "",
        "## 会话信息",
        f"- 标题: {info['title']}",
        f"- 会话ID: {info['session_id']}",
        f"- 项目: {info['directory'] or '(未知)'}",
        f"- 存储: {info['source']}",
    ]
    if info["agent"]:
        lines.append(f"- Agent: {info['agent']}")
    if info["model"]:
        lines.append(f"- 模型: {info['provider'] + '/' if info['provider'] else ''}{info['model']}")
    if info["version"]:
        lines.append(f"- Kilo 版本: {info['version']}")
    if info["parent_id"]:
        lines.append(f"- 父会话: {info['parent_id']}")
    if info["archived"]:
        lines.append("- 状态: 已归档")
    if info["cost"]:
        lines.append(f"- 累计费用: ${float(info['cost']):.4f}")
    tokens = format_tokens(info["tokens"])
    if tokens:
        lines.append(f"- Token 用量: {tokens}")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(norm['items'])}")
    lines.append("")
    if norm["summaries"]:
        lines.append("## 历史摘要（原会话 compact）")
        for value in norm["summaries"]:
            lines.append(f"- {truncate(value, max_chars)}")
        lines.append("")
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    if state["todos"]:
        lines.append("### 任务清单（todo）")
        for todo in state["todos"]:
            status = todo.get("status") or ""
            mark = "[x]" if status == "completed" else ("[~]" if status == "in_progress" else "[ ]")
            suffix = f"（{status}）" if status and status != "pending" else ""
            lines.append(f"- {mark} {truncate(todo.get('content'), 200)}{suffix}")
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
            first = result["content"].strip().splitlines()[0] if result["content"].strip().splitlines() else ""
            prefix = " [错误]" if result["is_error"] else ""
            lines.append(f"-{prefix} {truncate(first, 200)}")
        lines.append("")
    lines.extend(["### 最近用户消息", text_block(state["last_user"], max_chars) or "(无)"])
    lines.extend(["", "### 最近助手消息", text_block(state["last_assistant"], max_chars) or "(无)", ""])
    recent = norm["items"][-recent_n:] if recent_n else []
    lines.extend([f"## 近期对话（最近 {len(recent)} 条）", ""])
    for item in recent:
        lines.extend(render_item(item, max_chars))
        lines.append("")
    older_tools = [item for item in norm["items"][:-recent_n] if item["kind"] == "tool_use"] if recent_n else []
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 60 条）")
        for item in older_tools[-60:]:
            stamp = fmt_time(item.get("timestamp")) or "(无时间)"
            lines.append(f"- [{stamp}] {tool_brief(item)}")
        lines.append("")
    lines.extend(
        [
            "## 接管建议",
            "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。",
            "- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。",
            "- 不要逐字复述历史；基于现状决定下一步动作。",
            "",
        ]
    )
    return "\n".join(lines)


def project_sessions(sessions, project_path):
    target = norm_path(project_path)
    return [meta for meta in sessions if norm_path(meta["directory"]) == target]


def pick_session(sessions, session_arg, project_path):
    if session_arg:
        for meta in sessions:
            if meta["session_id"].startswith(session_arg) or session_arg in meta["session_id"]:
                return meta
        return None
    selected = project_sessions(sessions, project_path)
    return selected[0] if selected else None


def print_list(sessions, project_path, limit):
    selected = project_sessions(sessions, project_path)
    shown = selected[:limit] if limit > 0 else selected
    print(f"当前项目: {project_path}")
    suffix = f"（仅显示最近 {len(shown)} 个）" if len(shown) < len(selected) else ""
    print(f"找到 {len(selected)} 个会话{suffix}：\n")
    for index, meta in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        print(f"{mark} {fmt_time(meta['updated']) or '(无时间)'}  {meta['session_id'][:16]}  标题: {meta['title']}")


def print_help(parser):
    parser.print_help()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="resume_kilo.py",
        description="读取 Kilo Code 本地会话，生成结构化接管摘要。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "数据来源（按优先级）:\n"
            "  1. Kilo 数据目录下的 kilo.db（CLI 与新版 VS Code 扩展，opencode 风格 SQLite）\n"
            "  2. 旧版 VS Code 扩展任务目录 globalStorage/kilocode.kilo-code/tasks\n"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    group.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", metavar="ID", help="指定会话 ID 或前缀；跨项目、跨来源查找")
    parser.add_argument("--project", metavar="PATH", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--kilo-dir", metavar="DIR", help="Kilo 数据目录（含 kilo.db 的一级），默认 ~/.local/share/kilo")
    parser.add_argument("--tasks-dir", metavar="DIR", help="旧版任务目录或其上层（globalStorage），默认自动探测")
    parser.add_argument("--recent", type=int, default=8, help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, help="单条截断长度，默认 1500")
    parser.add_argument("--limit", type=int, default=0, help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", metavar="FILE", help="将摘要写入文件")
    return parser


def main():
    setup_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    data_dirs = [Path(args.kilo_dir).expanduser()] if args.kilo_dir else default_data_dir_candidates()
    has_kilo_data = (
        bool(args.kilo_dir)
        or bool(os.environ.get("KILO_DB"))
        or any(d.is_dir() for d in data_dirs)
        or any(d.is_dir() for d in legacy_task_roots(args.tasks_dir))
    )
    if not has_kilo_data:
        joined = "、".join(str(d) for d in data_dirs)
        print(f"错误：未找到 Kilo Code 本地数据（已探测 {joined} 与旧版扩展任务目录）。", file=sys.stderr)
        print("可用 --kilo-dir / --tasks-dir 指定。", file=sys.stderr)
        sys.exit(1)
    sessions = scan_sessions(data_dirs, args.tasks_dir)
    if not sessions:
        print("错误：未找到任何 Kilo 会话（已检查 kilo.db 与旧版扩展任务目录）。", file=sys.stderr)
        sys.exit(1)
    project_path = os.path.abspath(args.project)
    if args.list:
        if not project_sessions(sessions, project_path):
            print(f"错误：未找到项目 {project_path} 的 Kilo 会话。可用 --session ID 跨项目查找。", file=sys.stderr)
            sys.exit(1)
        print_list(sessions, project_path, args.limit)
        return
    target = pick_session(sessions, args.session, project_path)
    if target is None:
        print(f"错误：未匹配到会话 '{args.session or '当前项目'}'。", file=sys.stderr)
        sys.exit(1)
    try:
        session = load_session(target)
    except (sqlite3.Error, OSError) as error:
        print(f"错误：解析会话失败：{error}", file=sys.stderr)
        sys.exit(1)
    state = build_state(session["normalized"]["items"], session["todos"])
    recent_count = max(args.recent, 0)
    recent_items = session["normalized"]["items"][-recent_count:] if recent_count else []
    if args.json:
        output = json.dumps(
            {
                "info": session["info"],
                "state": state,
                "summaries": session["normalized"]["summaries"],
                "recent_items": recent_items,
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = render_summary(session, state, recent_count, max(args.max_chars, 1))
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(output)
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
