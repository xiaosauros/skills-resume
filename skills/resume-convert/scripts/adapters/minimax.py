#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/minimax: MiniMax Code（~/.minimax，兼容 ~/.mavis）-> 归一化会话。

格式依据（与 resume-minimax 一致）：
  数据源两级，会话列表取 SQLite、消息读 JSONL 优先（resume_minimax 的读取顺序）：
  1. SQLite 会话库 <主目录>/v2/sqlite/runtime-state.sqlite：
     local_runtime_sessions（列表/标题/cwd）、local_runtime_message_rows、
     local_runtime_messages（display JSON 兜底）
  2. 规范历史 JSONL <主目录>/v2/sessions/<年>/<月>/<日>/<时间>-session_<id>/
     messages.jsonl（信封 {message_id, turn_id, message{role,content,timestamp}}，
     manifest.json 提供 sessionId）
  消息语义：
  - 用户文本：message(role=user) 的 content（字符串或 text 块拼接；带 archonCompaction
    标记的消息是压缩边界，不按用户文本处理）
  - 助手文本/工具调用：content 的 text / toolCall 块（toolCall.id 为源真实调用 id，
    arguments 是 JSON 串或对象）
  - 工具结果：message(role=toolResult)，toolCallId 配对、isError 标记错误
  - 附件（image 块 / attachments 列表）：本体不跨工具迁移，转成转换注记保留位置信息
  - 压缩：archonCompaction / role=compactionSummary / 行 kind=compaction -> 转换注记
  时间戳为毫秒 epoch（兼容秒/ISO 兜底），统一转成 UTC ISO 'Z' 串交给共享库；
  条目顺序按 resume_minimax 的语义：源顺序展开 + 时间戳稳定排序。
archonCompaction.todoState 任务清单只服务于 resume 摘要，Claude 会话流没有对应载体，
不转换。与 minimax.js 功能等价、输出一致。
"""

import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

# 会话库中需要跳过的非对话会话类型（与 resume_minimax 一致）
SKIP_KINDS = {"peek", "channel"}


def default_dir():
    for env_name in ("MINIMAX_DATA_DIR", "MAVIS_DATA_DIR"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return os.path.expanduser(value.strip())
    home = os.path.expanduser("~")
    primary = os.path.join(home, ".minimax")
    if os.path.exists(primary):
        return primary
    legacy = os.path.join(home, ".mavis")
    if os.path.exists(legacy):
        return legacy
    return primary


def db_path(data_dir):
    return os.path.join(data_dir, "v2", "sqlite", "runtime-state.sqlite")


def history_root(data_dir):
    return os.path.join(data_dir, "v2", "sessions")


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


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
    """工具结果 JSON 串 -> 展示文本（对象转紧凑 JSON，与 JSON.stringify 对齐）。"""
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
    """resume_minimax 的时间戳语义：秒/毫秒 epoch 兼容，ISO 串兜底，失败为 0。"""
    if value is None or value == "":
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0
    # resume 只排除 NaN；这里把 inf 也归零，与 JS 版 Number.isFinite 行为对齐
    if not math.isfinite(number):
        return 0
    return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)


def ms_to_iso(ms):
    """毫秒 epoch -> UTC ISO 'Z' 串（共享库 to_iso_z 可解析）。"""
    if not ms:
        return ""
    try:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(ms))
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms) % 1000:03d}Z"
    except (OverflowError, OSError, ValueError):
        return ""


def open_db(db_file):
    """只读打开（与 resume_minimax 一致：file:...?mode=ro URI）。"""
    uri = Path(db_file).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 会话列表

def sessions_dir_candidates(data_dir):
    """v2/sessions 下 年/月/日/时间 四层目录里的会话目录。"""
    root = history_root(data_dir)
    dirs = []
    if not os.path.isdir(root):
        return dirs
    for year in sorted(os.listdir(root)):
        year_path = os.path.join(root, year)
        if not os.path.isdir(year_path):
            continue
        for month in sorted(os.listdir(year_path)):
            month_path = os.path.join(year_path, month)
            if not os.path.isdir(month_path):
                continue
            for day in sorted(os.listdir(month_path)):
                day_path = os.path.join(month_path, day)
                if not os.path.isdir(day_path):
                    continue
                for time_dir in sorted(os.listdir(day_path)):
                    time_path = os.path.join(day_path, time_dir)
                    if os.path.isdir(time_path):
                        dirs.append(time_path)
    return dirs


def manifest_of(session_dir):
    file = os.path.join(session_dir, "manifest.json")
    if not os.path.isfile(file):
        return None
    try:
        with open(file, "r", encoding="utf-8") as f:
            value = parse_json(f.read(), None)
    except OSError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str) \
            or not value.get("sessionId"):
        return None
    return value


def scan_sessions_files(data_dir):
    """无会话库时的兜底：按 manifest.json 扫描（无法得知 cwd，标题交给首条用户消息）。"""
    metas = []
    for session_dir in sessions_dir_candidates(data_dir):
        manifest = manifest_of(session_dir)
        if not manifest:
            continue
        messages_file = os.path.join(session_dir, "messages.jsonl")
        updated = timestamp_ms(manifest.get("updatedAtMs"))
        if not updated:
            try:
                updated = int(os.path.getmtime(messages_file) * 1000)
            except OSError:
                updated = 0
        metas.append({
            "session_id": manifest["sessionId"],
            "title": "",
            "directory": "",
            "model": "",
            "archived": False,
            "created": timestamp_ms(manifest.get("createdAtMs")),
            "updated": updated,
            "source": "files",
            "history_dir": session_dir,
            "path": messages_file,
        })
    metas.sort(key=lambda meta: meta["updated"] or 0, reverse=True)
    return metas


def scan_sessions_db(data_dir):
    """会话库扫描（跳过 hidden 与 peek/channel）；库缺失或不可读返回 None 走文件兜底。"""
    file = db_path(data_dir)
    if not os.path.isfile(file):
        return None
    try:
        conn = open_db(file)
        try:
            rows = conn.execute("SELECT * FROM local_runtime_sessions").fetchall()
        finally:
            conn.close()
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
        metas.append({
            "session_id": row["session_id"],
            "title": row["title"] or "",
            "directory": row["workspace_dir"] or row["project_workspace_dir"]
            or record.get("workspaceDir") or "",
            "model": record.get("effectiveModel") or "",
            "archived": bool(row["archived"]),
            "created": timestamp_ms(row["created_at_ms"] if row["created_at_ms"] is not None
                                    else record.get("createdAtMs")),
            "updated": timestamp_ms(row["updated_at_ms"] if row["updated_at_ms"] is not None
                                    else record.get("updatedAtMs")),
            "source": "sqlite",
            "history_dir": os.path.join(history_root(data_dir), history_rel) if history_rel else "",
            "path": file,
        })
    metas.sort(key=lambda meta: (1 if meta["archived"] else 0, -(meta["updated"] or 0)))
    return metas


def scan_sessions(data_dir):
    from_db = scan_sessions_db(data_dir)
    if from_db is not None:
        return from_db
    return scan_sessions_files(data_dir)


# ---------------------------------------------------------------- 消息条目

def user_text_of(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def normalize_envelopes(envelopes):
    """信封消息 -> 统一条目（user_text / assistant_text / tool_use / tool_result /
    compaction / attachment），语义与 resume_minimax.normalize_envelopes 一致。"""
    items = []
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
                # 压缩边界消息：摘要转注记；todoState 仅供 resume 摘要，不转换
                compaction_count += 1
                if isinstance(marker.get("summary"), str):
                    latest_compaction = marker["summary"]
                items.append({"kind": "compaction", "timestamp": ts,
                              "text": marker.get("summary") or "(压缩边界)"})
                continue
            content = message.get("content")
            if isinstance(content, list):
                index = 0
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        index += 1
                        items.append({"kind": "attachment", "timestamp": ts,
                                      "text": f"[图片 #{index}]"})
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
                    if block.get("type") == "text" and isinstance(block.get("text"), str) \
                            and block["text"].strip():
                        items.append({"kind": "assistant_text", "timestamp": ts,
                                      "text": block["text"]})
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
                    if isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
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
            items.append({"kind": "compaction", "timestamp": ts,
                          "text": message.get("summary") or "(压缩摘要)"})
    items.sort(key=lambda item: item["timestamp"] or 0)
    return {"items": items, "model": model,
            "compaction_count": compaction_count, "latest_compaction": latest_compaction}


def normalize_rows(rows):
    """会话库 message 行（display message）-> 统一条目，语义与 resume_minimax 一致。"""
    items = []
    compaction_count = 0
    latest_compaction = ""
    for row in rows:
        data = parse_json(row["data_json"], {})
        role = data.get("role") or row["role"] or ""
        ts = timestamp_ms(data.get("timestamp") if data.get("timestamp") is not None
                          else data.get("created_at") if data.get("created_at") is not None
                          else row["created_at_ms"])
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
                    # 库行把结果内联在调用上：status 2 成功 / 3 失败
                    status = call.get("tool_call_status")
                    if status in (2, 3):
                        items.append({
                            "kind": "tool_result",
                            "timestamp": ts,
                            "tool_use_id": call["tool_call_id"],
                            "content": parse_json_string(call.get("tool_call_result_data")),
                            "is_error": status == 3,
                        })
        elif data.get("kind") == "compaction" and isinstance(data.get("msg_content"), str) \
                and data["msg_content"].strip():
            compaction_count += 1
            latest_compaction = data["msg_content"]
            items.append({"kind": "compaction", "timestamp": ts, "text": data["msg_content"]})
    items.sort(key=lambda item: item["timestamp"] or 0)
    return {"items": items, "model": "",
            "compaction_count": compaction_count, "latest_compaction": latest_compaction}


def read_envelopes(messages_file):
    envelopes = []
    try:
        with open(messages_file, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        raise cs.ConvertError(f"无法读取会话历史文件：{messages_file}（{e}）")
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        envelope = parse_json(line, None)
        if isinstance(envelope, dict) and isinstance(envelope.get("message"), dict):
            envelopes.append(envelope)
    return envelopes


def find_session_dir(data_dir, session_id):
    for session_dir in sessions_dir_candidates(data_dir):
        manifest = manifest_of(session_dir)
        if manifest and manifest["session_id"] == session_id:
            return session_dir
    return ""


def load_messages_jsonl(data_dir, meta):
    file = os.path.join(meta["history_dir"], "messages.jsonl") if meta["history_dir"] else ""
    if not file or not os.path.isfile(file):
        session_dir = find_session_dir(data_dir, meta["session_id"])
        file = os.path.join(session_dir, "messages.jsonl") if session_dir else ""
    if not file or not os.path.isfile(file):
        return None
    return normalize_envelopes(read_envelopes(file))


def load_messages_rows(data_dir, session_id):
    file = db_path(data_dir)
    if not os.path.isfile(file):
        return None
    try:
        conn = open_db(file)
        try:
            rows = conn.execute(
                "SELECT msg_id, role, created_at_ms, data_json "
                "FROM local_runtime_message_rows WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall()
            normalized = normalize_rows(rows)
            if normalized["items"]:
                return normalized
            blob = conn.execute(
                "SELECT display_messages_json FROM local_runtime_messages WHERE session_id=?",
                (session_id,)).fetchone()
        finally:
            conn.close()
        if blob and blob["display_messages_json"]:
            display = parse_json(blob["display_messages_json"], [])
            if isinstance(display, list):
                rows_like = [
                    {
                        "msg_id": message.get("msg_id") or str(index),
                        "role": message.get("role"),
                        "created_at_ms": timestamp_ms(
                            message.get("timestamp") if message.get("timestamp") is not None
                            else message.get("created_at")),
                        "data_json": json.dumps(message, ensure_ascii=False),
                    }
                    for index, message in enumerate(display)
                    if isinstance(message, dict)
                ]
                return normalize_rows(rows_like)
    except sqlite3.Error:
        return None
    return normalized


def load_normalized(data_dir, meta):
    """消息条目：JSONL 优先；缺失且会话来自 SQLite 时回退会话库（与 resume_minimax 一致）。"""
    normalized = load_messages_jsonl(data_dir, meta)
    if normalized is None and meta["source"] == "sqlite":
        normalized = load_messages_rows(data_dir, meta["session_id"])
    if normalized is None:
        normalized = {"items": [], "model": "",
                      "compaction_count": 0, "latest_compaction": ""}
    return normalized


# ---------------------------------------------------------------- 转换

def items_to_msgs(items):
    """条目 -> 共享库归一化消息（保持 resume_minimax 排序后的源顺序）。"""
    msgs = []
    for item in items:
        ts = ms_to_iso(item["timestamp"])
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
        elif kind == "compaction":
            msgs.append(cs.m_note(item["text"], ts))
        elif kind == "attachment":
            # 附件（图片/文件）本体不跨工具迁移，转换为注记保留其位置信息
            msgs.append(cs.m_note(item["text"], ts))
    return msgs


def derive_title(meta, normalized):
    """标题链与 resume_minimax 一致：库标题 -> 首条用户消息 -> 最近压缩摘要 -> 会话ID。"""
    title = meta["title"]
    if not title:
        first_user = next((item for item in normalized["items"]
                           if item["kind"] == "user_text"), None)
        title = first_user["text"] if first_user else \
            (normalized["latest_compaction"] or meta["session_id"])
    title = title.splitlines()[0].strip() if title.splitlines() else ""
    return title or meta["session_id"]


def list_sessions(dir_=None, project=None):
    data_dir = os.path.abspath(dir_ or default_dir())
    sessions = scan_sessions(data_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    out = []
    for meta in sessions[:50]:
        count = 0
        last_ts = ""
        normalized = None
        try:
            normalized = load_normalized(data_dir, meta)
            msgs = items_to_msgs(normalized["items"])
            count = len(msgs)
            last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
        except (cs.ConvertError, sqlite3.Error):
            pass  # 单会话失败不阻塞列表
        out.append({
            "session_id": meta["session_id"],
            "title": derive_title(meta, normalized or {"items": [], "latest_compaction": ""}),
            "cwd": meta["directory"],
            "mtime": meta["updated"] or 0,
            "path": meta["path"],
            "count": count,
            "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    data_dir = os.path.abspath(dir_ or default_dir())
    sessions = scan_sessions(data_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 MiniMax Code 会话（dir={data_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    meta = sessions[0]
    try:
        normalized = load_normalized(data_dir, meta)
    except (OSError, sqlite3.Error) as error:
        raise cs.ConvertError(f"解析会话失败：{meta['path']}（{error}）")
    msgs = items_to_msgs(normalized["items"])
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{meta['path']}（{meta['session_id']}）")

    title = derive_title(meta, normalized)
    timestamps = [item["timestamp"] for item in normalized["items"] if item["timestamp"]]
    ref = cs.make_ref(
        "minimax", meta["session_id"], title, meta["directory"],
        model=meta["model"] or normalized["model"],
        started_at=ms_to_iso(timestamps[0] if timestamps else meta["created"]),
        ended_at=ms_to_iso(timestamps[-1] if timestamps else meta["updated"]),
        source=str(meta["path"]))
    return ref, msgs
