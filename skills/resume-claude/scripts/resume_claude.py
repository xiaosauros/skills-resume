#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-claude: 读取 Claude Code 本地会话 JSONL，生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Claude Code / Grok / Codex 等）均可直接调用。
用法见同目录 SKILL.md。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# AGENTS.md 约定：时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss
CST = timezone(timedelta(hours=8))

# 涉及文件的工具，用于「已调查文件 / 代码修改」归类
READ_TOOLS = {"Read", "Glob", "Grep"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


# ---------- 路径与项目定位 ----------

def encode_project_path(path):
    """Claude Code 把项目绝对路径中的 : \\ / 替换为 - 作为目录名。"""
    return path.replace(":", "-").replace("\\", "-").replace("/", "-")


def get_claude_dir():
    return os.environ.get(
        "CLAUDE_CONFIG_DIR",
        os.path.expanduser(os.path.join("~", ".claude")),
    )


def peek_cwd(jsonl_path, max_lines=30):
    """读取前若干行，取首个带 cwd 字段的事件，用于兜底匹配项目。"""
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


def find_project_dir(claude_dir, project_path):
    """定位当前项目对应的 ~/.claude/projects/<encoded> 目录。"""
    projects_root = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_root):
        return None

    encoded = encode_project_path(project_path)

    # 1. 精确匹配
    cand = os.path.join(projects_root, encoded)
    if os.path.isdir(cand):
        return cand

    # 2. 大小写无关匹配
    enc_low = encoded.lower()
    for d in os.listdir(projects_root):
        if d.lower() == enc_low:
            return os.path.join(projects_root, d)

    # 3. 兜底：扫描会话文件中的 cwd 字段（限量，避免过慢）
    norm = os.path.normcase(os.path.normpath(project_path))
    scanned = 0
    for d in sorted(os.listdir(projects_root)):
        full = os.path.join(projects_root, d)
        if not os.path.isdir(full):
            continue
        for fn in os.listdir(full):
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

def fmt_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
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
    """从 message.content 中提取纯文本（content 可能是 str 或 block 列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def extract_tool_uses(content):
    uses = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append({
                    "name": block.get("name", ""),
                    "input": block.get("input", {}) or {},
                })
    return uses


def extract_tool_results(content):
    results = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, list):
                    text = "\n".join(
                        b.get("text", "")
                        for b in c
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = "" if c is None else str(c)
                results.append({
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": text,
                    "is_error": bool(block.get("is_error", False)),
                })
    return results


def normalize_events(events):
    """把原始 JSONL 事件拍平为有序的归一化条目列表。"""
    items = []  # 每项: {kind, ...} kind ∈ user_text/assistant_text/tool_use/tool_result
    summaries = []
    titles = {"custom-title": None, "ai-title": None, "agent-name": None}
    cwd = None
    git_branch = None
    is_sidechain = False

    for ev in events:
        etype = ev.get("type")

        if etype == "summary":
            summaries.append(ev.get("summary", ""))
            continue

        if etype in titles:
            key = {
                "custom-title": "customTitle",
                "ai-title": "aiTitle",
                "agent-name": "agentName",
            }[etype]
            titles[etype] = ev.get(key)
            continue

        if ev.get("cwd") and not cwd:
            cwd = ev["cwd"]
        if ev.get("gitBranch") and not git_branch:
            git_branch = ev["gitBranch"]
        if ev.get("isSidechain"):
            is_sidechain = True

        if etype not in ("user", "assistant"):
            continue

        msg = ev.get("message", {}) or {}
        content = msg.get("content")
        ts = ev.get("timestamp", "")

        if etype == "user":
            # tool_result 作为工具结果条目
            for tr in extract_tool_results(content):
                items.append({"kind": "tool_result", "timestamp": ts, **tr})
            text = extract_text(content)
            # 过滤掉纯 system-reminder 噪声
            text = strip_system_reminders(text)
            if text.strip():
                items.append({"kind": "user_text", "timestamp": ts, "text": text})

        elif etype == "assistant":
            text = extract_text(content)
            if text.strip():
                items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
            for tu in extract_tool_uses(content):
                items.append({
                    "kind": "tool_use",
                    "timestamp": ts,
                    "name": tu["name"],
                    "input": tu["input"],
                })

    return {
        "items": items,
        "summaries": summaries,
        "titles": titles,
        "cwd": cwd,
        "git_branch": git_branch,
        "is_sidechain": is_sidechain,
    }


SYS_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def strip_system_reminders(text):
    if not text:
        return text
    return SYS_REMINDER_RE.sub("", text).strip()


# ---------- 标题解析 ----------

def resolve_title(titles, items, session_id):
    """按优先级 custom-title > ai-title > agent-name > 兜底。"""
    if titles.get("custom-title"):
        return titles["custom-title"]
    if titles.get("ai-title"):
        return titles["ai-title"]
    if titles.get("agent-name"):
        return titles["agent-name"]
    # 兜底：首条用户消息摘要
    for it in items:
        if it["kind"] == "user_text":
            t = it["text"].strip().splitlines()[0] if it["text"].strip() else ""
            return (t[:60] + "…") if len(t) > 60 else t or session_id
    return session_id


# ---------- 工具输入摘要 ----------

def tool_file(name, inp):
    if name in {"Read", "Write", "Edit", "MultiEdit"}:
        return inp.get("file_path")
    if name == "NotebookEdit":
        return inp.get("notebook_path")
    if name in {"Glob", "Grep"}:
        return inp.get("path") or inp.get("glob") or inp.get("pattern")
    return None


def tool_brief(name, inp):
    """单行紧凑描述一个工具调用。"""
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", " ")
        return f"Bash({truncate(cmd, 100)})"
    f = tool_file(name, inp)
    if f:
        return f"{name}({truncate(f, 100)})"
    if name in {"Task", "Agent"}:
        return f"{name}({truncate(inp.get('description') or inp.get('subagent_type') or '', 80)})"
    if name == "TodoWrite":
        return "TodoWrite(...)"
    # 其它工具：取首个字符串型入参
    for v in inp.values():
        if isinstance(v, str) and v:
            return f"{name}({truncate(v, 80)})"
    return f"{name}(...)"


def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


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


def build_state(items):
    """从归一化条目提取结构化任务状态。"""
    files_read = []
    files_edited = []
    commands = []
    test_results = []
    first_user = ""
    last_user = ""
    last_assistant = ""

    # tool_use 与其紧随的 tool_result 通过 tool_use_id 关联困难（tool_use 的 id 未保留），
    # 这里用顺序启发：tool_result 紧跟在对应 tool_use 之后。
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
            content = it.get("content", "")
            is_err = it.get("is_error", False)
            is_test_cmd = last_tool_name == "Bash" and bool(
                TEST_CMD_RE.search(last_command)
            )
            looks_test = bool(
                is_test_cmd
                or (content and TEST_RESULT_RE.search(content[:2000]))
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


def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------- Markdown 渲染 ----------

def render_summary(session, state, recent_n=8, max_chars=1500, older_limit=60):
    lines = []
    info = session["info"]
    norm = session["normalized"]

    lines.append("# Resume-Claude 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("git_branch"):
        lines.append(f"- Git 分支: {info['git_branch']}")
    if norm.get("is_sidechain"):
        lines.append("- 类型: 子 agent 会话 (sidechain)")
    lines.append(f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}")
    lines.append(f"- 消息条目数: {len(norm['items'])}")
    lines.append("")

    # 历史摘要
    if norm["summaries"]:
        lines.append("## 历史摘要（原会话 compact）")
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
            lines.append(f"-{tag} {truncate(tr['content'].strip().splitlines()[0] if tr['content'].strip() else '', 200)}")
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
    if k == "tool_use":
        return [f"### [工具调用] {it['name']} {ts}", "```", truncate(json.dumps(it["input"], ensure_ascii=False), max_chars), "```"]
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
            "session_id": fn[:-len(".jsonl")],
            "path": path,
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_all_sessions(claude_dir):
    """跨项目扫描：遍历 ~/.claude/projects 下所有项目目录的会话。按 mtime 倒序。"""
    projects_root = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_root):
        return []
    sessions = []
    for d in os.listdir(projects_root):
        full = os.path.join(projects_root, d)
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
    sid = os.path.splitext(os.path.basename(path))[0]
    timestamps = [it.get("timestamp") for it in items if it.get("timestamp")]
    return {
        "session_id": sid,
        "title": resolve_title(norm["titles"], items, sid),
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
    sid = os.path.splitext(os.path.basename(jsonl_path))[0]
    timestamps = [it.get("timestamp") for it in items if it.get("timestamp")]
    title = resolve_title(norm["titles"], items, sid)
    info = {
        "session_id": sid,
        "title": title,
        "cwd": norm.get("cwd"),
        "git_branch": norm.get("git_branch"),
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
    }
    return {"info": info, "normalized": norm, "events": events}, None


def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0]  # 最近
    for s in sessions:
        if s["session_id"].startswith(session_arg) or session_arg in s["session_id"]:
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
        description="读取 Claude Code 本地会话 JSONL，生成结构化接管摘要。"
    )
    ap.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    ap.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    ap.add_argument("--session", default=None, help="指定会话 ID 或前缀（支持跨项目全局查找）")
    ap.add_argument("--project", default=None, help="项目路径，默认当前工作目录")
    ap.add_argument("--claude-dir", default=None, help="Claude 配置目录，默认 ~/.claude 或 $CLAUDE_CONFIG_DIR")
    ap.add_argument("--recent", type=int, default=8, help="近期对话保留条目数，默认 8")
    ap.add_argument("--max-chars", type=int, default=1500, help="单条内容截断长度，默认 1500")
    ap.add_argument("--limit", type=int, default=0, help="--list 返回的会话数量上限，0 表示不限制")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    ap.add_argument("--output", default=None, help="将摘要写入文件")
    args = ap.parse_args()

    claude_dir = args.claude_dir or get_claude_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    target = None
    project_dir = None
    project_path = project_path_arg

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        all_sessions = scan_all_sessions(claude_dir)
        target = pick_session(all_sessions, args.session)
        if target:
            project_dir = os.path.dirname(target["path"])
            cwd = peek_cwd(target["path"])
            if cwd:
                project_path = cwd

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if target is None:
        project_dir = find_project_dir(claude_dir, project_path_arg)
        if not project_dir:
            print(f"错误：未找到项目 {project_path_arg} 对应的会话目录。", file=sys.stderr)
            print(f"已查找：{os.path.join(claude_dir, 'projects')}", file=sys.stderr)
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
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(out.encode("utf-8"))
        if not out.endswith("\n"):
            sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
