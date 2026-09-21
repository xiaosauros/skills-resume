#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/pi: Pi Coding Agent（~/.pi/agent/sessions 树状 JSONL）-> 归一化会话。

格式依据（与 resume-pi 一致）：
  ~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl
  <encoded-cwd> = `--` + cwd 去掉开头分隔符、把 / \\ : 替换为 - + `--`
  首行为 {type:"session"} 头（id/cwd/timestamp），其后条目经 id/parentId 构成树，
  当前对话 = 从最后一个叶子条目沿 parentId 回溯到根的激活路径（分支会话取主线）。
  - 用户/助手消息：message 条目 role=user / assistant（assistant 的 content 为块列表，
    text 块为文本、toolCall 块为工具调用；thinking 块推理不可跨模型迁移，跳过）
  - 工具结果：role=toolResult（toolCallId / content / isError）
  - TUI 内以 ! 直接执行的命令：role=bashExecution -> bash 工具调用 + 结果
  - 压缩/分支摘要：compaction / branch_summary 条目与 compactionSummary / branchSummary role
  - 扩展消息：custom_message 条目与 role=custom -> 转换注记（非对话内容）
  - session_info.name 为会话标题；model_change 提供 provider/modelId
dir 参数兼容 ~/.pi 这一级或 ~/.pi/agent 这一级（与 resume-pi 的 --pi-dir 一致）。
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

# 与 Python str.splitlines() 对齐的「取首行」换行集合（含 \v \f \x1c-\x1e \x85 等），
# 与 resume_pi.py / resume_pi.js 保持一致
LINE_BREAK_RE = re.compile(r"[\n\r\v\f\x1c\x1d\x1e\x85  ]")


def expand_tilde(p):
    if p == "~":
        return os.path.expanduser("~")
    if p.startswith("~/") or p.startswith("~\\"):
        return os.path.join(os.path.expanduser("~"), p[2:])
    return p


def default_dir():
    """pi 的 agent 目录：默认 ~/.pi/agent，可用 PI_CODING_AGENT_DIR 覆盖。"""
    env = os.environ.get("PI_CODING_AGENT_DIR")
    if env:
        return expand_tilde(env)
    return os.path.join(os.path.expanduser("~"), ".pi", "agent")


def is_dir_safe(p):
    return os.path.isdir(p)


def resolve_sessions_root(pi_dir):
    """把 dir（~/.pi 这一级或 agent 这一级）归一为 sessions 根目录。"""
    if pi_dir:
        candidates = [
            os.path.join(pi_dir, "agent", "sessions"),
            os.path.join(pi_dir, "sessions"),
        ]
    else:
        candidates = [os.path.join(default_dir(), "sessions")]
    for c in candidates:
        if is_dir_safe(c):
            return c
    return candidates[0]


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def session_id_from_filename(p):
    """从文件名推断会话 UUID：<timestamp>_<uuid>.jsonl -> uuid。"""
    base = os.path.splitext(os.path.basename(p))[0]
    idx = base.rfind("_")
    return base[idx + 1:] if idx >= 0 else base


def peek_header(path):
    """读取会话文件前几行，取 {type:"session"} 头的 id 与 cwd。"""
    sid, cwd = None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for _ in range(10):
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
                if obj.get("id") and not sid:
                    sid = obj["id"]
                if obj.get("cwd") and not cwd:
                    cwd = obj["cwd"]
                if sid and cwd:
                    break
    except OSError:
        pass
    return sid, cwd


def parse_jsonl(path):
    """读会话 JSONL，返回事件列表（坏行跳过）。"""
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


def extract_text(content):
    """content 可能是 str 或 block 列表，只取 text block。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def first_line(s):
    return LINE_BREAK_RE.split(str(s))[0]


def active_path(events):
    """沿最后一个叶子条目回溯 parentId 到根，得到当前激活的对话路径（支持分支）。"""
    by_id = {}
    for ev in events:
        if isinstance(ev, dict) and ev.get("id"):
            by_id[ev["id"]] = ev
    leaf = None
    for ev in reversed(events):
        if isinstance(ev, dict) and ev.get("id"):
            leaf = ev
            break
    if leaf is None:
        return []
    chain = []
    seen = set()
    cur = leaf
    while isinstance(cur, dict) and cur.get("id") and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        cur = by_id.get(cur.get("parentId")) if cur.get("parentId") else None
    chain.reverse()
    return chain


def custom_note(text, custom_type):
    """扩展消息统一转成转换注记（保留 customType 便于识别来源）。"""
    tag = f"（{custom_type}）" if custom_type else ""
    return f"扩展消息{tag}：{text}"


def normalize(events):
    """激活路径上的条目 -> (meta, msgs)。"""
    msgs = []
    meta = {"session_id": None, "title": None, "cwd": None, "model": None,
            "started_at": "", "ended_at": ""}

    # session 头没有 parentId，不在激活路径上，直接取全文件首个 {type:"session"} 条目。
    header = None
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "session":
            header = ev
            break

    path = active_path(events)
    if not path:
        path = events  # 兼容无 id/parentId 的旧格式：按文件顺序线性处理

    for ev in path:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        if etype == "session":
            continue
        if etype == "session_info":
            if ev.get("name"):
                meta["title"] = ev["name"]
            continue
        if etype in ("compaction", "branch_summary"):
            if ev.get("summary"):
                msgs.append(cs.m_note(f"历史摘要：{ev['summary']}",
                                      ev.get("timestamp", "") or ""))
            continue
        if etype == "model_change":
            provider, model_id = ev.get("provider") or "", ev.get("modelId") or ""
            if model_id:
                meta["model"] = f"{provider}/{model_id}" if provider else model_id
            continue
        if etype == "thinking_level_change":
            continue
        if etype not in ("message", "custom_message"):
            continue  # label / custom 等其他条目类型忽略

        ts = ev.get("timestamp", "") or ""

        if etype == "custom_message":
            text = extract_text(ev.get("content"))
            if text.strip():
                msgs.append(cs.m_note(custom_note(text, ev.get("customType") or ""), ts))
            continue

        msg = ev.get("message") or {}
        role = msg.get("role")

        if role == "user":
            text = extract_text(msg.get("content"))
            if text.strip():
                msgs.append(cs.m_message("user", text, ts))
        elif role == "assistant":
            content = msg.get("content")
            content = content if isinstance(content, list) else []
            text_only = ""
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    if block.get("text"):
                        text_only += ("\n" if text_only else "") + block["text"]
                elif block.get("type") == "toolCall":
                    # call_id 用源真实 id，没有传 ""
                    msgs.append(cs.m_tool_use(block.get("id") or "",
                                              block.get("name") or "",
                                              block.get("arguments") or {}, ts))
                # thinking block（推理）不可跨模型迁移，跳过
            if text_only.strip():
                msgs.append(cs.m_message("assistant", text_only, ts))
            if msg.get("errorMessage"):
                msgs.append(cs.m_note(f"助手错误信息：{msg['errorMessage']}", ts))
        elif role == "toolResult":
            msgs.append(cs.m_tool_result(
                msg.get("toolCallId") or "",
                extract_text(msg.get("content")),
                bool(msg.get("isError") or msg.get("is_error")), ts))
        elif role == "bashExecution":
            # 用户在 TUI 里以 ! / !! 直接执行的 shell 命令
            cmd = msg.get("command") or ""
            msgs.append(cs.m_tool_use("", "bash", {"command": cmd}, ts))
            exit_code = msg.get("exitCode")
            is_err = bool(msg.get("cancelled")) or (0 if exit_code is None else exit_code) != 0
            msgs.append(cs.m_tool_result("", msg.get("output") or "", is_err, ts))
        elif role == "custom":
            text = extract_text(msg.get("content"))
            if text.strip():
                msgs.append(cs.m_note(custom_note(text, msg.get("customType") or ""), ts))
        elif role in ("compactionSummary", "branchSummary"):
            if msg.get("summary"):
                msgs.append(cs.m_note(f"历史摘要：{msg['summary']}", ts))
        # image / 其他 role 忽略

    if header:
        meta["session_id"] = header.get("id")
        meta["cwd"] = header.get("cwd")
        meta["started_at"] = header.get("timestamp", "") or ""
    if msgs:
        meta["ended_at"] = msgs[-1]["ts"] or meta["started_at"]
    return meta, msgs


def resolve_title(title, msgs, sid):
    """session_info.name > 首条用户消息摘要 > 会话 ID。"""
    if title:
        return title
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m["text"].strip():
            t = first_line(m["text"].strip())
            return (t[:60] + "…") if len(t) > 60 else (t or sid)
    return sid


def scan_sessions(root):
    """跨项目扫描 sessions 根目录下所有项目目录的会话，按 mtime 倒序。"""
    sessions = []
    if not is_dir_safe(root):
        return sessions
    for d in os.listdir(root):
        full = os.path.join(root, d)
        if not is_dir_safe(full):
            continue
        for fn in os.listdir(full):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(full, fn)
            if not os.path.isfile(p):
                continue
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            sid, cwd = peek_header(p)
            sessions.append({
                "session_id": sid or session_id_from_filename(p),
                "path": p,
                "cwd": cwd or "",
                "mtime": mtime,
            })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def list_sessions(dir_=None, project=None):
    root = resolve_sessions_root(dir_)
    sessions = scan_sessions(root)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if s["cwd"] and norm_path(s["cwd"]) == norm]
    out = []
    for s in sessions[:50]:
        meta, msgs = {}, []
        try:
            meta, msgs = normalize(parse_jsonl(s["path"]))
        except cs.ConvertError:
            pass
        sid = meta.get("session_id") or s["session_id"]
        out.append({
            "session_id": sid,
            "title": resolve_title(meta.get("title"), msgs, sid),
            "cwd": s["cwd"],
            "mtime": s["mtime"],
            "path": s["path"],
            "count": len(msgs),
            "last_ts": cs.fmt_cst(msgs[-1]["ts"]) if msgs else "",
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    root = resolve_sessions_root(dir_)
    sessions = scan_sessions(root)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if s["cwd"] and norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Pi 会话（dir={root}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    meta, msgs = normalize(parse_jsonl(target["path"]))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    sid = meta.get("session_id") or target["session_id"]
    title = resolve_title(meta.get("title"), msgs, sid)
    ref = cs.make_ref("pi", sid, title, meta.get("cwd") or target["cwd"] or "",
                      model=meta.get("model") or "",
                      started_at=meta.get("started_at") or "",
                      ended_at=meta.get("ended_at") or "",
                      source=target["path"])
    return ref, msgs
