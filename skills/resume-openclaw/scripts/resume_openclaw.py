#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 OpenClaw（原 Clawdbot/Moltbot）本地会话，生成结构化「接管摘要」。

与同目录 resume_openclaw.js 功能等价、输出可互换。用法见同目录 SKILL.md。

OpenClaw 会话存储（状态目录：OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot）：
  - 当前版本（每 agent SQLite）：agents/<agentId>/agent/openclaw-agent.sqlite
      · session_nodes：session_key 主键，entry_json 为会话元数据（SessionEntry）
      · session_windows：session_id <-> session_key 映射
      · transcript_events：(session_id, seq) -> event_json（与 JSONL 行同构的条目树）
      · session_transcript_active_events：活动分支投影（active_position -> event_seq）
  - 旧版（JSON）：agents/<agentId>/sessions/sessions.json（sessionKey -> SessionEntry 对象）
      与根级 sessions/sessions.json；会话正文为 sessions/<sessionId>.jsonl：
      首行 {type:"session",cwd,...} 头，其后为 {type,id,parentId,timestamp,...} 条目
  - 条目 message.message 为 {role:"user"|"assistant"|"toolResult", content, ...}，
    compaction 条目含 summary 与 firstKeptEntryId（其前内容已被压缩为摘要）
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
READ_TOOLS = {"read", "find", "grep", "ls", "glob", "codebase"}
EDIT_TOOLS = {"write", "edit", "apply_patch", "applypatch", "multiedit"}
SHELL_TOOLS = {"bash", "exec", "process", "terminal", "shell"}
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
READ_HINT_RE = re.compile(r"read|grep|search|glob|list|\bls\b|view|fetch|find|web|docs?", re.IGNORECASE)
EDIT_HINT_RE = re.compile(r"edit|write|create|apply|patch|replace|insert|generate", re.IGNORECASE)
SHELL_HINT_RE = re.compile(r"terminal|command|exec|shell|bash|\brun\b|process", re.IGNORECASE)


def setup_utf8_stdio():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")


def expand_tilde(value):
    if not value:
        return value
    return os.path.expanduser(str(value))


def is_dir(path) -> bool:
    return os.path.isdir(path)


def is_file(path) -> bool:
    return os.path.isfile(path)


def norm_path(value) -> str:
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def parse_json(value, fallback=None):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {} if fallback is None else fallback


def timestamp_ms(value) -> int:
    if value is None or value == "":
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    if number is not None:
        return int(round(number * 1000 if abs(number) < 1e10 else number))
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def fmt_time(value) -> str:
    ms = timestamp_ms(value)
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=CST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def truncate(value, limit) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def text_block(value, limit) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def dedupe(values):
    seen, output = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def read_text_file(path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


# ---------- 状态目录与会话源定位 ----------

def state_dir_candidates(openclaw_dir):
    """状态目录候选：OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot（与 src/config/state-dir.ts 一致）。"""
    if openclaw_dir:
        return [Path(expand_tilde(openclaw_dir))]
    candidates = []
    env_dir = os.environ.get("OPENCLAW_STATE_DIR")
    if env_dir and env_dir.strip():
        candidates.append(Path(expand_tilde(env_dir.strip())))
    candidates.append(Path.home() / ".openclaw")
    candidates.append(Path.home() / ".clawdbot")
    seen, result = set(), []
    for candidate in candidates:
        key = norm_path(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def collect_sources(state_dir: Path):
    """会话源：每 agent 的 SQLite 库 + 每 agent 旧版 sessions.json + 根级旧版 sessions.json。"""
    sources = []
    agents_root = state_dir / "agents"
    if agents_root.is_dir():
        try:
            agent_ids = sorted(p.name for p in agents_root.iterdir() if p.is_dir())
        except OSError:
            agent_ids = []
        for agent_id in agent_ids:
            agent_dir = agents_root / agent_id
            sqlite_path = agent_dir / "agent" / "openclaw-agent.sqlite"
            if sqlite_path.is_file():
                sources.append({"kind": "sqlite", "agent_id": agent_id, "path": sqlite_path})
            legacy_store = agent_dir / "sessions" / "sessions.json"
            if legacy_store.is_file():
                sources.append({"kind": "legacy", "agent_id": agent_id, "path": legacy_store,
                                "sessions_dir": legacy_store.parent})
    root_legacy = state_dir / "sessions" / "sessions.json"
    if root_legacy.is_file():
        sources.append({"kind": "legacy", "agent_id": "", "path": root_legacy,
                        "sessions_dir": root_legacy.parent})
    return sources


# ---------- 会话元数据 ----------

def entry_title(entry) -> str:
    for key in ("label", "displayName", "subject", "autoLabel"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def entry_directory(entry) -> str:
    worktree = entry.get("worktree") if isinstance(entry.get("worktree"), dict) else {}
    baseline = entry.get("sessionDiffBaseline") if isinstance(entry.get("sessionDiffBaseline"), dict) else {}
    for value in (entry.get("spawnedCwd"), entry.get("execCwd"), worktree.get("repoRoot"), baseline.get("root")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def legacy_store_entries(source):
    """sessions.json 为对象映射 sessionKey -> SessionEntry（与 legacy-store-inspection.ts 一致）。"""
    data = parse_json(read_text_file(source["path"]), None)
    if not isinstance(data, dict):
        return []
    entries = []
    for session_key, value in data.items():
        if isinstance(value, dict) and value.get("sessionId"):
            entries.append((session_key, value))
    return entries


def entry_meta(entry, session_id, session_key, source, created=0, updated=0,
               row_label="", row_status="", archived=0, pinned=0):
    worktree = entry.get("worktree") if isinstance(entry.get("worktree"), dict) else {}
    return {
        "session_id": session_id,
        "session_key": session_key,
        "agent_id": source["agent_id"],
        "title": entry_title(entry) or row_label,
        "directory": entry_directory(entry),
        "model_provider": str(entry.get("modelProvider") or ""),
        "model": str(entry.get("modelOverride") or entry.get("model") or ""),
        "chat_type": str(entry.get("chatType") or ""),
        "status": row_status or str(entry.get("status") or ""),
        "created": created or timestamp_ms(entry.get("createdAt")),
        "updated": max(updated or 0, timestamp_ms(entry.get("updatedAt"))),
        "archived": bool(archived or entry.get("archivedAt")),
        "pinned": bool(pinned or entry.get("pinnedAt")),
        "source_kind": source["kind"],
        "source": f"{source['kind']}:{source['path']}",
        "sqlite_path": source["path"] if source["kind"] == "sqlite" else "",
        "transcript_path": "",
        "tokens": {
            "input": int(entry.get("inputTokens") or 0),
            "output": int(entry.get("outputTokens") or 0),
            "total": int(entry.get("totalTokens") or 0),
        },
        "cost": float(entry.get("estimatedCostUsd") or 0),
        "goal": entry.get("goal") if isinstance(entry.get("goal"), dict) else None,
        "compaction_count": int(entry.get("compactionCount") or 0),
        "_checkpoints": entry.get("compactionCheckpoints") if isinstance(entry.get("compactionCheckpoints"), list) else [],
    }


def sqlite_nodes(source):
    try:
        conn = sqlite3.connect(f"file:{source['path']}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"错误：无法打开 SQLite 库 {source['path']}：{exc}", file=sys.stderr)
        sys.exit(1)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT session_key, current_session_id, entry_json, label, display_name,"
                " created_at, updated_at, last_activity_at, archived_at, pinned_at, status,"
                " parent_session_key, spawned_by, project_id FROM session_nodes"
            ).fetchall()
        except sqlite3.OperationalError:
            try:
                rows = conn.execute(
                    "SELECT session_key, current_session_id, entry_json, updated_at FROM session_nodes"
                ).fetchall()
            except sqlite3.Error:
                rows = []
        except sqlite3.Error:
            rows = []
        result = []
        for row in rows:
            keys = row.keys()
            result.append({
                "session_key": row["session_key"] or "",
                "current_session_id": row["current_session_id"] or "",
                "entry": parse_json(row["entry_json"], {}),
                "row_label": (row["label"] or "") if "label" in keys else "",
                "row_display_name": (row["display_name"] or "") if "display_name" in keys else "",
                "created_at": (row["created_at"] or 0) if "created_at" in keys else 0,
                "updated_at": (row["updated_at"] or 0) if "updated_at" in keys else 0,
                "last_activity_at": (row["last_activity_at"] or 0) if "last_activity_at" in keys else 0,
                "archived_at": (row["archived_at"] or 0) if "archived_at" in keys else 0,
                "pinned_at": (row["pinned_at"] or 0) if "pinned_at" in keys else 0,
                "row_status": (row["status"] or "") if "status" in keys else "",
            })
        return result
    finally:
        conn.close()


def scan_sessions(state_dirs):
    sessions = []
    seen_ids = set()
    for state_dir in state_dirs:
        for source in collect_sources(Path(state_dir)):
            if source["kind"] == "sqlite":
                for node in sqlite_nodes(source):
                    entry = node["entry"]
                    session_id = str(entry.get("sessionId") or node["current_session_id"] or "")
                    if not session_id or session_id in seen_ids:
                        continue
                    seen_ids.add(session_id)
                    meta = entry_meta(
                        entry, session_id, node["session_key"], source,
                        created=node["created_at"] or timestamp_ms(entry.get("createdAt")),
                        updated=max(node["updated_at"] or 0, node["last_activity_at"] or 0)
                        or timestamp_ms(entry.get("updatedAt")),
                        row_label=node["row_label"] or node["row_display_name"],
                        row_status=node["row_status"],
                        archived=node["archived_at"], pinned=node["pinned_at"],
                    )
                    sessions.append(meta)
            else:
                for session_key, entry in legacy_store_entries(source):
                    session_id = str(entry.get("sessionId") or "")
                    if not session_id or session_id in seen_ids:
                        continue
                    # 正文 JSONL 与 sessions.json 同目录（resolveSessionFilePathCore 的相对路径约定）
                    transcript = source["sessions_dir"] / f"{session_id}.jsonl"
                    seen_ids.add(session_id)
                    meta = entry_meta(entry, session_id, session_key, source,
                                      updated=timestamp_ms(entry.get("updatedAt")) or file_mtime(transcript))
                    meta["directory"] = meta["directory"] or jsonl_header_cwd(transcript)
                    meta["transcript_path"] = str(transcript)
                    sessions.append(meta)
    sessions.sort(key=lambda meta: (meta["updated"] or meta["created"]), reverse=True)
    return sessions


def file_mtime(path) -> int:
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return 0


def jsonl_header_cwd(transcript_path) -> str:
    """JSONL 首行会话头 {type:"session", id, timestamp, cwd}。"""
    for line in read_text_file(transcript_path).splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        parsed = parse_json(trimmed, None)
        if isinstance(parsed, dict) and parsed.get("type") == "session":
            return str(parsed.get("cwd") or "")
    return ""


# ---------- 正文加载 ----------

def sqlite_events(sqlite_path, session_id):
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"错误：无法打开 SQLite 库 {sqlite_path}：{exc}", file=sys.stderr)
        sys.exit(1)
    try:
        # 优先读活动分支投影；无投影或表不存在时回退为按 seq 全量
        try:
            rows = conn.execute(
                "SELECT te.event_json FROM session_transcript_active_events ae"
                " JOIN transcript_events te ON te.session_id = ae.session_id AND te.seq = ae.event_seq"
                " WHERE ae.session_id = ? ORDER BY ae.active_position",
                (session_id,),
            ).fetchall()
            return [parsed for parsed in (parse_json(row[0], None) for row in rows) if parsed]
        except sqlite3.OperationalError:
            pass
        rows = conn.execute(
            "SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [parsed for parsed in (parse_json(row[0], None) for row in rows) if parsed]
    except sqlite3.Error as exc:
        print(f"错误：读取会话 {session_id} 的 transcript_events 失败：{exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def jsonl_branch(transcript_path):
    """JSONL：条目树（parentId + leaf 指针）。存在 leaf 时沿最后一条 leaf 的 targetId
    回溯父链得到当前分支；指针失效时回退为文件顺序。"""
    entries = []
    header = None
    for line in read_text_file(transcript_path).splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        parsed = parse_json(trimmed, None)
        if not isinstance(parsed, dict):
            continue
        if header is None and parsed.get("type") == "session":
            header = parsed
            continue
        if not isinstance(parsed.get("type"), str):
            continue
        entries.append(parsed)
    by_id = {entry["id"]: entry for entry in entries if entry.get("id")}
    leaves = [entry for entry in entries if entry.get("type") == "leaf"]
    if leaves:
        branch = []
        cursor = leaves[-1].get("targetId")
        guard = set()
        while cursor and cursor in by_id and cursor not in guard:
            guard.add(cursor)
            entry = by_id[cursor]
            branch.append(entry)
            cursor = entry.get("parentId")
        branch.reverse()
        if branch:
            return branch
    return entries


def message_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image":
                parts.append("[图片]")
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(p for p in parts if p)


def normalize_entries(entries):
    """条目归一化：消息 -> user/assistant 文本与工具调用；compaction/branch_summary -> 摘要。
    摘要单独呈现于「历史摘要」，条目全量保留（近期对话取尾部窗口，其余进「更早活动」）。"""
    items, summaries = [], []
    for entry in entries:
        entry_type = entry.get("type") or ""
        ts = timestamp_ms(entry.get("timestamp"))
        if entry_type == "message":
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            role = message.get("role") or ""
            if role == "user":
                text = message_content_text(message.get("content")).strip()
                if text:
                    items.append({"kind": "user_text", "timestamp": ts, "text": text})
            elif role == "assistant":
                blocks = message.get("content") if isinstance(message.get("content"), list) else []
                text = "\n".join(
                    str(block.get("text") or "") for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
                if text:
                    items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    raw_input = block.get("arguments")
                    if not isinstance(raw_input, dict):
                        raw_input = {} if raw_input in (None, "") else {"raw": raw_input}
                    items.append({
                        "kind": "tool_use", "timestamp": ts,
                        "name": str(block.get("name") or "tool"),
                        "input": raw_input,
                        "tool_use_id": str(block.get("id") or ""),
                    })
                if message.get("stopReason") == "error" and message.get("errorMessage"):
                    items.append({"kind": "error_text", "timestamp": ts, "text": str(message["errorMessage"])})
            elif role == "toolResult":
                items.append({
                    "kind": "tool_result", "timestamp": ts,
                    "tool_use_id": str(message.get("toolCallId") or ""),
                    "content": message_content_text(message.get("content")),
                    "is_error": bool(message.get("isError")),
                })
        elif entry_type == "compaction":
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append({"summary": summary.strip(),
                                  "first_kept_entry_id": str(entry.get("firstKeptEntryId") or ""),
                                  "timestamp": ts})
        elif entry_type == "branch_summary":
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append({"summary": summary.strip(),
                                  "first_kept_entry_id": str(entry.get("fromId") or ""),
                                  "timestamp": ts})
        elif entry_type == "custom_message":
            text = message_content_text(entry.get("content")).strip()
            if entry.get("display") is not False and text:
                items.append({"kind": "extension_text", "timestamp": ts, "text": text})
        # thinking 块、label/session_info/reset/model_change/thinking_level_change/custom/leaf 等不进入接管摘要
    return {"items": items, "summaries": summaries}


def load_session_full(meta):
    if meta["source_kind"] == "sqlite":
        entries = sqlite_events(meta["sqlite_path"], meta["session_id"])
    else:
        entries = jsonl_branch(meta["transcript_path"])
    normalized = normalize_entries(entries)
    timestamps = [item["timestamp"] for item in normalized["items"] if item["timestamp"]]
    checkpoints = meta["_checkpoints"] or []
    checkpoint_summaries = [
        cp["summary"].strip() for cp in checkpoints
        if isinstance(cp, dict) and isinstance(cp.get("summary"), str) and cp["summary"].strip()
    ]
    info = {
        "session_id": meta["session_id"],
        "session_key": meta["session_key"],
        "agent": meta["agent_id"],
        "title": meta["title"] or meta["session_id"],
        "directory": meta["directory"],
        "chat_type": meta["chat_type"],
        "status": meta["status"],
        "model": meta["model"],
        "model_provider": meta["model_provider"],
        "tokens": meta["tokens"],
        "cost": meta["cost"],
        "archived": meta["archived"],
        "pinned": meta["pinned"],
        "created": fmt_time(meta["created"]),
        "updated": fmt_time(meta["updated"]),
        "first_ts": fmt_time(timestamps[0] if timestamps else meta["created"]),
        "last_ts": fmt_time(timestamps[-1] if timestamps else meta["updated"]),
        "source": meta["source"],
        "compaction_count": meta["compaction_count"] + len(checkpoint_summaries),
    }
    return {"info": info, "normalized": normalized, "checkpoint_summaries": checkpoint_summaries}


# ---------- 任务状态重建 ----------

def classify_tool(name) -> str:
    lowered = str(name or "").lower()
    if not lowered:
        return "other"
    if lowered in SHELL_TOOLS or SHELL_HINT_RE.search(lowered):
        return "shell"
    if lowered in EDIT_TOOLS or EDIT_HINT_RE.search(lowered):
        return "edit"
    if lowered in READ_TOOLS or READ_HINT_RE.search(lowered):
        return "read"
    return "other"


def tool_file(name, item_input) -> str:
    for key in ("path", "file_path", "filePath", "file", "filename", "directory", "url"):
        value = item_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("pattern", "query", "search"):
        if item_input.get(key):
            return str(item_input[key])
    for value in item_input.values():
        if isinstance(value, str) and ("/" in value or "\\" in value) and len(value) < 260:
            return value
    return ""


def shell_command(item_input) -> str:
    for key in ("command", "cmd", "script"):
        value = item_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_state(items):
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
        elif kind == "error_text":
            test_results.append({"command_hint": "assistant error", "is_error": True, "content": item["text"]})
        elif kind == "tool_use":
            name = str(item.get("name") or "").lower()
            item_input = item.get("input") or {}
            calls[item.get("tool_use_id") or f"#{len(calls)}"] = (name, item_input)
            category = classify_tool(name)
            if category == "read":
                files_read.append(tool_file(name, item_input))
            elif category == "edit":
                files_edited.append(tool_file(name, item_input))
            elif category == "shell":
                commands.append(shell_command(item_input))
        elif kind == "tool_result":
            name, call_input = calls.get(item.get("tool_use_id"), ("", {}))
            content = item.get("content") or ""
            command = shell_command(call_input) if name in SHELL_TOOLS else ""
            if content.strip() and (
                item.get("is_error")
                or TEST_CMD_RE.search(command)
                or TEST_RESULT_RE.search(content[:2000])
            ):
                test_results.append({"command_hint": command or name, "is_error": bool(item.get("is_error")),
                                     "content": content})
    return {
        "goal": first_user,
        "files_read": dedupe(files_read),
        "files_edited": dedupe(files_edited),
        "commands": dedupe(commands),
        "test_results": test_results,
        "last_user": last_user,
        "last_assistant": last_assistant,
    }


# ---------- Markdown 渲染 ----------

def tool_brief(item) -> str:
    name = item.get("name") or "tool"
    item_input = item.get("input") or {}
    detail = shell_command(item_input) or tool_file(str(name).lower(), item_input)
    return f"{name}({truncate(detail, 100)})" if detail else f"{name}(...)"


def render_item(item, max_chars):
    ts = fmt_time(item["timestamp"])
    kind = item["kind"]
    if kind == "user_text":
        return [f"### [用户] {ts}", text_block(item["text"], max_chars)]
    if kind == "assistant_text":
        return [f"### [助手] {ts}", text_block(item["text"], max_chars)]
    if kind == "error_text":
        return [f"### [错误] {ts}", text_block(item["text"], max_chars)]
    if kind == "extension_text":
        return [f"### [插件消息] {ts}", text_block(item["text"], max_chars)]
    if kind == "tool_use":
        return [f"### [工具调用] {item.get('name') or 'tool'} {ts}", "```json",
                truncate(json.dumps(item.get("input") or {}, ensure_ascii=False, separators=(",", ":")), max_chars), "```"]
    if kind == "tool_result":
        error = " (错误)" if item.get("is_error") else ""
        return [f"### [工具结果]{error} {ts}", text_block(item["content"], max_chars)]
    return []


def format_tokens(tokens) -> str:
    parts = []
    if tokens.get("input"):
        parts.append(f"输入 {tokens['input']}")
    if tokens.get("output"):
        parts.append(f"输出 {tokens['output']}")
    if tokens.get("total") and tokens["total"] != tokens.get("input", 0) + tokens.get("output", 0):
        parts.append(f"总计 {tokens['total']}")
    return " / ".join(parts)


def render_summary(session, state, recent_n, max_chars) -> str:
    info = session["info"]
    normalized = session["normalized"]
    lines = [
        "# Resume-OpenClaw 会话接管摘要", "", "## 会话信息",
        f"- 标题: {truncate(info['title'], 120)}",
        f"- 会话ID: {info['session_id']}",
        f"- 会话Key: {info['session_key'] or '(未知)'}" + (f"（agent: {info['agent']}）" if info["agent"] else ""),
        f"- 项目: {info['directory'] or '(未知)'}",
        f"- 存储: {info['source']}",
    ]
    if info["chat_type"]:
        lines.append(f"- 聊天类型: {info['chat_type']}")
    if info["model"]:
        lines.append(f"- 模型: {info['model_provider'] + '/' if info['model_provider'] else ''}{info['model']}")
    token_line = format_tokens(info["tokens"] or {})
    if token_line:
        lines.append(f"- Token 用量: {token_line}")
    if info["cost"]:
        lines.append(f"- 累计费用: ${float(info['cost']):.4f}")
    if info["status"]:
        lines.append(f"- 运行状态: {info['status']}")
    if info["archived"]:
        lines.append("- 状态: 已归档")
    if info["pinned"]:
        lines.append("- 状态: 已置顶")
    lines.append(f"- 时间范围: {info['first_ts'] or info['created'] or '(未知)'} ~ {info['last_ts'] or info['updated'] or '(未知)'}")
    lines.append(f"- 消息条目数: {len(normalized['items'])}")
    lines.append("")
    summaries = [
        {"text": item["summary"], "source": "transcript"} for item in normalized["summaries"]
    ] + [{"text": text, "source": "checkpoint"} for text in session["checkpoint_summaries"]]
    if summaries:
        lines.append("## 历史摘要（原会话 compact）")
        for item in summaries[-3:]:
            lines.append(f"- {truncate(item['text'], max_chars)}")
        lines.append("")
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
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
            prefix = " [错误]" if result["is_error"] else ""
            lines.append(f"-{prefix} {truncate(first, 200)}")
        lines.append("")
    lines.extend(["### 最近用户消息", text_block(state["last_user"], max_chars) or "(无)",
                  "", "### 最近助手消息", text_block(state["last_assistant"], max_chars) or "(无)", ""])
    recent = normalized["items"][-recent_n:] if recent_n else []
    lines.extend([f"## 近期对话（最近 {len(recent)} 条）", ""])
    for item in recent:
        lines.extend(render_item(item, max_chars))
        lines.append("")
    if recent_n:
        older_tools = [item for item in normalized["items"][:-recent_n] if item["kind"] == "tool_use"]
    else:
        older_tools = []
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 60 条）")
        for item in older_tools[-60:]:
            lines.append(f"- [{fmt_time(item['timestamp'])}] {tool_brief(item)}")
        lines.append("")
    lines.extend([
        "## 接管建议",
        "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。",
        "- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。",
        "- 不要逐字复述历史；基于现状决定下一步动作。",
        "- 若想让 OpenClaw 自己原生续接：在同会话渠道继续对话即可（网关按 sessionKey 恢复上下文）；CLI 可用 `openclaw sessions` 查看会话。",
        "",
    ])
    return "\n".join(lines)


# ---------- CLI ----------

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
        time_label = fmt_time(meta["updated"]) or fmt_time(meta["created"]) or "(无时间)"
        flags = "/".join(flag for flag in ("已归档" if meta["archived"] else "", "置顶" if meta["pinned"] else "") if flag)
        agent_label = f"[{meta['agent_id']}] " if meta["agent_id"] else ""
        key_label = f" key={meta['session_key']}" if meta["session_key"] and meta["session_key"] != meta["session_id"] else ""
        print(f"{mark} {time_label}  {meta['session_id'][:16]}  {agent_label}{flags + ' ' if flags else ''}"
              f"标题: {meta['title'] or '(无标题)'}{key_label}")


def print_help(parser):
    parser.print_help()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="resume_openclaw",
        description="读取 OpenClaw（原 Clawdbot/Moltbot）本地会话，生成结构化接管摘要。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "数据来源:\n"
            "  状态目录（OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot）下：\n"
            "  - 当前版本：agents/<agentId>/agent/openclaw-agent.sqlite（session_nodes/transcript_events）\n"
            "  - 旧版：agents/<agentId>/sessions/sessions.json + <sessionId>.jsonl、根级 sessions/sessions.json"
        ),
    )
    parser.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    parser.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", default=None, metavar="ID", help="指定会话 ID 或前缀；跨项目查找")
    parser.add_argument("--project", default=os.getcwd(), metavar="PATH", help="项目路径，默认当前目录")
    parser.add_argument("--openclaw-dir", default=None, metavar="DIR", help="OpenClaw 状态目录（~/.openclaw 一级），默认自动探测")
    parser.add_argument("--recent", type=int, default=8, metavar="N", help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, metavar="N", help="单条截断长度，默认 1500")
    parser.add_argument("--limit", type=int, default=0, metavar="N", help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", default=None, metavar="FILE", help="将摘要写入文件")
    return parser


def main(argv=None) -> int:
    setup_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    state_dirs = state_dir_candidates(args.openclaw_dir)
    if not any(is_dir(d) for d in state_dirs):
        print(f"错误：未找到 OpenClaw 状态目录（已探测 {'、'.join(str(d) for d in state_dirs)}）。", file=sys.stderr)
        print("可用 --openclaw-dir 指定状态目录，或设置 OPENCLAW_STATE_DIR 环境变量。", file=sys.stderr)
        return 1
    sessions = scan_sessions(state_dirs)
    if not sessions:
        print("错误：状态目录下未找到任何 OpenClaw 会话（SQLite session_nodes 或 sessions.json）。", file=sys.stderr)
        return 1
    project_path = os.path.abspath(args.project)
    if args.list:
        if not project_sessions(sessions, project_path):
            print(f"错误：未找到项目 {project_path} 的 OpenClaw 会话。可用 --session ID 跨项目查找。", file=sys.stderr)
            return 1
        print_list(sessions, project_path, args.limit)
        return 0
    target = pick_session(sessions, args.session, project_path)
    if not target:
        print(f"错误：未匹配到会话 '{args.session or '当前项目'}'。", file=sys.stderr)
        return 1
    try:
        session = load_session_full(target)
    except (OSError, sqlite3.Error) as exc:
        print(f"错误：解析会话失败：{exc}", file=sys.stderr)
        return 1
    state = build_state(session["normalized"]["items"])
    recent_count = max(args.recent, 0)
    recent_items = session["normalized"]["items"][-recent_count:] if recent_count else []
    if args.json:
        output = json.dumps({
            "info": session["info"],
            "state": state,
            "summaries": [item["summary"] for item in session["normalized"]["summaries"]] + session["checkpoint_summaries"],
            "recent_items": recent_items,
        }, ensure_ascii=False, indent=2)
    else:
        output = render_summary(session, state, recent_count, max(args.max_chars, 1))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output if output.endswith("\n") else output + "\n")
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        print(output if output.endswith("\n") else output + "\n", end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
