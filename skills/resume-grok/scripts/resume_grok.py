#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""resume-grok: 读取 Grok Build CLI 本地会话记录（~/.grok 下的
sessions/<encoded-cwd>/<session-id>/summary.json + chat_history.jsonl
[+ events.jsonl / updates.jsonl]），生成结构化「接管摘要」。

仅用标准库，任意 agent（Claude Code / Codex / Kimi 等）均可直接调用：
  python resume_grok.py [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]

与同目录 resume_grok.js 功能等价、输出可互换。用法见同目录 SKILL.md。
Windows 中文环境建议：python -X utf8 resume_grok.py ...
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

# 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude/codex/cursor/kimi 一致）
CST_OFFSET_MS = 8 * 3600 * 1000


# ---------- 路径与项目定位 ----------

def get_grok_dir():
    return os.environ.get("GROK_HOME") or os.path.join(os.path.expanduser("~"), ".grok")


def norm_path(p):
    # 归一化路径用于比对：反斜杠转正斜杠、去尾斜杠、小写。
    # 与 Node 实现完全一致，保证跨实现可互换。
    return str(p).replace("\\", "/").rstrip("/").lower()


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


def decode_cwd_dir(name):
    # Grok 把工作目录 URL-encode 后作为会话分组目录名；这里做兜底解码
    # （主来源仍是 summary.info.cwd）。
    try:
        return unquote(name)
    except Exception:
        return name


# ---------- summary.json（会话元数据 / 索引） ----------

def load_summary(session_dir):
    p = os.path.join(session_dir, "summary.json")
    if not is_file_safe(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def summary_cwd(summary, session_dir, dir_name):
    # 从 summary 解析会话所属工作目录（匹配 --project 的主来源）。
    if summary and isinstance(summary.get("info"), dict) and summary["info"].get("cwd"):
        return summary["info"]["cwd"]
    if summary and summary.get("git_root_dir"):
        return summary["git_root_dir"]
    # 兜底 1：同目录的 .cwd 文件（编码名超长时 Grok 写入）
    cwd_file = os.path.join(session_dir, ".cwd")
    if is_file_safe(cwd_file):
        try:
            with open(cwd_file, "r", encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
        except Exception:
            pass
    # 兜底 2：解码分组目录名
    return decode_cwd_dir(dir_name)


# ---------- 时间格式化（同时支持 epoch 毫秒与 ISO 字符串） ----------

def fmt_time(ts):
    if ts is None or ts == "":
        return ""
    import datetime
    try:
        if isinstance(ts, (int, float)):
            d = datetime.datetime.fromtimestamp(ts / 1000.0, tz=datetime.timezone.utc)
        else:
            s = str(ts).strip()
            n = _to_number(s)
            if n is not None:
                d = datetime.datetime.fromtimestamp(n / 1000.0, tz=datetime.timezone.utc)
            else:
                # 兼容 ISO 字符串（含/不含 Z、含小数秒）
                t = s.replace("Z", "+00:00")
                d = datetime.datetime.fromisoformat(t)
        d = d + datetime.timedelta(milliseconds=CST_OFFSET_MS)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _to_number(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_iso_ms(s):
    if not s:
        return 0
    import datetime
    try:
        t = str(s).replace("Z", "+00:00")
        d = datetime.datetime.fromisoformat(t)
        return d.timestamp() * 1000.0
    except Exception:
        return 0


# ---------- JSONL 读取 ----------

def read_jsonl(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return [], str(e)
    rows = []
    for line in content.split("\n"):
        l = line.strip()
        if not l:
            continue
        try:
            rows.append(json.loads(l))
        except Exception:
            continue
    return rows, None


# ---------- 文本抽取 ----------

def text_of(content):
    # content 可能是字符串、[{type,text},...] 列表或 {text} 对象，统一抽为纯文本。
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
    # OpenAI 风格的 tool_call.function.arguments 通常是 JSON 字符串。
    if args is None:
        return {}
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {"_raw": s}
    return args


# ---------- 工具输入归一化 ----------

# 按工具名从 input 抽取简洁字段（与 resume-kimi 的 normalizeInput 思路一致）。
def normalize_input(name, inp):
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


# ---------- chat_history.jsonl 解析（主 transcript 来源） ----------

# Grok 的 chat_history.jsonl 是发给模型的原始消息（Anthropic 风格 content block）。
# 每行：{type:"system"|"user"|"assistant"|..., content:str|[block...], synthetic_reason?}
def normalize_chat(rows):
    items = []  # kind ∈ user_text/assistant_text/tool_use/tool_result
    for msg in rows:
        if not isinstance(msg, dict):
            continue
        typ = msg.get("type") or msg.get("role") or ""

        # system 消息（系统提示）跳过
        if typ == "system":
            continue
        # 注入的合成消息（技能清单 / system-reminder 等）跳过，不计入 goal / transcript
        if typ == "user" and msg.get("synthetic_reason"):
            continue

        # OpenAI 风格：assistant 携带 tool_calls 数组
        if typ == "assistant" and isinstance(msg.get("tool_calls"), list):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                items.append({"kind": "assistant_text", "timestamp": "", "text": content})
            for tc in msg["tool_calls"]:
                fn = (tc or {}).get("function") or {}
                name = fn.get("name") or ""
                inp = parse_json_args(fn.get("arguments"))
                items.append({"kind": "tool_use", "timestamp": "", "name": name,
                              "input": normalize_input(name, inp), "call_id": tc.get("id") or ""})
            continue
        # OpenAI 风格：role "tool" 的工具结果
        if typ == "tool":
            items.append({"kind": "tool_result", "timestamp": "",
                          "call_id": msg.get("tool_call_id") or "",
                          "content": text_of(msg.get("content")), "is_error": False})
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
                    items.append({"kind": "user_text" if typ == "user" else "assistant_text",
                                  "timestamp": "", "text": text})
                elif bt == "tool_use":
                    name = b.get("name") or ""
                    items.append({"kind": "tool_use", "timestamp": "", "name": name,
                                  "input": normalize_input(name, b.get("input") or {}),
                                  "call_id": b.get("id") or ""})
                elif bt == "tool_result":
                    items.append({"kind": "tool_result", "timestamp": "",
                                  "call_id": b.get("tool_use_id") or "",
                                  "content": text_of(b.get("content")),
                                  "is_error": b.get("is_error") is True})
                # thinking / image / 其他 block 跳过
            continue
        # content 为字符串
        if isinstance(content, str) and content.strip():
            items.append({"kind": "user_text" if typ == "user" else "assistant_text",
                          "timestamp": "", "text": content})
    return {"items": items, "model": None}


# ---------- events.jsonl / updates.jsonl 解析（ACP 流，兜底 transcript） ----------

# 每行是一个 ACP session/update 事件：可能是完整 JSON-RPC 通知
#   {method:"session/update", params:{update:{sessionUpdate,...}}}
# 也可能是裸 update 对象 {sessionUpdate:"agent_message_chunk", content:{text}}。
def normalize_acp(rows):
    items = []
    model = None
    for o in rows:
        if not isinstance(o, dict):
            continue
        u = o
        if o.get("method") == "session/update" and isinstance(o.get("params"), dict) and isinstance(o["params"].get("update"), dict):
            u = o["params"]["update"]
        elif isinstance(o.get("update"), dict):
            u = o["update"]
        kind = u.get("sessionUpdate") or u.get("type") or ""
        if not kind:
            continue

        if kind == "agent_message_chunk":
            text = text_of(u.get("content"))
            if text.strip():
                items.append({"kind": "assistant_text", "timestamp": "", "text": text})
        elif kind == "user_message_chunk":
            text = text_of(u.get("content"))
            if text.strip():
                items.append({"kind": "user_text", "timestamp": "", "text": text})
        elif kind == "agent_thought_chunk":
            # 推理片段，跳过（与 kimi 跳过 think / claude 跳过 thinking 一致）
            continue
        elif kind in ("tool_call", "tool"):
            name = u.get("tool") or u.get("name") or ""
            inp = u.get("arguments") or u.get("rawInput") or u.get("input") or {}
            call_id = u.get("id") or u.get("callId") or u.get("toolCallId") or ""
            items.append({"kind": "tool_use", "timestamp": "", "name": name,
                          "input": normalize_input(name, inp), "call_id": call_id})
            state = u.get("state") or ""
            out = text_of(u.get("rawOutput")) if u.get("rawOutput") else (
                text_of(u.get("content")) if state in ("completed", "failed") else "")
            if out and out.strip():
                items.append({"kind": "tool_result", "timestamp": "", "call_id": call_id,
                              "content": out, "is_error": state == "failed"})
        elif kind in ("tool_result", "tool_call_result"):
            items.append({"kind": "tool_result", "timestamp": "",
                          "call_id": u.get("toolUseId") or u.get("tool_use_id") or u.get("id") or "",
                          "content": text_of(u.get("content") or u.get("output")),
                          "is_error": u.get("isError") is True or u.get("is_error") is True})
        elif kind in ("config", "config.update"):
            if not model and (u.get("modelId") or u.get("model")):
                model = u.get("modelId") or u.get("model")
        # plan / error / metadata 等跳过
    return {"items": items, "model": model}


# ---------- 标题解析 ----------

def resolve_title(summary, items, session_id):
    # 主来源：summary 的 generated_title / title / session_summary（Grok 模型生成的标题与摘要）。
    for k in ("generated_title", "title", "session_summary"):
        v = summary.get(k) if summary else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 兜底：首条用户消息首行（按码点截断 60 字符）或会话 ID
    for it in items:
        if it.get("kind") == "user_text":
            trimmed = it["text"].strip()
            first_line = trimmed.split("\r\n")[0].split("\r")[0].split("\n")[0] if trimmed else ""
            return (first_line[:60] + "…") if len(first_line) > 60 else (first_line or session_id)
    return session_id


# ---------- 工具输入摘要 ----------

def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def tool_brief(name, inp):
    # 单行紧凑描述一个工具调用（用于「更早活动」列表）。
    inp = inp or {}

    def c(v, n=80):
        return truncate(str(v or "").replace("\n", " "), n)
    if name in ("bash", "Bash"):
        return f"bash({c(inp.get('command'), 100)})"
    if name in ("read_file", "Read"):
        return f"{name}({c(inp.get('path'), 80)})"
    if name in ("write_file", "Write", "search_replace", "Edit"):
        return f"{name}({c(inp.get('path'), 80)})"
    if name == "list_dir":
        return f"list_dir({c(inp.get('path'), 80)})"
    if name in ("grep_search", "Grep"):
        return f"{name}({c(inp.get('pattern'), 60)})"
    if name in ("glob", "Glob"):
        return f"{name}({c(inp.get('pattern'), 60)})"
    if name in ("task", "Agent", "subagent"):
        return f"{name}({c(inp.get('agent_name'), 40)})"
    if name in ("web_search", "WebSearch"):
        return f"{name}({c(inp.get('query'), 60)})"
    if name in ("web_fetch", "FetchURL"):
        return f"{name}({c(inp.get('url'), 60)})"
    if name in ("monitor", "Monitor"):
        return f"{name}({c(inp.get('command'), 60)})"
    for v in inp.values():
        if isinstance(v, str) and v:
            return f"{name}({c(v, 80)})"
    return f"{name}(...)"


# ---------- 任务状态重建 ----------

TEST_CMD_RE = re.compile(r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b", re.IGNORECASE | re.ASCII)
TEST_RESULT_RE = re.compile(r"(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)", re.IGNORECASE | re.ASCII)
ERR_RE = re.compile(r"(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)", re.IGNORECASE | re.ASCII)
ERR_NEG_RE = re.compile(r"no error|0 failed", re.IGNORECASE | re.ASCII)
READ_TOOLS = {"read_file", "Read", "list_dir", "grep_search", "Grep", "glob", "Glob"}
EDIT_TOOLS = {"write_file", "Write", "search_replace", "Edit"}


def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_state(items):
    # 从归一化条目提取结构化任务状态。
    files_read, files_edited, commands, test_results = [], [], [], []
    first_user = last_user = last_assistant = ""
    last_tool_name = last_command = ""
    # 并行工具调用时，多个 tool_use 先于各自 tool_result 出现；按 call_id 精确归属结果，
    # call_id 缺失时回退到最近一次 tool_use（与 resume-kimi 的顺序启发式一致）。
    call_tool = {}

    for it in items:
        k = it.get("kind")
        if k == "user_text":
            if not first_user:
                first_user = it["text"]
            last_user = it["text"]
        elif k == "assistant_text":
            last_assistant = it["text"]
        elif k == "tool_use":
            name = it.get("name")
            inp = it.get("input") or {}
            last_tool_name = name
            last_command = ""
            cmd = ""
            if name in ("bash", "Bash") and inp.get("command"):
                cmd = str(inp["command"]).strip()
                last_command = cmd
                if cmd:
                    commands.append(cmd)
            elif name in READ_TOOLS:
                if inp.get("path"):
                    files_read.append(inp["path"])
            elif name in EDIT_TOOLS:
                if inp.get("path"):
                    files_edited.append(inp["path"])
            if it.get("call_id"):
                call_tool[it["call_id"]] = {"name": name, "command": cmd}
        elif k == "tool_result":
            content = it.get("content") or ""
            meta = call_tool.get(it.get("call_id")) if it.get("call_id") else None
            if meta is None:
                meta = {"name": last_tool_name, "command": last_command}
            t_name, t_cmd = meta["name"], meta["command"]
            is_test_cmd = t_name in ("bash", "Bash") and bool(TEST_CMD_RE.search(t_cmd))
            head = content[:2000]
            looks_test = is_test_cmd or (bool(content) and bool(TEST_RESULT_RE.search(head)))
            is_err = it.get("is_error") or (bool(ERR_RE.search(head)) and not ERR_NEG_RE.search(head))
            if (looks_test or is_err) and content.strip():
                test_results.append({
                    "command_hint": t_cmd or t_name,
                    "is_error": is_err,
                    "content": content,
                })

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

def py_json_dumps(obj):
    # 模仿 Python json.dumps 默认格式（分隔 ", " / ": "，键双引号），
    # ensure_ascii=False 保留非 ASCII 原字符，与 Node 端 pyJsonStringify 输出一致。
    return json.dumps(obj, ensure_ascii=False)


def block(text, max_chars):
    if not text or not text.strip():
        return ""
    text = text.strip()
    full_len = len(text)
    if full_len > max_chars:
        text = text[:max_chars] + f"\n…（已截断，原长 {full_len} 字符）"
    return text


def render_item(it, max_chars):
    k = it.get("kind")
    ts = fmt_time(it.get("timestamp"))
    if k == "user_text":
        return [f"### [用户] {ts}", block(it.get("text"), max_chars)]
    if k == "assistant_text":
        return [f"### [助手] {ts}", block(it.get("text"), max_chars)]
    if k == "tool_use":
        return [f"### [工具调用] {it.get('name')} {ts}", "```",
                truncate(py_json_dumps(it.get("input")), max_chars), "```"]
    if k == "tool_result":
        tag = " (错误)" if it.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", block(it.get("content") or "", max_chars)]
    return []


def render_summary(session, state, recent_n=8, max_chars=1500, older_limit=60):
    lines = []
    info = session["info"]
    norm = session["normalized"]
    items = norm["items"]

    lines.append("# Resume-Grok 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info['cwd'] or '(未知)'}")
    if info.get("model"):
        lines.append(f"- 模型: {info['model']}")
    if info.get("agent_name"):
        lines.append(f"- Agent: {info['agent_name']}")
    if info.get("reasoning_effort"):
        lines.append(f"- 推理强度: {info['reasoning_effort']}")
    if info.get("git_branch"):
        lines.append(f"- Git分支: {info['git_branch']}")
    if info.get("parent_session_id"):
        lines.append(f"- 派生自: {info['parent_session_id']}")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(items)}")
    lines.append("")

    lines.append("## 任务状态重建")
    lines.append("")
    lines.append("### 目标")
    lines.append(block(state["goal"], max_chars) or "(未识别)")
    lines.append("")
    if state["files_read"]:
        lines.append("### 已调查文件")
        for f in state["files_read"]:
            lines.append(f"- {f}")
        lines.append("")
    if state["files_edited"]:
        lines.append("### 代码修改")
        for f in state["files_edited"]:
            lines.append(f"- {f}")
        lines.append("")
    if state["commands"]:
        lines.append("### 执行命令")
        for c in state["commands"]:
            lines.append(f"- `{truncate(c, 200)}`")
        lines.append("")
    if state["test_results"]:
        lines.append("### 测试 / 错误结果")
        for tr in state["test_results"][-5:]:
            tag = " [错误]" if tr["is_error"] else ""
            trimmed = tr["content"].strip()
            first_line = trimmed.split("\r\n")[0].split("\r")[0].split("\n")[0] if trimmed else ""
            lines.append(f"-{tag} {truncate(first_line, 200)}")
        lines.append("")
    lines.append("### 最近用户消息")
    lines.append(block(state["last_user"], max_chars) or "(无)")
    lines.append("")
    lines.append("### 最近助手消息")
    lines.append(block(state["last_assistant"], max_chars) or "(无)")
    lines.append("")

    recent = items[-recent_n:] if len(items) > recent_n else items
    lines.append(f"## 近期对话（最近 {len(recent)} 条）")
    lines.append("")
    for it in recent:
        lines.extend(render_item(it, max_chars))
        lines.append("")

    older = items[:-recent_n] if len(items) > recent_n else []
    older_tools = [it for it in older if it.get("kind") == "tool_use"]
    if older_tools:
        lines.append(f"## 更早活动（工具调用，仅最近 {older_limit} 条）")
        for it in older_tools[-older_limit:]:
            lines.append(f"- [{fmt_time(it.get('timestamp'))}] {tool_brief(it.get('name'), it.get('input'))}")
        lines.append("")

    lines.append("## 接管建议")
    lines.append("- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。")
    lines.append("- 以「任务状态重建」+「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。")
    lines.append("- 不要逐字复述历史；基于现状决定下一步动作。")
    lines.append("")

    return "\n".join(lines)


# ---------- 会话扫描 ----------

def scan_sessions(grok_dir, project_path):
    # 收集当前项目的会话，按 summary.updated_at 倒序。
    norm = norm_path(project_path)
    sessions_root = os.path.join(grok_dir, "sessions")
    sessions = []
    if not is_dir_safe(sessions_root):
        return sessions
    try:
        groups = os.listdir(sessions_root)
    except OSError:
        return sessions
    for gname in groups:
        group_path = os.path.join(sessions_root, gname)
        if not is_dir_safe(group_path):
            continue
        try:
            sids = os.listdir(group_path)
        except OSError:
            continue
        for sname in sids:
            session_dir = os.path.join(group_path, sname)
            if not is_dir_safe(session_dir):
                continue
            summary = load_summary(session_dir)
            cwd = summary_cwd(summary, session_dir, gname)
            if not cwd or norm_path(cwd) != norm:
                continue
            updated_at = (summary or {}).get("updated_at") or ""
            sort_key = parse_iso_ms(updated_at)
            if not sort_key:
                try:
                    sort_key = os.path.getmtime(session_dir) * 1000.0
                except OSError:
                    sort_key = 0
            session_id = ((summary or {}).get("info") or {}).get("id") or sname
            sessions.append({
                "sessionId": session_id,
                "sessionDir": session_dir,
                "cwd": cwd,
                "summary": summary,
                "updatedAt": updated_at,
                "mtime": sort_key,
            })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_all_sessions(grok_dir):
    """跨项目扫描：遍历 ~/.grok/sessions 下所有会话，不按 cwd 过滤。按 summary.updated_at 倒序。"""
    sessions_root = os.path.join(grok_dir, "sessions")
    sessions = []
    if not is_dir_safe(sessions_root):
        return sessions
    try:
        groups = os.listdir(sessions_root)
    except OSError:
        return sessions
    for gname in groups:
        group_path = os.path.join(sessions_root, gname)
        if not is_dir_safe(group_path):
            continue
        try:
            sids = os.listdir(group_path)
        except OSError:
            continue
        for sname in sids:
            session_dir = os.path.join(group_path, sname)
            if not is_dir_safe(session_dir):
                continue
            summary = load_summary(session_dir)
            cwd = summary_cwd(summary, session_dir, gname)
            updated_at = (summary or {}).get("updated_at") or ""
            sort_key = parse_iso_ms(updated_at)
            if not sort_key:
                try:
                    sort_key = os.path.getmtime(session_dir) * 1000.0
                except OSError:
                    sort_key = 0
            session_id = ((summary or {}).get("info") or {}).get("id") or sname
            sessions.append({
                "sessionId": session_id,
                "sessionDir": session_dir,
                "cwd": cwd,
                "summary": summary,
                "updatedAt": updated_at,
                "mtime": sort_key,
            })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


# ---------- 会话加载（完整） ----------

def load_session(session_dir, session_id):
    summary = load_summary(session_dir) or {}

    # 主 transcript：chat_history.jsonl；为空时兜底 events.jsonl / updates.jsonl（ACP 流）。
    norm = {"items": [], "model": None}
    ch_path = os.path.join(session_dir, "chat_history.jsonl")
    if is_file_safe(ch_path):
        rows, _ = read_jsonl(ch_path)
        r = normalize_chat(rows)
        if r["items"]:
            norm = r
    if not norm["items"]:
        for name in ("events.jsonl", "updates.jsonl"):
            p = os.path.join(session_dir, name)
            if not is_file_safe(p):
                continue
            rows, _ = read_jsonl(p)
            r = normalize_acp(rows)
            if r["items"]:
                norm = r
                break

    items = norm["items"]
    title = resolve_title(summary, items, session_id)
    cwd = (summary.get("info") or {}).get("cwd") or ""
    created_at = summary.get("created_at") or ""
    updated_at = summary.get("updated_at") or ""
    item_ts = [it["timestamp"] for it in items if it.get("timestamp")]
    info = {
        "session_id": session_id,
        "title": title,
        "cwd": cwd,
        "model": norm["model"] or summary.get("current_model_id") or "",
        "agent_name": summary.get("agent_name") or "",
        "reasoning_effort": summary.get("reasoning_effort") or "",
        "git_branch": summary.get("head_branch") or "",
        "parent_session_id": summary.get("parent_session_id") or "",
        "first_ts": fmt_time(created_at) if created_at else (fmt_time(item_ts[0]) if item_ts else ""),
        "last_ts": fmt_time(updated_at) if updated_at else (fmt_time(item_ts[-1]) if item_ts else ""),
    }
    return {"session": {"info": info, "normalized": norm}}, None


# ---------- 会话扫描（轻量，用于 --list） ----------

def session_meta_lite(s):
    # 轻量解析：标题取 summary，时间取 updated_at，条目数取 chat_history（兜底 events）。
    summary = s.get("summary") or load_summary(s["sessionDir"]) or {}
    count = 0
    first_user_text = ""
    ch_path = os.path.join(s["sessionDir"], "chat_history.jsonl")
    if is_file_safe(ch_path):
        rows, _ = read_jsonl(ch_path)
        r = normalize_chat(rows)
        count = len(r["items"])
        for it in r["items"]:
            if it.get("kind") == "user_text":
                first_user_text = it["text"]
                break
    if not count:
        for name in ("events.jsonl", "updates.jsonl"):
            p = os.path.join(s["sessionDir"], name)
            if not is_file_safe(p):
                continue
            rows, _ = read_jsonl(p)
            r = normalize_acp(rows)
            if r["items"]:
                count = len(r["items"])
                for it in r["items"]:
                    if it.get("kind") == "user_text":
                        first_user_text = it["text"]
                        break
                break
    title = resolve_title(summary, [{"kind": "user_text", "text": first_user_text}] if first_user_text else [], s["sessionId"])
    return {
        "session_id": s["sessionId"],
        "title": title,
        "last_ts": fmt_time(summary.get("updated_at")) if summary.get("updated_at") else "",
        "count": count,
    }


def print_session_list(sessions, project_path, grok_dir, limit=0):
    total = len(sessions)
    shown = sessions[:limit] if limit and limit > 0 else sessions
    print(f"当前项目: {project_path}")
    print(f"会话根: {os.path.join(grok_dir, 'sessions')}")
    if limit and 0 < limit < total:
        print(f"找到 {total} 个会话（仅显示最近 {len(shown)} 个）：\n")
    else:
        print(f"找到 {total} 个会话：\n")
    for i, s in enumerate(shown):
        meta = session_meta_lite(s)
        mark = "[最近]" if i == 0 else "      "
        title = meta["title"] or "(无标题)"
        last_ts = meta["last_ts"] or "(无时间)"
        count = meta["count"] or 0
        print(f"{mark} {last_ts}  {s['sessionId'][:20]}  消息数:{str(count).ljust(4)} 标题: {title}")


# ---------- 选择会话 ----------

def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0]
    arg = session_arg.lower()
    # 先按 sessionId 前缀/包含匹配
    for s in sessions:
        sid = s["sessionId"].lower()
        if sid.startswith(arg) or arg in sid:
            return s
    # 再按标题关键词匹配
    for s in sessions:
        title = resolve_title(s.get("summary"), [], s["sessionId"]).lower()
        if title and arg in title:
            return s
    return None


# ---------- CLI ----------

def setup_utf8_stdio():
    """Windows 中文控制台兜底：强制 stdout/stderr 使用 UTF-8，且换行统一为 LF（与 Node 实现一致）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", newline="")
    except Exception:
        pass


def build_parser():
    p = argparse.ArgumentParser(
        prog="resume_grok.py",
        description="读取 Grok Build CLI 本地会话记录（summary.json + chat_history.jsonl [+ events.jsonl]），生成结构化接管摘要。",
        add_help=False,
    )
    p.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    p.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    p.add_argument("--session", metavar="ID", help="指定会话 ID 或前缀（支持跨项目全局查找，也匹配标题关键词）")
    p.add_argument("--project", metavar="PATH", help="项目路径，默认当前工作目录")
    p.add_argument("--grok-dir", metavar="DIR", help="Grok 配置目录，默认 ~/.grok 或 $GROK_HOME")
    p.add_argument("--recent", type=int, default=8, help="近期对话保留条目数，默认 8")
    p.add_argument("--max-chars", type=int, default=1500, help="单条内容截断长度，默认 1500")
    p.add_argument("--limit", type=int, default=0, help="--list 返回的会话数量上限，0 不限制（默认 0）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    p.add_argument("--output", metavar="FILE", help="将摘要写入文件")
    p.add_argument("-h", "--help", action="help", help="显示此帮助")
    return p


def main():
    setup_utf8_stdio()
    args = build_parser().parse_args()

    grok_dir = args.grok_dir or get_grok_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    target = None
    project_path = project_path_arg
    sessions = []

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        sessions = scan_all_sessions(grok_dir)
        target = pick_session(sessions, args.session)
        if target and target.get("cwd"):
            project_path = target["cwd"]

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if target is None:
        sessions = scan_sessions(grok_dir, project_path_arg)
        if not sessions:
            print(f"错误：未找到项目 {project_path_arg} 对应的 Grok 会话。", file=sys.stderr)
            print(f"已查找：{os.path.join(grok_dir, 'sessions')}", file=sys.stderr)
            sys.exit(1)
        target = pick_session(sessions, args.session)
        if not target:
            print(f"错误：未匹配到会话 '{args.session}'。使用 --list 查看可用会话。", file=sys.stderr)
            sys.exit(1)

    if args.list:
        print_session_list(sessions, project_path, grok_dir, args.limit)
        return

    res, err = load_session(target["sessionDir"], target["sessionId"])
    if err:
        print(f"错误：解析会话失败：{err}", file=sys.stderr)
        sys.exit(1)

    session = res["session"]
    state = build_state(session["normalized"]["items"])

    if args.json:
        out = json.dumps({
            "info": session["info"],
            "state": {
                "goal": state["goal"],
                "files_read": state["files_read"],
                "files_edited": state["files_edited"],
                "commands": state["commands"],
                "test_results": state["test_results"],
                "last_user": state["last_user"],
                "last_assistant": state["last_assistant"],
            },
            "recent_items": session["normalized"]["items"][-args.recent:],
        }, ensure_ascii=False, indent=2)
    else:
        out = render_summary(session, state, args.recent, args.max_chars)

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(out.encode("utf-8"))
        if not out.endswith("\n"):
            sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
