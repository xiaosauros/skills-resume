#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 Hermes Agent 本地 SQLite 会话库（<hermes_home>/state.db），生成接管摘要。"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
# 压缩交接摘要的识别前缀（含历史版本），见 agent/context_compressor.py 的 SUMMARY_PREFIX
COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")
SUMMARY_PREFIX_CUT = "avoid repeating it:"
SUMMARY_END_MARKER_RE = re.compile(r"^--- END OF CONTEXT SUMMARY[^\n]*---\s*$", re.MULTILINE)
READ_TOOLS = {
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_search", "web_extract", "vision_analyze", "browser_navigate",
}
EDIT_TOOLS = {"write_file", "patch"}
SHELL_TOOLS = {"terminal", "process", "execute_code"}
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


def default_hermes_dir():
    # 与 hermes_constants.get_hermes_home 的平台默认一致
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def db_path(data_dir):
    return data_dir / "state.db"


def norm_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def sql_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def fmt_time(value):
    if not value:
        return ""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def open_db(db_file):
    uri = db_file.resolve().as_uri() + "?mode=ro"
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


def scan_sessions(data_dir):
    file = db_path(data_dir)
    if not file.is_file():
        return []
    try:
        conn = open_db(file)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"sessions", "messages"}.issubset(tables):
                return []
            messages_cols = table_columns(conn, "messages")
            sessions = fetch_rows(conn, "sessions", [
                "id", "source", "session_key", "chat_type", "model", "model_config",
                "parent_session_id", "started_at", "ended_at", "end_reason",
                "message_count", "tool_call_count", "cwd", "git_branch", "git_repo_root",
                "title", "display_name", "archived",
            ])
            last_active = {}
            for row in conn.execute("SELECT session_id, MAX(timestamp) AS ts FROM messages GROUP BY session_id"):
                last_active[row["session_id"]] = row["ts"] or 0
            active_where = "(active = 1 OR active IS NULL)" if "active" in messages_cols else "1=1"
            active_counts = {}
            for row in conn.execute("SELECT session_id, COUNT(*) AS n FROM messages WHERE %s GROUP BY session_id" % active_where):
                active_counts[row["session_id"]] = row["n"]
            return [build_entry(session, last_active, active_counts) for session in sessions]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return []


def fallback_time(session):
    return float(session.get("ended_at") or session.get("started_at") or 0)


def build_entry(session, last_active, active_counts):
    return {
        "id": session["id"],
        "session": session,
        "source": session.get("source") or "",
        "chat_type": session.get("chat_type") or "",
        "session_key": session.get("session_key") or "",
        "model_config": parse_json(session.get("model_config")),
        "parent_session_id": session.get("parent_session_id") or "",
        "started_at": float(session.get("started_at") or 0),
        "end_reason": session.get("end_reason") or "",
        "title": (session.get("display_name") or session.get("title") or "").strip(),
        "archived": bool(session.get("archived")),
        "last_active": last_active.get(session["id"]) or fallback_time(session),
        "active_messages": active_counts.get(session["id"], 0),
    }


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
            "chain": chain,
            "root": chain[0],
            "tip": chain[-1],
            "chain_ids": [item["id"] for item in chain],
            "chain_length": len(chain),
            "compaction_count": max(len(chain) - 1, sum(1 for item in chain if item["end_reason"] == "compression")),
            "paths": paths,
            "title": titles[0] if titles else "",
            "last_active": max(item["last_active"] for item in chain),
            "active_messages": sum(item["active_messages"] for item in chain),
            "archived": any(item["archived"] for item in chain),
        })
    results.sort(key=lambda e: (-e["last_active"], e["root"]["id"]))
    return results


def load_messages(data_dir, chain_ids):
    conn = open_db(db_path(data_dir))
    try:
        available = table_columns(conn, "messages")
        optional = [c for c in ("active", "compacted", "_compressed_summary") if c in available]
        cols = ", ".join(["id", "session_id", "role", "content", "tool_call_id", "tool_calls",
                          "tool_name", "timestamp", "finish_reason"] + optional)
        compacted_count = 0
        rows = []
        for session_id in chain_ids:
            where = ["session_id = %s" % sql_quote(session_id)]
            if "active" in optional:
                where.append("(active = 1 OR active IS NULL)")
            if "compacted" in optional:
                where.append("(compacted = 0 OR compacted IS NULL)")
            sql = "SELECT %s FROM messages WHERE %s ORDER BY id" % (cols, " AND ".join(where))
            rows.extend(dict(row) for row in conn.execute(sql))
            if "compacted" in optional:
                counted = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE session_id = %s AND compacted = 1" % sql_quote(session_id)
                ).fetchone()
                compacted_count += counted["n"]
        return rows, compacted_count
    finally:
        conn.close()


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


def normalize_messages(rows):
    items = []
    summaries = []
    for row in rows:
        role = row.get("role") or ""
        content = row.get("content")
        seconds = float(row.get("timestamp") or 0)
        timestamp = int(seconds) if seconds.is_integer() else seconds  # 与 JS Number 输出保持一致
        if role == "session_meta":
            continue
        if is_summary_row(row):
            body = clean_summary_body(content or "")
            if body:
                summaries.append({"timestamp": timestamp, "text": body})
            continue
        if role == "user":
            text = (content or "").strip()
            if text:
                items.append({"kind": "user_text", "timestamp": timestamp, "text": text})
        elif role == "assistant":
            text = (content or "").strip()
            if text:
                items.append({"kind": "assistant_text", "timestamp": timestamp, "text": text})
            calls = parse_json(row.get("tool_calls"))
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    name = function.get("name") or call.get("name") or "tool"
                    arguments = function.get("arguments", call.get("arguments"))
                    inputs = arguments if isinstance(arguments, dict) else parse_json(arguments)
                    items.append({
                        "kind": "tool_use",
                        "timestamp": timestamp,
                        "name": str(name),
                        "input": inputs,
                        "tool_use_id": call.get("id") or call.get("call_id") or "",
                    })
        elif role == "tool":
            text, is_error = tool_result_content(content)
            items.append({
                "kind": "tool_result",
                "timestamp": timestamp,
                "tool_use_id": row.get("tool_call_id") or "",
                "name": row.get("tool_name") or "",
                "content": text,
                "is_error": is_error,
            })
        elif role == "system":
            text = (content or "").strip()
            if text:
                items.append({"kind": "system_text", "timestamp": timestamp, "text": text})
    items.sort(key=lambda item: item["timestamp"])
    return items, summaries


def tool_file(name, inputs):
    for key in ("path", "filePath", "file_path", "filename"):
        if inputs.get(key):
            return str(inputs[key])
    if name == "search_files":
        return str(inputs.get("pattern") or inputs.get("query") or "")
    if name == "web_search":
        return str(inputs.get("query") or "")
    if name == "web_extract":
        urls = inputs.get("urls")
        if isinstance(urls, list):
            return " ".join(str(url) for url in urls)
        return str(urls or "")
    if name == "browser_navigate":
        return str(inputs.get("url") or "")
    return ""


def shell_command(inputs):
    for key in ("command", "cmd", "code"):
        if inputs.get(key):
            # execute_code 等工具的代码是多行文本，折叠换行保持列表可读
            return re.sub(r"[\r\n]+", " ; ", str(inputs[key])).strip()
    if inputs.get("action"):
        return ("%s %s" % (inputs["action"], inputs.get("id") or "")).strip()
    return ""


def dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def build_state(items):
    files_read, files_edited, commands, test_results = [], [], [], []
    first_user = last_user = last_assistant = ""
    pending_command = ""
    for item in items:
        kind = item["kind"]
        if kind == "user_text":
            first_user = first_user or item["text"]
            last_user = item["text"]
        elif kind == "assistant_text":
            last_assistant = item["text"]
        elif kind == "tool_use":
            name = str(item.get("name") or "").lower()
            inputs = item.get("input") or {}
            pending_command = shell_command(inputs) if name in SHELL_TOOLS else ""
            if name in READ_TOOLS:
                files_read.append(tool_file(name, inputs))
            elif name in EDIT_TOOLS:
                files_edited.append(tool_file(name, inputs))
            elif name in SHELL_TOOLS:
                if pending_command:
                    commands.append(pending_command)
        elif kind == "tool_result":
            content = item.get("content") or ""
            name = str(item.get("name") or "").lower()
            if (item.get("is_error") or TEST_CMD_RE.search(pending_command) or TEST_RESULT_RE.search(content[:2000])) and content.strip():
                test_results.append({"command_hint": pending_command or name, "is_error": bool(item.get("is_error")), "content": content})
    return {
        "goal": first_user,
        "files_read": dedupe(files_read),
        "files_edited": dedupe(files_edited),
        "commands": dedupe(commands),
        "test_results": test_results,
        "last_user": last_user,
        "last_assistant": last_assistant,
    }


def truncate(value, limit):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def text_block(value, limit):
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "\n…（已截断，原长 %d 字符）" % len(text)
    return text


def tool_brief(item):
    name = item.get("name") or "tool"
    inputs = item.get("input") or {}
    detail = (shell_command(inputs) or tool_file(str(name).lower(), inputs)
              or str(inputs.get("url") or inputs.get("query") or inputs.get("pattern") or ""))
    return "%s(%s)" % (name, truncate(detail, 100)) if detail else "%s(...)" % name


def render_item(item, max_chars):
    ts = fmt_time(item.get("timestamp"))
    kind = item["kind"]
    if kind == "user_text":
        return ["### [用户] %s" % ts, text_block(item["text"], max_chars)]
    if kind == "assistant_text":
        return ["### [助手] %s" % ts, text_block(item["text"], max_chars)]
    if kind == "system_text":
        return ["### [系统] %s" % ts, text_block(item["text"], max_chars)]
    if kind == "tool_use":
        payload = json.dumps(item.get("input") or {}, ensure_ascii=False, separators=(",", ":"))
        return ["### [工具调用] %s %s" % (item.get("name") or "tool", ts), "```json", truncate(payload, max_chars), "```"]
    if kind == "tool_result":
        tag = " (错误)" if item.get("is_error") else ""
        return ["### [工具结果]%s %s" % (tag, ts), text_block(item.get("content"), max_chars)]
    return []


def build_info(entry, summaries, compacted_rows, data_dir):
    root, tip = entry["root"], entry["tip"]
    root_session, tip_session = root["session"], tip["session"]
    paths = entry["paths"]
    timestamps = []
    items = entry.get("_items") or []
    for item in items:
        if item.get("timestamp"):
            timestamps.append(item["timestamp"])
    source = root["source"]
    return {
        "session_id": root["id"],
        "tip_id": tip["id"],
        "chain_length": entry["chain_length"],
        "compaction_count": entry["compaction_count"],
        "title": entry["title"] or root["id"],
        "source": source,
        "chat_type": root["chat_type"],
        "session_key": root["session_key"],
        "parent_session_id": root["parent_session_id"],
        "project": paths[0] if paths else "",
        "cwd": root_session.get("cwd") or "",
        "git_branch": (tip_session.get("git_branch") or root_session.get("git_branch") or ""),
        "git_repo_root": root_session.get("git_repo_root") or "",
        "model": (tip_session.get("model") or root_session.get("model") or ""),
        "archived": entry["archived"],
        "end_reason": tip["end_reason"],
        "started": fmt_time(root["started_at"]),
        "first_ts": fmt_time(timestamps[0]) if timestamps else fmt_time(root["started_at"]),
        "last_ts": fmt_time(timestamps[-1]) if timestamps else fmt_time(entry["last_active"]),
        "message_count": entry["active_messages"],
        "item_count": len(items),
        "compaction_summaries": len(summaries),
        "compacted_rows": compacted_rows,
        "hermes_home": str(data_dir),
        "db_path": str(db_path(data_dir)),
    }


def render_summary(info, state, summaries, recent_items, older_items, max_chars):
    lines = [
        "# Resume-Hermes 会话接管摘要",
        "",
        "## 会话信息",
        "- 标题: %s" % info["title"],
        "- 根会话ID: %s" % info["session_id"],
    ]
    if info["tip_id"] != info["session_id"]:
        lines.append("- 当前会话ID: %s" % info["tip_id"])
    if info["chain_length"] > 1:
        lines.append("- 会话链: 共 %d 段（%d 次压缩续链）" % (info["chain_length"], info["compaction_count"]))
    source = info["source"] or "(未知)"
    if info["chat_type"]:
        source += "（%s）" % info["chat_type"]
    lines.extend([
        "- 来源: %s" % source,
        "- 项目: %s" % (info["project"] or "(未知)"),
    ])
    if info["git_branch"]:
        lines.append("- Git 分支: %s" % info["git_branch"])
    if info["model"]:
        lines.append("- 模型: %s" % info["model"])
    lines.extend([
        "- 时间范围: %s ~ %s" % (info["first_ts"] or "(无)", info["last_ts"] or "(无)"),
        "- 消息条目数: %d" % info["item_count"],
    ])
    if info["compacted_rows"]:
        lines.append("- 已压缩原始消息: %d 条（不再展开）" % info["compacted_rows"])
    if info["archived"]:
        lines.append("- 状态: 已归档")
    lines.append("")
    if summaries:
        lines.extend(["## 历史摘要（原会话压缩交接，共 %d 份）" % len(summaries), ""])
        for index, summary in enumerate(summaries, 1):
            lines.extend([
                "### 摘要 %d · %s" % (index, fmt_time(summary["timestamp"]) or "(无时间)"),
                text_block(summary["text"], max_chars),
                "",
            ])
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    for title, key in (("已调查文件", "files_read"), ("代码修改", "files_edited"), ("执行命令", "commands")):
        if state[key]:
            lines.append("### %s" % title)
            lines.extend("- %s" % truncate(value, 200) for value in state[key])
            lines.append("")
    if state["test_results"]:
        lines.extend(["### 测试 / 错误结果", ""])
        for result in state["test_results"][-5:]:
            tag = " [错误]" if result["is_error"] else ""
            first = (result["content"].strip().splitlines() or [""])[0]
            lines.append("-%s %s" % (tag, truncate(first, 200)))
        lines.append("")
    lines.extend([
        "### 最近用户消息",
        text_block(state["last_user"], max_chars) or "(无)",
        "",
        "### 最近助手消息",
        text_block(state["last_assistant"], max_chars) or "(无)",
        "",
    ])
    lines.extend(["## 近期对话（最近 %d 条）" % len(recent_items), ""])
    for item in recent_items:
        lines.extend(render_item(item, max_chars))
        lines.append("")
    if older_items:
        lines.extend(["## 更早活动（工具调用，仅最近 60 条）", ""])
        for item in older_items[-60:]:
            lines.append("- [%s] %s" % (fmt_time(item.get("timestamp")), tool_brief(item)))
        lines.append("")
    lines.extend([
        "## 接管建议",
        "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。",
        "- 「历史摘要」中的压缩交接内容仅作背景参考：hermes 约定以最后一条真实用户消息为准，勿把历史待办当作当前任务。",
        "- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续；不要逐字复述历史。",
        "- 如需回到 Hermes 原生环境继续，可运行 `hermes --resume <会话ID>`。",
        "",
    ])
    return "\n".join(lines)


def entry_matches_project(entry, project_path):
    target = norm_path(project_path)
    for value in entry["paths"]:
        normed = norm_path(value)
        if normed == target or (normed and target.startswith(normed + os.sep)):
            return True
    return False


def project_entries(entries, project_path):
    return [entry for entry in entries if entry_matches_project(entry, project_path)]


def pick_entry(entries, session_arg, project_path):
    if session_arg:
        needle = session_arg.strip().lower()
        for entry in entries:
            for session_id in entry["chain_ids"]:
                if session_id.lower() == needle or session_id.lower().startswith(needle) or needle in session_id.lower():
                    return entry
        return None
    for entry in entries:
        if entry_matches_project(entry, project_path):
            return entry
    return None


def entry_title_line(entry):
    title = entry["title"] or entry["root"]["id"]
    chain_tag = "（%d 次压缩续链）" % (entry["chain_length"] - 1) if entry["chain_length"] > 1 else ""
    archived_tag = "（已归档）" if entry["archived"] else ""
    return "%s%s%s" % (truncate(title, 60), chain_tag, archived_tag)


def print_list(entries, project_path, limit):
    selected = project_entries(entries, project_path)
    shown = selected[:limit] if limit > 0 else selected
    print("当前项目: %s" % project_path)
    suffix = "（仅显示最近 %d 个）" % len(shown) if len(shown) < len(selected) else ""
    print("找到 %d 个会话%s：\n" % (len(selected), suffix))
    for index, entry in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        source = entry["root"]["source"] or "?"
        project = entry["paths"][0] if entry["paths"] else "(无项目路径)"
        print("%s %s  %s  [%s] %s条  项目: %s  标题: %s" % (
            mark, fmt_time(entry["last_active"]) or "(无时间)", entry["root"]["id"],
            source, entry["tip"]["active_messages"], truncate(project, 40), entry_title_line(entry)))


def print_list_all(entries, limit):
    shown = entries[:limit] if limit > 0 else entries
    suffix = "（仅显示最近 %d 个）" % len(shown) if len(shown) < len(entries) else ""
    print("共 %d 个会话%s：\n" % (len(entries), suffix))
    for index, entry in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        source = entry["root"]["source"] or "?"
        project = entry["paths"][0] if entry["paths"] else "(无项目路径)"
        print("%s %s  %s  [%s] %s条  项目: %s  标题: %s" % (
            mark, fmt_time(entry["last_active"]) or "(无时间)", entry["root"]["id"],
            source, entry["tip"]["active_messages"], truncate(project, 40), entry_title_line(entry)))


def parse_args():
    parser = argparse.ArgumentParser(description="读取 Hermes Agent 本地会话，生成结构化接管摘要。")
    parser.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    parser.add_argument("--list-all", action="store_true", help="列出全部会话（含无项目路径的网关/IM 会话）")
    parser.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", help="指定会话 ID 或前缀；匹配压缩续链上任意一段；跨项目查找")
    parser.add_argument("--project", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--hermes-dir", help="Hermes 主目录（默认 HERMES_HOME 或 %%LOCALAPPDATA%%\\hermes、~/.hermes）")
    parser.add_argument("--recent", type=int, default=8, help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, help="单条截断长度")
    parser.add_argument("--limit", type=int, default=0, help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", help="将摘要写入文件")
    return parser.parse_args()


def main():
    setup_utf8_stdio()
    args = parse_args()
    data_dir = Path(args.hermes_dir).expanduser() if args.hermes_dir else default_hermes_dir()
    entries = classify_chains(scan_sessions(data_dir))
    if not entries:
        raise SystemExit("错误：在 %s 未找到 Hermes 会话（已检查 state.db）。" % data_dir)
    project_path = os.path.abspath(args.project)
    if args.list:
        if not project_entries(entries, project_path):
            raise SystemExit("错误：未找到项目 %s 的 Hermes 会话。可用 --list-all 查看全部，或 --session ID 跨项目查找。" % project_path)
        print_list(entries, project_path, args.limit)
        return
    if args.list_all:
        print_list_all(entries, args.limit)
        return
    entry = pick_entry(entries, args.session, project_path)
    if not entry:
        raise SystemExit("错误：未匹配到会话 '%s'。" % (args.session or "当前项目"))
    rows, compacted_rows = load_messages(data_dir, entry["chain_ids"])
    items, summaries = normalize_messages(rows)
    entry["_items"] = items
    state = build_state(items)
    info = build_info(entry, summaries, compacted_rows, data_dir)
    recent_count = max(args.recent, 0)
    recent_items = items[-recent_count:] if recent_count else []
    older_items = [item for item in (items[:-recent_count] if recent_count else items) if item["kind"] == "tool_use"]
    if args.json:
        output = json.dumps(
            {"info": info, "state": state, "summaries": summaries, "recent_items": recent_items},
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = render_summary(info, state, summaries, recent_items, older_items, max(args.max_chars, 1))
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8", newline="\n")
        print("摘要已写入：%s" % args.output, file=sys.stderr)
    else:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
