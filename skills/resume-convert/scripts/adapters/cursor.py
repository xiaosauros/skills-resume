#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/cursor: Cursor Composer（state.vscdb SQLite）-> 归一化会话。

格式依据（与 resume-cursor 一致）：
  <cursor_dir>/User/globalStorage/state.vscdb 的 cursorDiskKV 表：
    - composerData:<composerId> -> JSON：name（侧边栏标题）/ status / subtitle /
      createdAt、lastUpdatedAt（epoch 毫秒）/ workspaceIdentifier.uri.fsPath（项目）/
      modelConfig.modelName / fullConversationHeadersOnly（[{bubbleId}]，严格按序）
    - bubbleId:<composerId>:<bubbleId> -> JSON：type 1=用户消息 2=助手；
      capabilityType 15=工具调用（toolFormerData 内 params/result 为 JSON 字符串、
      toolCallId 为真实调用 id），30=思考（跳过）；createdAt 为 ISO 串
  兜底定位：User/workspaceStorage/<hash>/workspace.json 的 folder URI 匹配项目，
  再从该工作区库 ItemTable 的 composer.composerData 取 composerId 集合回查全局库。
转换语义：工具调用与结果在同一 bubble 内拆为一对 m_tool_use / m_tool_result；
思考（capabilityType=30）不可跨模型迁移，跳过。
标题：composerData.name 优先，兜底首条用户消息或会话 ID。
与 cursor.js 功能等价、输出一致。
"""

import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402


def default_dir():
    if os.environ.get("CURSOR_HOME"):
        return os.environ["CURSOR_HOME"]
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser(os.path.join("~", "AppData", "Roaming")),
            "Cursor")
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Library", "Application Support", "Cursor"))
    return os.path.expanduser(os.path.join("~", ".config", "Cursor"))


def global_db_path(cursor_dir):
    return os.path.join(cursor_dir, "User", "globalStorage", "state.vscdb")


def norm_path(p):
    """统一小写 + 正斜杠，便于跨平台/跨分隔符匹配（与 resume-cursor 一致）。"""
    return os.path.normcase(os.path.normpath(p)).replace("\\", "/")


def _connect(db_path):
    """只读方式打开 SQLite（mode=ro），Cursor 运行时也可安全读取；文件不存在返回 None。"""
    if not os.path.isfile(db_path):
        return None
    uri = "file:" + urllib.parse.quote(db_path.replace("\\", "/"), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


# ---------- JSON / 时间工具 ----------

def parse_json(s):
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def ms_to_iso(ms):
    """composerData 的 epoch 毫秒 -> 毫秒精度 UTC 'Z' 串（非法 / <=0 返回 ''）。"""
    try:
        n = int(ms)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    sec, msec = divmod(n, 1000)
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % msec


def bubble_ts(b):
    """bubble.createdAt：ISO 串直接用，epoch 毫秒转 ISO（兼容 resume fmt_time 的双类型）。"""
    ts = b.get("createdAt")
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        return ms_to_iso(ts)
    return str(ts)


# ---------- composer 摘要（标题 / 项目 / 时间来源） ----------

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
        key[len("composerData:"):] if key else "")
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
    """读取全局 state.vscdb 的 cursorDiskKV 表中所有 composerData:* 记录（按键序稳定输出）。"""
    out = []
    for key, value in con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY key"):
        try:
            v = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        out.append(composer_summary(v, key))
    return out


def decode_folder_uri(uri):
    """workspace.json 的 folder 字段形如 file:///d%3A/workspace/x
    解码为文件系统路径（Windows 去掉盘符前的 /）。"""
    try:
        p = urllib.parse.unquote(uri)
    except Exception:
        p = uri
    p = re.sub(r"^file://", "", p)
    p = re.sub(r"^/([a-zA-Z]:)", r"\1", p)
    return p


def scan_workspace_fallback(cursor_dir, project_path, con):
    """兜底路径：全局库 fsPath 匹配不到时，扫描 workspaceStorage。
    通过 workspace.json 的 folder 字段定位项目，再从该工作区库 ItemTable 的
    composer.composerData 取 composerId（保持插入序），回查全局库的标题/时间。"""
    ws_root = os.path.join(cursor_dir, "User", "workspaceStorage")
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
        except (OSError, ValueError, AttributeError):
            continue
        if not folder or norm_path(decode_folder_uri(folder)) != norm:
            continue

        ws_db_path = os.path.join(d, "state.vscdb")
        if not os.path.isfile(ws_db_path):
            continue
        ws_con = _connect(ws_db_path)
        if ws_con is None:
            continue
        ids = []
        seen = set()
        try:
            row = ws_con.execute(
                "SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
            if row:
                v = json.loads(row[0])
                if isinstance(v, dict):
                    for c in (v.get("allComposers") or []):
                        if isinstance(c, dict) and c.get("composerId") \
                                and c["composerId"] not in seen:
                            seen.add(c["composerId"])
                            ids.append(c["composerId"])
                    for k in ("selectedComposerIds", "lastFocusedComposerIds"):
                        for cid in (v.get(k) or []):
                            if cid and cid not in seen:
                                seen.add(cid)
                                ids.append(cid)
        except (json.JSONDecodeError, ValueError, TypeError, sqlite3.Error):
            pass
        ws_con.close()

        for cid in ids:
            # 在全局库回查该 composer 的标题/时间（可能没有记录）。
            summary = {
                "session_id": cid, "name": None, "status": "", "subtitle": "",
                "model": "", "fs_path": "", "lastUpdatedAt": 0, "createdAt": 0,
                "bubble_count": 0,
            }
            try:
                row = con.execute(
                    "SELECT value FROM cursorDiskKV WHERE key=?",
                    ("composerData:" + cid,)).fetchone()
                if row:
                    v = json.loads(row[0])
                    if isinstance(v, dict):
                        summary = composer_summary(v, "composerData:" + cid)
            except (json.JSONDecodeError, ValueError, TypeError, sqlite3.Error):
                pass
            sessions.append(summary)

    sessions.sort(key=lambda s: s["lastUpdatedAt"] or 0, reverse=True)
    return sessions


# ---------- 工具调用归一化 ----------

def normalize_tool_input(name, params):
    """Cursor 工具名 -> 归一化 input（仅保留关键字段，与 resume-cursor 一致）。
    返回的 dict 键顺序与 JS 实现保持一致，以保证两实现输出相同。"""
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
        # params.tools = [{name, parameters(JSON 字符串), serverName}]
        tools = p.get("tools") if isinstance(p, dict) else None
        t = tools[0] if isinstance(tools, list) and tools and isinstance(tools[0], dict) else {}
        return {
            "server": t.get("serverName") or "",
            "tool": t.get("name") or "",
            "parameters": parse_json(t.get("parameters")) or {},
        }
    return p  # 兜底：原始 params


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
                len(dirs), "\n".join(d["absPath"] for d in dirs[:10] if isinstance(d, dict) and d.get("absPath")))
        return ""
    if name in ("edit_file_v2", "delete_file"):
        return json.dumps(r, ensure_ascii=False) if r else ""
    # 默认 / MCP
    if name and name.startswith("mcp-") and isinstance(r.get("result"), str):
        inner = parse_json(r["result"])
        if isinstance(inner, dict) and isinstance(inner.get("content"), list):
            return "\n".join(c.get("text", "") for c in inner["content"]
                             if isinstance(c, dict) and c.get("text"))
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
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?", (prefix + "%",)):
            bid = key[len(prefix):]
            try:
                b = json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if b is not None:
                mapping[bid] = b
    except sqlite3.Error:
        pass
    return mapping


def normalize_session(composer, bubble_map):
    """按 fullConversationHeadersOnly 顺序拍平为消息流（严格保持 bubble 顺序）。"""
    msgs = []
    headers = composer.get("fullConversationHeadersOnly") if isinstance(composer, dict) else None
    if not isinstance(headers, list):
        headers = []
    for h in headers:
        if not isinstance(h, dict):
            continue
        b = bubble_map.get(h.get("bubbleId"))
        if not b:
            continue
        ts = bubble_ts(b)
        cap = b.get("capabilityType")

        if b.get("type") == 1:
            # 用户消息
            text = str(b.get("text") or "").strip()
            if text:
                msgs.append(cs.m_message("user", text, ts))
        elif b.get("type") == 2:
            tf = b.get("toolFormerData")
            if cap == 15 and tf and isinstance(tf, dict):
                # 工具调用 + 结果（同一 bubble 内拆为一对消息）
                name = tf.get("name") or "tool"
                params = parse_json(tf.get("params")) or {}
                result = parse_json(tf.get("result")) or {}
                call_id = tf.get("toolCallId") or ""
                msgs.append(cs.m_tool_use(call_id, name,
                                          normalize_tool_input(name, params), ts))
                msgs.append(cs.m_tool_result(
                    call_id,
                    tool_result_content(name, result, tf.get("additionalData")),
                    tool_is_error(tf), ts))
            elif cap == 30:
                # thinking bubble：跳过（推理不可跨模型迁移）
                continue
            else:
                # 助手文本消息
                text = str(b.get("text") or "").strip()
                if text:
                    msgs.append(cs.m_message("assistant", text, ts))
    return msgs


def load_session_data(con, cid):
    """读取 composerData 与全部 bubble，归一化为消息流。JSON 解包失败给出中文报错。"""
    row = con.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + cid,)).fetchone()
    if not row:
        raise cs.ConvertError("未找到 composerData:" + cid)
    try:
        composer = json.loads(row[0])
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise cs.ConvertError("composerData JSON 解析失败：" + str(e))
    if not isinstance(composer, dict):
        raise cs.ConvertError("composerData 结构异常（非 JSON 对象）：" + cid)
    bubble_map = load_bubbles(con, cid)
    msgs = normalize_session(composer, bubble_map)
    return composer, msgs


# ---------- 标题解析 ----------

def resolve_title(name, msgs, session_id):
    """主来源：composerData.name（Cursor 侧边栏标题）；兜底首条用户消息首行或会话 ID。"""
    if name:
        return name
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m["text"].strip():
            t = m["text"].strip().splitlines()[0]
            return (t[:60] + "…") if len(t) > 60 else (t or session_id)
    return session_id


def composer_cwd(composer):
    """workspaceIdentifier.uri.fsPath（可能缺失）。"""
    wi = composer.get("workspaceIdentifier")
    uri = wi.get("uri") if isinstance(wi, dict) else None
    fs = uri.get("fsPath") if isinstance(uri, dict) else ""
    return fs or ""


# ---------- 对外接口 ----------

def list_sessions(dir_=None, project=None):
    cursor_dir = dir_ or default_dir()
    db_path = global_db_path(cursor_dir)
    con = _connect(db_path)
    if con is None:
        return []
    try:
        if project:
            norm = norm_path(project)
            sessions = [c for c in list_composers(con)
                        if c["fs_path"] and norm_path(c["fs_path"]) == norm]
            if not sessions:
                # 兜底：扫描 workspaceStorage 定位 composerId
                sessions = scan_workspace_fallback(cursor_dir, project, con)
        else:
            sessions = list_composers(con)
        sessions.sort(key=lambda s: s["lastUpdatedAt"] or 0, reverse=True)
        try:
            mtime = os.path.getmtime(db_path)
        except OSError:
            mtime = 0
        out = []
        for s in sessions[:50]:
            title = s["name"] or ""
            count = 0
            last_ts = ""
            msgs = []
            try:
                _, msgs = load_session_data(con, s["session_id"])
                count = len(msgs)
                last_ts = cs.fmt_cst(msgs[-1]["ts"]) if msgs else ""
            except cs.ConvertError:
                msgs = []
            if not title:
                title = resolve_title("", msgs, s["session_id"])
            if not last_ts:
                last_ts = cs.fmt_cst(ms_to_iso(s["lastUpdatedAt"]))
            out.append({
                "session_id": s["session_id"], "title": title, "cwd": s["fs_path"],
                "mtime": mtime, "path": db_path,
                "count": count, "last_ts": last_ts,
            })
        return out
    finally:
        con.close()


def load_session(dir_=None, project=None, session_id=None):
    cursor_dir = dir_ or default_dir()
    db_path = global_db_path(cursor_dir)
    con = _connect(db_path)
    if con is None:
        raise cs.ConvertError(
            f"未找到 Cursor 会话（dir={cursor_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + f"）：数据库不存在 {db_path}")
    try:
        if project:
            norm = norm_path(project)
            sessions = [c for c in list_composers(con)
                        if c["fs_path"] and norm_path(c["fs_path"]) == norm]
            if not sessions:
                sessions = scan_workspace_fallback(cursor_dir, project, con)
        else:
            sessions = list_composers(con)
        sessions.sort(key=lambda s: s["lastUpdatedAt"] or 0, reverse=True)
        if session_id:
            sessions = [s for s in sessions
                        if s["session_id"].startswith(session_id)
                        or session_id in s["session_id"]]
        if not sessions:
            raise cs.ConvertError(
                f"未找到 Cursor 会话（dir={cursor_dir}"
                + (f"，project={project}" if project else "")
                + (f"，session={session_id}" if session_id else "") + "）")

        target = sessions[0]
        composer, msgs = load_session_data(con, target["session_id"])
        if not msgs:
            raise cs.ConvertError(f"会话无可转换内容：composerData:{target['session_id']}")

        sid = target["session_id"]
        title = resolve_title(target["name"], msgs, sid)
        ref = cs.make_ref(
            "cursor", sid, title, composer_cwd(composer) or target["fs_path"],
            model=model_from_composer(composer),
            started_at=ms_to_iso(composer.get("createdAt")) or (msgs[0]["ts"] if msgs else ""),
            ended_at=ms_to_iso(composer.get("lastUpdatedAt")) or (msgs[-1]["ts"] if msgs else ""),
            source=db_path)
        return ref, msgs
    finally:
        con.close()
