#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/copilot: GitHub Copilot CLI（~/.copilot/session-state）-> 归一化会话。

格式依据（与 resume-copilot 一致）：
  ~/.copilot/session-state/<目录名含UUID>/workspace.yaml + events.jsonl
  events.jsonl 每行 {type, data, timestamp}，type ∈ session.start / user.message /
  assistant.message / tool.execution_complete / permission.* / session.compaction_* 等。
  - 用户真实输入：user.message 的 content（transformedContent 含注入上下文，不用）
  - 模型真实输出：assistant.message 的 content + toolRequests（工具调用）
  - 工具结果：tool.execution_complete 为权威结果；permission.completed 仅当该调用
    没有执行结果时作兜底（真实数据里两者同现，都记会造成同 call_id 双结果，无法配对）
  - 推理（reasoningText / reasoningOpaque）不可跨模型迁移，跳过
  - session.compaction_complete -> 转换注记（携带压缩摘要，是压缩前上下文的唯一线索）
标题：workspace.yaml 的 name 优先，其次首条用户消息首行。
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

SID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def default_dir():
    return os.environ.get("COPILOT_HOME",
                          os.path.expanduser(os.path.join("~", ".copilot")))


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def is_file_safe(p):
    try:
        return os.path.isfile(p)
    except OSError:
        return False


def is_dir_safe(p):
    try:
        return os.path.isdir(p)
    except OSError:
        return False


def list_session_dirs(copilot_dir):
    root = os.path.join(copilot_dir, "session-state")
    out = []
    if not is_dir_safe(root):
        return out
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if not is_dir_safe(full):
            continue
        m = SID_RE.search(name)
        if not m:
            continue
        out.append({"session_id": m.group(0), "dir": full})
    return out


def parse_workspace_yaml(text):
    """字段少、无嵌套，简单按行解析即可，不引入 yaml 依赖。"""
    obj = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip()
        val = line[idx + 1:].strip()
        if val == "true":
            val = True
        elif val == "false":
            val = False
        elif re.match(r"^\d+$", val):
            val = int(val)
        obj[key] = val
    return obj


def load_workspace(session_dir):
    p = os.path.join(session_dir, "workspace.yaml")
    if not is_file_safe(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return parse_workspace_yaml(f.read())
    except OSError:
        return None


def get_mtime(session_dir):
    # 优先用 events.jsonl 的 mtime，回退 workspace.yaml，再回退目录本身。
    candidates = [
        os.path.join(session_dir, "events.jsonl"),
        os.path.join(session_dir, "workspace.yaml"),
        session_dir,
    ]
    for p in candidates:
        try:
            return os.path.getmtime(p)
        except OSError:
            pass
    return 0


def scan_sessions(copilot_dir):
    """扫描全部会话目录（workspace.yaml 缺失也保留，cwd 记空串），按 mtime 倒序。"""
    sessions = []
    for s in list_session_dirs(copilot_dir):
        ws = load_workspace(s["dir"])
        sessions.append({
            "session_id": s["session_id"],
            "dir": s["dir"],
            "path": os.path.join(s["dir"], "events.jsonl"),
            "cwd": (ws or {}).get("cwd", ""),
            "mtime": get_mtime(s["dir"]),
            "workspace": ws,
        })
    sessions.sort(key=lambda x: x["mtime"], reverse=True)
    return sessions


def parse_events(jsonl_path):
    """读 events.jsonl，返回事件列表（坏行跳过）。"""
    events = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        raise cs.ConvertError(f"无法读取 events.jsonl：{e}")
    return events


def output_to_text(output):
    """工具结果统一抽为纯文本；图片块不可跨工具迁移，转占位符说明。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if str(output.get("type") or "") == "image":
            return "[图片已省略]"
        try:
            return json.dumps(output, ensure_ascii=False, indent=2)
        except TypeError:
            return str(output)
    if isinstance(output, list):
        return "\n".join(p for p in (output_to_text(b) for b in output) if p)
    return str(output)


def first_user_title(msgs, fallback):
    """首条用户消息首行作为标题（与 resume-copilot 的截断规则一致）。"""
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and (m["text"] or "").strip():
            first = m["text"].strip().splitlines()[0]
            return first if len(first) <= 60 else first[:60] + "…"
    return fallback


def normalize(events):
    """events.jsonl 事件 -> (meta, msgs)。msgs 严格保持源事件时间顺序。"""
    msgs = []
    meta = {"session_id": None, "cwd": None, "model": None,
            "started_at": "", "ended_at": ""}

    # 第一遍：收集 assistant.message 已声明的工具调用 id，用于判断 permission
    # 流程是否为某个 shell 调用的唯一记录（旧格式兜底）。
    request_call_ids = set()
    for ev in events:
        if ev.get("type") == "assistant.message":
            for tr in (ev.get("data") or {}).get("toolRequests") or []:
                if isinstance(tr, dict) and tr.get("toolCallId"):
                    request_call_ids.add(tr["toolCallId"])

    pending_perm_results = {}  # permission.completed 兜底结果：call_id -> (content, ts)
    open_calls = {}            # 原始 call_id -> 该调用待配对的派生 id 队列（FIFO）
    used_call_ids = set()      # 已分配出去的 call_id（含派生 id）
    emitted_result_ids = set() # 已产出结果的原始 call_id（重复结果事件去重）

    def emit_tool_use(base, name, input_obj, ts):
        # Copilot 在上下文压缩后会重置工具调用计数器，同一 call_id 会被
        # 完全不同的调用复用（真实数据实测）；这里给复用的 id 派生唯一后缀，
        # 保证每个调用与结果能一一配对。
        cid = base or ""
        if cid:
            if cid in used_call_ids:
                n = 2
                while cid + "-r%d" % n in used_call_ids:
                    n += 1
                cid = cid + "-r%d" % n
            used_call_ids.add(cid)
            open_calls.setdefault(base, []).append(cid)
        msgs.append(cs.m_tool_use(cid, name, input_obj, ts))

    def emit_tool_result(base, content, is_error, ts):
        # 结果按 FIFO 配对到最早的未决调用；无未决调用时交由共享库作孤立结果降级。
        queue = open_calls.get(base) if base else None
        if not queue and base and base in emitted_result_ids:
            return  # 同一调用的重复结果事件，跳过
        cid = queue.pop(0) if queue else (base or "")
        if base:
            emitted_result_ids.add(base)
        msgs.append(cs.m_tool_result(cid, content, is_error, ts))

    for ev in events:
        etype = ev.get("type")
        data = ev.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        ts = ev.get("timestamp", "") or ""
        if ts:
            meta["ended_at"] = ts

        if etype == "session.start":
            if not meta["session_id"] and data.get("sessionId"):
                meta["session_id"] = data["sessionId"]
            ctx = data.get("context") or {}
            if not meta["cwd"] and ctx.get("cwd"):
                meta["cwd"] = ctx["cwd"]
            if ts and not meta["started_at"]:
                meta["started_at"] = ts
            continue

        if etype == "session.model_change":
            if data.get("newModel"):
                meta["model"] = data["newModel"]
            elif data.get("previousModel") and not meta["model"]:
                meta["model"] = data["previousModel"]
            continue

        if etype in ("session.info", "session.error"):
            message = str(data.get("message") or "").strip()
            if message:
                prefix = "[会话错误] " if etype == "session.error" else ""
                msgs.append(cs.m_note(prefix + message, ts))
            continue

        if etype == "session.compaction_complete":
            summary = str(data.get("summaryContent") or "").strip()
            if summary:
                msgs.append(cs.m_note("会话发生过上下文压缩，摘要如下：\n" + summary, ts))
            else:
                msgs.append(cs.m_note("会话发生过上下文压缩", ts))
            continue

        if etype == "user.message":
            text = str(data.get("content") or "").strip()
            if text:
                msgs.append(cs.m_message("user", text, ts))
            continue

        if etype == "assistant.message":
            content = str(data.get("content") or "").strip()
            if content:
                msgs.append(cs.m_message("assistant", content, ts))
            if data.get("model"):
                meta["model"] = data["model"]
            for tr in data.get("toolRequests") or []:
                if not isinstance(tr, dict):
                    continue
                emit_tool_use(tr.get("toolCallId") or "",
                              tr.get("name", ""),
                              tr.get("arguments") or {}, ts)
            continue

        if etype == "tool.execution_complete":
            emit_tool_result(data.get("toolCallId") or "",
                             output_to_text(data.get("result")),
                             data.get("success") is False, ts)
            continue

        if etype == "permission.requested":
            pr = data.get("permissionRequest") or {}
            cid = pr.get("toolCallId") or data.get("requestId") or ""
            # 仅当该 shell 调用未出现在 assistant.message.toolRequests 时补一条
            # bash 调用（旧格式兜底；新格式里它与 toolRequests 重复，跳过）。
            if (pr.get("kind") == "shell" and pr.get("fullCommandText")
                    and cid and cid not in request_call_ids):
                emit_tool_use(cid, "bash", {"command": pr["fullCommandText"]}, ts)
            continue

        if etype == "permission.completed":
            cid = data.get("toolCallId") or data.get("requestId") or ""
            if cid and cid not in pending_perm_results:
                pending_perm_results[cid] = (output_to_text(data.get("result")), ts)
            continue

        # tool.execution_start 与 toolRequests 重复；assistant.turn_* / system.message /
        # session.shutdown / session.usage_checkpoint / session.task_complete /
        # session.mode_changed / session.compaction_start 为遥测或非对话内容，跳过。

    # permission.completed 兜底结果：仅当该调用没有任何执行结果时补上，
    # 保持时间顺序语义（真实数据里它通常与 tool.execution_complete 同现，跳过）。
    merged = []
    consumed = set()
    for m in msgs:
        merged.append(m)
        if m["kind"] == "tool_use" and m["call_id"] and m["call_id"] not in consumed:
            base = m["call_id"]
            if base in pending_perm_results and base not in emitted_result_ids:
                content, ts = pending_perm_results.pop(base)
                consumed.add(base)
                merged.append(cs.m_tool_result(base, content, False, ts))
    # 剩余兜底结果无可配对的调用（或已被执行结果覆盖），丢弃。

    if merged:
        meta["ended_at"] = merged[-1]["ts"] or meta["ended_at"]
    return meta, merged


def list_sessions(dir_=None, project=None):
    copilot_dir = dir_ or default_dir()
    sessions = scan_sessions(copilot_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    out = []
    for s in sessions[:50]:
        sid = s["session_id"]
        ws = s["workspace"] or {}
        title = ws.get("name") or ""
        count = 0
        last_ts = ""
        try:
            _, msgs = normalize(parse_events(s["path"]))
            count = len(msgs)
            last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
            if not title:
                title = first_user_title(msgs, "")
        except cs.ConvertError:
            pass
        if not title:
            title = sid
        out.append({
            "session_id": sid, "title": title, "cwd": s["cwd"],
            "mtime": s["mtime"], "path": s["path"],
            "count": count, "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    copilot_dir = dir_ or default_dir()
    sessions = scan_sessions(copilot_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Copilot CLI 会话（dir={copilot_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    meta, msgs = normalize(parse_events(target["path"]))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    ws = target["workspace"] or {}
    sid = ws.get("id") or meta.get("session_id") or target["session_id"]
    title = ws.get("name") or first_user_title(msgs, "") or sid
    cwd = ws.get("cwd") or meta.get("cwd") or target["cwd"] or ""

    ref = cs.make_ref("copilot", sid, title, cwd,
                      model=meta.get("model") or "",
                      started_at=meta.get("started_at") or "",
                      ended_at=meta.get("ended_at") or "",
                      source=target["path"])
    return ref, msgs
