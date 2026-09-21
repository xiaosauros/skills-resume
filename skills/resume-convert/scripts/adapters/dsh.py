#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters/dsh: DeepSeek Harness（~/.dsh 的 zstd 压缩 JSONL 会话）-> 归一化会话。

格式依据（与 resume-dsh 一致）：
  <dsh_dir>/sessions/<group>/<sid>/session.jsonl.zstd（多 frame 追加，兼容明文 session.jsonl）
  首行为 {type:"session", id, version:0, cwd, createdAt} header；事件带 seq/time/data。
  - 按 surfaceOp（append/replace）重建当前 surface，只导出现在面上的消息
  - 用户输入 user/message、模型输出 assistant/message、工具 tool/call + tool/result
  - 推理（reasoning/thinking）不可迁移，跳过；compaction/summary -> 转换注记
  - 已被压缩出面的陈旧 tool/call 跳过，避免产生没有结果配对的调用
标题：storages/session_projcache.json 的 title 优先，其次 session/title 事件、首条用户消息。
"""

import datetime as _dt
import json
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_session as cs  # noqa: E402

ZSTD_MAGIC = 0xFD2FB528
CST_OFFSET_MS = 8 * 60 * 60 * 1000


def default_dir():
    return os.path.abspath(os.environ.get("DSH_HOME")
                           or os.path.expanduser(os.path.join("~", ".dsh")))


def norm_path(value):
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def js_string(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def number_or_zero(value):
    if isinstance(value, bool) or value is None or value == "":
        return 0
    try:
        number = float(value)
        return number if math.isfinite(number) else 0
    except (TypeError, ValueError):
        return 0


def ts_to_iso(value):
    """DSH 事件时间为 epoch 毫秒，转成毫秒精度 UTC ISO 串（claude_session 只认 ISO）。"""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            millis = float(value)
        elif re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
            millis = float(value)
        else:
            text = str(value)
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.astimezone(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    except (OverflowError, ValueError, TypeError):
        return ""
    if not math.isfinite(millis):
        return ""
    try:
        parsed = _dt.datetime.fromtimestamp(millis / 1000, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- zstd 多 frame 日志

def scan_zstd_frames(buffer, max_frames=None):
    """逐 frame 扫描 zstd 缓冲（DSH 日志由多个独立 frame 追加组成）。

    返回 ([(start, end)], 撕裂尾帧起点或 None)；与 resume_dsh 的实现一致。"""
    frames = []
    offset = 0
    while offset < len(buffer):
        start = offset
        if len(buffer) - offset < 4:
            return frames, start
        if struct.unpack_from("<I", buffer, offset)[0] != ZSTD_MAGIC:
            raise cs.ConvertError(f"Zstandard 日志在字节 {offset} 处的 frame magic 无效")
        offset += 4
        if offset == len(buffer):
            return frames, start

        descriptor = buffer[offset]
        offset += 1
        if descriptor & 0x18:
            raise cs.ConvertError(
                f"Zstandard 日志在字节 {offset - 1} 处使用了保留的 frame header 位")
        content_size_flag = descriptor >> 6
        single_segment = bool(descriptor & 0x20)
        checksum = bool(descriptor & 0x04)
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0)
            if content_size_flag == 0
            else 1 << content_size_flag)
        remaining_header = (
            (0 if single_segment else 1) + dictionary_bytes + content_size_bytes)
        if len(buffer) - offset < remaining_header:
            return frames, start
        offset += remaining_header

        while True:
            if len(buffer) - offset < 3:
                return frames, start
            block_header = int.from_bytes(buffer[offset:offset + 3], "little")
            offset += 3
            last_block = bool(block_header & 1)
            block_type = (block_header >> 1) & 3
            block_size = block_header >> 3
            if block_type == 3:
                raise cs.ConvertError(
                    f"Zstandard 日志在字节 {offset - 3} 处使用了保留 block 类型")
            payload_bytes = 1 if block_type == 1 else block_size
            if len(buffer) - offset < payload_bytes:
                return frames, start
            offset += payload_bytes
            if last_block:
                break

        if checksum:
            if len(buffer) - offset < 4:
                return frames, start
            offset += 4
        frames.append((start, offset))
        if max_frames is not None and len(frames) == max_frames:
            return frames, None
    return frames, None


def get_zstd_decompressor():
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress
    except ImportError:
        try:
            import zstandard  # type: ignore[import-not-found]

            return zstandard.ZstdDecompressor().decompress
        except ImportError as error:
            raise cs.ConvertError(
                "当前 Python 不支持 Zstandard；请使用 Python 3.14+，或安装 zstandard 包"
            ) from error


def parse_jsonl(text):
    """解析 JSONL；最后一条残缺行（无换行结尾）按撕裂尾安全忽略。"""
    rows = []
    raw_lines = re.split(r"\r?\n", text)
    has_terminating_newline = bool(re.search(r"(?:\r?\n)$", text))
    for index, raw in enumerate(raw_lines):
        line = raw.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            if index == len(raw_lines) - 1 and not has_terminating_newline:
                break
            raise cs.ConvertError(
                f"会话日志第 {index + 1} 行不是有效 JSON：{error.msg}") from error
    return rows


def read_artifact(file, first_frame_only=False):
    if str(file).endswith(".zstd"):
        try:
            with open(file, "rb") as f:
                source = f.read()
        except OSError as error:
            raise cs.ConvertError(f"无法读取会话日志：{error}")
        frames, torn_start = scan_zstd_frames(source, 1 if first_frame_only else None)
        if not frames:
            raise cs.ConvertError("Zstandard 会话日志没有完整 frame")
        decompress = get_zstd_decompressor()
        chunks = []
        for start, end in frames:
            try:
                chunks.append(decompress(source[start:end]))
            except Exception as error:
                raise cs.ConvertError(
                    f"Zstandard 日志在字节 {start} 处的 frame 解压失败：{error}")
        return {
            "rows": parse_jsonl(b"".join(chunks).decode("utf-8")),
            "torn_tail": torn_start is not None,
            "frame_count": len(frames),
        }
    try:
        with open(file, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as error:
        raise cs.ConvertError(f"无法读取会话日志：{error}")
    return {"rows": parse_jsonl(text), "torn_tail": False, "frame_count": 0}


def find_artifact(session_dir):
    for name in ("session.jsonl.zstd", "session.jsonl"):
        file = os.path.join(session_dir, name)
        if os.path.isfile(file):
            return file
    return None


def load_projection_cache(dsh_dir):
    """storages/session_projcache.json 的 tables.sessions 表。"""
    file = os.path.join(dsh_dir, "storages", "session_projcache.json")
    if not os.path.isfile(file):
        return {}
    try:
        with open(file, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        tables = parsed.get("tables") if isinstance(parsed, dict) else None
        sessions = tables.get("sessions") if isinstance(tables, dict) else None
        return sessions if isinstance(sessions, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def projection_value(entry, name):
    if not isinstance(entry, dict):
        return None
    rows = entry.get("rows")
    row = rows.get(name) if isinstance(rows, dict) else None
    return row.get("val") if isinstance(row, dict) and "val" in row else None


def read_header(file, enforce_version=True):
    rows = read_artifact(file, True)["rows"]
    header = rows[0] if rows else None
    if (not isinstance(header, dict) or header.get("type") != "session"
            or not isinstance(header.get("id"), str)):
        raise cs.ConvertError("首条记录不是有效的 DSH session header")
    if enforce_version and header.get("version") != 0:
        raise cs.ConvertError(
            f"不支持 DSH session format version {header.get('version')}"
            "（当前解析器支持 0）")
    return header


# ---------------------------------------------------------------- 会话扫描

def scan_all_sessions(dsh_dir):
    """扫描 sessions/<group>/<sid>/，按最近活动倒序；坏会话记 scan_error 不中断。"""
    root = os.path.join(dsh_dir, "sessions")
    if not os.path.isdir(root):
        return []
    cache = load_projection_cache(dsh_dir)
    sessions = []
    try:
        groups = sorted(os.listdir(root))
    except OSError:
        return []
    for group in groups:
        group_dir = os.path.join(root, group)
        if not os.path.isdir(group_dir):
            continue
        try:
            entries = sorted(os.listdir(group_dir))
        except OSError:
            continue
        for name in entries:
            session_dir = os.path.join(group_dir, name)
            if not os.path.isdir(session_dir):
                continue
            artifact = find_artifact(session_dir)
            if artifact is None:
                continue
            try:
                header = read_header(artifact, False)
                projected = cache.get(header["id"], cache.get(name, {}))
                title = projection_value(projected, "title")
                list_meta = projection_value(projected, "sessionListMetadata") or {}
                stats = projection_value(projected, "sessionStats") or {}
                stat = os.stat(artifact)
                activity_at = (
                    number_or_zero(list_meta.get("lastPromptAt"))
                    if isinstance(list_meta, dict) else 0
                ) or stat.st_mtime * 1000 or number_or_zero(header.get("createdAt"))
                sessions.append({
                    "session_id": header["id"],
                    "session_dir": session_dir,
                    "artifact": artifact,
                    "header": header,
                    "projection": projected,
                    "cwd": header.get("cwd") or "",
                    "title": title if isinstance(title, str) else "",
                    "blank": isinstance(list_meta, dict)
                    and list_meta.get("blank") is True,
                    "count": number_or_zero(stats.get("turns"))
                    if isinstance(stats, dict) else 0,
                    "activity_at": activity_at,
                    "mtime": stat.st_mtime * 1000,
                    "scan_error": None if header.get("version") == 0 else (
                        "不支持 DSH session format version "
                        f"{header.get('version')}（当前解析器支持 0）"),
                })
            except Exception as error:
                sessions.append({
                    "session_id": name,
                    "session_dir": session_dir,
                    "artifact": artifact,
                    "header": {},
                    "projection": cache.get(name, {}),
                    "cwd": "",
                    "title": "",
                    "blank": False,
                    "count": 0,
                    "activity_at": 0,
                    "mtime": 0,
                    "scan_error": str(error),
                })
    sessions.sort(key=lambda item: (item["activity_at"], item["mtime"]), reverse=True)
    return sessions


def pick_session(sessions, value):
    """按 ID 前缀 -> ID 包含 -> 标题包含 匹配（与 resume_dsh --session 一致）。"""
    query = str(value).lower()
    for session in sessions:
        if session["session_id"].lower().startswith(query):
            return session
    for session in sessions:
        if query in session["session_id"].lower():
            return session
    for session in sessions:
        if query in (session.get("title") or "").lower():
            return session
    return None


# ---------------------------------------------------------------- 事件解析

def text_of(value, include_reasoning=False):
    """递归抽取文本；reasoning/thinking 块跳过（推理不可跨模型迁移）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part
            for part in (text_of(item, include_reasoning) for item in value)
            if part
        )
    if not isinstance(value, dict):
        return js_string(value)
    if not include_reasoning and value.get("type") in {
        "reasoning", "thinking", "reasoning-chunk",
    }:
        return ""
    if isinstance(value.get("text"), str):
        return value["text"]
    if "content" in value:
        return text_of(value["content"], include_reasoning)
    if "output" in value:
        return text_of(value["output"], include_reasoning)
    return ""


def parse_arguments(value):
    if value is None or value == "":
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(js_string(value))
    except json.JSONDecodeError:
        return {"_raw": js_string(value)}


def normalize_input(name, input_value):
    """工具入参按 resume-dsh 的规则归一化（run_code/shell/read/edit/grep/web）。"""
    source = input_value if isinstance(input_value, dict) else {}

    def pick(*keys):
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
        return ""

    if name == "run_code":
        return {"description": pick("description"), "code": pick("code")}
    if name in {"pwsh", "bash", "shell", "shell_command", "exec"}:
        return {
            "command": pick("command", "cmd", "script"),
            "workdir": pick("workdir", "cwd"),
        }
    if name in {"read", "read_file", "view_image"}:
        return {"path": pick("file_path", "path")}
    if name in {"edit", "write", "write_file", "str_replace"}:
        return {"path": pick("file_path", "path")}
    if name in {"grep", "glob", "search"}:
        return {
            "pattern": pick("pattern", "query"),
            "path": pick("path", "directory"),
        }
    if "web" in name:
        return {"query": pick("query", "q"), "url": pick("url")}
    return source


def is_real_user_message(data):
    source = data.get("source") if isinstance(data, dict) else None
    return (
        not isinstance(source, dict)
        or not source.get("kind")
        or source.get("kind") == "user"
    )


def current_surface_seqs(rows):
    """按 surfaceOp 重放，得到仍在当前 surface 上的 seq 集合。"""
    surface = []
    surface_types = {"user/message", "assistant/message", "tool/result"}
    for event in rows:
        if (not isinstance(event, dict)
                or event.get("type") not in surface_types
                or not (isinstance(event.get("seq"), int)
                        and not isinstance(event.get("seq"), bool))):
            continue
        seq = event["seq"]
        op = event.get("surfaceOp")
        if not op or op == "append":
            surface.append(seq)
            continue
        if not isinstance(op, dict) or op.get("op") != "replace":
            raise cs.ConvertError(f"会话事件 seq {seq} 使用了未知 surfaceOp")
        try:
            start = surface.index(op.get("start"))
            end = surface.index(op.get("end"))
        except ValueError as error:
            raise cs.ConvertError(
                f"会话事件 seq {seq} 的 surface replace 范围无效") from error
        if end < start:
            raise cs.ConvertError(f"会话事件 seq {seq} 的 surface replace 范围无效")
        surface[start:end + 1] = [seq]
    return set(surface)


def tool_result_call_id(data):
    """工具结果的 callId：message.source.callId 优先，其次块内 toolCallId。"""
    message = data.get("message") if isinstance(data, dict) else None
    message = message if isinstance(message, dict) else {}
    source = message.get("source")
    if isinstance(source, dict) and source.get("callId"):
        return js_string(source["callId"])
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("toolCallId"):
                return js_string(block["toolCallId"])
    return ""


def normalize_events(rows):
    """事件流 -> (msgs, meta)；仅保留当前 surface 上的消息/工具活动。"""
    msgs = []
    meta = {"title": "", "model": "", "provider": ""}
    surface_seqs = current_surface_seqs(rows)
    all_result_call_ids = set()
    current_result_call_ids = set()
    for event in rows:
        if not isinstance(event, dict) or event.get("type") != "tool/result":
            continue
        call_id = tool_result_call_id(event.get("data"))
        if not call_id:
            continue
        all_result_call_ids.add(call_id)
        if event.get("seq") in surface_seqs:
            current_result_call_ids.add(call_id)

    for row_index, event in enumerate(rows):
        if not isinstance(event, dict):
            continue
        event_type = event.get("type") or ""
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        ts = ts_to_iso(event.get("time"))

        if event_type == "session/title" and isinstance(data.get("title"), str):
            meta["title"] = data["title"]
            continue
        if event_type == "request/header":
            header = data.get("header")
            config = header.get("config") if isinstance(header, dict) else None
            if isinstance(config, dict):
                if config.get("model"):
                    meta["model"] = js_string(config["model"])
                if config.get("provider"):
                    meta["provider"] = js_string(config["provider"])
            continue
        if event_type == "compaction/summary":
            # 仅当随后紧跟 replace 进面的 user/message 时才是当前摘要，否则已被后续压缩覆盖
            replacement = rows[row_index + 1] if row_index + 1 < len(rows) else None
            replacement_op = (
                replacement.get("surfaceOp") if isinstance(replacement, dict) else None
            )
            is_current = (
                isinstance(replacement, dict)
                and replacement.get("type") == "user/message"
                and isinstance(replacement_op, dict)
                and replacement_op.get("op") == "replace"
                and replacement.get("seq") in surface_seqs
            )
            text = text_of(data.get("summary")).strip() if is_current else ""
            if text:
                msgs.append(cs.m_note(f"历史压缩摘要：{text}", ts))
            continue

        if event_type == "user/message":
            if event.get("seq") in surface_seqs and is_real_user_message(data):
                text = text_of(data.get("content")).strip()
                if text:
                    msgs.append(cs.m_message("user", text, ts))
        elif event_type == "assistant/message":
            if event.get("seq") in surface_seqs:
                message = data.get("message")
                message = message if isinstance(message, dict) else {}
                text = text_of(message.get("content")).strip()
                if text:
                    msgs.append(cs.m_message("assistant", text, ts))
        elif event_type == "tool/call":
            name = js_string(data.get("name") or "")
            call_id = js_string(data.get("callId") or "")
            # 结果已存在但不在当前 surface -> 陈旧调用，跳过以免产生无配对的调用
            if (call_id and call_id in all_result_call_ids
                    and call_id not in current_result_call_ids):
                continue
            msgs.append(cs.m_tool_use(
                call_id, name,
                normalize_input(name, parse_arguments(data.get("arguments"))), ts))
        elif event_type == "tool/result":
            if event.get("seq") in surface_seqs:
                message = data.get("message")
                message = message if isinstance(message, dict) else {}
                call_id = tool_result_call_id(data)
                is_error = bool(data.get("error"))
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("isError"):
                            is_error = True
                msgs.append(cs.m_tool_result(
                    call_id, text_of(message.get("content")), is_error, ts))
    return msgs, meta


def resolve_title(session, meta, msgs):
    """projcache 标题 > session/title 事件 > 首条用户消息首行 > 会话 ID。"""
    projected = projection_value(session.get("projection"), "title")
    if isinstance(projected, str) and projected.strip():
        return projected.strip()
    if meta.get("title"):
        return meta["title"]
    for m in msgs:
        if m["kind"] == "message" and m["role"] == "user" and m.get("text", "").strip():
            line = re.split(r"\r?\n", m["text"].strip())[0]
            return truncate(line, 60) or session["session_id"]
    return session["session_id"]


def truncate(value, maximum):
    text = value if isinstance(value, str) else js_string(value)
    return text if len(text) <= maximum else f"{text[:maximum]}…"


def parse_normalized(session):
    """解压并规范化一个已扫描的会话，返回 (rows, header, msgs, meta)。"""
    rows = read_artifact(session["artifact"], False)["rows"]
    header = rows[0] if rows else None
    if not isinstance(header, dict) or header.get("type") != "session":
        raise cs.ConvertError("会话缺少有效 header")
    msgs, meta = normalize_events(rows[1:])
    return rows, header, msgs, meta


# ---------------------------------------------------------------- 对外接口

def list_sessions(dir_=None, project=None):
    dsh_dir = os.path.abspath(dir_ or default_dir())
    sessions = scan_all_sessions(dsh_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s.get("cwd")) == norm]
    out = []
    for s in sessions[:50]:
        title = (s.get("title") or "").strip()
        if not title:
            try:
                _, _, msgs, meta = parse_normalized(s)
                title = resolve_title(s, meta, msgs)
            except Exception:
                title = ""
        out.append({
            "session_id": s["session_id"],
            "title": title or s["session_id"],
            "cwd": s["cwd"],
            "mtime": s["mtime"] / 1000,
            "path": s["artifact"],
            "count": int(s["count"]) if float(s["count"]).is_integer() else s["count"],
            "last_ts": cs.fmt_cst(ts_to_iso(s["activity_at"])),
        })
    return out


def load_session(dir_=None, project=None, session_id=None):
    dsh_dir = os.path.abspath(dir_ or default_dir())
    sessions = scan_all_sessions(dsh_dir)
    if project:
        norm = norm_path(project)
        sessions = [s for s in sessions if norm_path(s.get("cwd")) == norm]
    if not sessions:
        raise cs.ConvertError(
            f"未找到 DSH 会话（dir={dsh_dir}"
            + (f"，project={project}" if project else "")
            + (f"，session={session_id}" if session_id else "")
            + "）")

    if session_id:
        target = pick_session(sessions, session_id)
        if target is None:
            raise cs.ConvertError(
                f"未找到 DSH 会话（dir={dsh_dir}"
                + (f"，project={project}" if project else "")
                + f"，session={session_id}）")
    else:
        # 默认取最近一个可转换的非空白会话（与 resume_dsh --latest 一致）
        target = next((s for s in sessions
                       if not s.get("blank") and not s.get("scan_error")), None)
        target = target or next(
            (s for s in sessions if not s.get("scan_error")), None) or sessions[0]

    if target.get("scan_error"):
        raise cs.ConvertError(
            f"会话 {target['session_id']} 无法解析：{target['scan_error']}")

    try:
        rows, header, msgs, meta = parse_normalized(target)
    except cs.ConvertError as error:
        raise cs.ConvertError(
            f"会话 {target['session_id']} 解析失败：{error}")

    if not msgs:
        raise cs.ConvertError(f"会话无可转换内容：{target['artifact']}")

    sid = header.get("id") or target["session_id"]
    title = resolve_title(target, meta, msgs)
    cwd = header.get("cwd") or target.get("cwd") or ""

    times = []
    for row in rows:
        t = number_or_zero(row.get("time")) if isinstance(row, dict) else 0
        if t:
            times.append(t)
    started_at = ts_to_iso(header.get("createdAt")) or (ts_to_iso(times[0]) if times else "")
    ended_at = (ts_to_iso(times[-1]) if times else "") or (msgs[-1]["ts"] if msgs else "")
    model = "/".join(v for v in (meta.get("provider"), meta.get("model")) if v)

    ref = cs.make_ref("dsh", sid, title, cwd,
                      model=model, started_at=started_at, ended_at=ended_at,
                      source=target["artifact"])
    return ref, msgs
