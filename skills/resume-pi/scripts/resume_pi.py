#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-pi: 读取 Pi Coding Agent 本地会话 JSONL，生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Pi / Claude Code / Grok / Codex 等）均可直接调用。
用法见同目录 SKILL.md。

pi 会话格式（v3）：
  - 存储于 ~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl
  - <encoded-cwd> = `--` + cwd 去掉开头分隔符、把 / \\ : 替换为 - + `--`
  - 首行为 {type:"session"} 头（id/cwd/timestamp），其后条目经 id/parentId 构成树
  - 当前对话 = 从最后一个叶子条目沿 parentId 回溯到根的路径
  - 条目类型：message（user/assistant/toolResult/bashExecution/custom 等 role）、
    model_change、thinking_level_change、compaction、branch_summary、
    session_info（自定义标题）、custom_message、label、custom
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# AGENTS.md 约定：时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss
CST = timezone(timedelta(hours=8))

# pi 内置工具分类（小写匹配），用于「已调查文件 / 代码修改」归类
READ_TOOLS = {"read", "ls", "find", "grep"}
EDIT_TOOLS = {"write", "edit"}
SHELL_TOOLS = {"bash", "powershell"}


# ---------- 路径与项目定位 ----------

def expand_tilde(p):
    if p == "~":
        return os.path.expanduser("~")
    if p.startswith("~/") or p.startswith("~\\"):
        return os.path.join(os.path.expanduser("~"), p[2:])
    return p


def get_default_agent_dir():
    """pi 的 agent 目录：默认 ~/.pi/agent，可用 PI_CODING_AGENT_DIR 覆盖。"""
    env = os.environ.get("PI_CODING_AGENT_DIR")
    if env:
        return expand_tilde(env)
    return os.path.join(os.path.expanduser("~"), ".pi", "agent")


def is_dir_safe(p):
    return os.path.isdir(p)


def resolve_sessions_root(pi_dir):
    """把 --pi-dir（~/.pi 这一级）或 PI_CODING_AGENT_DIR（agent 这一级）归一为 sessions 根目录。"""
    if pi_dir:
        candidates = [
            os.path.join(pi_dir, "agent", "sessions"),
            os.path.join(pi_dir, "sessions"),
        ]
    else:
        candidates = [os.path.join(get_default_agent_dir(), "sessions")]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


def encode_project_path(path):
    """pi 把 cwd 编码为会话目录名：--<去掉开头分隔符、[/\\:] → -> 的 cwd>--。"""
    p = os.path.abspath(path)
    p = re.sub(r"^[/\\]", "", p)
    p = re.sub(r"[/\\:]", "-", p)
    return f"--{p}--"


def peek_cwd(jsonl_path, max_lines=10):
    """读取会话文件首行的 {type:"session"} 头，取 cwd 字段用于兜底匹配项目。"""
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
                if obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        return None
    return None


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def find_project_dir(sessions_root, project_path):
    """定位当前项目对应的 <sessions_root>/<encoded-cwd> 目录。"""
    if not os.path.isdir(sessions_root):
        return None

    encoded = encode_project_path(project_path)

    # 1. 精确匹配
    cand = os.path.join(sessions_root, encoded)
    if os.path.isdir(cand):
        return cand

    # 2. 大小写无关匹配
    enc_low = encoded.lower()
    for d in os.listdir(sessions_root):
        if d.lower() == enc_low:
            full = os.path.join(sessions_root, d)
            if os.path.isdir(full):
                return full

    # 3. 兜底：扫描会话头中的 cwd 字段（限量，避免过慢）
    norm = norm_path(project_path)
    scanned = 0
    for d in sorted(os.listdir(sessions_root)):
        full = os.path.join(sessions_root, d)
        if not os.path.isdir(full):
            continue
        for fn in os.listdir(full):
            if not fn.endswith(".jsonl"):
                continue
            scanned += 1
            if scanned > 300:
                break
            cwd = peek_cwd(os.path.join(full, fn))
            if cwd and norm_path(cwd) == norm:
                return full
        if scanned > 300:
            break
    return None


# ---------- 时间格式化 ----------

def fmt_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(iso)


# ---------- JSONL 解析 ----------

def parse_events(jsonl_path):
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
        return [], str(e)
    return events, None


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


def session_id_from_filename(p):
    """从文件名推断会话 UUID：<timestamp>_<uuid>.jsonl → uuid。"""
    base = os.path.splitext(os.path.basename(p))[0]
    if base.endswith(".jsonl"):  # 防御：调用方传入带扩展名的路径
        base = base[:-len(".jsonl")]
    idx = base.rfind("_")
    return base[idx + 1:] if idx >= 0 else base


def header_session_id(events, fallback):
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "session" and ev.get("id"):
            return ev["id"]
    return fallback


def active_path(events):
    """沿最后一个叶子条目回溯 parentId 到根，得到当前激活的对话路径（支持 pi 会话分支）。"""
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


def shell_command(inp):
    return inp.get("command") or inp.get("script") or ""


def normalize_events(events):
    """把激活路径上的条目拍平为有序的归一化条目列表。"""
    items = []  # kind ∈ user_text/assistant_text/error_text/custom_text/tool_use/tool_result
    summaries = []
    title = None            # session_info.name（手动 /cmd:name 设置的会话名）
    model = None            # provider/modelId
    thinking_level = None
    calls = {}              # toolCall id → {name, input}

    # session 头没有 id，不在激活路径上，直接从整个文件取首个 {type:"session"} 条目。
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
                title = ev["name"]
            continue
        if etype in ("compaction", "branch_summary"):
            if ev.get("summary"):
                summaries.append(ev["summary"])
            continue
        if etype == "model_change":
            provider, model_id = ev.get("provider") or "", ev.get("modelId") or ""
            if model_id:
                model = f"{provider}/{model_id}" if provider else model_id
            continue
        if etype == "thinking_level_change":
            if ev.get("thinkingLevel"):
                thinking_level = ev["thinkingLevel"]
            continue
        if etype not in ("message", "custom_message"):
            continue

        ts = ev.get("timestamp", "")

        if etype == "custom_message":
            text = extract_text(ev.get("content"))
            if text.strip():
                items.append({
                    "kind": "custom_text",
                    "timestamp": ts,
                    "text": text,
                    "custom_type": ev.get("customType") or "",
                })
            continue

        msg = ev.get("message") or {}
        role = msg.get("role")

        if role == "user":
            text = extract_text(msg.get("content"))
            if text.strip():
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
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
                    inp = block.get("arguments") or {}
                    calls[block.get("id")] = {"name": block.get("name") or "", "input": inp}
                    items.append({
                        "kind": "tool_use",
                        "timestamp": ts,
                        "name": block.get("name") or "",
                        "input": inp,
                        "call_id": block.get("id") or "",
                    })
                # thinking block 不进入接管摘要
            if text_only.strip():
                items.append({"kind": "assistant_text", "timestamp": ts, "text": text_only})
            if msg.get("errorMessage"):
                items.append({"kind": "error_text", "timestamp": ts, "text": str(msg["errorMessage"])})
        elif role == "toolResult":
            call = calls.get(msg.get("toolCallId"))
            name = msg.get("toolName") or (call["name"] if call else "")
            command_hint = ""
            if call and str(name).lower() in SHELL_TOOLS:
                command_hint = shell_command(call["input"])
            items.append({
                "kind": "tool_result",
                "timestamp": ts,
                "tool_call_id": msg.get("toolCallId") or "",
                "tool_name": name or "",
                "content": extract_text(msg.get("content")),
                "is_error": bool(msg.get("isError") or msg.get("is_error")),
                "command_hint": command_hint,
            })
        elif role == "bashExecution":
            # 用户在 TUI 里以 ! / !! 直接执行的 shell 命令
            cmd = msg.get("command") or ""
            items.append({"kind": "tool_use", "timestamp": ts, "name": "bash",
                          "input": {"command": cmd}, "call_id": ""})
            exit_code = msg.get("exitCode")
            is_err = bool(msg.get("cancelled")) or (0 if exit_code is None else exit_code) != 0
            items.append({
                "kind": "tool_result",
                "timestamp": ts,
                "tool_call_id": "",
                "tool_name": "bash",
                "content": msg.get("output") or "",
                "is_error": is_err,
                "command_hint": cmd,
            })
        elif role == "custom":
            text = extract_text(msg.get("content"))
            if text.strip():
                items.append({
                    "kind": "custom_text",
                    "timestamp": ts,
                    "text": text,
                    "custom_type": msg.get("customType") or "",
                })
        elif role in ("compactionSummary", "branchSummary"):
            if msg.get("summary"):
                summaries.append(msg["summary"])
        # image / 其他 role 忽略

    return {
        "items": items,
        "summaries": summaries,
        "title": title,
        "cwd": (header or {}).get("cwd"),
        "parent_session": (header or {}).get("parentSession"),
        "model": model,
        "thinking_level": thinking_level,
    }


# ---------- 标题解析 ----------

def resolve_title(title, items, session_id):
    """session_info.name > 首条用户消息摘要 > 会话 ID。"""
    if title:
        return title
    for it in items:
        if it["kind"] == "user_text":
            t = first_line(it["text"].strip())
            return (t[:60] + "…") if len(t) > 60 else t or session_id
    return session_id


# ---------- 工具输入摘要 ----------

def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


# 与 Node split 对齐的「取首行」辅助：显式按 str.splitlines() 认可的全部换行符切断，
# 保证两实现输出一致（\v \f \x1c-\x1e \x85 等也算换行）。
LINE_BREAK_RE = re.compile(r"[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def first_line(s):
    return LINE_BREAK_RE.split(str(s))[0]


def tool_file(name, inp):
    n = str(name).lower()
    if n in ("read", "write", "edit", "ls"):
        return inp.get("path") or inp.get("file_path")
    if n in ("find", "grep"):
        return inp.get("pattern") or inp.get("path")
    return None


def tool_brief(name, inp):
    """单行紧凑描述一个工具调用。"""
    n = str(name).lower()
    if n in ("bash", "powershell"):
        cmd = shell_command(inp).strip().replace("\n", " ")
        return f"{name}({truncate(cmd, 100)})"
    f = tool_file(name, inp)
    if f:
        return f"{name}({truncate(f, 100)})"
    # 其它工具：取首个字符串型入参
    for v in inp.values():
        if isinstance(v, str) and v:
            return f"{name}({truncate(v, 80)})"
    return f"{name}(...)"


# ---------- 任务状态重建 ----------

TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|"
    r"go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b",
    re.IGNORECASE,
)
# 只匹配明确的测试摘要标记，避免把含 error/test/fail 的普通代码文件误判为测试结果。
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|"
    r"\b\d+\s*(passed|failed|tests?)\b|"
    r"\b(passed|failed)\s*\d+\b|"
    r"\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE,
)


def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_state(items):
    """从归一化条目提取结构化任务状态。"""
    files_read = []
    files_edited = []
    commands = []
    test_results = []
    first_user = ""
    last_user = ""
    last_assistant = ""
    calls = {}  # call_id → tool_use 条目
    last_command = ""

    for it in items:
        k = it["kind"]
        if k == "user_text":
            if not first_user:
                first_user = it["text"]
            last_user = it["text"]
        elif k == "assistant_text":
            last_assistant = it["text"]
        elif k == "tool_use":
            name = str(it["name"]).lower()
            inp = it.get("input") or {}
            if it.get("call_id"):
                calls[it["call_id"]] = it
            if name in READ_TOOLS:
                f = tool_file(it["name"], inp)
                if f:
                    files_read.append(f)
            elif name in EDIT_TOOLS:
                f = tool_file(it["name"], inp)
                if f:
                    files_edited.append(f)
            elif name in SHELL_TOOLS:
                cmd = shell_command(inp).strip()
                if cmd:
                    commands.append(cmd)
                    last_command = cmd
        elif k == "tool_result":
            call = calls.get(it.get("tool_call_id")) if it.get("tool_call_id") else None
            name = str(it.get("tool_name") or (call["name"] if call else "")).lower()
            if it.get("command_hint"):
                cmd = it["command_hint"]
            elif call and str(call["name"]).lower() in SHELL_TOOLS:
                cmd = shell_command(call["input"])
            elif name in SHELL_TOOLS:
                cmd = last_command
            else:
                cmd = ""
            content = it.get("content", "")
            is_err = bool(it.get("is_error"))
            is_test_cmd = name in SHELL_TOOLS and bool(TEST_CMD_RE.search(cmd)) if cmd else False
            looks_test = bool(is_test_cmd or (content and TEST_RESULT_RE.search(content[:2000])))
            if (looks_test or is_err) and content.strip():
                test_results.append({
                    "command_hint": cmd or it.get("tool_name") or (call["name"] if call else ""),
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

def render_summary(session, state, recent_n=8, max_chars=1500, older_limit=60):
    lines = []
    info = session["info"]
    norm = session["normalized"]

    lines.append("# Resume-Pi 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("model"):
        lines.append(f"- 模型: {info['model']}")
    if info.get("thinking_level"):
        lines.append(f"- 思考级别: {info['thinking_level']}")
    if info.get("parent_session"):
        lines.append(f"- 父会话: {info['parent_session']}")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(norm['items'])}")
    lines.append("")

    # 历史摘要
    if norm["summaries"]:
        lines.append("## 历史摘要（原会话 compact / 分支摘要）")
        for s in norm["summaries"]:
            lines.append(f"- {truncate(s, max_chars)}")
        lines.append("")

    # 任务状态重建
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
            first_line_str = first_line(tr["content"].strip())
            lines.append(f"-{tag} {truncate(first_line_str, 200)}")
        lines.append("")
    lines.append("### 最近用户消息")
    lines.append(block(state["last_user"], max_chars) or "(无)")
    lines.append("")
    lines.append("### 最近助手消息")
    lines.append(block(state["last_assistant"], max_chars) or "(无)")
    lines.append("")

    # 近期对话
    items = norm["items"]
    recent = items[-recent_n:] if len(items) > recent_n else items
    lines.append(f"## 近期对话（最近 {len(recent)} 条）")
    lines.append("")
    for it in recent:
        lines.extend(render_item(it, max_chars))
        lines.append("")

    # 更早活动
    older = items[:-recent_n] if len(items) > recent_n else []
    older_tools = [it for it in older if it["kind"] == "tool_use"]
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 %d 条）" % older_limit)
        for it in older_tools[-older_limit:]:
            lines.append(f"- [{fmt_time(it.get('timestamp'))}] {tool_brief(it['name'], it['input'])}")
        lines.append("")

    # 接管建议
    lines.append("## 接管建议")
    lines.append("- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。")
    lines.append("- 以「任务状态重建」+「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。")
    lines.append("- 不要逐字复述历史；基于现状决定下一步动作。")
    lines.append("")

    return "\n".join(lines)


def render_item(it, max_chars):
    k = it["kind"]
    ts = fmt_time(it.get("timestamp"))
    if k == "user_text":
        return [f"### [用户] {ts}", block(it["text"], max_chars)]
    if k == "assistant_text":
        return [f"### [助手] {ts}", block(it["text"], max_chars)]
    if k == "error_text":
        return [f"### [错误] {ts}", block(it["text"], max_chars)]
    if k == "custom_text":
        tag = f"（{it['custom_type']}）" if it.get("custom_type") else ""
        return [f"### [扩展消息]{tag} {ts}", block(it["text"], max_chars)]
    if k == "tool_use":
        return [f"### [工具调用] {it['name']} {ts}", "```",
                truncate(json.dumps(it["input"], ensure_ascii=False), max_chars), "```"]
    if k == "tool_result":
        tag = " (错误)" if it.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", block(it.get("content", ""), max_chars)]
    return []


def block(text, max_chars):
    if not text or not text.strip():
        return ""
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断，原长 {len(text)} 字符）"
    return text


# ---------- 会话扫描 ----------

def scan_sessions(project_dir):
    """轻量扫描：仅按文件名与修改时间列出会话，不解析内容。按 mtime 倒序。"""
    sessions = []
    try:
        files = os.listdir(project_dir)
    except OSError:
        return sessions
    for fn in files:
        if not fn.endswith(".jsonl"):
            continue
        path = os.path.join(project_dir, fn)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        sessions.append({
            "session_id": session_id_from_filename(path),
            "path": path,
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_all_sessions(sessions_root):
    """跨项目扫描：遍历 sessions 根目录下所有项目目录的会话。按 mtime 倒序。"""
    if not os.path.isdir(sessions_root):
        return []
    sessions = []
    for d in os.listdir(sessions_root):
        full = os.path.join(sessions_root, d)
        if not os.path.isdir(full):
            continue
        sessions.extend(scan_sessions(full))
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def session_meta(path):
    """解析单个会话，返回标题/时间范围/条目数（用于 --list 展示）。"""
    events, err = parse_events(path)
    if err:
        return None
    norm = normalize_events(events)
    items = norm["items"]
    sid = header_session_id(events, session_id_from_filename(path))
    timestamps = [it.get("timestamp") for it in items if it.get("timestamp")]
    return {
        "session_id": sid,
        "title": resolve_title(norm["title"], items, sid),
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
        "count": len(items),
    }


def print_session_list(sessions, project_dir, project_path, limit=0):
    total = len(sessions)
    shown = sessions[:limit] if limit and limit > 0 else sessions
    print(f"当前项目: {project_path}")
    print(f"项目目录: {os.path.basename(project_dir)}")
    if limit and 0 < limit < total:
        print(f"找到 {total} 个会话（仅显示最近 {len(shown)} 个）：\n")
    else:
        print(f"找到 {total} 个会话：\n")
    for i, s in enumerate(shown):
        meta = session_meta(s["path"]) or {}
        mark = "[最近]" if i == 0 else "      "
        title = meta.get("title") or "(无标题)"
        last_ts = meta.get("last_ts") or "(无时间)"
        count = meta.get("count", 0)
        print(f"{mark} {last_ts}  {s['session_id'][:12]}  消息数:{count:<4} 标题: {title}")


# ---------- 主流程 ----------

def load_session(jsonl_path):
    events, err = parse_events(jsonl_path)
    if err:
        return None, err
    norm = normalize_events(events)
    items = norm["items"]
    sid = header_session_id(events, session_id_from_filename(jsonl_path))
    timestamps = [it.get("timestamp") for it in items if it.get("timestamp")]
    info = {
        "session_id": sid,
        "title": resolve_title(norm["title"], items, sid),
        "cwd": norm.get("cwd"),
        "model": norm.get("model"),
        "thinking_level": norm.get("thinking_level"),
        "parent_session": norm.get("parent_session"),
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
    }
    return {"info": info, "normalized": norm, "events": events}, None


def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0]  # 最近
    for s in sessions:
        fname = os.path.splitext(os.path.basename(s["path"]))[0]
        if s["session_id"].startswith(session_arg) or session_arg in s["session_id"]:
            return s
        if fname.startswith(session_arg) or session_arg in fname:
            return s
    return None


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


def main():
    setup_utf8_stdio()
    ap = argparse.ArgumentParser(
        description="读取 Pi Coding Agent 本地会话 JSONL，生成结构化接管摘要。"
    )
    ap.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    ap.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    ap.add_argument("--session", default=None, help="指定会话 ID / ID 前缀 / 文件名片段（支持跨项目全局查找）")
    ap.add_argument("--project", default=None, help="项目路径，默认当前工作目录")
    ap.add_argument("--pi-dir", default=None, help="Pi 数据目录，默认 ~/.pi（会话位于 <pi-dir>/agent/sessions）")
    ap.add_argument("--recent", type=int, default=8, help="近期对话保留条目数，默认 8")
    ap.add_argument("--max-chars", type=int, default=1500, help="单条内容截断长度，默认 1500")
    ap.add_argument("--limit", type=int, default=0, help="--list 返回的会话数量上限，0 表示不限制")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    ap.add_argument("--output", default=None, help="将摘要写入文件")
    args = ap.parse_args()

    pi_dir = expand_tilde(args.pi_dir) if args.pi_dir else None
    sessions_root = resolve_sessions_root(pi_dir)
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    target = None
    project_dir = None
    project_path = project_path_arg

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        all_sessions = scan_all_sessions(sessions_root)
        target = pick_session(all_sessions, args.session)
        if target:
            project_dir = os.path.dirname(target["path"])
            cwd = peek_cwd(target["path"])
            if cwd:
                project_path = cwd

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if target is None:
        project_dir = find_project_dir(sessions_root, project_path_arg)
        if not project_dir:
            print(f"错误：未找到项目 {project_path_arg} 对应的会话目录。", file=sys.stderr)
            print(f"已查找：{sessions_root}", file=sys.stderr)
            sys.exit(1)
        sessions = scan_sessions(project_dir)
        if not sessions:
            print(f"错误：项目目录 {project_dir} 下没有 .jsonl 会话文件。", file=sys.stderr)
            sys.exit(1)
        target = pick_session(sessions, args.session)
        if not target:
            print(f"错误：未匹配到会话 '{args.session}'。使用 --list 查看可用会话。", file=sys.stderr)
            sys.exit(1)

    if args.list:
        sessions = scan_sessions(project_dir)
        if not sessions:
            print(f"错误：项目目录 {project_dir} 下没有 .jsonl 会话文件。", file=sys.stderr)
            sys.exit(1)
        print_session_list(sessions, project_dir, project_path, args.limit)
        return

    session, err = load_session(target["path"])
    if err:
        print(f"错误：解析会话失败：{err}", file=sys.stderr)
        sys.exit(1)

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
            "summaries": session["normalized"]["summaries"],
            "recent_items": session["normalized"]["items"][-args.recent:],
        }, ensure_ascii=False, indent=2)
    else:
        out = render_summary(
            session, state,
            recent_n=args.recent,
            max_chars=args.max_chars,
        )

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
