#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-workbuddy: 读取 WorkBuddy（腾讯 AI 智能体工作站）本地会话 JSONL，生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Claude Code / Grok / Codex 等）均可直接调用。
用法见同目录 SKILL.md。
"""

import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import sys

# AGENTS.md 约定：时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss
CST = timezone(timedelta(hours=8))

# 涉及文件的工具，用于「已调查文件 / 代码修改」归类
READ_TOOLS = {"Read", "Glob", "Grep"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit"}


# ---------- 路径与项目定位 ----------

def encode_project_path(path_str):
    """WorkBuddy 把项目绝对路径中的 : \\ / 替换为 - 并转为小写。"""
    cleaned = re.sub(r"[:\\/]+", "-", path_str).strip("-").lower()
    return cleaned


def get_workbuddy_dir():
    return os.environ.get(
        "WORKBUDDY_HOME",
        os.path.expanduser(os.path.join("~", ".workbuddy")),
    )


def peek_cwd(jsonl_path, max_lines=30):
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


def find_project_dir(workbuddy_dir, project_path):
    projects_root = os.path.join(workbuddy_dir, "projects")
    if not os.path.isdir(projects_root):
        return None

    encoded = encode_project_path(project_path)

    # 1. 精确匹配
    cand = os.path.join(projects_root, encoded)
    if os.path.isdir(cand):
        return cand

    # 2. 大小写无关匹配
    enc_low = encoded.lower()
    try:
        dirs = os.listdir(projects_root)
    except OSError:
        return None

    for d in dirs:
        if d.lower() == enc_low:
            full = os.path.join(projects_root, d)
            if os.path.isdir(full):
                return full

    # 3. 兜底：扫描会话文件中的 cwd 字段
    norm = os.path.normcase(os.path.normpath(project_path))
    scanned = 0
    for d in sorted(dirs):
        full = os.path.join(projects_root, d)
        if not os.path.isdir(full):
            continue
        try:
            files = os.listdir(full)
        except OSError:
            continue
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            scanned += 1
            if scanned > 300:
                break
            cwd = peek_cwd(os.path.join(full, fn))
            if cwd and os.path.normcase(os.path.normpath(cwd)) == norm:
                return full
        if scanned > 300:
            break
    return None


# ---------- 时间格式化 ----------

def fmt_time(ts):
    if not ts:
        return ""
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        else:
            val = float(str(ts).strip())
            dt = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return str(ts)


# ---------- JSONL 解析与归一化 ----------

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
    stripped = re.sub(r"<system-reminder[\s\S]*?</system-reminder>", "", raw, flags=re.IGNORECASE).strip()
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
        if "text" in output and isinstance(output["text"], str):
            return output["text"]
        if "content" in output and isinstance(output["content"], str):
            return output["content"]
        return json.dumps(output, ensure_ascii=False)
    return str(output)


def normalize_events(events):
    items = []
    ai_title = None
    cwd = None
    model = None
    tracked_files = set()
    tasks = []

    for ev in events:
        etype = ev.get("type")

        if ev.get("cwd") and not cwd:
            cwd = ev["cwd"]

        if etype == "ai-title":
            if ev.get("aiTitle"):
                ai_title = ev["aiTitle"]
            continue

        if etype == "reasoning":
            # 内部推理，严格跳过，不予输出
            continue

        if etype == "file-history-snapshot":
            backups = (ev.get("snapshot") or {}).get("trackedFileBackups")
            if isinstance(backups, dict):
                for fp in backups.keys():
                    tracked_files.add(fp)
            continue

        ts = ev.get("timestamp", "")

        if etype == "message":
            role = ev.get("role")
            if role == "user":
                text = extract_user_text(ev.get("content"))
                if text:
                    items.append({"kind": "user_text", "timestamp": ts, "text": text, "id": ev.get("id")})
            elif role == "assistant":
                if not model and ev.get("providerData"):
                    prov = ev["providerData"]
                    m = prov.get("model")
                    req_m = prov.get("requestModelName")
                    if m and req_m:
                        model = f"{m} ({req_m})"
                    else:
                        model = m or req_m or None
                text = extract_assistant_text(ev.get("content"))
                if text:
                    items.append({"kind": "assistant_text", "timestamp": ts, "text": text, "id": ev.get("id")})
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

            items.append({
                "kind": "tool_use",
                "timestamp": ts,
                "name": ev.get("name", ""),
                "input": inp,
                "call_id": ev.get("callId") or ev.get("id") or "",
            })
            continue

        if etype == "function_call_result":
            is_err = ev.get("status") == "error" or bool(ev.get("is_error", False))
            text = extract_result_content(ev.get("output"))

            # 若为 TaskList，尝试解析任务列表
            if ev.get("name") == "TaskList" and ev.get("providerData"):
                tr = (ev["providerData"].get("toolResult") or {})
                raw_resp = tr.get("rawResponse")
                if isinstance(raw_resp, dict) and isinstance(raw_resp.get("tasks"), list):
                    tasks.clear()
                    for t in raw_resp["tasks"]:
                        tasks.append({
                            "id": t.get("id"),
                            "subject": t.get("subject"),
                            "status": t.get("status"),
                            "description": t.get("description", ""),
                        })

            items.append({
                "kind": "tool_result",
                "timestamp": ts,
                "name": ev.get("name", ""),
                "content": text,
                "is_error": is_err,
                "call_id": ev.get("callId", ""),
            })
            continue

    return {
        "items": items,
        "ai_title": ai_title,
        "cwd": cwd,
        "model": model,
        "tracked_files": list(tracked_files),
        "tasks": tasks,
    }


# ---------- 标题解析 ----------

def resolve_title(ai_title, items, session_id):
    if ai_title:
        return ai_title
    for it in items:
        if it["kind"] == "user_text":
            trimmed = it["text"].strip()
            first_line = trimmed.splitlines()[0] if trimmed else ""
            return (first_line[:60] + "…") if len(first_line) > 60 else (first_line or session_id)
    return session_id


# ---------- 工具摘要与状态提取 ----------

def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def tool_file(name, inp):
    if name in {"Read", "Write", "Edit", "MultiEdit"}:
        return inp.get("file_path")
    if name in {"Glob", "Grep"}:
        return inp.get("path") or inp.get("glob") or inp.get("pattern")
    if name == "present_files" and isinstance(inp.get("files"), list) and inp["files"]:
        return inp["files"][0]
    return None


def tool_brief(name, inp):
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", " ")
        return f"Bash({truncate(cmd, 100)})"
    if name in {"Read", "Write", "Edit", "MultiEdit"} and inp.get("file_path"):
        return f"{name}({truncate(inp['file_path'], 100)})"
    if name == "Agent":
        desc = inp.get("description") or inp.get("name") or inp.get("prompt") or ""
        return f"Agent({truncate(desc, 80)})"
    if name == "TaskCreate":
        return f"TaskCreate({truncate(inp.get('subject') or '', 80)})"
    if name == "TaskUpdate":
        return f"TaskUpdate(#{inp.get('taskId') or ''} -> {inp.get('status') or ''})"
    if name == "TaskList":
        return "TaskList()"
    if name == "AskUserQuestion":
        questions = inp.get("questions") or []
        q_str = ", ".join(x.get("header") or x.get("question") or "" for x in questions if isinstance(x, dict))
        return f"AskUserQuestion({truncate(q_str or '...', 80)})"
    if name == "present_files":
        files = inp.get("files") or []
        f_str = ", ".join(os.path.basename(f) for f in files if isinstance(f, str))
        return f"present_files({truncate(f_str, 80)})"
    f = tool_file(name, inp)
    if f:
        return f"{name}({truncate(f, 100)})"
    for v in inp.values():
        if isinstance(v, str) and v:
            return f"{name}({truncate(v, 80)})"
    return f"{name}(...)"


def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b",
    re.IGNORECASE,
)
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE,
)


def build_state(items, tracked_files, tasks):
    files_read = []
    files_edited = list(tracked_files)
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
            inp = it["input"]
            last_tool_name = name
            if name in READ_TOOLS:
                f = tool_file(name, inp)
                if f:
                    files_read.append(f)
            elif name in EDIT_TOOLS:
                f = tool_file(name, inp)
                if f:
                    files_edited.append(f)
            elif name == "Bash":
                cmd = (inp.get("command") or "").strip()
                last_command = cmd
                if cmd:
                    commands.append(cmd)
        elif k == "tool_result":
            content = it.get("content") or ""
            is_err = bool(it.get("is_error"))
            is_test_cmd = last_tool_name == "Bash" and bool(TEST_CMD_RE.search(last_command))
            looks_test = is_test_cmd or (bool(content) and bool(TEST_RESULT_RE.search(content[:2000])))
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
        "tasks": tasks or [],
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
    ts = fmt_time(it["timestamp"])
    if k == "user_text":
        return [f"### [用户] {ts}", block(it["text"], max_chars)]
    if k == "assistant_text":
        return [f"### [助手] {ts}", block(it["text"], max_chars)]
    if k == "tool_use":
        inp_str = json.dumps(it["input"], ensure_ascii=False)
        return [f"### [工具调用] {it['name']} {ts}", "```", truncate(inp_str, max_chars), "```"]
    if k == "tool_result":
        tag = " (错误)" if it.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", block(it.get("content") or "", max_chars)]
    return []


def render_summary(session, state, recent_n=8, max_chars=1500, older_limit=60):
    lines = []
    info = session["info"]
    norm = session["normalized"]

    lines.append("# Resume-WorkBuddy 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("model"):
        lines.append(f"- 模型: {info['model']}")
    if info.get("subagent_count", 0) > 0:
        lines.append(f"- 子 agent 会话数: {info['subagent_count']}")
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
            tag = " [错误]" if tr.get("is_error") else ""
            trimmed = (tr.get("content") or "").strip()
            first_line = trimmed.splitlines()[0] if trimmed else ""
            lines.append(f"-{tag} {truncate(first_line, 200)}")
        lines.append("")

    if state.get("tasks"):
        lines.append("### 待办任务清单")
        for t in state["tasks"]:
            mark = "x" if t.get("status") == "completed" else " "
            lines.append(f"- [{mark}] #{t.get('id')} [{t.get('status')}] {t.get('subject')}")
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
        lines.append(f"## 更早活动（工具调用，仅最近 {older_limit} 条）")
        for it in older_tools[-older_limit:]:
            lines.append(f"- [{fmt_time(it['timestamp'])}] {tool_brief(it['name'], it['input'])}")
        lines.append("")

    lines.append("## 接管建议")
    lines.append("- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。")
    lines.append("- 以「任务状态重建」+「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。")
    lines.append("- 不要逐字复述历史；基于现状决定下一步动作。")
    lines.append("")

    return "\n".join(lines)


# ---------- 会话扫描 ----------

def scan_sessions(project_dir):
    sessions = []
    try:
        entries = os.listdir(project_dir)
    except OSError:
        return sessions

    for fn in entries:
        if not fn.endswith(".jsonl"):
            continue
        p = os.path.join(project_dir, fn)
        try:
            stat = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        sessions.append({
            "session_id": fn[:-len(".jsonl")],
            "path": p,
            "mtime": stat.st_mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_all_sessions(workbuddy_dir):
    projects_root = os.path.join(workbuddy_dir, "projects")
    if not os.path.isdir(projects_root):
        return []
    try:
        dirs = os.listdir(projects_root)
    except OSError:
        return []

    sessions = []
    for d in dirs:
        full = os.path.join(projects_root, d)
        if not os.path.isdir(full):
            continue
        sessions.extend(scan_sessions(full))
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def count_subagents(jsonl_path):
    base = jsonl_path[:-len(".jsonl")]
    subagent_dir = os.path.join(base, "subagents")
    if not os.path.isdir(subagent_dir):
        return 0
    try:
        return len([f for f in os.listdir(subagent_dir) if f.endswith(".jsonl")])
    except OSError:
        return 0


def session_meta(p):
    events, err = parse_events(p)
    if err:
        return None
    norm = normalize_events(events)
    items = norm["items"]
    sid = Path(p).stem
    timestamps = [it["timestamp"] for it in items if it.get("timestamp")]
    return {
        "session_id": sid,
        "title": resolve_title(norm["ai_title"], items, sid),
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
        "count": len(items),
        "model": norm.get("model") or "",
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
        count = meta.get("count") or 0
        sid = s["session_id"][:12]
        print(f"{mark} {last_ts}  {sid}  消息数:{str(count).ljust(4)} 标题: {title}")


# ---------- 主流程 ----------

def load_session(jsonl_path):
    events, err = parse_events(jsonl_path)
    if err:
        return None, err
    norm = normalize_events(events)
    items = norm["items"]
    sid = Path(jsonl_path).stem
    timestamps = [it["timestamp"] for it in items if it.get("timestamp")]
    subagent_count = count_subagents(jsonl_path)
    info = {
        "session_id": sid,
        "title": resolve_title(norm["ai_title"], items, sid),
        "cwd": norm["cwd"],
        "model": norm["model"],
        "subagent_count": subagent_count,
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
    }
    return {"info": info, "normalized": norm, "events": events}, None


def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0]
    for s in sessions:
        if s["session_id"].startswith(session_arg) or session_arg in s["session_id"]:
            return s
    return None


# ---------- CLI ----------

def build_parser():
    p = argparse.ArgumentParser(
        description="读取 WorkBuddy 本地会话 JSONL，生成结构化接管摘要。",
    )
    p.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    p.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    p.add_argument("--session", metavar="ID", help="指定会话 ID 或前缀（支持跨项目全局查找）")
    p.add_argument("--project", metavar="PATH", default=None, help="项目路径，默认当前工作目录")
    p.add_argument("--workbuddy-dir", metavar="DIR", default=None, help="WorkBuddy 配置目录，默认 ~/.workbuddy 或 $WORKBUDDY_HOME")
    p.add_argument("--recent", type=int, default=8, metavar="N", help="近期对话保留条目数，默认 8")
    p.add_argument("--max-chars", type=int, default=1500, metavar="N", help="单条内容截断长度，默认 1500")
    p.add_argument("--limit", type=int, default=0, metavar="N", help="--list 返回的会话数量上限，0 不限制（默认 0）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    p.add_argument("--output", metavar="FILE", default=None, help="将摘要写入文件")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    workbuddy_dir = args.workbuddy_dir or get_workbuddy_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    target = None
    project_dir = None
    project_path = project_path_arg

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        all_sessions = scan_all_sessions(workbuddy_dir)
        target = pick_session(all_sessions, args.session)
        if target:
            project_dir = os.path.dirname(target["path"])
            cwd = peek_cwd(target["path"])
            if cwd:
                project_path = cwd

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if not target:
        project_dir = find_project_dir(workbuddy_dir, project_path_arg)
        if not project_dir:
            print(f"错误：未找到项目 {project_path_arg} 对应的会话目录。", file=sys.stderr)
            print(f"已查找：{os.path.join(workbuddy_dir, 'projects')}", file=sys.stderr)
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

    norm = session["normalized"]
    state = build_state(norm["items"], norm["tracked_files"], norm["tasks"])

    if args.json:
        payload = {
            "info": session["info"],
            "state": {
                "goal": state["goal"],
                "files_read": state["files_read"],
                "files_edited": state["files_edited"],
                "commands": state["commands"],
                "test_results": state["test_results"],
                "tasks": state["tasks"],
                "last_user": state["last_user"],
                "last_assistant": state["last_assistant"],
            },
            "recent_items": norm["items"][-args.recent:],
        }
        out = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        out = render_summary(session, state, args.recent, args.max_chars)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
            if not out.endswith("\n"):
                f.write("\n")
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")


if __name__ == "__main__":
    main()
