#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/kilo: Kilo Code（kilo.db SQLite 或旧版 VS Code 扩展任务目录）-> 归一化会话。

格式依据（与 resume-kilo 一致）：
  数据来源（按优先级）：
  1. Kilo 数据目录下的 kilo.db（CLI 与新版 VS Code 扩展，opencode 风格 SQLite）
     - v1 存储：message（role 数据）+ part（text/reasoning/tool/patch/file 分片）
     - v2 投影：session_message（user/synthetic/compaction/shell/assistant/model-switched 等）
  2. 旧版 VS Code 扩展任务目录 globalStorage/kilocode.kilo-code/tasks/<任务ID>/
     - api_conversation_history.json（Anthropic 风格消息数组）；
       history_item.json / _index.json 提供标题与工作区
转换语义：
  - 用户/助手文本 -> m_message；推理（reasoning）与 patch/附件等元数据跳过
  - 工具调用/结果 -> m_tool_use / m_tool_result（call_id 用源真实 callID/id）
  - synthetic / compaction 摘要 -> m_note
标题：session.title/slug（DB）或 history_item.task / _index.task（旧版）。
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

# 与 resume-kilo 一致：正式渠道 kilo.db，开发渠道 kilo-<channel>.db，回退 opencode-*.db
DB_NAME_RE = re.compile(r"(kilo|opencode)[^/\\]*\.db", re.IGNORECASE)
# 旧版任务在用户消息外包裹的环境与系统块，不属于任务内容本身
LEGACY_BLOCK_RE = re.compile(
    r"<(environment_details|system-reminder|fetch_instructions|notice|custom_instructions|rules)[\s\S]*?</\1>")
LEGACY_TASK_RE = re.compile(r"<task>([\s\S]*?)</task>")
LEGACY_TASK_RE_G = re.compile(r"<task>([\s\S]*?)</task>")


# ---------------------------------------------------------------- 基础工具

def default_dir():
    """Kilo 数据目录（含 kilo.db 的一级）：$KILO_DATA_DIR > ~/.local/share/kilo。"""
    override = os.environ.get("KILO_DATA_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".local", "share", "kilo")


def data_dir_candidates():
    """无显式目录时的候选数据目录（与 resume-kilo 探测顺序一致）。"""
    candidates = []
    override = os.environ.get("KILO_DATA_DIR")
    if override:
        candidates.append(os.path.expanduser(override))
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidates.append(os.path.join(os.path.expanduser(xdg), "kilo"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".local", "share", "kilo"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "kilo"))
    return candidates


def editor_global_storage_roots():
    """Kilo 扩展可能装在 VS Code 及其各类分支中，逐个探测 globalStorage。"""
    editors = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Trae"]
    roots = []
    if sys.platform == "darwin":
        home = os.path.expanduser("~")
        for editor in editors:
            roots.append(os.path.join(home, "Library", "Application Support", editor,
                                      "User", "globalStorage"))
    elif sys.platform == "win32":
        app_data = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        for editor in editors:
            roots.append(os.path.join(app_data, editor, "User", "globalStorage"))
    else:
        home = os.path.expanduser("~")
        for editor in editors:
            roots.append(os.path.join(home, ".config", editor, "User", "globalStorage"))
    return roots


def default_tasks_roots():
    """旧版任务目录候选：$KILO_TASKS_DIR + 各编辑器 globalStorage。"""
    roots = []
    override = os.environ.get("KILO_TASKS_DIR")
    if override:
        roots.append(os.path.abspath(os.path.expanduser(override)))
    for root in editor_global_storage_roots():
        roots.append(os.path.join(root, "kilocode.kilo-code", "tasks"))
    return roots


def legacy_task_roots(configured):
    """旧版任务目录解析：tasks 结尾直接用；否则探测 kilocode.kilo-code/tasks 或 <dir>/tasks。"""
    if configured:
        resolved = os.path.abspath(os.path.expanduser(str(configured)))
        if os.path.basename(resolved) == "tasks":
            return [resolved]
        nested = os.path.join(resolved, "kilocode.kilo-code", "tasks")
        if os.path.isdir(nested):
            return [nested]
        return [os.path.join(resolved, "tasks")]
    return default_tasks_roots()


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if value is None or value == "":
        return {}
    if isinstance(value, str) and not value.strip():
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def timestamp_ms(value):
    """秒/毫秒时间戳与 ISO 字符串统一为毫秒整数（与 resume-kilo 一致）。"""
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
    """毫秒时间戳 -> UTC ISO（毫秒精度）；0/非法返回空，供 cs.m_* 的 ts 使用。"""
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def row_get(row, key, default=""):
    """安全读取 sqlite3.Row 字段；列缺失时给默认值（脚本假设完整 schema）。"""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


# ---------------------------------------------------------------- 数据库来源

def open_db(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def find_databases(data_dir):
    override = os.environ.get("KILO_DB")
    if override:
        configured = override if os.path.isabs(override) else os.path.join(data_dir, override)
        return [configured] if os.path.isfile(configured) else []
    if not os.path.isdir(data_dir):
        return []
    try:
        entries = os.listdir(data_dir)
    except OSError:
        return []
    candidates = []
    for name in entries:
        full = os.path.join(data_dir, name)
        if os.path.isfile(full) and DB_NAME_RE.fullmatch(name):
            try:
                candidates.append((full, os.path.getmtime(full)))
            except OSError:
                continue
    candidates.sort(key=lambda item: -item[1])
    return [path for path, _ in candidates]


def db_sessions(db_path):
    if not os.path.isfile(db_path):
        return []
    sessions = []
    try:
        conn = open_db(db_path)
        try:
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
            source = f"sqlite:{os.path.basename(db_path)}"
            for row in rows:
                sessions.append({
                    "session_id": row["id"],
                    "title": row_get(row, "title") or row_get(row, "slug") or row["id"],
                    "directory": (row_get(row, "directory") or row_get(row, "workspace_directory")
                                  or row_get(row, "project_worktree") or ""),
                    "parent_id": row_get(row, "parent_id", None),
                    "version": row_get(row, "version"),
                    "agent": row_get(row, "agent"),
                    "model": row_get(row, "model"),
                    "created": row_get(row, "time_created", 0),
                    "updated": row_get(row, "time_updated", 0),
                    "archived": row_get(row, "time_archived", 0),
                    "cost": row_get(row, "cost", 0),
                    "source": source,
                    "path": db_path,
                    "tables": tables,
                })
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return sessions


# ---------------------------------------------------------------- 旧版任务目录

def migrated_session_id(task_id):
    """与扩展迁移逻辑一致：迁移后的会话 ID = ses_migrated_<sha1(旧任务ID)[:26]>。"""
    import hashlib
    digest = hashlib.sha1(str(task_id).encode("utf-8")).hexdigest()
    return "ses_migrated_" + digest[:26]


def legacy_task_date(task_id):
    try:
        value = float(task_id)
    except (TypeError, ValueError):
        return 0
    return int(value) if value > 1_000_000_000_000 else 0


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
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


def read_index_entries(tasks_root):
    try:
        entries = parse_json(read_text(os.path.join(tasks_root, "_index.json")))
    except OSError:
        entries = {}
    by_id = {}
    for entry in entries.get("entries") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            by_id[entry["id"]] = entry
    return by_id


def legacy_sessions(task_roots, known_ids):
    sessions = []
    for tasks_root in task_roots:
        if not os.path.isdir(tasks_root):
            continue
        try:
            dirs = os.listdir(tasks_root)
        except OSError:
            continue
        index_by_id = read_index_entries(tasks_root)
        for name in dirs:
            task_dir = os.path.join(tasks_root, name)
            if not os.path.isdir(task_dir):
                continue
            api_file = os.path.join(task_dir, "api_conversation_history.json")
            history = read_json_array(api_file)
            if history is None:
                continue
            if name in known_ids or migrated_session_id(name) in known_ids:
                continue
            stored = parse_json(read_text(os.path.join(task_dir, "history_item.json")))
            indexed = index_by_id.get(name, {})
            messages = [entry for entry in history if isinstance(entry, dict)]
            directory = str(stored.get("workspace") or indexed.get("workspace") or "").strip()
            created = (timestamp_ms(stored.get("ts", indexed.get("ts")))
                       or legacy_task_date(name))
            updated = created
            try:
                updated = max(created, int(os.stat(task_dir).st_mtime * 1000))
            except OSError:
                pass
            source_label = os.path.basename(os.path.dirname(os.path.dirname(tasks_root)))
            sessions.append({
                "session_id": name,
                "title": str(stored.get("task") or indexed.get("task") or "").strip()[:120],
                "directory": directory,
                "parent_id": None,
                "version": "",
                "agent": "code",
                "model": "",
                "created": created,
                "updated": updated,
                "archived": 0,
                "cost": 0,
                "source": f"legacy-tasks:{source_label}",
                "path": api_file,
                "tables": None,
                "_history": messages,
            })
    sessions.sort(key=lambda meta: -timestamp_ms(meta["updated"]))
    return sessions


def scan_sessions(dir_=None):
    """扫描全部 Kilo 会话（DB + 旧版任务），按更新时间倒序。"""
    if dir_:
        data_dirs = [os.path.abspath(os.path.expanduser(str(dir_)))]
        tasks_roots = legacy_task_roots(dir_)
    else:
        data_dirs = data_dir_candidates()
        tasks_roots = default_tasks_roots()
    seen = set()
    sessions = []
    for data_dir in data_dirs:
        for db_path in find_databases(data_dir):
            for meta in db_sessions(db_path):
                if meta["session_id"] in seen:
                    continue
                seen.add(meta["session_id"])
                sessions.append(meta)
    sessions.extend(legacy_sessions(tasks_roots, seen))
    sessions.sort(key=lambda meta: -timestamp_ms(meta["updated"]))
    return sessions


# ---------------------------------------------------------------- v1 归一化

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
                items.append({"kind": "tool_use", "timestamp": pts, "name": name,
                              "input": state.get("input") or {}, "tool_use_id": call_id})
                status = str(state.get("status") or "")
                output_value = state.get("output")
                error = state.get("error")
                if output_value is not None or error is not None or status in ("completed", "error"):
                    output = output_value if isinstance(output_value, str) else (
                        "" if output_value is None else json.dumps(
                            output_value, ensure_ascii=False, separators=(",", ":")))
                    items.append({
                        "kind": "tool_result",
                        "timestamp": timestamp_ms((state.get("time") or {}).get("end")) or pts,
                        "tool_use_id": call_id,
                        "content": str(error) if error is not None else output,
                        "is_error": status == "error" or error is not None,
                    })
            # patch / file 为源工具元数据（代码补丁、附件），不进入对话
    items.sort(key=lambda item: item.get("timestamp") or 0)
    return {"items": items, "summaries": summaries, "model": model,
            "provider": provider, "agent": agent}


# ---------------------------------------------------------------- v2 归一化

def tool_result_text(state):
    if state.get("error") is not None:
        error = state["error"]
        return error if isinstance(error, str) else json.dumps(
            error, ensure_ascii=False, separators=(",", ":"))
    output = state.get("output")
    if output is not None and not isinstance(output, (dict, list)):
        return str(output)
    content = state.get("content")
    if not isinstance(content, list):
        return "" if output is None else json.dumps(
            output, ensure_ascii=False, separators=(",", ":"))
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
        return (row["seq"] if row["seq"] is not None else timestamp_ms(row["time_created"]),
                str(row["id"]))

    for row in sorted(rows, key=sort_key):
        data = row["data"] or {}
        ts = timestamp_ms((data.get("time") or {}).get("created")) or timestamp_ms(row["time_created"])
        rtype = row["type"]
        if rtype == "user":
            text = str(data.get("text") or "").strip()
            if text:
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
            # user.files 附件为元数据，不进入对话
        elif rtype in ("synthetic", "compaction"):
            text = str(data.get("summary") or data.get("kilo_summary") or data.get("text") or "").strip()
            if text:
                summaries.append(text)
        elif rtype == "shell":
            command = str(data.get("command") or "").strip()
            call_id = data.get("callID") or f"shell-{row['id']}"
            items.append({"kind": "tool_use", "timestamp": ts, "name": "bash",
                          "input": {"command": command}, "tool_use_id": call_id})
            items.append({
                "kind": "tool_result",
                "timestamp": timestamp_ms((data.get("time") or {}).get("completed")) or ts,
                "tool_use_id": call_id,
                "content": str(data.get("output") or ""),
                "is_error": False,
            })
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
                    items.append({"kind": "tool_use", "timestamp": ts,
                                  "name": entry.get("name") or "tool",
                                  "input": state.get("input") or {}, "tool_use_id": call_id})
                    status = str(state.get("status") or "")
                    if status in ("completed", "error"):
                        items.append({
                            "kind": "tool_result",
                            "timestamp": timestamp_ms((entry.get("time") or {}).get("completed")) or ts,
                            "tool_use_id": call_id,
                            "content": tool_result_text(state),
                            "is_error": status == "error",
                        })
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
    return {"items": items, "summaries": summaries, "model": model,
            "provider": provider, "agent": agent}


# ---------------------------------------------------------------- 旧版归一化

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
                                    parts.append(str(entry.get("text") or json.dumps(
                                        entry, ensure_ascii=False, separators=(",", ":"))))
                                else:
                                    parts.append("" if entry is None else str(entry))
                            text = "\n".join(part for part in parts if part)
                        else:
                            text = "" if block_content is None else str(block_content)
                        items.append({
                            "kind": "tool_result",
                            "timestamp": 0,
                            "tool_use_id": block.get("tool_use_id") or "",
                            "content": text,
                            "is_error": bool(block.get("is_error")),
                        })
            text = strip_legacy_noise(
                LEGACY_TASK_RE_G.sub(lambda m: "\n" + m.group(1) + "\n",
                                     legacy_text_from_content(content)))
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
                        items.append({
                            "kind": "tool_use",
                            "timestamp": 0,
                            "name": block.get("name") or "tool",
                            "input": block.get("input") or {},
                            "tool_use_id": block.get("id") or "",
                        })
    return {"items": items, "summaries": [], "model": "", "provider": "", "agent": "code"}


# ---------------------------------------------------------------- 加载与转换

def load_db_session(meta):
    normalized = {"items": [], "summaries": [], "model": meta.get("model") or "",
                  "provider": "", "agent": meta.get("agent") or ""}
    conn = open_db(meta["path"])
    try:
        tables = meta["tables"] or table_names(conn)
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
                    {"id": row["id"], "time_created": row["time_created"], "data": parse_json(row["data"])})
            if messages:
                normalized = normalize_v1(messages, parts_by_message)
        if not normalized["items"] and "session_message" in tables:
            rows = [
                {"id": row["id"], "type": row["type"], "seq": row["seq"],
                 "time_created": row["time_created"], "data": parse_json(row["data"])}
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
        return normalized
    finally:
        conn.close()


def load_normalized(meta):
    """单个会话元信息 -> 归一化结果（items/summaries/model/...）。"""
    if str(meta["source"]).startswith("sqlite:"):
        normalized = load_db_session(meta)
    else:
        normalized = normalize_legacy(meta.get("_history") or [])
    if not normalized["model"] and meta.get("model"):
        normalized["model"] = meta["model"]
    if not normalized["agent"] and meta.get("agent"):
        normalized["agent"] = meta["agent"]
    return normalized


def items_to_msgs(normalized):
    """归一化条目 -> cs 消息列表；摘要注记置于最前，其余严格保持源顺序。"""
    msgs = [cs.m_note(text) for text in normalized.get("summaries") or []]
    for item in normalized.get("items") or []:
        ts = ms_to_iso(item.get("timestamp"))
        kind = item["kind"]
        if kind == "user_text":
            msgs.append(cs.m_message("user", item["text"], ts))
        elif kind == "assistant_text":
            msgs.append(cs.m_message("assistant", item["text"], ts))
        elif kind == "tool_use":
            msgs.append(cs.m_tool_use(item.get("tool_use_id") or "",
                                      item.get("name") or "tool",
                                      item.get("input") or {}, ts))
        elif kind == "tool_result":
            msgs.append(cs.m_tool_result(item.get("tool_use_id") or "",
                                         item.get("content") or "",
                                         bool(item.get("is_error")), ts))
        # patch / attachment 等元数据分支不产生对话消息
    return msgs


def first_user_title(msgs, fallback):
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m["text"].strip():
            return m["text"].strip().splitlines()[0][:60]
    return fallback


# ---------------------------------------------------------------- 对外接口

def list_sessions(dir_=None, project=None):
    sessions = scan_sessions(dir_)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    out = []
    for meta in sessions[:50]:
        title = meta["title"] or ""
        count = 0
        last_ts = ""
        try:
            normalized = load_normalized(meta)
            msgs = items_to_msgs(normalized)
            count = len(msgs)
            last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
            if not title:
                title = first_user_title(msgs, meta["session_id"])
        except (cs.ConvertError, sqlite3.Error, OSError):
            if not title:
                title = meta["session_id"]
        out.append({
            "session_id": meta["session_id"], "title": title,
            "cwd": meta["directory"] or "",
            "mtime": timestamp_ms(meta["updated"]) / 1000.0,
            "path": str(meta["path"]),
            "count": count, "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    sessions = scan_sessions(dir_)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Kilo 会话（dir={dir_ or default_dir()}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    try:
        normalized = load_normalized(target)
    except (sqlite3.Error, OSError) as e:
        raise cs.ConvertError(f"解析会话失败：{e}")
    msgs = items_to_msgs(normalized)
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    sid = target["session_id"]
    title = target["title"] or first_user_title(msgs, sid)
    ref = cs.make_ref("kilo", sid, title, target["directory"] or "",
                      model=normalized["model"] or "",
                      started_at=ms_to_iso(timestamp_ms(target["created"])),
                      ended_at=ms_to_iso(timestamp_ms(target["updated"])),
                      source=str(target["path"]))
    return ref, msgs
