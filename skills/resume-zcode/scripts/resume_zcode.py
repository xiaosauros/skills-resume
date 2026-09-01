#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 ZCode 本地 SQLite 会话库（~/.zcode/cli/db/db.sqlite），生成接管摘要。"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
READ_TOOLS = {"read", "glob", "grep", "list", "ls"}
EDIT_TOOLS = {"write", "edit", "multiedit", "notebookedit", "applypatch"}
SHELL_TOOLS = {"bash", "shell", "shell_command", "terminal"}
TODO_STATUS_LABELS = {"completed": "已完成", "in_progress": "进行中", "pending": "待办"}
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
    # newline="\n" 保证 Windows 下与 Node 版输出逐字节一致
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stderr.reconfigure(encoding="utf-8", newline="\n")


def default_zcode_dir():
    override = os.environ.get("ZCODE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".zcode"


def db_path(data_dir):
    return data_dir / "cli" / "db" / "db.sqlite"


def norm_path(value):
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def timestamp_ms(value):
    if value is None or value == "":
        return 0
    if isinstance(value, dict):
        value = value.get("updated") or value.get("completed") or value.get("created")
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


def open_db(db_file):
    uri = db_file.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_current_schema(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"session", "message", "part"}.issubset(names)


def scan_sessions(data_dir):
    file = db_path(data_dir)
    if not file.is_file():
        return []
    try:
        with open_db(file) as conn:
            if not has_current_schema(conn):
                return []
            rows = conn.execute(
                """
                SELECT id, parent_id, title, directory, version, task_type,
                       time_created, time_updated, time_archived,
                       summary_additions, summary_deletions, summary_files
                FROM session ORDER BY time_updated DESC
                """
            ).fetchall()
            return [
                {
                    "session_id": row["id"],
                    "title": row["title"] or row["id"],
                    "directory": row["directory"] or "",
                    "parent_id": row["parent_id"],
                    "version": row["version"] or "",
                    "task_type": row["task_type"] or "",
                    "created": row["time_created"],
                    "updated": row["time_updated"],
                    "archived": row["time_archived"],
                    "summary": {
                        "additions": row["summary_additions"],
                        "deletions": row["summary_deletions"],
                        "files": row["summary_files"],
                    },
                    "source": "sqlite",
                    "path": str(file),
                }
                for row in rows
            ]
    except (OSError, sqlite3.Error):
        return []


def message_time(data, fallback):
    return timestamp_ms((data.get("time") or {}).get("created") or fallback)


def part_time(data, fallback):
    return timestamp_ms((data.get("time") or {}).get("start") or fallback)


def normalize_records(messages, parts_by_message):
    items = []
    model = ""
    provider = ""
    agent = ""
    for message in messages:
        data = message["data"]
        role = data.get("role") or ""
        ts = message_time(data, message.get("time_created"))
        model_info = data.get("model") or {}
        model = data.get("modelID") or model_info.get("modelID") or model
        provider = data.get("providerID") or model_info.get("providerID") or provider
        agent = data.get("agent") or agent
        for part in parts_by_message.get(message["id"], []):
            pdata = part["data"]
            ptype = pdata.get("type") or ""
            pts = part_time(pdata, part.get("time_created") or ts)
            if ptype == "text":
                # synthetic 文本是系统注入的提醒/通知（如 TodoWrite 提示），不属于真实对话
                if pdata.get("synthetic"):
                    continue
                text = pdata.get("text") or ""
                if not text.strip():
                    continue
                items.append({"kind": f"{role}_text", "timestamp": pts, "text": text})
            elif ptype == "tool":
                state = pdata.get("state") or {}
                name = pdata.get("tool") or "tool"
                call_id = pdata.get("callID") or ""
                items.append(
                    {
                        "kind": "tool_use",
                        "timestamp": pts,
                        "name": name,
                        "input": state.get("input") or {},
                        "tool_use_id": call_id,
                    }
                )
                status = str(state.get("status") or "")
                output = state.get("output")
                error = state.get("error")
                if output is not None or error is not None or status in {"completed", "error"}:
                    if not isinstance(output, str):
                        output = json.dumps(output, ensure_ascii=False) if output is not None else ""
                    items.append(
                        {
                            "kind": "tool_result",
                            "timestamp": timestamp_ms((state.get("time") or {}).get("end")) or pts,
                            "tool_use_id": call_id,
                            "content": str(error) if error is not None else output,
                            "is_error": status == "error" or error is not None,
                        }
                    )
            elif ptype == "file":
                label = pdata.get("filename") or ((pdata.get("source") or {}).get("path")) or "附件"
                items.append({"kind": "attachment", "timestamp": pts, "text": str(label)})
            # reasoning / step-start / step-finish / timeline 等类型与接管无关，跳过
    items.sort(key=lambda item: item.get("timestamp") or 0)
    return {"items": items, "model": model, "provider": provider, "agent": agent}


def load_todos(conn, session_id):
    rows = conn.execute(
        "SELECT content, status, priority, position FROM todo WHERE session_id=? ORDER BY position",
        (session_id,),
    ).fetchall()
    return [
        {
            "content": row["content"],
            "status": row["status"] or "",
            "priority": row["priority"],
            "position": row["position"],
        }
        for row in rows
    ]


def load_session(data_dir, meta):
    with open_db(db_path(data_dir)) as conn:
        msg_rows = conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY sequence, time_created, id",
            (meta["session_id"],),
        ).fetchall()
        messages = [{"id": row["id"], "time_created": row["time_created"], "data": parse_json(row["data"])} for row in msg_rows]
        parts_by_message = {}
        part_rows = conn.execute(
            "SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY sequence, time_created, id",
            (meta["session_id"],),
        ).fetchall()
        for row in part_rows:
            parts_by_message.setdefault(row["message_id"], []).append(
                {"id": row["id"], "time_created": row["time_created"], "data": parse_json(row["data"])}
            )
        todos = load_todos(conn, meta["session_id"])
    normalized = normalize_records(messages, parts_by_message)
    timestamps = [item["timestamp"] for item in normalized["items"] if item.get("timestamp")]
    info = {
        "session_id": meta["session_id"],
        "title": meta["title"],
        "directory": meta["directory"],
        "parent_id": meta["parent_id"],
        "version": meta["version"],
        "task_type": meta["task_type"],
        "model": normalized["model"],
        "provider": normalized["provider"],
        "agent": normalized["agent"],
        "source": meta["source"],
        "first_ts": fmt_time(timestamps[0] if timestamps else meta["created"]),
        "last_ts": fmt_time(timestamps[-1] if timestamps else meta["updated"]),
        "archived": bool(meta["archived"]),
        "summary": meta["summary"],
    }
    return {"info": info, "normalized": normalized, "todos": todos}


def tool_file(name, inputs):
    for key in ("filePath", "file_path", "path", "filename"):
        if inputs.get(key):
            return str(inputs[key])
    if name in {"glob", "grep"}:
        return str(inputs.get("pattern") or inputs.get("query") or "")
    return ""


def shell_command(inputs):
    return str(inputs.get("command") or inputs.get("cmd") or "").strip()


def dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def build_state(items, todos):
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
        "todos": todos,
    }


def truncate(value, limit):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def text_block(value, limit):
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"
    return text


def tool_brief(item):
    name = item.get("name") or "tool"
    inputs = item.get("input") or {}
    detail = shell_command(inputs) or tool_file(str(name).lower(), inputs) or str(inputs.get("url") or inputs.get("query") or "")
    return f"{name}({truncate(detail, 100)})" if detail else f"{name}(...)"


def render_item(item, max_chars):
    ts = fmt_time(item.get("timestamp"))
    kind = item["kind"]
    if kind == "user_text":
        return [f"### [用户] {ts}", text_block(item["text"], max_chars)]
    if kind == "assistant_text":
        return [f"### [助手] {ts}", text_block(item["text"], max_chars)]
    if kind == "tool_use":
        payload = json.dumps(item.get("input") or {}, ensure_ascii=False, separators=(",", ":"))
        return [f"### [工具调用] {item.get('name') or 'tool'} {ts}", "```json", truncate(payload, max_chars), "```"]
    if kind == "tool_result":
        tag = " (错误)" if item.get("is_error") else ""
        return [f"### [工具结果]{tag} {ts}", text_block(item.get("content"), max_chars)]
    if kind == "attachment":
        return [f"### [附件] {ts}", item.get("text") or ""]
    return []


def todo_label(status):
    return TODO_STATUS_LABELS.get(status, status or "未知")


def render_summary(session, state, recent_n, max_chars):
    info = session["info"]
    norm = session["normalized"]
    lines = [
        "# Resume-ZCode 会话接管摘要",
        "",
        "## 会话信息",
        f"- 标题: {info['title']}",
        f"- 会话ID: {info['session_id']}",
        f"- 项目: {info['directory'] or '(未知)'}",
        f"- 存储: {info['source']}",
    ]
    if info["model"]:
        lines.append(f"- 模型: {info['provider'] + '/' if info['provider'] else ''}{info['model']}")
    if info["agent"]:
        lines.append(f"- Agent: {info['agent']}")
    if info["version"]:
        lines.append(f"- ZCode 版本: {info['version']}")
    if info["task_type"]:
        lines.append(f"- 会话类型: {info['task_type']}")
    if info["parent_id"]:
        lines.append(f"- 父会话: {info['parent_id']}")
    lines.extend([f"- 时间范围: {info['first_ts']} ~ {info['last_ts']}", f"- 消息条目数: {len(norm['items'])}", ""])
    lines.extend(["## 任务状态重建", "", "### 目标", text_block(state["goal"], max_chars) or "(未识别)", ""])
    if state["todos"]:
        lines.append(f"### 任务清单（原会话 TodoWrite，共 {len(state['todos'])} 项）")
        lines.extend(f"- [{todo_label(todo['status'])}] {truncate(todo['content'], 200)}" for todo in state["todos"])
        lines.append("")
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
    lines.extend(
        [
            "### 最近用户消息",
            text_block(state["last_user"], max_chars) or "(无)",
            "",
            "### 最近助手消息",
            text_block(state["last_assistant"], max_chars) or "(无)",
            "",
        ]
    )
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
    lines.extend(
        [
            "## 接管建议",
            "- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。",
            "- 优先核对「任务清单」中未完成项，再以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。",
            "- 不要逐字复述历史；基于现状决定下一步动作。",
            "",
        ]
    )
    return "\n".join(lines)


def pick_session(sessions, session_arg, project_path):
    if session_arg:
        return next((s for s in sessions if s["session_id"].startswith(session_arg) or session_arg in s["session_id"]), None)
    project_norm = norm_path(project_path)
    matches = [s for s in sessions if norm_path(s["directory"]) == project_norm]
    return matches[0] if matches else None


def project_sessions(sessions, project_path):
    project_norm = norm_path(project_path)
    return [s for s in sessions if norm_path(s["directory"]) == project_norm]


def print_list(sessions, project_path, limit):
    selected = project_sessions(sessions, project_path)
    shown = selected[:limit] if limit > 0 else selected
    print(f"当前项目: {project_path}")
    print(f"找到 {len(selected)} 个会话" + (f"（仅显示最近 {len(shown)} 个）" if len(shown) < len(selected) else "") + "：\n")
    for index, meta in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        print(f"{mark} {fmt_time(meta['updated']) or '(无时间)'}  {meta['session_id'][:12]}  标题: {meta['title']}")


def parse_args():
    parser = argparse.ArgumentParser(description="读取 ZCode 本地会话，生成结构化接管摘要。")
    parser.add_argument("--list", action="store_true", help="仅列出当前项目会话")
    parser.add_argument("--latest", action="store_true", help="取最近一个会话（默认）")
    parser.add_argument("--session", help="指定会话 ID 或前缀；跨项目查找")
    parser.add_argument("--project", default=os.getcwd(), help="项目路径，默认当前目录")
    parser.add_argument("--zcode-dir", help="ZCode 主目录（默认 ~/.zcode）")
    parser.add_argument("--recent", type=int, default=8, help="近期条目数，默认 8")
    parser.add_argument("--max-chars", type=int, default=1500, help="单条截断长度")
    parser.add_argument("--limit", type=int, default=0, help="--list 数量上限，0 不限制")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", help="将摘要写入文件")
    return parser.parse_args()


def main():
    setup_utf8_stdio()
    args = parse_args()
    data_dir = Path(args.zcode_dir).expanduser() if args.zcode_dir else default_zcode_dir()
    sessions = scan_sessions(data_dir)
    if not sessions:
        raise SystemExit(f"错误：在 {data_dir} 未找到 ZCode 会话（已检查 cli/db/db.sqlite）。")
    project_path = os.path.abspath(args.project)
    if args.list:
        selected = project_sessions(sessions, project_path)
        if not selected:
            raise SystemExit(f"错误：未找到项目 {project_path} 的 ZCode 会话。可用 --session ID 跨项目查找。")
        print_list(sessions, project_path, args.limit)
        return
    target = pick_session(sessions, args.session, project_path)
    if not target:
        raise SystemExit(f"错误：未匹配到会话 '{args.session or '当前项目'}'。")
    session = load_session(data_dir, target)
    state = build_state(session["normalized"]["items"], session["todos"])
    if args.json:
        recent_items = session["normalized"]["items"][-args.recent :] if args.recent > 0 else []
        output = json.dumps(
            {
                "info": session["info"],
                "state": state,
                "recent_items": recent_items,
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = render_summary(session, state, max(args.recent, 0), max(args.max_chars, 1))
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8", newline="\n")
        print(f"摘要已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
