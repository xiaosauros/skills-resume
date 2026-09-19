#!/usr/bin/env python3
"""install_skills: 把本仓库 skills/ 下的 skills 安装到各 AI 编码工具的 skills 目录。

- 默认复制安装全部 skills 到全部支持的工具目录；目标已存在时跳过（不覆盖）
- --force（--overwrite）覆盖已有目标；--link 改用链接（Windows 为目录联接 junction）
- 用位置参数或 --skills 指定要安装的 skills；--tool 指定目标工具；--dir 指定任意目标目录

与 scripts/install_skills.mjs（Node.js 版）参数和行为保持一致。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_SRC = REPO_ROOT / "skills"

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


# ---------------------------------------------------------------- 工具注册表

def tool_table():
    """目标工具注册表：(名称, 用户级 skills 目录, 说明)。目录约定以各工具官方文档为准。"""
    home = Path.home()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local_app_data:
        hermes_dir = Path(local_app_data) / "hermes" / "skills"
    else:
        hermes_dir = home / ".hermes" / "skills"
    return [
        ("claude",    home / ".claude" / "skills",            "Claude Code"),
        ("codex",     home / ".codex" / "skills",             "Codex CLI"),
        ("copilot",   home / ".copilot" / "skills",           "GitHub Copilot CLI"),
        ("cursor",    home / ".cursor" / "skills",            "Cursor"),
        ("kimi",      home / ".kimi-code" / "skills",         "Kimi Code CLI"),
        ("grok",      home / ".grok" / "skills",              "Grok Build CLI"),
        ("dsh",       home / ".dsh" / "skills",               "DeepSeek Harness"),
        ("hermes",    hermes_dir,                             "Hermes Agent"),
        ("kilo",      home / ".kilo" / "skills",              "Kilo Code"),
        ("minimax",   home / ".minimax" / "skills",           "MiniMax Code (mcode)"),
        ("mimo",      home / ".mimo" / "skills",              "MiMo-Code"),
        ("opencode",  home / ".config" / "opencode" / "skills", "OpenCode"),
        ("pi",        home / ".pi" / "agent" / "skills",      "Pi Coding Agent"),
        ("qoder",     home / ".qoder" / "skills",             "Qoder CLI"),
        ("workbuddy", home / ".workbuddy" / "skills",         "WorkBuddy"),
        ("zcode",     home / ".zcode" / "skills",             "ZCode"),
        ("agents",    home / ".agents" / "skills",            "跨工具共享目录 ~/.agents/skills"),
    ]


# ---------------------------------------------------------------- 基础工具

def is_junction(p: Path) -> bool:
    """Windows 目录联接判断（os.path.isjunction 需要 Python 3.12+，此处做兼容）。"""
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(p)
    if os.name != "nt":
        return False
    try:
        st = os.stat(p, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_link_like(p: Path) -> bool:
    return os.path.islink(p) or is_junction(p)


def path_exists(p: Path) -> bool:
    # os.path.exists 对断开的链接返回 False，这里需要把链接本身也算作存在
    return os.path.lexists(p)


def display_path(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    if s.lower().startswith(home.lower()):
        return "~" + s[len(home):]
    return s


def refuse_inside_source(dst: Path) -> None:
    """防止删除操作穿透链接误删仓库源文件。"""
    try:
        real_dst = Path(os.path.realpath(dst)).resolve()
        real_src = SKILLS_SRC.resolve()
        if str(real_dst).lower().startswith(str(real_src).lower() + os.sep):
            raise SystemExit(
                f"错误：{display_path(dst)} 指向仓库内部（{display_path(real_dst)}），"
                f"拒绝删除，请手动处理"
            )
    except OSError:
        pass


def remove_path(dst: Path) -> None:
    if is_link_like(dst):
        try:
            os.unlink(dst)  # Python 3.8+ 的 os.unlink 可直接移除目录联接
            return
        except OSError:
            pass
    if dst.is_dir():
        refuse_inside_source(dst)
        shutil.rmtree(dst)
    else:
        dst.unlink()


def create_link(src: Path, dst: Path) -> str:
    """创建指向 src 的链接，返回链接类型描述。"""
    if os.name != "nt":
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    except OSError:
        pass
    try:
        import _winapi

        _winapi.CreateJunction(str(src), str(dst))
        return "junction"
    except Exception:
        pass
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not path_exists(dst):
        raise OSError("创建目录联接失败：" + (result.stderr or result.stdout).strip())
    return "junction"


def is_link_to(dst: Path, src: Path) -> bool:
    if not (path_exists(dst) and is_link_like(dst)):
        return False
    try:
        return os.path.realpath(dst) == os.path.realpath(src)
    except OSError:
        return False


# ---------------------------------------------------------------- 信息查询

def discover_skills() -> dict:
    skills = {}
    if not SKILLS_SRC.is_dir():
        return skills
    for entry in sorted(SKILLS_SRC.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            skills[entry.name] = entry
    return skills


def read_description(skill_dir: Path) -> str:
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")[:2000]
    except OSError:
        return ""
    match = re.search(r"^description:\s*(.+)$", text, re.M)
    if not match:
        return ""
    desc = match.group(1).strip().strip("'\"")
    return desc if len(desc) <= 60 else desc[:57] + "..."


def resolve_skill_name(token: str, skills: dict):
    if token in skills:
        return token
    alias = "resume-" + token
    if alias in skills:
        return alias
    return None


def print_skill_list(skills: dict) -> None:
    print(f"可安装的 skills（共 {len(skills)} 个，来源 {display_path(SKILLS_SRC)}）：")
    for name, path in skills.items():
        desc = read_description(path)
        print(f"  {name:<20}{('- ' + desc) if desc else ''}")


def print_tool_table(tools, skills_count: int) -> None:
    print(f"支持的目标工具（共 {len(tools)} 个；共可安装 {skills_count} 个 skills）：")
    for name, target, note in tools:
        exists = "目录已存在" if target.is_dir() else "尚未创建"
        print(f"  {name:<10}{display_path(target):<44}{note}（{exists}）")


# ---------------------------------------------------------------- 安装

def install_one(src: Path, dst: Path, use_link: bool, force: bool, dry_run: bool) -> str:
    """安装单个 skill，返回动作：copy / link / skip / already-linked / error。"""
    if path_exists(dst):
        if is_link_to(dst, src):
            print(f"  [已链接] {display_path(dst)} 已指向本仓库，跳过")
            return "already-linked"
        if not force:
            mode = "--link 与 " if use_link else ""
            print(f"  [跳过] {display_path(dst)} 已存在（默认不覆盖，{mode}--force 可覆盖）")
            return "skip"
        if dry_run:
            print(f"  [预演] 删除并重新安装 {display_path(dst)}")
            return "copy" if not use_link else "link"
        try:
            remove_path(dst)
        except OSError as exc:
            print(f"  [错误] 无法删除 {display_path(dst)}：{exc}")
            return "error"
    if dry_run:
        verb = "链接" if use_link else "安装"
        print(f"  [预演] {verb} {display_path(src)} -> {display_path(dst)}")
        return "copy" if not use_link else "link"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if use_link:
            kind = create_link(src, dst)
            print(f"  [链接] {display_path(src)} -> {display_path(dst)}（{kind}）")
            return "link"
        shutil.copytree(src, dst)
        print(f"  [安装] {display_path(src)} -> {display_path(dst)}")
        return "copy"
    except OSError as exc:
        print(f"  [错误] {display_path(src)} -> {display_path(dst)}：{exc}")
        return "error"


def run_install(selected_skills, selected_tools, use_link: bool, force: bool, dry_run: bool) -> int:
    counts = {"copy": 0, "link": 0, "skip": 0, "already-linked": 0, "error": 0}
    total = 0
    for tool_name, target_dir, _ in selected_tools:
        print(f"\n目标工具 {tool_name}（{display_path(target_dir)}）：")
        for skill_name in selected_skills:
            dst = target_dir / skill_name
            action = install_one(SKILLS_SRC / skill_name, dst, use_link, force, dry_run)
            counts[action] += 1
            total += 1
    print(
        f"\n{'预演' if dry_run else '完成'}：共 {total} 项"
        f"（安装 {counts['copy']}，链接 {counts['link']}，"
        f"跳过 {counts['skip'] + counts['already-linked']}，错误 {counts['error']}）"
    )
    return 1 if counts["error"] else 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_skills",
        description="把本仓库 skills/ 下的 skills 安装到各 AI 编码工具的 skills 目录"
                    "（默认复制安装全部 skills，目标已存在时跳过；--link 改为链接）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/install_skills.py -n                  # 预览全部安装\n"
            "  python scripts/install_skills.py                     # 全部 skills 安装到全部工具\n"
            "  python scripts/install_skills.py resume-claude -t kimi\n"
            "  python scripts/install_skills.py --skills claude,kimi --tool zcode\n"
            "  python scripts/install_skills.py --link -f -t zcode  # 链接方式 + 覆盖\n"
            "  python scripts/install_skills.py --dir .claude/skills resume-claude\n"
            "  python scripts/install_skills.py --list / --list-tools\n"
        ),
    )
    parser.add_argument("skills_pos", nargs="*", metavar="skill",
                        help="要安装的 skills（名称如 resume-claude 或简写 claude）")
    parser.add_argument("-s", "--skills", action="append", default=[], metavar="名称",
                        help="要安装的 skills，逗号分隔，可多次使用")
    parser.add_argument("-t", "--tool", action="append", default=[], metavar="名称",
                        help="目标工具，逗号分隔，可多次使用（all=全部，默认 all）")
    parser.add_argument("-l", "--link", action="store_true",
                        help="用链接代替复制（Windows 下自动使用目录联接 junction，无需管理员）")
    parser.add_argument("-f", "--force", "--overwrite", action="store_true",
                        help="目标已存在时先删除再安装（默认跳过）")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="演练模式：只显示将要执行的操作，不实际写入")
    parser.add_argument("--dir", default=None, metavar="目录",
                        help="安装到指定目录（覆盖 --tool）")
    parser.add_argument("--list", action="store_true", help="列出仓库中可安装的 skills")
    parser.add_argument("--list-tools", action="store_true", help="列出支持的目标工具及 skills 目录")
    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    skills = discover_skills()
    tools = tool_table()

    if args.list:
        print_skill_list(skills)
    if args.list_tools:
        if args.list:
            print()
        print_tool_table(tools, len(skills))
    if args.list or args.list_tools:
        if args.skills or args.skills_pos or args.tool or args.dir:
            print("\n提示：--list / --list-tools 为查询操作，已忽略安装相关参数。")
        return 0

    if not skills:
        print(f"错误：未在 {display_path(SKILLS_SRC)} 下找到任何包含 SKILL.md 的 skill 目录", file=sys.stderr)
        return 1
    if args.dir and args.tool:
        print("错误：--dir 与 --tool 不能同时使用", file=sys.stderr)
        return 1

    # 解析要安装的 skills
    requested = []
    for token in args.skills_pos + args.skills:
        requested.extend(t.strip() for t in token.split(",") if t.strip())
    if requested:
        selected_skills, unknown = [], []
        for token in requested:
            name = resolve_skill_name(token, skills)
            if name and name not in selected_skills:
                selected_skills.append(name)
            elif not name:
                unknown.append(token)
        if unknown:
            print(f"错误：未知的 skill：{'、'.join(unknown)}（用 --list 查看可安装列表）", file=sys.stderr)
            return 1
    else:
        selected_skills = list(skills)

    # 解析目标工具
    if args.dir:
        selected_tools = [("custom", Path(args.dir), "指定目录")]
    else:
        tool_names = []
        for token in args.tool:
            tool_names.extend(t.strip() for t in token.split(",") if t.strip())
        if not tool_names or tool_names == ["all"]:
            selected_tools = tools
        else:
            known = {name: (name, d, n) for name, d, n in tools}
            selected_tools, unknown_tools = [], []
            for name in tool_names:
                if name in known:
                    if name not in [t[0] for t in selected_tools]:
                        selected_tools.append(known[name])
                else:
                    unknown_tools.append(name)
            if unknown_tools:
                print(f"错误：未知的目标工具：{'、'.join(unknown_tools)}（用 --list-tools 查看支持列表）",
                      file=sys.stderr)
                return 1

    mode = "链接" if args.link else "复制"
    print(
        f"{'[预演] ' if args.dry_run else ''}计划以{mode}方式安装 {len(selected_skills)} 个 skills"
        f" 到 {len(selected_tools)} 个目标：{('、'.join(s for s in selected_skills))}"
    )
    return run_install(selected_skills, selected_tools, args.link, args.force, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
