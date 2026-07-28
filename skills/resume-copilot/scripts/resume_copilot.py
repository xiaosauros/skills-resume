#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-copilot: 读取 GitHub Copilot CLI 本地会话记录（~/.copilot/session-state 下的
workspace.yaml + events.jsonl），生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Claude Code / Codex / Grok / Kimi 等）均可直接调用。
与同目录 resume_copilot.js 功能等价、输出可互换。用法见同目录 SKILL.md。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude / resume-codex 一致）
CST = timezone(timedelta(hours=8))

# Session ID（UUID）正则
SID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b",
    re.IGNORECASE,
)
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE,
)
FILE_EDIT_RE = re.compile(r"\b(edit|create|apply_patch)\b", re.IGNORECASE)


# ---------- 路径与项目定位 ----------

def get_copilot_dir():
    return os.environ.get(
        "COPILOT_HOME",
        os.path.expanduser(os.path.join("~", ".copilot")),
    )


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


# ---------- workspace.yaml 解析 ----------

def parse_workspace_yaml(text):
    obj = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip()
        val = line[idx + 1 :].strip()
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


def scan_sessions(copilot_dir, project_path):
    norm = norm_path(project_path)
    sessions = []
    for s in list_session_dirs(copilot_dir):
        ws = load_workspace(s["dir"])
        if not ws:
            continue
        cwd = ws.get("cwd", "")
        if not cwd or norm_path(cwd) != norm:
            continue
        sessions.append(
            {
                "session_id": s["session_id"],
                "dir": s["dir"],
                "cwd": cwd,
                "mtime": get_mtime(s["dir"]),
                "workspace": ws,
            }
        )
    sessions.sort(key=lambda x: x["mtime"], reverse=True)
    return sessions


def scan_all_sessions(copilot_dir):
    sessions = []
    for s in list_session_dirs(copilot_dir):
        ws = load_workspace(s["dir"])
        cwd = ws.get("cwd", "") if ws else ""
        sessions.append(
            {
                "session_id": s["session_id"],
                "dir": s["dir"],
                "cwd": cwd,
                "mtime": get_mtime(s["dir"]),
                "workspace": ws,
            }
        )
    sessions.sort(key=lambda x: x["mtime"], reverse=True)
    return sessions


# ---------- 时间格式化 ----------

def fmt_time(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    t = d.astimezone(CST)
    return t.strftime("%Y-%m-%d %H:%M:%S")


# ---------- events.jsonl 解析 ----------

def parse_events(jsonl_path):
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return {"events": [], "err": str(e)}
    events = []
    for line in content.splitlines():
        l = line.strip()
        if not l:
            continue
        try:
            events.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return {"events": events, "err": None}


# ---------- 事件归一化 ----------

def output_to_text(output):
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output, ensure_ascii=False, indent=2)
        except TypeError:
            return str(output)
    return str(output)


def extract_shell_cmd(arguments_):
    if isinstance(arguments_, str):
        return arguments_
    if isinstance(arguments_, dict):
        if isinstance(arguments_.get("command"), str):
            return arguments_["command"]
        if isinstance(arguments_.get("cmd"), str):
            return arguments_["cmd"]
    return None


def normalize_events(events):
    items = []
    summaries = []
    cwd = None
    session_id = None
    copilot_version = None
    model = None
    mode = None

    pending_tool_results = {}

    for ev in events:
        etype = ev.get("type")
        data = ev.get("data") or {}
        ts = ev.get("timestamp", "")

        if etype == "session.start":
            if not session_id and data.get("sessionId"):
                session_id = data["sessionId"]
            if not copilot_version and data.get("copilotVersion"):
                copilot_version = data["copilotVersion"]
            ctx = data.get("context") or {}
            if not cwd and ctx.get("cwd"):
                cwd = ctx["cwd"]
            continue

        if etype == "session.model_change":
            if data.get("newModel"):
                model = data["newModel"]
            elif data.get("previousModel") and not model:
                model = data["previousModel"]
            continue

        if etype == "session.mode_changed":
            if data.get("newMode"):
                mode = data["newMode"]
            continue

        if etype == "session.info":
            if data.get("message"):
                summaries.append(data["message"])
            continue

        if etype == "session.error":
            if data.get("message"):
                summaries.append(f"[会话错误] {data['message']}")
            continue

        if etype == "user.message":
            text = str(data.get("content") or "").strip()
            if text:
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
            continue

        if etype == "assistant.message":
            content = str(data.get("content") or "").strip()
            if content:
                items.append({"kind": "assistant_text", "timestamp": ts, "text": content})
            if data.get("model"):
                model = data["model"]
            for tr in data.get("toolRequests") or []:
                items.append(
                    {
                        "kind": "tool_use",
                        "timestamp": ts,
                        "name": tr.get("name", ""),
                        "input": tr.get("arguments") or {},
                        "call_id": tr.get("toolCallId", ""),
                        "intention": tr.get("intentionSummary", ""),
                    }
                )
            continue

        if etype == "tool.execution_start":
            continue

        if etype == "tool.execution_complete":
            items.append(
                {
                    "kind": "tool_result",
                    "timestamp": ts,
                    "call_id": data.get("toolCallId", ""),
                    "content": output_to_text(data.get("result")),
                    "is_error": data.get("success") is False,
                }
            )
            continue

        if etype == "permission.requested":
            pr = data.get("permissionRequest") or {}
            if pr.get("kind") == "shell" and pr.get("fullCommandText"):
                items.append(
                    {
                        "kind": "tool_use",
                        "timestamp": ts,
                        "name": "bash",
                        "input": {"command": pr["fullCommandText"]},
                        "call_id": data.get("requestId", ""),
                        "intention": pr.get("intention", ""),
                    }
                )
            continue

        if etype == "permission.completed":
            pending_tool_results[data.get("toolCallId") or data.get("requestId", "")] = {
                "kind": "tool_result",
                "timestamp": ts,
                "call_id": data.get("toolCallId") or data.get("requestId", ""),
                "content": output_to_text(data.get("result")),
                "is_error": False,
            }
            continue

    i = 0
    while i < len(items):
        it = items[i]
        if it["kind"] == "tool_use" and it.get("call_id") and it["call_id"] in pending_tool_results:
            items.insert(i + 1, pending_tool_results.pop(it["call_id"]))
            i += 1
        i += 1
    for it in pending_tool_results.values():
        items.append(it)

    return {
        "items": items,
        "summaries": summaries,
        "cwd": cwd,
        "session_id": session_id,
        "copilot_version": copilot_version,
        "model": model,
        "mode": mode,
    }


# ---------- 标题解析 ----------

def resolve_title(workspace, items, session_id):
    if workspace and workspace.get("name"):
        return workspace["name"]
    for it in items:
        if it["kind"] == "user_text":
            first_line = it["text"].split("\n")[0].strip()
            if first_line:
                return first_line if len(first_line) <= 60 else first_line[:60] + "…"
    return session_id


# ---------- 工具输入摘要 ----------

def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def tool_brief(name, inp):
    if name in ("bash", "shell"):
        cmd = extract_shell_cmd(inp) or (inp.get("raw") if isinstance(inp, dict) else "") or ""
        return f"bash({truncate(str(cmd).replace(chr(10), ' '), 100)})"
    if isinstance(inp, dict):
        for v in inp.values():
            if isinstance(v, str) and v:
                return f"{name}({truncate(v, 80)})"
    return f"{name}(...)"


# ---------- 任务状态重建 ----------

def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def collect_files_from_tool_input(name, input_):
    files = []
    if not isinstance(input_, dict):
        return files
    if name in ("view", "read"):
        if isinstance(input_.get("path"), str):
            files.append(input_["path"])
        if isinstance(input_.get("paths"), list):
            for p in input_["paths"]:
                if isinstance(p, str):
                    files.append(p)
    elif name in ("edit", "create", "apply_patch"):
        if isinstance(input_.get("path"), str):
            files.append(input_["path"])
        if name == "apply_patch" and isinstance(input_.get("patch"), str):
            m = re.search(r"---\s+(.+)", input_["patch"])
            if m:
                files.append(m.group(1).strip().lstrip("a/"))
    return files


def build_state(items):
    commands = []
    test_results = []
    files_read = []
    files_edited = []
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
            input_ = it.get("input") or {}
            last_tool_name = name
            if name in ("bash", "shell"):
                cmd = extract_shell_cmd(input_)
                if cmd:
                    last_command = cmd
                    commands.append(cmd)
            else:
                last_command = ""
                if FILE_EDIT_RE.search(name):
                    files_edited.extend(collect_files_from_tool_input(name, input_))
                elif name in ("view", "read"):
                    files_read.extend(collect_files_from_tool_input(name, input_))
        elif k == "tool_result":
            content = it.get("content") or ""
            is_test_cmd = last_tool_name in ("bash", "shell") and TEST_CMD_RE.search(last_command)
            looks_test = is_test_cmd or (bool(content) and TEST_RESULT_RE.search(content[:2000]))
            is_err = (
                re.search(r"(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)", content[:2000], re.IGNORECASE)
                and not re.search(r"no error|0 failed", content[:2000], re.IGNORECASE)
            )
            if (looks_test or is_err) and content.strip():
                test_results.append(
                    {
                        "command_hint": last_command or last_tool_name,
                        "is_error": is_err,
                        "content": content,
                    }
                )

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

def py_json_stringify(obj):
    if obj is None:
        return "null"
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, list):
        return "[" + ", ".join(py_json_stringify(x) for x in obj) + "]"
    if isinstance(obj, dict):
        items = [json.dumps(k, ensure_ascii=False) + ": " + py_json_stringify(v) for k, v in obj.items()]
        return "{" + ", ".join(items) + "}"
    return json.dumps(obj, ensure_ascii=False)


def block(text, max_chars):
    if not text or not str(text).strip():
        return ""
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断，原长 {len(text)} 字符）"
    return text


def render_item(it, max_chars):
    k = it["kind"]
    ts = fmt_time(it.get("timestamp", ""))
    if k == "user_text":
        return [f"### [用户] {ts}", block(it["text"], max_chars)]
    if k == "assistant_text":
        return [f"### [助手] {ts}", block(it["text"], max_chars)]
    if k == "tool_use":
        extra = f" # {it['intention']}" if it.get("intention") else ""
        return [
            f"### [工具调用] {it['name']}{extra} {ts}",
            "```",
            truncate(py_json_stringify(it.get("input")), max_chars),
            "```",
        ]
    if k == "tool_result":
        tag = " (错误)" if it.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", block(it.get("content", ""), max_chars)]
    return []


def render_summary(session, state, recent_n=8, max_chars=1500, older_limit=60):
    lines = []
    info = session["info"]
    norm = session["normalized"]

    lines.append("# Resume-Copilot 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("copilot_version"):
        lines.append(f"- Copilot CLI 版本: {info['copilot_version']}")
    if info.get("model"):
        lines.append(f"- 模型: {info['model']}")
    if info.get("mode"):
        lines.append(f"- 模式: {info['mode']}")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(norm['items'])}")
    lines.append("")

    if norm["summaries"]:
        lines.append("## 历史摘要")
        for s in norm["summaries"]:
            lines.append(f"- {truncate(s, max_chars)}")
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
            trimmed = tr["content"].strip()
            first_line = trimmed.split("\n")[0] if trimmed else ""
            lines.append(f"-{tag} {truncate(first_line, 200)}")
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
            lines.append(f"- [{fmt_time(it.get('timestamp', ''))}] {tool_brief(it['name'], it.get('input'))}")
        lines.append("")

    lines.append("## 接管建议")
    lines.append("- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。")
    lines.append("- 以「任务状态重建」+「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。")
    lines.append("- 不要逐字复述历史；基于现状决定下一步动作。")
    lines.append("")

    return "\n".join(lines)


# ---------- 轻量列表 ----------

def session_meta_lite(s):
    sid = s["session_id"]
    ws = s.get("workspace") or {}
    events_path = os.path.join(s["dir"], "events.jsonl")
    res = parse_events(events_path)
    if res["err"]:
        return {"session_id": sid, "title": ws.get("name") or sid, "first_ts": "", "last_ts": "", "count": 0}
    norm = normalize_events(res["events"])
    items = norm["items"]
    timestamps = [it["timestamp"] for it in items if it.get("timestamp")]
    return {
        "session_id": sid,
        "title": resolve_title(ws, items, sid),
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
        "count": len(items),
    }


def print_session_list(sessions, project_path, copilot_dir, limit=0):
    total = len(sessions)
    shown = sessions[:limit] if limit and limit > 0 else sessions
    print(f"当前项目: {project_path}")
    print(f"会话根: {os.path.join(copilot_dir, 'session-state')}")
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
        print(f"{mark} {last_ts}  {s['session_id'][:12]}  消息数:{str(count).ljust(4)} 标题: {title}")


# ---------- 主流程 ----------

def load_session(session_dir, workspace):
    events_path = os.path.join(session_dir, "events.jsonl")
    res = parse_events(events_path)
    if res["err"]:
        return {"session": None, "err": res["err"]}
    norm = normalize_events(res["events"])
    items = norm["items"]
    sid = (workspace or {}).get("id") or norm.get("session_id") or os.path.basename(session_dir)
    timestamps = [it["timestamp"] for it in items if it.get("timestamp")]
    info = {
        "session_id": sid,
        "title": resolve_title(workspace, items, sid),
        "cwd": (workspace or {}).get("cwd") or norm.get("cwd") or "",
        "git_root": (workspace or {}).get("git_root") or "",
        "branch": (workspace or {}).get("branch") or "",
        "copilot_version": norm.get("copilot_version") or "",
        "model": norm.get("model") or "",
        "mode": norm.get("mode") or "",
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
    }
    return {"session": {"info": info, "normalized": norm, "events": res["events"]}, "err": None}


def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0] if sessions else None
    for s in sessions:
        if s["session_id"].startswith(session_arg) or session_arg in s["session_id"]:
            return s
    return None


# ---------- CLI ----------

def print_help():
    print(
        """用法: python -X utf8 resume_copilot.py [选项]

读取 GitHub Copilot CLI 本地会话记录（~/.copilot/session-state 下的 workspace.yaml + events.jsonl），
生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID 或前缀（支持跨项目全局查找）
  --project PATH      项目路径，默认当前工作目录
  --copilot-dir DIR   Copilot CLI 配置目录，默认 ~/.copilot 或 $COPILOT_HOME
  --recent N          近期对话保留条目数，默认 8
  --max-chars N       单条内容截断长度，默认 1500
  --limit N           --list 返回的会话数量上限，0 不限制（默认 0）
  --json              以 JSON 输出（机器可读）
  --output FILE       将摘要写入文件
  -h, --help          显示此帮助
"""
    )


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--session", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--copilot-dir", default=None, dest="copilot_dir")
    parser.add_argument("--recent", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=1500, dest="max_chars")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", default=None)
    parser.add_argument("-h", "--help", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.help:
        print_help()
        return

    copilot_dir = args.copilot_dir or get_copilot_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    target = None
    project_path = project_path_arg
    sessions = []

    if args.session:
        sessions = scan_all_sessions(copilot_dir)
        target = pick_session(sessions, args.session)
        if target and target.get("cwd"):
            project_path = target["cwd"]

    if not target:
        sessions = scan_sessions(copilot_dir, project_path_arg)
        if not sessions:
            print(f"错误：未找到项目 {project_path_arg} 对应的 Copilot CLI 会话。", file=sys.stderr)
            print(f"已查找：{os.path.join(copilot_dir, 'session-state')}", file=sys.stderr)
            sys.exit(1)
        target = pick_session(sessions, args.session)
        if not target:
            print(f"错误：未匹配到会话 '{args.session}'。使用 --list 查看可用会话。", file=sys.stderr)
            sys.exit(1)

    if args.list:
        print_session_list(sessions, project_path, copilot_dir, args.limit)
        return

    ws = load_workspace(target["dir"]) or {}
    res = load_session(target["dir"], ws)
    if res["err"]:
        print(f"错误：解析会话失败：{res['err']}", file=sys.stderr)
        sys.exit(1)

    session = res["session"]
    state = build_state(session["normalized"]["items"])

    if args.json:
        out = json.dumps(
            {
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
                "recent_items": session["normalized"]["items"][-args.recent :],
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        out = render_summary(session, state, args.recent, args.max_chars)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")


if __name__ == "__main__":
    main()
