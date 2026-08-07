#!/usr/bin/env node
// -*- coding: utf-8 -*-
/** 读取 Qoder CLI 本地会话 JSONL，生成结构化接管摘要。 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const CST_OFFSET_MS = 8 * 3600 * 1000;
const READ_TOOLS = new Set(['read', 'glob', 'grep', 'list', 'codesearch']);
const EDIT_TOOLS = new Set(['write', 'edit', 'multiedit', 'notebookedit', 'patch', 'apply_patch']);
const SHELL_TOOLS = new Set(['bash', 'shell', 'shell_command', 'terminal']);
const SYS_REMINDER_RE = /<system-reminder>[\s\S]*?<\/system-reminder>/g;
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;

function qoderDirDefault() {
  return process.env.QODER_HOME || path.join(os.homedir(), '.qoder');
}

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

function timestampMs(value) {
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) return Math.abs(number) < 1e10 ? Math.trunc(number * 1000) : Math.trunc(number);
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function fmtTime(value) {
  const ms = timestampMs(value);
  if (!ms) return '';
  const d = new Date(ms + CST_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

function isDir(value) {
  try { return fs.statSync(value).isDirectory(); } catch (_) { return false; }
}

function parseJsonl(filePath) {
  try {
    const events = [];
    for (const line of fs.readFileSync(filePath, 'utf-8').split(/\r?\n/)) {
      if (!line.trim()) continue;
      try { events.push(JSON.parse(line)); } catch (_) { /* skip malformed line */ }
    }
    return { events, error: null };
  } catch (error) {
    return { events: [], error: error.message };
  }
}

function extractText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((block) => block && typeof block === 'object' && block.type === 'text' && block.text)
    .map((block) => block.text).join('\n');
}

function resultText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((block) => {
      if (block && typeof block === 'object') return String(block.text || block.content || '');
      return block == null ? '' : String(block);
    }).filter(Boolean).join('\n');
  }
  if (content === null || content === undefined) return '';
  return typeof content === 'object' ? JSON.stringify(content) : String(content);
}

function stripReminders(value) {
  return String(value || '').replace(SYS_REMINDER_RE, '').trim();
}

function normalizeEvents(events) {
  const items = [], summaries = [];
  const titles = { custom: '', ai: '', agent: '' };
  let cwd = '', gitBranch = '', model = '', reasoningEffort = '', contextWindow = '', entrypoint = '', version = '';
  let isSidechain = false;
  for (const event of events) {
    const eventType = event.type;
    if (eventType === 'custom-title') { titles.custom = event.customTitle || titles.custom; continue; }
    if (eventType === 'ai-title') { titles.ai = event.aiTitle || titles.ai; continue; }
    if (eventType === 'agent-name') { titles.agent = event.agentName || titles.agent; continue; }
    if (eventType === 'runtime-config') {
      model = event.model || model;
      reasoningEffort = event.reasoningEffort || reasoningEffort;
      contextWindow = event.contextWindow || contextWindow;
      continue;
    }
    cwd = cwd || event.cwd || '';
    gitBranch = gitBranch || event.gitBranch || '';
    entrypoint = entrypoint || event.entrypoint || '';
    version = version || event.version || '';
    isSidechain = isSidechain || !!event.isSidechain;
    if (event.isCompactSummary) {
      const compact = event.summary || event.content || extractText((event.message || {}).content);
      if (compact) summaries.push(compact);
      continue;
    }
    if (eventType === 'system') {
      if (['compact_summary', 'summary'].includes(event.subtype)) {
        const compact = event.summary || event.content;
        if (compact) summaries.push(String(compact));
      }
      continue;
    }
    if (!['user', 'assistant'].includes(eventType)) continue;
    const message = event.message || {};
    const content = message.content;
    const ts = timestampMs(event.timestamp);
    if (message.model) model = message.model;
    if (eventType === 'user') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === 'object' && block.type === 'tool_result') {
            items.push({
              kind: 'tool_result', timestamp: ts, tool_use_id: block.tool_use_id || '',
              content: resultText(block.content), is_error: !!block.is_error,
            });
          }
        }
      }
      if (!event.isMeta && !event.isVisibleInTranscriptOnly) {
        const text = stripReminders(extractText(content));
        if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      }
    } else {
      const text = extractText(content);
      if (text.trim()) items.push({ kind: 'assistant_text', timestamp: ts, text });
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === 'object' && block.type === 'tool_use') {
            items.push({
              kind: 'tool_use', timestamp: ts, name: block.name || 'tool',
              input: block.input || {}, tool_use_id: block.id || '',
            });
          }
        }
      }
      if (event.error && !text.trim()) {
        items.push({ kind: 'assistant_text', timestamp: ts, text: '[API 错误] ' + resultText(event.errorDetails || event.error) });
      }
    }
  }
  return {
    items, summaries, titles, cwd, git_branch: gitBranch, model,
    reasoning_effort: reasoningEffort, context_window: contextWindow,
    entrypoint, version, is_sidechain: isSidechain,
  };
}

function titleFor(norm, sessionId) {
  for (const key of ['custom', 'ai', 'agent']) if (norm.titles[key]) return norm.titles[key];
  for (const item of norm.items) {
    if (item.kind === 'user_text') {
      const first = item.text.trim().split(/\r?\n/)[0] || '';
      return first.length > 60 ? first.slice(0, 60) + '…' : (first || sessionId);
    }
  }
  return sessionId;
}

function stateWorkspaces(jsonlPath) {
  const statePath = path.join(jsonlPath.slice(0, -'.jsonl'.length), 'state.json');
  try {
    const data = JSON.parse(fs.readFileSync(statePath, 'utf-8'));
    return Array.isArray(data.workspaceDirectories) ? data.workspaceDirectories.map(String) : [];
  } catch (_) { return []; }
}

function currentMeta(filePath) {
  const { events, error } = parseJsonl(filePath);
  if (error) return null;
  const normalized = normalizeEvents(events);
  const timestamps = normalized.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  const sessionId = path.basename(filePath, '.jsonl');
  const workspaces = stateWorkspaces(filePath);
  const mtime = fs.statSync(filePath).mtimeMs;
  return {
    session_id: sessionId,
    title: titleFor(normalized, sessionId),
    directory: normalized.cwd || workspaces[0] || '',
    created: timestamps[0] || mtime,
    updated: timestamps[timestamps.length - 1] || mtime,
    count: normalized.items.length,
    source: 'jsonl',
    path: filePath,
    normalized,
  };
}

function legacyMeta(filePath) {
  try {
    const obj = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    const sessionId = obj.id || path.basename(filePath).replace(/-session\.json$/i, '');
    const mtime = fs.statSync(filePath).mtimeMs;
    return {
      session_id: sessionId,
      title: obj.title || sessionId,
      directory: obj.working_dir || '',
      created: obj.created_at || mtime,
      updated: obj.updated_at || mtime,
      count: obj.message_count || 0,
      source: 'legacy-metadata',
      path: filePath,
      legacy: obj,
    };
  } catch (_) { return null; }
}

function scanSessions(qoderDir) {
  const root = path.join(qoderDir, 'projects');
  if (!isDir(root)) return [];
  const sessions = [], currentIds = new Set();
  for (const name of fs.readdirSync(root)) {
    const projectDir = path.join(root, name);
    if (!isDir(projectDir)) continue;
    const files = fs.readdirSync(projectDir);
    for (const file of files.filter((value) => value.endsWith('.jsonl'))) {
      const meta = currentMeta(path.join(projectDir, file));
      if (meta) { sessions.push(meta); currentIds.add(meta.session_id); }
    }
    for (const file of files.filter((value) => value.endsWith('-session.json'))) {
      const meta = legacyMeta(path.join(projectDir, file));
      if (meta && !currentIds.has(meta.session_id)) sessions.push(meta);
    }
  }
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

function loadSession(meta) {
  const normalized = meta.source === 'jsonl' ? meta.normalized : {
    items: [], summaries: [], titles: { custom: meta.title, ai: '', agent: '' },
    cwd: meta.directory, git_branch: '', model: '', reasoning_effort: '', context_window: '',
    entrypoint: '', version: '', is_sidechain: false,
  };
  const timestamps = normalized.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  return {
    info: {
      session_id: meta.session_id, title: meta.title, directory: meta.directory, source: meta.source,
      model: normalized.model, reasoning_effort: normalized.reasoning_effort,
      context_window: normalized.context_window, entrypoint: normalized.entrypoint,
      version: normalized.version, git_branch: normalized.git_branch,
      first_ts: fmtTime(timestamps[0] || meta.created),
      last_ts: fmtTime(timestamps[timestamps.length - 1] || meta.updated),
    },
    normalized,
  };
}

function toolFile(name, input) {
  for (const key of ['file_path', 'filePath', 'notebook_path', 'path', 'filename']) if (input[key]) return String(input[key]);
  if (['glob', 'grep', 'codesearch'].includes(name)) return String(input.pattern || input.query || '');
  return '';
}

function shellCommand(input) { return String(input.command || input.cmd || '').trim(); }

function dedupe(values) {
  const seen = new Set(), output = [];
  for (const value of values) if (value && !seen.has(value)) { seen.add(value); output.push(value); }
  return output;
}

function buildState(items) {
  const filesRead = [], filesEdited = [], commands = [], testResults = [], calls = new Map();
  let firstUser = '', lastUser = '', lastAssistant = '';
  for (const item of items) {
    if (item.kind === 'user_text') { firstUser = firstUser || item.text; lastUser = item.text; }
    else if (item.kind === 'assistant_text') lastAssistant = item.text;
    else if (item.kind === 'tool_use') {
      const name = String(item.name || '').toLowerCase(), input = item.input || {};
      calls.set(item.tool_use_id || `#${calls.size}`, [name, input]);
      if (READ_TOOLS.has(name)) filesRead.push(toolFile(name, input));
      else if (EDIT_TOOLS.has(name)) filesEdited.push(toolFile(name, input));
      else if (SHELL_TOOLS.has(name)) commands.push(shellCommand(input));
    } else if (item.kind === 'tool_result') {
      const [name, input] = calls.get(item.tool_use_id) || ['', {}];
      const content = item.content || '';
      const command = SHELL_TOOLS.has(name) ? shellCommand(input) : '';
      if ((item.is_error || TEST_CMD_RE.test(command) || TEST_RESULT_RE.test(content.slice(0, 2000))) && content.trim()) {
        testResults.push({ command_hint: command || name, is_error: !!item.is_error, content });
      }
    }
  }
  return {
    goal: firstUser, files_read: dedupe(filesRead), files_edited: dedupe(filesEdited),
    commands: dedupe(commands), test_results: testResults, last_user: lastUser, last_assistant: lastAssistant,
  };
}

function truncate(value, limit) {
  const text = String(value);
  return text.length <= limit ? text : text.slice(0, limit) + '…';
}

function textBlock(value, limit) {
  const text = String(value || '').trim();
  return text.length <= limit ? text : text.slice(0, limit) + `\n…（已截断，原长 ${text.length} 字符）`;
}

function toolBrief(item) {
  const name = item.name || 'tool', input = item.input || {};
  const detail = shellCommand(input) || toolFile(String(name).toLowerCase(), input);
  return detail ? `${name}(${truncate(detail, 100)})` : `${name}(...)`;
}

function renderItem(item, maxChars) {
  const ts = fmtTime(item.timestamp);
  if (item.kind === 'user_text') return [`### [用户] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'} ${ts}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果]${item.is_error ? ' (错误)' : ''} ${ts}`, textBlock(item.content, maxChars)];
  return [];
}

function renderSummary(session, state, recentN, maxChars) {
  const info = session.info, norm = session.normalized;
  const lines = [
    '# Resume-Qoder 会话接管摘要', '', '## 会话信息',
    `- 标题: ${info.title}`, `- 会话ID: ${info.session_id}`,
    `- 项目: ${info.directory || '(未知)'}`, `- 存储: ${info.source}`,
  ];
  if (info.model) lines.push(`- 模型: ${info.model}`);
  if (info.reasoning_effort) lines.push(`- 推理强度: ${info.reasoning_effort}`);
  if (info.context_window) lines.push(`- Context Window: ${info.context_window}`);
  if (info.git_branch) lines.push(`- Git 分支: ${info.git_branch}`);
  if (info.entrypoint) lines.push(`- 入口: ${info.entrypoint}`);
  if (info.version) lines.push(`- Qoder 版本: ${info.version}`);
  if (norm.is_sidechain) lines.push('- 类型: 子 agent 会话 (sidechain)');
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`, `- 消息条目数: ${norm.items.length}`, '');
  if (info.source === 'legacy-metadata') lines.push('## 兼容性说明', '- 该旧版会话只保留本地元数据、文件快照和工具结果，未发现可读取的主对话 transcript。', '');
  if (norm.summaries.length) {
    lines.push('## 历史摘要（原会话 compact）');
    for (const value of norm.summaries) lines.push(`- ${truncate(value, maxChars)}`);
    lines.push('');
  }
  lines.push('## 任务状态重建', '', '### 目标', textBlock(state.goal, maxChars) || '(未识别)', '');
  for (const [title, key] of [['已调查文件', 'files_read'], ['代码修改', 'files_edited'], ['执行命令', 'commands']]) {
    if (state[key].length) {
      lines.push(`### ${title}`);
      for (const value of state[key]) lines.push(`- ${truncate(value, 200)}`);
      lines.push('');
    }
  }
  if (state.test_results.length) {
    lines.push('### 测试 / 错误结果');
    for (const result of state.test_results.slice(-5)) {
      const first = result.content.trim().split(/\r?\n/)[0] || '';
      lines.push(`-${result.is_error ? ' [错误]' : ''} ${truncate(first, 200)}`);
    }
    lines.push('');
  }
  lines.push('### 最近用户消息', textBlock(state.last_user, maxChars) || '(无)', '', '### 最近助手消息', textBlock(state.last_assistant, maxChars) || '(无)', '');
  const recent = recentN ? norm.items.slice(-recentN) : [];
  lines.push(`## 近期对话（最近 ${recent.length} 条）`, '');
  for (const item of recent) { lines.push(...renderItem(item, maxChars), ''); }
  const olderTools = recentN ? norm.items.slice(0, -recentN).filter((item) => item.kind === 'tool_use') : [];
  if (olderTools.length) {
    lines.push('## 更早活动（工具调用，仅最近 60 条）');
    for (const item of olderTools.slice(-60)) lines.push(`- [${fmtTime(item.timestamp)}] ${toolBrief(item)}`);
    lines.push('');
  }
  lines.push('## 接管建议', '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。', '- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。', '- 不要逐字复述历史；基于现状决定下一步动作。', '');
  return lines.join('\n');
}

function projectSessions(sessions, projectPath) {
  const target = normPath(projectPath);
  return sessions.filter((item) => normPath(item.directory) === target);
}

function pickSession(sessions, sessionArg, projectPath) {
  if (sessionArg) return sessions.find((item) => item.session_id.startsWith(sessionArg) || item.session_id.includes(sessionArg)) || null;
  return projectSessions(sessions, projectPath)[0] || null;
}

function printList(sessions, projectPath, limit) {
  const selected = projectSessions(sessions, projectPath);
  const shown = limit > 0 ? selected.slice(0, limit) : selected;
  console.log(`当前项目: ${projectPath}`);
  console.log(`找到 ${selected.length} 个会话${shown.length < selected.length ? `（仅显示最近 ${shown.length} 个）` : ''}：\n`);
  shown.forEach((meta, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    console.log(`${mark} ${fmtTime(meta.updated) || '(无时间)'}  ${meta.session_id.slice(0, 12)}  消息数:${String(meta.count).padEnd(4)} 标题: ${meta.title}`);
  });
}

function printHelp() {
  console.log(`用法: node resume_qoder.js [选项]

读取 Qoder CLI 本地会话，生成结构化接管摘要。

选项:
  --list              仅列出当前项目会话
  --latest            取最近一个会话（默认）
  --session ID        指定会话 ID 或前缀；跨项目查找
  --project PATH      项目路径，默认当前目录
  --qoder-dir DIR     Qoder 用户数据目录，默认 ~/.qoder 或 $QODER_HOME
  --recent N          近期条目数，默认 8
  --max-chars N       单条截断长度，默认 1500
  --limit N           --list 数量上限，0 不限制
  --json              输出 JSON
  --output FILE       将摘要写入文件
  -h, --help          显示帮助`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: process.cwd(), qoderDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
  const value = (flag, index) => {
    if (index + 1 >= argv.length) throw new Error(`${flag} 需要一个参数`);
    return argv[index + 1];
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--list') args.list = true;
    else if (arg === '--latest') args.latest = true;
    else if (arg === '--json') args.json = true;
    else if (arg === '-h' || arg === '--help') args.help = true;
    else if (arg === '--session') args.session = value(arg, i++);
    else if (arg.startsWith('--session=')) args.session = arg.slice(10);
    else if (arg === '--project') args.project = value(arg, i++);
    else if (arg.startsWith('--project=')) args.project = arg.slice(10);
    else if (arg === '--qoder-dir') args.qoderDir = value(arg, i++);
    else if (arg.startsWith('--qoder-dir=')) args.qoderDir = arg.slice(12);
    else if (arg === '--recent') args.recent = Number.parseInt(value(arg, i++), 10);
    else if (arg.startsWith('--recent=')) args.recent = Number.parseInt(arg.slice(9), 10);
    else if (arg === '--max-chars') args.maxChars = Number.parseInt(value(arg, i++), 10);
    else if (arg.startsWith('--max-chars=')) args.maxChars = Number.parseInt(arg.slice(12), 10);
    else if (arg === '--limit') args.limit = Number.parseInt(value(arg, i++), 10);
    else if (arg.startsWith('--limit=')) args.limit = Number.parseInt(arg.slice(8), 10);
    else if (arg === '--output') args.output = value(arg, i++);
    else if (arg.startsWith('--output=')) args.output = arg.slice(9);
    else throw new Error(`未知参数 '${arg}'`);
  }
  return args;
}

function main() {
  let args;
  try { args = parseArgs(process.argv.slice(2)); } catch (error) { console.error('错误：' + error.message); process.exit(1); }
  if (args.help) { printHelp(); return; }
  const qoderDir = path.resolve(args.qoderDir || qoderDirDefault());
  const sessions = scanSessions(qoderDir);
  if (!sessions.length) { console.error(`错误：在 ${path.join(qoderDir, 'projects')} 未找到 Qoder 会话。`); process.exit(1); }
  const projectPath = path.resolve(args.project);
  if (args.list) {
    if (!projectSessions(sessions, projectPath).length) { console.error(`错误：未找到项目 ${projectPath} 的 Qoder 会话。可用 --session ID 跨项目查找。`); process.exit(1); }
    printList(sessions, projectPath, args.limit);
    return;
  }
  const target = pickSession(sessions, args.session, projectPath);
  if (!target) { console.error(`错误：未匹配到会话 '${args.session || '当前项目'}'。`); process.exit(1); }
  const session = loadSession(target), state = buildState(session.normalized.items);
  const recentCount = Math.max(args.recent, 0);
  const recentItems = recentCount ? session.normalized.items.slice(-recentCount) : [];
  const output = args.json
    ? JSON.stringify({ info: session.info, state, summaries: session.normalized.summaries, recent_items: recentItems }, null, 2)
    : renderSummary(session, state, Math.max(args.recent, 0), Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output, 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
