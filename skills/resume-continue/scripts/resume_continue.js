#!/usr/bin/env node
// -*- coding: utf-8 -*-
/** 读取 Continue（VS Code/JetBrains 扩展与 cn CLI 共用）本地会话（~/.continue/sessions/*.json），生成接管摘要。 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const CST_OFFSET_MS = 8 * 3600 * 1000;

function normTool(name) {
  return String(name || '').replace(/[\s_-]+/g, '').toLowerCase();
}

// Continue 内置工具（新版 snake_case / 旧版 camelCase 两套命名并存），键为去掉分隔符的统一形式
const READ_TOOLS = new Set([
  'readfile', 'readfilerange', 'readcurrentlyopenfile',
  'grepsearch', 'fileglobsearch', 'globsearch', 'searchcodebase',
  'ls', 'codebase', 'codebasetool', 'readskill',
  'viewdiff', 'viewrepomap', 'viewsubdirectory',
  'searchweb', 'fetchurlcontent',
]);
const EDIT_TOOLS = new Set([
  'editexistingfile', 'editfile', 'singlefindandreplace', 'multiedit', 'createnewfile',
]);
const SHELL_TOOLS = new Set(['runterminalcommand', 'runcommand']);
// 未收录工具的命名启发式（MCP 等自定义工具）
const READ_HINT_RE = /read|grep|search|glob|list|\bls\b|view|fetch|web|codebase|docs?|skill/i;
const EDIT_HINT_RE = /edit|write|create|apply|patch|replace|insert/i;
const SHELL_HINT_RE = /terminal|command|exec|shell|bash|\brun\b|process/i;
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
const UNTITLED_TITLES = new Set(['', 'untitled session', 'new session', '新会话', '未命名会话']);
const PATH_KEYS = ['filepath', 'file_path', 'filepath_', 'path', 'file', 'filename', 'directory', 'directorypath', 'url', 'uri'];
const TOOL_STATUS_ERROR = new Set(['errored', 'error', 'failed']);

function defaultSessionsDirs() {
  const candidates = [];
  if (process.env.CONTINUE_DATA_DIR) candidates.push(path.resolve(process.env.CONTINUE_DATA_DIR));
  if (process.env.CONTINUE_GLOBAL_DIR) candidates.push(path.join(path.resolve(process.env.CONTINUE_GLOBAL_DIR), 'sessions'));
  candidates.push(path.join(os.homedir(), '.continue', 'sessions'));
  return candidates;
}

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (_) { return false; } }
function isDir(value) { try { return fs.statSync(value).isDirectory(); } catch (_) { return false; } }

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
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

function fileTimes(filePath) {
  const stat = fs.statSync(filePath);
  const birth = (stat.birthtimeMs && stat.birthtimeMs > 0) ? Math.trunc(stat.birthtimeMs) : 0;
  return [birth, Math.trunc(stat.mtimeMs)];
}

// sessions.json 里的 dateCreated（创建时刻，毫秒字符串）作为创建时间兜底
function sessionsListCreated(sessionsDir) {
  const byId = new Map();
  const entries = parseJson(readTextFile(path.join(sessionsDir, 'sessions.json')), null);
  if (Array.isArray(entries)) {
    for (const entry of entries) {
      if (entry && typeof entry === 'object' && typeof entry.sessionId === 'string') {
        byId.set(entry.sessionId, timestampMs(entry.dateCreated));
      }
    }
  }
  return byId;
}

function readTextFile(filePath) {
  try { return fs.readFileSync(filePath, 'utf-8'); } catch (_) { return ''; }
}

function scanSessions(sessionsDirs) {
  const sessions = [];
  for (const sessionsDir of sessionsDirs) {
    if (!isDir(sessionsDir)) continue;
    let entries = [];
    try {
      entries = fs.readdirSync(sessionsDir)
        .filter((name) => name.endsWith('.json') && name !== 'sessions.json')
        .map((name) => {
          const full = path.join(sessionsDir, name);
          try { return { full, mtime: fs.statSync(full).mtimeMs }; } catch (_) { return null; }
        })
        .filter(Boolean)
        .sort((a, b) => a.mtime - b.mtime);
    } catch (_) { continue; }
    let createdById = null;
    for (const { full } of entries) {
      const data = parseJson(readTextFile(full), null);
      if (!data || typeof data !== 'object' || !Array.isArray(data.history)) continue;
      let created = 0, updated = 0;
      try { [created, updated] = fileTimes(full); } catch (_) { /* ignore */ }
      const sessionId = String(data.sessionId || data.session_id || path.basename(full, '.json'));
      if (!created) {
        if (!createdById) createdById = sessionsListCreated(sessionsDir);
        created = createdById.get(sessionId) || 0;
      }
      sessions.push({
        session_id: sessionId,
        title: String(data.title || '').trim(),
        first_user: firstUserText(data.history),
        directory: String(data.workspaceDirectory || data.workspace || '').trim(),
        mode: String(data.mode || '').trim(),
        chat_model_title: String(data.chatModelTitle || '').trim(),
        usage: (data.usage && typeof data.usage === 'object') ? data.usage : {},
        created, updated,
        source: `continue-sessions:${path.basename(path.dirname(sessionsDir))}`,
        path: full,
        _history: data.history,
      });
    }
  }
  sessions.sort((a, b) => (b.updated || b.created) - (a.updated || a.created));
  return sessions;
}

function messageContentText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const part of content) {
    if (part && typeof part === 'object') {
      if (part.type === 'text') parts.push(String(part.text || ''));
      else if (part.type === 'imageUrl') parts.push('[图片]');
    } else if (typeof part === 'string') {
      parts.push(part);
    }
  }
  return parts.filter(Boolean).join('\n');
}

function toolStatusIsError(status) {
  return TOOL_STATUS_ERROR.has(String(status || '').toLowerCase());
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null) return value;
  }
  return null;
}

function contextItemsText(output) {
  if (!Array.isArray(output)) return output == null ? '' : String(output);
  const parts = [];
  for (const entry of output) {
    if (!entry || typeof entry !== 'object') parts.push(entry == null ? '' : String(entry));
    else if (entry.content) parts.push(String(entry.content));
    else if (entry.description) parts.push(String(entry.description));
  }
  return parts.filter(Boolean).join('\n');
}

function accumulateTokens(history) {
  let prompt = 0, completion = 0;
  for (const entry of history) {
    const message = (entry && typeof entry === 'object' && entry.message && typeof entry.message === 'object') ? entry.message : {};
    const usage = (message.usage && typeof message.usage === 'object') ? message.usage : {};
    prompt += Number(usage.promptTokens) || 0;
    completion += Number(usage.completionTokens) || 0;
  }
  return { input: Math.trunc(prompt), output: Math.trunc(completion) };
}

// 历史条目结构：{message:{role, content, toolCalls?}, contextItems, toolCallStates, conversationSummary?}
function normalizeHistory(history) {
  const items = [], summaries = [];
  const stateOutputs = new Set(); // 已由 role:"tool" 消息给出结果，避免与 toolCallStates.output 重复
  for (const entry of history) {
    if (!entry || typeof entry !== 'object') continue;
    const message = (entry.message && typeof entry.message === 'object') ? entry.message : {};
    const role = message.role || '';
    const summary = entry.conversationSummary;
    if (typeof summary === 'string' && summary.trim()) summaries.push(summary.trim());
    if (role === 'user') {
      const text = messageContentText(message.content).trim();
      if (text) items.push({ kind: 'user_text', timestamp: 0, text });
      for (const context of Array.isArray(entry.contextItems) ? entry.contextItems : []) {
        if (!context || typeof context !== 'object') continue;
        let label = String(context.name || '').trim();
        const uri = (context.uri && typeof context.uri === 'object') ? context.uri : {};
        const value = String(uri.value || '').trim();
        if (value && value !== label) label = label ? `${label} (${value})` : value;
        if (label) items.push({ kind: 'attachment', timestamp: 0, text: label });
      }
    } else if (role === 'assistant') {
      const text = messageContentText(message.content).trim();
      if (text) items.push({ kind: 'assistant_text', timestamp: 0, text });
      const states = Array.isArray(entry.toolCallStates) ? entry.toolCallStates : [];
      const stateIds = new Set();
      for (const state of states) {
        if (!state || typeof state !== 'object') continue;
        const call = (state.toolCall && typeof state.toolCall === 'object') ? state.toolCall : {};
        const fn = (call.function && typeof call.function === 'object') ? call.function : {};
        const name = String(fn.name || state.toolCallId || 'tool');
        const callId = String(call.id || state.toolCallId || '');
        stateIds.add(callId);
        let input = firstDefined(state.parsedArgs, state.processedArgs, parseJson(fn.arguments, null));
        if (!input || typeof input !== 'object' || Array.isArray(input)) {
          input = (input == null || input === '') ? {} : { raw: input };
        }
        items.push({ kind: 'tool_use', timestamp: 0, name, input, tool_use_id: callId });
        const status = state.status;
        if (!stateOutputs.has(callId) && (state.output !== undefined && state.output !== null || toolStatusIsError(status))) {
          items.push({
            kind: 'tool_result',
            timestamp: 0,
            tool_use_id: callId,
            content: contextItemsText(state.output),
            is_error: toolStatusIsError(status),
          });
        }
      }
      // 旧版本仅在 message.toolCalls 上记录调用（无状态对象）
      for (const call of Array.isArray(message.toolCalls) ? message.toolCalls : []) {
        if (!call || typeof call !== 'object') continue;
        const fn = (call.function && typeof call.function === 'object') ? call.function : {};
        const callId = String(call.id || '');
        if (callId && stateIds.has(callId)) continue;
        const name = String(fn.name || 'tool');
        let input = parseJson(fn.arguments, null);
        if (!input || typeof input !== 'object' || Array.isArray(input)) {
          input = (input == null || input === '') ? {} : { raw: input };
        }
        items.push({ kind: 'tool_use', timestamp: 0, name, input, tool_use_id: callId });
      }
    } else if (role === 'tool') {
      const callId = String(message.toolCallId || message.tool_call_id || '');
      if (callId) stateOutputs.add(callId);
      items.push({
        kind: 'tool_result',
        timestamp: 0,
        tool_use_id: callId,
        content: messageContentText(message.content),
        is_error: false,
      });
    }
    // thinking / system 角色不进入接管摘要
  }
  return { items, summaries, tokens: accumulateTokens(history) };
}

function classifyTool(name) {
  const lowered = normTool(name);
  if (!lowered) return 'other';
  if (SHELL_TOOLS.has(lowered) || SHELL_HINT_RE.test(lowered)) return 'shell';
  if (EDIT_TOOLS.has(lowered) || EDIT_HINT_RE.test(lowered)) return 'edit';
  if (READ_TOOLS.has(lowered) || READ_HINT_RE.test(lowered)) return 'read';
  return 'other';
}

function toolFile(name, input) {
  for (const key of PATH_KEYS) {
    const value = input[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (Array.isArray(value) && value.length) return String(value[0]);
  }
  for (const value of Object.values(input)) {
    if (typeof value === 'string' && (value.includes('/') || value.includes('\\')) && value.length < 260) return value;
  }
  for (const key of ['query', 'pattern', 'search']) {
    if (input[key]) return String(input[key]);
  }
  return '';
}

function shellCommand(input) {
  for (const key of ['command', 'cmd', 'commandline']) {
    const value = input[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function dedupe(values) {
  const seen = new Set(), output = [];
  for (const value of values) {
    if (value && !seen.has(value)) { seen.add(value); output.push(value); }
  }
  return output;
}

function buildState(items) {
  const filesRead = [], filesEdited = [], commands = [], testResults = [];
  const calls = {};
  let firstUser = '', lastUser = '', lastAssistant = '';
  for (const item of items) {
    const kind = item.kind;
    if (kind === 'user_text') {
      firstUser = firstUser || item.text;
      lastUser = item.text;
    } else if (kind === 'assistant_text') {
      lastAssistant = item.text;
    } else if (kind === 'tool_use') {
      const name = normTool(item.name);
      const input = item.input || {};
      calls[item.tool_use_id || `#${Object.keys(calls).length}`] = [name, input];
      const category = classifyTool(name);
      if (category === 'read') filesRead.push(toolFile(name, input));
      else if (category === 'edit') filesEdited.push(toolFile(name, input));
      else if (category === 'shell') commands.push(shellCommand(input));
    } else if (kind === 'tool_result') {
      const call = calls[item.tool_use_id] || ['', {}];
      const content = item.content || '';
      const command = (SHELL_TOOLS.has(call[0]) || SHELL_HINT_RE.test(call[0])) ? shellCommand(call[1]) : '';
      if (content.trim() && (
        item.is_error || TEST_CMD_RE.test(command) || TEST_RESULT_RE.test(content.slice(0, 2000))
      )) {
        testResults.push({ command_hint: command || call[0], is_error: !!item.is_error, content });
      }
    }
  }
  return {
    goal: firstUser,
    files_read: dedupe(filesRead),
    files_edited: dedupe(filesEdited),
    commands: dedupe(commands),
    test_results: testResults,
    last_user: lastUser,
    last_assistant: lastAssistant,
  };
}

function truncate(value, limit) {
  const text = String(value);
  return text.length <= limit ? text : text.slice(0, limit) + '…';
}

function textBlock(value, limit) {
  const text = String(value || '').trim();
  if (text.length <= limit) return text;
  return text.slice(0, limit) + `\n…（已截断，原长 ${text.length} 字符）`;
}

function toolBrief(item) {
  const name = item.name || 'tool';
  const input = item.input || {};
  const detail = shellCommand(input) || toolFile(name, input);
  return detail ? `${name}(${truncate(detail, 100)})` : `${name}(...)`;
}

function renderItem(item, maxChars) {
  const kind = item.kind;
  if (kind === 'user_text') return [`### [用户]`, textBlock(item.text, maxChars)];
  if (kind === 'assistant_text') return [`### [助手]`, textBlock(item.text, maxChars)];
  if (kind === 'tool_use') {
    return [
      `### [工具调用] ${item.name || 'tool'}`,
      '```json',
      truncate(JSON.stringify(item.input || {}), maxChars),
      '```',
    ];
  }
  if (kind === 'tool_result') {
    const error = item.is_error ? ' (错误)' : '';
    return [`### [工具结果]${error}`, textBlock(item.content, maxChars)];
  }
  if (kind === 'attachment') return ['### [附件]', item.text || ''];
  return [];
}

function formatTokens(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const parts = [];
  const prompt = Number(usage.promptTokens) || 0;
  const completion = Number(usage.completionTokens) || 0;
  if (prompt) parts.push(`输入 ${prompt}`);
  if (completion) parts.push(`输出 ${completion}`);
  const details = (usage.promptTokensDetails && typeof usage.promptTokensDetails === 'object') ? usage.promptTokensDetails : {};
  const cached = Number(details.cachedTokens) || 0;
  if (cached) parts.push(`缓存读 ${cached}`);
  return parts.join(' / ');
}

function firstUserText(history) {
  for (const entry of history) {
    if (!entry || typeof entry !== 'object') continue;
    const message = (entry.message && typeof entry.message === 'object') ? entry.message : {};
    if (message.role !== 'user') continue;
    const text = messageContentText(message.content).trim();
    if (!text) continue;
    return text.split('\n')[0].slice(0, 120);
  }
  return '';
}

function resolveTitle(meta) {
  const title = (meta.title || '').trim();
  if (!UNTITLED_TITLES.has(title.toLowerCase())) return title;
  return meta.first_user || meta.session_id;
}

function renderSummary(meta, items, state, summaries, tokens, recentN, maxChars) {
  const usage = meta.usage || {};
  const lines = [
    '# Resume-Continue 会话接管摘要',
    '',
    '## 会话信息',
    `- 标题: ${truncate(resolveTitle(meta), 120)}`,
    `- 会话ID: ${meta.session_id}`,
    `- 项目: ${meta.directory || '(未知)'}`,
    `- 存储: ${meta.path}`,
  ];
  if (meta.mode) lines.push(`- 模式: ${meta.mode}`);
  if (meta.chat_model_title) lines.push(`- 模型: ${meta.chat_model_title}`);
  const cost = Number(usage.totalCost) || 0;
  if (cost) lines.push(`- 累计费用: $${cost.toFixed(4)}`);
  let tokenLine = formatTokens(usage);
  if (!tokenLine && tokens.input) {
    const parts = [];
    if (tokens.input) parts.push(`输入 ${tokens.input}`);
    if (tokens.output) parts.push(`输出 ${tokens.output}`);
    tokenLine = parts.join(' / ');
  }
  if (tokenLine) lines.push(`- Token 用量: ${tokenLine}`);
  lines.push(`- 时间范围: ${fmtTime(meta.created) || '(未知)'} ~ ${fmtTime(meta.updated) || '(未知)'}（会话文件时间，条目本身无时间戳）`);
  lines.push(`- 消息条目数: ${items.length}`);
  lines.push('');
  if (summaries.length) {
    lines.push('## 历史摘要（原会话 compact）');
    for (const value of summaries.slice(-3)) lines.push(`- ${truncate(value, maxChars)}`);
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
      const first = (result.content.trim().split('\n')[0] || '');
      const prefix = result.is_error ? ' [错误]' : '';
      lines.push(`-${prefix} ${truncate(first, 200)}`);
    }
    lines.push('');
  }
  lines.push('### 最近用户消息', textBlock(state.last_user, maxChars) || '(无)');
  lines.push('', '### 最近助手消息', textBlock(state.last_assistant, maxChars) || '(无)', '');
  const recent = recentN ? items.slice(-recentN) : [];
  lines.push(`## 近期对话（最近 ${recent.length} 条）`, '');
  for (const item of recent) {
    lines.push(...renderItem(item, maxChars));
    lines.push('');
  }
  const olderTools = recentN ? items.slice(0, Math.max(items.length - recentN, 0)).filter((item) => item.kind === 'tool_use') : [];
  if (olderTools.length) {
    lines.push('## 更早活动（工具调用，仅最近 60 条）');
    for (const item of olderTools.slice(-60)) lines.push(`- ${toolBrief(item)}`);
    lines.push('');
  }
  lines.push(
    '## 接管建议',
    '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。',
    '- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。',
    '- 不要逐字复述历史；基于现状决定下一步动作。',
    '- 若想让 Continue 自己原生续接：CLI 用 `cn --resume`（最近会话）或 `cn ls` 选择；IDE 在 Continue 侧边栏的会话历史中选中该会话。',
    '',
  );
  return lines.join('\n');
}

function projectSessions(sessions, projectPath) {
  const target = normPath(projectPath);
  return sessions.filter((meta) => normPath(meta.directory) === target);
}

function pickSession(sessions, sessionArg, projectPath) {
  if (sessionArg) {
    return sessions.find((meta) => meta.session_id.startsWith(sessionArg) || meta.session_id.includes(sessionArg)) || null;
  }
  const selected = projectSessions(sessions, projectPath);
  return selected[0] || null;
}

function printList(sessions, projectPath, limit) {
  const selected = projectSessions(sessions, projectPath);
  const shown = limit > 0 ? selected.slice(0, limit) : selected;
  console.log(`当前项目: ${projectPath}`);
  const suffix = shown.length < selected.length ? `（仅显示最近 ${shown.length} 个）` : '';
  console.log(`找到 ${selected.length} 个会话${suffix}：\n`);
  shown.forEach((meta, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    const timeLabel = fmtTime(meta.updated) || fmtTime(meta.created) || '(无时间)';
    console.log(`${mark} ${timeLabel}  ${meta.session_id.slice(0, 16)}  标题: ${resolveTitle(meta)}`);
  });
}

function resolveSessionsDir(configured) {
  if (!configured) return null;
  const resolved = path.resolve(configured);
  if (path.basename(resolved) === 'sessions' && isDir(resolved)) return resolved;
  const nested = path.join(resolved, 'sessions');
  return isDir(nested) ? nested : resolved;
}

function buildArgv() {
  const argv = { _: [], list: false, latest: false, session: null, project: process.cwd(), 'continue-dir': null, recent: 8, 'max-chars': 1500, limit: 0, json: false, output: null };
  const args = process.argv.slice(2);
  const valueFlags = new Set(['--session', '--project', '--continue-dir', '--recent', '--max-chars', '--limit', '--output']);
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (valueFlags.has(arg)) {
      const value = args[++i];
      if (value === undefined) { console.error(`错误：${arg} 需要一个值。`); process.exit(2); }
      argv[arg.slice(2)] = value;
    } else if (arg === '--list') argv.list = true;
    else if (arg === '--latest') argv.latest = true;
    else if (arg === '--json') argv.json = true;
    else if (arg === '-h' || arg === '--help') { printHelp(); process.exit(0); }
    else if (arg.startsWith('--')) { console.error(`错误：未知参数 ${arg}`); process.exit(2); }
    else argv._.push(arg);
  }
  if (argv._.length) { console.error(`错误：无法识别的位置参数 ${argv._.join(' ')}`); process.exit(2); }
  return argv;
}

function printHelp() {
  console.log(`用法: resume_continue.js [--list|--latest|--session ID] [--project PATH]
                           [--continue-dir DIR] [--recent N] [--max-chars N]
                           [--limit N] [--json] [--output FILE]

读取 Continue（VS Code/JetBrains 扩展与 cn CLI）本地会话，生成结构化接管摘要。

选项:
  --list              仅列出当前项目会话
  --latest            取最近一个会话（默认）
  --session ID        指定会话 ID 或前缀；跨项目查找
  --project PATH      项目路径，默认当前目录
  --continue-dir DIR  Continue 主目录（~/.continue）或 sessions 目录
  --recent N          近期条目数，默认 8
  --max-chars N       单条截断长度，默认 1500
  --limit N           --list 数量上限，0 不限制
  --json              输出 JSON
  --output FILE       将摘要写入文件
  -h, --help          显示帮助

数据来源:
  ~/.continue/sessions/*.json（IDE 扩展与 CLI 共用；受 CONTINUE_GLOBAL_DIR 环境变量影响）`);
}

function main() {
  const argv = buildArgv();
  const configured = resolveSessionsDir(argv['continue-dir']);
  const sessionsDirs = configured ? [configured] : defaultSessionsDirs();
  if (!sessionsDirs.some((dir) => isDir(dir))) {
    console.error(`错误：未找到 Continue 本地会话目录（已探测 ${sessionsDirs.join('、')}）。`);
    console.error('可用 --continue-dir 指定 ~/.continue 主目录或 sessions 目录。');
    process.exit(1);
  }
  const sessions = scanSessions(sessionsDirs);
  if (!sessions.length) {
    console.error('错误：sessions 目录下未找到任何 Continue 会话（*.json）。');
    process.exit(1);
  }
  const projectPath = path.resolve(argv.project);
  if (argv.list) {
    if (!projectSessions(sessions, projectPath).length) {
      console.error(`错误：未找到项目 ${projectPath} 的 Continue 会话。可用 --session ID 跨项目查找。`);
      process.exit(1);
    }
    printList(sessions, projectPath, Number(argv.limit) || 0);
    return;
  }
  const target = pickSession(sessions, argv.session, projectPath);
  if (!target) {
    console.error(`错误：未匹配到会话 '${argv.session || '当前项目'}'。`);
    process.exit(1);
  }
  const meta = { ...target };
  const history = meta._history;
  delete meta._history;
  const normalized = normalizeHistory(history);
  const items = normalized.items;
  const state = buildState(items);
  const recentCount = Math.max(Number(argv.recent) || 0, 0);
  const recentItems = recentCount ? items.slice(-recentCount) : [];
  const info = {
    session_id: meta.session_id,
    title: resolveTitle(meta),
    directory: meta.directory,
    mode: meta.mode,
    chat_model_title: meta.chat_model_title,
    source: meta.source,
    path: meta.path,
    created: fmtTime(meta.created),
    updated: fmtTime(meta.updated),
    cost: (meta.usage && Number(meta.usage.totalCost)) || 0,
    tokens: (meta.usage && Object.keys(meta.usage).length ? meta.usage : normalized.tokens),
  };
  let output;
  if (argv.json) {
    output = JSON.stringify({ info, state, summaries: normalized.summaries, recent_items: recentItems }, null, 2);
  } else {
    output = renderSummary(meta, items, state, normalized.summaries, normalized.tokens, recentCount, Math.max(Number(argv['max-chars']) || 1, 1));
  }
  if (argv.output) {
    fs.writeFileSync(argv.output, output.endsWith('\n') ? output : output + '\n', 'utf-8');
    console.error(`摘要已写入：${argv.output}`);
  } else {
    process.stdout.write(output.endsWith('\n') ? output : output + '\n');
  }
}

main();
