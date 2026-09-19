#!/usr/bin/env node
// install_skills: 把本仓库 skills/ 下的 skills 安装到各 AI 编码工具的 skills 目录。
//
// - 默认复制安装全部 skills 到全部支持的工具目录；目标已存在时跳过（不覆盖）
// - --force（--overwrite）覆盖已有目标；--link 改用链接（Windows 为目录联接 junction）
// - 用位置参数或 --skills 指定要安装的 skills；--tool 指定目标工具；--dir 指定任意目标目录
//
// 与 scripts/install_skills.py（Python 版）参数和行为保持一致。
// 需要 Node.js >= 16.7（使用 fs.cpSync）。

import { cpSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, unlinkSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SKILLS_SRC = path.join(REPO_ROOT, 'skills');

// ---------------------------------------------------------------- 工具注册表

function toolTable() {
  const home = homedir();
  const hermesDir = process.platform === 'win32' && process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, 'hermes', 'skills')
    : path.join(home, '.hermes', 'skills');
  return [
    ['claude',    path.join(home, '.claude', 'skills'),            'Claude Code'],
    ['codex',     path.join(home, '.codex', 'skills'),             'Codex CLI'],
    ['copilot',   path.join(home, '.copilot', 'skills'),           'GitHub Copilot CLI'],
    ['cursor',    path.join(home, '.cursor', 'skills'),            'Cursor'],
    ['kimi',      path.join(home, '.kimi-code', 'skills'),         'Kimi Code CLI'],
    ['grok',      path.join(home, '.grok', 'skills'),              'Grok Build CLI'],
    ['dsh',       path.join(home, '.dsh', 'skills'),               'DeepSeek Harness'],
    ['hermes',    hermesDir,                                       'Hermes Agent'],
    ['kilo',      path.join(home, '.kilo', 'skills'),              'Kilo Code'],
    ['minimax',   path.join(home, '.minimax', 'skills'),           'MiniMax Code (mcode)'],
    ['mimo',      path.join(home, '.mimo', 'skills'),              'MiMo-Code'],
    ['opencode',  path.join(home, '.config', 'opencode', 'skills'), 'OpenCode'],
    ['pi',        path.join(home, '.pi', 'agent', 'skills'),       'Pi Coding Agent'],
    ['qoder',     path.join(home, '.qoder', 'skills'),             'Qoder CLI'],
    ['workbuddy', path.join(home, '.workbuddy', 'skills'),         'WorkBuddy'],
    ['zcode',     path.join(home, '.zcode', 'skills'),             'ZCode'],
    ['agents',    path.join(home, '.agents', 'skills'),            '跨工具共享目录 ~/.agents/skills'],
  ];
}

// ---------------------------------------------------------------- 基础工具

function pathExists(p) {
  // lstat 不跟随链接，断开的链接也算存在
  try { lstatSync(p); return true; } catch { return false; }
}

function isLinkLike(p) {
  try { return lstatSync(p).isSymbolicLink(); } catch { return false; }
}

function displayPath(p) {
  const home = homedir();
  if (p.toLowerCase().startsWith(home.toLowerCase())) return '~' + p.slice(home.length);
  return p;
}

function refuseInsideSource(dst) {
  // 防止删除操作穿透链接误删仓库源文件
  let realDst;
  try {
    realDst = realpathSync(dst);
  } catch {
    return;
  }
  const realSrc = realpathSync(SKILLS_SRC);
  if (realDst === realSrc || realDst.startsWith(realSrc + path.sep)) {
    console.error(`错误：${displayPath(dst)} 指向仓库内部（${displayPath(realDst)}），拒绝删除，请手动处理`);
    process.exit(1);
  }
}

function removePath(dst) {
  if (isLinkLike(dst)) {
    try { unlinkSync(dst); return; } catch { /* 落到下面的常规删除 */ }
  }
  const st = statSync(dst);
  if (st.isDirectory()) {
    refuseInsideSource(dst);
    rmSync(dst, { recursive: true });
  } else {
    unlinkSync(dst);
  }
}

function createLink(src, dst) {
  if (process.platform !== 'win32') {
    symlinkSync(src, dst, 'dir');
    return 'symlink';
  }
  // Windows 优先使用目录联接 junction：无需管理员权限或开发者模式
  try {
    symlinkSync(src, dst, 'junction');
    return 'junction';
  } catch (err) {
    const r = spawnSync('cmd', ['/c', 'mklink', '/J', dst, src], { encoding: 'utf8' });
    if (r.status !== 0 || !pathExists(dst)) {
      throw new Error('创建目录联接失败：' + ((r.stderr || r.stdout || err.message || '').trim()));
    }
    return 'junction';
  }
}

function isLinkTo(dst, src) {
  if (!pathExists(dst) || !isLinkLike(dst)) return false;
  try {
    return realpathSync(dst) === realpathSync(src);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- 信息查询

function discoverSkills() {
  const skills = new Map();
  let entries = [];
  try {
    entries = readdirSync(SKILLS_SRC, { withFileTypes: true });
  } catch {
    return skills;
  }
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.isDirectory() && pathExists(path.join(SKILLS_SRC, entry.name, 'SKILL.md'))) {
      skills.set(entry.name, path.join(SKILLS_SRC, entry.name));
    }
  }
  return skills;
}

function readDescription(skillDir) {
  try {
    const text = readFileSync(path.join(skillDir, 'SKILL.md'), 'utf8').slice(0, 2000);
    const m = text.match(/^description:\s*(.+)$/m);
    if (!m) return '';
    const desc = m[1].trim().replace(/^['"]|['"]$/g, '');
    return desc.length <= 60 ? desc : desc.slice(0, 57) + '...';
  } catch {
    return '';
  }
}

function resolveSkillName(token, skills) {
  if (skills.has(token)) return token;
  const alias = 'resume-' + token;
  if (skills.has(alias)) return alias;
  return null;
}

function printSkillList(skills) {
  console.log(`可安装的 skills（共 ${skills.size} 个，来源 ${displayPath(SKILLS_SRC)}）：`);
  for (const [name, dir] of skills) {
    const desc = readDescription(dir);
    console.log(`  ${name.padEnd(20)}${desc ? '- ' + desc : ''}`);
  }
}

function printToolTable(tools, skillsCount) {
  console.log(`支持的目标工具（共 ${tools.length} 个；共可安装 ${skillsCount} 个 skills）：`);
  for (const [name, target, note] of tools) {
    const exists = statSyncNoThrow(target)?.isDirectory() ? '目录已存在' : '尚未创建';
    console.log(`  ${name.padEnd(10)}${displayPath(target).padEnd(44)}${note}（${exists}）`);
  }
}

function statSyncNoThrow(p) {
  try { return statSync(p); } catch { return null; }
}

// ---------------------------------------------------------------- 安装

function installOne(src, dst, useLink, force, dryRun) {
  if (pathExists(dst)) {
    if (isLinkTo(dst, src)) {
      console.log(`  [已链接] ${displayPath(dst)} 已指向本仓库，跳过`);
      return 'already-linked';
    }
    if (!force) {
      const mode = useLink ? '--link 与 ' : '';
      console.log(`  [跳过] ${displayPath(dst)} 已存在（默认不覆盖，${mode}--force 可覆盖）`);
      return 'skip';
    }
    if (dryRun) {
      console.log(`  [预演] 删除并重新安装 ${displayPath(dst)}`);
      return useLink ? 'link' : 'copy';
    }
    try {
      removePath(dst);
    } catch (err) {
      console.log(`  [错误] 无法删除 ${displayPath(dst)}：${err.message}`);
      return 'error';
    }
  }
  if (dryRun) {
    const verb = useLink ? '链接' : '安装';
    console.log(`  [预演] ${verb} ${displayPath(src)} -> ${displayPath(dst)}`);
    return useLink ? 'link' : 'copy';
  }
  try {
    mkdirSync(path.dirname(dst), { recursive: true });
    if (useLink) {
      const kind = createLink(src, dst);
      console.log(`  [链接] ${displayPath(src)} -> ${displayPath(dst)}（${kind}）`);
      return 'link';
    }
    cpSync(src, dst, { recursive: true });
    console.log(`  [安装] ${displayPath(src)} -> ${displayPath(dst)}`);
    return 'copy';
  } catch (err) {
    console.log(`  [错误] ${displayPath(src)} -> ${displayPath(dst)}：${err.message}`);
    return 'error';
  }
}

function runInstall(selectedSkills, selectedTools, useLink, force, dryRun) {
  const counts = { copy: 0, link: 0, skip: 0, 'already-linked': 0, error: 0 };
  let total = 0;
  for (const [toolName, targetDir] of selectedTools) {
    console.log(`\n目标工具 ${toolName}（${displayPath(targetDir)}）：`);
    for (const skillName of selectedSkills) {
      const dst = path.join(targetDir, skillName);
      const action = installOne(path.join(SKILLS_SRC, skillName), dst, useLink, force, dryRun);
      counts[action] += 1;
      total += 1;
    }
  }
  console.log(
    `\n${dryRun ? '预演' : '完成'}：共 ${total} 项` +
    `（安装 ${counts.copy}，链接 ${counts.link}，跳过 ${counts.skip + counts['already-linked']}，错误 ${counts.error}）`
  );
  return counts.error ? 1 : 0;
}

// ---------------------------------------------------------------- 入口

const HELP = `用法:
  node scripts/install_skills.mjs [选项] [skill ...]

把本仓库 skills/ 下的 skills 安装到各 AI 编码工具的 skills 目录
（默认复制安装全部 skills，目标已存在时跳过；--link 改为链接）。

选项:
  -s, --skills <名称,...>   要安装的 skills，逗号分隔，可多次使用
  -t, --tool <名称,...>     目标工具，逗号分隔，可多次使用（all=全部，默认 all）
  -l, --link                用链接代替复制（Windows 下自动使用目录联接 junction，无需管理员）
  -f, --force, --overwrite  目标已存在时先删除再安装（默认跳过）
  -n, --dry-run             演练模式：只显示将要执行的操作，不实际写入
      --dir <目录>          安装到指定目录（覆盖 --tool）
      --list                列出仓库中可安装的 skills
      --list-tools          列出支持的目标工具及 skills 目录
  -h, --help                显示本帮助

skill 名称可用完整目录名（resume-claude）或简写（claude）。

示例:
  node scripts/install_skills.mjs -n                  # 预览全部安装
  node scripts/install_skills.mjs                     # 全部 skills 安装到全部工具
  node scripts/install_skills.mjs resume-claude -t kimi
  node scripts/install_skills.mjs --skills claude,kimi --tool zcode
  node scripts/install_skills.mjs --link -f -t zcode  # 链接方式 + 覆盖
  node scripts/install_skills.mjs --dir .claude/skills resume-claude
  node scripts/install_skills.mjs --list / --list-tools
`;

function splitList(token) {
  return token.split(',').map((s) => s.trim()).filter(Boolean);
}

function parseArgs(argv) {
  const opts = { skills: [], tool: [], link: false, force: false, dryRun: false, dir: null, list: false, listTools: false, help: false, positional: [] };
  const needValue = (i, flag) => {
    if (i + 1 >= argv.length) {
      console.error(`错误：选项 ${flag} 需要一个值`);
      process.exit(1);
    }
    return argv[i + 1];
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const longVal = (name) => (arg.startsWith(`--${name}=`) ? arg.slice(name.length + 3) : null);
    let v;
    if ((v = longVal('tool')) !== null || arg === '-t' || arg === '--tool') {
      opts.tool.push(...splitList(v ?? needValue(i++, arg)));
    } else if ((v = longVal('skills')) !== null || arg === '-s' || arg === '--skills') {
      opts.skills.push(...splitList(v ?? needValue(i++, arg)));
    } else if ((v = longVal('dir')) !== null || arg === '--dir') {
      opts.dir = v ?? needValue(i++, arg);
    } else if (arg === '-l' || arg === '--link') {
      opts.link = true;
    } else if (arg === '-f' || arg === '--force' || arg === '--overwrite') {
      opts.force = true;
    } else if (arg === '-n' || arg === '--dry-run') {
      opts.dryRun = true;
    } else if (arg === '--list') {
      opts.list = true;
    } else if (arg === '--list-tools') {
      opts.listTools = true;
    } else if (arg === '-h' || arg === '--help') {
      opts.help = true;
    } else if (arg.startsWith('-') && arg !== '-') {
      console.error(`错误：未知选项 ${arg}（-h 查看帮助）`);
      process.exit(1);
    } else {
      opts.positional.push(arg);
    }
  }
  return opts;
}

function main() {
  const argv = process.argv.slice(2);
  if (argv.length === 0) {
    // 无参数时按默认行为安装，但先给出简要提示
    console.log('未指定参数：将安装全部 skills 到全部支持的工具（复制模式，已存在则跳过）。加 -n 预览，-h 查看帮助。\n');
  }
  const opts = parseArgs(argv);
  if (opts.help) {
    console.log(HELP);
    return 0;
  }

  const skills = discoverSkills();
  const tools = toolTable();

  if (opts.list) printSkillList(skills);
  if (opts.listTools) {
    if (opts.list) console.log('');
    printToolTable(tools, skills.size);
  }
  if (opts.list || opts.listTools) {
    if (opts.skills.length || opts.positional.length || opts.tool.length || opts.dir) {
      console.log('\n提示：--list / --list-tools 为查询操作，已忽略安装相关参数。');
    }
    return 0;
  }

  if (skills.size === 0) {
    console.error(`错误：未在 ${displayPath(SKILLS_SRC)} 下找到任何包含 SKILL.md 的 skill 目录`);
    return 1;
  }
  if (opts.dir && opts.tool.length) {
    console.error('错误：--dir 与 --tool 不能同时使用');
    return 1;
  }

  // 解析要安装的 skills
  let selectedSkills;
  const requested = [...opts.positional.flatMap(splitList), ...opts.skills];
  if (requested.length) {
    selectedSkills = [];
    const unknown = [];
    for (const token of requested) {
      const name = resolveSkillName(token, skills);
      if (name && !selectedSkills.includes(name)) selectedSkills.push(name);
      else if (!name) unknown.push(token);
    }
    if (unknown.length) {
      console.error(`错误：未知的 skill：${unknown.join('、')}（用 --list 查看可安装列表）`);
      return 1;
    }
  } else {
    selectedSkills = [...skills.keys()];
  }

  // 解析目标工具
  let selectedTools;
  if (opts.dir) {
    selectedTools = [['custom', path.resolve(opts.dir)]];
  } else if (!opts.tool.length || opts.tool.includes('all')) {
    selectedTools = tools.map(([name, dir]) => [name, dir]);
  } else {
    selectedTools = [];
    const unknownTools = [];
    for (const name of opts.tool) {
      const found = tools.find(([n]) => n === name);
      if (!found) unknownTools.push(name);
      else if (!selectedTools.some(([n]) => n === name)) selectedTools.push([found[0], found[1]]);
    }
    if (unknownTools.length) {
      console.error(`错误：未知的目标工具：${unknownTools.join('、')}（用 --list-tools 查看支持列表）`);
      return 1;
    }
  }

  const mode = opts.link ? '链接' : '复制';
  console.log(
    `${opts.dryRun ? '[预演] ' : ''}计划以${mode}方式安装 ${selectedSkills.length} 个 skills` +
    ` 到 ${selectedTools.length} 个目标：${selectedSkills.join('、')}`
  );
  return runInstall(selectedSkills, selectedTools, opts.link, opts.force, opts.dryRun);
}

process.exit(main());
