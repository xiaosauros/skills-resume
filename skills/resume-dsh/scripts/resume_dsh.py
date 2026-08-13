#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""resume-dsh: 独立读取 DeepSeek Harness 的 ~/.dsh 会话并生成接管摘要。

DSH 的压缩日志由多个独立 Zstandard frame 追加组成。本实现不调用 Node.js
或任何 JS 脚本；会话扫描、surface/compaction 重建、状态提取与输出逻辑均
与 Node 实现等价。Windows 中文环境建议：
  python -X utf8 resume_dsh.py ...
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, Callable


ZSTD_MAGIC = 0xFD2FB528
CST_OFFSET_MS = 8 * 60 * 60 * 1000


class ResumeDshError(Exception):
    """可直接展示给用户的解析错误。"""


def die(message: object) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def is_file_safe(file: Path) -> bool:
    try:
        return file.is_file()
    except OSError:
        return False


def is_dir_safe(directory: Path) -> bool:
    try:
        return directory.is_dir()
    except OSError:
        return False


def norm_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def js_string(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def fmt_time(value: object) -> str:
    if value is None or value == "":
        return ""
    text = js_string(value)
    millis: float
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            millis = float(value)
        elif re.fullmatch(r"\d+(?:\.\d+)?", text):
            millis = float(text)
        else:
            iso = text.replace("Z", "+00:00")
            parsed = dt.datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            millis = parsed.timestamp() * 1000
    except (OverflowError, ValueError, TypeError):
        return text
    if not math.isfinite(millis):
        return text
    try:
        parsed = dt.datetime.fromtimestamp(
            (millis + CST_OFFSET_MS) / 1000,
            tz=dt.timezone.utc,
        )
    except (OverflowError, OSError, ValueError):
        return text
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def truncate(value: object, maximum: int) -> str:
    text = js_string(value)
    return text if len(text) <= maximum else f"{text[:maximum]}…"


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def scan_zstd_frames(
    buffer: bytes,
    max_frames: int | None = None,
) -> tuple[list[tuple[int, int]], int | None]:
    frames: list[tuple[int, int]] = []
    offset = 0
    while offset < len(buffer):
        start = offset
        if len(buffer) - offset < 4:
            return frames, start
        if struct.unpack_from("<I", buffer, offset)[0] != ZSTD_MAGIC:
            raise ResumeDshError(
                f"Zstandard 日志在字节 {offset} 处的 frame magic 无效"
            )
        offset += 4
        if offset == len(buffer):
            return frames, start

        descriptor = buffer[offset]
        offset += 1
        if descriptor & 0x18:
            raise ResumeDshError(
                f"Zstandard 日志在字节 {offset - 1} 处使用了保留的 frame header 位"
            )
        content_size_flag = descriptor >> 6
        single_segment = bool(descriptor & 0x20)
        checksum = bool(descriptor & 0x04)
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0)
            if content_size_flag == 0
            else 1 << content_size_flag
        )
        remaining_header = (
            (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        )
        if len(buffer) - offset < remaining_header:
            return frames, start
        offset += remaining_header

        while True:
            if len(buffer) - offset < 3:
                return frames, start
            block_header = int.from_bytes(buffer[offset : offset + 3], "little")
            offset += 3
            last_block = bool(block_header & 1)
            block_type = (block_header >> 1) & 3
            block_size = block_header >> 3
            if block_type == 3:
                raise ResumeDshError(
                    f"Zstandard 日志在字节 {offset - 3} 处使用了保留 block 类型"
                )
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


def get_zstd_decompressor() -> Callable[[bytes], bytes]:
    try:
        from compression import zstd  # type: ignore[attr-defined]

        return zstd.decompress
    except ImportError:
        try:
            import zstandard  # type: ignore[import-not-found]

            decompressor = zstandard.ZstdDecompressor()
            return decompressor.decompress
        except ImportError as error:
            raise ResumeDshError(
                "当前 Python 不支持 Zstandard；请使用 Python 3.14+，"
                "或安装 zstandard 包"
            ) from error


def parse_jsonl(text: str) -> list[Any]:
    rows: list[Any] = []
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
            raise ResumeDshError(
                f"会话日志第 {index + 1} 行不是有效 JSON：{error.msg}"
            ) from error
    return rows


def read_artifact(file: Path, first_frame_only: bool = False) -> dict[str, Any]:
    if str(file).endswith(".zstd"):
        source = file.read_bytes()
        frames, torn_start = scan_zstd_frames(source, 1 if first_frame_only else None)
        if not frames:
            raise ResumeDshError("Zstandard 会话日志没有完整 frame")
        decompress = get_zstd_decompressor()
        chunks: list[bytes] = []
        for start, end in frames:
            try:
                chunks.append(decompress(source[start:end]))
            except Exception as error:
                raise ResumeDshError(
                    f"Zstandard 日志在字节 {start} 处的 frame 解压失败：{error}"
                ) from error
        return {
            "rows": parse_jsonl(b"".join(chunks).decode("utf-8")),
            "tornTail": torn_start is not None,
            "frameCount": len(frames),
        }
    return {
        "rows": parse_jsonl(file.read_text(encoding="utf-8")),
        "tornTail": False,
        "frameCount": 0,
    }


def find_artifact(session_dir: Path) -> Path | None:
    for name in ("session.jsonl.zstd", "session.jsonl"):
        file = session_dir / name
        if is_file_safe(file):
            return file
    return None


def load_projection_cache(dsh_dir: Path) -> dict[str, Any]:
    file = dsh_dir / "storages" / "session_projcache.json"
    if not is_file_safe(file):
        return {}
    try:
        parsed = json.loads(file.read_text(encoding="utf-8"))
        tables = parsed.get("tables") if isinstance(parsed, dict) else None
        sessions = tables.get("sessions") if isinstance(tables, dict) else None
        return sessions if isinstance(sessions, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def projection_value(entry: object, name: str) -> Any:
    if not isinstance(entry, dict):
        return None
    rows = entry.get("rows")
    row = rows.get(name) if isinstance(rows, dict) else None
    return row.get("val") if isinstance(row, dict) and "val" in row else None


def read_header(file: Path, enforce_version: bool = True) -> dict[str, Any]:
    rows = read_artifact(file, True)["rows"]
    header = rows[0] if rows else None
    if (
        not isinstance(header, dict)
        or header.get("type") != "session"
        or not isinstance(header.get("id"), str)
    ):
        raise ResumeDshError("首条记录不是有效的 DSH session header")
    if enforce_version and header.get("version") != 0:
        raise ResumeDshError(
            f"不支持 DSH session format version {header.get('version')}"
            "（当前解析器支持 0）"
        )
    return header


def number_or_zero(value: object) -> float:
    if isinstance(value, bool) or value is None or value == "":
        return 0
    try:
        number = float(value)
        return number if math.isfinite(number) else 0
    except (TypeError, ValueError):
        return 0


def scan_all_sessions(dsh_dir: Path) -> list[dict[str, Any]]:
    root = dsh_dir / "sessions"
    if not is_dir_safe(root):
        return []
    cache = load_projection_cache(dsh_dir)
    sessions: list[dict[str, Any]] = []
    try:
        groups = list(os.scandir(root))
    except OSError:
        return []
    for group in groups:
        if not group.is_dir(follow_symlinks=False):
            continue
        try:
            entries = list(os.scandir(group.path))
        except OSError:
            continue
        for item in entries:
            if not item.is_dir(follow_symlinks=False):
                continue
            session_dir = Path(item.path)
            artifact = find_artifact(session_dir)
            if artifact is None:
                continue
            try:
                header = read_header(artifact, False)
                projected = cache.get(header["id"], cache.get(item.name, {}))
                title = projection_value(projected, "title")
                list_meta = projection_value(projected, "sessionListMetadata") or {}
                stats = projection_value(projected, "sessionStats") or {}
                file_stat = artifact.stat()
                activity_at = (
                    number_or_zero(list_meta.get("lastPromptAt"))
                    if isinstance(list_meta, dict)
                    else 0
                ) or file_stat.st_mtime * 1000 or number_or_zero(header.get("createdAt"))
                sessions.append(
                    {
                        "sessionId": header["id"],
                        "sessionDir": session_dir,
                        "artifact": artifact,
                        "header": header,
                        "projection": projected,
                        "cwd": header.get("cwd") or "",
                        "title": title if isinstance(title, str) else "",
                        "blank": isinstance(list_meta, dict)
                        and list_meta.get("blank") is True,
                        "count": number_or_zero(stats.get("turns"))
                        if isinstance(stats, dict)
                        else 0,
                        "activityAt": activity_at,
                        "mtime": file_stat.st_mtime * 1000,
                        "scanError": None
                        if header.get("version") == 0
                        else (
                            "不支持 DSH session format version "
                            f"{header.get('version')}（当前解析器支持 0）"
                        ),
                    }
                )
            except Exception as error:
                sessions.append(
                    {
                        "sessionId": item.name,
                        "sessionDir": session_dir,
                        "artifact": artifact,
                        "header": {},
                        "projection": cache.get(item.name, {}),
                        "cwd": "",
                        "title": "",
                        "blank": False,
                        "count": 0,
                        "activityAt": 0,
                        "mtime": 0,
                        "scanError": str(error),
                    }
                )
    sessions.sort(key=lambda item: (item["activityAt"], item["mtime"]), reverse=True)
    return sessions


def text_of(value: object, include_reasoning: bool = False) -> str:
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
        "reasoning",
        "thinking",
        "reasoning-chunk",
    }:
        return ""
    if isinstance(value.get("text"), str):
        return value["text"]
    if "content" in value:
        return text_of(value["content"], include_reasoning)
    if "output" in value:
        return text_of(value["output"], include_reasoning)
    return ""


def parse_arguments(value: object) -> object:
    if value is None or value == "":
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(js_string(value))
    except json.JSONDecodeError:
        return {"_raw": js_string(value)}


def normalize_input(name: str, input_value: object) -> object:
    source = input_value if isinstance(input_value, dict) else {}

    def pick(*keys: str) -> object:
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


def is_real_user_message(data: object) -> bool:
    source = data.get("source") if isinstance(data, dict) else None
    return (
        not isinstance(source, dict)
        or not source.get("kind")
        or source.get("kind") == "user"
    )


def is_js_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def current_surface_seqs(rows: list[Any]) -> set[int]:
    surface: list[int] = []
    surface_types = {"user/message", "assistant/message", "tool/result"}
    for event in rows:
        if (
            not isinstance(event, dict)
            or event.get("type") not in surface_types
            or not is_js_integer(event.get("seq"))
        ):
            continue
        seq = event["seq"]
        op = event.get("surfaceOp")
        if not op or op == "append":
            surface.append(seq)
            continue
        if not isinstance(op, dict) or op.get("op") != "replace":
            raise ResumeDshError(f"会话事件 seq {seq} 使用了未知 surfaceOp")
        try:
            start = surface.index(op.get("start"))
            end = surface.index(op.get("end"))
        except ValueError as error:
            raise ResumeDshError(
                f"会话事件 seq {seq} 的 surface replace 范围无效"
            ) from error
        if end < start:
            raise ResumeDshError(f"会话事件 seq {seq} 的 surface replace 范围无效")
        surface[start : end + 1] = [seq]
    return set(surface)


def tool_result_call_id(data: object) -> str:
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


def normalize_events(rows: list[Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "title": "",
        "model": "",
        "provider": "",
        "todos": None,
        "turnEnd": None,
    }
    surface_seqs = current_surface_seqs(rows)
    all_result_call_ids: set[str] = set()
    current_result_call_ids: set[str] = set()
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
        timestamp = event.get("time")
        if timestamp is None:
            timestamp = ""

        if event_type == "session/title" and isinstance(data.get("title"), str):
            meta["title"] = data["title"]
        if event_type == "todo/write" and isinstance(data.get("todos"), list):
            meta["todos"] = data["todos"]
        if event_type == "turn/end":
            meta["turnEnd"] = data.get("reason")
        if event_type == "compaction/summary":
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
                items.append(
                    {
                        "kind": "compact_summary",
                        "timestamp": timestamp,
                        "seq": event.get("seq"),
                        "text": text,
                    }
                )
            continue
        if event_type == "request/header":
            header = data.get("header")
            config = header.get("config") if isinstance(header, dict) else None
            if isinstance(config, dict):
                if config.get("model"):
                    meta["model"] = config["model"]
                if config.get("provider"):
                    meta["provider"] = config["provider"]
            continue

        if event_type == "user/message":
            if event.get("seq") not in surface_seqs or not is_real_user_message(data):
                continue
            text = text_of(data.get("content")).strip()
            if text:
                items.append(
                    {
                        "kind": "user_text",
                        "timestamp": timestamp,
                        "seq": event.get("seq"),
                        "text": text,
                    }
                )
        elif event_type == "assistant/message":
            if event.get("seq") not in surface_seqs:
                continue
            message = data.get("message")
            message = message if isinstance(message, dict) else {}
            text = text_of(message.get("content")).strip()
            if text:
                items.append(
                    {
                        "kind": "assistant_text",
                        "timestamp": timestamp,
                        "seq": event.get("seq"),
                        "text": text,
                    }
                )
        elif event_type == "tool/call":
            name = js_string(data.get("name") or "")
            call_id = js_string(data.get("callId") or "")
            if (
                call_id
                and call_id in all_result_call_ids
                and call_id not in current_result_call_ids
            ):
                continue
            items.append(
                {
                    "kind": "tool_use",
                    "timestamp": timestamp,
                    "seq": event.get("seq"),
                    "name": name,
                    "call_id": call_id,
                    "input": normalize_input(name, parse_arguments(data.get("arguments"))),
                }
            )
        elif event_type == "tool/result":
            if event.get("seq") not in surface_seqs:
                continue
            message = data.get("message")
            message = message if isinstance(message, dict) else {}
            call_id = tool_result_call_id(data)
            is_error = bool(data.get("error"))
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("isError"):
                        is_error = True
            items.append(
                {
                    "kind": "tool_result",
                    "timestamp": timestamp,
                    "seq": event.get("seq"),
                    "call_id": call_id,
                    "content": text_of(message.get("content")),
                    "is_error": is_error,
                }
            )
    return {"items": items, "meta": meta}


def resolve_title(session: dict[str, Any], normalized: dict[str, Any]) -> str:
    projected = projection_value(session.get("projection"), "title")
    if isinstance(projected, str) and projected.strip():
        return projected.strip()
    if normalized["meta"].get("title"):
        return normalized["meta"]["title"]
    for item in normalized["items"]:
        if item.get("kind") == "user_text":
            line = re.split(r"\r?\n", item["text"].strip())[0]
            return truncate(line, 60) or session["sessionId"]
    return session["sessionId"]


def decode_js_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace("\\\\", "\\").replace('\\"', '"')


def strings_after_tool(
    code: str,
    tools: list[str],
    keys: list[str],
    max_distance: int = 1600,
) -> list[str]:
    tool_part = "|".join(re.escape(name) for name in tools)
    key_part = "|".join(re.escape(name) for name in keys)
    pattern = re.compile(
        rf'tools\.(?:{tool_part})\s*\([\s\S]{{0,{max_distance}}}?'
        rf'\b(?:{key_part})\s*:\s*"((?:\\.|[^"\\])*)"'
    )
    return [decode_js_string(match.group(1)) for match in pattern.finditer(code)]


def json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tool_brief(item: dict[str, Any]) -> str:
    input_value = item.get("input")
    input_value = input_value if isinstance(input_value, dict) else {}
    name = item.get("name") or ""
    if name == "run_code":
        return f"{name}: {truncate(input_value.get('description') or input_value.get('code') or '', 180)}"
    if input_value.get("command"):
        return f"{name}: {truncate(input_value['command'], 180)}"
    if input_value.get("path"):
        return f"{name}: {truncate(input_value['path'], 180)}"
    if input_value.get("query"):
        return f"{name}: {truncate(input_value['query'], 180)}"
    return f"{name}: {truncate(json_compact(input_value), 180)}"


RESULT_SIGNAL_RE = re.compile(
    r"(?:exit\s*(?:code)?\s*[:=]?\s*[0-9-]+|error|failed|passed|success|"
    r"成功|失败|通过|finished)",
    re.IGNORECASE,
)
VERIFICATION_CODE_RE = re.compile(
    r"tools\.(?:pwsh|bash|shell|shell_command|exec|job_output)\s*\(|"
    r"\b(?:test|check|build|lint|pytest|cargo|tsc)\b",
    re.IGNORECASE,
)


def build_state(items: list[dict[str, Any]], projection: object) -> dict[str, Any]:
    state: dict[str, Any] = {
        "goal": "",
        "files_read": [],
        "files_edited": [],
        "commands": [],
        "test_results": [],
        "last_user": "",
        "last_assistant": "",
        "history_summary": "",
        "todos": projection_value(projection, "todos"),
    }
    calls: dict[str, dict[str, Any]] = {}
    for item in items:
        kind = item.get("kind")
        if kind == "user_text":
            if not state["goal"]:
                state["goal"] = item["text"]
            state["last_user"] = item["text"]
        elif kind == "assistant_text":
            state["last_assistant"] = item["text"]
        elif kind == "compact_summary":
            state["history_summary"] = item["text"]
            if not state["goal"]:
                state["goal"] = item["text"]
            if RESULT_SIGNAL_RE.search(item["text"]):
                state["test_results"].append(truncate(item["text"], 800))
        elif kind == "tool_use":
            if item.get("call_id"):
                calls[item["call_id"]] = item
            input_value = item.get("input")
            input_value = input_value if isinstance(input_value, dict) else {}
            if item.get("name") == "run_code":
                code = js_string(input_value.get("code") or "")
                state["files_read"].extend(
                    strings_after_tool(
                        code,
                        ["read", "read_file", "view_image", "grep", "glob"],
                        ["file_path", "path"],
                    )
                )
                state["files_edited"].extend(
                    strings_after_tool(
                        code,
                        ["edit", "write", "write_file", "str_replace", "apply_patch"],
                        ["file_path", "path"],
                    )
                )
                state["commands"].extend(
                    strings_after_tool(
                        code,
                        ["pwsh", "bash", "shell", "shell_command", "exec"],
                        ["command"],
                        3000,
                    )
                )
            elif (
                item.get("name")
                in {"read", "read_file", "view_image", "grep", "glob"}
                and input_value.get("path")
            ):
                state["files_read"].append(js_string(input_value["path"]))
            elif (
                item.get("name")
                in {"edit", "write", "write_file", "str_replace", "apply_patch"}
                and input_value.get("path")
            ):
                state["files_edited"].append(js_string(input_value["path"]))
            elif input_value.get("command"):
                state["commands"].append(js_string(input_value["command"]))
        elif kind == "tool_result":
            content = js_string(item.get("content") or "").strip()
            if not content:
                continue
            call = calls.get(item.get("call_id") or "")
            call_input = call.get("input") if isinstance(call, dict) else None
            call_input = call_input if isinstance(call_input, dict) else {}
            direct_command = call_input.get("command")
            code = (
                js_string(call_input.get("code") or "")
                if isinstance(call, dict) and call.get("name") == "run_code"
                else ""
            )
            if (
                item.get("is_error")
                or direct_command
                or VERIFICATION_CODE_RE.search(code)
                or RESULT_SIGNAL_RE.search(content)
            ):
                state["test_results"].append(truncate(content, 800))

    projected_goal = projection_value(projection, "goal")
    if projected_goal:
        if isinstance(projected_goal, str):
            state["goal"] = projected_goal
        elif isinstance(projected_goal, dict) and projected_goal.get("objective"):
            state["goal"] = projected_goal["objective"]
    state["files_read"] = unique(state["files_read"])
    state["files_edited"] = unique(state["files_edited"])
    state["commands"] = unique(state["commands"])
    state["test_results"] = unique(state["test_results"])[-12:]
    return state


def render_item(item: dict[str, Any], max_chars: int) -> str:
    when = "" if item.get("timestamp") == "" else f" ({fmt_time(item.get('timestamp'))})"
    if item.get("kind") == "user_text":
        return f"- 用户{when}: {truncate(item.get('text'), max_chars)}"
    if item.get("kind") == "assistant_text":
        return f"- 助手{when}: {truncate(item.get('text'), max_chars)}"
    if item.get("kind") == "compact_summary":
        return f"- 历史压缩摘要{when}: {truncate(item.get('text'), max_chars)}"
    if item.get("kind") == "tool_use":
        return f"- 工具调用{when}: {tool_brief(item)}"
    status = "错误" if item.get("is_error") else "结果"
    return f"- 工具{status}{when}: {truncate(item.get('content') or '(空)', max_chars)}"


def js_slice_last(values: list[Any], count: int) -> list[Any]:
    return values[:] if count == 0 else values[-count:]


def render_summary(
    session: dict[str, Any],
    normalized: dict[str, Any],
    state: dict[str, Any],
    recent_n: int,
    max_chars: int,
) -> str:
    header = session.get("header") or {}
    items = normalized["items"]
    title = resolve_title(session, normalized)
    event_times = [
        number_or_zero(row.get("time"))
        for row in session["rows"]
        if isinstance(row, dict) and number_or_zero(row.get("time")) != 0
    ]
    first_ts = number_or_zero(header.get("createdAt")) or (event_times[0] if event_times else 0)
    last_ts = event_times[-1] if event_times else first_ts
    model_parts = [
        value
        for value in (normalized["meta"].get("provider"), normalized["meta"].get("model"))
        if value
    ]
    model = "/".join(js_string(value) for value in model_parts) or "(未知)"
    todos = normalized["meta"].get("todos") or state.get("todos")
    recent = js_slice_last(items, recent_n)
    older_end = max(0, len(items) - len(recent))
    older_tools = [
        item for item in items[:older_end] if item.get("kind") == "tool_use"
    ][-60:]
    frame_text = session["frameCount"] if session["frameCount"] else "明文"
    torn_text = "（检测到未完成尾帧，已安全忽略）" if session["tornTail"] else ""
    lines = [
        "# DeepSeek Harness 会话接管摘要",
        "",
        "## 会话信息",
        "",
        f"- 标题: {title}",
        f"- 会话 ID: {session['sessionId']}",
        f"- 项目路径: {header.get('cwd') or session.get('cwd') or '(未知)'}",
        f"- Agent preset: {header.get('agentPreset') or '(未知)'}",
        f"- 模型: {model}",
        f"- 父会话: {header.get('parentSession') or '(无)'}",
        f"- 时间范围: {fmt_time(first_ts)} ~ {fmt_time(last_ts)}",
        f"- 日志: {session['artifact'].name}，{frame_text} frame，"
        f"规范化活动 {len(items)} 条{torn_text}",
        "",
        "## 任务状态重建",
        "",
        "### 目标",
        "",
        truncate(state.get("goal") or "(未能从会话中识别)", max_chars),
        "",
    ]
    if state.get("history_summary"):
        lines.extend(
            [
                "### 历史压缩摘要",
                "",
                truncate(state["history_summary"], max_chars),
                "",
            ]
        )

    def add_list(heading: str, values: list[Any], empty: str = "(无)") -> None:
        lines.extend([f"### {heading}", ""])
        if not values:
            lines.append(f"- {empty}")
        else:
            lines.extend(f"- {truncate(value, max_chars)}" for value in values)
        lines.append("")

    add_list("已调查文件/路径", state["files_read"])
    add_list("已修改文件/路径", state["files_edited"])
    add_list("执行过的命令", state["commands"])
    add_list("测试、命令结果与错误线索", state["test_results"])

    lines.extend(["### TODO / 计划", ""])
    if not isinstance(todos, list) or not todos:
        lines.append("- (会话未留下 TODO)")
    else:
        for todo in todos:
            todo = todo if isinstance(todo, dict) else {}
            mark = "x" if todo.get("status") == "completed" else " "
            lines.append(
                f"- [{mark}] {todo.get('content') or ''} "
                f"({todo.get('status') or 'unknown'})"
            )
    lines.extend(
        [
            "",
            "### 最后状态",
            "",
            f"- 最近用户消息: {truncate(state.get('last_user') or '(无)', max_chars)}",
            f"- 最近助手消息: {truncate(state.get('last_assistant') or '(无)', max_chars)}",
            "",
            f"## 近期活动（最后 {len(recent)} 条）",
            "",
        ]
    )
    if not recent:
        lines.append("- (无可显示活动)")
    else:
        lines.extend(render_item(item, max_chars) for item in recent)
    lines.extend(["", "## 更早工具活动（紧凑）", ""])
    if not older_tools:
        lines.append("- (无)")
    else:
        lines.extend(f"- {tool_brief(item)}" for item in older_tools)
    lines.extend(
        [
            "",
            "## 接管建议",
            "",
            "- 先读取当前 Git 状态与摘要提到的关键文件，确认磁盘现场是否晚于日志。",
            "- 优先处理未完成 TODO、最近错误和最后一条真实用户要求。",
            "- 不要假设原 DSH 的运行中进程仍存在；需要时重新执行验证命令。",
            "- 若日志格式版本与当前解析器不兼容，停止猜测并对照 deepseek-harness 上游实现。",
        ]
    )
    return "\n".join(lines)


def load_session(candidate: dict[str, Any]) -> dict[str, Any]:
    artifact_data = read_artifact(candidate["artifact"], False)
    rows = artifact_data["rows"]
    header = rows[0] if rows else None
    if not isinstance(header, dict) or header.get("type") != "session":
        raise ResumeDshError("会话缺少有效 header")
    result = dict(candidate)
    result.update(
        {
            "header": header,
            "rows": rows[1:],
            "frameCount": artifact_data["frameCount"],
            "tornTail": artifact_data["tornTail"],
            "normalized": normalize_events(rows[1:]),
        }
    )
    return result


def session_title_lite(session: dict[str, Any]) -> str:
    if session.get("title"):
        return session["title"]
    projected = projection_value(session.get("projection"), "title")
    return projected if isinstance(projected, str) and projected else session["sessionId"]


def pick_session(
    sessions: list[dict[str, Any]],
    value: str | None,
    prefer_non_blank: bool = True,
) -> dict[str, Any] | None:
    if not sessions:
        return None
    if not value:
        if prefer_non_blank:
            for session in sessions:
                if not session.get("blank") and not session.get("scanError"):
                    return session
        for session in sessions:
            if not session.get("scanError"):
                return session
        return sessions[0]
    query = value.lower()
    for session in sessions:
        if session["sessionId"].lower().startswith(query):
            return session
    for session in sessions:
        if query in session["sessionId"].lower():
            return session
    for session in sessions:
        if query in session_title_lite(session).lower():
            return session
    return None


def json_output(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def write_utf8_file(file: str, content: str) -> None:
    with open(file, "w", encoding="utf-8", newline="") as output:
        output.write(content)


def list_output(
    sessions: list[dict[str, Any]],
    project: str,
    dsh_dir: Path,
    limit: int,
    as_json: bool,
) -> str:
    shown = sessions[:limit] if limit > 0 else sessions
    if as_json:
        return json_output(
            {
                "project": project,
                "sessions": [
                    {
                        "session_id": session["sessionId"],
                        "title": session_title_lite(session),
                        "cwd": session["cwd"],
                        "last_ts": fmt_time(session["activityAt"]),
                        "turns": int(session["count"])
                        if float(session["count"]).is_integer()
                        else session["count"],
                        "blank": session["blank"],
                        "error": session.get("scanError") or None,
                    }
                    for session in shown
                ],
            }
        )
    limited = f"（显示最近 {len(shown)} 个）" if len(shown) < len(sessions) else ""
    lines = [
        f"当前项目: {project}",
        f"会话根: {dsh_dir / 'sessions'}",
        f"找到 {len(sessions)} 个会话{limited}：",
        "",
    ]
    for index, session in enumerate(shown):
        mark = "[最近]" if index == 0 else "      "
        blank = " [空白]" if session.get("blank") else ""
        broken = (
            f" [解析失败: {session['scanError']}]" if session.get("scanError") else ""
        )
        count = int(session["count"]) if float(session["count"]).is_integer() else session["count"]
        lines.append(
            f"{mark} {fmt_time(session['activityAt']) or '(无时间)'}  "
            f"{session['sessionId'][:28].ljust(28)}  回合:{str(count).ljust(3)} "
            f"标题: {session_title_lite(session)}{blank}{broken}"
        )
    return "\n".join(lines)


HELP = """用法: python -X utf8 resume_dsh.py [选项]

读取 DeepSeek Harness 本地 session.jsonl.zstd / session.jsonl，生成结构化接管摘要。

选项:
  --list              列出当前项目的会话
  --latest            取最近一个非空会话（默认）
  --session VALUE     指定 ID、ID 前缀或标题关键词（支持跨项目）
  --project PATH      项目路径，默认当前工作目录
  --dsh-dir DIR       DSH 数据目录，默认 ~/.dsh 或 $DSH_HOME
  --recent N          近期活动条目数，默认 10
  --max-chars N       单条内容截断长度，默认 1800
  --limit N           --list 数量上限，0 不限制
  --json              JSON 输出
  --output FILE       写入 UTF-8 文件
  -h, --help          显示帮助"""


def parse_args(argv: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "list": False,
        "latest": False,
        "session": None,
        "project": None,
        "dshDir": None,
        "recent": 10,
        "maxChars": 1800,
        "limit": 0,
        "json": False,
        "output": None,
        "help": False,
    }

    def need_value(flag: str, index: int) -> str:
        if index + 1 >= len(argv):
            die(f"{flag} 需要参数")
        return argv[index + 1]

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--list":
            args["list"] = True
        elif arg == "--latest":
            args["latest"] = True
        elif arg == "--json":
            args["json"] = True
        elif arg in {"-h", "--help"}:
            args["help"] = True
        elif arg == "--session":
            args["session"] = need_value(arg, index)
            index += 1
        elif arg.startswith("--session="):
            args["session"] = arg[10:]
        elif arg == "--project":
            args["project"] = need_value(arg, index)
            index += 1
        elif arg.startswith("--project="):
            args["project"] = arg[10:]
        elif arg == "--dsh-dir":
            args["dshDir"] = need_value(arg, index)
            index += 1
        elif arg.startswith("--dsh-dir="):
            args["dshDir"] = arg[10:]
        elif arg == "--recent":
            args["recent"] = parse_integer(arg, need_value(arg, index))
            index += 1
        elif arg.startswith("--recent="):
            args["recent"] = parse_integer("--recent", arg[9:])
        elif arg == "--max-chars":
            args["maxChars"] = parse_integer(arg, need_value(arg, index))
            index += 1
        elif arg.startswith("--max-chars="):
            args["maxChars"] = parse_integer("--max-chars", arg[12:])
        elif arg == "--limit":
            args["limit"] = parse_integer(arg, need_value(arg, index))
            index += 1
        elif arg.startswith("--limit="):
            args["limit"] = parse_integer("--limit", arg[8:])
        elif arg == "--output":
            args["output"] = need_value(arg, index)
            index += 1
        elif arg.startswith("--output="):
            args["output"] = arg[9:]
        else:
            die(f"未知参数 '{arg}'，使用 --help 查看用法")
        index += 1
    for name, number in (
        ("--recent", args["recent"]),
        ("--max-chars", args["maxChars"]),
        ("--limit", args["limit"]),
    ):
        if number < 0:
            die(f"{name} 必须是非负整数")
    if args["maxChars"] == 0:
        die("--max-chars 必须大于 0")
    return args


def parse_integer(flag: str, value: str) -> int:
    if not re.fullmatch(r"[+-]?\d+", value):
        die(f"{flag} 必须是非负整数")
    return int(value)


def resolve_path(value: str | None, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default.resolve()


def main() -> None:
    args = parse_args(sys.argv[1:])
    if args["help"]:
        print(HELP)
        return
    dsh_dir = resolve_path(
        args["dshDir"] or os.environ.get("DSH_HOME"),
        Path.home() / ".dsh",
    )
    project_path = resolve_path(args["project"], Path.cwd())
    project = str(project_path)
    all_sessions = scan_all_sessions(dsh_dir)
    if not all_sessions:
        die(f"在 {dsh_dir / 'sessions'} 下未找到 DSH 会话")

    candidates = [
        session
        for session in all_sessions
        if norm_path(session.get("cwd")) == norm_path(project)
    ]
    target: dict[str, Any] | None = None
    if args["session"]:
        target = pick_session(all_sessions, args["session"], False)
        if target is None:
            die(f"未匹配到会话 '{args['session']}'")
        if not args["list"]:
            candidates = all_sessions

    if args["list"]:
        if args["session"]:
            candidates = all_sessions
        if not candidates:
            die(f"未找到项目 {project} 对应的 DSH 会话")
        output = list_output(
            candidates,
            "(全部项目)" if args["session"] else project,
            dsh_dir,
            args["limit"],
            args["json"],
        )
        if args["output"]:
            write_utf8_file(args["output"], output)
        else:
            print(output)
        return

    if target is None:
        if not candidates:
            die(f"未找到项目 {project} 对应的 DSH 会话；可用 --session ID 跨项目选择")
        target = pick_session(candidates, None, True)
    if target is None:
        die("未找到可接管的 DSH 会话")
    if target.get("scanError"):
        die(f"会话 {target['sessionId']} 无法解析：{target['scanError']}")

    session = load_session(target)
    normalized = session["normalized"]
    state = build_state(normalized["items"], session.get("projection"))
    if args["json"]:
        times = [
            number_or_zero(row.get("time"))
            for row in session["rows"]
            if isinstance(row, dict) and number_or_zero(row.get("time")) != 0
        ]
        header = session["header"]
        output = json_output(
            {
                "info": {
                    "session_id": session["sessionId"],
                    "title": resolve_title(session, normalized),
                    "cwd": header.get("cwd") or "",
                    "agent_preset": header.get("agentPreset") or "",
                    "parent_session_id": header.get("parentSession") or "",
                    "model": normalized["meta"].get("model") or "",
                    "provider": normalized["meta"].get("provider") or "",
                    "first_ts": fmt_time(header.get("createdAt")),
                    "last_ts": fmt_time(
                        times[-1] if times else header.get("createdAt")
                    ),
                    "artifact": str(session["artifact"]),
                    "frame_count": session["frameCount"],
                    "torn_tail": session["tornTail"],
                },
                "state": state,
                "recent_items": js_slice_last(normalized["items"], args["recent"]),
            }
        )
    else:
        output = render_summary(
            session,
            normalized,
            state,
            args["recent"],
            args["maxChars"],
        )
    if args["output"]:
        write_utf8_file(args["output"], output)
        print(f"摘要已写入：{args['output']}", file=sys.stderr)
    else:
        sys.stdout.write(output if output.endswith("\n") else f"{output}\n")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        die(error)
