#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude_session: 把任意工具的归一化会话内容转换成 Claude Code 原生 JSONL 会话文件。

共享工具类，供同目录 export_to_claude.py 与各 adapters/<tool>.py 使用：
  - Msg / SessionRef 归一化模型（各工具适配器唯一的输出契约）
  - build_records(): 归一化消息 -> Claude Code JSONL 记录（uuid 链、工具配对、截断）
  - validate_records(): 写盘前校验（JSON 可序列化、链完整性、tool_use/tool_result 配对）
  - write_session(): 原子写入 ~/.claude/projects/<项目>/<sessionId>.jsonl

只写新文件、绝不覆盖已有会话；支持 --dry-run 与导出到任意目录（测试用）。
与同目录 claude_session.js 功能等价、输出一致。
"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

# 写入记录时标记的 Claude Code 版本（信息性字段，与当前版本一致即可）
CLAUDE_CODE_VERSION = "2.1.278"

# 默认截断长度：工具结果 / 普通文本 / 工具入参
DEFAULT_MAX_TOOL_OUTPUT = 20000
DEFAULT_MAX_TEXT = 200000

# 转换注记前缀（compact 摘要等元信息以用户消息形式注入，明确标记非原始对话）
NOTE_PREFIX = "[转换注记] "


class ConvertError(Exception):
    """转换失败（找不到会话 / 目标不合法等），携带面向用户的中文信息。"""


# ---------------------------------------------------------------- 归一化模型

def m_message(role, text, ts=""):
    """普通文本消息。role: 'user' | 'assistant'"""
    return {"kind": "message", "role": role, "text": text, "ts": ts}


def m_tool_use(call_id, name, input_obj, ts=""):
    """助手发起的工具调用（assistant 侧）。"""
    return {"kind": "tool_use", "call_id": call_id, "name": name,
            "input": input_obj, "ts": ts}


def m_tool_result(call_id, content, is_error=False, ts=""):
    """工具返回结果（user 侧），必须与前面的 tool_use.call_id 配对。"""
    return {"kind": "tool_result", "call_id": call_id,
            "content": content, "is_error": bool(is_error), "ts": ts}


def m_note(text, ts=""):
    """转换注记（compact 摘要、跳过的推理等元信息），渲染为带前缀的用户消息。"""
    return {"kind": "note", "text": text, "ts": ts}


def make_ref(tool, session_id, title, cwd, model="", started_at="", ended_at="", source=""):
    """会话元信息。cwd 决定写入哪个 Claude 项目目录；title 用于 ai-title。"""
    return {
        "tool": tool, "session_id": session_id, "title": title or "",
        "cwd": cwd or "", "model": model or "",
        "started_at": started_at or "", "ended_at": ended_at or "",
        "source": source or "",
    }


# ---------------------------------------------------------------- 基础工具

def get_claude_dir(explicit=None):
    """Claude Code 配置目录：--claude-dir > $CLAUDE_CONFIG_DIR > ~/.claude"""
    return os.path.abspath(explicit or os.environ.get("CLAUDE_CONFIG_DIR")
                           or os.path.expanduser(os.path.join("~", ".claude")))


def project_dir_name(cwd):
    """Claude Code 的项目目录编码规则：非字母数字字符全部替换为 '-'。
    实测样本：C:\\Users\\x\\Desktop -> C--Users-x-Desktop；
             D:\\workspace\\a\\b-repo -> D--workspace-a-b-repo。"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def new_uuid():
    return str(uuid.uuid4())


def new_session_id():
    return str(uuid.uuid4())


def _rand_hex(n=12):
    return uuid.uuid4().hex[:n]


def new_msg_id(ts_iso=""):
    """合成 assistant message.id（形如 msg_<时间戳><随机>，信息性字段）。"""
    digits = re.sub(r"[^0-9]", "", ts_iso or "")[:17]
    return "msg_" + digits + _rand_hex(24 - len(digits))


def sanitize_session_id(sid):
    s = re.sub(r"[^A-Za-z0-9_-]", "-", str(sid or "").strip())
    return s[:100] or new_session_id()


def sanitize_tool_name(name):
    """工具名必须是 ^[a-zA-Z0-9_-]{1,64}$（API 校验），非法字符替换为 '_'。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(name or "tool"))
    return s[:64] or "tool"


def to_iso_z(ts):
    """把各种 ISO8601 时间统一为毫秒精度的 UTC 'Z' 结尾格式；解析失败返回 ''。"""
    if not ts:
        return ""
    s = str(ts).strip()
    if not s:
        return ""
    try:
        txt = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    except (ValueError, TypeError):
        return ""


def fmt_cst(ts):
    """展示用时间：UTC -> UTC+8 'YYYY-MM-dd HH:mm:ss'（与 resume-* 一致）。"""
    iso = to_iso_z(ts)
    if not iso:
        return ""
    try:
        from datetime import timedelta
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""


def truncate_text(s, limit):
    s = str(s)
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + f"\n…[已截断，原长 {len(s)} 字符]"


def _ensure_iso(value, fallback):
    return to_iso_z(value) or to_iso_z(fallback)


# ---------------------------------------------------------------- 归一化 -> 记录

def _normalize_msgs(msgs, ref, base_time):
    """规整 Msg 列表：时间戳回填 + 单调化、工具名净化、call_id 去重、内容字符串化。"""
    out = []
    seen_ids = set()

    for m in msgs:
        if not isinstance(m, dict) or not m.get("kind"):
            continue
        kind = m["kind"]
        m = dict(m)
        m["ts"] = to_iso_z(m.get("ts") or "")
        if kind == "tool_use":
            m["name"] = sanitize_tool_name(m.get("name"))
            inp = m.get("input")
            if inp is None:
                inp = {}
            if not isinstance(inp, dict):
                inp = {"raw": inp if isinstance(inp, str) else json.dumps(
                    inp, ensure_ascii=False, default=str)}
            m["input"] = inp
            cid = str(m.get("call_id") or "")
            if not cid or cid in seen_ids:
                cid = f"call_{len(seen_ids) + 1}_{_rand_hex(8)}"
            seen_ids.add(cid)
            m["call_id"] = cid
        elif kind == "tool_result":
            cid = str(m.get("call_id") or "")
            if not cid or cid not in seen_ids:
                # 结果先于调用（异常流），保留内容但给独立 id，后续会被当孤儿处理
                cid = cid or f"call_orphan_{len(out)}_{_rand_hex(6)}"
                if cid not in seen_ids:
                    seen_ids.add(cid)
            m["call_id"] = cid
            m["content"] = "" if m.get("content") is None else str(m.get("content"))
            m["is_error"] = bool(m.get("is_error"))
        elif kind in ("message", "note"):
            m["text"] = "" if m.get("text") is None else str(m.get("text"))
        else:
            continue
        out.append(m)

    # 时间戳单调化：无 ts 的用 started_at 递推兜底；所有 ts 保证非递减
    last = ""
    fallback_base = _ensure_iso(ref.get("started_at"), "") or ""
    for m in out:
        if not m["ts"] and fallback_base:
            m["ts"] = to_iso_z(fallback_base)  # 同一基准即可，单调化会兜底排序
        if m["ts"] and last and m["ts"] < last:
            m["ts"] = last
        last = m["ts"] or last
    return out


def _group_records(msgs, degrade_tools):
    """把归一化消息按角色分组为记录：连续同角色合并为一条记录。

    assistant 记录块序：[text..., tool_use...]（tool_use 必须在末尾）
    user 记录块序：[tool_result..., text...]（API 要求 tool_result 位于 user 消息前部）
    degrade_tools=True 时把工具调用/结果降级为纯文本，完全不产生配对结构。"""
    records = []  # [{role, blocks:[...], ts}]
    for m in msgs:
        kind = m["kind"]
        if degrade_tools and kind == "tool_use":
            # 紧凑分隔符与 JS JSON.stringify 输出一致，保证双语言指纹相同
            args = json.dumps(m.get("input", {}), ensure_ascii=False, separators=(",", ":"))
            m = m_message("assistant", f"【工具调用】{m['name']} {truncate_text(args, 2000)}", m["ts"])
            kind = "message"
        elif degrade_tools and kind == "tool_result":
            tag = "（错误）" if m.get("is_error") else ""
            m = m_message("user", f"【工具结果】{tag}{m.get('content', '')}", m["ts"])
            kind = "message"
        elif kind == "note":
            m = m_message("user", NOTE_PREFIX + str(m.get("text", "")), m["ts"])
            kind = "message"

        if kind == "message":
            role = "assistant" if m.get("role") == "assistant" else "user"
            text = truncate_text(m.get("text", ""), DEFAULT_MAX_TEXT)
            block = {"type": "text", "text": text}
        elif kind == "tool_use":
            block = {"type": "tool_use", "id": m["call_id"], "name": m["name"],
                     "input": m.get("input", {})}
            role = "assistant"
        elif kind == "tool_result":
            content = truncate_text(m.get("content", ""), DEFAULT_MAX_TOOL_OUTPUT)
            block = {"type": "tool_result", "tool_use_id": m["call_id"],
                     "content": content, "is_error": bool(m.get("is_error"))}
            role = "user"
        else:
            continue

        if records and records[-1]["role"] == role:
            records[-1]["blocks"].append(block)
        else:
            records.append({"role": role, "blocks": [block], "ts": m.get("ts", "")})

    # 块序整理
    for r in records:
        if r["role"] == "assistant":
            texts = [b for b in r["blocks"] if b["type"] == "text"]
            tools = [b for b in r["blocks"] if b["type"] == "tool_use"]
            r["blocks"] = texts + tools
        else:
            results = [b for b in r["blocks"] if b["type"] == "tool_result"]
            texts = [b for b in r["blocks"] if b["type"] == "text"]
            r["blocks"] = results + texts
    return records


def _fix_tool_pairing(records, warnings):
    """保证每个 tool_use 都被紧随其后的 user 记录中的 tool_result 覆盖：
    缺失的结果以合成 user 记录补齐（API 硬性要求，否则恢复后继续对话会报错）。"""
    fixed = []
    for r in records:
        fixed.append(r)
        if r["role"] != "assistant":
            continue
        pending = [b["id"] for b in r["blocks"] if b["type"] == "tool_use"]
        if not pending:
            continue
        nxt = None
        if len(fixed) < len(records):
            nxt = records[len(fixed)]
        if nxt is not None and nxt["role"] == "user":
            covered = {b["tool_use_id"] for b in nxt["blocks"] if b["type"] == "tool_result"}
            missing = [c for c in pending if c not in covered]
            for cid in missing:
                nxt["blocks"].insert(0, {
                    "type": "tool_result", "tool_use_id": cid,
                    "content": "（转换器注记：原会话未记录该工具的返回结果）",
                    "is_error": False,
                })
                warnings.append(f"工具调用 {cid} 缺少结果记录，已合成空结果补齐")
            continue
        for cid in pending:
            warnings.append(f"工具调用 {cid} 后无用户记录，已合成结果记录补齐")
        fixed.append({
            "role": "user",
            "blocks": [{
                "type": "tool_result", "tool_use_id": cid,
                "content": "（转换器注记：原会话未记录该工具的返回结果）",
                "is_error": False,
            } for cid in pending],
            "ts": r["ts"],
        })
    return fixed


def _drop_orphan_results(records, warnings):
    """没有对应 tool_use 的 tool_result 降级为普通文本（API 会拒绝无配对的结果）。"""
    used = set()
    for r in records:
        if r["role"] == "assistant":
            for b in r["blocks"]:
                if b["type"] == "tool_use":
                    used.add(b["id"])
    for i, r in enumerate(records):
        if r["role"] != "user":
            continue
        results = [b for b in r["blocks"] if b["type"] == "tool_result"]
        keep = [b for b in results if b["tool_use_id"] in used]
        dropped = [b for b in results if b["tool_use_id"] not in used]
        if dropped:
            for b in dropped:
                warnings.append(f"孤立的工具结果 {b['tool_use_id']} 已降级为文本保留")
                note = {
                    "type": "text",
                    "text": NOTE_PREFIX + f"孤立工具结果（{b['tool_use_id']}）：\n{b['content']}",
                }
                # 插到 user 记录文本块最前面，保持时间语义
                texts = [b2 for b2 in r["blocks"] if b2["type"] == "text"]
                if texts:
                    texts[0]["text"] = note["text"] + "\n\n" + texts[0]["text"]
                else:
                    r["blocks"].append(note)
        r["blocks"] = keep + [b for b in r["blocks"] if b["type"] == "text"]
    return records


def build_records(ref, msgs, degrade_tools=False,
                  max_tool_output=DEFAULT_MAX_TOOL_OUTPUT,
                  max_text=DEFAULT_MAX_TEXT):
    """归一化消息 -> (claude_records, warnings)。

    claude_records 为可直接 JSONL 序列化的记录列表，首条为 ai-title，
    之后 user/assistant 记录以 parentUuid -> uuid 链接。"""
    global DEFAULT_MAX_TOOL_OUTPUT, DEFAULT_MAX_TEXT
    saved = (DEFAULT_MAX_TOOL_OUTPUT, DEFAULT_MAX_TEXT)
    DEFAULT_MAX_TOOL_OUTPUT, DEFAULT_MAX_TEXT = max_tool_output, max_text
    try:
        warnings = []
        msgs = _normalize_msgs(msgs, ref, ref.get("started_at") or "")

        sid = sanitize_session_id(ref.get("session_id") or new_session_id())
        title = (ref.get("title") or "").strip()
        if not title:
            for m in msgs:
                if m["kind"] == "message" and m.get("role") == "user" and m.get("text", "").strip():
                    first = m["text"].strip().splitlines()[0]
                    title = first[:60] + ("…" if len(first) > 60 else "")
                    break
        if not title:
            title = sid

        records = _group_records(msgs, degrade_tools)
        records = _fix_tool_pairing(records, warnings)
        records = _drop_orphan_results(records, warnings)

        if not records:
            raise ConvertError("会话内容为空，无法转换")
        if records[0]["role"] == "assistant":
            warnings.append("会话以助手消息开头，已前置一条用户注记（API 要求首条为 user）")
            records.insert(0, {
                "role": "user",
                "blocks": [{"type": "text", "text": NOTE_PREFIX + "历史会话由 "
                            + (ref.get("tool") or "外部工具") + " 转换而来"}],
                "ts": records[0]["ts"],
            })

        # 移除内部字段，生成 JSONL 记录
        claude_records = [{"type": "ai-title", "aiTitle": title, "sessionId": sid}]
        parent = None
        for i, r in enumerate(records):
            uid = new_uuid()
            ts = r.get("ts") or to_iso_z(ref.get("started_at") or "") \
                or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if r["role"] == "user":
                if any(b["type"] == "tool_result" for b in r["blocks"]):
                    content = []
                    for b in r["blocks"]:
                        if b["type"] == "tool_result":
                            content.append({
                                "type": "tool_result", "tool_use_id": b["tool_use_id"],
                                "content": b["content"], "is_error": bool(b["is_error"]),
                            })
                        else:
                            content.append({"type": "text", "text": b["text"]})
                else:
                    content = "\n\n".join(b["text"] for b in r["blocks"])
                message = {"role": "user", "content": content}
            else:
                blocks = []
                for b in r["blocks"]:
                    if b["type"] == "text":
                        blocks.append({"type": "text", "text": b["text"]})
                    else:
                        blocks.append({
                            "type": "tool_use", "id": b["id"], "name": b["name"],
                            "input": b["input"],
                        })
                message = {
                    "id": new_msg_id(ts), "type": "message", "role": "assistant",
                    "model": ref.get("model") or "unknown",
                    "content": blocks, "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            rec = {
                "parentUuid": parent, "isSidechain": False, "type": r["role"],
                "message": message, "uuid": uid, "timestamp": ts,
                "sessionId": sid, "session_id": sid,
                "userType": "external", "cwd": ref.get("cwd") or "",
                "version": CLAUDE_CODE_VERSION,
            }
            claude_records.append(rec)
            parent = uid
        return claude_records, warnings
    finally:
        DEFAULT_MAX_TOOL_OUTPUT, DEFAULT_MAX_TEXT = saved


# ---------------------------------------------------------------- 校验

def validate_records(records):
    """写盘前校验，返回 (errors, warnings)。errors 非空时禁止写盘。"""
    errors = []
    warnings = []
    if not records:
        return ["记录为空"], warnings

    if records[0].get("type") != "ai-title":
        errors.append("首条记录必须是 ai-title")
    sid = records[0].get("sessionId")

    parent = None
    seen_uuids = set()
    pending_calls = []   # 尚未被结果的 tool_use id
    msg_records = 0
    for i, rec in enumerate(records[1:], start=1):
        try:
            json.dumps(rec, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            errors.append(f"第 {i + 1} 行不可 JSON 序列化: {e}")
            continue
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            errors.append(f"第 {i + 1} 行 type 非法: {rtype}")
            continue
        msg_records += 1
        uid = rec.get("uuid")
        if not uid or uid in seen_uuids:
            errors.append(f"第 {i + 1} 行 uuid 缺失或重复")
            continue
        seen_uuids.add(uid)
        if rec.get("parentUuid") != parent:
            errors.append(f"第 {i + 1} 行 parentUuid 链断裂")
        parent = uid
        if rec.get("sessionId") != sid:
            errors.append(f"第 {i + 1} 行 sessionId 与标题记录不一致")
        if not rec.get("timestamp"):
            warnings.append(f"第 {i + 1} 行缺少 timestamp")

        content = (rec.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else [])
        if rtype == "assistant":
            for b in blocks:
                if b.get("type") == "tool_use":
                    tid = b.get("id")
                    if not tid or b.get("name") is None:
                        errors.append(f"第 {i + 1} 行 tool_use 缺少 id/name")
                    else:
                        pending_calls.append(tid)
                elif b.get("type") not in ("text", "thinking"):
                    errors.append(f"第 {i + 1} 行 assistant 含非法块: {b.get('type')}")
        else:
            for b in blocks:
                if b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid in pending_calls:
                        pending_calls.remove(tid)
                    else:
                        errors.append(f"第 {i + 1} 行 tool_result 无配对 tool_use: {tid}")
    if pending_calls:
        errors.append(f"存在未被结果的 tool_use: {pending_calls}")
    if msg_records < 1:
        errors.append("没有任何 user/assistant 消息记录")
    return errors, warnings


# ---------------------------------------------------------------- 写入

def decide_target(ref, claude_dir=None, project_override=None,
                  out_dir=None, out_file=None, session_id=None):
    """决定输出文件路径。返回 (path, session_id, project_dir_name)。"""
    sid = sanitize_session_id(session_id or ref.get("session_id") or new_session_id())
    if out_file:
        return os.path.abspath(out_file), sid, ""
    cwd = project_override or ref.get("cwd") or ""
    if not cwd:
        raise ConvertError("会话缺少项目路径（cwd），无法定位 ~/.claude/projects 目标目录；"
                           "请用 --to-project 指定项目路径或 --out-file 指定输出文件")
    proj = project_dir_name(cwd)
    root = out_dir or os.path.join(get_claude_dir(claude_dir), "projects", proj)
    return os.path.join(os.path.abspath(root), f"{sid}.jsonl"), sid, proj


def write_session(ref, msgs, claude_dir=None, project_override=None,
                  out_dir=None, out_file=None, session_id=None,
                  degrade_tools=False, title_prefix="",
                  max_tool_output=DEFAULT_MAX_TOOL_OUTPUT,
                  max_text=DEFAULT_MAX_TEXT, dry_run=False,
                  allow_overwrite=False):
    """构建、校验并写入 Claude 会话文件。返回结果 dict：
    {ok, path, session_id, title, records, warnings, errors, dry_run, wrote}"""
    ref = dict(ref)
    if title_prefix and ref.get("title"):
        ref["title"] = title_prefix + ref["title"]

    records, warnings = build_records(
        ref, msgs, degrade_tools=degrade_tools,
        max_tool_output=max_tool_output, max_text=max_text)
    errors, vwarns = validate_records(records)
    warnings = warnings + vwarns

    path, sid, proj = decide_target(ref, claude_dir, project_override,
                                    out_dir, out_file, session_id)
    result = {
        "ok": not errors, "path": path, "session_id": sid,
        "title": records[0].get("aiTitle", "") if records else "",
        "records": len(records), "warnings": warnings, "errors": errors,
        "dry_run": bool(dry_run), "wrote": False, "project_dir": proj,
    }
    if errors:
        return result

    if dry_run:
        return result
    if os.path.exists(path) and not allow_overwrite:
        result["errors"] = [f"目标文件已存在，拒绝覆盖：{path}（换 --session-id 或删除后重试）"]
        result["ok"] = False
        return result

    body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for r in records)
    parent_dir = os.path.dirname(path)
    os.makedirs(parent_dir, exist_ok=True)
    tmp = path + ".tmp-" + _rand_hex(6)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    result["wrote"] = True
    return result


def setup_utf8_stdio():
    """Windows 中文控制台兜底：强制 stdout/stderr 使用 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", newline="")
        except Exception:
            pass
