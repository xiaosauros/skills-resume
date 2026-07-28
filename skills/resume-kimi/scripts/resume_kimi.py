#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-kimi: 读取 Kimi Code CLI 本地会话记录（~/.kimi-code 下的
session_index.jsonl + sessions/<workspace>/<session>/state.json +
agents/main/wire.jsonl），生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Claude Code / Codex / Grok 等）均可直接调用。
与同目录 resume_kimi.js 功能等价、输出可互换。用法见同目录 SKILL.md。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude/codex/cursor 一致）
CST = timezone(timedelta(hours=8))


# ---------- 路径与项目定位 ----------

def get_kimi_dir():
    return os.environ.get(
        "KIMI_HOME",
        os.path.expanduser(os.path.join("~", ".kimi-code")),
    )


def norm_path(p):
    """归一化路径用于比对：反斜杠转正斜杠、去尾斜杠、小写。
    与 Node 实现完全一致，保证跨实现可互换。"""
    return str(p).replace("\\", "/").rstrip("/").lower()


# ---------- session_index（会话列表与 workDir 主来源） ----------

def load_session_index(kimi_dir):
    """~/.kimi-code/session_index.jsonl 每行 {"sessionId","sessionDir","workDir"}。
    workDir 是会话所属项目路径，用于按 --project 匹配。"""
    idx_path = os.path.join(kimi_dir, "session_index.jsonl")
    out = []
    if not os.path.isfile(idx_path):
        return out
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("sessionId") and o.get("sessionDir"):
                    out.append({
                        "sessionId": o["sessionId"],
                        "sessionDir": o["sessionDir"],
                        "workDir": o.get("workDir") or "",
                    })
    except OSError:
        pass
    return out


# ---------- workspaces.json（workDir 兜底来源） ----------

def load_workspaces(kimi_dir):
    """~/.kimi-code/workspaces.json: {workspaces: {wd_<name>_<hash>: {root,...}}}
    当 session_index 缺失时，用工作区 id（sessionDir 的父目录名）反查项目根。"""
    wj_path = os.path.join(kimi_dir, "workspaces.json")
    mapping = {}
    if not os.path.isfile(wj_path):
        return mapping
    try:
        with open(wj_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return mapping
    ws = obj.get("workspaces") if isinstance(obj, dict) else None
    if isinstance(ws, dict):
        for wid, info in ws.items():
            root = info.get("root") if isinstance(info, dict) else None
            if root:
                mapping[wid] = root
    return mapping


# ---------- 兜底：直接扫描 sessions 目录 ----------

def walk_sessions(kimi_dir, workspaces):
    """session_index 缺失时兜底：遍历 sessions/<wd_id>/<session_id>/state.json。"""
    sessions_root = os.path.join(kimi_dir, "sessions")
    out = []
    if not os.path.isdir(sessions_root):
        return out
    try:
        wds = os.listdir(sessions_root)
    except OSError:
        return out
    for wd in wds:
        wd_path = os.path.join(sessions_root, wd)
        if not os.path.isdir(wd_path):
            continue
        try:
            sids = os.listdir(wd_path)
        except OSError:
            continue
        for sid in sids:
            session_dir = os.path.join(wd_path, sid)
            state_path = os.path.join(session_dir, "state.json")
            if not os.path.isfile(state_path):
                continue
            work_dir = ""
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                work_dir = st.get("workDir") or ""
            except (OSError, json.JSONDecodeError):
                pass
            if not work_dir:
                work_dir = workspaces.get(wd, "")
            out.append({"sessionId": sid, "sessionDir": session_dir, "workDir": work_dir})
    return out


# ---------- state.json（标题与时间） ----------

def load_state(session_dir):
    state_path = os.path.join(session_dir, "state.json")
    if not os.path.isfile(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---------- 时间格式化（同时支持 epoch 毫秒与 ISO 字符串） ----------

def fmt_time(ts):
    if ts is None or ts == "":
        return ""
    if isinstance(ts, bool):
        return str(ts)
    if isinstance(ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return str(ts)
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                return str(ts)
    else:
        return str(ts)
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso_ms(s):
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0
    return dt.timestamp() * 1000.0


# ---------- wire.jsonl 解析 ----------

def parse_wire(wire_path):
    events = []
    try:
        with open(wire_path, "r", encoding="utf-8") as f:
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


def join_input_text(inp):
    """turn.prompt.input 是 [{type,text},...]，抽取文本部分。"""
    if not isinstance(inp, list):
        return ""
    parts = [
        b.get("text", "")
        for b in inp
        if isinstance(b, dict) and isinstance(b.get("text"), str)
    ]
    return "".join(p for p in parts if p)


def output_to_text(out):
    """tool.result.output 可能是字符串（错误信息）或 [{type,text},...] 列表。"""
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "\n".join(
            b.get("text", "")
            for b in out
            if isinstance(b, dict) and b.get("text")
        )
    if isinstance(out, dict):
        return json.dumps(out, ensure_ascii=False)
    return str(out)


def normalize_input(name, args, disp):
    """优先用 Kimi 自带的结构化 display 字段；缺失时按工具名从 args 抽取。"""
    if not isinstance(args, dict):
        args = {}
    if isinstance(disp, dict):
        k = disp.get("kind")
        if k == "command":
            return {"command": disp.get("command") or "", "cwd": disp.get("cwd") or ""}
        if k == "file_io":
            return {"operation": disp.get("operation") or "", "path": disp.get("path") or ""}
        if k == "agent_call":
            return {"agent_name": disp.get("agent_name") or "", "prompt": disp.get("prompt") or ""}
        if k == "skill_call":
            return {"skill": disp.get("skill_name") or "", "args": disp.get("args") or ""}
        if k == "url_fetch":
            return {"url": disp.get("url") or ""}

    def pick(*ks):
        for key in ks:
            v = args.get(key)
            if v is not None and v != "":
                return v
        return ""

    if name == "Bash":
        return {"command": pick("command", "cmd")}
    if name in ("Read", "ReadMediaFile"):
        return {"path": pick("path", "file_path", "targetFile")}
    if name in ("Write", "Edit"):
        return {"path": pick("path", "file_path")}
    if name == "Grep":
        return {"pattern": pick("pattern"), "path": pick("path")}
    if name == "Glob":
        return {"pattern": pick("pattern"), "path": pick("path", "targetDirectory")}
    if name == "WebSearch":
        return {"query": pick("query", "searchTerm")}
    if name == "FetchURL":
        return {"url": pick("url")}
    if name == "Skill":
        return {"skill": pick("skill", "name"), "args": pick("args")}
    if name in ("Agent", "AgentSwarm"):
        return {"agent_name": pick("agent_name", "subagent_type", "name"), "prompt": pick("prompt", "description", "task")}
    return args


def normalize_events(events):
    """把 wire.jsonl 事件拍平为有序的归一化条目列表。
    顶层 type: metadata / config.update / turn.prompt / context.append_message /
      context.append_loop_event / llm.request / usage.record / tools.set_active_tools / ...
    loop_event.event.type: step.begin / step.end / content.part / tool.call / tool.result"""
    items = []  # 每项: {kind, ...} kind ∈ user_text/assistant_text/tool_use/tool_result
    model = None
    protocol_version = None

    pending_text = ""
    pending_ts = ""

    def flush_text():
        nonlocal pending_text, pending_ts
        if pending_text and pending_text.strip():
            items.append({"kind": "assistant_text", "timestamp": pending_ts, "text": pending_text})
        pending_text = ""
        pending_ts = ""

    for o in events:
        if not isinstance(o, dict):
            continue
        t = o.get("type")
        ts = o.get("time") if o.get("time") is not None else ""

        if t == "metadata":
            if not protocol_version and o.get("protocol_version"):
                protocol_version = o["protocol_version"]
            continue
        if t == "config.update":
            if not model and o.get("modelAlias"):
                model = o["modelAlias"]
            continue
        if t == "turn.prompt":
            flush_text()
            text = join_input_text(o.get("input")).strip()
            if text:
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
            continue
        if t == "context.append_loop_event":
            ev = o.get("event")
            if not isinstance(ev, dict):
                continue
            et = ev.get("type")
            if et == "step.begin":
                flush_text()
            elif et == "content.part":
                p = ev.get("part")
                if isinstance(p, dict) and p.get("type") == "text":
                    if not pending_text:
                        pending_ts = ts
                    pending_text += p.get("text") or ""
                # think / 其他 part 跳过
            elif et == "tool.call":
                flush_text()
                items.append({
                    "kind": "tool_use",
                    "timestamp": ts,
                    "name": ev.get("name") or "",
                    "input": normalize_input(ev.get("name"), ev.get("args"), ev.get("display")),
                    "call_id": ev.get("toolCallId") or "",
                })
            elif et == "tool.result":
                flush_text()
                r = ev.get("result")
                r = r if isinstance(r, dict) else {}
                content = output_to_text(r.get("output"))
                if not content.strip() and r.get("note"):
                    content = str(r["note"])
                items.append({
                    "kind": "tool_result",
                    "timestamp": ts,
                    "call_id": ev.get("toolCallId") or ev.get("parentUuid") or "",
                    "content": content,
                    "is_error": r.get("isError") is True,
                })
            # step.end / 其他跳过
            continue
        # context.append_message（多为 injection）、llm.request、usage.record 等跳过

    flush_text()

    return {"items": items, "model": model, "protocol_version": protocol_version}


# ---------- 标题解析 ----------

def resolve_title(state_title, items, session_id):
    """主来源：state.json 的 title（Kimi 侧边栏显示的标题）。
    "New Session" 是未生成标题时的占位符，视为无标题走兜底。"""
    if state_title and state_title.strip() and state_title.strip() != "New Session":
        return state_title.strip()
    for it in items:
        if it.get("kind") == "user_text":
            t = it["text"].strip()
            # 用 \r\n|\r|\n 切分（与 Node 实现一致），避免 CRLF 残留 \r 导致两端输出不一致。
            first = re.split(r"\r\n|\r|\n", t)[0] if t else ""
            return (first[:60] + "…") if len(first) > 60 else (first or session_id)
    return session_id


# ---------- 工具输入摘要 ----------

def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def tool_brief(name, inp):
    """单行紧凑描述一个工具调用（用于「更早活动」列表）。"""
    inp = inp or {}

    def c(v, n=80):
        return truncate(str(v or "").replace("\n", " "), n)

    if name == "Bash":
        return f"Bash({c(inp.get('command'), 100)})"
    if name in ("Read", "ReadMediaFile"):
        return f"{name}({c(inp.get('path'), 80)})"
    if name in ("Write", "Edit"):
        return f"{name}({c(inp.get('path'), 80)})"
    if name == "Grep":
        return f"Grep({c(inp.get('pattern'), 60)})"
    if name == "Glob":
        return f"Glob({c(inp.get('pattern'), 60)})"
    if name in ("Agent", "AgentSwarm"):
        return f"{name}({c(inp.get('agent_name'), 40)})"
    if name == "WebSearch":
        return f"WebSearch({c(inp.get('query'), 60)})"
    if name == "FetchURL":
        return f"FetchURL({c(inp.get('url'), 60)})"
    if name == "Skill":
        return f"Skill({c(inp.get('skill'), 40)})"
    if isinstance(inp, dict):
        for v in inp.values():
            if isinstance(v, str) and v:
                return f"{name}({c(v, 80)})"
    return f"{name}(...)"


# ---------- 任务状态重建 ----------

TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|"
    r"go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b",
    re.IGNORECASE | re.ASCII,
)
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|"
    r"\b\d+\s*(passed|failed|tests?)\b|"
    r"\b(passed|failed)\s*\d+\b|"
    r"\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE | re.ASCII,
)
# re.ASCII 使 \b 仅按 ASCII 单词字符判定边界，与 JS 实现一致（避免「Exception改」这类中文紧贴英文时两端 \b 判定不同）。
ERR_RE = re.compile(r"(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)", re.IGNORECASE | re.ASCII)
ERR_NEG_RE = re.compile(r"no error|0 failed", re.IGNORECASE)
READ_TOOLS = {"Read", "ReadMediaFile", "Grep", "Glob"}
EDIT_TOOLS = {"Write", "Edit"}


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
    last_tool_name = ""
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
            name = it["name"]
            inp = it.get("input") or {}
            last_tool_name = name
            last_command = ""
            if name == "Bash" and inp.get("command"):
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
            elif inp.get("operation") == "delete" and inp.get("path"):
                files_edited.append(inp["path"])
        elif k == "tool_result":
            content = it.get("content", "")
            is_test_cmd = last_tool_name == "Bash" and bool(TEST_CMD_RE.search(last_command))
            looks_test = bool(
                is_test_cmd or (content and TEST_RESULT_RE.search(content[:2000]))
            )
            head = content[:2000]
            is_err = bool(it.get("is_error")) or (
                bool(ERR_RE.search(head)) and not bool(ERR_NEG_RE.search(head))
            )
            if (looks_test or is_err) and content.strip():
                test_results.append({
                    "command_hint": last_command or last_tool_name,
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

def block(text, max_chars):
    if not text or not text.strip():
        return ""
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断，原长 {len(text)} 字符）"
    return text


def render_item(it, max_chars):
    k = it["kind"]
    ts = fmt_time(it.get("timestamp"))
    if k == "user_text":
        return [f"### [用户] {ts}", block(it["text"], max_chars)]
    if k == "assistant_text":
        return [f"### [助手] {ts}", block(it["text"], max_chars)]
    if k == "tool_use":
        return [f"### [工具调用] {it['name']} {ts}", "```", truncate(json.dumps(it["input"], ensure_ascii=False), max_chars), "```"]
    if k == "tool_result":
        tag = " (错误)" if it.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", block(it.get("content", ""), max_chars)]
    return []


def render_summary(session, state, recent_n=8, max_chars=1500, older_limit=60):
    lines = []
    info = session["info"]
    norm = session["normalized"]

    lines.append("# Resume-Kimi 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("protocol_version"):
        lines.append(f"- 协议版本: {info['protocol_version']}")
    if info.get("model"):
        lines.append(f"- 模型: {info['model']}")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(norm['items'])}")
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
            _ct = tr["content"].strip()
            # 用 \r\n|\r|\n 切分（与 Node 实现一致），避免 CRLF 残留 \r 导致两端输出不一致。
            first = re.split(r"\r\n|\r|\n", _ct)[0] if _ct else ""
            lines.append(f"-{tag} {truncate(first, 200)}")
        lines.append("")
    lines.append("### 最近用户消息")
    lines.append(block(state["last_user"], max_chars) or "(无)")
    lines.append("")
    lines.append("### 最近助手消息")
    lines.append(block(state["last_assistant"], max_chars) or "(无)")
    lines.append("")

    items = norm["items"]
    recent = items[-recent_n:] if len(items) > recent_n else items
    lines.append(f"## 近期对话（最近 {len(recent)} 条）")
    lines.append("")
    for it in recent:
        lines.extend(render_item(it, max_chars))
        lines.append("")

    older = items[:-recent_n] if len(items) > recent_n else []
    older_tools = [it for it in older if it["kind"] == "tool_use"]
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 %d 条）" % older_limit)
        for it in older_tools[-older_limit:]:
            lines.append(f"- [{fmt_time(it.get('timestamp'))}] {tool_brief(it['name'], it['input'])}")
        lines.append("")

    lines.append("## 接管建议")
    lines.append("- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。")
    lines.append("- 以「任务状态重建」+「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。")
    lines.append("- 不要逐字复述历史；基于现状决定下一步动作。")
    lines.append("")

    return "\n".join(lines)


# ---------- 会话扫描 ----------

def gather_session_entries(kimi_dir):
    """收集所有会话条目（不分项目），优先读 session_index，缺失时扫描 sessions 目录。"""
    workspaces = load_workspaces(kimi_dir)
    entries = load_session_index(kimi_dir)
    if not entries:
        entries = walk_sessions(kimi_dir, workspaces)
    return entries


def scan_sessions(kimi_dir, project_path):
    """收集当前项目的会话，按 state.updatedAt 倒序。"""
    norm = norm_path(project_path)
    entries = gather_session_entries(kimi_dir)
    sessions = []
    for e in entries:
        if not e.get("workDir") or norm_path(e["workDir"]) != norm:
            continue
        state = load_state(e["sessionDir"]) or {}
        updated_at = state.get("updatedAt") or ""
        sort_key = parse_iso_ms(updated_at)
        if not sort_key:
            try:
                sort_key = os.path.getmtime(e["sessionDir"]) * 1000.0
            except OSError:
                sort_key = 0.0
        sessions.append({
            "sessionId": e["sessionId"],
            "sessionDir": e["sessionDir"],
            "workDir": e["workDir"],
            "stateTitle": state.get("title") or "",
            "updatedAt": updated_at,
            "mtime": sort_key,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_all_sessions(kimi_dir):
    """跨项目扫描：收集所有会话条目，不按 workDir 过滤。按 state.updatedAt 倒序。"""
    entries = gather_session_entries(kimi_dir)
    sessions = []
    for e in entries:
        state = load_state(e["sessionDir"]) or {}
        updated_at = state.get("updatedAt") or ""
        sort_key = parse_iso_ms(updated_at)
        if not sort_key:
            try:
                sort_key = os.path.getmtime(e["sessionDir"]) * 1000.0
            except OSError:
                sort_key = 0.0
        sessions.append({
            "sessionId": e["sessionId"],
            "sessionDir": e["sessionDir"],
            "workDir": e["workDir"],
            "stateTitle": state.get("title") or "",
            "updatedAt": updated_at,
            "mtime": sort_key,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


# ---------- 会话加载（完整） ----------

def load_session(session_dir, session_id, work_dir):
    state = load_state(session_dir) or {}
    wire_path = os.path.join(session_dir, "agents", "main", "wire.jsonl")
    events, err = parse_wire(wire_path)
    if err:
        return None, err

    norm = normalize_events(events)
    items = norm["items"]
    title = resolve_title(state.get("title"), items, session_id)
    cwd = work_dir or state.get("workDir") or ""
    created_at = state.get("createdAt") or ""
    updated_at = state.get("updatedAt") or ""
    item_ts = [it["timestamp"] for it in items if it.get("timestamp") not in ("", None)]
    info = {
        "session_id": session_id,
        "title": title,
        "cwd": cwd,
        "protocol_version": norm.get("protocol_version") or "",
        "model": norm.get("model") or "",
        "first_ts": fmt_time(created_at) if created_at else (fmt_time(item_ts[0]) if item_ts else ""),
        "last_ts": fmt_time(updated_at) if updated_at else (fmt_time(item_ts[-1]) if item_ts else ""),
    }
    return {"info": info, "normalized": norm, "events": events}, None


# ---------- 会话扫描（轻量，用于 --list） ----------

def session_meta_lite(s):
    """轻量解析：标题取 state.json，时间取 state.updatedAt，条目数取 wire。"""
    state = load_state(s["sessionDir"]) or {}
    wire_path = os.path.join(s["sessionDir"], "agents", "main", "wire.jsonl")
    events, err = parse_wire(wire_path)
    count = 0
    first_user_text = ""
    if not err:
        norm = normalize_events(events)
        count = len(norm["items"])
        for it in norm["items"]:
            if it["kind"] == "user_text":
                first_user_text = it["text"]
                break
    fallback_items = [{"kind": "user_text", "text": first_user_text}] if (not err and first_user_text) else []
    title = resolve_title(state.get("title") or s.get("stateTitle") or "", fallback_items, s["sessionId"])
    return {
        "session_id": s["sessionId"],
        "title": title,
        "last_ts": fmt_time(state.get("updatedAt")) if state.get("updatedAt") else "",
        "count": count,
    }


def print_session_list(sessions, project_path, kimi_dir, limit=0):
    total = len(sessions)
    shown = sessions[:limit] if limit and limit > 0 else sessions
    print(f"当前项目: {project_path}")
    print(f"会话根: {os.path.join(kimi_dir, 'sessions')}")
    if limit and 0 < limit < total:
        print(f"找到 {total} 个会话（仅显示最近 {len(shown)} 个）：\n")
    else:
        print(f"找到 {total} 个会话：\n")
    for i, s in enumerate(shown):
        meta = session_meta_lite(s) or {}
        mark = "[最近]" if i == 0 else "      "
        title = meta.get("title") or "(无标题)"
        last_ts = meta.get("last_ts") or "(无时间)"
        count = meta.get("count", 0)
        print(f"{mark} {last_ts}  {s['sessionId'][:20]}  消息数:{count:<4} 标题: {title}")


# ---------- 选择会话 ----------

def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0]
    arg = session_arg.lower()
    for s in sessions:  # 先按 sessionId 前缀/包含匹配
        sid = s["sessionId"].lower()
        if sid.startswith(arg) or arg in sid:
            return s
    for s in sessions:  # 再按标题关键词匹配
        title = (s.get("stateTitle") or "").lower()
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


def main():
    setup_utf8_stdio()
    ap = argparse.ArgumentParser(
        description="读取 Kimi Code CLI 本地会话记录（session_index + state.json + wire.jsonl），生成结构化接管摘要。"
    )
    ap.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    ap.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    ap.add_argument("--session", default=None, help="指定会话 ID 或前缀（支持跨项目全局查找，也匹配标题关键词）")
    ap.add_argument("--project", default=None, help="项目路径，默认当前工作目录")
    ap.add_argument("--kimi-dir", default=None, help="Kimi Code 配置目录，默认 ~/.kimi-code 或 $KIMI_HOME")
    ap.add_argument("--recent", type=int, default=8, help="近期对话保留条目数，默认 8")
    ap.add_argument("--max-chars", type=int, default=1500, help="单条内容截断长度，默认 1500")
    ap.add_argument("--limit", type=int, default=0, help="--list 返回的会话数量上限，0 表示不限制")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    ap.add_argument("--output", default=None, help="将摘要写入文件")
    args = ap.parse_args()

    kimi_dir = args.kimi_dir or get_kimi_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    target = None
    project_path = project_path_arg
    sessions = []

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        sessions = scan_all_sessions(kimi_dir)
        target = pick_session(sessions, args.session)
        if target and target.get("workDir"):
            project_path = target["workDir"]

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if target is None:
        sessions = scan_sessions(kimi_dir, project_path_arg)
        if not sessions:
            print(f"错误：未找到项目 {project_path_arg} 对应的 Kimi Code 会话。", file=sys.stderr)
            print(f"已查找：{os.path.join(kimi_dir, 'sessions')}", file=sys.stderr)
            sys.exit(1)
        target = pick_session(sessions, args.session)
        if not target:
            print(f"错误：未匹配到会话 '{args.session}'。使用 --list 查看可用会话。", file=sys.stderr)
            sys.exit(1)

    if args.list:
        print_session_list(sessions, project_path, kimi_dir, args.limit)
        return

    session, err = load_session(target["sessionDir"], target["sessionId"], target["workDir"])
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
            "recent_items": session["normalized"]["items"][-args.recent:],
        }, ensure_ascii=False, indent=2)
    else:
        out = render_summary(
            session, state,
            recent_n=args.recent,
            max_chars=args.max_chars,
        )

    if args.output:
        # newline="" 禁用换行符转换，保证写入 LF（与 Node fs.writeFileSync 输出一致）。
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(out.encode("utf-8"))
        if not out.endswith("\n"):
            sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
