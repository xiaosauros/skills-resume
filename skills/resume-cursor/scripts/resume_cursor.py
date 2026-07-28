#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume-cursor: 读取 Cursor IDE 本地 Agent/Composer 会话记录（SQLite state.vscdb），
生成结构化「接管摘要」。

不依赖任何模型专属 API，任意 agent（Claude Code / Codex / Grok 等）均可直接调用。
与同目录 resume_cursor.js 功能等价、输出可互换。用法见同目录 SKILL.md。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta

# 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude / resume-codex 一致）
CST = timezone(timedelta(hours=8))

# Cursor 工具结果/参数中的测试与错误标记
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


# ---------- 路径与项目定位 ----------

def get_cursor_dir():
    if os.environ.get("CURSOR_HOME"):
        return os.environ["CURSOR_HOME"]
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser(os.path.join("~", "AppData", "Roaming")),
            "Cursor",
        )
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Library", "Application Support", "Cursor"))
    return os.path.expanduser(os.path.join("~", ".config", "Cursor"))


def global_db_path(cursor_dir):
    return os.path.join(cursor_dir, "User", "globalStorage", "state.vscdb")


def workspace_storage_dir(cursor_dir):
    return os.path.join(cursor_dir, "User", "workspaceStorage")


def norm_path(p):
    """统一小写 + 正斜杠，便于跨平台/跨分隔符匹配。"""
    return os.path.normcase(os.path.normpath(p)).replace("\\", "/")


def decode_folder_uri(uri):
    """workspace.json 的 folder 字段形如 file:///d%3A/workspace/xiaosauros/bot
    解码为文件系统路径（Windows 去掉盘符前的 /）。"""
    try:
        p = urllib.parse.unquote(uri)
    except Exception:
        p = uri
    p = re.sub(r"^file://", "", p)
    p = re.sub(r"^/([a-zA-Z]:)", r"\1", p)
    return p


# ---------- 数据库 ----------

def open_db(db_path):
    """以只读方式打开 SQLite 库（mode=ro），Cursor 运行时也可安全读取（WAL）。"""
    if not os.path.isfile(db_path):
        raise FileNotFoundError("数据库不存在：" + db_path)
    uri = "file:" + urllib.parse.quote(db_path.replace("\\", "/"), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


# ---------- composer 列表（标题 / 项目 / 时间来源） ----------

def model_from_composer(v):
    """composerData.modelConfig.modelName 通常是 "default"，无意义；仅在非 default 时返回。"""
    mc = v.get("modelConfig") if isinstance(v, dict) else None
    if isinstance(mc, dict):
        name = mc.get("modelName")
        if name and name != "default":
            return str(name)
    return ""


def composer_summary(v, key=None):
    """从 composerData JSON 提取轻量摘要（不加载 bubble）。"""
    cid = (v.get("composerId") if isinstance(v, dict) else None) or (
        key[len("composerData:"):] if key else ""
    )
    uri = (v.get("workspaceIdentifier") or {}).get("uri") if isinstance(v, dict) else None
    fs_path = (uri or {}).get("fsPath", "") if isinstance(uri, dict) else ""
    headers = v.get("fullConversationHeadersOnly") if isinstance(v, dict) else None
    return {
        "session_id": cid,
        "name": (v.get("name") if isinstance(v, dict) else None) or None,
        "status": (v.get("status") if isinstance(v, dict) else None) or "",
        "subtitle": (v.get("subtitle") if isinstance(v, dict) else None) or "",
        "model": model_from_composer(v),
        "fs_path": fs_path,
        "lastUpdatedAt": (v.get("lastUpdatedAt") if isinstance(v, dict) else None) or 0,
        "createdAt": (v.get("createdAt") if isinstance(v, dict) else None) or 0,
        "bubble_count": len(headers) if isinstance(headers, list) else 0,
    }


def list_composers(con):
    """读取全局 state.vscdb 的 cursorDiskKV 表中所有 composerData:* 记录。"""
    out = []
    for key, value in con.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        try:
            v = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(v, dict):
            continue
        out.append(composer_summary(v, key))
    return out


def scan_sessions(con, project_path):
    """主路径：按 composerData.workspaceIdentifier.uri.fsPath 匹配当前项目，按 lastUpdatedAt 倒序。"""
    norm = norm_path(project_path)
    sessions = [c for c in list_composers(con) if c["fs_path"] and norm_path(c["fs_path"]) == norm]
    sessions.sort(key=lambda s: s["lastUpdatedAt"] or 0, reverse=True)
    return sessions


def scan_all_sessions(con):
    """跨项目扫描：返回全局库中所有 composer，不按项目过滤。按 lastUpdatedAt 倒序。"""
    sessions = list_composers(con)
    sessions.sort(key=lambda s: s["lastUpdatedAt"] or 0, reverse=True)
    return sessions


def scan_workspace_fallback(cursor_dir, project_path, con):
    """兜底路径：当全局库的 workspaceIdentifier 匹配不到时，扫描 workspaceStorage。
    通过 workspace.json 的 folder 字段定位项目，再从该工作区库的 composer.composerData 取 composerId。"""
    ws_root = workspace_storage_dir(cursor_dir)
    if not os.path.isdir(ws_root):
        return []
    norm = norm_path(project_path)
    sessions = []
    try:
        names = os.listdir(ws_root)
    except OSError:
        return []
    for name in names:
        d = os.path.join(ws_root, name)
        if not os.path.isdir(d):
            continue
        try:
            with open(os.path.join(d, "workspace.json"), "r", encoding="utf-8") as f:
                folder = json.load(f).get("folder")
        except (OSError, json.JSONDecodeError):
            continue
        if not folder or norm_path(decode_folder_uri(folder)) != norm:
            continue

        ws_db_path = os.path.join(d, "state.vscdb")
        if not os.path.isfile(ws_db_path):
            continue
        try:
            ws_con = open_db(ws_db_path)
        except (OSError, sqlite3.Error):
            continue
        ids = []
        seen = set()
        try:
            row = ws_con.execute(
                "SELECT value FROM ItemTable WHERE key='composer.composerData'"
            ).fetchone()
            if row:
                v = json.loads(row[0])
                if isinstance(v, dict):
                    for c in v.get("allComposers") or []:
                        if isinstance(c, dict) and c.get("composerId"):
                            seen.add(c["composerId"])
                    for cid in v.get("selectedComposerIds") or []:
                        if cid:
                            seen.add(cid)
                    for cid in v.get("lastFocusedComposerIds") or []:
                        if cid:
                            seen.add(cid)
        except (json.JSONDecodeError, sqlite3.Error, TypeError):
            pass
        ws_con.close()

        for cid in seen:
            # 在全局库回查该 composer 的标题/时间。
            summary = {
                "session_id": cid, "name": None, "status": "", "subtitle": "",
                "model": "", "fs_path": "", "lastUpdatedAt": 0, "createdAt": 0, "bubble_count": 0,
            }
            try:
                row = con.execute(
                    "SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + cid,)
                ).fetchone()
                if row:
                    v = json.loads(row[0])
                    if isinstance(v, dict):
                        summary = composer_summary(v, "composerData:" + cid)
            except (json.JSONDecodeError, sqlite3.Error, TypeError):
                pass
            sessions.append(summary)

    sessions.sort(key=lambda s: s["lastUpdatedAt"] or 0, reverse=True)
    return sessions


# ---------- 时间格式化 ----------

def fmt_time(ts):
    """ts 可能是 ISO 字符串（bubble.createdAt）或 epoch 毫秒（composerData.lastUpdatedAt）。"""
    if not ts:
        return ""
    dt = None
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return str(ts)
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


# ---------- JSON 工具 ----------

def parse_json(s):
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------- 工具调用归一化 ----------

def normalize_tool_input(name, params):
    """Cursor 工具名 -> 归一化 input（仅保留关键字段，便于展示与状态重建）。
    返回的 dict 键顺序与 Node 实现保持一致，以保证两实现输出相同。"""
    p = params if isinstance(params, dict) else {}

    if name == "run_terminal_command_v2":
        return {"command": p.get("command") or "", "cwd": p.get("cwd") or ""}
    if name in ("edit_file_v2", "delete_file"):
        return {"file": p.get("relativeWorkspacePath") or ""}
    if name == "read_file_v2":
        return {"file": p.get("targetFile") or ""}
    if name == "ripgrep_raw_search":
        return {"pattern": p.get("pattern") or "", "path": p.get("path") or ""}
    if name == "glob_file_search":
        return {"glob": p.get("globPattern") or "", "directory": p.get("targetDirectory") or ""}
    if name == "semantic_search_full":
        return {"query": p.get("query") or ""}
    if name == "web_search":
        return {"query": p.get("searchTerm") or ""}
    if name == "web_fetch":
        return {"url": p.get("url") or ""}
    if name == "task_v2":
        return {"description": p.get("description") or "", "prompt": str(p.get("prompt") or "")[:200]}
    if name == "ask_question":
        return {"title": p.get("title") or ""}
    if name == "todo_write":
        return {}
    if name and name.startswith("mcp-"):
        tools = p.get("tools") if isinstance(p, dict) else None
        t = tools[0] if isinstance(tools, list) and tools and isinstance(tools[0], dict) else {}
        return {
            "server": t.get("serverName") or "",
            "tool": t.get("name") or "",
            "parameters": parse_json(t.get("parameters")) or {},
        }
    return p  # 兜底：原始 params（展示时会截断）


def tool_result_content(name, result, additional_data):
    """工具结果 -> 纯文本内容（按工具类型抽取最有用的部分）。"""
    r = result if isinstance(result, dict) else {}
    ad = additional_data if isinstance(additional_data, dict) else {}

    if name == "run_terminal_command_v2":
        return r["output"] if isinstance(r.get("output"), str) else json.dumps(r, ensure_ascii=False)
    if name == "read_file_v2":
        total = r.get("totalLinesInFile")
        total_s = total if total is not None else "?"
        if r.get("contents"):
            return f"（文件内容，共 {total_s} 行）"
        return f"（空文件 / 未读取，共 {total_s} 行）"
    if name == "ripgrep_raw_search":
        total = ad.get("totalMatches")
        if total is None:
            total = ad.get("totalFiles")
        files = [
            f["uri"]
            for f in (ad.get("topFiles") or [])[:10]
            if isinstance(f, dict) and f.get("uri")
        ]
        if files:
            return "匹配 {}：\n{}".format(total if total is not None else len(files), "\n".join(files))
        return f"匹配 {total}" if total is not None else ""
    if name == "glob_file_search":
        dirs = r.get("directories") if isinstance(r.get("directories"), list) else []
        if dirs:
            return "命中 {} 项：\n{}".format(
                len(dirs), "\n".join(d["absPath"] for d in dirs[:10] if isinstance(d, dict) and d.get("absPath"))
            )
        return ""
    if name in ("edit_file_v2", "delete_file"):
        return json.dumps(r, ensure_ascii=False) if r else ""
    # 默认 / MCP
    if name and name.startswith("mcp-") and isinstance(r.get("result"), str):
        inner = parse_json(r["result"])
        if isinstance(inner, dict) and isinstance(inner.get("content"), list):
            return "\n".join(
                c.get("text", "") for c in inner["content"]
                if isinstance(c, dict) and c.get("text")
            )
        return r["result"]
    return json.dumps(r, ensure_ascii=False) if r else ""


def tool_is_error(tf):
    if tf.get("status") and tf["status"] != "completed":
        return True
    r = parse_json(tf.get("result")) or {}
    if r.get("rejected") is True:
        return True
    ad = tf.get("additionalData")
    if isinstance(ad, dict) and ad.get("status") in ("error", "failed"):
        return True
    return False


# ---------- 会话加载与事件归一化 ----------

def load_bubbles(con, cid):
    """一次 LIKE 查询取出该 composer 的全部 bubble，构建 bubbleId -> value 映射。"""
    prefix = "bubbleId:" + cid + ":"
    mapping = {}
    try:
        for key, value in con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?", (prefix + "%",)
        ):
            bid = key[len(prefix):]
            try:
                b = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if b is not None:
                mapping[bid] = b
    except sqlite3.Error:
        pass
    return mapping


def normalize_session(composer, bubble_map):
    """按 fullConversationHeadersOnly 顺序拍平为归一化条目流。"""
    items = []
    headers = composer.get("fullConversationHeadersOnly") if isinstance(composer, dict) else None
    if not isinstance(headers, list):
        headers = []
    for h in headers:
        b = bubble_map.get(h.get("bubbleId"))
        if not b:
            continue
        ts = b.get("createdAt") or ""
        cap = b.get("capabilityType")

        if b.get("type") == 1:
            # 用户消息
            text = str(b.get("text") or "").strip()
            if text:
                items.append({"kind": "user_text", "timestamp": ts, "text": text})
        elif b.get("type") == 2:
            if cap == 15 and b.get("toolFormerData"):
                # 工具调用 + 结果（同一 bubble 内）
                tf = b["toolFormerData"]
                name = tf.get("name") or "tool"
                params = parse_json(tf.get("params")) or {}
                result = parse_json(tf.get("result")) or {}
                call_id = tf.get("toolCallId") or ""
                items.append({
                    "kind": "tool_use",
                    "timestamp": ts,
                    "name": name,
                    "input": normalize_tool_input(name, params),
                    "call_id": call_id,
                })
                items.append({
                    "kind": "tool_result",
                    "timestamp": ts,
                    "call_id": call_id,
                    "tool_name": name,
                    "content": tool_result_content(name, result, tf.get("additionalData")),
                    "is_error": tool_is_error(tf),
                })
            elif cap == 30:
                # thinking bubble：跳过（与 codex 跳过 reasoning、claude 跳过 thinking 一致）
                pass
            else:
                # 助手文本消息
                text = str(b.get("text") or "").strip()
                if text:
                    items.append({"kind": "assistant_text", "timestamp": ts, "text": text})
    return items


# ---------- 标题解析 ----------

def resolve_title(name, items, session_id):
    """主来源：composerData.name（Cursor 侧边栏显示的标题）；兜底首条用户消息或会话 ID。"""
    if name:
        return name
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
    i = inp or {}
    if name == "run_terminal_command_v2":
        return f"terminal({truncate(str(i.get('command') or '').replace(chr(10), ' '), 100)})"
    if name == "edit_file_v2":
        return f"edit_file({truncate(i.get('file') or '', 100)})"
    if name == "delete_file":
        return f"delete_file({truncate(i.get('file') or '', 100)})"
    if name == "read_file_v2":
        return f"read_file({truncate(i.get('file') or '', 100)})"
    if name == "ripgrep_raw_search":
        return f"grep({truncate(i.get('pattern') or '', 40)} @ {truncate(i.get('path') or '', 60)})"
    if name == "glob_file_search":
        return f"glob({truncate(i.get('glob') or '', 40)} @ {truncate(i.get('directory') or '', 60)})"
    if name == "semantic_search_full":
        return f"semantic_search({truncate(i.get('query') or '', 80)})"
    if name == "web_search":
        return f"web_search({truncate(i.get('query') or '', 80)})"
    if name == "web_fetch":
        return f"web_fetch({truncate(i.get('url') or '', 80)})"
    if name == "task_v2":
        return f"task({truncate(i.get('description') or '', 80)})"
    if name == "ask_question":
        return f"ask_question({truncate(i.get('title') or '', 80)})"
    if name == "todo_write":
        return "todo_write(...)"
    if name and name.startswith("mcp-"):
        return f"mcp:{i.get('tool') or name}"
    if isinstance(i, dict):
        for v in i.values():
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


def build_state(items):
    """从归一化条目提取结构化任务状态。"""
    commands = []
    test_results = []
    files_edited = []
    files_investigated = []
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
            if name == "run_terminal_command_v2" and inp.get("command"):
                cmd = str(inp["command"]).strip()
                last_command = cmd
                if cmd:
                    commands.append(cmd)
            else:
                last_command = ""
            if name in ("edit_file_v2", "delete_file") and inp.get("file"):
                files_edited.append(inp["file"])
            if name == "read_file_v2" and inp.get("file"):
                files_investigated.append(inp["file"])
            if name == "ripgrep_raw_search" and inp.get("path"):
                files_investigated.append(inp["path"])
            if name == "glob_file_search" and inp.get("directory"):
                files_investigated.append(inp["directory"])
        elif k == "tool_result":
            content = it.get("content") or ""
            is_test_cmd = last_tool_name == "run_terminal_command_v2" and bool(TEST_CMD_RE.search(last_command))
            looks_test = bool(is_test_cmd or (content and TEST_RESULT_RE.search(content[:2000])))
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
        "files_edited": dedupe(files_edited),
        "files_investigated": dedupe(files_investigated),
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
        return [
            f"### [工具调用] {it['name']} {ts}",
            "```",
            truncate(json.dumps(it["input"], ensure_ascii=False), max_chars),
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

    lines.append("# Resume-Cursor 会话接管摘要")
    lines.append("")
    lines.append("## 会话信息")
    lines.append(f"- 标题: {info['title']}")
    lines.append(f"- 会话ID: {info['session_id']}")
    lines.append(f"- 项目: {info.get('cwd') or '(未知)'}")
    if info.get("status"):
        lines.append(f"- 状态: {info['status']}")
    if info.get("subtitle"):
        lines.append(f"- 副标题: {truncate(info['subtitle'], 200)}")
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
    if state["files_investigated"]:
        lines.append("### 已调查文件")
        for f in state["files_investigated"]:
            lines.append(f"- {truncate(f, 200)}")
        lines.append("")
    if state["files_edited"]:
        lines.append("### 代码修改")
        for f in state["files_edited"]:
            lines.append(f"- {truncate(f, 200)}")
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


# ---------- 会话加载 ----------

def load_session(con, summary):
    cid = summary["session_id"]
    row = con.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + cid,)
    ).fetchone()
    if not row:
        return None, "未找到 composerData:" + cid
    try:
        composer = json.loads(row[0])
    except (json.JSONDecodeError, TypeError) as e:
        return None, "composerData JSON 解析失败：" + str(e)
    bubble_map = load_bubbles(con, cid)
    items = normalize_session(composer, bubble_map)
    fs_path = summary["fs_path"] or (
        (composer.get("workspaceIdentifier") or {}).get("uri", {}).get("fsPath", "")
        if isinstance(composer, dict) else ""
    )
    info = {
        "session_id": cid,
        "title": resolve_title(summary["name"], items, cid),
        "cwd": fs_path,
        "status": summary["status"],
        "subtitle": summary["subtitle"],
        "model": summary["model"],
        "first_ts": fmt_time(summary["createdAt"]),
        "last_ts": fmt_time(summary["lastUpdatedAt"]),
    }
    return {"info": info, "normalized": {"items": items, "summaries": []}, "composer": composer}, None


def pick_session(sessions, session_arg):
    if not session_arg:
        return sessions[0]
    for s in sessions:
        if s["session_id"].startswith(session_arg) or session_arg in s["session_id"]:
            return s
    # 标题包含也允许匹配
    for s in sessions:
        if s.get("name") and session_arg in s["name"]:
            return s
    return None


# ---------- --list 渲染 ----------

def print_session_list(sessions, project_path, cursor_dir, limit=0):
    total = len(sessions)
    shown = sessions[:limit] if limit and limit > 0 else sessions
    print(f"当前项目: {project_path}")
    print(f"全局库: {global_db_path(cursor_dir)}")
    if limit and 0 < limit < total:
        print(f"找到 {total} 个会话（仅显示最近 {len(shown)} 个）：\n")
    else:
        print(f"找到 {total} 个会话：\n")
    for i, s in enumerate(shown):
        mark = "[最近]" if i == 0 else "      "
        title = s.get("name") or "(无标题)"
        last_ts = fmt_time(s["lastUpdatedAt"]) or "(无时间)"
        count = s.get("bubble_count", 0)
        print(f"{mark} {last_ts}  {s['session_id'][:12]}  消息数:{count:<4} 标题: {title}")


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
        description="读取 Cursor IDE 本地 Agent/Composer 会话记录（SQLite state.vscdb），生成结构化接管摘要。"
    )
    ap.add_argument("--list", action="store_true", help="仅列出当前项目的会话")
    ap.add_argument("--latest", action="store_true", help="取最近一个会话（默认行为）")
    ap.add_argument("--session", default=None, help="指定会话 ID 或前缀（支持跨项目全局查找，也匹配标题关键词）")
    ap.add_argument("--project", default=None, help="项目路径，默认当前工作目录")
    ap.add_argument("--cursor-dir", default=None, help="Cursor 配置目录，默认按平台推断或 $CURSOR_HOME")
    ap.add_argument("--recent", type=int, default=8, help="近期对话保留条目数，默认 8")
    ap.add_argument("--max-chars", type=int, default=1500, help="单条内容截断长度，默认 1500")
    ap.add_argument("--limit", type=int, default=0, help="--list 返回的会话数量上限，0 表示不限制")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    ap.add_argument("--output", default=None, help="将摘要写入文件")
    args = ap.parse_args()

    cursor_dir = args.cursor_dir or get_cursor_dir()
    project_path_arg = os.path.abspath(args.project or os.getcwd())

    gdb_path = global_db_path(cursor_dir)
    try:
        con = open_db(gdb_path)
    except (OSError, sqlite3.Error) as e:
        print(f"错误：{e}", file=sys.stderr)
        print(f"已查找：{gdb_path}", file=sys.stderr)
        sys.exit(1)

    target = None
    project_path = project_path_arg
    sessions = []

    # 若指定了 session，优先跨项目全局查找。
    if args.session:
        sessions = scan_all_sessions(con)
        target = pick_session(sessions, args.session)
        if target and target.get("fs_path"):
            project_path = target["fs_path"]

    # 未指定 session 或全局未找到时，回退到基于项目路径查找。
    if target is None:
        sessions = scan_sessions(con, project_path_arg)
        if not sessions:
            # 兜底：扫描 workspaceStorage
            sessions = scan_workspace_fallback(cursor_dir, project_path_arg, con)

        if not sessions:
            print(f"错误：未找到项目 {project_path_arg} 对应的 Cursor 会话。", file=sys.stderr)
            print(f"已查找：{gdb_path}", file=sys.stderr)
            con.close()
            sys.exit(1)

        target = pick_session(sessions, args.session)
        if not target:
            print(f"错误：未匹配到会话 '{args.session}'。使用 --list 查看可用会话。", file=sys.stderr)
            con.close()
            sys.exit(1)

    if args.list:
        print_session_list(sessions, project_path, cursor_dir, args.limit)
        con.close()
        return

    session, err = load_session(con, target)
    con.close()
    if err:
        print(f"错误：解析会话失败：{err}", file=sys.stderr)
        sys.exit(1)

    state = build_state(session["normalized"]["items"])

    if args.json:
        out = json.dumps({
            "info": session["info"],
            "state": {
                "goal": state["goal"],
                "files_investigated": state["files_investigated"],
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
