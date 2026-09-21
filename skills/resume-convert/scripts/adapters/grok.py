#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/grok: Grok Build CLI（~/.grok/sessions/...）-> 归一化会话。

格式依据（与 resume-grok 一致）：
  ~/.grok/sessions/<URL编码的cwd或slug>/<session-id>/summary.json + chat_history.jsonl
  summary.json：info.id / info.cwd、generated_title|title|session_summary（标题优先级）、
  created_at / updated_at（会话起止，行内消息无独立时间戳）。
  chat_history.jsonl（主 transcript，文件行序即时间序）：
    - system / reasoning 行跳过；带 synthetic_reason 的注入消息跳过，
      其中 compaction_meta（压缩摘要）转成转换注记保留
    - user / assistant 文本：字符串或 content block 列表（text/tool_use/tool_result）
    - assistant.tool_calls（OpenAI 风格，arguments 为 JSON 字符串；兼容嵌套
      function{name,arguments} 旧形态与当前 CLI 的顶层 {id,name,arguments} 平铺形态）
    - type=tool 行为工具结果
  chat_history 为空/缺失时兜底 events.jsonl / updates.jsonl（ACP 流）。
与 grok.js 功能等价、输出一致。
"""

import json
import os
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402


def default_dir():
    return os.environ.get("GROK_HOME") or os.path.expanduser(os.path.join("~", ".grok"))


def norm_path(p):
    # 与 resume-grok 一致：反斜杠转正斜杠、去尾斜杠、小写（跨实现可互换）
    return str(p).replace("\\", "/").rstrip("/").lower()


def is_file_safe(p):
    try:
        return os.path.isfile(p)
    except OSError:
        return False


def decode_cwd_dir(name):
    # Grok 把工作目录 URL-encode 后作为会话分组目录名；兜底解码（主来源仍是 summary.info.cwd）
    try:
        return unquote(name)
    except Exception:
        return name


def read_jsonl(p):
    """读 JSONL，坏行跳过；读不到返回空列表（chat_history 缺失属正常，走兜底）。"""
    rows = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return rows
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_summary(session_dir):
    p = os.path.join(session_dir, "summary.json")
    if not is_file_safe(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def summary_cwd(summary, session_dir, dir_name):
    # 会话所属工作目录：info.cwd > git_root_dir > .cwd 文件 > 解码分组目录名
    if isinstance(summary.get("info"), dict) and summary["info"].get("cwd"):
        return summary["info"]["cwd"]
    if summary.get("git_root_dir"):
        return summary["git_root_dir"]
    cwd_file = os.path.join(session_dir, ".cwd")
    if is_file_safe(cwd_file):
        try:
            with open(cwd_file, "r", encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
        except OSError:
            pass
    return decode_cwd_dir(dir_name)


def parse_iso_ms(s):
    if not s:
        return 0
    import datetime
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.timestamp() * 1000.0
    except (ValueError, TypeError):
        return 0


# ---------- 文本抽取与工具入参 ----------

def text_of(content):
    # content 可能是字符串、[{type,text},...] 列表或 {text} 对象，统一抽为纯文本
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b.get("content"), str):
                parts.append(b["content"])
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def parse_json_args(args):
    # OpenAI 风格的 tool_calls[].arguments 通常是 JSON 字符串
    if args is None:
        return {}
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {"_raw": s}
    return args


def normalize_input(name, inp):
    # 按工具名从 input 抽取简洁字段（与 resume-grok 一致）
    if not isinstance(inp, dict):
        inp = {}

    def pick(*ks):
        for k in ks:
            v = inp.get(k)
            if v is not None and v != "":
                return v
        return ""
    if name in ("bash", "Bash"):
        return {"command": pick("command", "cmd", "script")}
    if name in ("read_file", "Read"):
        return {"path": pick("path", "file_path", "filePath")}
    if name in ("write_file", "Write"):
        return {"path": pick("path", "file_path", "filePath")}
    if name in ("search_replace", "Edit"):
        return {"path": pick("path", "file_path", "filePath")}
    if name == "list_dir":
        return {"path": pick("path", "dir", "directory")}
    if name in ("grep_search", "Grep"):
        return {"pattern": pick("pattern", "regex", "query"), "path": pick("path", "directory", "cwd")}
    if name in ("glob", "Glob"):
        return {"pattern": pick("pattern"), "path": pick("path", "directory")}
    if name in ("web_search", "WebSearch"):
        return {"query": pick("query", "q", "searchTerm")}
    if name in ("web_fetch", "FetchURL"):
        return {"url": pick("url", "uri")}
    if name in ("monitor", "Monitor"):
        return {"command": pick("command", "cmd")}
    if name in ("task", "Agent", "subagent"):
        return {"agent_name": pick("agent_name", "subagent_type", "agent", "name"),
                "prompt": pick("prompt", "description", "task", "directive")}
    if name == "todo_write":
        return {"description": pick("description", "content")}
    if name == "memory_search":
        return {"query": pick("query", "q")}
    if name == "memory_get":
        return {"path": pick("path", "file_path")}
    if name in ("image_gen", "image_edit"):
        return {"prompt": pick("prompt", "description")}
    return inp


# ---------- transcript 归一化 ----------

def normalize_chat_rows(rows):
    """chat_history.jsonl -> (msgs, model)。行内无时间戳，保持行序即时间序。"""
    msgs = []
    for msg in rows:
        if not isinstance(msg, dict):
            continue
        typ = msg.get("type") or msg.get("role") or ""

        # system（系统提示）与 reasoning（推理不可跨模型迁移）跳过
        if typ in ("system", "reasoning"):
            continue
        # 注入的合成消息（技能清单 / system-reminder 等）跳过；
        # 压缩摘要（compaction_meta）转成转换注记，恢复时保留先前上下文
        if typ == "user" and msg.get("synthetic_reason"):
            if msg["synthetic_reason"] == "compaction_meta":
                text = text_of(msg.get("content")).strip()
                if text:
                    msgs.append(cs.m_note("会话发生过上下文压缩，摘要如下：\n" + text, ""))
            continue

        # OpenAI 风格：assistant 携带 tool_calls 数组
        if typ == "assistant" and isinstance(msg.get("tool_calls"), list):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                msgs.append(cs.m_message("assistant", content, ""))
            for tc in msg["tool_calls"]:
                tc = tc if isinstance(tc, dict) else {}
                # 兼容两种形态：嵌套 function{name,arguments}（resume-grok 解析的旧格式）
                # 与当前 CLI 实测的顶层平铺 {id,name,arguments}
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                args = fn.get("arguments")
                if args is None:
                    args = tc.get("arguments")
                inp = parse_json_args(args)
                msgs.append(cs.m_tool_use(tc.get("id") or "", name,
                                          normalize_input(name, inp), ""))
            continue
        # OpenAI 风格：type=tool 的工具结果
        if typ == "tool":
            msgs.append(cs.m_tool_result(msg.get("tool_call_id") or "",
                                         text_of(msg.get("content")), False, ""))
            continue
        # Grok 实际写入的顶层工具结果行（{type:"tool_result", tool_call_id, content}）
        if typ == "tool_result":
            msgs.append(cs.m_tool_result(msg.get("tool_call_id") or "",
                                         text_of(msg.get("content")),
                                         msg.get("is_error") is True, ""))
            continue

        content = msg.get("content")
        # Anthropic 风格 content block 列表
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    text = b.get("text") or ""
                    if not text.strip():
                        continue
                    msgs.append(cs.m_message("user" if typ == "user" else "assistant",
                                             text, ""))
                elif bt == "tool_use":
                    name = b.get("name") or ""
                    msgs.append(cs.m_tool_use(b.get("id") or "", name,
                                              normalize_input(name, b.get("input") or {}), ""))
                elif bt == "tool_result":
                    msgs.append(cs.m_tool_result(b.get("tool_use_id") or "",
                                                 text_of(b.get("content")),
                                                 b.get("is_error") is True, ""))
                # thinking / image / 其他 block 跳过
            continue
        # content 为字符串
        if isinstance(content, str) and content.strip():
            msgs.append(cs.m_message("user" if typ == "user" else "assistant", content, ""))
    return msgs, None


def normalize_acp_rows(rows):
    """events.jsonl / updates.jsonl（ACP 流）-> (msgs, model)，chat_history 缺失时兜底。

    每行可能是完整 JSON-RPC 通知 {method:"session/update", params:{update:{...}}}
    或裸 update 对象 {sessionUpdate:"agent_message_chunk", content:{text}}。"""
    msgs = []
    model = None
    for o in rows:
        if not isinstance(o, dict):
            continue
        u = o
        if o.get("method") == "session/update" and isinstance(o.get("params"), dict) \
                and isinstance(o["params"].get("update"), dict):
            u = o["params"]["update"]
        elif isinstance(o.get("update"), dict):
            u = o["update"]
        kind = u.get("sessionUpdate") or u.get("type") or ""
        if not kind:
            continue

        if kind == "agent_message_chunk":
            text = text_of(u.get("content"))
            if text.strip():
                msgs.append(cs.m_message("assistant", text, ""))
        elif kind == "user_message_chunk":
            text = text_of(u.get("content"))
            if text.strip():
                msgs.append(cs.m_message("user", text, ""))
        elif kind == "agent_thought_chunk":
            # 推理片段，跳过
            continue
        elif kind in ("tool_call", "tool"):
            name = u.get("tool") or u.get("name") or ""
            inp = u.get("arguments") or u.get("rawInput") or u.get("input") or {}
            call_id = u.get("id") or u.get("callId") or u.get("toolCallId") or ""
            msgs.append(cs.m_tool_use(call_id, name, normalize_input(name, inp), ""))
            # 完成态携带输出时，同步产出工具结果
            state = u.get("state") or ""
            out = text_of(u.get("rawOutput")) if u.get("rawOutput") else (
                text_of(u.get("content")) if state in ("completed", "failed") else "")
            if out and out.strip():
                msgs.append(cs.m_tool_result(call_id, out, state == "failed", ""))
        elif kind in ("tool_result", "tool_call_result"):
            msgs.append(cs.m_tool_result(
                u.get("toolUseId") or u.get("tool_use_id") or u.get("id") or "",
                text_of(u.get("content") or u.get("output")),
                u.get("isError") is True or u.get("is_error") is True, ""))
        elif kind in ("config", "config.update"):
            if not model and (u.get("modelId") or u.get("model")):
                model = u.get("modelId") or u.get("model")
        # plan / error / metadata 等跳过
    return msgs, model


def load_transcript(session_dir):
    """读会话 transcript：chat_history.jsonl 优先，为空时兜底 events/updates（ACP）。"""
    msgs, model = normalize_chat_rows(
        read_jsonl(os.path.join(session_dir, "chat_history.jsonl")))
    if msgs:
        return msgs, model
    for name in ("events.jsonl", "updates.jsonl"):
        p = os.path.join(session_dir, name)
        if not is_file_safe(p):
            continue
        m2, mo2 = normalize_acp_rows(read_jsonl(p))
        if m2:
            return m2, mo2 or model
    return msgs, model


# ---------- 标题解析 ----------

def resolve_title(summary, msgs, session_id):
    # 主来源：summary 的 generated_title / title / session_summary；兜底首条用户消息首行
    for k in ("generated_title", "title", "session_summary"):
        v = summary.get(k) if summary else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    for m in msgs or []:
        if m.get("kind") == "message" and m.get("role") == "user" \
                and (m.get("text") or "").strip():
            first_line = m["text"].strip().splitlines()[0]
            return (first_line[:60] + "…") if len(first_line) > 60 else first_line
    return session_id


# ---------- 会话扫描 ----------

def scan_sessions(grok_dir):
    """跨项目扫描 ~/.grok/sessions 下全部会话，按 summary.updated_at 倒序。"""
    root = os.path.join(grok_dir, "sessions")
    sessions = []
    try:
        groups = os.listdir(root)
    except OSError:
        return sessions
    for gname in groups:
        group_path = os.path.join(root, gname)
        if not os.path.isdir(group_path):
            continue
        try:
            sids = os.listdir(group_path)
        except OSError:
            continue
        for sname in sids:
            session_dir = os.path.join(group_path, sname)
            if not os.path.isdir(session_dir):
                continue
            summary = load_summary(session_dir)
            sort_key = parse_iso_ms(summary.get("updated_at") or "")
            if not sort_key:
                try:
                    sort_key = os.path.getmtime(session_dir) * 1000.0
                except OSError:
                    sort_key = 0
            sessions.append({
                "session_id": (summary.get("info") or {}).get("id") or sname,
                "session_dir": session_dir,
                "cwd": summary_cwd(summary, session_dir, gname),
                "summary": summary,
                "mtime": sort_key,
            })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


# ---------- 对外接口 ----------

def list_sessions(dir_=None, project=None):
    grok_dir = dir_ or default_dir()
    sessions = scan_sessions(grok_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    out = []
    for s in sessions[:50]:
        msgs, _ = load_transcript(s["session_dir"])
        out.append({
            "session_id": s["session_id"],
            "title": resolve_title(s["summary"], msgs, s["session_id"]),
            "cwd": s["cwd"],
            "mtime": s["mtime"] / 1000.0,
            "path": s["session_dir"],
            "count": len(msgs),
            "last_ts": cs.fmt_cst(s["summary"].get("updated_at") or ""),
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    grok_dir = dir_ or default_dir()
    sessions = scan_sessions(grok_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Grok 会话（dir={grok_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    msgs, model = load_transcript(target["session_dir"])
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['session_dir']}")

    summary = target["summary"]
    sid = target["session_id"]
    title = resolve_title(summary, msgs, sid)
    ref = cs.make_ref(
        "grok", sid, title, target["cwd"] or "",
        model=model or summary.get("current_model_id") or "",
        started_at=summary.get("created_at") or "",
        ended_at=summary.get("updated_at") or "",
        source=target["session_dir"])
    return ref, msgs
