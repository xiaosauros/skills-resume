#!/usr/bin/env node
// -*- coding: utf-8 -*-
/** 读取 MiniMax Code（mcode）本地会话记录，生成接管摘要。
 *
 * 数据源（按优先级）：
 *   1. SQLite 会话库 <主目录>/v2/sqlite/runtime-state.sqlite（local_runtime_sessions /
 *      local_runtime_message_rows / local_runtime_messages）；
 *   2. 规范历史 JSONL <主目录>/v2/sessions/<日期>/<时间>-session_<id>/messages.jsonl
 *      （信封格式：message_id / turn_id / message{role,content,timestamp} / turn_config）；
 *   3. 仅有 JSONL 时（无 SQLite 支持或库不可读），扫描 manifest.json 列出会话。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const CST_OFFSET_MS = 8 * 3600 * 1000;
const READ_TOOLS = new Set(['read', 'grep', 'glob', 'find', 'ls', 'list', 'web_search', 'tool_search', 'webfetch']);
const EDIT_TOOLS = new Set(['write', 'edit', 'apply_patch', 'applypatch', 'multiedit', 'notebookedit']);
const SHELL_TOOLS = new Set(['bash', 'shell', 'shell_command', 'terminal', 'run_command']);
const TODO_STATUS_LABELS = { completed: '已完成', in_progress: '进行中', pending: '待办' };
const SKIP_KINDS = new Set(['peek', 'channel']);
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;

function defaultMinimaxDir() {
  for (const envName of ['MINIMAX_DATA_DIR', 'MAVIS_DATA_DIR']) {
    const value = process.env[envName];
    if (value && value.trim()) return value.trim();
  }
  const home = os.homedir();
  const primary = path.join(home, '.minimax');
  if (fs.existsSync(primary)) return primary;
  const legacy = path.join(home, '.mavis');
  if (fs.existsSync(legacy)) return legacy;
  return primary;
}

function dbPath(dataDir) { return path.join(dataDir, 'v2', 'sqlite', 'runtime-state.sqlite'); }
function historyRoot(dataDir) { return path.join(dataDir, 'v2', 'sessions'); }

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (_) { return false; } }

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function parseJsonString(value) {
  if (typeof value !== 'string' || !value) return '';
  try {
    const parsed = JSON.parse(value);
    if (typeof parsed === 'string') return parsed;
    return JSON.stringify(parsed);
  } catch (_) { return value; }
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

function openDb(dbFile) {
  if (!DatabaseSync) throw new Error('no node:sqlite');
  return new DatabaseSync(dbFile, { readOnly: true });
}

function sessionsDirCandidates(dataDir) {
  const root = historyRoot(dataDir);
  let days;
  try { days = fs.readdirSync(root, { withFileTypes: true }); } catch (_) { return []; }
  const dirs = [];
  for (const day of days) {
    if (!day.isDirectory()) continue;
    const dayPath = path.join(root, day.name);
    let months;
    try { months = fs.readdirSync(dayPath, { withFileTypes: true }); } catch (_) { continue; }
    for (const month of months) {
      if (!month.isDirectory()) continue;
      const monthPath = path.join(dayPath, month.name);
      let times;
      try { times = fs.readdirSync(monthPath, { withFileTypes: true }); } catch (_) { continue; }
      for (const time of times) {
        if (!time.isDirectory()) continue;
        dirs.push(path.join(monthPath, time.name));
      }
    }
  }
  return dirs;
}

function manifestOf(sessionDir) {
  const file = path.join(sessionDir, 'manifest.json');
  if (!isFile(file)) return null;
  const value = parseJson(fs.readFileSync(file, 'utf8'), null);
  if (!value || typeof value.sessionId !== 'string' || !value.sessionId) return null;
  return value;
}

/** 从规范历史 JSONL 提取首条真实用户消息文本（用于兜底标题）。 */
function firstUserText(messagesFile) {
  let text = '';
  try {
    const lines = fs.readFileSync(messagesFile, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      if (!line.trim()) continue;
      const envelope = parseJson(line, null);
      const message = envelope && envelope.message;
      if (!message || message.role !== 'user') continue;
      if (message.archonCompaction) continue;
      text = userTextOf(message);
      if (text.trim()) break;
    }
  } catch (_) { /* 忽略读取失败 */ }
  return text;
}

function userTextOf(message) {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n');
}

function scanSessionsFiles(dataDir) {
  const metas = [];
  for (const sessionDir of sessionsDirCandidates(dataDir)) {
    const manifest = manifestOf(sessionDir);
    if (!manifest) continue;
    const messagesFile = path.join(sessionDir, 'messages.jsonl');
    let updated = timestampMs(manifest.updatedAtMs);
    if (!updated) {
      try { updated = fs.statSync(messagesFile).mtimeMs; } catch (_) { updated = 0; }
    }
    metas.push({
      session_id: manifest.sessionId,
      title: '',
      directory: '',
      agent: '',
      model: '',
      kind: '',
      status: '',
      archived: false,
      parent_id: typeof manifest.parentSessionId === 'string' ? manifest.parentSessionId : null,
      created: timestampMs(manifest.createdAtMs),
      updated,
      source: 'files',
      history_dir: sessionDir,
    });
  }
  metas.sort((a, b) => (b.updated || 0) - (a.updated || 0));
  return metas;
}

function scanSessionsDb(dataDir) {
  const file = dbPath(dataDir);
  if (!isFile(file) || !DatabaseSync) return null;
  let db;
  try {
    db = openDb(file);
  } catch (_) { return null; } // WAL 遗留等只读打开失败时退回文件扫描
  try {
    const rows = db.prepare('SELECT * FROM local_runtime_sessions').all();
    const metas = [];
    for (const row of rows) {
      if (row.visibility === 'hidden') continue;
      if (SKIP_KINDS.has(row.session_kind)) continue;
      const record = parseJson(row.record_json, {});
      metas.push({
        session_id: row.session_id,
        title: row.title || '',
        directory: row.workspace_dir || row.project_workspace_dir || record.workspaceDir || '',
        project_dir: row.project_workspace_dir || '',
        agent: row.agent_name || record.agentName || '',
        model: record.effectiveModel || '',
        kind: row.session_kind || record.sessionKind || '',
        status: row.status || record.status || '',
        archived: !!row.archived,
        parent_id: row.parent_session_id || record.parentSessionId || null,
        created: timestampMs(row.created_at_ms ?? record.createdAtMs),
        updated: timestampMs(row.updated_at_ms ?? record.updatedAtMs),
        source: 'sqlite',
        history_dir: row.history_relative_dir
          ? path.join(historyRoot(dataDir), row.history_relative_dir)
          : '',
      });
    }
    metas.sort((a, b) => ((a.archived ? 1 : 0) - (b.archived ? 1 : 0)) || ((b.updated || 0) - (a.updated || 0)));
    return metas;
  } catch (_) {
    return null;
  } finally { try { db.close(); } catch (_) { /* 已关闭 */ } }
}

function scanSessions(dataDir) {
  const fromDb = scanSessionsDb(dataDir);
  if (Array.isArray(fromDb)) return { sessions: fromDb, usedDb: true };
  return { sessions: scanSessionsFiles(dataDir), usedDb: false };
}

function findSessionDir(dataDir, sessionId) {
  for (const sessionDir of sessionsDirCandidates(dataDir)) {
    const manifest = manifestOf(sessionDir);
    if (manifest && manifest.sessionId === sessionId) return sessionDir;
  }
  return '';
}

/** 信封消息 → 统一条目（user_text / assistant_text / tool_use / tool_result / compaction / attachment）。 */
function normalizeEnvelopes(envelopes) {
  const items = [];
  const todos = [];
  let model = '';
  let compactionCount = 0;
  let latestCompaction = '';
  for (const envelope of envelopes) {
    const message = envelope.message || {};
    const ts = timestampMs(message.timestamp);
    const role = message.role || '';
    if (role === 'user') {
      const marker = message.archonCompaction;
      if (marker && typeof marker === 'object') {
        compactionCount += 1;
        latestCompaction = typeof marker.summary === 'string' ? marker.summary : latestCompaction;
        items.push({ kind: 'compaction', timestamp: ts, text: marker.summary || '(压缩边界)' });
        if (Array.isArray(marker.todoState)) {
          todos.length = 0;
          for (const todo of marker.todoState) {
            if (todo && typeof todo.content === 'string') {
              todos.push({ content: todo.content, status: String(todo.status || ''), priority: todo.priority });
            }
          }
        }
        continue;
      }
      const content = message.content;
      if (Array.isArray(content)) {
        let index = 0;
        for (const block of content) {
          if (block && block.type === 'image') {
            index += 1;
            items.push({ kind: 'attachment', timestamp: ts, text: `[图片 #${index}]` });
          }
        }
      }
      const text = userTextOf(message);
      if (text.trim()) items.push({ kind: 'user_text', timestamp: ts, text });
    } else if (role === 'assistant') {
      if (typeof message.model === 'string' && message.model) model = message.model;
      if (Array.isArray(message.content)) {
        for (const block of message.content) {
          if (!block) continue;
          if (block.type === 'text' && typeof block.text === 'string' && block.text.trim()) {
            items.push({ kind: 'assistant_text', timestamp: ts, text: block.text });
          } else if (block.type === 'toolCall' && block.id) {
            items.push({
              kind: 'tool_use',
              timestamp: ts,
              name: block.name || 'tool',
              input: parseJson(block.arguments, {}),
              tool_use_id: block.id,
            });
          }
        }
      }
    } else if (role === 'toolResult') {
      const content = Array.isArray(message.content)
        ? message.content
          .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
          .map((block) => block.text)
          .join('\n')
        : (typeof message.content === 'string' ? message.content : '');
      items.push({
        kind: 'tool_result',
        timestamp: ts,
        tool_use_id: message.toolCallId || '',
        content,
        is_error: !!message.isError,
      });
    } else if (role === 'compactionSummary') {
      compactionCount += 1;
      latestCompaction = typeof message.summary === 'string' ? message.summary : latestCompaction;
      items.push({ kind: 'compaction', timestamp: ts, text: message.summary || '(压缩摘要)' });
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, model, todos, compactionCount, latestCompaction };
}

/** 会话库 message 行（display message）→ 统一条目。 */
function normalizeRows(rows) {
  const items = [];
  let compactionCount = 0;
  let latestCompaction = '';
  for (const row of rows) {
    const data = parseJson(row.data_json, {});
    const role = data.role || row.role || '';
    const ts = timestampMs(data.timestamp ?? data.created_at ?? row.created_at_ms);
    if (role === 'user') {
      const text = typeof data.msg_content === 'string' ? data.msg_content : '';
      if (text.trim()) items.push({ kind: 'user_text', timestamp: ts, text });
      if (Array.isArray(data.attachments)) {
        let index = 0;
        for (const attachment of data.attachments) {
          index += 1;
          const name = (attachment && ((attachment.meta || {}).fileName || attachment.fileName || attachment.filePath)) || `#${index}`;
          items.push({ kind: 'attachment', timestamp: ts, text: `[附件] ${name}` });
        }
      }
    } else if (role === 'assistant') {
      const text = typeof data.msg_content === 'string' ? data.msg_content : '';
      if (text.trim()) items.push({ kind: 'assistant_text', timestamp: ts, text });
      if (Array.isArray(data.tool_calls)) {
        for (const call of data.tool_calls) {
          if (!call || typeof call.tool_call_id !== 'string') continue;
          items.push({
            kind: 'tool_use',
            timestamp: ts,
            name: call.tool_name || 'tool',
            input: parseJson(call.tool_call_args, {}),
            tool_use_id: call.tool_call_id,
          });
          const status = call.tool_call_status;
          if (status === 2 || status === 3) {
            items.push({
              kind: 'tool_result',
              timestamp: ts,
              tool_use_id: call.tool_call_id,
              content: parseJsonString(call.tool_call_result_data),
              is_error: status === 3,
            });
          }
        }
      }
    } else if (data.kind === 'compaction' && typeof data.msg_content === 'string' && data.msg_content.trim()) {
      compactionCount += 1;
      latestCompaction = data.msg_content;
      items.push({ kind: 'compaction', timestamp: ts, text: data.msg_content });
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, model: '', todos: [], compactionCount, latestCompaction };
}

function readEnvelopes(messagesFile) {
  const envelopes = [];
  const lines = fs.readFileSync(messagesFile, 'utf8').split(/\r?\n/);
  for (const line of lines) {
    if (!line.trim()) continue;
    const envelope = parseJson(line, null);
    if (envelope && envelope.message && typeof envelope.message === 'object') envelopes.push(envelope);
  }
  return envelopes;
}

function loadMessagesJsonl(dataDir, meta) {
  let file = meta.history_dir ? path.join(meta.history_dir, 'messages.jsonl') : '';
  if (!isFile(file)) {
    const sessionDir = findSessionDir(dataDir, meta.session_id);
    file = sessionDir ? path.join(sessionDir, 'messages.jsonl') : '';
  }
  if (!isFile(file)) return null;
  return normalizeEnvelopes(readEnvelopes(file));
}

function loadMessagesRows(dataDir, sessionId) {
  const file = dbPath(dataDir);
  if (!isFile(file) || !DatabaseSync) return null;
  let db;
  try { db = openDb(file); } catch (_) { return null; }
  try {
    const rows = db.prepare(
      'SELECT msg_id, role, created_at_ms, data_json FROM local_runtime_message_rows WHERE session_id=? ORDER BY id',
    ).all(sessionId);
    const normalized = normalizeRows(rows);
    if (normalized.items.length) return normalized;
    const blob = db.prepare(
      'SELECT display_messages_json FROM local_runtime_messages WHERE session_id=?',
    ).get(sessionId);
    if (blob && blob.display_messages_json) {
      const display = parseJson(blob.display_messages_json, []);
      if (Array.isArray(display)) {
        const rowsLike = display.map((message, index) => ({
          msg_id: message.msg_id || String(index),
          role: message.role || null,
          created_at_ms: timestampMs(message.timestamp ?? message.created_at),
          data_json: JSON.stringify(message),
        }));
        return normalizeRows(rowsLike);
      }
    }
    return normalized;
  } catch (_) {
    return null;
  } finally { try { db.close(); } catch (_) { /* 已关闭 */ } }
}

function loadSession(dataDir, meta) {
  let normalized = loadMessagesJsonl(dataDir, meta);
  if (!normalized && meta.source === 'sqlite') normalized = loadMessagesRows(dataDir, meta.session_id);
  if (!normalized) normalized = { items: [], model: '', todos: [], compactionCount: 0, latestCompaction: '' };
  if (!meta.model && normalized.model) meta.model = normalized.model;
  if (!meta.title) {
    const firstUser = normalized.items.find((item) => item.kind === 'user_text');
    meta.title = firstUser ? firstUser.text : meta.session_id;
  }
  meta.title = meta.title.split(/\r?\n/)[0].trim() || meta.session_id;
  const timestamps = normalized.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  return {
    info: {
      session_id: meta.session_id,
      title: meta.title,
      directory: meta.directory,
      agent: meta.agent,
      model: meta.model,
      kind: meta.kind,
      status: meta.status,
      archived: meta.archived,
      parent_id: meta.parent_id,
      source: meta.source,
      first_ts: fmtTime(timestamps[0] || meta.created),
      last_ts: fmtTime(timestamps[timestamps.length - 1] || meta.updated),
    },
    normalized,
  };
}

function toolFile(name, input) {
  for (const key of ['filePath', 'file_path', 'path', 'filename']) if (input[key]) return String(input[key]);
  if (['glob', 'grep', 'find'].includes(name)) return String(input.pattern || input.query || input.regex || '');
  return '';
}

function shellCommand(input) { return String(input.command || input.cmd || '').trim(); }

function dedupe(values) {
  const seen = new Set(), output = [];
  for (const value of values) if (value && !seen.has(value)) { seen.add(value); output.push(value); }
  return output;
}

function buildState(items, todos) {
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
      const content = item.content || '', command = SHELL_TOOLS.has(name) ? shellCommand(input) : '';
      if ((item.is_error || TEST_CMD_RE.test(command) || TEST_RESULT_RE.test(content.slice(0, 2000))) && content.trim()) {
        testResults.push({ command_hint: command || name, is_error: !!item.is_error, content });
      }
    }
  }
  return {
    goal: firstUser, files_read: dedupe(filesRead), files_edited: dedupe(filesEdited),
    commands: dedupe(commands), test_results: testResults, last_user: lastUser, last_assistant: lastAssistant,
    todos,
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
  const detail = shellCommand(input) || toolFile(String(name).toLowerCase(), input) || String(input.url || input.query || '');
  return detail ? `${name}(${truncate(detail, 100)})` : `${name}(...)`;
}

function renderItem(item, maxChars) {
  const ts = fmtTime(item.timestamp);
  if (item.kind === 'user_text') return [`### [用户] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'} ${ts}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果]${item.is_error ? ' (错误)' : ''} ${ts}`, textBlock(item.content, maxChars)];
  if (item.kind === 'compaction') return [`### [压缩摘要] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'attachment') return [`### [附件] ${ts}`, item.text || ''];
  return [];
}

function todoLabel(status) { return TODO_STATUS_LABELS[status] || status || '未知'; }

function renderSummary(session, state, recentN, maxChars) {
  const info = session.info, norm = session.normalized;
  const lines = [
    '# Resume-MiniMax 会话接管摘要', '', '## 会话信息',
    `- 标题: ${info.title}`, `- 会话ID: ${info.session_id}`,
    `- 项目: ${info.directory || '(未知)'}`, `- 存储: ${info.source}`,
  ];
  if (info.agent) lines.push(`- Agent: ${info.agent}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  if (info.kind) lines.push(`- 会话类型: ${info.kind}`);
  if (info.status) lines.push(`- 状态: ${info.status}`);
  if (info.parent_id) lines.push(`- 父会话: ${info.parent_id}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`, `- 消息条目数: ${norm.items.length}`, '');
  if (norm.compactionCount > 0) {
    lines.push(`## 历史摘要（会话内共 ${norm.compactionCount} 次压缩，最近一次内容）`, textBlock(norm.latestCompaction, maxChars) || '(无)', '');
  }
  lines.push('## 任务状态重建', '', '### 目标', textBlock(state.goal, maxChars) || '(未识别)', '');
  if (state.todos.length) {
    lines.push(`### 任务清单（来自压缩快照，共 ${state.todos.length} 项）`);
    for (const todo of state.todos) lines.push(`- [${todoLabel(todo.status)}] ${truncate(todo.content, 200)}`);
    lines.push('');
  }
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
  for (const item of recent) lines.push(...renderItem(item, maxChars), '');
  const olderTools = recentN ? norm.items.slice(0, -recentN).filter((item) => item.kind === 'tool_use') : [];
  if (olderTools.length) {
    lines.push('## 更早活动（工具调用，仅最近 60 条）');
    for (const item of olderTools.slice(-60)) lines.push(`- [${fmtTime(item.timestamp)}] ${toolBrief(item)}`);
    lines.push('');
  }
  lines.push('## 接管建议', '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。', '- 优先核对「任务清单」中未完成项，再以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。', '- 也可以直接用 `mcode --resume <会话ID>`（或当前目录下 `mcode -c`）让 MiniMax Code 原生续接本会话。', '- 不要逐字复述历史；基于现状决定下一步动作。', '');
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
  const selected = projectSessions(sessions, projectPath), shown = limit > 0 ? selected.slice(0, limit) : selected;
  console.log(`当前项目: ${projectPath}`);
  console.log(`找到 ${selected.length} 个会话${shown.length < selected.length ? `（仅显示最近 ${shown.length} 个）` : ''}：\n`);
  shown.forEach((meta, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    console.log(`${mark} ${fmtTime(meta.updated) || '(无时间)'}  ${meta.session_id.slice(0, 12)}  标题: ${meta.title || '(未命名)'}`);
  });
}

function printHelp() {
  console.log(`用法: node resume_minimax.js [选项]

读取 MiniMax Code（mcode）本地会话，生成结构化接管摘要。

选项:
  --list                 仅列出当前项目会话
  --latest               取最近一个会话（默认）
  --session ID           指定会话 ID 或前缀；跨项目查找
  --project PATH         项目路径，默认当前目录
  --minimax-dir DIR      MiniMax Code 主目录（默认 ~/.minimax，兼容 ~/.mavis）
  --recent N             近期条目数，默认 8
  --max-chars N          单条截断长度，默认 1500
  --limit N              --list 数量上限，0 不限制
  --json                 输出 JSON
  --output FILE          将摘要写入文件
  -h, --help             显示帮助`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: process.cwd(), minimaxDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
  const value = (flag, index) => { if (index + 1 >= argv.length) throw new Error(`${flag} 需要一个参数`); return argv[index + 1]; };
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
    else if (arg === '--minimax-dir') args.minimaxDir = value(arg, i++);
    else if (arg.startsWith('--minimax-dir=')) args.minimaxDir = arg.slice(14);
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
  const dataDir = path.resolve(args.minimaxDir || defaultMinimaxDir());
  const { sessions, usedDb } = scanSessions(dataDir);
  if (!sessions.length) {
    console.error(`错误：在 ${dataDir} 未找到 MiniMax Code 会话（已检查 v2/sqlite/runtime-state.sqlite 与 v2/sessions/）。`);
    console.error('若安装在自定义目录，请用 --minimax-dir 指定，或设置 MINIMAX_DATA_DIR 环境变量。');
    process.exit(1);
  }
  if (!usedDb) {
    if (!DatabaseSync && isFile(dbPath(dataDir))) {
      console.error('提示：当前 Node 不支持 node:sqlite（需要 22.5+），仅从会话 JSONL 读取；低版本 Node 可改用 Python 实现获得完整数据。');
    } else {
      console.error('提示：SQLite 会话库不可用，仅从会话 JSONL 读取（标题与项目路径信息可能不完整）。');
    }
  }
  const projectPath = path.resolve(args.project);
  if (args.list) {
    const selected = projectSessions(sessions, projectPath);
    if (!selected.length) {
      console.error(`错误：未找到项目 ${projectPath} 的 MiniMax Code 会话。可用 --session ID 跨项目查找。`);
      process.exit(1);
    }
    for (const meta of selected) {
      if (meta.title) continue;
      const normalized = loadMessagesJsonl(dataDir, meta)
        || (meta.source === 'sqlite' ? loadMessagesRows(dataDir, meta.session_id) : null);
      const firstUser = normalized && normalized.items.find((item) => item.kind === 'user_text');
      meta.title = firstUser ? firstUser.text.split(/\r?\n/)[0].trim() : '';
    }
    printList(sessions, projectPath, args.limit);
    return;
  }
  const target = pickSession(sessions, args.session, projectPath);
  if (!target) { console.error(`错误：未匹配到会话 '${args.session || '当前项目'}'。`); process.exit(1); }
  let session;
  try { session = loadSession(dataDir, target); } catch (error) { console.error('错误：解析会话失败：' + error.message); process.exit(1); }
  const state = buildState(session.normalized.items, session.normalized.todos);
  const recentCount = Math.max(args.recent, 0);
  const recentItems = recentCount ? session.normalized.items.slice(-recentCount) : [];
  const output = args.json
    ? JSON.stringify({ info: session.info, state, recent_items: recentItems }, null, 2)
    : renderSummary(session, state, recentCount, Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output, 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
