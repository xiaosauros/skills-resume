#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 Antigravity CLI（agy）本地 transcript，生成结构化接管摘要。"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
READ_TOOLS = {"list_dir", "grep_search", "view_file", "search_web", "read_url_content"}
EDIT_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
SHELL_TOOLS = {"run_command"}
RESULT_TYPES = {
    "LIST_DIRECTORY", "GREP_SEARCH", "VIEW_FILE", "RUN_COMMAND", "CODE_ACTION",
    "INVOKE_SUBAGENT", "SEARCH_WEB", "ASK_QUESTION", "READ_URL_CONTENT",
}
TYPE_TOOL = {
    "LIST_DIRECTORY": "list_dir", "GREP_SEARCH": "grep_search", "VIEW_FILE": "view_file",
    "RUN_COMMAND": "run_command", "CODE_ACTION": "code_action",
    "INVOKE_SUBAGENT": "invoke_subagent", "SEARCH_WEB": "search_web",
    "ASK_QUESTION": "ask_question", "READ_URL_CONTENT": "read_url_content",
}
ARTIFACTS = ("task.md", "implementation_plan.md", "walkthrough.md")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
ADDITIONAL_METADATA_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL)
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


def setup_utf8_stdio():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")


def default_agy_dir():
    return Path(os.environ.get("ANTIGRAVITY_HOME", Path.home() / ".gemini" / "antigravity")).expanduser()


def norm_path(value):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value)))) if value else ""


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
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def parse_jsonl(path):
    events = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        return [], str(exc)
    return events, None


def value_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def command_text(value):
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value or "").strip()


def clean_visible_text(value):
    text = str(value or "").strip()
    match = USER_REQUEST_RE.search(text)
    if match:
        return match.group(1).strip()
    return ADDITIONAL_METADATA_RE.sub("", text).strip()


def clean_path(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                text = decoded
        except ValueError:
            text = text[1:-1]
    return text


def git_root(value):
    candidate = Path(value)
    if candidate.suffix and not candidate.is_dir():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists():
            return norm_path(current)
    return norm_path(candidate)


def normalize_events(events):
    items, summaries = [], []
    for event in events:
        ts = timestamp_ms(event.get("created_at"))
        source = str(event.get("source") or "")
        event_type = str(event.get("type") or "")
        status = str(event.get("status") or "")
        content = clean_visible_text(value_text(event.get("content")))
        error = value_text(event.get("error")).strip()
        if source == "USER_EXPLICIT" and event_type == "USER_INPUT" and content:
            items.append({"kind": "user_text", "timestamp": ts, "text": content})
        elif event_type in {"CHECKPOINT", "CONVERSATION_HISTORY"} and content:
            summaries.append(content)
        elif source == "MODEL" and event_type == "PLANNER_RESPONSE" and content:
            items.append({"kind": "assistant_text", "timestamp": ts, "text": content})
        elif event_type in RESULT_TYPES and content:
            items.append({
                "kind": "tool_result", "timestamp": ts, "name": TYPE_TOOL.get(event_type, event_type.lower()),
                "content": content, "is_error": status == "ERROR",
            })
        elif source == "MODEL" and content and event_type not in {"EPHEMERAL_MESSAGE"}:
            items.append({"kind": "assistant_text", "timestamp": ts, "text": content})
        for index, call in enumerate(event.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            items.append({
                "kind": "tool_use", "timestamp": ts, "name": str(call.get("name") or "tool"),
                "input": call.get("args") if isinstance(call.get("args"), dict) else {},
                "tool_use_id": f"{event.get('step_index', '')}:{index}",
            })
        if error:
            items.append({
                "kind": "tool_result", "timestamp": ts, "name": TYPE_TOOL.get(event_type, event_type.lower() or "error"),
                "content": error, "is_error": True,
            })
    return {"items": items, "summaries": summaries}


def tool_path(name, inputs):
    keys = {
        "list_dir": ("DirectoryPath",), "grep_search": ("SearchPath", "Query"),
        "view_file": ("AbsolutePath",), "write_to_file": ("TargetFile",),
        "replace_file_content": ("TargetFile",), "multi_replace_file_content": ("TargetFile",),
    }.get(name, ("TargetFile", "AbsolutePath", "DirectoryPath", "SearchPath"))
    for key in keys:
        if inputs.get(key):
            return clean_path(inputs[key])
    return ""


def infer_directory(items):
    cwds, paths = [], []
    for item in items:
        if item.get("kind") != "tool_use":
            continue
        inputs = item.get("input") or {}
        cwd = clean_path(inputs.get("Cwd"))
        if cwd and os.path.isabs(cwd):
            cwds.append(cwd)
        candidate = tool_path(str(item.get("name") or ""), inputs)
        if candidate and os.path.isabs(candidate) and ".gemini\\antigravity\\brain" not in candidate.lower().replace("/", "\\"):
            paths.append(candidate)
    if cwds:
        return git_root(Counter(norm_path(value) for value in cwds).most_common(1)[0][0])
    if not paths:
        return ""
    try:
        common = os.path.commonpath([norm_path(value) for value in paths])
    except ValueError:
        return ""
    return git_root(common)


def title_for(items, session_id):
    for item in items:
        if item.get("kind") == "user_text":
            first = (item.get("text", "").strip().splitlines() or [""])[0]
            return first[:60] + ("…" if len(first) > 60 else "") or session_id
    return session_id


def read_artifacts(session_dir):
    result = {}
    for name in ARTIFACTS:
        path = session_dir / name
        try:
            if path.is_file():
                result[name] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return result


def session_meta(transcript):
    events, error = parse_jsonl(transcript)
    if error:
        return None
    normalized = normalize_events(events)
    items = normalized["items"]
    timestamps = [item["timestamp"] for item in items if item.get("timestamp")]
    session_dir = transcript.parents[2]
    session_id = session_dir.name
    updated = timestamps[-1] if timestamps else transcript.stat().st_mtime_ns // 1_000_000
    created = timestamps[0] if timestamps else updated
    directory = infer_directory(items)
    return {
        "session_id": session_id, "title": title_for(items, session_id), "directory": directory,
        "created": created, "updated": updated, "count": len(events), "path": str(transcript),
        "normalized": normalized, "artifacts": read_artifacts(session_dir),
    }


def scan_sessions(agy_dir):
    brain = agy_dir / "brain"
    if not brain.is_dir():
        return []
    sessions = []
    for transcript in brain.glob("*/.system_generated/logs/transcript.jsonl"):
        meta = session_meta(transcript)
        if meta:
            sessions.append(meta)
    sessions.sort(key=lambda item: timestamp_ms(item["updated"]), reverse=True)
    return sessions


def dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def build_state(items):
    files_read, files_edited, commands, test_results = [], [], [], []
    first_user = last_user = last_assistant = ""
    last_command = ""
    for item in items:
        kind = item.get("kind")
        if kind == "user_text":
            first_user = first_user or item["text"]
            last_user = item["text"]
        elif kind == "assistant_text":
            last_assistant = item["text"]
        elif kind == "tool_use":
            name = str(item.get("name") or "").lower()
            inputs = item.get("input") or {}
            if name in READ_TOOLS:
                files_read.append(tool_path(name, inputs))
            elif name in EDIT_TOOLS:
                files_edited.append(tool_path(name, inputs))
            elif name in SHELL_TOOLS:
                last_command = command_text(inputs.get("CommandLine"))
                commands.append(last_command)
        elif kind == "tool_result":
            content = item.get("content") or ""
            is_command_result = item.get("name") == "run_command"
            if item.get("is_error") or (is_command_result and TEST_CMD_RE.search(last_command)) or TEST_RESULT_RE.search(content[:2000]):
                test_results.append({"command_hint": last_command or item.get("name", ""), "is_error": bool(item.get("is_error")), "content": content})
    return {
        "goal": first_user, "files_read": dedupe(files_read), "files_edited": dedupe(files_edited),
        "commands": dedupe(commands), "test_results": test_results,
        "last_user": last_user, "last_assistant": last_assistant,
    }


def truncate(value, limit):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def text_block(value, limit):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def render_item(item, max_chars):
    ts = fmt_time(item.get("timestamp"))
    if item["kind"] == "user_text":
        return [f"### [用户] {ts}", text_block(item["text"], max_chars)]
    if item["kind"] == "assistant_text":
        return [f"### [助手] {ts}", text_block(item["text"], max_chars)]
    if item["kind"] == "tool_use":
        return [f"### [工具调用] {item.get('name', 'tool')} {ts}", "```json", truncate(json.dumps(item.get("input") or {}, ensure_ascii=False), max_chars), "```"]
    if item["kind"] == "tool_result":
        return [f"### [工具结果] {item.get('name', 'tool')}{' (错误)' if item.get('is_error') else ''} {ts}", text_block(item.get("content"), max_chars)]
    return []


def render_summary(meta, state, recent_n, max_chars):
    norm = meta["normalized"]
    timestamps = [item["timestamp"] for item in norm["items"] if item.get("timestamp")]
    lines = [
        "# Resume-AGY 会话接管摘要", "", "## 会话信息",
        f"- 标题: {meta['title']}", f"- 会话ID: {meta['session_id']}",
        f"- 项目: {meta['directory'] or '(未知)'}", "- 存储: Antigravity transcript.jsonl",
        f"- 时间范围: {fmt_time(timestamps[0] if timestamps else meta['created'])} ~ {fmt_time(timestamps[-1] if timestamps else meta['updated'])}",
        f"- 原始事件数: {meta['count']}", f"- 可见条目数: {len(norm['items'])}", "",
    ]
    if norm["summaries"]:
        lines.append("## 历史摘要（Checkpoint / Conversation History）")
        for value in norm["summaries"]:
            lines.extend([text_block(value, max_chars), ""])
    if meta["artifacts"]:
        lines.append("## Antigravity 任务产物")
        for name, content in meta["artifacts"].items():
            lines.extend([f"### {name}", text_block(content, max_chars), ""])
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    for title, key in (("已调查文件", "files_read"), ("代码修改", "files_edited"), ("执行命令", "commands")):
        if state[key]:
            lines.append(f"### {title}")
            lines.extend(f"- {truncate(value, 200)}" for value in state[key])
            lines.append("")
    if state["test_results"]:
        lines.append("### 测试 / 错误结果")
        for result in state["test_results"][-5:]:
            first = (result["content"].strip().splitlines() or [""])[0]
            lines.append(f"-{' [错误]' if result['is_error'] else ''} {truncate(first, 200)}")
        lines.append("")
    lines.extend([
        "### 最近用户消息", text_block(state["last_user"], max_chars) or "(无)", "",
        "### 最近助手消息", text_block(state["last_assistant"], max_chars) or "(无)", "",
    ])
    recent = norm["items"][-recent_n:] if recent_n else []
    lines.extend([f"## 近期对话（最近 {len(recent)} 条）", ""])
    for item in recent:
        lines.extend(render_item(item, max_chars) + [""])
    older_tools = norm["items"][:-recent_n] if recent_n else norm["items"]
    older_tools = [item for item in older_tools if item.get("kind") == "tool_use"]
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 60 条）")
        for item in older_tools[-60:]:
            detail = command_text((item.get("input") or {}).get("CommandLine")) or tool_path(str(item.get("name") or ""), item.get("input") or {})
            lines.append(f"- [{fmt_time(item.get('timestamp'))}] {item.get('name', 'tool')}({truncate(detail, 100) if detail else '...'})")
        lines.append("")
    lines.extend([
        "## 接管建议",
        "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。",
        "- 以「任务状态重建」「任务产物」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。",
        "- 不要逐字复述历史；基于现状决定下一步动作。", "",
    ])
    return "\n".join(lines)


def project_sessions(sessions, project):
    target = norm_path(project)
    return [item for item in sessions if item["directory"] and norm_path(item["directory"]) == target]


def pick_session(sessions, session_arg, project):
    if session_arg:
        return next((item for item in sessions if item["session_id"].startswith(session_arg) or session_arg in item["session_id"]), None)
    matches = project_sessions(sessions, project)
    return matches[0] if matches else None


def print_list(sessions, project, limit):
    selected = project_sessions(sessions, project)
    shown = selected[:limit] if limit > 0 else selected
    print(f"当前项目: {project}")
    print(f"找到 {len(selected)} 个会话" + (f"（仅显示最近 {len(shown)} 个）" if len(shown) < len(selected) else "") + "：\n")
    for index, meta in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        print(f"{mark} {fmt_time(meta['updated']) or '(无时间)'}  {meta['session_id'][:12]}  事件数:{str(meta['count']).ljust(4)} 标题: {meta['title']}")


def parse_args():
    parser = argparse.ArgumentParser(description="读取 Antigravity CLI（agy）本地会话，生成结构化接管摘要。")
    parser.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    parser.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", "--conversation", dest="session", help="会话 ID 或前缀；跨项目查找")
    parser.add_argument("--project", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--agy-dir", help="Antigravity 数据目录")
    parser.add_argument("--recent", type=int, default=8, help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, help="Markdown 摘要单条截断长度，默认 1500")
    parser.add_argument("--limit", type=int, default=0, help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", help="将摘要写入 UTF-8 文件")
    return parser.parse_args()


def main():
    setup_utf8_stdio()
    args = parse_args()
    agy_dir = Path(args.agy_dir).expanduser().resolve() if args.agy_dir else default_agy_dir().resolve()
    sessions = scan_sessions(agy_dir)
    if not sessions:
        print(f"错误：在 {agy_dir / 'brain'} 未找到 Antigravity 会话。", file=sys.stderr)
        return 1
    project = str(Path(args.project).resolve())
    if args.list:
        if not project_sessions(sessions, project):
            print(f"错误：未找到项目 {project} 的 Antigravity 会话。可用 --session ID 跨项目查找。", file=sys.stderr)
            return 1
        print_list(sessions, project, max(args.limit, 0))
        return 0
    target = pick_session(sessions, args.session, project)
    if not target:
        print(f"错误：未匹配到会话 '{args.session or '当前项目'}'。", file=sys.stderr)
        return 1
    state = build_state(target["normalized"]["items"])
    recent_count = max(args.recent, 0)
    output = json.dumps({
        "info": {key: target[key] for key in ("session_id", "title", "directory", "created", "updated", "count", "path")},
        "state": state, "summaries": target["normalized"]["summaries"], "artifacts": target["artifacts"],
        "recent_items": target["normalized"]["items"][-recent_count:] if recent_count else [],
    }, ensure_ascii=False, indent=2) if args.json else render_summary(target, state, recent_count, max(args.max_chars, 1))
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
