#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/kimi: Kimi Code CLI（~/.kimi-code）-> 归一化会话。

格式依据（与 resume-kimi 一致）：
  ~/.kimi-code/session_index.jsonl 每行 {"sessionId","sessionDir","workDir"}
    （缺失时兜底扫描 sessions/<工作区>/<会话>/state.json，workspaces.json 反查项目根）
  <sessionDir>/state.json：title（标题主来源，"New Session" 为占位符）/
    createdAt / updatedAt（ISO 字符串）/ workDir
  <sessionDir>/agents/main/wire.jsonl 事件流，顶层 type 主要有：
    - turn.prompt：input=[{type,text},...] -> 用户输入
    - context.append_loop_event.event.type：
        content.part(part.type=text) -> 助手输出（分片累加，遇边界合并为一条）
        tool.call / tool.result -> 工具调用与结果（toolCallId 配对）
        content.part(part.type=think) 等推理内容不可迁移，跳过
    - context.apply_compaction：全量压缩，summary 压缩摘要 -> 转换注记
      （full_compaction.begin/complete、metadata、llm.request、usage.record、
      context.append_message（注入）等其余事件跳过）
  wire 事件 time 为 epoch 毫秒，state.json 时间为 ISO 字符串，统一转 UTC ISO 'Z'。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402


def default_dir():
    return os.environ.get("KIMI_HOME",
                          os.path.expanduser(os.path.join("~", ".kimi-code")))


def norm_path(p):
    """归一化路径用于比对（与 resume-kimi 一致）：反斜杠转正斜杠、去尾斜杠、小写。"""
    return str(p).replace("\\", "/").rstrip("/").lower()


# ---------- 时间：epoch 毫秒 / ISO 字符串 -> UTC ISO 'Z' ----------

def _iso_z(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_iso_z(ts):
    """Kimi 事件时间（epoch 毫秒或 ISO 字符串）-> cs 可识别的 UTC ISO 'Z'。

    解析规则与 resume-kimi 的 fmt_time 一致；ISO 字符串无时区时按本地时区解释。"""
    if ts is None or ts == "" or isinstance(ts, bool):
        return ""
    if isinstance(ts, (int, float)):
        try:
            return _iso_z(datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc))
        except (OSError, ValueError, OverflowError):
            return ""
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return ""
        try:
            return _iso_z(datetime.fromisoformat(s.replace("Z", "+00:00")))
        except ValueError:
            pass
        try:
            return _iso_z(datetime.fromtimestamp(float(s) / 1000.0, tz=timezone.utc))
        except (ValueError, OSError, OverflowError):
            return ""
    return ""


def parse_ms(ts):
    """updatedAt -> epoch 毫秒（排序用）；解析失败返回 0。"""
    iso = to_iso_z(ts)
    if not iso:
        return 0.0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0


# ---------- session_index / workspaces / 兜底扫描 ----------

def load_session_index(kimi_dir):
    """session_index.jsonl -> [{sessionId, sessionDir, workDir}]（坏行跳过）。"""
    out = []
    idx = os.path.join(kimi_dir, "session_index.jsonl")
    if not os.path.isfile(idx):
        return out
    try:
        with open(idx, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("sessionId") and o.get("sessionDir"):
                    out.append({"sessionId": o["sessionId"],
                                "sessionDir": o["sessionDir"],
                                "workDir": o.get("workDir") or ""})
    except OSError:
        pass
    return out


def load_workspaces(kimi_dir):
    """workspaces.json -> {工作区id: 项目根}（session_index 缺失时兜底反查）。"""
    mapping = {}
    wj = os.path.join(kimi_dir, "workspaces.json")
    if not os.path.isfile(wj):
        return mapping
    try:
        with open(wj, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return mapping
    ws = obj.get("workspaces") if isinstance(obj, dict) else None
    if isinstance(ws, dict):
        for wid, info in ws.items():
            root = info.get("root") if isinstance(info, dict) else None
            if root:
                mapping[wid] = root
    return mapping


def load_state(session_dir):
    state_path = os.path.join(session_dir, "state.json")
    if not os.path.isfile(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def walk_sessions(kimi_dir, workspaces):
    """session_index 缺失时兜底：遍历 sessions/<工作区>/<会话>/state.json。"""
    out = []
    root = os.path.join(kimi_dir, "sessions")
    if not os.path.isdir(root):
        return out
    try:
        wds = os.listdir(root)
    except OSError:
        return out
    for wd in wds:
        wd_path = os.path.join(root, wd)
        if not os.path.isdir(wd_path):
            continue
        try:
            sids = os.listdir(wd_path)
        except OSError:
            continue
        for sid in sids:
            session_dir = os.path.join(wd_path, sid)
            if not os.path.isfile(os.path.join(session_dir, "state.json")):
                continue
            work_dir = ""
            try:
                with open(os.path.join(session_dir, "state.json"),
                          "r", encoding="utf-8") as f:
                    work_dir = json.load(f).get("workDir") or ""
            except (OSError, json.JSONDecodeError):
                pass
            if not work_dir:
                work_dir = workspaces.get(wd, "")
            out.append({"sessionId": sid, "sessionDir": session_dir,
                        "workDir": work_dir})
    return out


def wire_path_of(session_dir):
    return os.path.join(session_dir, "agents", "main", "wire.jsonl")


# ---------- wire.jsonl 解析 ----------

def parse_wire(path):
    """读 wire.jsonl 事件流（坏行跳过）；文件不可读抛 ConvertError。"""
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        raise cs.ConvertError(f"无法读取 wire.jsonl：{e}")
    return events


def join_input_text(inp):
    """turn.prompt.input 是 [{type,text},...]，抽取文本部分。"""
    if not isinstance(inp, list):
        return ""
    parts = [
        b.get("text", "")
        for b in inp
        if isinstance(b, dict) and isinstance(b.get("text"), str)
    ]
    return "".join(p for p in parts if p)


def output_to_text(out):
    """tool.result.output 可能是字符串、[{type,text},...] 列表或对象。"""
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "\n".join(b.get("text", "") for b in out
                         if isinstance(b, dict) and b.get("text"))
    if isinstance(out, dict):
        return json.dumps(out, ensure_ascii=False)
    return str(out)


def normalize_input(name, args, disp):
    """优先用 Kimi 自带的结构化 display 字段；缺失时按工具名从 args 抽取。"""
    if not isinstance(args, dict):
        args = {}
    if isinstance(disp, dict):
        k = disp.get("kind")
        if k == "command":
            return {"command": disp.get("command") or "", "cwd": disp.get("cwd") or ""}
        if k == "file_io":
            return {"operation": disp.get("operation") or "", "path": disp.get("path") or ""}
        if k == "agent_call":
            return {"agent_name": disp.get("agent_name") or "", "prompt": disp.get("prompt") or ""}
        if k == "skill_call":
            return {"skill": disp.get("skill_name") or "", "args": disp.get("args") or ""}
        if k == "url_fetch":
            return {"url": disp.get("url") or ""}

    def pick(*ks):
        for key in ks:
            v = args.get(key)
            if v is not None and v != "":
                return v
        return ""

    if name == "Bash":
        return {"command": pick("command", "cmd")}
    if name in ("Read", "ReadMediaFile"):
        return {"path": pick("path", "file_path", "targetFile")}
    if name in ("Write", "Edit"):
        return {"path": pick("path", "file_path")}
    if name == "Grep":
        return {"pattern": pick("pattern"), "path": pick("path")}
    if name == "Glob":
        return {"pattern": pick("pattern"), "path": pick("path", "targetDirectory")}
    if name == "WebSearch":
        return {"query": pick("query", "searchTerm")}
    if name == "FetchURL":
        return {"url": pick("url")}
    if name == "Skill":
        return {"skill": pick("skill", "name"), "args": pick("args")}
    if name in ("Agent", "AgentSwarm"):
        return {"agent_name": pick("agent_name", "subagent_type", "name"), "prompt": pick("prompt", "description", "task")}
    return args


def normalize(events):
    """wire 事件 -> (meta, msgs)，严格保持源时间顺序。"""
    msgs = []
    meta = {"model": ""}

    # 相邻 content.part(text) 累加为同一条助手消息，遇边界事件统一 flush
    pending_text = ""
    pending_ts = None  # 原始事件时间（epoch 毫秒或 ISO），flush 时统一转换

    def flush_text():
        nonlocal pending_text, pending_ts
        if pending_text.strip():
            msgs.append(cs.m_message("assistant", pending_text, to_iso_z(pending_ts)))
        pending_text = ""
        pending_ts = None

    for o in events:
        if not isinstance(o, dict):
            continue
        t = o.get("type")
        ts = o.get("time")
        if ts is None:
            ts = ""

        if t == "config.update":
            if not meta["model"] and o.get("modelAlias"):
                meta["model"] = o["modelAlias"]
            continue
        if t == "turn.prompt":
            flush_text()
            text = join_input_text(o.get("input")).strip()
            if text:
                msgs.append(cs.m_message("user", text, to_iso_z(ts)))
            continue
        if t == "context.apply_compaction":
            # 全量压缩：压缩摘要本身是接续上下文的关键信息，转注记保留
            flush_text()
            summary = str(o.get("summary") or "").strip()
            text = "会话发生过上下文压缩（context.apply_compaction）"
            if summary:
                text += "，压缩摘要：\n" + summary
            msgs.append(cs.m_note(text, to_iso_z(ts)))
            continue
        if t == "context.append_loop_event":
            ev = o.get("event")
            if not isinstance(ev, dict):
                continue
            et = ev.get("type")
            if et == "step.begin":
                flush_text()
            elif et == "content.part":
                p = ev.get("part")
                if isinstance(p, dict) and p.get("type") == "text":
                    if not pending_text:
                        pending_ts = ts
                    pending_text += p.get("text") or ""
                # part.type=think 等推理内容不可跨模型迁移，跳过
            elif et == "tool.call":
                flush_text()
                msgs.append(cs.m_tool_use(
                    ev.get("toolCallId") or "",
                    ev.get("name") or "",
                    normalize_input(ev.get("name"), ev.get("args"), ev.get("display")),
                    to_iso_z(ts)))
            elif et == "tool.result":
                flush_text()
                r = ev.get("result")
                r = r if isinstance(r, dict) else {}
                content = output_to_text(r.get("output"))
                if not content.strip() and r.get("note"):
                    content = str(r["note"])
                msgs.append(cs.m_tool_result(
                    ev.get("toolCallId") or ev.get("parentUuid") or "",
                    content, r.get("isError") is True, to_iso_z(ts)))
            # step.end / 其他 loop 事件跳过
            continue
        # metadata / full_compaction.begin|complete / llm.request / usage.record /
        # context.append_message（注入）等跳过

    flush_text()
    return meta, msgs


# ---------- 标题解析 ----------

def resolve_title(state_title, msgs, sid):
    """主来源 state.title（Kimi 侧边栏显示的标题）。
    "New Session" 是未生成标题时的占位符，视为无标题走兜底：
    首条用户消息首行（<=60 字符，超出加省略号），再兜底会话 ID。"""
    t = (state_title or "").strip()
    if t and t != "New Session":
        return t
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m["text"].strip():
            first = re.split(r"\r\n|\r|\n", m["text"].strip())[0]
            return (first[:60] + "…") if len(first) > 60 else (first or sid)
    return sid


# ---------- 会话扫描 ----------

def scan_sessions(kimi_dir):
    """跨项目收集全部会话，按 state.updatedAt（缺省目录 mtime）倒序。"""
    workspaces = load_workspaces(kimi_dir)
    entries = load_session_index(kimi_dir)
    if not entries:
        entries = walk_sessions(kimi_dir, workspaces)
    sessions = []
    for e in entries:
        state = load_state(e["sessionDir"]) or {}
        updated_at = state.get("updatedAt") or ""
        sort_key = parse_ms(updated_at)
        if not sort_key:
            try:
                sort_key = os.path.getmtime(e["sessionDir"]) * 1000.0
            except OSError:
                sort_key = 0.0
        sessions.append({
            "session_id": e["sessionId"],
            "session_dir": e["sessionDir"],
            "wire_path": wire_path_of(e["sessionDir"]),
            "cwd": e.get("workDir") or state.get("workDir") or "",
            "state_title": state.get("title") or "",
            "created_at": state.get("createdAt") or "",
            "updated_at": updated_at,
            "mtime": sort_key,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def _light_parse(s):
    """轻量解析 wire 供列表展示（标题兜底/条数/末次时间）；失败返回空列表。"""
    try:
        return normalize(parse_wire(s["wire_path"]))[1]
    except cs.ConvertError:
        return []


def list_sessions(dir_=None, project=None):
    kimi_dir = dir_ or default_dir()
    sessions = scan_sessions(kimi_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    out = []
    for s in sessions[:50]:
        msgs = _light_parse(s)
        out.append({
            "session_id": s["session_id"],
            "title": resolve_title(s["state_title"], msgs, s["session_id"]),
            "cwd": s["cwd"],
            "mtime": s["mtime"],
            "path": s["wire_path"],
            "count": len(msgs),
            "last_ts": cs.fmt_cst(msgs[-1]["ts"]) if msgs
                       else cs.fmt_cst(to_iso_z(s["updated_at"])),
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    kimi_dir = dir_ or default_dir()
    sessions = scan_sessions(kimi_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["cwd"]) == norm]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id)
                    or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Kimi 会话（dir={kimi_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    meta, msgs = normalize(parse_wire(target["wire_path"]))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['wire_path']}")

    state = load_state(target["session_dir"]) or {}
    sid = target["session_id"]
    title = resolve_title(target["state_title"], msgs, sid)
    ref = cs.make_ref("kimi", sid, title, target["cwd"],
                      model=meta["model"],
                      started_at=to_iso_z(state.get("createdAt")) or msgs[0]["ts"],
                      ended_at=to_iso_z(state.get("updatedAt")) or msgs[-1]["ts"],
                      source=target["wire_path"])
    return ref, msgs
