#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/mimo: MiMo-Code（小米 MiMo，OpenCode fork）SQLite 会话 -> 归一化会话。

格式依据（与 resume-mimo 一致）：
  数据目录下 mimocode.db（渠道库 mimocode-<channel>.db 按修改时间兜底，MIMOCODE_DB 可整体覆盖），
  表 session / message / part（data 列为 JSON）。
  - 用户/助手文本：part(type=text)；synthetic 或 message.summary=true 视为压缩摘要
  - 工具调用：part(type=tool)，callID + state.input/output/error；结果仅在有
    output/error 或 status ∈ {completed, error} 时产出（与 resume 一致）
  - 推理（reasoning / step-start / step-finish / snapshot / retry / agent / checkpoint）
    不可跨模型迁移，跳过
  - 压缩摘要（synthetic 文本、message.summary、compaction.projection.summary）、
    助手消息级错误、patch、file 附件、subtask -> 转换注记（m_note）
标题：session.title，缺省回退 slug、id；cwd：session.directory，缺省回退 project.worktree。
记录顺序严格保持源序（message 按 time_created,id；part 按 time_created,id；错误注记先于该消息 parts）。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def default_dir():
    """MiMo 数据目录：与 resume_mimo.data_dir_candidates 同序，取第一个存在的目录。"""
    candidates = []
    if os.environ.get("MIMOCODE_HOME"):
        candidates.append(os.path.join(
            os.path.expanduser(os.environ["MIMOCODE_HOME"]), "data"))
    if os.environ.get("XDG_DATA_HOME"):
        candidates.append(os.path.join(
            os.path.expanduser(os.environ["XDG_DATA_HOME"]), "mimocode"))
    candidates.append(os.path.expanduser(
        os.path.join("~", ".local", "share", "mimocode")))
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(os.path.join(os.environ["LOCALAPPDATA"], "mimocode"))
    candidates.append(os.path.expanduser(
        os.path.join("~", "Library", "Application Support", "mimocode")))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


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
    """源时间值（ISO 字符串 / 秒或毫秒数字）-> 毫秒数；无法解析返回 0（与 resume 一致）。"""
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


def to_iso(value):
    """源时间值 -> UTC ISO 毫秒字符串（供 cs 消费），无法解析返回 ''。"""
    ms = timestamp_ms(value)
    if not ms:
        return ""
    dt = _EPOCH + timedelta(milliseconds=ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- 数据库定位

def db_candidates(data_dir):
    """数据目录内的数据库候选：MIMOCODE_DB > mimocode.db > mimocode-<channel>.db（按修改时间）。"""
    candidates = []
    env_db = os.environ.get("MIMOCODE_DB")
    if env_db and env_db != ":memory:":
        env_path = os.path.expanduser(env_db)
        candidates.append(env_path if os.path.isabs(env_path)
                          else os.path.join(data_dir, env_path))
    candidates.append(os.path.join(data_dir, "mimocode.db"))
    try:
        channel_dbs = sorted(
            (os.path.join(data_dir, name) for name in os.listdir(data_dir)
             if name.startswith("mimocode-") and name.endswith(".db")),
            key=os.path.getmtime, reverse=True)
    except OSError:
        channel_dbs = []
    candidates.extend(channel_dbs)
    seen = set()
    result = []
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen and os.path.isfile(candidate):
            seen.add(key)
            result.append(candidate)
    return result


def open_db(db_path):
    """只读打开（URI mode=ro，绝不意外创建库文件）。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_current_schema(conn):
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {"session", "message", "part"}.issubset(names)


def db_sessions(db_path):
    """读取单个库内的全部会话元信息（title/directory 与 resume 同源同回退）。"""
    out = []
    try:
        conn = open_db(db_path)
    except (OSError, sqlite3.Error):
        return out
    try:
        if not has_current_schema(conn):
            return out
        rows = conn.execute(
            """
            SELECT s.*, p.worktree AS project_worktree
            FROM session s LEFT JOIN project p ON p.id = s.project_id
            ORDER BY s.time_updated DESC
            """
        ).fetchall()
        for row in rows:
            item = dict(row)
            out.append({
                "session_id": item.get("id"),
                "title": item.get("title") or item.get("slug") or item.get("id"),
                "cwd": item.get("directory") or item.get("project_worktree") or "",
                "created": item.get("time_created"),
                "updated": item.get("time_updated"),
                "db_path": db_path,
            })
    except (OSError, sqlite3.Error):
        pass
    finally:
        conn.close()
    return out


def scan_sessions(mimo_dir):
    """扫描数据目录下所有库的会话，按更新时间倒序。"""
    sessions = []
    for db_path in db_candidates(mimo_dir):
        sessions.extend(db_sessions(db_path))
    sessions.sort(key=lambda s: timestamp_ms(s["updated"]), reverse=True)
    return sessions


# ---------------------------------------------------------------- 记录归一化

def load_rows(db_path, session_id):
    """读取 message / part 原始行（源序：time_created, id）。"""
    conn = open_db(db_path)
    try:
        messages = []
        for row in conn.execute(
                "SELECT id, time_created, data FROM message "
                "WHERE session_id=? ORDER BY time_created, id",
                (session_id,)):
            messages.append({
                "id": row["id"],
                "time_created": row["time_created"],
                "data": parse_json(row["data"]),
            })
        parts_by_message = {}
        for row in conn.execute(
                "SELECT id, message_id, time_created, data FROM part "
                "WHERE session_id=? ORDER BY time_created, id",
                (session_id,)):
            parts_by_message.setdefault(row["message_id"], []).append({
                "id": row["id"],
                "time_created": row["time_created"],
                "data": parse_json(row["data"]),
            })
        return messages, parts_by_message
    finally:
        conn.close()


def model_provider(messages):
    """取会话最后生效的 modelID / providerID（与 resume 同源：顶层优先于 model 对象）。"""
    model = ""
    provider = ""
    for message in messages:
        data = message["data"]
        info = data.get("model") or {}
        model = data.get("modelID") or info.get("modelID") or model
        provider = data.get("providerID") or info.get("providerID") or provider
    return model, provider


def normalize(messages, parts_by_message):
    """message/part 行 -> msgs；严格保持源记录顺序。"""
    msgs = []
    for message in messages:
        data = message["data"]
        role = data.get("role") or ""
        ts = to_iso((data.get("time") or {}).get("created") or message["time_created"])
        # 助手消息级错误：元信息，转注记（resume 的 error_text 分支）
        if role == "assistant" and data.get("error"):
            err = data["error"]
            detail = (err.get("data") or {}).get("message") or err.get("message") or ""
            name = err.get("name") or "Error"
            ets = to_iso((data.get("time") or {}).get("completed")) or ts
            text = f"{name}: {detail}" if detail else str(name)
            msgs.append(cs.m_note(f"助手执行出错：{text}", ets))
        for part in parts_by_message.get(message["id"], []):
            pdata = part["data"]
            ptype = pdata.get("type") or ""
            pts = to_iso((pdata.get("time") or {}).get("start")
                         or part["time_created"] or ts)
            if ptype == "text":
                text = pdata.get("text") or ""
                if not text.strip():
                    continue
                if pdata.get("synthetic") or data.get("summary") is True:
                    msgs.append(cs.m_note(f"历史摘要（compact）：{text}", pts))
                elif role in ("user", "assistant"):
                    msgs.append(cs.m_message(role, text, pts))
            elif ptype == "tool":
                state = pdata.get("state") or {}
                name = pdata.get("tool") or "tool"
                call_id = pdata.get("callID") or ""
                msgs.append(cs.m_tool_use(call_id, name, state.get("input") or {}, pts))
                status = str(state.get("status") or "")
                output = state.get("output")
                error = state.get("error")
                # 结果产出条件与 resume 一致：有 output / error，或 status 已完结
                if output is not None or error is not None or status in ("completed", "error"):
                    if not isinstance(output, str):
                        # 紧凑序列化，保证与 JS 版 JSON.stringify 逐字节一致
                        output = (json.dumps(output, ensure_ascii=False,
                                             separators=(",", ":"))
                                  if output is not None else "")
                    rts = to_iso((state.get("time") or {}).get("end")) or pts
                    msgs.append(cs.m_tool_result(
                        call_id,
                        str(error) if error is not None else output,
                        status == "error" or error is not None,
                        rts))
            elif ptype == "patch":
                files = pdata.get("files") or []
                if files:
                    msgs.append(cs.m_note(
                        "代码补丁涉及文件：" + "、".join(str(f) for f in files), pts))
            elif ptype == "file":
                label = (pdata.get("filename") or (pdata.get("source") or {}).get("path")
                         or pdata.get("url") or "附件")
                msgs.append(cs.m_note(f"用户附件：{label}", pts))
            elif ptype == "compaction":
                summary = (pdata.get("projection") or {}).get("summary") or ""
                if summary.strip():
                    msgs.append(cs.m_note(f"历史摘要（compact）：{summary}", pts))
            elif ptype == "subtask":
                agent = pdata.get("agent") or ""
                desc = pdata.get("description") or ""
                head = f"子任务[{agent}]" if agent else "子任务"
                msgs.append(cs.m_note(f"{head}：{desc}" if desc else head, pts))
            # reasoning / step-start / step-finish / snapshot / retry / agent /
            # checkpoint 等不进入转换
    return msgs


# ---------------------------------------------------------------- 对外接口

def list_sessions(dir_=None, project=None):
    mimo_dir = dir_ or default_dir()
    sessions = scan_sessions(mimo_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    out = []
    for s in sessions[:50]:
        try:
            messages, parts = load_rows(s["db_path"], s["session_id"])
            msgs = normalize(messages, parts)
        except (OSError, sqlite3.Error):
            msgs = []
        out.append({
            "session_id": s["session_id"], "title": s["title"], "cwd": s["cwd"],
            "mtime": timestamp_ms(s["updated"]) / 1000.0, "path": s["db_path"],
            "count": len(msgs), "last_ts": cs.fmt_cst(msgs[-1]["ts"]) if msgs else "",
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    mimo_dir = dir_ or default_dir()
    sessions = scan_sessions(mimo_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id)
                    or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 MiMo 会话（dir={mimo_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "") + "）")

    target = sessions[0]
    messages, parts_by_message = load_rows(target["db_path"], target["session_id"])
    msgs = normalize(messages, parts_by_message)
    if not msgs:
        raise cs.ConvertError(
            f"会话无可转换内容：{target['session_id']}（{target['db_path']}）")

    model, provider = model_provider(messages)
    model = f"{provider}/{model}" if provider else model
    ref = cs.make_ref("mimo", target["session_id"], target["title"],
                      target["cwd"] or "",
                      model=model,
                      started_at=to_iso(target["created"]),
                      ended_at=msgs[-1]["ts"] or to_iso(target["updated"]),
                      source=target["db_path"])
    return ref, msgs
