#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/workbuddy: WorkBuddy（腾讯 AI 智能体工作站）-> 归一化会话。

格式依据（与 resume-workbuddy 一致）：
  <workbuddy_dir>/projects/<编码项目名>/<session_id>.jsonl
  workbuddy_dir 默认 $WORKBUDDY_HOME 或 ~/.workbuddy（dir_ 参数等价 --workbuddy-dir）。
  项目目录名：项目绝对路径中的 : \\ / 替换为 - 并转为小写。
  每行一个事件，type ∈ message / function_call / function_call_result /
  reasoning / ai-title / file-history-snapshot；timestamp 为毫秒级 epoch。
  - 用户真实输入：message(role=user)，优先提取 <user_query>，剥离 system-reminder
  - 模型真实输出：message(role=assistant)，providerData 提供模型名
  - 工具调用：function_call + function_call_result（callId 配对，缺省回退 id）
  - 推理（reasoning）与文件快照不可迁移，跳过；该格式无独立压缩记录类型
标题：ai-title 事件的 aiTitle 优先，否则取首条用户消息首行（60 字符截断）。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402


def default_dir():
    return os.environ.get("WORKBUDDY_HOME",
                          os.path.expanduser(os.path.join("~", ".workbuddy")))


def encode_project_path(path_str):
    """WorkBuddy 把项目绝对路径中的 : \\ / 替换为 - 并转为小写。"""
    return re.sub(r"[:\\/]+", "-", path_str).strip("-").lower()


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def to_iso_ms(ts):
    """WorkBuddy 的 timestamp 为毫秒级 epoch，统一转成 UTC ISO（Z 结尾）。"""
    if ts in (None, ""):
        return ""
    try:
        val = float(str(ts).strip())
        dt = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    except (ValueError, TypeError, OverflowError, OSError):
        return cs.to_iso_z(ts)  # 兜底：已是 ISO 字符串则直接规整


def peek_cwd(jsonl_path, max_lines=30):
    """取文件前若干行里首个 cwd 字段（与 resume-workbuddy.peek_cwd 一致）。"""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        return None
    return None


def parse_events(path):
    """读 JSONL 事件流，坏行跳过。"""
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        raise cs.ConvertError(f"无法读取会话文件：{e}")
    return events


def extract_user_text(content):
    raw = ""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("input_text", "text"):
                parts.append(b.get("text", ""))
        raw = "\n".join(parts)

    # 优先提取 <user_query>
    m = re.search(r"<user_query>([\s\S]*?)</user_query>", raw)
    if m and m.group(1).strip():
        return m.group(1).strip()

    # 兜底：剥离 system-reminder 及其他环境前缀
    stripped = re.sub(r"<system-reminder[\s\S]*?</system-reminder>", "",
                      raw, flags=re.IGNORECASE).strip()
    return stripped or raw.strip()


def extract_assistant_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("output_text", "text"):
                parts.append(b.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return ""


def extract_result_content(output):
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if isinstance(output.get("text"), str):
            return output["text"]
        if isinstance(output.get("content"), str):
            return output["content"]
        return json.dumps(output, ensure_ascii=False)
    if isinstance(output, list):
        return json.dumps(output, ensure_ascii=False)
    return str(output)


def normalize(events):
    """事件流 -> (meta, msgs)。meta 含 cwd/model/ai_title/started_at/ended_at。"""
    msgs = []
    meta = {"cwd": None, "model": None, "ai_title": None,
            "started_at": "", "ended_at": ""}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("cwd") and not meta["cwd"]:
            meta["cwd"] = ev["cwd"]

        etype = ev.get("type")
        if etype == "ai-title":
            if ev.get("aiTitle"):
                meta["ai_title"] = ev["aiTitle"]
            continue
        if etype in ("reasoning", "file-history-snapshot"):
            # 内部推理 / 文件快照，不可迁移，跳过
            continue

        ts = to_iso_ms(ev.get("timestamp", ""))

        if etype == "message":
            role = ev.get("role")
            if role == "user":
                text = extract_user_text(ev.get("content"))
                if text:
                    msgs.append(cs.m_message("user", text, ts))
            elif role == "assistant":
                if not meta["model"] and ev.get("providerData"):
                    prov = ev["providerData"]
                    m = prov.get("model")
                    req_m = prov.get("requestModelName")
                    if m and req_m:
                        meta["model"] = f"{m} ({req_m})"
                    else:
                        meta["model"] = m or req_m or None
                text = extract_assistant_text(ev.get("content"))
                if text:
                    msgs.append(cs.m_message("assistant", text, ts))
            continue

        if etype == "function_call":
            raw_args = ev.get("arguments")
            inp = {}
            if isinstance(raw_args, str):
                try:
                    inp = json.loads(raw_args)
                except json.JSONDecodeError:
                    inp = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                inp = raw_args
            msgs.append(cs.m_tool_use(ev.get("callId") or ev.get("id") or "",
                                      ev.get("name", ""), inp, ts))
            continue

        if etype == "function_call_result":
            is_err = ev.get("status") == "error" or bool(ev.get("is_error", False))
            text = extract_result_content(ev.get("output"))
            msgs.append(cs.m_tool_result(ev.get("callId", ""), text, is_err, ts))
            continue

    # 时间范围：取首末条消息时间（与 resume-workbuddy 的 first_ts/last_ts 一致）
    tss = [m["ts"] for m in msgs if m["ts"]]
    meta["started_at"] = tss[0] if tss else ""
    meta["ended_at"] = tss[-1] if tss else ""
    return meta, msgs


def resolve_title(ai_title, msgs, session_id):
    """与 resume-workbuddy.resolve_title 一致：aiTitle 优先，其次首条用户消息首行。"""
    if ai_title:
        return ai_title
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m["text"].strip():
            first_line = m["text"].strip().splitlines()[0]
            return (first_line[:60] + "…") if len(first_line) > 60 \
                else (first_line or session_id)
    return session_id


def scan_sessions(workbuddy_dir):
    """扫描 projects/<项目目录>/*.jsonl，按 mtime 倒序。"""
    projects_root = os.path.join(workbuddy_dir, "projects")
    sessions = []
    if not os.path.isdir(projects_root):
        return sessions
    try:
        dirs = os.listdir(projects_root)
    except OSError:
        return sessions
    for d in dirs:
        full = os.path.join(projects_root, d)
        if not os.path.isdir(full):
            continue
        try:
            entries = os.listdir(full)
        except OSError:
            continue
        for fn in entries:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(full, fn)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            sessions.append({
                "session_id": fn[:-len(".jsonl")],
                "path": p,
                "cwd": peek_cwd(p) or "",
                "mtime": mtime,
            })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def list_sessions(dir_=None, project=None):
    workbuddy_dir = dir_ or default_dir()
    sessions = scan_sessions(workbuddy_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if s["cwd"] and norm_path(s["cwd"]) == norm]
    out = []
    for s in sessions[:50]:
        try:
            meta, msgs = normalize(parse_events(s["path"]))
        except cs.ConvertError:
            meta, msgs = {"ai_title": None}, []
        out.append({
            "session_id": s["session_id"],
            "title": resolve_title(meta.get("ai_title"), msgs, s["session_id"]),
            "cwd": s["cwd"],
            "mtime": s["mtime"],
            "path": s["path"],
            "count": len(msgs),
            "last_ts": cs.fmt_cst(msgs[-1]["ts"]) if msgs and msgs[-1]["ts"] else "",
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    workbuddy_dir = dir_ or default_dir()
    sessions = scan_sessions(workbuddy_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if s["cwd"] and norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 WorkBuddy 会话（dir={workbuddy_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    meta, msgs = normalize(parse_events(target["path"]))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    sid = target["session_id"]
    title = resolve_title(meta.get("ai_title"), msgs, sid)
    ref = cs.make_ref("workbuddy", sid, title, meta.get("cwd") or target["cwd"] or "",
                      model=meta.get("model") or "",
                      started_at=meta.get("started_at") or "",
                      ended_at=meta.get("ended_at") or "",
                      source=target["path"])
    return ref, msgs
