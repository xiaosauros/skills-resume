#!/usr/bin/env node
'use strict';

/** 读取 Antigravity CLI（agy）本地 transcript，生成结构化接管摘要。 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const READ_TOOLS = new Set(['list_dir', 'grep_search', 'view_file', 'search_web', 'read_url_content']);
const EDIT_TOOLS = new Set(['write_to_file', 'replace_file_content', 'multi_replace_file_content']);
const SHELL_TOOLS = new Set(['run_command']);
const RESULT_TYPES = new Set([
  'LIST_DIRECTORY', 'GREP_SEARCH', 'VIEW_FILE', 'RUN_COMMAND', 'CODE_ACTION',
  'INVOKE_SUBAGENT', 'SEARCH_WEB', 'ASK_QUESTION', 'READ_URL_CONTENT',
]);
const TYPE_TOOL = {
  LIST_DIRECTORY: 'list_dir', GREP_SEARCH: 'grep_search', VIEW_FILE: 'view_file',
  RUN_COMMAND: 'run_command', CODE_ACTION: 'code_action', INVOKE_SUBAGENT: 'invoke_subagent',
  SEARCH_WEB: 'search_web', ASK_QUESTION: 'ask_question', READ_URL_CONTENT: 'read_url_content',
};
const ARTIFACTS = ['task.md', 'implementation_plan.md', 'walkthrough.md'];
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;

function isDir(value) { try { return fs.statSync(value).isDirectory(); } catch (_) { return false; } }
function defaultAgyDir() { return path.resolve(process.env.ANTIGRAVITY_HOME || path.join(os.homedir(), '.gemini', 'antigravity')); }
function normPath(value) { return value ? path.normalize(path.resolve(String(value))).toLowerCase() : ''; }

function timestampMs(value) {
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) return Math.abs(number) < 10_000_000_000 ? Math.trunc(number * 1000) : Math.trunc(number);
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function fmtTime(value) {
  const ms = timestampMs(value);
  if (!ms) return '';
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(ms));
  const get = (type) => parts.find((item) => item.type === type)?.value || '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`;
}

function parseJsonl(filePath) {
  const events = [];
  try {
    for (const line of fs.readFileSync(filePath, 'utf-8').split(/\r?\n/)) {
      if (!line.trim()) continue;
      try { events.push(JSON.parse(line)); } catch (_) { /* skip malformed line */ }
    }
    return { events, error: null };
  } catch (error) { return { events: [], error: error.message }; }
}

function valueText(value) {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function commandText(value) {
  if (Array.isArray(value)) return value.map(String).join('\n');
  return String(value || '').trim();
}

function cleanVisibleText(value) {
  const text = String(value || '').trim();
  const match = text.match(/<USER_REQUEST>\s*([\s\S]*?)\s*<\/USER_REQUEST>/);
  if (match) return match[1].trim();
  return text.replace(/<ADDITIONAL_METADATA>[\s\S]*?<\/ADDITIONAL_METADATA>/g, '').trim();
}

function cleanPath(value) {
  let text = String(value || '').trim();
  if (text.length >= 2 && text.startsWith('"') && text.endsWith('"')) {
    try { const decoded = JSON.parse(text); if (typeof decoded === 'string') text = decoded; }
    catch (_) { text = text.slice(1, -1); }
  }
  return text;
}

function gitRoot(value) {
  let candidate = path.win32.normalize(value);
  let candidateIsDir = false;
  try { candidateIsDir = fs.statSync(candidate).isDirectory(); } catch (_) { /* path may no longer exist */ }
  if (path.win32.extname(candidate) && !candidateIsDir) candidate = path.win32.dirname(candidate);
  let current = candidate;
  while (current) {
    if (fs.existsSync(path.win32.join(current, '.git'))) return current.toLowerCase();
    const parent = path.win32.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return candidate.toLowerCase();
}

function normalizeEvents(events) {
  const items = [], summaries = [];
  for (const event of events) {
    const ts = timestampMs(event.created_at);
    const source = String(event.source || ''), eventType = String(event.type || ''), status = String(event.status || '');
    const content = cleanVisibleText(valueText(event.content));
    const error = valueText(event.error).trim();
    if (source === 'USER_EXPLICIT' && eventType === 'USER_INPUT' && content) items.push({ kind: 'user_text', timestamp: ts, text: content });
    else if (['CHECKPOINT', 'CONVERSATION_HISTORY'].includes(eventType) && content) summaries.push(content);
    else if (source === 'MODEL' && eventType === 'PLANNER_RESPONSE' && content) items.push({ kind: 'assistant_text', timestamp: ts, text: content });
    else if (RESULT_TYPES.has(eventType) && content) items.push({ kind: 'tool_result', timestamp: ts, name: TYPE_TOOL[eventType] || eventType.toLowerCase(), content, is_error: status === 'ERROR' });
    else if (source === 'MODEL' && content && eventType !== 'EPHEMERAL_MESSAGE') items.push({ kind: 'assistant_text', timestamp: ts, text: content });
    (Array.isArray(event.tool_calls) ? event.tool_calls : []).forEach((call, index) => {
      if (!call || typeof call !== 'object') return;
      items.push({
        kind: 'tool_use', timestamp: ts, name: String(call.name || 'tool'),
        input: call.args && typeof call.args === 'object' && !Array.isArray(call.args) ? call.args : {},
        tool_use_id: `${event.step_index || ''}:${index}`,
      });
    });
    if (error) items.push({ kind: 'tool_result', timestamp: ts, name: TYPE_TOOL[eventType] || eventType.toLowerCase() || 'error', content: error, is_error: true });
  }
  return { items, summaries };
}

function toolPath(name, input) {
  const byTool = {
    list_dir: ['DirectoryPath'], grep_search: ['SearchPath', 'Query'], view_file: ['AbsolutePath'],
    write_to_file: ['TargetFile'], replace_file_content: ['TargetFile'], multi_replace_file_content: ['TargetFile'],
  };
  for (const key of byTool[name] || ['TargetFile', 'AbsolutePath', 'DirectoryPath', 'SearchPath']) {
    if (input[key]) return cleanPath(input[key]);
  }
  return '';
}

function isAbsoluteAny(value) { return path.isAbsolute(value) || /^[A-Za-z]:[\\/]/.test(value); }

function commonWindowsPath(values) {
  if (!values.length) return '';
  const parsed = values.map((value) => path.win32.normalize(value).split(/[\\/]+/));
  const first = parsed[0], common = [];
  for (let index = 0; index < first.length; index++) {
    if (parsed.every((parts) => String(parts[index] || '').toLowerCase() === String(first[index]).toLowerCase())) common.push(first[index]);
    else break;
  }
  return common.join('\\');
}

function inferDirectory(items) {
  const cwds = [], paths = [];
  for (const item of items) {
    if (item.kind !== 'tool_use') continue;
    const input = item.input || {}, cwd = cleanPath(input.Cwd);
    if (cwd && isAbsoluteAny(cwd)) cwds.push(path.win32.normalize(cwd));
    const candidate = toolPath(String(item.name || ''), input);
    if (candidate && isAbsoluteAny(candidate) && !candidate.replaceAll('/', '\\').toLowerCase().includes('.gemini\\antigravity\\brain')) paths.push(path.win32.normalize(candidate));
  }
  if (cwds.length) {
    const count = new Map();
    for (const cwd of cwds) count.set(cwd.toLowerCase(), (count.get(cwd.toLowerCase()) || 0) + 1);
    return gitRoot(cwds.find((cwd) => cwd.toLowerCase() === [...count].sort((a, b) => b[1] - a[1])[0][0]) || '');
  }
  const common = commonWindowsPath(paths);
  if (!common) return '';
  return gitRoot(common);
}

function titleFor(items, sessionId) {
  const first = items.find((item) => item.kind === 'user_text');
  if (!first) return sessionId;
  const line = String(first.text || '').trim().split(/\r?\n/)[0] || '';
  return line.length > 60 ? line.slice(0, 60) + '…' : (line || sessionId);
}

function readArtifacts(sessionDir) {
  const result = {};
  for (const name of ARTIFACTS) {
    const filePath = path.join(sessionDir, name);
    try { if (fs.statSync(filePath).isFile()) result[name] = fs.readFileSync(filePath, 'utf-8'); } catch (_) { /* absent */ }
  }
  return result;
}

function sessionMeta(transcript) {
  const { events, error } = parseJsonl(transcript);
  if (error) return null;
  const normalized = normalizeEvents(events), items = normalized.items;
  const timestamps = items.filter((item) => item.timestamp).map((item) => item.timestamp);
  const sessionDir = path.dirname(path.dirname(path.dirname(transcript)));
  const sessionId = path.basename(sessionDir), mtime = fs.statSync(transcript).mtimeMs;
  return {
    session_id: sessionId, title: titleFor(items, sessionId), directory: inferDirectory(items),
    created: timestamps[0] || mtime, updated: timestamps[timestamps.length - 1] || mtime,
    count: events.length, path: transcript, normalized, artifacts: readArtifacts(sessionDir),
  };
}

function scanSessions(agyDir) {
  const brain = path.join(agyDir, 'brain');
  if (!isDir(brain)) return [];
  const sessions = [];
  for (const name of fs.readdirSync(brain)) {
    const transcript = path.join(brain, name, '.system_generated', 'logs', 'transcript.jsonl');
    try {
      if (!fs.statSync(transcript).isFile()) continue;
      const meta = sessionMeta(transcript);
      if (meta) sessions.push(meta);
    } catch (_) { /* absent */ }
  }
  return sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
}

function dedupe(values) {
  const seen = new Set(), output = [];
  for (const value of values) if (value && !seen.has(value)) { seen.add(value); output.push(value); }
  return output;
}

function buildState(items) {
  const filesRead = [], filesEdited = [], commands = [], testResults = [];
  let firstUser = '', lastUser = '', lastAssistant = '', lastCommand = '';
  for (const item of items) {
    if (item.kind === 'user_text') { firstUser = firstUser || item.text; lastUser = item.text; }
    else if (item.kind === 'assistant_text') lastAssistant = item.text;
    else if (item.kind === 'tool_use') {
      const name = String(item.name || '').toLowerCase(), input = item.input || {};
      if (READ_TOOLS.has(name)) filesRead.push(toolPath(name, input));
      else if (EDIT_TOOLS.has(name)) filesEdited.push(toolPath(name, input));
      else if (SHELL_TOOLS.has(name)) { lastCommand = commandText(input.CommandLine); commands.push(lastCommand); }
    } else if (item.kind === 'tool_result') {
      const content = item.content || '';
      const isCommandResult = item.name === 'run_command';
      if (item.is_error || (isCommandResult && TEST_CMD_RE.test(lastCommand)) || TEST_RESULT_RE.test(content.slice(0, 2000))) testResults.push({ command_hint: lastCommand || item.name || '', is_error: !!item.is_error, content });
    }
  }
  return {
    goal: firstUser, files_read: dedupe(filesRead), files_edited: dedupe(filesEdited), commands: dedupe(commands),
    test_results: testResults, last_user: lastUser, last_assistant: lastAssistant,
  };
}

function truncate(value, limit) { const text = String(value); return text.length <= limit ? text : text.slice(0, limit) + '…'; }
function textBlock(value, limit) { const text = String(value || '').trim(); return text.length <= limit ? text : text.slice(0, limit) + `\n…（已截断，原长 ${text.length} 字符）`; }

function renderItem(item, maxChars) {
  const ts = fmtTime(item.timestamp);
  if (item.kind === 'user_text') return [`### [用户] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'} ${ts}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果] ${item.name || 'tool'}${item.is_error ? ' (错误)' : ''} ${ts}`, textBlock(item.content, maxChars)];
  return [];
}

function renderSummary(meta, state, recentN, maxChars) {
  const norm = meta.normalized, timestamps = norm.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  const lines = [
    '# Resume-AGY 会话接管摘要', '', '## 会话信息', `- 标题: ${meta.title}`, `- 会话ID: ${meta.session_id}`,
    `- 项目: ${meta.directory || '(未知)'}`, '- 存储: Antigravity transcript.jsonl',
    `- 时间范围: ${fmtTime(timestamps[0] || meta.created)} ~ ${fmtTime(timestamps[timestamps.length - 1] || meta.updated)}`,
    `- 原始事件数: ${meta.count}`, `- 可见条目数: ${norm.items.length}`, '',
  ];
  if (norm.summaries.length) {
    lines.push('## 历史摘要（Checkpoint / Conversation History）');
    for (const value of norm.summaries) lines.push(textBlock(value, maxChars), '');
  }
  if (Object.keys(meta.artifacts).length) {
    lines.push('## Antigravity 任务产物');
    for (const [name, content] of Object.entries(meta.artifacts)) lines.push(`### ${name}`, textBlock(content, maxChars), '');
  }
  lines.push('## 任务状态重建', '', '### 目标', textBlock(state.goal, maxChars) || '(未识别)', '');
  for (const [title, key] of [['已调查文件', 'files_read'], ['代码修改', 'files_edited'], ['执行命令', 'commands']]) {
    if (state[key].length) { lines.push(`### ${title}`); for (const value of state[key]) lines.push(`- ${truncate(value, 200)}`); lines.push(''); }
  }
  if (state.test_results.length) {
    lines.push('### 测试 / 错误结果');
    for (const result of state.test_results.slice(-5)) lines.push(`-${result.is_error ? ' [错误]' : ''} ${truncate(result.content.trim().split(/\r?\n/)[0] || '', 200)}`);
    lines.push('');
  }
  lines.push('### 最近用户消息', textBlock(state.last_user, maxChars) || '(无)', '', '### 最近助手消息', textBlock(state.last_assistant, maxChars) || '(无)', '');
  const recent = recentN ? norm.items.slice(-recentN) : [];
  lines.push(`## 近期对话（最近 ${recent.length} 条）`, '');
  for (const item of recent) lines.push(...renderItem(item, maxChars), '');
  const olderTools = (recentN ? norm.items.slice(0, -recentN) : norm.items).filter((item) => item.kind === 'tool_use');
  if (olderTools.length) {
    lines.push('## 更早活动（工具调用，仅最近 60 条）');
    for (const item of olderTools.slice(-60)) {
      const input = item.input || {}, detail = commandText(input.CommandLine) || toolPath(String(item.name || ''), input);
      lines.push(`- [${fmtTime(item.timestamp)}] ${item.name || 'tool'}(${detail ? truncate(detail, 100) : '...'})`);
    }
    lines.push('');
  }
  lines.push('## 接管建议', '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。', '- 以「任务状态重建」「任务产物」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。', '- 不要逐字复述历史；基于现状决定下一步动作。', '');
  return lines.join('\n');
}

function projectSessions(sessions, project) { const target = normPath(project); return sessions.filter((item) => item.directory && normPath(item.directory) === target); }
function pickSession(sessions, sessionArg, project) {
  if (sessionArg) return sessions.find((item) => item.session_id.startsWith(sessionArg) || item.session_id.includes(sessionArg)) || null;
  return projectSessions(sessions, project)[0] || null;
}

function printList(sessions, project, limit) {
  const selected = projectSessions(sessions, project), shown = limit > 0 ? selected.slice(0, limit) : selected;
  console.log(`当前项目: ${project}`);
  console.log(`找到 ${selected.length} 个会话${shown.length < selected.length ? `（仅显示最近 ${shown.length} 个）` : ''}：\n`);
  shown.forEach((meta, index) => console.log(`${index === 0 ? '[最近]' : '      '} ${fmtTime(meta.updated) || '(无时间)'}  ${meta.session_id.slice(0, 12)}  事件数:${String(meta.count).padEnd(4)} 标题: ${meta.title}`));
}

function printHelp() {
  console.log(`用法: node resume_agy.js [选项]

读取 Antigravity CLI（agy）本地会话，生成结构化接管摘要。

选项:
  --list              仅列出当前项目会话
  --latest            取最近一个会话（默认）
  --session ID        指定会话 ID 或前缀；跨项目查找
  --conversation ID   --session 的别名
  --project PATH      项目路径，默认当前目录
  --agy-dir DIR       Antigravity 数据目录，默认 ~/.gemini/antigravity
  --recent N          近期条目数，默认 8
  --max-chars N       Markdown 摘要单条截断长度，默认 1500
  --limit N           --list 数量上限，0 不限制
  --json              输出 JSON
  --output FILE       将摘要写入 UTF-8 文件
  -h, --help          显示帮助`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: process.cwd(), agyDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
  const value = (flag, index) => { if (index + 1 >= argv.length) throw new Error(`${flag} 需要一个参数`); return argv[index + 1]; };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--list') args.list = true;
    else if (arg === '--latest') args.latest = true;
    else if (arg === '--json') args.json = true;
    else if (arg === '-h' || arg === '--help') args.help = true;
    else if (arg === '--session' || arg === '--conversation') args.session = value(arg, i++);
    else if (arg.startsWith('--session=')) args.session = arg.slice(10);
    else if (arg.startsWith('--conversation=')) args.session = arg.slice(15);
    else if (arg === '--project') args.project = value(arg, i++);
    else if (arg.startsWith('--project=')) args.project = arg.slice(10);
    else if (arg === '--agy-dir') args.agyDir = value(arg, i++);
    else if (arg.startsWith('--agy-dir=')) args.agyDir = arg.slice(10);
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
  const agyDir = path.resolve(args.agyDir || defaultAgyDir()), sessions = scanSessions(agyDir);
  if (!sessions.length) { console.error(`错误：在 ${path.join(agyDir, 'brain')} 未找到 Antigravity 会话。`); process.exit(1); }
  const project = path.resolve(args.project);
  if (args.list) {
    if (!projectSessions(sessions, project).length) { console.error(`错误：未找到项目 ${project} 的 Antigravity 会话。可用 --session ID 跨项目查找。`); process.exit(1); }
    printList(sessions, project, Math.max(args.limit, 0));
    return;
  }
  const target = pickSession(sessions, args.session, project);
  if (!target) { console.error(`错误：未匹配到会话 '${args.session || '当前项目'}'。`); process.exit(1); }
  const state = buildState(target.normalized.items), recentCount = Math.max(args.recent, 0);
  const output = args.json ? JSON.stringify({
    info: Object.fromEntries(['session_id', 'title', 'directory', 'created', 'updated', 'count', 'path'].map((key) => [key, target[key]])),
    state, summaries: target.normalized.summaries, artifacts: target.artifacts,
    recent_items: recentCount ? target.normalized.items.slice(-recentCount) : [],
  }, null, 2) : renderSummary(target, state, recentCount, Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output, 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
