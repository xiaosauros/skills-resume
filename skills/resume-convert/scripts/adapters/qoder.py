#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/qoder: Qoder CLI（~/.qoder/projects 会话 JSONL）-> 归一化会话。

格式依据（与 resume-qoder 一致）：
  ~/.qoder/projects/<编码项目路径>/<session-id>.jsonl，同目录还可能有
  <session-id>/state.json（workspaceDirectories 兜底 cwd）与旧版
  <session-id>-session.json（仅元数据快照，无可转换 transcript）。
  事件 type ∈ runtime-config / user / assistant / custom-title / ai-title /
  agent-name / system / last-prompt / file-history-snapshot 等。
  - 用户输入：type=user 且非 isMeta / isVisibleInTranscriptOnly 的文本
    （剔除 <system-reminder> 包裹内容）
  - 模型输出：type=assistant 的 text 块；tool_use / tool_result 块按对转换
  - 推理（thinking / redacted_thinking）不可跨模型迁移，跳过
  - isCompactSummary / system(compact_summary|summary) -> 转换注记
  - isSidechain 事件按原始顺序作为普通消息保留（与 resume-qoder 语义一致）
标题：custom-title > ai-title > agent-name > 首条用户消息首行（60 字截断）。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

SYS_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def default_dir():
    return os.environ.get("QODER_HOME",
                          os.path.expanduser(os.path.join("~", ".qoder")))


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p)))) if p else ""


def timestamp_ms(value):
    """时间统一成毫秒：秒/毫秒时间戳或 ISO 字符串均可（与 resume-qoder 一致）。"""
    if value is None or value == "":
        return 0
    try:
        number = float(value)
        return int(number * 1000) if abs(number) < 10_000_000_000 else int(number)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return 0


def ts_to_iso(value):
    """毫秒时间 -> ISO UTC 'Z' 结尾（消息时间戳用；py/js 输出保持一致）。"""
    ms = timestamp_ms(value)
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_jsonl(path):
    """读会话 JSONL，返回事件列表（坏行跳过）。"""
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
        raise cs.ConvertError(f"无法读取 Qoder 会话文件：{e}")
    return events


def extract_text(content):
    """content 中的 text 块拼接（thinking 等其他块自然跳过）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return ""


def result_text(content):
    """tool_result.content 统一抽为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values = []
        for b in content:
            if isinstance(b, dict):
                values.append(str(b.get("text") or b.get("content") or ""))
            elif b is not None:
                values.append(str(b))
        return "\n".join(v for v in values if v)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def as_text(value):
    """非字符串值统一为 JSON 文本（保证 py/js 序列化一致）。"""
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"))


def strip_reminders(value):
    return SYS_REMINDER_RE.sub("", value or "").strip()


def normalize(events):
    """事件流 -> 归一化条目 + 元信息（与 resume-qoder 的 normalize_events 一致；
    差异：compact 摘要作为 note 条目进入主时间线，严格保持源顺序）。"""
    items = []
    titles = {"custom": "", "ai": "", "agent": ""}
    cwd = git_branch = model = reasoning_effort = context_window = ""
    entrypoint = version = ""
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
            compact = (event.get("summary") or event.get("content")
                       or extract_text((event.get("message") or {}).get("content")))
            if compact:
                items.append({"kind": "note", "timestamp": timestamp_ms(event.get("timestamp")),
                              "text": as_text(compact)})
            continue
        if event_type == "system":
            if event.get("subtype") in {"compact_summary", "summary"}:
                compact = event.get("summary") or event.get("content")
                if compact:
                    items.append({"kind": "note", "timestamp": timestamp_ms(event.get("timestamp")),
                                  "text": as_text(compact)})
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
                        items.append({
                            "kind": "tool_result", "timestamp": ts,
                            "tool_use_id": block.get("tool_use_id") or "",
                            "content": result_text(block.get("content")),
                            "is_error": bool(block.get("is_error")),
                        })
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
                        items.append({
                            "kind": "tool_use", "timestamp": ts,
                            "name": block.get("name") or "tool",
                            "input": block.get("input") or {},
                            "tool_use_id": block.get("id") or "",
                        })
            if event.get("error") and not text.strip():
                items.append({
                    "kind": "assistant_text", "timestamp": ts,
                    "text": "[API 错误] " + result_text(
                        event.get("errorDetails") or event.get("error")),
                })
    return {
        "items": items, "titles": titles, "cwd": cwd, "git_branch": git_branch,
        "model": model, "reasoning_effort": reasoning_effort,
        "context_window": context_window, "entrypoint": entrypoint,
        "version": version, "is_sidechain": is_sidechain,
    }


def title_for(norm, session_id):
    """标题：自定义 > AI > 子代理名 > 首条用户消息首行（60 字截断）。"""
    for key in ("custom", "ai", "agent"):
        if norm["titles"].get(key):
            return norm["titles"][key]
    for item in norm["items"]:
        if item["kind"] == "user_text":
            first = (item["text"].strip().splitlines() or [""])[0]
            return first[:60] + ("…" if len(first) > 60 else "") or session_id
    return session_id


def state_workspaces(path):
    """<session-id>/state.json 的 workspaceDirectories（cwd 兜底）。"""
    state_path = os.path.join(os.path.splitext(path)[0], "state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return [str(v) for v in data.get("workspaceDirectories") or []]
    except (OSError, ValueError, TypeError):
        pass
    return []


def current_meta(path):
    """当前格式（JSONL transcript）的会话元信息；文件不可读返回 None。"""
    try:
        events = parse_jsonl(path)
        mtime = os.stat(path).st_mtime_ns / 1_000_000  # 毫秒
    except (OSError, cs.ConvertError):
        return None
    norm = normalize(events)
    timestamps = [i["timestamp"] for i in norm["items"] if i.get("timestamp")]
    sid = os.path.splitext(os.path.basename(path))[0]
    workspaces = state_workspaces(path)
    fallback = int(mtime)
    return {
        "session_id": sid,
        "title": title_for(norm, sid),
        "directory": norm["cwd"] or (workspaces[0] if workspaces else ""),
        "created": timestamps[0] if timestamps else fallback,
        "updated": timestamps[-1] if timestamps else fallback,
        "count": len(norm["items"]),
        "source": "jsonl",
        "path": path,
        "mtime": mtime,
        "norm": norm,
    }


def legacy_meta(path):
    """旧版 *-session.json：只有元数据快照，没有可读 transcript。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        mtime = os.stat(path).st_mtime_ns / 1_000_000  # 毫秒
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    name = os.path.basename(path)
    sid = obj.get("id") or name[:-len("-session.json")]
    fallback = int(mtime)
    return {
        "session_id": str(sid),
        "title": obj.get("title") or str(sid),
        "directory": obj.get("working_dir") or "",
        "created": obj.get("created_at") or fallback,
        "updated": obj.get("updated_at") or fallback,
        "count": obj.get("message_count") or 0,
        "source": "legacy-metadata",
        "path": path,
        "mtime": mtime,
    }


def scan_sessions(qoder_dir):
    """扫描 ~/.qoder/projects 全部会话，按最后活动时间倒序；
    旧版 *-session.json 与当前 JSONL 同 ID 时以当前为准。"""
    root = os.path.join(qoder_dir, "projects")
    sessions = []
    try:
        projects = sorted(os.listdir(root))
    except OSError:
        return sessions
    current_ids = set()
    for name in projects:
        project_dir = os.path.join(root, name)
        if not os.path.isdir(project_dir):
            continue
        try:
            entries = sorted(os.listdir(project_dir))
        except OSError:
            continue
        # 先扫当前格式（与 resume-qoder 的 glob 顺序一致），再补旧版元数据
        for fn in entries:
            if not fn.endswith(".jsonl"):
                continue
            meta = current_meta(os.path.join(project_dir, fn))
            if meta:
                sessions.append(meta)
                current_ids.add(meta["session_id"])
        for fn in entries:
            if not fn.endswith("-session.json"):
                continue
            meta = legacy_meta(os.path.join(project_dir, fn))
            if meta and meta["session_id"] not in current_ids:
                sessions.append(meta)
    sessions.sort(key=lambda s: timestamp_ms(s["updated"]), reverse=True)
    return sessions


def list_sessions(dir_=None, project=None):
    qoder_dir = dir_ or default_dir()
    sessions = scan_sessions(qoder_dir)
    if project:
        target = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == target]
    return [{
        "session_id": s["session_id"], "title": s["title"], "cwd": s["directory"],
        "mtime": s["mtime"], "path": s["path"],
        "count": s["count"], "last_ts": cs.fmt_cst(ts_to_iso(s["updated"])),
    } for s in sessions]


def load_session(dir_=None, project=None, session_id=None):
    qoder_dir = dir_ or default_dir()
    sessions = scan_sessions(qoder_dir)
    if project:
        target = norm_path(project)
        sessions = [s for s in sessions if norm_path(s["directory"]) == target]
    if session_id:
        sessions = [s for s in sessions
                    if s["session_id"].startswith(session_id) or session_id in s["session_id"]]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 Qoder 会话（dir={qoder_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    target = sessions[0]
    if target["source"] != "jsonl":
        raise cs.ConvertError(
            f"旧版 Qoder 会话只保留本地元数据快照，没有可转换的对话记录：{target['path']}")
    norm = target["norm"]
    msgs = []
    for item in norm["items"]:
        ts = ts_to_iso(item.get("timestamp"))
        kind = item["kind"]
        if kind == "user_text":
            msgs.append(cs.m_message("user", item["text"], ts))
        elif kind == "assistant_text":
            msgs.append(cs.m_message("assistant", item["text"], ts))
        elif kind == "tool_use":
            msgs.append(cs.m_tool_use(item.get("tool_use_id") or "",
                                      item.get("name") or "tool",
                                      item.get("input") or {}, ts))
        elif kind == "tool_result":
            msgs.append(cs.m_tool_result(item.get("tool_use_id") or "",
                                         item.get("content") or "",
                                         bool(item.get("is_error")), ts))
        elif kind == "note":
            msgs.append(cs.m_note(item.get("text") or "", ts))
    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['path']}")

    sid = target["session_id"]
    timestamps = [i["timestamp"] for i in norm["items"] if i.get("timestamp")]
    ref = cs.make_ref("qoder", sid, title_for(norm, sid),
                      norm["cwd"] or target["directory"] or "",
                      model=norm["model"] or "",
                      started_at=ts_to_iso(timestamps[0]) if timestamps else "",
                      ended_at=ts_to_iso(timestamps[-1]) if timestamps else "",
                      source=target["path"])
    return ref, msgs
