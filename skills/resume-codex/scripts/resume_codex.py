#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-codex: 读取 Codex CLI 本地会话记录（rollout JSONL + session_index），
生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Claude Code / Codex / Grok 等）均可直接调用。
与同目录 resume_codex.js 功能等价、输出可互换。用法见同目录 SKILL.md。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude 一致）
CST = timezone(timedelta(hours=8))

# Session ID（UUID）正则，用于从 rollout 文件名提取会话 ID
SID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# exec 工具的 input 是 JS 源码串，提取其中的 cmd 字面量
EXEC_CMD_RE = re.compile(r"""cmd\s*:\s*(['"`])((?:\\.|(?!\1)[\s\S])*?)\1""")


# ---------- 路径与项目定位 ----------

def get_codex_dir():
    return os.environ.get(
        "CODEX_HOME",
        os.path.expanduser(os.path.join("~", ".codex")),
    )


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def sid_from_filename(fn):
    base = os.path.basename(fn)
    m = SID_RE.search(base)
    if m:
        return m.group(0)
    base = re.sub(r"^rollout-", "", base, flags=re.IGNORECASE)
    return re.sub(r"\.jsonl$", "", base, flags=re.IGNORECASE)


def peek_head(path, bytes_=32768):
    """只读文件开头若干字节，用于快速 peek cwd，避免整文件读取。"""
    try:
        with open(path, "rb") as f:
            data = f.read(bytes_)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def peek_cwd(path):
    """session_meta 首行可能极长（含 base_instructions），无法整行 JSON.parse；
    直接用正则取首个 "cwd":"..." 字段。"""
    head = peek_head(path)
    m = re.search(r'"cwd"\s*:\s*"([^"]+)"', head)
    return m.group(1) if m else None


def walk_jsonl(dir_):
    """递归收集目录下所有 .jsonl 文件（codex 按 sessions/YYYY/MM/DD/ 组织）。"""
    out = []
    try:
        entries = os.listdir(dir_)
    except OSError:
        return out
    for name in entries:
        full = os.path.join(dir_, name)
        if os.path.isdir(full):
            out.extend(walk_jsonl(full))
        elif os.path.isfile(full) and name.endswith(".jsonl"):
            out.append(full)
    return out


def scan_sessions(codex_dir, project_path):
    """扫描 ~/.codex/sessions/**/*.jsonl，按 cwd 匹配当前项目，按 mtime 倒序。"""
    sessions_root = os.path.join(codex_dir, "sessions")
    norm = norm_path(project_path)
    sessions = []
    for p in walk_jsonl(sessions_root):
        cwd = peek_cwd(p)
        if not cwd or norm_path(cwd) != norm:
            continue
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        sessions.append({
            "session_id": sid_from_filename(p),
            "path": p,
            "cwd": cwd,
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_all_sessions(codex_dir):
    """跨项目扫描：遍历 ~/.codex/sessions 下所有 .jsonl，不按 cwd 过滤。按 mtime 倒序。"""
    sessions_root = os.path.join(codex_dir, "sessions")
    sessions = []
    for p in walk_jsonl(sessions_root):
        cwd = peek_cwd(p)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        sessions.append({
            "session_id": sid_from_filename(p),
            "path": p,
            "cwd": cwd or "",
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


# ---------- session_index（标题来源） ----------

def load_session_index(codex_dir):
    """~/.codex/session_index.jsonl 每行 {"id","thread_name","updated_at"}。
    thread_name 是 codex resume 实际展示/按名恢复 Session 用的标题字段。"""
    idx_path = os.path.join(codex_dir, "session_index.jsonl")
    mapping = {}  # session_id -> thread_name
    if not os.path.isfile(idx_path):
        return mapping
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
                if o.get("id") and o.get("thread_name"):
                    mapping[o["id"]] = o["thread_name"]
    except OSError:
        pass
    return mapping


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


def output_to_text(output):
    """custom_tool_call_output.output 是 [{type,text}] 列表；
    function_call_output.output 通常是 JSON 字符串。统一抽为纯文本。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            b.get("text", "") for b in output
            if isinstance(b, dict) and b.get("text")
        )
    if isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False)
    return str(output)


def extract_exec_cmd(input_str):
    """exec 工具的 input 是 JS 源码串，形如：
        const r = await tools.exec_command({ cmd: "cat foo", workdir: "..." }); text(r.output);
    正则提取 cmd 字面量内容并反转义。"""
    if not isinstance(input_str, str):
        return None
    m = EXEC_CMD_RE.search(input_str)
    if not m:
        return None
    s = m.group(2)
    return (
        s.replace('\\"', '"')
         .replace("\\'", "'")
         .replace('\\n', '\n')
         .replace('\\t', '\t')
         .replace('\\\\', '\\')
    )


def normalize_events(events):
    """把 codex rollout 事件拍平为有序的归一化条目列表。
    每行: {timestamp, type, payload}；顶层 type ∈ session_meta/event_msg/response_item/..."""
    items = []  # 每项: {kind, ...} kind ∈ user_text/assistant_text/tool_use/tool_result
    summaries = []  # compact 标记
    changed_files = []  # patch_apply_end 修改的文件
    cwd = None
    session_id = None
    cli_version = None
    model = None

    for ev in events:
        top = ev.get("type")
        p = ev.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        ts = ev.get("timestamp", "")

        # 元信息：cwd / session id / 版本 / 模型
        if top == "session_meta":
            if not session_id and p.get("id"):
                session_id = p["id"]
            if not cwd and p.get("cwd"):
                cwd = p["cwd"]
            if not cli_version and p.get("cli_version"):
                cli_version = p["cli_version"]
            continue
        if top == "turn_context":
            if not cwd and p.get("cwd"):
                cwd = p["cwd"]
            if not model and p.get("model"):
                model = p["model"]
            continue

        # 事件消息
        if top == "event_msg":
            if pt == "user_message":
                text = (p.get("message") or "").strip()
                if text:
                    items.append({"kind": "user_text", "timestamp": ts, "text": text})
            elif pt == "agent_message":
                text = (p.get("message") or "").strip()
                if text:
                    items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
            elif pt == "context_compacted":
                summaries.append("（会话发生过上下文压缩 context_compacted）")
            elif pt == "patch_apply_end":
                changes = p.get("changes")
                if isinstance(changes, dict):
                    for f in changes.keys():
                        changed_files.append(f)
                if isinstance(p.get("stdout"), str):
                    m = re.match(
                        r"^Success\. Updated the following files:\n([\s\S]*)",
                        p["stdout"],
                    )
                    if m:
                        for line in m.group(1).split("\n"):
                            f = re.sub(r"^[AM]\s+", "", line).strip()
                            if f:
                                changed_files.append(f)
            # token_count / task_started / task_complete / agent_reasoning 等跳过
            continue

        # 响应项
        if top == "response_item":
            if pt == "message":
                if p.get("role") == "assistant":
                    text = output_to_text(p.get("content"))
                    if text and text.strip():
                        items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
                # role=developer / role=user（多为注入）跳过
            elif pt == "custom_tool_call":
                cmd = extract_exec_cmd(p.get("input"))
                items.append({
                    "kind": "tool_use",
                    "timestamp": ts,
                    "name": p.get("name") or "exec",
                    "input": {"cmd": cmd} if cmd else {"raw": p.get("input")},
                    "call_id": p.get("call_id") or "",
                })
            elif pt == "function_call":
                input_obj = {}
                args = p.get("arguments")
                if isinstance(args, str) and args:
                    try:
                        input_obj = json.loads(args)
                    except json.JSONDecodeError:
                        input_obj = {"raw": args}
                elif isinstance(args, dict):
                    input_obj = args
                items.append({
                    "kind": "tool_use",
                    "timestamp": ts,
                    "name": p.get("name") or "function",
                    "input": input_obj,
                    "call_id": p.get("call_id") or "",
                })
            elif pt in ("custom_tool_call_output", "function_call_output"):
                items.append({
                    "kind": "tool_result",
                    "timestamp": ts,
                    "call_id": p.get("call_id") or "",
                    "content": output_to_text(p.get("output")),
                    "is_error": False,
                })
            # reasoning 等跳过
            continue

        # compacted / world_state / inter_agent_communication_metadata 等跳过

    return {
        "items": items,
        "summaries": summaries,
        "changed_files": changed_files,
        "cwd": cwd,
        "session_id": session_id,
        "cli_version": cli_version,
        "model": model,
    }


# ---------- 标题解析 ----------

def resolve_title(thread_name, items, session_id):
    """主来源：session_index 的 thread_name；兜底首条用户消息或 session id。"""
    if thread_name:
        return thread_name
    for it in items:
        if it["kind"] == "user_text":
            t = it["text"].strip().splitlines()[0] if it["text"].strip() else ""
            return (t[:60] + "…") if len(t) > 60 else t or session_id
    return session_id


# ---------- 工具输入摘要 ----------

def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def tool_brief(name, inp):
    """单行紧凑描述一个工具调用。"""
    if name == "exec":
        cmd = (inp.get("cmd") or inp.get("raw") or "") if isinstance(inp, dict) else ""
        return f"exec({truncate(str(cmd).replace(chr(10), ' '), 100)})"
    if name in {"spawn_agent", "followup_task"}:
        return f"{name}({truncate(inp.get('task_name') or inp.get('message') or '', 80)})"
    if name == "send_message":
        return f"send_message({truncate(inp.get('message') or inp.get('recipient') or '', 80)})"
    if isinstance(inp, dict):
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
TEST_RESULT_RE = re.compile(
    r"(✓|✗|\bPASS\b|\bFAIL\b|"
    r"\b\d+\s*(passed|failed|tests?)\b|"
    r"\b(passed|failed)\s*\d+\b|"
    r"\b(failures?|errors?)\s*[:=]\s*\d)",
    re.IGNORECASE,
)
ERR_RE = re.compile(r"(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)", re.IGNORECASE)
ERR_NEG_RE = re.compile(r"no error|0 failed", re.IGNORECASE)


def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_state(items, changed_files):
    """从归一化条目提取结构化任务状态。"""
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
            inp = it["input"] or {}
            last_tool_name = name
            if name == "exec" and inp.get("cmd"):
                cmd = str(inp["cmd"]).strip()
                last_command = cmd
                if cmd:
                    commands.append(cmd)
            else:
                last_command = ""
        elif k == "tool_result":
            content = it.get("content", "")
            is_test_cmd = last_tool_name == "exec" and bool(TEST_CMD_RE.search(last_command))
            looks_test = bool(
                is_test_cmd or (content and TEST_RESULT_RE.search(content[:2000]))
            )
            head = content[:2000]
            is_err = bool(ERR_RE.search(head)) and not bool(ERR_NEG_RE.search(head))
            if (looks_test or is_err) and content.strip():
                test_results.append({
                    "command_hint": last_command or last_tool_name,
                    "is_error": is_err,
                    "content": content,
                })

    return {
        "goal": first_user,
        "files_edited": dedupe(changed_files),
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

    lines.append("# Resume-Codex 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("cli_version"):
        lines.append(f"- Codex 版本: {info['cli_version']}")
    if info.get("model"):
        lines.append(f"- 模型: {info['model']}")
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
            first = tr["content"].strip().splitlines()[0] if tr["content"].strip() else ""
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


# ---------- 会话扫描（轻量，用于 --list） ----------

def session_meta_lite(path, index_map):
    """轻量解析：仅取标题/时间范围/条目数。标题优先取 session_index 的 thread_name。"""
    sid = sid_from_filename(path)
    thread_name = index_map.get(sid)
    events, err = parse_events(path)
    if err:
        return {"session_id": sid, "title": thread_name or sid, "first_ts": "", "last_ts": "", "count": 0}
    norm = normalize_events(events)
    items = norm["items"]
    timestamps = [it.get("timestamp") for it in items if it.get("timestamp")]
    return {
        "session_id": sid,
        "title": resolve_title(thread_name, items, sid),
        "first_ts": fmt_time(timestamps[0]) if timestamps else "",
        "last_ts": fmt_time(timestamps[-1]) if timestamps else "",
        "count": len(items),
    }


def print_session_list(sessions, index_map, project_path, codex_dir, limit=0):
    total = len(sessions)
    shown = sessions[:limit] if limit and limit > 0 else sessions
    print(f"当前项目: {project_path}")
    print(f"会话根: {os.path.join(codex_dir, 'sessions')}")
    if limit and 0 < limit < total:
        print(f"找到 {total} 个会话（仅显示最近 {len(shown)} 个）：\n")
    else:
        print(f"找到 {total} 个会话：\n")
    for i, s in enumerate(shown):
        meta = session_meta_lite(s["path"], index_map) or {}
        mark = "[最近]" if i == 0 else "      "
        title = meta.get("title") or "(无标题)"
        last_ts = meta.get("last_ts") or "(无时间)"
        count = meta.get("count", 0)
        print(f"{mark} {last_ts}  {s['session_id'][:12]}  消息数:{count:<4} 标题: {title}")


# ---------- 主流程 ----------

def load_session(jsonl_path, index_map):
    events, err = parse_events(jsonl_path)
    if err:
        return None, err
    norm = normalize_events(events)
    items = norm["items"]
    sid = norm["session_id"] or sid_from_filename(jsonl_path)
    thread_name = index_map.get(sid)
    timestamps = [it.get("timestamp") for it in items if it.get("timestamp")]
    info = {
        "session_id": sid,
        "title": resolve_title(thread_name, items, sid),
        "cwd": norm.get("cwd"),
        "cli_version": norm.get("cli_version") or "",
        "model": norm.get("model") or "",
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
        description="读取 Codex CLI 本地会话记录（rollout JSONL + session_index），生成结构化接管摘要。"
    )
    ap.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    ap.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    ap.add_argument("--session", default=None, help="指定会话 ID 或前缀（支持跨项目全局查找）")
    ap.add_argument("--project", default=None, help="项目路径，默认当前工作目录")
    ap.add_argument("--codex-dir", default=None, help="Codex 配置目录，默认 ~/.codex 或 $CODEX_HOME")
    ap.add_argument("--recent", type=int, default=8, help="近期对话保留条目数，默认 8")
    ap.add_argument("--max-chars", type=int, default=1500, help="单条内容截断长度，默认 1500")
    ap.add_argument("--limit", type=int, default=0, help="--list 返回的会话数量上限，0 表示不限制")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    ap.add_argument("--output", default=None, help="将摘要写入文件")
    args = ap.parse_args()

    codex_dir = args.codex_dir or get_codex_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    index_map = load_session_index(codex_dir)

    target = None
    project_path = project_path_arg
    sessions = []

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        sessions = scan_all_sessions(codex_dir)
        target = pick_session(sessions, args.session)
        if target and target.get("cwd"):
            project_path = target["cwd"]

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if target is None:
        sessions = scan_sessions(codex_dir, project_path_arg)
        if not sessions:
            print(f"错误：未找到项目 {project_path_arg} 对应的 Codex 会话。", file=sys.stderr)
            print(f"已查找：{os.path.join(codex_dir, 'sessions')}", file=sys.stderr)
            sys.exit(1)
        target = pick_session(sessions, args.session)
        if not target:
            print(f"错误：未匹配到会话 '{args.session}'。使用 --list 查看可用会话。", file=sys.stderr)
            sys.exit(1)

    if args.list:
        print_session_list(sessions, index_map, project_path, codex_dir, args.limit)
        return

    session, err = load_session(target["path"], index_map)
    if err:
        print(f"错误：解析会话失败：{err}", file=sys.stderr)
        sys.exit(1)

    state = build_state(session["normalized"]["items"], session["normalized"]["changed_files"])

    if args.json:
        out = json.dumps({
            "info": session["info"],
            "state": {
                "goal": state["goal"],
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
