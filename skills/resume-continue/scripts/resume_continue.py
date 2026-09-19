#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 Continue（VS Code/JetBrains 扩展与 cn CLI 共用）本地会话（~/.continue/sessions/*.json），生成接管摘要。"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


def norm_tool(name):
    return re.sub(r"[\s_-]+", "", str(name or "")).lower()


# Continue 内置工具（新版 snake_case / 旧版 camelCase 两套命名并存），键为去掉分隔符的统一形式
READ_TOOLS = {
    "readfile", "readfilerange", "readcurrentlyopenfile",
    "grepsearch", "fileglobsearch", "globsearch", "searchcodebase",
    "ls", "codebase", "codebasetool", "readskill",
    "viewdiff", "viewrepomap", "viewsubdirectory",
    "searchweb", "fetchurlcontent",
}
EDIT_TOOLS = {
    "editexistingfile", "editfile", "singlefindandreplace", "multiedit", "createnewfile",
}
SHELL_TOOLS = {"runterminalcommand", "runcommand"}
# 未收录工具的命名启发式（MCP 等自定义工具）
READ_HINT_RE = re.compile(r"read|grep|search|glob|list|\bls\b|view|fetch|web|codebase|docs?|skill", re.IGNORECASE)
EDIT_HINT_RE = re.compile(r"edit|write|create|apply|patch|replace|insert", re.IGNORECASE)
SHELL_HINT_RE = re.compile(r"terminal|command|exec|shell|bash|run\b|process", re.IGNORECASE)
TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|"
    r"cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b",
    re.IGNORECASE,
)
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|"
    r"\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE,
)
UNTITLED_TITLES = {"", "untitled session", "new session", "新会话", "未命名会话"}
PATH_KEYS = ("filepath", "file_path", "filepath_", "path", "file", "filename", "directory", "directorypath", "url", "uri")
TOOL_STATUS_ERROR = {"errored", "error", "failed"}


def setup_utf8_stdio():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="")
        sys.stderr.reconfigure(encoding="utf-8", newline="")


def default_sessions_dirs():
    candidates = []
    override = os.environ.get("CONTINUE_DATA_DIR")
    if override:
        candidates.append(Path(override).expanduser())
    global_dir = os.environ.get("CONTINUE_GLOBAL_DIR")
    if global_dir:
        candidates.append(Path(global_dir).expanduser() / "sessions")
    candidates.append(Path.home() / ".continue" / "sessions")
    return candidates


def norm_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def parse_json(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    if default is None and isinstance(value, str) and not value.strip():
        return {}
    if value is None or value == "":
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def timestamp_ms(value):
    if value is None or value == "":
        return 0
    try:
        number = float(value)
        return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0


def fmt_time(value):
    ms = timestamp_ms(value)
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def file_times(path):
    stat = path.stat()
    created = int(getattr(stat, "st_birthtime", 0) or 0) * 1000 or int(stat.st_ctime * 1000)
    if os.name != "nt" and not hasattr(stat, "st_birthtime"):
        created = 0  # 非 Windows 且无 birthtime 时 ctime 是 inode 变更时间，不用作创建时间
    return created, int(stat.st_mtime * 1000)


def sessions_list_created(sessions_dir):
    """sessions.json 里的 dateCreated（创建时刻，毫秒字符串）作为创建时间兜底。"""
    by_id = {}
    text = read_text(sessions_dir / "sessions.json")
    entries = parse_json(text, None)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
                by_id[entry["sessionId"]] = timestamp_ms(entry.get("dateCreated"))
    return by_id


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def scan_sessions(sessions_dirs):
    sessions = []
    for sessions_dir in sessions_dirs:
        if not sessions_dir.is_dir():
            continue
        created_by_id = {}
        entries = []
        try:
            entries = sorted(
                (entry for entry in sessions_dir.iterdir() if entry.is_file() and entry.suffix == ".json" and entry.name != "sessions.json"),
                key=lambda entry: entry.stat().st_mtime,
            )
        except OSError:
            continue
        for entry in entries:
            data = parse_json(read_text(entry), None)
            if not isinstance(data, dict) or not isinstance(data.get("history"), list):
                continue
            created, updated = 0, 0
            try:
                created, updated = file_times(entry)
            except OSError:
                pass
            session_id = str(data.get("sessionId") or data.get("session_id") or entry.stem)
            if not created:
                if not created_by_id:
                    created_by_id = sessions_list_created(sessions_dir)
                created = created_by_id.get(session_id, 0) or created
            sessions.append(
                {
                    "session_id": session_id,
                    "title": str(data.get("title") or "").strip(),
                    "first_user": first_user_text(data["history"]),
                    "directory": str(data.get("workspaceDirectory") or data.get("workspace") or "").strip(),
                    "mode": str(data.get("mode") or "").strip(),
                    "chat_model_title": str(data.get("chatModelTitle") or "").strip(),
                    "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
                    "created": created,
                    "updated": updated,
                    "source": f"continue-sessions:{sessions_dir.parent.name}",
                    "path": entry,
                    "_history": data["history"],
                }
            )
    sessions.sort(key=lambda meta: -(meta["updated"] or meta["created"]))
    return sessions


def message_content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "imageUrl":
                parts.append("[图片]")
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(part for part in parts if part)


def tool_status_is_error(status):
    return str(status or "").lower() in TOOL_STATUS_ERROR


def normalize_history(history):
    items, summaries = [], []
    state_outputs = {}  # toolCallId -> 已由 role:"tool" 消息给出结果，避免与 toolCallStates.output 重复
    for entry in history:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        role = message.get("role") or ""
        summary = entry.get("conversationSummary")
        if isinstance(summary, str) and summary.strip():
            summaries.append(summary.strip())
        if role == "user":
            text = message_content_text(message.get("content")).strip()
            if text:
                items.append({"kind": "user_text", "timestamp": 0, "text": text})
            for context in entry.get("contextItems") or []:
                if not isinstance(context, dict):
                    continue
                label = str(context.get("name") or "").strip()
                uri = context.get("uri") or {}
                value = str(uri.get("value") or "").strip() if isinstance(uri, dict) else ""
                if value and value != label:
                    label = f"{label} ({value})" if label else value
                if label:
                    items.append({"kind": "attachment", "timestamp": 0, "text": label})
        elif role == "assistant":
            text = message_content_text(message.get("content")).strip()
            if text:
                items.append({"kind": "assistant_text", "timestamp": 0, "text": text})
            states = entry.get("toolCallStates") if isinstance(entry.get("toolCallStates"), list) else []
            state_ids = set()
            for state in states:
                if not isinstance(state, dict):
                    continue
                call = state.get("toolCall") if isinstance(state.get("toolCall"), dict) else {}
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or state.get("toolCallId") or "tool")
                call_id = str(call.get("id") or state.get("toolCallId") or "")
                state_ids.add(call_id)
                input_data = first_defined(
                    state.get("parsedArgs"),
                    state.get("processedArgs"),
                    parse_json(function.get("arguments"), None),
                )
                if not isinstance(input_data, dict):
                    input_data = {"raw": input_data} if input_data else {}
                items.append({"kind": "tool_use", "timestamp": 0, "name": name, "input": input_data, "tool_use_id": call_id})
                status = state.get("status")
                if call_id not in state_outputs and (state.get("output") is not None or tool_status_is_error(status)):
                    content = context_items_text(state.get("output"))
                    items.append(
                        {
                            "kind": "tool_result",
                            "timestamp": 0,
                            "tool_use_id": call_id,
                            "content": content,
                            "is_error": tool_status_is_error(status),
                        }
                    )
            # 旧版本仅在 message.toolCalls 上记录调用（无状态对象）
            for call in message.get("toolCalls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_id = str(call.get("id") or "")
                if call_id and call_id in state_ids:
                    continue
                name = str(function.get("name") or "tool")
                input_data = parse_json(function.get("arguments"), None)
                if not isinstance(input_data, dict):
                    input_data = {"raw": input_data} if input_data else {}
                items.append({"kind": "tool_use", "timestamp": 0, "name": name, "input": input_data, "tool_use_id": call_id})
        elif role == "tool":
            call_id = str(message.get("toolCallId") or message.get("tool_call_id") or "")
            content = message_content_text(message.get("content"))
            if call_id:
                state_outputs[call_id] = True
            items.append(
                {
                    "kind": "tool_result",
                    "timestamp": 0,
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": False,
                }
            )
        # thinking / system 角色不进入接管摘要
    return {"items": items, "summaries": summaries, "tokens": accumulate_tokens(history)}


def first_defined(*values):
    for value in values:
        if value is not None:
            return value
    return None


def context_items_text(output):
    if not isinstance(output, list):
        return "" if output is None else str(output)
    parts = []
    for entry in output:
        if not isinstance(entry, dict):
            parts.append("" if entry is None else str(entry))
        elif entry.get("content"):
            parts.append(str(entry["content"]))
        elif entry.get("description"):
            parts.append(str(entry["description"]))
    return "\n".join(part for part in parts if part)


def accumulate_tokens(history):
    prompt = completion = 0
    for entry in history:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        try:
            prompt += int(usage.get("promptTokens") or 0)
            completion += int(usage.get("completionTokens") or 0)
        except (TypeError, ValueError):
            pass
    return {"input": prompt, "output": completion}


def classify_tool(name):
    lowered = norm_tool(name)
    if not lowered:
        return "other"
    if lowered in SHELL_TOOLS or SHELL_HINT_RE.search(lowered):
        return "shell"
    if lowered in EDIT_TOOLS or EDIT_HINT_RE.search(lowered):
        return "edit"
    if lowered in READ_TOOLS or READ_HINT_RE.search(lowered):
        return "read"
    return "other"


def tool_file(name, input_data):
    for key in PATH_KEYS:
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return str(value[0])
    for value in input_data.values():
        if isinstance(value, str) and ("/" in value or "\\" in value) and len(value) < 260:
            return value
    for key in ("query", "pattern", "search"):
        if input_data.get(key):
            return str(input_data[key])
    return ""


def shell_command(input_data):
    for key in ("command", "cmd", "commandline"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def dedupe(values):
    seen = set()
    output = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def build_state(items):
    files_read, files_edited, commands, test_results = [], [], [], []
    calls = {}
    first_user = last_user = last_assistant = ""
    for item in items:
        kind = item["kind"]
        if kind == "user_text":
            first_user = first_user or item["text"]
            last_user = item["text"]
        elif kind == "assistant_text":
            last_assistant = item["text"]
        elif kind == "tool_use":
            name = norm_tool(item.get("name"))
            input_data = item.get("input") or {}
            calls[item.get("tool_use_id") or f"#{len(calls)}"] = (name, input_data)
            category = classify_tool(name)
            if category == "read":
                files_read.append(tool_file(name, input_data))
            elif category == "edit":
                files_edited.append(tool_file(name, input_data))
            elif category == "shell":
                commands.append(shell_command(input_data))
        elif kind == "tool_result":
            name, input_data = calls.get(item.get("tool_use_id"), ("", {}))
            content = item.get("content") or ""
            command = shell_command(input_data) if name in SHELL_TOOLS or SHELL_HINT_RE.search(name) else ""
            if content.strip() and (
                item.get("is_error") or TEST_CMD_RE.search(command) or TEST_RESULT_RE.search(content[:2000])
            ):
                test_results.append(
                    {"command_hint": command or name, "is_error": bool(item.get("is_error")), "content": content}
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


def truncate(value, limit):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def text_block(value, limit):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def tool_brief(item):
    name = item.get("name") or "tool"
    input_data = item.get("input") or {}
    detail = shell_command(input_data) or tool_file(str(name).lower(), input_data)
    if detail:
        return f"{name}({truncate(detail, 100)})"
    return f"{name}(...)"


def render_item(item, max_chars):
    kind = item["kind"]
    if kind == "user_text":
        return ["### [用户]", text_block(item["text"], max_chars)]
    if kind == "assistant_text":
        return ["### [助手]", text_block(item["text"], max_chars)]
    if kind == "tool_use":
        return [
            f"### [工具调用] {item.get('name') or 'tool'}",
            "```json",
            truncate(json.dumps(item.get("input") or {}, ensure_ascii=False, separators=(",", ":")), max_chars),
            "```",
        ]
    if kind == "tool_result":
        error = " (错误)" if item.get("is_error") else ""
        return [f"### [工具结果]{error}", text_block(item.get("content"), max_chars)]
    if kind == "attachment":
        return ["### [附件]", item.get("text") or ""]
    return []


def format_tokens(usage):
    if not isinstance(usage, dict):
        return ""
    parts = []
    prompt = int(usage.get("promptTokens") or 0)
    completion = int(usage.get("completionTokens") or 0)
    if prompt:
        parts.append(f"输入 {prompt}")
    if completion:
        parts.append(f"输出 {completion}")
    details = usage.get("promptTokensDetails") if isinstance(usage.get("promptTokensDetails"), dict) else {}
    cached = int(details.get("cachedTokens") or 0)
    if cached:
        parts.append(f"缓存读 {cached}")
    return " / ".join(parts)


def first_user_text(history):
    """首条用户消息的首行，作为无标题会话的展示标题。"""
    for entry in history:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if message.get("role") != "user":
            continue
        text = message_content_text(message.get("content")).strip()
        if not text:
            continue
        return text.splitlines()[0][:120]
    return ""


def resolve_title(meta):
    title = (meta.get("title") or "").strip()
    if title.lower() not in UNTITLED_TITLES:
        return title
    return meta.get("first_user") or meta["session_id"]


def render_summary(meta, items, state, summaries, tokens, recent_n, max_chars):
    usage = meta.get("usage") or {}
    lines = [
        "# Resume-Continue 会话接管摘要",
        "",
        "## 会话信息",
        f"- 标题: {truncate(resolve_title(meta), 120)}",
        f"- 会话ID: {meta['session_id']}",
        f"- 项目: {meta['directory'] or '(未知)'}",
        f"- 存储: {meta['path']}",
    ]
    if meta["mode"]:
        lines.append(f"- 模式: {meta['mode']}")
    if meta["chat_model_title"]:
        lines.append(f"- 模型: {meta['chat_model_title']}")
    cost = usage.get("totalCost")
    if cost:
        lines.append(f"- 累计费用: ${float(cost):.4f}")
    token_line = format_tokens(usage)
    if not token_line and tokens.get("input"):
        parts = []
        if tokens.get("input"):
            parts.append(f"输入 {tokens['input']}")
        if tokens.get("output"):
            parts.append(f"输出 {tokens['output']}")
        token_line = " / ".join(parts)
    if token_line:
        lines.append(f"- Token 用量: {token_line}")
    lines.append(f"- 时间范围: {fmt_time(meta['created']) or '(未知)'} ~ {fmt_time(meta['updated']) or '(未知)'}（会话文件时间，条目本身无时间戳）")
    lines.append(f"- 消息条目数: {len(items)}")
    lines.append("")
    if summaries:
        lines.append("## 历史摘要（原会话 compact）")
        for value in summaries[-3:]:
            lines.append(f"- {truncate(value, max_chars)}")
        lines.append("")
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    for title, key in (("已调查文件", "files_read"), ("代码修改", "files_edited"), ("执行命令", "commands")):
        if state[key]:
            lines.append(f"### {title}")
            for value in state[key]:
                lines.append(f"- {truncate(value, 200)}")
            lines.append("")
    if state["test_results"]:
        lines.append("### 测试 / 错误结果")
        for result in state["test_results"][-5:]:
            content_lines = result["content"].strip().splitlines()
            first = content_lines[0] if content_lines else ""
            prefix = " [错误]" if result["is_error"] else ""
            lines.append(f"-{prefix} {truncate(first, 200)}")
        lines.append("")
    lines.extend(["### 最近用户消息", text_block(state["last_user"], max_chars) or "(无)"])
    lines.extend(["", "### 最近助手消息", text_block(state["last_assistant"], max_chars) or "(无)", ""])
    recent = items[-recent_n:] if recent_n else []
    lines.extend([f"## 近期对话（最近 {len(recent)} 条）", ""])
    for item in recent:
        lines.extend(render_item(item, max_chars))
        lines.append("")
    older_tools = [item for item in items[:-recent_n] if item["kind"] == "tool_use"] if recent_n else []
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 60 条）")
        for item in older_tools[-60:]:
            lines.append(f"- {tool_brief(item)}")
        lines.append("")
    lines.extend(
        [
            "## 接管建议",
            "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。",
            "- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。",
            "- 不要逐字复述历史；基于现状决定下一步动作。",
            "- 若想让 Continue 自己原生续接：CLI 用 `cn --resume`（最近会话）或 `cn ls` 选择；IDE 在 Continue 侧边栏的会话历史中选中该会话。",
            "",
        ]
    )
    return "\n".join(lines)


def project_sessions(sessions, project_path):
    target = norm_path(project_path)
    return [meta for meta in sessions if norm_path(meta["directory"]) == target]


def pick_session(sessions, session_arg, project_path):
    if session_arg:
        for meta in sessions:
            if meta["session_id"].startswith(session_arg) or session_arg in meta["session_id"]:
                return meta
        return None
    selected = project_sessions(sessions, project_path)
    return selected[0] if selected else None


def print_list(sessions, project_path, limit):
    selected = project_sessions(sessions, project_path)
    shown = selected[:limit] if limit > 0 else selected
    print(f"当前项目: {project_path}")
    suffix = f"（仅显示最近 {len(shown)} 个）" if len(shown) < len(selected) else ""
    print(f"找到 {len(selected)} 个会话{suffix}：\n")
    for index, meta in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        time_label = fmt_time(meta["updated"]) or fmt_time(meta["created"]) or "(无时间)"
        title = resolve_title(meta)
        print(f"{mark} {time_label}  {meta['session_id'][:16]}  标题: {title}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="resume_continue.py",
        description="读取 Continue（VS Code/JetBrains 扩展与 cn CLI）本地会话，生成结构化接管摘要。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "数据来源:\n"
            "  ~/.continue/sessions/*.json（IDE 扩展与 CLI 共用；受 CONTINUE_GLOBAL_DIR 环境变量影响）\n"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    group.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", metavar="ID", help="指定会话 ID 或前缀；跨项目查找")
    parser.add_argument("--project", metavar="PATH", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--continue-dir", metavar="DIR", help="Continue 主目录（~/.continue）或 sessions 目录")
    parser.add_argument("--recent", type=int, default=8, help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, help="单条截断长度，默认 1500")
    parser.add_argument("--limit", type=int, default=0, help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", metavar="FILE", help="将摘要写入文件")
    return parser


def resolve_sessions_dir(configured):
    if not configured:
        return None
    resolved = Path(configured).expanduser()
    if resolved.name == "sessions" and resolved.is_dir():
        return resolved
    nested = resolved / "sessions"
    return nested if nested.is_dir() else resolved


def main():
    setup_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    configured = resolve_sessions_dir(args.continue_dir)
    sessions_dirs = [configured] if configured else default_sessions_dirs()
    if not any(d.is_dir() for d in sessions_dirs):
        joined = "、".join(str(d) for d in sessions_dirs)
        print(f"错误：未找到 Continue 本地会话目录（已探测 {joined}）。", file=sys.stderr)
        print("可用 --continue-dir 指定 ~/.continue 主目录或 sessions 目录。", file=sys.stderr)
        sys.exit(1)
    sessions = scan_sessions(sessions_dirs)
    if not sessions:
        print("错误：sessions 目录下未找到任何 Continue 会话（*.json）。", file=sys.stderr)
        sys.exit(1)
    project_path = os.path.abspath(args.project)
    if args.list:
        if not project_sessions(sessions, project_path):
            print(f"错误：未找到项目 {project_path} 的 Continue 会话。可用 --session ID 跨项目查找。", file=sys.stderr)
            sys.exit(1)
        print_list(sessions, project_path, args.limit)
        return
    target = pick_session(sessions, args.session, project_path)
    if target is None:
        print(f"错误：未匹配到会话 '{args.session or '当前项目'}'。", file=sys.stderr)
        sys.exit(1)
    meta = dict(target)
    normalized = normalize_history(meta.pop("_history"))
    items = normalized["items"]
    state = build_state(items)
    recent_count = max(args.recent, 0)
    recent_items = items[-recent_count:] if recent_count else []
    info = {
        "session_id": meta["session_id"],
        "title": resolve_title(meta),
        "directory": meta["directory"],
        "mode": meta["mode"],
        "chat_model_title": meta["chat_model_title"],
        "source": meta["source"],
        "path": str(meta["path"]),
        "created": fmt_time(meta["created"]),
        "updated": fmt_time(meta["updated"]),
        "cost": (meta.get("usage") or {}).get("totalCost", 0),
        "tokens": (meta.get("usage") or {}) or normalized["tokens"],
    }
    if args.json:
        output = json.dumps(
            {
                "info": info,
                "state": state,
                "summaries": normalized["summaries"],
                "recent_items": recent_items,
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = render_summary(meta, items, state, normalized["summaries"], normalized["tokens"], recent_count, max(args.max_chars, 1))
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(output)
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
