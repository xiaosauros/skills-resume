#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * export_to_claude: 把其他 AI 编码工具的本地会话转换成 Claude Code 原生 JSONL 会话文件，
 * 写入 ~/.claude/projects/<项目>/<sessionId>.jsonl，从而可用原生 /resume 恢复完整对话。
 *
 * 支持的工具由 adapters/ 下的适配器决定（每个工具一个适配器，共用 claude_session 工具类）。
 * 存储不可行或格式无法无损映射的工具不会提供适配器。
 *
 * 零第三方依赖：node export_to_claude.js --tool codex [--session ID] [--dry-run] ...
 * 与同目录 export_to_claude.py 功能等价、参数一致。
 */

'use strict';

const path = require('path');
const cs = require('./claude_session.js');

// ---------------------------------------------------------------- 适配器注册表
// （tool 名与 adapters/<tool>.js 一一对应；storage 仅用于展示）

const TOOL_REGISTRY = [
  { tool: 'codex', storage: 'JSONL', desc: 'Codex CLI（~/.codex）' },
  { tool: 'copilot', storage: 'JSONL+YAML', desc: 'GitHub Copilot CLI（~/.copilot）' },
  { tool: 'dsh', storage: 'zstd+JSONL', desc: 'DeepSeek Harness（~/.dsh）' },
  { tool: 'grok', storage: 'JSONL+JSON', desc: 'Grok Build CLI（~/.grok）' },
  { tool: 'kimi', storage: 'JSONL', desc: 'Kimi Code CLI（~/.kimi-code）' },
  { tool: 'qoder', storage: 'JSONL', desc: 'Qoder CLI（~/.qoder）' },
  { tool: 'zcode', storage: 'SQLite', desc: 'ZCode（~/.zcode）' },
  { tool: 'agy', storage: 'JSONL', desc: 'Antigravity CLI（~/.gemini/antigravity）' },
  { tool: 'continue', storage: 'JSON', desc: 'Continue（~/.continue）' },
  { tool: 'cursor', storage: 'SQLite', desc: 'Cursor IDE（state.vscdb）' },
  { tool: 'hermes', storage: 'SQLite', desc: 'Hermes Agent（state.db）' },
  { tool: 'kilo', storage: 'SQLite+JSON', desc: 'Kilo Code（kilo.db）' },
  { tool: 'mimo', storage: 'SQLite', desc: 'MiMo-Code（mimocode.db）' },
  { tool: 'minimax', storage: 'SQLite+JSONL', desc: 'MiniMax Code（~/.minimax）' },
  { tool: 'openclaw', storage: 'SQLite+JSON', desc: 'OpenClaw（~/.openclaw）' },
  { tool: 'opencode', storage: 'SQLite+JSON', desc: 'OpenCode（opencode.db）' },
  { tool: 'pi', storage: 'JSONL', desc: 'Pi Coding Agent（~/.pi）' },
  { tool: 'workbuddy', storage: 'JSONL', desc: 'WorkBuddy（~/.workbuddy）' },
];

function knownTools() {
  return TOOL_REGISTRY.map((e) => e.tool);
}

function loadAdapter(tool) {
  // 加载 adapters/<tool>.js；文件缺失说明该工具不可行/不支持
  const p = path.join(__dirname, 'adapters', `${tool}.js`);
  let mod;
  try {
    mod = require(p);
  } catch (e) {
    if (e && e.code === 'MODULE_NOT_FOUND' && e.message.includes(tool)) {
      throw new cs.ConvertError(`工具 ${tool} 没有可用适配器（不支持或未实现）`);
    }
    throw e;
  }
  return mod;
}


// ---------------------------------------------------------------- 列表

function cmdList(args) {
  const wanted = args.tool && args.tool.length ? args.tool : knownTools();
  const unknown = wanted.filter((t) => !knownTools().includes(t));
  if (unknown.length) {
    process.stderr.write(`错误：未知工具：${unknown.join('、')}\n`);
    return 1;
  }
  const report = [];
  for (const name of wanted) {
    const entry = TOOL_REGISTRY.find((e) => e.tool === name);
    const item = {
      tool: name, storage: entry.storage, desc: entry.desc,
      available: false, sessions: [],
    };
    try {
      const mod = loadAdapter(name);
      const sessions = mod.listSessions({ dir: args.dir, project: null });
      item.available = true;
      const shown = args.limit > 0 ? sessions.slice(0, args.limit) : sessions;
      for (const s of shown) {
        item.sessions.push({
          session_id: s.session_id || '',
          title: s.title || '',
          cwd: s.cwd || '',
          last_ts: s.last_ts || '',
          count: s.count || 0,
        });
      }
    } catch (e) {
      item.error = e instanceof cs.ConvertError ? e.message : `适配器异常: ${e.message}`;
    }
    report.push(item);
  }

  if (args.json) {
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
    return 0;
  }
  for (const item of report) {
    const mark = item.available ? '可用' : '不可用';
    const err = item.error ? `（${item.error}）` : '';
    process.stdout.write(`[${mark}] ${item.tool.padEnd(10)} ${item.storage.padEnd(12)} ${item.desc}${err}\n`);
    for (const s of item.sessions) {
      const ts = (s.last_ts || '(无时间)').padEnd(20);
      const sid = s.session_id.slice(0, 12).padEnd(14);
      const count = String(s.count).padEnd(5);
      process.stdout.write(`    ${ts} ${sid} 消息数:${count} ${s.title.slice(0, 50)}\n`);
    }
  }
  return 0;
}


// ---------------------------------------------------------------- 转换

function cmdConvert(args) {
  if (!args.tool) {
    process.stderr.write('错误：必须用 --tool 指定来源工具（--list 可查看支持列表）\n');
    return 1;
  }
  if (!knownTools().includes(args.tool)) {
    process.stderr.write(`错误：未知工具：${args.tool}\n`);
    return 1;
  }

  const mod = loadAdapter(args.tool);
  const loaded = mod.loadSession({ dir: args.dir, project: args.project, session_id: args.session });
  const ref = loaded.ref;
  const msgs = loaded.msgs;
  if (!msgs || !msgs.length) {
    process.stderr.write('错误：会话内容为空，未转换\n');
    return 1;
  }

  const result = cs.writeSession(ref, msgs, {
    claude_dir: args.claude_dir,
    project_override: args.to_project,
    out_dir: args.out_dir,
    out_file: args.out_file,
    session_id: args.session_id,
    degrade_tools: args.degrade_tools,
    title_prefix: args.no_prefix ? '' : `[${ref.tool || args.tool}] `,
    max_tool_output: args.max_tool_output,
    max_text: args.max_text,
    dry_run: args.dry_run,
  });

  if (args.json) {
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } else {
    const status = result.dry_run ? '预演完成' : (result.ok ? '转换成功' : '转换失败');
    process.stdout.write(`${status}：${result.title}\n`);
    process.stdout.write(`  会话ID: ${result.session_id}\n`);
    process.stdout.write(`  记录数: ${result.records}\n`);
    process.stdout.write(`  目标文件: ${result.path}\n`);
    if (result.dry_run) process.stdout.write('  （dry-run 模式，未实际写入）\n');
    for (const w of result.warnings) process.stdout.write(`  警告: ${w}\n`);
    for (const e of result.errors) process.stdout.write(`  错误: ${e}\n`);
    if (result.ok && !result.dry_run) {
      process.stdout.write('  现在可在对应项目目录用 claude 后执行 /resume 恢复该会话。\n');
    }
  }
  return result.ok ? 0 : 1;
}


// ---------------------------------------------------------------- 入口

function parseArgs(argv) {
  const args = {
    list: false, tool: null, session: null, project: null, dir: null,
    claude_dir: null, to_project: null, out_dir: null, out_file: null,
    session_id: null, dry_run: false, degrade_tools: false, no_prefix: false,
    max_tool_output: cs.DEFAULT_MAX_TOOL_OUTPUT, max_text: cs.DEFAULT_MAX_TEXT,
    limit: 20, json: false,
  };
  const aliases = {
    '--tool': 'tool',
    '--session': 'session', '--project': 'project', '--dir': 'dir',
    '--claude-dir': 'claude_dir', '--to-project': 'to_project',
    '--out-dir': 'out_dir', '--out-file': 'out_file', '--session-id': 'session_id',
    '--max-tool-output': 'max_tool_output', '--max-text': 'max_text', '--limit': 'limit',
  };
  const flags = {
    '--list': 'list', '--dry-run': 'dry_run',
    '--degrade-tools': 'degrade_tools', '--no-prefix': 'no_prefix', '--json': 'json',
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (aliases[a] != null) {
      args[aliases[a]] = argv[++i];
    } else if (flags[a] != null) {
      args[flags[a]] = true;
    } else if (a === '-h' || a === '--help') {
      return null;
    } else {
      throw new Error(`未知参数: ${a}`);
    }
  }
  return args;
}

function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (e) {
    process.stderr.write(`错误：${e.message}\n（--help 查看用法）\n`);
    return 2;
  }
  if (!args) {
    process.stdout.write([
      '用法: node export_to_claude.js --tool <工具> [选项]',
      '  --list                    列出工具与可用会话',
      '  --tool <名称>             来源工具（codex/copilot/dsh/grok/kimi/qoder/zcode/...）',
      '  --session <ID|前缀>       来源会话（默认取最新）',
      '  --project <路径>          按项目路径过滤来源会话',
      '  --dir <目录>              来源数据目录覆盖',
      '  --claude-dir <目录>       Claude 配置目录（默认 ~/.claude）',
      '  --to-project <路径>       目标项目路径（默认用会话原 cwd）',
      '  --out-dir <目录>          输出目录覆盖（测试用）',
      '  --out-file <文件>         输出文件覆盖（优先级最高）',
      '  --session-id <ID>         指定新会话 ID',
      '  --dry-run                 只构建与校验，不写入',
      '  --degrade-tools           工具调用降级为纯文本（最大兼容）',
      '  --no-prefix               标题不加来源前缀',
      '  --json                    JSON 输出',
      '详见同目录 SKILL.md',
    ].join('\n') + '\n');
    return 0;
  }
  if (args.tool) args.tool = String(args.tool).trim().toLowerCase();
  try {
    if (args.list) {
      args.tool = args.tool ? args.tool.split(',').map((s) => s.trim()).filter(Boolean) : null;
      return cmdList(args);
    }
    return cmdConvert(args);
  } catch (e) {
    if (e instanceof cs.ConvertError) {
      process.stderr.write(`错误：${e.message}\n`);
      return 1;
    }
    process.stderr.write(`错误：${e.message}\n`);
    return 1;
  }
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = { TOOL_REGISTRY, loadAdapter, main };
