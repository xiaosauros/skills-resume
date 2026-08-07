#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 Qoder CLI 本地会话 JSONL，生成结构化接管摘要。"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
READ_TOOLS = {"read", "glob", "grep", "list", "codesearch"}
EDIT_TOOLS = {"write", "edit", "multiedit", "notebookedit", "patch", "apply_patch"}
SHELL_TOOLS = {"bash", "shell", "shell_command", "terminal"}
SYS_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
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


def qoder_dir_default():
    return Path(os.environ.get("QODER_HOME", Path.home() / ".qoder")).expanduser()


def norm_path(value):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value)))) if value else ""


def encode_project_path(value):
    return str(value).replace(":", "-").replace("\\", "-").replace("/", "-")


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
                try:
                    events.append(json.loads(line))
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        return [], str(exc)
    return events, None


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        )
    return ""


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values = []
        for block in content:
            if isinstance(block, dict):
                values.append(str(block.get("text") or block.get("content") or ""))
            elif block is not None:
                values.append(str(block))
        return "\n".join(value for value in values if value)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False) if isinstance(content, (dict, list)) else str(content)


def strip_reminders(value):
    return SYS_REMINDER_RE.sub("", value or "").strip()


def normalize_events(events):
    items, summaries = [], []
    titles = {"custom": "", "ai": "", "agent": ""}
    cwd = git_branch = model = reasoning_effort = context_window = entrypoint = version = ""
    is_sidechain = False
    for event in events:
        event_type = event.get("type")
        if event_type == "custom-title":
            titles["custom"] = event.get("customTitle") or titles["custom"]
            continue
        if event_type == "ai-title":
            titles["ai"] = event.get("aiTitle") or titles["ai"]
            continue
        if event_type == "agent-name":
            titles["agent"] = event.get("agentName") or titles["agent"]
            continue
        if event_type == "runtime-config":
            model = event.get("model") or model
            reasoning_effort = event.get("reasoningEffort") or reasoning_effort
            context_window = event.get("contextWindow") or context_window
            continue
        cwd = cwd or event.get("cwd") or ""
        git_branch = git_branch or event.get("gitBranch") or ""
        entrypoint = entrypoint or event.get("entrypoint") or ""
        version = version or event.get("version") or ""
        is_sidechain = is_sidechain or bool(event.get("isSidechain"))
        if event.get("isCompactSummary"):
            compact = event.get("summary") or event.get("content") or extract_text((event.get("message") or {}).get("content"))
            if compact:
                summaries.append(compact)
            continue
        if event_type == "system":
            if event.get("subtype") in {"compact_summary", "summary"}:
                compact = event.get("summary") or event.get("content")
                if compact:
                    summaries.append(str(compact))
            continue
        if event_type not in {"user", "assistant"}:
            continue
        message = event.get("message") or {}
        content = message.get("content")
        ts = timestamp_ms(event.get("timestamp"))
        if message.get("model"):
            model = message.get("model")
        if event_type == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        items.append(
                            {
                                "kind": "tool_result",
                                "timestamp": ts,
                                "tool_use_id": block.get("tool_use_id") or "",
                                "content": result_text(block.get("content")),
                                "is_error": bool(block.get("is_error")),
                            }
                        )
            if not event.get("isMeta") and not event.get("isVisibleInTranscriptOnly"):
                text = strip_reminders(extract_text(content))
                if text:
                    items.append({"kind": "user_text", "timestamp": ts, "text": text})
        else:
            text = extract_text(content)
            if text.strip():
                items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        items.append(
                            {
                                "kind": "tool_use",
                                "timestamp": ts,
                                "name": block.get("name") or "tool",
                                "input": block.get("input") or {},
                                "tool_use_id": block.get("id") or "",
                            }
                        )
            if event.get("error") and not text.strip():
                items.append(
                    {
                        "kind": "assistant_text",
                        "timestamp": ts,
                        "text": "[API 错误] " + result_text(event.get("errorDetails") or event.get("error")),
                    }
                )
    return {
        "items": items,
        "summaries": summaries,
        "titles": titles,
        "cwd": cwd,
        "git_branch": git_branch,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "context_window": context_window,
        "entrypoint": entrypoint,
        "version": version,
        "is_sidechain": is_sidechain,
    }


def title_for(norm, session_id):
    for key in ("custom", "ai", "agent"):
        if norm["titles"].get(key):
            return norm["titles"][key]
    for item in norm["items"]:
        if item["kind"] == "user_text":
            first = (item["text"].strip().splitlines() or [""])[0]
            return first[:60] + ("…" if len(first) > 60 else "") or session_id
    return session_id


def state_workspaces(path):
    state_path = path.with_suffix("") / "state.json"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return [str(value) for value in data.get("workspaceDirectories") or []]
    except (OSError, ValueError, TypeError):
        return []


def current_meta(path):
    events, error = parse_jsonl(path)
    if error:
        return None
    norm = normalize_events(events)
    timestamps = [item["timestamp"] for item in norm["items"] if item.get("timestamp")]
    sid = path.stem
    workspaces = state_workspaces(path)
    directory = norm["cwd"] or (workspaces[0] if workspaces else "")
    return {
        "session_id": sid,
        "title": title_for(norm, sid),
        "directory": directory,
        "created": timestamps[0] if timestamps else path.stat().st_mtime_ns // 1_000_000,
        "updated": timestamps[-1] if timestamps else path.stat().st_mtime_ns // 1_000_000,
        "count": len(norm["items"]),
        "source": "jsonl",
        "path": str(path),
        "normalized": norm,
    }


def legacy_meta(path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = obj.get("id") or path.name[: -len("-session.json")]
    return {
        "session_id": sid,
        "title": obj.get("title") or sid,
        "directory": obj.get("working_dir") or "",
        "created": obj.get("created_at") or path.stat().st_mtime_ns // 1_000_000,
        "updated": obj.get("updated_at") or path.stat().st_mtime_ns // 1_000_000,
        "count": obj.get("message_count") or 0,
        "source": "legacy-metadata",
        "path": str(path),
        "legacy": obj,
    }


def scan_sessions(qoder_dir):
    root = qoder_dir / "projects"
    if not root.is_dir():
        return []
    sessions = []
    current_ids = set()
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            meta = current_meta(path)
            if meta:
                sessions.append(meta)
                current_ids.add(meta["session_id"])
        for path in project_dir.glob("*-session.json"):
            meta = legacy_meta(path)
            if meta and meta["session_id"] not in current_ids:
                sessions.append(meta)
    sessions.sort(key=lambda item: timestamp_ms(item["updated"]), reverse=True)
    return sessions


def load_session(meta):
    if meta["source"] == "jsonl":
        norm = meta["normalized"]
    else:
        norm = {
            "items": [],
            "summaries": [],
            "titles": {"custom": meta["title"], "ai": "", "agent": ""},
            "cwd": meta["directory"],
            "git_branch": "",
            "model": "",
            "reasoning_effort": "",
            "context_window": "",
            "entrypoint": "",
            "version": "",
            "is_sidechain": False,
        }
    timestamps = [item["timestamp"] for item in norm["items"] if item.get("timestamp")]
    return {
        "info": {
            "session_id": meta["session_id"],
            "title": meta["title"],
            "directory": meta["directory"],
            "source": meta["source"],
            "model": norm["model"],
            "reasoning_effort": norm["reasoning_effort"],
            "context_window": norm["context_window"],
            "entrypoint": norm["entrypoint"],
            "version": norm["version"],
            "git_branch": norm["git_branch"],
            "first_ts": fmt_time(timestamps[0] if timestamps else meta["created"]),
            "last_ts": fmt_time(timestamps[-1] if timestamps else meta["updated"]),
        },
        "normalized": norm,
    }


def tool_file(name, inputs):
    for key in ("file_path", "filePath", "notebook_path", "path", "filename"):
        if inputs.get(key):
            return str(inputs[key])
    if name in {"glob", "grep", "codesearch"}:
        return str(inputs.get("pattern") or inputs.get("query") or "")
    return ""


def shell_command(inputs):
    return str(inputs.get("command") or inputs.get("cmd") or "").strip()


def dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def build_state(items):
    files_read, files_edited, commands, test_results = [], [], [], []
    first_user = last_user = last_assistant = ""
    calls = {}
    for item in items:
        kind = item["kind"]
        if kind == "user_text":
            first_user = first_user or item["text"]
            last_user = item["text"]
        elif kind == "assistant_text":
            last_assistant = item["text"]
        elif kind == "tool_use":
            name = str(item.get("name") or "").lower()
            inputs = item.get("input") or {}
            calls[item.get("tool_use_id") or f"#{len(calls)}"] = (name, inputs)
            if name in READ_TOOLS:
                files_read.append(tool_file(name, inputs))
            elif name in EDIT_TOOLS:
                files_edited.append(tool_file(name, inputs))
            elif name in SHELL_TOOLS:
                commands.append(shell_command(inputs))
        elif kind == "tool_result":
            name, inputs = calls.get(item.get("tool_use_id"), ("", {}))
            content = item.get("content") or ""
            cmd = shell_command(inputs) if name in SHELL_TOOLS else ""
            if item.get("is_error") or TEST_CMD_RE.search(cmd) or TEST_RESULT_RE.search(content[:2000]):
                if content.strip():
                    test_results.append({"command_hint": cmd or name, "is_error": bool(item.get("is_error")), "content": content})
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
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def tool_brief(item):
    name = item.get("name") or "tool"
    inputs = item.get("input") or {}
    detail = shell_command(inputs) or tool_file(str(name).lower(), inputs)
    return f"{name}({truncate(detail, 100)})" if detail else f"{name}(...)"


def render_item(item, max_chars):
    ts = fmt_time(item.get("timestamp"))
    kind = item["kind"]
    if kind == "user_text":
        return [f"### [用户] {ts}", text_block(item["text"], max_chars)]
    if kind == "assistant_text":
        return [f"### [助手] {ts}", text_block(item["text"], max_chars)]
    if kind == "tool_use":
        payload = json.dumps(item.get("input") or {}, ensure_ascii=False)
        return [f"### [工具调用] {item.get('name') or 'tool'} {ts}", "```json", truncate(payload, max_chars), "```"]
    if kind == "tool_result":
        tag = " (错误)" if item.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", text_block(item.get("content"), max_chars)]
    return []


def render_summary(session, state, recent_n, max_chars):
    info, norm = session["info"], session["normalized"]
    lines = [
        "# Resume-Qoder 会话接管摘要",
        "",
        "## 会话信息",
        f"- 标题: {info['title']}",
        f"- 会话ID: {info['session_id']}",
        f"- 项目: {info['directory'] or '(未知)'}",
        f"- 存储: {info['source']}",
    ]
    if info["model"]:
        lines.append(f"- 模型: {info['model']}")
    if info["reasoning_effort"]:
        lines.append(f"- 推理强度: {info['reasoning_effort']}")
    if info["context_window"]:
        lines.append(f"- Context Window: {info['context_window']}")
    if info["git_branch"]:
        lines.append(f"- Git 分支: {info['git_branch']}")
    if info["entrypoint"]:
        lines.append(f"- 入口: {info['entrypoint']}")
    if info["version"]:
        lines.append(f"- Qoder 版本: {info['version']}")
    if norm["is_sidechain"]:
        lines.append("- 类型: 子 agent 会话 (sidechain)")
    lines.extend([f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}", f"- 消息条目数: {len(norm['items'])}", ""])
    if info["source"] == "legacy-metadata":
        lines.extend(["## 兼容性说明", "- 该旧版会话只保留本地元数据、文件快照和工具结果，未发现可读取的主对话 transcript。", ""])
    if norm["summaries"]:
        lines.append("## 历史摘要（原会话 compact）")
        lines.extend(f"- {truncate(value, max_chars)}" for value in norm["summaries"])
        lines.append("")
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    for title, key in (("已调查文件", "files_read"), ("代码修改", "files_edited"), ("执行命令", "commands")):
        if state[key]:
            lines.append(f"### {title}")
            lines.extend(f"- {truncate(value, 200)}" for value in state[key])
            lines.append("")
    if state["test_results"]:
        lines.append("### 测试 / 错误结果")
        for result in state["test_results"][-5:]:
            tag = " [错误]" if result["is_error"] else ""
            first = (result["content"].strip().splitlines() or [""])[0]
            lines.append(f"-{tag} {truncate(first, 200)}")
        lines.append("")
    lines.extend(["### 最近用户消息", text_block(state["last_user"], max_chars) or "(无)", "", "### 最近助手消息", text_block(state["last_assistant"], max_chars) or "(无)", ""])
    recent = norm["items"][-recent_n:] if recent_n else []
    lines.extend([f"## 近期对话（最近 {len(recent)} 条）", ""])
    for item in recent:
        lines.extend(render_item(item, max_chars))
        lines.append("")
    older_tools = [item for item in norm["items"][:-recent_n] if item["kind"] == "tool_use"] if recent_n else []
    if older_tools:
        lines.append("## 更早活动（工具调用，仅最近 60 条）")
        lines.extend(f"- [{fmt_time(item.get('timestamp'))}] {tool_brief(item)}" for item in older_tools[-60:])
        lines.append("")
    lines.extend(["## 接管建议", "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。", "- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。", "- 不要逐字复述历史；基于现状决定下一步动作。", ""])
    return "\n".join(lines)


def project_sessions(sessions, project_path):
    target = norm_path(project_path)
    return [item for item in sessions if norm_path(item["directory"]) == target]


def pick_session(sessions, session_arg, project_path):
    if session_arg:
        return next((item for item in sessions if item["session_id"].startswith(session_arg) or session_arg in item["session_id"]), None)
    matches = project_sessions(sessions, project_path)
    return matches[0] if matches else None


def print_list(sessions, project_path, limit):
    selected = project_sessions(sessions, project_path)
    shown = selected[:limit] if limit > 0 else selected
    print(f"当前项目: {project_path}")
    print(f"找到 {len(selected)} 个会话" + (f"（仅显示最近 {len(shown)} 个）" if len(shown) < len(selected) else "") + "：\n")
    for index, meta in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        print(f"{mark} {fmt_time(meta['updated']) or '(无时间)'}  {meta['session_id'][:12]}  消息数:{str(meta['count']).ljust(4)} 标题: {meta['title']}")


def parse_args():
    parser = argparse.ArgumentParser(description="读取 Qoder CLI 本地会话，生成结构化接管摘要。")
    parser.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    parser.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", help="指定会话 ID 或前缀；跨项目查找")
    parser.add_argument("--project", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--qoder-dir", help="Qoder 用户数据目录，默认 ~/.qoder")
    parser.add_argument("--recent", type=int, default=8, help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, help="单条截断长度")
    parser.add_argument("--limit", type=int, default=0, help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", help="将摘要写入文件")
    return parser.parse_args()


def main():
    setup_utf8_stdio()
    args = parse_args()
    qoder_dir = Path(args.qoder_dir).expanduser() if args.qoder_dir else qoder_dir_default()
    sessions = scan_sessions(qoder_dir)
    if not sessions:
        raise SystemExit(f"错误：在 {qoder_dir / 'projects'} 未找到 Qoder 会话。")
    project_path = os.path.abspath(args.project)
    if args.list:
        if not project_sessions(sessions, project_path):
            raise SystemExit(f"错误：未找到项目 {project_path} 的 Qoder 会话。可用 --session ID 跨项目查找。")
        print_list(sessions, project_path, args.limit)
        return
    target = pick_session(sessions, args.session, project_path)
    if not target:
        raise SystemExit(f"错误：未匹配到会话 '{args.session}'。")
    session = load_session(target)
    state = build_state(session["normalized"]["items"])
    if args.json:
        recent_items = session["normalized"]["items"][-args.recent :] if args.recent > 0 else []
        output = json.dumps({"info": session["info"], "state": state, "summaries": session["normalized"]["summaries"], "recent_items": recent_items}, ensure_ascii=False, indent=2)
    else:
        output = render_summary(session, state, max(args.recent, 0), max(args.max_chars, 1))
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
