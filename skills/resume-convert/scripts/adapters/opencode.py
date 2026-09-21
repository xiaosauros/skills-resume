#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/opencode: OpenCode（~/.local/share/opencode SQLite / 旧版 JSON storage）-> 归一化会话。

格式依据（与 resume-opencode 一致）：
  数据目录：$OPENCODE_DATA_DIR > $XDG_DATA_HOME/opencode > ~/.local/share/opencode
  当前格式 opencode.db（SQLite，需含 session/message/part 表）：
    - session: id/title/slug/directory/project_id/version/time_created/time_updated...
      （LEFT JOIN project 取 worktree 作为目录兜底）
    - message: id, session_id, time_created, data(JSON: role/modelID/providerID/agent/
      summary/time.created)
    - part:    id, message_id, session_id, time_created, data(JSON, 按 type 分支)
  旧版格式 storage/{session,message,part}/**/*.json（db 缺失或无当前 schema 时回退）。
  part.type 分支：
    - text: 正文；synthetic 或所在消息 summary=true 视为压缩摘要（-> 转换注记）
    - tool: state.input/output/error + callID -> 工具调用/结果对
    - patch: 代码补丁文件列表（-> 转换注记）
    - file: 附件（filename 或 source.path -> 转换注记）
  推理/思考：源格式不记录，无需跳过分支。
标题：session.title || slug || id。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402


def default_dir():
    override = os.environ.get("OPENCODE_DATA_DIR")
    if override:
        return os.path.expanduser(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(os.path.expanduser(xdg), "opencode")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "opencode")


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def parse_json(value, default=None):
    """data 列可能已是 dict（驱动差异）或 TEXT JSON。"""
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if default is None else default


def to_ms(value):
    """时间值 -> 毫秒整数（与 resume_opencode.timestamp_ms 一致）：
    <1e10 视为秒、否则毫秒；字符串走 ISO 解析；解析失败返回 0。"""
    if value is None or value == "":
        return 0
    if isinstance(value, dict):
        value = value.get("updated") or value.get("completed") or value.get("created")
    try:
        number = float(value)
        return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0


def iso_from_ms(ms):
    """毫秒时间戳 -> UTC ISO（毫秒精度，Z 结尾，与 claude_session.to_iso_z 对齐）；0 视为无时间。"""
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def dump_compact(value):
    """非字符串的 output/error 统一为紧凑 JSON。与 JS JSON.stringify 逐字节一致
    （resume_opencode.py 的默认分隔符带空格，跨语言会不一致，这里统一取紧凑形式）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- SQLite / 旧版扫描

def open_db(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_current_schema(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"session", "message", "part"}.issubset(names)


def db_sessions(data_dir):
    db_path = os.path.join(data_dir, "opencode.db")
    if not os.path.isfile(db_path):
        return []
    try:
        conn = open_db(db_path)
        try:
            if not has_current_schema(conn):
                return []
            rows = conn.execute(
                """
                SELECT s.*, p.worktree AS project_worktree, p.name AS project_name
                FROM session s LEFT JOIN project p ON p.id = s.project_id
                ORDER BY s.time_updated DESC
                """
            ).fetchall()
            return [
                {
                    "session_id": row["id"],
                    "title": row["title"] or row["slug"] or row["id"],
                    "directory": row["directory"] or row["project_worktree"] or "",
                    "created": row["time_created"],
                    "updated": row["time_updated"],
                    "source": "sqlite",
                    "path": db_path,
                }
                for row in rows
            ]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return []


def legacy_sessions(data_dir):
    """旧版 storage/session/<projectID>/<sid>.json 扫描（resume_opencode.legacy_sessions）。"""
    root = os.path.join(data_dir, "storage", "session")
    sessions = []
    if not os.path.isdir(root):
        return sessions
    for project_id in sorted(os.listdir(root)):
        project_dir = os.path.join(root, project_id)
        if not os.path.isdir(project_dir):
            continue
        for name in sorted(os.listdir(project_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(project_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                continue
            sid = obj.get("id") or os.path.splitext(name)[0]
            times = obj.get("time") or {}
            try:
                mtime_ms = os.stat(path).st_mtime_ns // 1_000_000
            except OSError:
                mtime_ms = 0
            sessions.append({
                "session_id": sid,
                "title": obj.get("title") or obj.get("slug") or sid,
                "directory": obj.get("directory") or "",
                "created": times.get("created") or mtime_ms,
                "updated": times.get("updated") or mtime_ms,
                "source": "legacy-json",
                "path": path,
            })
    sessions.sort(key=lambda item: to_ms(item["updated"]), reverse=True)
    return sessions


def scan_sessions(data_dir):
    """当前 SQLite 优先，空结果时回退旧版 JSON（与 resume_opencode.scan_sessions 一致）。"""
    current = db_sessions(data_dir)
    return current if current else legacy_sessions(data_dir)


# ---------------------------------------------------------------- 归一化

def normalize_records(messages, parts_by_message):
    """消息+part -> 归一化条目（与 resume_opencode.normalize_records 的分支一致）。
    条目 kind：user_text / assistant_text / note(压缩摘要) / tool_use / tool_result /
    patch / attachment；按时间戳稳定排序保持源顺序。"""
    items = []
    model = provider = agent = ""
    for message in messages:
        data = message["data"]
        role = data.get("role") or ""
        ts = to_ms((data.get("time") or {}).get("created") or message.get("time_created"))
        model_info = data.get("model") or {}
        model = data.get("modelID") or model_info.get("modelID") or model
        provider = data.get("providerID") or model_info.get("providerID") or provider
        agent = data.get("agent") or agent
        for part in parts_by_message.get(message["id"], []):
            pdata = part["data"]
            ptype = pdata.get("type") or ""
            pts = to_ms((pdata.get("time") or {}).get("start") or part.get("time_created") or ts)
            if ptype == "text":
                text = pdata.get("text") or ""
                if not text.strip():
                    continue
                if pdata.get("synthetic") or data.get("summary") is True:
                    items.append({"kind": "note", "timestamp": pts, "text": text})
                else:
                    items.append({"kind": f"{role}_text", "timestamp": pts, "text": text})
            elif ptype == "tool":
                state = pdata.get("state") or {}
                name = pdata.get("tool") or "tool"
                call_id = pdata.get("callID") or ""
                items.append({
                    "kind": "tool_use", "timestamp": pts, "name": name,
                    "input": state.get("input") or {}, "call_id": call_id,
                })
                status = str(state.get("status") or "")
                output = state.get("output")
                error = state.get("error")
                if output is not None or error is not None or status in ("completed", "error"):
                    if not isinstance(output, str):
                        output = dump_compact(output) if output is not None else ""
                    if error is not None:
                        # resume 的 JS 版对 error 用 String()，对象会退化为 "[object Object]"；
                        # 这里两语言统一为：字符串原样、对象紧凑 JSON
                        content = error if isinstance(error, str) else dump_compact(error)
                    else:
                        content = output
                    items.append({
                        "kind": "tool_result",
                        "timestamp": to_ms((state.get("time") or {}).get("end")) or pts,
                        "call_id": call_id, "content": content,
                        "is_error": status == "error" or error is not None,
                    })
            elif ptype == "patch":
                items.append({"kind": "patch", "timestamp": pts,
                              "files": pdata.get("files") or []})
            elif ptype == "file":
                label = pdata.get("filename") or (pdata.get("source") or {}).get("path") or "附件"
                items.append({"kind": "attachment", "timestamp": pts, "text": str(label)})
    items.sort(key=lambda item: item.get("timestamp") or 0)
    return {"items": items, "model": model, "provider": provider, "agent": agent}


def items_to_msgs(items):
    """归一化条目 -> cs 消息流；patch / 附件降级为转换注记。"""
    msgs = []
    for item in items:
        ts = iso_from_ms(item.get("timestamp"))
        kind = item["kind"]
        if kind == "user_text":
            msgs.append(cs.m_message("user", item["text"], ts))
        elif kind == "assistant_text":
            msgs.append(cs.m_message("assistant", item["text"], ts))
        elif kind == "note":
            msgs.append(cs.m_note(item["text"], ts))
        elif kind == "tool_use":
            msgs.append(cs.m_tool_use(item["call_id"], item["name"], item["input"], ts))
        elif kind == "tool_result":
            msgs.append(cs.m_tool_result(item["call_id"], item["content"],
                                         item["is_error"], ts))
        elif kind == "patch":
            names = "、".join(str(f) for f in item.get("files") or [])
            text = f"代码补丁涉及文件：{names}" if names else "代码补丁（无文件记录）"
            msgs.append(cs.m_note(text, ts))
        elif kind == "attachment":
            msgs.append(cs.m_note(f"会话附件：{item['text']}", ts))
    return msgs


def load_db_session(data_dir, meta):
    conn = open_db(os.path.join(data_dir, "opencode.db"))
    try:
        messages = [
            {"id": row["id"], "time_created": row["time_created"],
             "data": parse_json(row["data"])}
            for row in conn.execute(
                "SELECT id, time_created, data FROM message WHERE session_id=? "
                "ORDER BY time_created, id", (meta["session_id"],))
        ]
        parts_by_message = {}
        for row in conn.execute(
                "SELECT id, message_id, time_created, data FROM part WHERE session_id=? "
                "ORDER BY time_created, id", (meta["session_id"],)):
            parts_by_message.setdefault(row["message_id"], []).append(
                {"id": row["id"], "time_created": row["time_created"],
                 "data": parse_json(row["data"])})
    finally:
        conn.close()
    return normalize_records(messages, parts_by_message)


def load_legacy_session(data_dir, meta):
    """旧版 storage/message/<sid>/*.json + storage/part/<mid>/*.json（与 resume 一致）。"""
    sid = meta["session_id"]
    msg_root = os.path.join(data_dir, "storage", "message", sid)
    messages = []
    parts_by_message = {}
    if os.path.isdir(msg_root):
        for name in sorted(os.listdir(msg_root)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(msg_root, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            mid = data.get("id") or os.path.splitext(name)[0]
            messages.append({"id": mid,
                             "time_created": (data.get("time") or {}).get("created"),
                             "data": data})
            part_root = os.path.join(data_dir, "storage", "part", mid)
            if os.path.isdir(part_root):
                for pname in sorted(os.listdir(part_root)):
                    if not pname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(part_root, pname), "r", encoding="utf-8") as f:
                            pdata = json.load(f)
                    except (OSError, ValueError):
                        continue
                    parts_by_message.setdefault(mid, []).append(
                        {"id": pdata.get("id") or os.path.splitext(pname)[0],
                         "time_created": os.stat(path).st_mtime_ns // 1_000_000,
                         "data": pdata})
    messages.sort(key=lambda item: to_ms(
        (item["data"].get("time") or {}).get("created") or item.get("time_created")))
    return normalize_records(messages, parts_by_message)


def load_any(data_dir, meta):
    normz = (load_db_session(data_dir, meta) if meta["source"] == "sqlite"
             else load_legacy_session(data_dir, meta))
    return normz


# ---------------------------------------------------------------- 对外接口

def list_sessions(dir_=None, project=None):
    data_dir = dir_ or default_dir()
    sessions = scan_sessions(data_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    out = []
    for s in sessions[:50]:
        try:
            msgs = items_to_msgs(load_any(data_dir, s)["items"])
        except (OSError, ValueError, sqlite3.Error):
            msgs = []
        out.append({
            "session_id": s["session_id"], "title": s["title"],
            "cwd": s["directory"], "mtime": (to_ms(s["updated"]) or 0) / 1000,
            "path": s["path"], "count": len(msgs),
            "last_ts": cs.fmt_cst(msgs[-1]["ts"]) if msgs else "",
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    data_dir = dir_ or default_dir()
    sessions = scan_sessions(data_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 OpenCode 会话（dir={data_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    normz = load_any(data_dir, target)
    msgs = items_to_msgs(normz["items"])
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    timestamps = [it["timestamp"] for it in normz["items"] if it.get("timestamp")]
    started_at = iso_from_ms(timestamps[0]) or iso_from_ms(to_ms(target["created"]))
    ended_at = iso_from_ms(timestamps[-1]) or iso_from_ms(to_ms(target["updated"]))
    ref = cs.make_ref("opencode", target["session_id"], target["title"],
                      target["directory"] or "",
                      model=(normz["provider"] + "/" if normz["provider"] else "") + normz["model"],
                      started_at=started_at, ended_at=ended_at,
                      source=target["path"])
    return ref, msgs
