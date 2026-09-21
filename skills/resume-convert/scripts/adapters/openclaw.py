#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/openclaw: OpenClaw（原 Clawdbot/Moltbot，~/.openclaw）-> 归一化会话。

格式依据（与 resume-openclaw 一致）：
  状态目录（OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot）下：
  - 当前版本（每 agent SQLite）：agents/<agentId>/agent/openclaw-agent.sqlite
      · session_nodes：session_key 主键，entry_json 为会话元数据（SessionEntry）；
        标题取 label > displayName > subject > autoLabel（行级 label/display_name 兜底），
        目录取 spawnedCwd > execCwd > worktree.repoRoot > sessionDiffBaseline.root
      · transcript_events：(session_id, seq) -> event_json（与 JSONL 行同构的条目树）
      · session_transcript_active_events：活动分支投影（优先按 active_position 读，
        表缺失/结构不符时回退按 seq 全量）
  - 旧版（JSON）：sessions/sessions.json（sessionKey -> SessionEntry 对象）+ 同目录
    <sessionId>.jsonl：首行 {type:"session",cwd} 头，其后 {type,id,parentId} 条目树，
    存在 leaf 指针时沿最后一条 leaf 的 targetId 回溯父链取当前分支
条目转换语义（与 resume_openclaw.normalize_entries 一致）：
  - user/assistant 文本消息 -> 对话消息（content 支持字符串与 [{type:text|image}] 块）
  - assistant 的 toolCall 块 -> 工具调用（id 为源真实 id；arguments 非对象时包装 {"raw":...}）
  - toolResult 消息 -> 工具结果（toolCallId 配对，isError 标记错误）
  - assistant stopReason=error 的 errorMessage -> 助手错误消息（保留排障信息）
  - compaction / branch_summary 的 summary -> 历史摘要注记（保持源记录位置）
  - custom_message（display!=False）-> 插件消息注记
  - thinking / label / session_info / reset / model_change / leaf 等跳过
标题：SessionEntry 标题字段优先，缺省回退首条用户消息首行（与 codex 适配器一致）。
与 openclaw.js 功能等价、输出一致。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402


def default_dir():
    """OpenClaw 状态目录：OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot。"""
    env_dir = os.environ.get("OPENCLAW_STATE_DIR")
    if env_dir and env_dir.strip():
        return os.path.expanduser(env_dir.strip())
    home = os.path.expanduser("~")
    for name in (".openclaw", ".clawdbot"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(home, ".openclaw")


def state_dirs_for(openclaw_dir):
    """待扫描的状态目录列表：显式 dir 只扫该目录；否则按优先级取所有存在的候选。"""
    if openclaw_dir:
        return [openclaw_dir]
    candidates = []
    env_dir = os.environ.get("OPENCLAW_STATE_DIR")
    if env_dir and env_dir.strip():
        candidates.append(os.path.expanduser(env_dir.strip()))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, ".openclaw"))
    candidates.append(os.path.join(home, ".clawdbot"))
    seen, result = set(), []
    for candidate in candidates:
        key = norm_path(candidate)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(candidate):
            result.append(candidate)
    return result


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def parse_json(value, fallback=None):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {} if fallback is None else fallback


def timestamp_ms(value):
    """resume_openclaw 的时间戳语义：秒/毫秒 epoch 兼容，ISO 串兜底，失败为 0。"""
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


def ms_to_iso(ms):
    """毫秒 epoch -> UTC ISO 'Z' 串（共享库 to_iso_z 可解析）。"""
    if not ms:
        return ""
    try:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(ms))
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms) % 1000:03d}Z"
    except (OverflowError, OSError, ValueError):
        return ""


def file_mtime(path):
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return 0


def message_content_text(content):
    """消息内容统一抽为纯文本：字符串原样；块列表取 text，image 记为 [图片]。"""
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


# ---------- 会话元数据 ----------

def entry_title(entry):
    for key in ("label", "displayName", "subject", "autoLabel"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def entry_directory(entry):
    worktree = entry.get("worktree") if isinstance(entry.get("worktree"), dict) else {}
    baseline = entry.get("sessionDiffBaseline") if isinstance(entry.get("sessionDiffBaseline"), dict) else {}
    for value in (entry.get("spawnedCwd"), entry.get("execCwd"),
                  worktree.get("repoRoot"), baseline.get("root")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def collect_sources(state_dir):
    """会话源：每 agent 的 SQLite 库 + 每 agent 旧版 sessions.json + 根级旧版 sessions.json。"""
    sources = []
    agents_root = os.path.join(state_dir, "agents")
    agent_ids = []
    if os.path.isdir(agents_root):
        try:
            agent_ids = sorted(name for name in os.listdir(agents_root)
                               if os.path.isdir(os.path.join(agents_root, name)))
        except OSError:
            agent_ids = []
    for agent_id in agent_ids:
        agent_dir = os.path.join(agents_root, agent_id)
        sqlite_path = os.path.join(agent_dir, "agent", "openclaw-agent.sqlite")
        if os.path.isfile(sqlite_path):
            sources.append({"kind": "sqlite", "agent_id": agent_id, "path": sqlite_path})
        legacy_store = os.path.join(agent_dir, "sessions", "sessions.json")
        if os.path.isfile(legacy_store):
            sources.append({"kind": "legacy", "agent_id": agent_id, "path": legacy_store,
                            "sessions_dir": os.path.dirname(legacy_store)})
    root_legacy = os.path.join(state_dir, "sessions", "sessions.json")
    if os.path.isfile(root_legacy):
        sources.append({"kind": "legacy", "agent_id": "", "path": root_legacy,
                        "sessions_dir": os.path.dirname(root_legacy)})
    return sources


def open_db_ro(sqlite_path):
    """只读打开（与 resume_openclaw 一致：file:...?mode=ro URI）。"""
    try:
        return sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise cs.ConvertError(f"无法打开 OpenClaw SQLite 库：{sqlite_path}（{exc}）")


def sqlite_nodes(sqlite_path):
    """session_nodes 全表 -> 节点列表（entry_json 解析为 SessionEntry）。"""
    conn = open_db_ro(sqlite_path)
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
                # 低版本库列不全时退回基础列
                rows = conn.execute(
                    "SELECT session_key, current_session_id, entry_json, updated_at FROM session_nodes"
                ).fetchall()
            except sqlite3.Error:
                return []
        except sqlite3.Error:
            return []
        nodes = []
        for row in rows:
            keys = row.keys()
            nodes.append({
                "session_key": row["session_key"] or "",
                "current_session_id": row["current_session_id"] or "",
                "entry": parse_json(row["entry_json"], {}),
                "row_label": (row["label"] or "") if "label" in keys else "",
                "row_display_name": (row["display_name"] or "") if "display_name" in keys else "",
                "created_at": (row["created_at"] or 0) if "created_at" in keys else 0,
                "updated_at": (row["updated_at"] or 0) if "updated_at" in keys else 0,
                "last_activity_at": (row["last_activity_at"] or 0) if "last_activity_at" in keys else 0,
                "row_status": (row["status"] or "") if "status" in keys else "",
            })
        return nodes
    finally:
        conn.close()


def legacy_store_entries(store_path):
    """sessions.json 为对象映射 sessionKey -> SessionEntry。"""
    data = parse_json(read_text_file(store_path), None)
    if not isinstance(data, dict):
        return []
    entries = []
    for session_key, value in data.items():
        if isinstance(value, dict) and value.get("sessionId"):
            entries.append((session_key, value))
    return entries


def read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def jsonl_header_cwd(transcript_path):
    """JSONL 首行会话头 {type:"session", id, timestamp, cwd} 中的 cwd。"""
    for line in read_text_file(transcript_path).splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        parsed = parse_json(trimmed, None)
        if isinstance(parsed, dict) and parsed.get("type") == "session":
            return str(parsed.get("cwd") or "")
    return ""


def jsonl_transcript(transcript_path):
    """JSONL -> (header, entries)。存在 leaf 指针时沿最后一条 leaf 的 targetId
    回溯父链得到当前分支；指针失效时回退为文件顺序（与 resume_openclaw 一致）。"""
    header = None
    entries = []
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
            return header or {}, branch
    return header or {}, entries


def sqlite_events(sqlite_path, session_id):
    """transcript_events -> 条目列表：优先读活动分支投影，回退按 seq 全量。"""
    conn = open_db_ro(sqlite_path)
    try:
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
        raise cs.ConvertError(f"读取会话 {session_id} 的 transcript_events 失败：{exc}")
    finally:
        conn.close()


# ---------- 扫描与转换 ----------

def scan_sessions(state_dirs):
    """跨项目扫描全部会话元信息（session_id 去重），按 updated 倒序。"""
    sessions = []
    seen_ids = set()
    for state_dir in state_dirs:
        for source in collect_sources(state_dir):
            if source["kind"] == "sqlite":
                for node in sqlite_nodes(source["path"]):
                    entry = node["entry"]
                    session_id = str(entry.get("sessionId") or node["current_session_id"] or "")
                    if not session_id or session_id in seen_ids:
                        continue
                    seen_ids.add(session_id)
                    sessions.append({
                        "session_id": session_id,
                        "session_key": node["session_key"],
                        "agent_id": source["agent_id"],
                        "title": entry_title(entry) or node["row_label"] or node["row_display_name"],
                        "directory": entry_directory(entry),
                        "model_provider": str(entry.get("modelProvider") or ""),
                        "model": str(entry.get("modelOverride") or entry.get("model") or ""),
                        "created": node["created_at"] or timestamp_ms(entry.get("createdAt")),
                        "updated": max(node["updated_at"] or 0, node["last_activity_at"] or 0)
                        or timestamp_ms(entry.get("updatedAt")),
                        "source_kind": "sqlite",
                        "path": source["path"],
                        "source": f"sqlite:{source['path']}",
                    })
            else:
                for session_key, entry in legacy_store_entries(source["path"]):
                    session_id = str(entry.get("sessionId") or "")
                    if not session_id or session_id in seen_ids:
                        continue
                    seen_ids.add(session_id)
                    # 正文 JSONL 与 sessions.json 同目录（resolveSessionFilePathCore 约定）
                    transcript = os.path.join(source["sessions_dir"], f"{session_id}.jsonl")
                    sessions.append({
                        "session_id": session_id,
                        "session_key": session_key,
                        "agent_id": source["agent_id"],
                        "title": entry_title(entry),
                        "directory": entry_directory(entry) or jsonl_header_cwd(transcript),
                        "model_provider": str(entry.get("modelProvider") or ""),
                        "model": str(entry.get("modelOverride") or entry.get("model") or ""),
                        "created": timestamp_ms(entry.get("createdAt")),
                        "updated": timestamp_ms(entry.get("updatedAt")) or file_mtime(transcript),
                        "source_kind": "legacy",
                        "path": transcript,
                        "source": f"legacy:{source['path']}",
                    })
    sessions.sort(key=lambda meta: (meta["updated"] or meta["created"]), reverse=True)
    return sessions


def load_entries(meta):
    """按存储类型读取会话正文条目。"""
    if meta["source_kind"] == "sqlite":
        return sqlite_events(meta["path"], meta["session_id"])
    return jsonl_transcript(meta["path"])[1]


def entries_to_msgs(entries):
    """事件条目 -> 共享库归一化消息（严格保持源记录顺序）。"""
    msgs = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type") or ""
        ts = ms_to_iso(timestamp_ms(entry.get("timestamp")))
        if entry_type == "message":
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            role = message.get("role") or ""
            if role == "user":
                text = message_content_text(message.get("content")).strip()
                if text:
                    msgs.append(cs.m_message("user", text, ts))
            elif role == "assistant":
                blocks = message.get("content") if isinstance(message.get("content"), list) else []
                text = "\n".join(
                    str(block.get("text") or "") for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text").strip()
                if text:
                    msgs.append(cs.m_message("assistant", text, ts))
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    raw_input = block.get("arguments")
                    if not isinstance(raw_input, dict):
                        raw_input = {} if raw_input in (None, "") else {"raw": raw_input}
                    msgs.append(cs.m_tool_use(str(block.get("id") or ""),
                                              str(block.get("name") or "tool"),
                                              raw_input, ts))
                if message.get("stopReason") == "error" and message.get("errorMessage"):
                    msgs.append(cs.m_message("assistant", str(message["errorMessage"]), ts))
            elif role == "toolResult":
                msgs.append(cs.m_tool_result(
                    str(message.get("toolCallId") or ""),
                    message_content_text(message.get("content")),
                    bool(message.get("isError")), ts))
        elif entry_type == "compaction":
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                msgs.append(cs.m_note(f"历史摘要（compact）：{summary.strip()}", ts))
        elif entry_type == "branch_summary":
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                msgs.append(cs.m_note(f"历史摘要（branch）：{summary.strip()}", ts))
        elif entry_type == "custom_message":
            text = message_content_text(entry.get("content")).strip()
            if entry.get("display") is not False and text:
                msgs.append(cs.m_note(f"插件消息：{text}", ts))
        # thinking / label / session_info / reset / model_change / custom / leaf 等跳过
    return msgs


def first_user_title(msgs, fallback):
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m["text"].strip():
            return m["text"].strip().splitlines()[0][:60]
    return fallback


def list_sessions(dir_=None, project=None):
    state_dirs = state_dirs_for(dir_)
    sessions = scan_sessions(state_dirs)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    out = []
    for meta in sessions[:50]:
        title = meta["title"]
        count = 0
        last_ts = ""
        try:
            msgs = entries_to_msgs(load_entries(meta))
            count = len(msgs)
            last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
            if not title:
                title = first_user_title(msgs, meta["session_id"])
        except cs.ConvertError:
            if not title:
                title = meta["session_id"]
        out.append({
            "session_id": meta["session_id"], "title": title,
            "cwd": meta["directory"], "mtime": meta["updated"],
            "path": meta["path"], "count": count, "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    state_dirs = state_dirs_for(dir_)
    sessions = scan_sessions(state_dirs)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 OpenClaw 会话（dir={dir_ or default_dir()}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    meta = sessions[0]
    msgs = entries_to_msgs(load_entries(meta))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{meta['path']}（{meta['session_id']}）")

    timestamps = [m["ts"] for m in msgs if m["ts"]]
    model_str = (f"{meta['model_provider']}/{meta['model']}"
                 if meta["model_provider"] and meta["model"] else meta["model"])
    title = meta["title"] or first_user_title(msgs, meta["session_id"])
    ref = cs.make_ref("openclaw", meta["session_id"], title, meta["directory"],
                      model=model_str,
                      started_at=timestamps[0] if timestamps else ms_to_iso(meta["created"]),
                      ended_at=timestamps[-1] if timestamps else ms_to_iso(meta["updated"]),
                      source=meta["source"])
    return ref, msgs
