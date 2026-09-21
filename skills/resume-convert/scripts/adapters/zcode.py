#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/zcode: ZCode（~/.zcode/cli/db/db.sqlite）-> 归一化会话。

格式依据（与 resume-zcode 一致）：
  SQLite 库含 session / message / part / todo 表，message.data / part.data 是 JSON 文本：
  - 用户/助手文本：part(type=text)；synthetic 标记的系统注入提醒（TodoWrite 提示、
    任务通知等）不属于真实对话，跳过
  - 工具调用：part(type=tool)，callID 为源真实 id；按 resume_zcode 的判定语义，
    有 output/error 或 status 为 completed/error 时产出结果；
    error 为 JSON null 视作无错误（resume_zcode.py 的 None 语义）
  - 附件：part(type=file) -> 转换注记（保留"会话里出现过附件"的信息）
  - 推理（reasoning）/ step-start / step-finish / timeline 跳过
  - 上下文压缩：session.time_compacting -> 转换注记
  时间戳一律为毫秒 epoch（兼容秒/ISO 兜底），统一转成 UTC ISO 'Z' 串交给共享库；
  条目顺序按 resume_zcode 的排序语义：SQL sequence 顺序展开 + 时间戳稳定排序。
todo 清单只服务于 resume_zcode 的摘要，Claude 会话流没有对应载体，不转换。
与 zcode.js 功能等价、输出一致；非字符串 error/output 序列化为紧凑 JSON
（分隔符与 JSON.stringify 对齐，保证双语言指纹一致）。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

COMPACT_NOTE = "会话发生过上下文压缩（compact）"


def default_dir():
    override = os.environ.get("ZCODE_HOME")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser(os.path.join("~", ".zcode"))


def db_path(data_dir):
    return os.path.join(data_dir, "cli", "db", "db.sqlite")


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def timestamp_ms(value):
    """resume_zcode 的时间戳语义：秒/毫秒 epoch 兼容，ISO 串兜底，失败为 0。"""
    if value is None or value == "":
        return 0
    if isinstance(value, dict):
        value = value.get("updated") or value.get("completed") or value.get("created")
    try:
        number = float(value)
        return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
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


def json_text(value):
    """紧凑 JSON 文本（与 JS JSON.stringify 输出一致）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def open_db(db_file):
    """只读打开（与 resume_zcode 一致：file:...?mode=ro URI）。"""
    try:
        uri = Path(db_file).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except (OSError, sqlite3.Error, ValueError) as e:
        raise cs.ConvertError(f"无法打开 ZCode 数据库：{db_file}（{e}）")


def has_current_schema(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"session", "message", "part"}.issubset(names)


def scan_sessions(zcode_dir):
    """按 time_updated 倒序扫描全部会话元信息（库缺失/结构不符返回空）。"""
    file = db_path(zcode_dir)
    if not os.path.isfile(file):
        return []
    try:
        conn = open_db(file)
    except cs.ConvertError:
        return []
    try:
        if not has_current_schema(conn):
            return []
        rows = conn.execute(
            """
            SELECT id, title, directory,
                   time_created, time_updated, time_compacting, time_archived
            FROM session ORDER BY time_updated DESC
            """
        ).fetchall()
        return [
            {
                "session_id": row["id"],
                "title": row["title"] or row["id"],
                "directory": row["directory"] or "",
                "created": row["time_created"],
                "updated": row["time_updated"],
                "compact": row["time_compacting"],
                "archived": row["time_archived"],
                "path": file,
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def normalize_records(conn, session_id, compact_ms=None):
    """message/part 表 -> 条目列表（毫秒时间戳），语义与 resume_zcode.normalize_records 一致。"""
    items = []
    model = ""
    provider = ""
    msg_rows = conn.execute(
        "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY sequence, time_created, id",
        (session_id,)).fetchall()
    parts_by_message = {}
    for row in conn.execute(
        "SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY sequence, time_created, id",
        (session_id,)):
        parts_by_message.setdefault(row["message_id"], []).append(
            {"id": row["id"], "time_created": row["time_created"], "data": parse_json(row["data"])})

    for row in msg_rows:
        data = parse_json(row["data"])
        role = data.get("role") or ""
        ts = timestamp_ms((data.get("time") or {}).get("created") or row["time_created"])
        model_info = data.get("model") or {}
        model = data.get("modelID") or model_info.get("modelID") or model
        provider = data.get("providerID") or model_info.get("providerID") or provider
        for part in parts_by_message.get(row["id"], []):
            pdata = part["data"]
            ptype = pdata.get("type") or ""
            pts = timestamp_ms((pdata.get("time") or {}).get("start")
                               or (part["time_created"] or ts))
            if ptype == "text":
                # synthetic 文本是系统注入的提醒/通知（如 TodoWrite 提示），不属于真实对话
                if pdata.get("synthetic"):
                    continue
                text = pdata.get("text") or ""
                if not text.strip():
                    continue
                items.append({"kind": f"{role}_text", "timestamp": pts, "text": text})
            elif ptype == "tool":
                state = pdata.get("state") or {}
                name = pdata.get("tool") or "tool"
                call_id = pdata.get("callID") or ""
                items.append(
                    {
                        "kind": "tool_use",
                        "timestamp": pts,
                        "name": name,
                        "input": state.get("input") or {},
                        "tool_use_id": call_id,
                    }
                )
                status = str(state.get("status") or "")
                output = state.get("output")
                error = state.get("error")
                # JSON null 与缺字段同样视作"无错误"（resume_zcode.py 的 None 语义）
                has_error = error is not None
                if output is not None or has_error or status in ("completed", "error"):
                    if not isinstance(output, str):
                        output = json_text(output) if output is not None else ""
                    items.append(
                        {
                            "kind": "tool_result",
                            "timestamp": timestamp_ms((state.get("time") or {}).get("end")) or pts,
                            "tool_use_id": call_id,
                            "content": (error if isinstance(error, str) else json_text(error))
                            if has_error else output,
                            "is_error": status == "error" or has_error,
                        }
                    )
            elif ptype == "file":
                label = (pdata.get("filename")
                         or (pdata.get("source") or {}).get("path") or "")
                if not label:
                    # 工具产出的内嵌文件（如截图）没有文件名，按 mime 生成可读描述
                    mime = pdata.get("mime") or ""
                    label = "内嵌图片" if mime.startswith("image/") else (mime or "内嵌附件")
                items.append({"kind": "attachment", "timestamp": pts, "text": str(label)})
            # reasoning / step-start / step-finish / timeline 等类型与对话迁移无关，跳过

    if compact_ms:
        # 会话级上下文压缩标记（session.time_compacting），按时间点排进事件流
        items.append({"kind": "compact_note", "timestamp": timestamp_ms(compact_ms),
                      "text": COMPACT_NOTE})
    items.sort(key=lambda item: item.get("timestamp") or 0)
    return items, model, provider


def items_to_msgs(items):
    """resume_zcode 条目 -> 共享库归一化消息（严格保持源时间顺序）。"""
    msgs = []
    for item in items:
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
        elif kind == "attachment":
            msgs.append(cs.m_note(f"附件：{item.get('text')}", ts))
        elif kind == "compact_note":
            msgs.append(cs.m_note(item["text"], ts))
        # 其他角色文本（system 等）不映射为对话消息，跳过
    return msgs


def build_ref(meta, items, model, provider, db_file):
    timestamps = [item["timestamp"] for item in items if item.get("timestamp")]
    model_str = f"{provider}/{model}" if provider and model else (model or provider)
    return cs.make_ref(
        "zcode", meta["session_id"], meta["title"], meta["directory"],
        model=model_str,
        started_at=ms_to_iso(timestamps[0] if timestamps else timestamp_ms(meta.get("created"))),
        ended_at=ms_to_iso(timestamps[-1] if timestamps else timestamp_ms(meta.get("updated"))),
        source=str(db_file),
    )


def list_sessions(dir_=None, project=None):
    zcode_dir = dir_ or default_dir()
    sessions = scan_sessions(zcode_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    out = []
    for meta in sessions[:50]:
        count = 0
        last_ts = ""
        try:
            conn = open_db(meta["path"])
            try:
                items, _, _ = normalize_records(conn, meta["session_id"], meta.get("compact"))
            finally:
                conn.close()
            msgs = items_to_msgs(items)
            count = len(msgs)
            last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
        except (cs.ConvertError, sqlite3.Error):
            pass  # 单会话失败不阻塞列表
        out.append({
            "session_id": meta["session_id"], "title": meta["title"],
            "cwd": meta["directory"], "mtime": timestamp_ms(meta.get("updated")),
            "path": meta["path"], "count": count, "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    zcode_dir = dir_ or default_dir()
    sessions = scan_sessions(zcode_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 ZCode 会话（dir={zcode_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    meta = sessions[0]
    db_file = meta["path"]
    conn = open_db(db_file)
    try:
        items, model, provider = normalize_records(conn, meta["session_id"], meta.get("compact"))
    finally:
        conn.close()
    msgs = items_to_msgs(items)
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{db_file}（{meta['session_id']}）")
    ref = build_ref(meta, items, model, provider, db_file)
    return ref, msgs
