#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_to_claude: 把其他 AI 编码工具的本地会话转换成 Claude Code 原生 JSONL 会话文件，
写入 ~/.claude/projects/<项目>/<sessionId>.jsonl，从而可用原生 /resume 恢复完整对话。

支持的工具由 adapters/ 下的适配器决定（每个工具一个适配器，共用 claude_session 工具类）。
存储不可行或格式无法无损映射的工具不会提供适配器。

用法：
  列出工具与会话：
    python export_to_claude.py --list [--tool codex] [--limit N]
  转换（默认写入 ~/.claude/projects，可直接 /resume）：
    python export_to_claude.py --tool codex [--session ID] [--project PATH]
        [--to-project PATH] [--out-dir DIR | --out-file FILE] [--session-id NEW]
        [--dry-run] [--degrade-tools] [--json]

与同目录 export_to_claude.js 功能等价、参数一致。
"""

import argparse
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_session as cs  # noqa: E402

# ---------------------------------------------------------------- 适配器注册表
# （tool 名与 adapters/<tool>.py 一一对应；storage 仅用于展示）

TOOL_REGISTRY = [
    {"tool": "codex", "storage": "JSONL", "desc": "Codex CLI（~/.codex）"},
    {"tool": "copilot", "storage": "JSONL+YAML", "desc": "GitHub Copilot CLI（~/.copilot）"},
    {"tool": "dsh", "storage": "zstd+JSONL", "desc": "DeepSeek Harness（~/.dsh）"},
    {"tool": "grok", "storage": "JSONL+JSON", "desc": "Grok Build CLI（~/.grok）"},
    {"tool": "kimi", "storage": "JSONL", "desc": "Kimi Code CLI（~/.kimi-code）"},
    {"tool": "qoder", "storage": "JSONL", "desc": "Qoder CLI（~/.qoder）"},
    {"tool": "zcode", "storage": "SQLite", "desc": "ZCode（~/.zcode）"},
    {"tool": "agy", "storage": "JSONL", "desc": "Antigravity CLI（~/.gemini/antigravity）"},
    {"tool": "continue", "storage": "JSON", "desc": "Continue（~/.continue）"},
    {"tool": "cursor", "storage": "SQLite", "desc": "Cursor IDE（state.vscdb）"},
    {"tool": "hermes", "storage": "SQLite", "desc": "Hermes Agent（state.db）"},
    {"tool": "kilo", "storage": "SQLite+JSON", "desc": "Kilo Code（kilo.db）"},
    {"tool": "mimo", "storage": "SQLite", "desc": "MiMo-Code（mimocode.db）"},
    {"tool": "minimax", "storage": "SQLite+JSONL", "desc": "MiniMax Code（~/.minimax）"},
    {"tool": "openclaw", "storage": "SQLite+JSON", "desc": "OpenClaw（~/.openclaw）"},
    {"tool": "opencode", "storage": "SQLite+JSON", "desc": "OpenCode（opencode.db）"},
    {"tool": "pi", "storage": "JSONL", "desc": "Pi Coding Agent（~/.pi）"},
    {"tool": "workbuddy", "storage": "JSONL", "desc": "WorkBuddy（~/.workbuddy）"},
]


def load_adapter(tool):
    """加载 adapters/<tool>.py；文件缺失说明该工具不可行/不支持。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "adapters", f"{tool}.py")
    if not os.path.isfile(path):
        raise cs.ConvertError(f"工具 {tool} 没有可用适配器（不支持或未实现）")
    spec = importlib.util.spec_from_file_location(f"_conv_adapter_{tool}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def known_tools():
    return [e["tool"] for e in TOOL_REGISTRY]


# ---------------------------------------------------------------- 列表

def cmd_list(args):
    tools = [t for t in known_tools() if not args.tool or t in args.tool]
    unknown = [t for t in (args.tool or []) if t not in known_tools()]
    if unknown:
        print(f"错误：未知工具：{'、'.join(unknown)}", file=sys.stderr)
        return 1
    report = []
    for name in tools:
        entry = next(e for e in TOOL_REGISTRY if e["tool"] == name)
        item = {"tool": name, "storage": entry["storage"], "desc": entry["desc"],
                "available": False, "sessions": []}
        try:
            mod = load_adapter(name)
            sessions = mod.list_sessions(dir_=args.dir, project=None)
            item["available"] = True
            sessions = sessions[:args.limit] if args.limit > 0 else sessions
            for s in sessions:
                item["sessions"].append({
                    "session_id": s.get("session_id", ""),
                    "title": s.get("title", ""),
                    "cwd": s.get("cwd", ""),
                    "last_ts": s.get("last_ts", "") or s.get("last_time", ""),
                    "count": s.get("count", 0),
                })
        except cs.ConvertError as e:
            item["error"] = str(e)
        except Exception as e:  # 适配器自身异常不阻塞其他工具
            item["error"] = f"适配器异常: {e}"
        report.append(item)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for item in report:
        mark = "可用" if item["available"] else "不可用"
        err = f"（{item['error']}）" if item.get("error") else ""
        print(f"[{mark}] {item['tool']:<10} {item['storage']:<12} {item['desc']}{err}")
        for s in item["sessions"]:
            print(f"    {s.get('last_ts') or '(无时间)':<20} {s['session_id'][:12]:<14} "
                  f"消息数:{s['count']:<5} {s['title'][:50]}")
    return 0


# ---------------------------------------------------------------- 转换

def cmd_convert(args):
    if not args.tool:
        print("错误：必须用 --tool 指定来源工具（--list 可查看支持列表）", file=sys.stderr)
        return 1
    if args.tool not in known_tools():
        print(f"错误：未知工具：{args.tool}", file=sys.stderr)
        return 1

    mod = load_adapter(args.tool)
    ref, msgs = mod.load_session(dir_=args.dir, project=args.project,
                                 session_id=args.session)
    if not msgs:
        print("错误：会话内容为空，未转换", file=sys.stderr)
        return 1

    result = cs.write_session(
        ref, msgs,
        claude_dir=args.claude_dir,
        project_override=args.to_project,
        out_dir=args.out_dir,
        out_file=args.out_file,
        session_id=args.session_id,
        degrade_tools=args.degrade_tools,
        title_prefix="" if args.no_prefix else f"[{ref.get('tool') or args.tool}] ",
        max_tool_output=args.max_tool_output,
        max_text=args.max_text,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "预演完成" if result["dry_run"] else (
            "转换成功" if result["ok"] else "转换失败")
        print(f"{status}：{result['title']}")
        print(f"  会话ID: {result['session_id']}")
        print(f"  记录数: {result['records']}")
        print(f"  目标文件: {result['path']}")
        if result["dry_run"]:
            print("  （dry-run 模式，未实际写入）")
        for w in result["warnings"]:
            print(f"  警告: {w}")
        for e in result["errors"]:
            print(f"  错误: {e}")
        if result["ok"] and not result["dry_run"]:
            print("  现在可在对应项目目录用 claude 后执行 /resume 恢复该会话。")
    return 0 if result["ok"] else 1


def build_parser():
    ap = argparse.ArgumentParser(
        prog="export_to_claude",
        description="把其他 AI 编码工具的本地会话转换成 Claude Code 原生 JSONL，"
                    "可用原生 /resume 恢复完整对话。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python export_to_claude.py --list                       # 所有工具的可用会话\n"
            "  python export_to_claude.py --list --tool codex          # 单工具的会话列表\n"
            "  python export_to_claude.py --tool codex                 # 最新会话 -> ~/.claude/projects\n"
            "  python export_to_claude.py --tool codex --session 019e8 --dry-run\n"
            "  python export_to_claude.py --tool zcode --to-project D:\\work\\repo\n"
            "  python export_to_claude.py --tool dsh --out-dir ./converted\n"
        ),
    )
    ap.add_argument("--list", action="store_true", help="列出工具与可用会话")
    ap.add_argument("--tool", default=None, help="来源工具（--list 可省略；可逗号分隔配合 --list）")
    ap.add_argument("--session", default=None, help="来源会话 ID 或前缀（默认取最新）")
    ap.add_argument("--project", default=None, help="按项目路径过滤来源会话")
    ap.add_argument("--dir", default=None, help="来源工具数据目录覆盖（默认各工具自动探测）")
    ap.add_argument("--claude-dir", default=None, help="Claude 配置目录，默认 ~/.claude")
    ap.add_argument("--to-project", default=None, help="目标项目路径（默认用会话原 cwd）")
    ap.add_argument("--out-dir", default=None, help="输出目录覆盖（测试用，不写 ~/.claude）")
    ap.add_argument("--out-file", default=None, help="输出文件路径覆盖（优先级最高）")
    ap.add_argument("--session-id", default=None, help="指定新会话 ID（默认随机 UUID）")
    ap.add_argument("--dry-run", action="store_true", help="只构建与校验，不实际写入")
    ap.add_argument("--degrade-tools", action="store_true",
                    help="把工具调用/结果降级为纯文本（最大兼容模式）")
    ap.add_argument("--no-prefix", action="store_true", help="标题不加来源工具前缀")
    ap.add_argument("--max-tool-output", type=int, default=cs.DEFAULT_MAX_TOOL_OUTPUT,
                    help="工具结果截断长度，默认 20000")
    ap.add_argument("--max-text", type=int, default=cs.DEFAULT_MAX_TEXT,
                    help="普通文本截断长度，默认 200000")
    ap.add_argument("--limit", type=int, default=20, help="--list 每工具显示条数上限")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    return ap


def main(argv=None):
    cs.setup_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.tool and not args.list:
        args.tool = args.tool.strip().lower()
    try:
        if args.list:
            tools = []
            for t in (args.tool or "").split(","):
                t = t.strip()
                if t:
                    tools.append(t)
            args.tool = tools or None
            return cmd_list(args)
        return cmd_convert(args)
    except cs.ConvertError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
