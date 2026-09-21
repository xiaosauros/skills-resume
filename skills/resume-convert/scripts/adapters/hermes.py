#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/hermes: Hermes Agent（<hermes_home>/state.db SQLite）-> 归一化会话。

格式依据（与 resume-hermes 一致）：
  HERMES_HOME（默认 %LOCALAPPDATA%\\hermes、~/.hermes）下 state.db，表 sessions + messages。
  - sessions: id/source/model/model_config/parent_session_id/started_at/ended_at/
    end_reason/cwd/git_branch/git_repo_root/title/display_name/archived 等
  - messages: id/session_id/role/content/tool_call_id/tool_calls/tool_name/timestamp
    （可选 active/compacted/_compressed_summary）
  - role ∈ session_meta（跳过）/user/assistant/tool/system
  - assistant.tool_calls 为 JSON 数组（{id, function:{name, arguments}} 或平铺），
    arguments 可为 JSON 串或对象
  - tool.content 通常为 {"output":..., "exit_code":...} 或 {"error":...}
  - 压缩交接摘要（[CONTEXT COMPACTION 前缀 / _compressed_summary 标记）-> 转换注记
  - 会话链：parent_session_id + 父 end_reason='compression' 续链；branched/delegate 剔除
标题：display_name 优先于 title；压缩续链取链尾标题。
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

# 压缩交接摘要的识别前缀（含历史版本），见 agent/context_compressor.py 的 SUMMARY_PREFIX
COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")
SUMMARY_PREFIX_CUT = "avoid repeating it:"
SUMMARY_END_MARKER_RE = re.compile(r"^--- END OF CONTEXT SUMMARY[^\n]*---\s*$", re.MULTILINE)


def default_dir():
    # 与 resume_hermes.default_hermes_dir 一致
    override = os.environ.get("HERMES_HOME")
    if override:
        return override
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            os.path.join("~", "AppData", "Local"))
        return os.path.join(local, "hermes")
    return os.path.expanduser(os.path.join("~", ".hermes"))


def db_path(data_dir):
    return os.path.join(data_dir, "state.db")


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def iso_z(seconds):
    """epoch 秒 -> 毫秒精度 UTC 'Z' 结尾 ISO（与 Node toISOString 一致）；<=0 返回 ''。"""
    if not seconds or seconds <= 0:
        return ""
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


# ---------------------------------------------------------------- SQLite 访问

def open_db(db_file):
    uri = Path(db_file).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def fetch_rows(conn, table, wanted):
    # 按实际存在的列取数，兼容新旧 schema 的列差异
    available = table_columns(conn, table)
    cols = [c for c in wanted if c in available]
    return [dict(row) for row in conn.execute("SELECT %s FROM %s" % (", ".join(cols), table))]


def fallback_time(session):
    return float(session.get("ended_at") or session.get("started_at") or 0)


def build_entry(session, last_active):
    return {
        "id": session["id"],
        "session": session,
        "model_config": parse_json(session.get("model_config")),
        "parent_session_id": session.get("parent_session_id") or "",
        "started_at": float(session.get("started_at") or 0),
        "end_reason": session.get("end_reason") or "",
        "title": (session.get("display_name") or session.get("title") or "").strip(),
        "last_active": last_active.get(session["id"]) or fallback_time(session),
    }


def scan_entries(data_dir):
    file = db_path(data_dir)
    if not os.path.isfile(file):
        return []
    try:
        conn = open_db(file)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"sessions", "messages"}.issubset(tables):
                return []
            sessions = fetch_rows(conn, "sessions", [
                "id", "source", "session_key", "chat_type", "model", "model_config",
                "parent_session_id", "started_at", "ended_at", "end_reason",
                "message_count", "tool_call_count", "cwd", "git_branch", "git_repo_root",
                "title", "display_name", "archived",
            ])
            last_active = {}
            for row in conn.execute("SELECT session_id, MAX(timestamp) AS ts FROM messages GROUP BY session_id"):
                last_active[row["session_id"]] = row["ts"] or 0
            return [build_entry(session, last_active) for session in sessions]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return []


def load_messages(data_dir, chain_ids):
    """按链上会话 ID 取未压缩的活跃消息，段内按 id 排序。"""
    conn = open_db(db_path(data_dir))
    try:
        available = table_columns(conn, "messages")
        optional = [c for c in ("active", "compacted", "_compressed_summary") if c in available]
        cols = ", ".join(["id", "session_id", "role", "content", "tool_call_id", "tool_calls",
                          "tool_name", "timestamp", "finish_reason"] + optional)
        rows = []
        for session_id in chain_ids:
            where = ["session_id = ?"]
            if "active" in optional:
                where.append("(active = 1 OR active IS NULL)")
            if "compacted" in optional:
                where.append("(compacted = 0 OR compacted IS NULL)")
            rows.extend(dict(row) for row in conn.execute(
                "SELECT %s FROM messages WHERE %s ORDER BY id" % (cols, " AND ".join(where)),
                (session_id,)))
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------- 行解析

def is_summary_row(row):
    if row.get("_compressed_summary"):
        return True
    content = (row.get("content") or "").lstrip()
    return any(content.startswith(prefix) for prefix in COMPACTION_PREFIXES)


def clean_summary_body(content):
    body = content.strip()
    cut = body.find(SUMMARY_PREFIX_CUT)
    if cut != -1:
        body = body[cut + len(SUMMARY_PREFIX_CUT):]
    else:
        for prefix in COMPACTION_PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
    body = SUMMARY_END_MARKER_RE.sub("", body)
    return body.strip()


def tool_result_content(raw):
    """tool 消息的 content 通常是 JSON（{"output":..., "exit_code":...} 等），抽取可读文本。"""
    data = parse_json(raw)
    if isinstance(data, dict) and data:
        if "output" in data:
            output = data["output"]
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            error = data.get("error")
            exit_code = data.get("exit_code")
            is_error = bool(error) or (isinstance(exit_code, int) and exit_code not in (0, None))
            if error:
                text = ("%s\n[error] %s" % (text, error)) if text else str(error)
            return text, is_error
        if "error" in data:
            return str(data["error"]), True
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")), False
    return str(raw or ""), False


def normalize_rows(rows):
    """messages 行 -> 归一化 msgs；(timestamp, 行序) 稳定排序，严格保持源记录顺序。"""
    items = []
    for order, row in enumerate(rows):
        role = row.get("role") or ""
        content = row.get("content")
        seconds = float(row.get("timestamp") or 0)
        ts = iso_z(seconds)
        if role == "session_meta":
            continue
        if is_summary_row(row):
            body = clean_summary_body(content or "")
            if body:
                items.append((seconds, order, cs.m_note("历史压缩摘要：" + body, ts)))
            continue
        if role == "user":
            text = (content or "").strip()
            if text:
                items.append((seconds, order, cs.m_message("user", text, ts)))
        elif role == "assistant":
            text = (content or "").strip()
            if text:
                items.append((seconds, order, cs.m_message("assistant", text, ts)))
            calls = parse_json(row.get("tool_calls"))
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    name = function.get("name") or call.get("name") or "tool"
                    arguments = function.get("arguments", call.get("arguments"))
                    inputs = arguments if isinstance(arguments, dict) else parse_json(arguments)
                    items.append((seconds, order, cs.m_tool_use(
                        call.get("id") or call.get("call_id") or "",
                        str(name), inputs, ts)))
        elif role == "tool":
            text, is_error = tool_result_content(content)
            items.append((seconds, order, cs.m_tool_result(
                row.get("tool_call_id") or "", text, is_error, ts)))
        elif role == "system":
            text = (content or "").strip()
            if text:
                items.append((seconds, order, cs.m_note("系统消息：" + text, ts)))
    items.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in items]


# ---------------------------------------------------------------- 会话链

def project_fields(session):
    # hermes 以 git_repo_root（缺失时退回 cwd）作为会话的项目归属
    values = []
    for key in ("git_repo_root", "cwd"):
        value = (session.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def classify_chains(entries):
    """组织成逻辑会话：根会话（含分支子会话）+ 压缩续链；delegate 子 agent 会话整体剔除。"""
    by_id = {entry["id"]: entry for entry in entries}
    children = {}
    for entry in entries:
        if entry["parent_session_id"]:
            children.setdefault(entry["parent_session_id"], []).append(entry)

    def is_delegate(entry):
        return "_delegate_from" in entry["model_config"]

    def is_branch(entry):
        if "_branched_from" in entry["model_config"]:
            return True
        parent = by_id.get(entry["parent_session_id"])
        return bool(
            parent and parent["end_reason"] == "branched"
            and entry["started_at"] and parent["session"].get("ended_at")
            and entry["started_at"] >= float(parent["session"]["ended_at"])
        )

    def is_compression_child(entry):
        parent = by_id.get(entry["parent_session_id"])
        return bool(parent and parent["end_reason"] == "compression")

    results = []
    for root in entries:
        if root["parent_session_id"] and not is_branch(root):
            continue  # 压缩续链 / delegate 子会话由链首代为呈现
        if is_delegate(root):
            continue
        chain = [root]
        tip = root
        while True:
            # 压缩续链可能因网关竞态出现多条，取最晚启动的一段
            kids = [c for c in children.get(tip["id"], [])
                    if is_compression_child(c) and not is_branch(c) and not is_delegate(c)]
            if not kids:
                break
            tip = max(kids, key=lambda c: (c["started_at"], c["id"]))
            chain.append(tip)
        paths = []
        for item in chain:
            for value in project_fields(item["session"]):
                if value not in paths:
                    paths.append(value)
        titles = [item["title"] for item in (chain[-1], chain[0]) if item["title"]]
        results.append({
            "chain_ids": [item["id"] for item in chain],
            "root": chain[0],
            "tip": chain[-1],
            "paths": paths,
            "title": titles[0] if titles else "",
            "last_active": max(item["last_active"] for item in chain),
        })
    results.sort(key=lambda e: (-e["last_active"], e["root"]["id"]))
    return results


def entry_matches_project(entry, project_path):
    target = norm_path(project_path)
    for value in entry["paths"]:
        normed = norm_path(value)
        if normed == target or (normed and target.startswith(normed + os.sep)):
            return True
    return False


def load_and_normalize(data_dir, chain_ids):
    return normalize_rows(load_messages(data_dir, chain_ids))


def first_user_title(msgs, fallback):
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m.get("text", "").strip():
            return m["text"].strip().splitlines()[0][:60]
    return fallback


# ---------------------------------------------------------------- 对外接口

def list_sessions(dir_=None, project=None):
    data_dir = dir_ or default_dir()
    entries = classify_chains(scan_entries(data_dir))
    if project:
        entries = [e for e in entries if entry_matches_project(e, project)]
    out = []
    for e in entries[:50]:
        title = e["title"]
        count, last_ts = 0, ""
        msgs = []
        try:
            msgs = load_and_normalize(data_dir, e["chain_ids"])
            count = len(msgs)
            last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
        except (cs.ConvertError, OSError, sqlite3.Error):
            msgs = []
        if not title:
            title = first_user_title(msgs, e["root"]["id"])
        out.append({
            "session_id": e["root"]["id"],
            "title": title,
            "cwd": e["paths"][0] if e["paths"] else "",
            "mtime": float(e["last_active"]),
            "path": db_path(data_dir),
            "count": count,
            "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    data_dir = dir_ or default_dir()
    entries = classify_chains(scan_entries(data_dir))
    if project:
        entries = [e for e in entries if entry_matches_project(e, project)]
    if session_id:
        needle = session_id.strip().lower()
        entries = [e for e in entries
                   if any(sid.lower() == needle or sid.lower().startswith(needle)
                          or needle in sid.lower() for sid in e["chain_ids"])]
    if not entries:
        raise cs.ConvertError(
            f"未找到 Hermes 会话（dir={data_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    entry = entries[0]
    msgs = load_and_normalize(data_dir, entry["chain_ids"])
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{db_path(data_dir)}")

    root, tip = entry["root"], entry["tip"]
    root_session, tip_session = root["session"], tip["session"]
    sid = root["id"]
    title = entry["title"] or first_user_title(msgs, sid)
    ended_at = msgs[-1]["ts"] or iso_z(fallback_time(tip_session))
    ref = cs.make_ref("hermes", sid, title,
                      entry["paths"][0] if entry["paths"] else "",
                      model=str(tip_session.get("model") or root_session.get("model") or ""),
                      started_at=iso_z(root["started_at"]),
                      ended_at=ended_at,
                      source=db_path(data_dir))
    return ref, msgs
