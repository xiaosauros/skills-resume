#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/codex: Codex CLI（~/.codex/sessions rollout JSONL）-> 归一化会话。

格式依据（与 resume-codex 一致）：
  ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<sid>.jsonl
  每行 {timestamp, type, payload}；type ∈ session_meta / turn_context / event_msg /
  response_item / compacted 等。
  - 用户真实输入：event_msg/user_message（response_item 的 role=user 多为环境注入，跳过）
  - 模型真实输出：response_item/message(role=assistant)
  - 工具调用：function_call + function_call_output、custom_tool_call + custom_tool_call_output
  - 推理（reasoning）不可跨模型迁移，跳过
  - compacted / context_compacted -> 转换注记
标题：~/.codex/session_index.jsonl 的 thread_name 优先。
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
    return os.environ.get("CODEX_HOME",
                          os.path.expanduser(os.path.join("~", ".codex")))


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def sid_from_filename(fn):
    base = os.path.basename(fn)
    m = SID_RE.search(base)
    if m:
        return m.group(0)
    base = re.sub(r"^rollout-", "", base, flags=re.IGNORECASE)
    return re.sub(r"\.jsonl$", "", base, flags=re.IGNORECASE)


def peek_cwd(path):
    """session_meta 首行可能极长，用正则取首个 cwd 字段。"""
    try:
        with open(path, "rb") as f:
            head = f.read(32768).decode("utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"cwd"\s*:\s*"([^"]+)"', head)
    return m.group(1) if m else None


def walk_jsonl(dir_):
    out = []
    try:
        entries = os.listdir(dir_)
    except OSError:
        return out
    for name in entries:
        full = os.path.join(dir_, name)
        if os.path.isdir(full):
            out.extend(walk_jsonl(full))
        elif name.endswith(".jsonl"):
            out.append(full)
    return out


def load_session_index(codex_dir):
    """session_id -> thread_name（codex resume 展示的标题）。"""
    idx = os.path.join(codex_dir, "session_index.jsonl")
    mapping = {}
    if not os.path.isfile(idx):
        return mapping
    try:
        with open(idx, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("id") and o.get("thread_name"):
                    mapping[o["id"]] = o["thread_name"]
    except OSError:
        pass
    return mapping


def parse_rollout(path):
    """读 rollout JSONL，返回事件列表（坏行跳过）。"""
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
        raise cs.ConvertError(f"无法读取 rollout 文件：{e}")
    return events


def output_to_text(output):
    """function_call_output.output 统一抽为纯文本。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(b.get("text", "") for b in output
                         if isinstance(b, dict) and b.get("text"))
    if isinstance(output, dict):
        # 紧凑分隔符与 JS JSON.stringify 输出一致，保证双语言指纹相同
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return str(output)


def extract_exec_cmd(input_str):
    """exec 工具 input 是 JS 源码串，提取 cmd 字面量。"""
    if not isinstance(input_str, str):
        return None
    m = re.search(r"""cmd\s*:\s*(['"`])((?:\\.|(?!\1)[\s\S])*?)\1""", input_str)
    if not m:
        return None
    s = m.group(2)
    for a, b in (('\\"', '"'), ("\\'", "'"), ("\\n", "\n"), ("\\t", "\t"), ("\\\\", "\\")):
        s = s.replace(a, b)
    return s


def normalize(events):
    """rollout 事件 -> (ref_parts, msgs)。"""
    msgs = []
    meta = {"session_id": None, "cwd": None, "model": None,
            "started_at": "", "ended_at": ""}
    has_user_from_event = False

    # 第一遍：确认 event_msg/user_message 是否存在（决定 user 文本来源）
    for ev in events:
        if ev.get("type") == "event_msg" and (ev.get("payload") or {}).get("type") == "user_message":
            has_user_from_event = True
            break

    for ev in events:
        top = ev.get("type")
        p = ev.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        ts = ev.get("timestamp", "") or ""
        if ts:
            meta["ended_at"] = ts

        if top == "session_meta":
            meta["session_id"] = meta["session_id"] or p.get("id")
            meta["cwd"] = meta["cwd"] or p.get("cwd")
            if ts and not meta["started_at"]:
                meta["started_at"] = ts
            continue
        if top == "turn_context":
            meta["cwd"] = meta["cwd"] or p.get("cwd")
            meta["model"] = meta["model"] or p.get("model")
            continue

        if top == "event_msg":
            if pt == "user_message" and has_user_from_event:
                text = (p.get("message") or "").strip()
                if text:
                    msgs.append(cs.m_message("user", text, ts))
            elif pt == "context_compacted":
                msgs.append(cs.m_note("会话发生过上下文压缩（context_compacted）", ts))
            continue

        if top == "compacted":
            msgs.append(cs.m_note("会话发生过上下文压缩（compacted）", ts))
            continue

        if top == "response_item":
            if pt == "message":
                if p.get("role") == "assistant":
                    text = output_to_text(p.get("content")).strip()
                    if text:
                        msgs.append(cs.m_message("assistant", text, ts))
                elif p.get("role") == "user" and not has_user_from_event:
                    # 旧版 codex 无 event_msg/user_message 时，从 response_item 取用户消息；
                    # 跳过 '<' 开头的环境注入内容
                    text = output_to_text(p.get("content")).strip()
                    if text and not text.startswith("<"):
                        msgs.append(cs.m_message("user", text, ts))
            elif pt == "function_call":
                args = p.get("arguments")
                if isinstance(args, str) and args:
                    try:
                        input_obj = json.loads(args)
                    except json.JSONDecodeError:
                        input_obj = {"raw": args}
                elif isinstance(args, dict):
                    input_obj = args
                else:
                    input_obj = {}
                msgs.append(cs.m_tool_use(p.get("call_id") or "",
                                          p.get("name") or "function",
                                          input_obj, ts))
            elif pt == "custom_tool_call":
                cmd = extract_exec_cmd(p.get("input"))
                msgs.append(cs.m_tool_use(p.get("call_id") or "",
                                          p.get("name") or "exec",
                                          {"cmd": cmd} if cmd else {"raw": p.get("input")},
                                          ts))
            elif pt in ("function_call_output", "custom_tool_call_output"):
                msgs.append(cs.m_tool_result(p.get("call_id") or "",
                                             output_to_text(p.get("output")),
                                             False, ts))
            elif pt == "web_search_call":
                msgs.append(cs.m_tool_use("", "web_search", {"query": ""}, ts))
            continue

    # ended_at 兜底：取最后一条消息时间
    if msgs:
        meta["ended_at"] = msgs[-1]["ts"] or meta["ended_at"]
    return meta, msgs


def scan_sessions(codex_dir):
    """跨项目扫描全部 rollout，按 mtime 倒序。"""
    root = os.path.join(codex_dir, "sessions")
    sessions = []
    for p in walk_jsonl(root):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        sessions.append({
            "session_id": sid_from_filename(p),
            "path": p,
            "cwd": peek_cwd(p) or "",
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def list_sessions(dir_=None, project=None):
    codex_dir = dir_ or default_dir()
    sessions = scan_sessions(codex_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    index = load_session_index(codex_dir)
    out = []
    for s in sessions[:50]:
        title = index.get(s["session_id"], "")
        count = 0
        last_ts = ""
        if not title:
            try:
                events = parse_rollout(s["path"])
                _, msgs = normalize(events)
                count = len(msgs)
                last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
                title = next((m["text"].strip().splitlines()[0][:60]
                              for m in msgs if m["kind"] == "message" and m["role"] == "user"),
                             s["session_id"])
            except cs.ConvertError:
                title = s["session_id"]
        else:
            try:
                _, msgs = normalize(parse_rollout(s["path"]))
                count = len(msgs)
                last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
            except cs.ConvertError:
                pass
        out.append({
            "session_id": s["session_id"], "title": title, "cwd": s["cwd"],
            "mtime": s["mtime"], "path": s["path"],
            "count": count, "last_ts": last_ts,
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    codex_dir = dir_ or default_dir()
    sessions = scan_sessions(codex_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Codex 会话（dir={codex_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    events = parse_rollout(target["path"])
    meta, msgs = normalize(events)
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    sid = meta.get("session_id") or target["session_id"]
    index = load_session_index(codex_dir)
    title = index.get(sid, "")
    if not title:
        title = next((m["text"].strip().splitlines()[0][:60]
                      for m in msgs if m["kind"] == "message" and m["role"] == "user"),
                     sid)

    ref = cs.make_ref("codex", sid, title, meta.get("cwd") or target["cwd"] or "",
                      model=meta.get("model") or "",
                      started_at=meta.get("started_at") or "",
                      ended_at=meta.get("ended_at") or "",
                      source=target["path"])
    return ref, msgs
