#!/usr/bin/env node
// -*- coding: utf-8 -*-
/** 读取 Kilo Code 本地会话（kilo.db SQLite 或旧版 VS Code 扩展任务目录），生成接管摘要。 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const CST_OFFSET_MS = 8 * 3600 * 1000;
const READ_TOOLS = new Set(['read', 'glob', 'grep', 'list', 'codesearch']);
const EDIT_TOOLS = new Set(['write', 'edit', 'patch', 'apply_patch', 'multiedit']);
const SHELL_TOOLS = new Set(['bash', 'shell', 'shell_command', 'terminal', 'execute_command']);
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
// Kilo/Cline 旧版任务在用户消息外包裹的环境与系统块，不属于任务内容本身
const LEGACY_BLOCK_RE = /<(environment_details|system-reminder|fetch_instructions|notice|custom_instructions|rules)[\s\S]*?<\/\1>/g;
const LEGACY_TASK_RE = /<task>([\s\S]*?)<\/task>/;
const LEGACY_TASK_RE_G = /<task>([\s\S]*?)<\/task>/g;

function defaultDataDirCandidates() {
  const candidates = [];
  if (process.env.KILO_DATA_DIR) candidates.push(process.env.KILO_DATA_DIR);
  if (process.env.XDG_DATA_HOME) candidates.push(path.join(process.env.XDG_DATA_HOME, 'kilo'));
  candidates.push(path.join(os.homedir(), '.local', 'share', 'kilo'));
  if (process.env.LOCALAPPDATA) candidates.push(path.join(process.env.LOCALAPPDATA, 'kilo'));
  return candidates;
}

// Kilo 扩展可能装在 VS Code 及其各类分支中，逐个探测 globalStorage
function editorGlobalStorageRoots() {
  const editors = ['Code', 'Code - Insiders', 'VSCodium', 'Cursor', 'Windsurf', 'Trae'];
  const roots = [];
  if (process.platform === 'darwin') {
    for (const editor of editors) roots.push(path.join(os.homedir(), 'Library', 'Application Support', editor, 'User', 'globalStorage'));
  } else if (process.platform === 'win32') {
    const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    for (const editor of editors) roots.push(path.join(appData, editor, 'User', 'globalStorage'));
  } else {
    for (const editor of editors) roots.push(path.join(os.homedir(), '.config', editor, 'User', 'globalStorage'));
  }
  return roots;
}

function defaultTasksRoots() {
  const roots = [];
  if (process.env.KILO_TASKS_DIR) roots.push(path.resolve(process.env.KILO_TASKS_DIR));
  for (const root of editorGlobalStorageRoots()) roots.push(path.join(root, 'kilocode.kilo-code', 'tasks'));
  return roots;
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

function requireSqlite() {
  if (!DatabaseSync) {
    console.error('错误：当前 Node 不支持 node:sqlite（需要 Node.js 22.5+）。');
    console.error('请改用 Python：python -X utf8 scripts/resume_kilo.py ...');
    process.exit(1);
  }
}

function openDb(dbPath) {
  requireSqlite();
  if (!isFile(dbPath)) throw new Error('数据库不存在：' + dbPath);
  return new DatabaseSync(dbPath, { readOnly: true });
}

function tableNames(db) {
  return new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((row) => row.name));
}

// Database.path()：正式渠道为 kilo.db，开发渠道为 kilo-<channel>.db，并回退 opencode-<channel>.db
function findDatabases(dataDir) {
  if (process.env.KILO_DB) {
    const configured = path.isAbsolute(process.env.KILO_DB)
      ? process.env.KILO_DB
      : path.join(dataDir, process.env.KILO_DB);
    return isFile(configured) ? [configured] : [];
  }
  if (!isDir(dataDir)) return [];
  const candidates = fs.readdirSync(dataDir)
    .filter((name) => /^(kilo|opencode)[^/\\]*\.db$/i.test(name))
    .map((name) => {
      const full = path.join(dataDir, name);
      let mtime = 0;
      try { mtime = fs.statSync(full).mtimeMs; } catch (_) { /* ignore */ }
      return { full, mtime };
    })
    .sort((a, b) => b.mtime - a.mtime);
  return candidates.map((item) => item.full);
}

function dbSessions(dbPath) {
  if (!isFile(dbPath) || !DatabaseSync) return [];
  let db;
  try {
    db = openDb(dbPath);
    const tables = tableNames(db);
    if (!tables.has('session')) return [];
    const workspace = tables.has('workspace');
    const rows = db.prepare(`
      SELECT s.*, p.worktree AS project_worktree, p.name AS project_name
      ${workspace ? ', w.directory AS workspace_directory' : ''}
      FROM session s
      LEFT JOIN project p ON p.id = s.project_id
      ${workspace ? 'LEFT JOIN workspace w ON w.id = s.workspace_id' : ''}
      ORDER BY s.time_updated DESC
    `).all();
    return rows.map((row) => ({
      session_id: row.id,
      title: row.title || row.slug || row.id,
      directory: row.directory || row.workspace_directory || row.project_worktree || '',
      project_id: row.project_id,
      project_name: row.project_name || '',
      parent_id: row.parent_id || null,
      version: row.version || '',
      agent: row.agent || '',
      model: row.model || '',
      created: row.time_created,
      updated: row.time_updated,
      archived: row.time_archived,
      cost: row.cost,
      summary_additions: row.summary_additions, summary_deletions: row.summary_deletions, summary_files: row.summary_files,
      tokens: {
        input: row.tokens_input, output: row.tokens_output,
        reasoning: row.tokens_reasoning, cache_read: row.tokens_cache_read, cache_write: row.tokens_cache_write,
      },
      source: `sqlite:${path.basename(dbPath)}`,
      path: dbPath,
      tables,
    }));
  } catch (_) { return []; }
  finally { if (db) db.close(); }
}

function legacyTaskRoots(configured) {
  if (configured) {
    const resolved = path.resolve(configured);
    if (path.basename(resolved) === 'tasks') return [resolved];
    const nested = path.join(resolved, 'kilocode.kilo-code', 'tasks');
    if (isDir(nested)) return [nested];
    return [path.join(resolved, 'tasks')];
  }
  return defaultTasksRoots();
}

// 与扩展迁移逻辑一致：迁移后的会话 ID = ses_migrated_<sha1(旧任务ID)[:26]>
function migratedSessionId(taskId) {
  return 'ses_migrated_' + crypto.createHash('sha1').update(String(taskId)).digest('hex').slice(0, 26);
}

function legacyTaskDate(id) {
  const value = Number(id);
  return Number.isFinite(value) && value > 1_000_000_000_000 ? value : 0;
}

function readIndexEntries(tasksRoot) {
  const entries = parseJson(readTextFile(path.join(tasksRoot, '_index.json')), {});
  const byId = new Map();
  for (const entry of (Array.isArray(entries.entries) ? entries.entries : [])) {
    if (entry && typeof entry === 'object' && typeof entry.id === 'string') byId.set(entry.id, entry);
  }
  return byId;
}

function readTextFile(filePath) {
  try { return fs.readFileSync(filePath, 'utf-8'); } catch (_) { return ''; }
}

function readJsonArray(filePath) {
  const text = readTextFile(filePath);
  if (!text) return null;
  try {
    const value = JSON.parse(text);
    return Array.isArray(value) ? value : null;
  } catch (_) { return null; }
}

function legacySessions(tasksRoots, knownIds) {
  const sessions = [];
  for (const tasksRoot of tasksRoots) {
    if (!isDir(tasksRoot)) continue;
    let dirs = [];
    try { dirs = fs.readdirSync(tasksRoot); } catch (_) { continue; }
    const indexById = readIndexEntries(tasksRoot);
    for (const name of dirs) {
      const taskDir = path.join(tasksRoot, name);
      if (!isDir(taskDir)) continue;
      const apiFile = path.join(taskDir, 'api_conversation_history.json');
      const history = readJsonArray(apiFile);
      if (!history) continue;
      if (knownIds.has(name) || knownIds.has(migratedSessionId(name))) continue;
      const stored = parseJson(readTextFile(path.join(taskDir, 'history_item.json')), null);
      const indexed = indexById.get(name) || {};
      const messages = history.filter((entry) => entry && typeof entry === 'object');
      const directory = String((stored && stored.workspace) || indexed.workspace || '').trim();
      const created = timestampMs((stored && stored.ts) ?? indexed.ts) || legacyTaskDate(name) || 0;
      let updated = created;
      try { updated = Math.max(created, fs.statSync(taskDir).mtimeMs); } catch (_) { /* ignore */ }
      sessions.push({
        session_id: name,
        title: String((stored && stored.task) || indexed.task || '').trim().slice(0, 120),
        directory,
        project_id: '', project_name: '', parent_id: null,
        version: '', agent: 'code', model: '',
        created, updated, archived: 0, cost: 0,
        tokens: { input: 0, output: 0, reasoning: 0, cache_read: 0, cache_write: 0 },
        summary_additions: null, summary_deletions: null, summary_files: null,
        source: `legacy-tasks:${path.basename(path.dirname(path.dirname(tasksRoot)))}`,
        path: apiFile,
        tables: null,
        message_count: messages.length,
        _history: messages,
      });
    }
  }
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

function scanSessions(dataDirs, tasksRoot) {
  const seen = new Set(), sessions = [];
  for (const dataDir of dataDirs) {
    for (const dbPath of findDatabases(dataDir)) {
      for (const meta of dbSessions(dbPath)) {
        if (seen.has(meta.session_id)) continue;
        seen.add(meta.session_id);
        sessions.push(meta);
      }
    }
  }
  sessions.push(...legacySessions(legacyTaskRoots(tasksRoot), seen));
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

function messageTime(data, fallback) { return timestampMs((data.time || {}).created) || timestampMs(fallback); }
function partTime(data, fallback) { return timestampMs((data.time || {}).start) || timestampMs(fallback); }

// v1 存储：message（role 数据）+ part（text/tool/patch/file 分片）
function normalizeV1(messages, partsByMessage) {
  const items = [], summaries = [];
  let model = '', provider = '', agent = '';
  for (const message of messages) {
    const data = message.data, role = data.role || '', ts = messageTime(data, message.time_created);
    const modelInfo = data.model || {};
    if (data.modelID || modelInfo.modelID) { model = data.modelID || modelInfo.modelID; }
    else if (typeof data.model === 'string' && data.model) { model = data.model; }
    if (data.providerID || modelInfo.providerID) { provider = data.providerID || modelInfo.providerID; }
    agent = data.agent || agent;
    for (const part of partsByMessage.get(message.id) || []) {
      const pdata = part.data, type = pdata.type || '', pts = partTime(pdata, part.time_created) || ts;
      if (type === 'text') {
        const text = pdata.text || '';
        if (!text.trim()) continue;
        if (pdata.synthetic || data.summary === true) summaries.push(text);
        else items.push({ kind: `${role}_text`, timestamp: pts, text });
      } else if (type === 'reasoning') {
        continue;
      } else if (type === 'tool') {
        const state = pdata.state || {}, name = pdata.tool || 'tool', callId = pdata.callID || '';
        items.push({ kind: 'tool_use', timestamp: pts, name, input: state.input || {}, tool_use_id: callId });
        const status = String(state.status || ''), outputValue = state.output, error = state.error;
        if (outputValue !== undefined || error !== undefined || ['completed', 'error'].includes(status)) {
          const output = typeof outputValue === 'string' ? outputValue : (outputValue == null ? '' : JSON.stringify(outputValue));
          items.push({
            kind: 'tool_result',
            timestamp: timestampMs((state.time || {}).end) || pts,
            tool_use_id: callId,
            content: error !== undefined ? String(error) : output,
            is_error: status === 'error' || error !== undefined,
          });
        }
      } else if (type === 'patch') {
        items.push({ kind: 'patch', timestamp: pts, files: pdata.files || [] });
      } else if (type === 'file') {
        const label = pdata.filename || (pdata.source || {}).path || (pdata.source || {}).uri || '附件';
        items.push({ kind: 'attachment', timestamp: pts, text: String(label) });
      }
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, summaries, model, provider, agent };
}

function toolResultText(state) {
  if (state.error !== undefined) return typeof state.error === 'string' ? state.error : JSON.stringify(state.error);
  if (state.output !== undefined && state.output !== null && typeof state.output !== 'object') return String(state.output);
  const content = state.content;
  if (!Array.isArray(content)) return state.output == null ? '' : JSON.stringify(state.output);
  return content.map((entry) => {
    if (!entry || typeof entry !== 'object') return String(entry ?? '');
    if (entry.type === 'text') return String(entry.text || '');
    return JSON.stringify(entry);
  }).filter(Boolean).join('\n');
}

// v2 投影存储（session_message）：data 为带类型的完整消息
function normalizeV2(rows) {
  const items = [], summaries = [];
  let model = '', provider = '', agent = '';
  const sorted = rows.slice().sort((a, b) =>
    (a.seq ?? timestampMs(a.time_created)) - (b.seq ?? timestampMs(b.time_created)) || String(a.id).localeCompare(String(b.id)));
  for (const row of sorted) {
    const data = row.data || {}, ts = timestampMs((data.time || {}).created) || timestampMs(row.time_created);
    if (row.type === 'user') {
      const text = String(data.text || '').trim();
      if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      for (const file of Array.isArray(data.files) ? data.files : []) {
        const label = (file && (file.name || file.uri)) || (typeof file === 'string' ? file : '') || '附件';
        items.push({ kind: 'attachment', timestamp: ts, text: String(label) });
      }
    } else if (row.type === 'synthetic' || row.type === 'compaction') {
      const text = String(data.summary || data.kilo_summary || data.text || '').trim();
      if (text) summaries.push(text);
    } else if (row.type === 'shell') {
      const command = String(data.command || '').trim();
      items.push({ kind: 'tool_use', timestamp: ts, name: 'bash', input: { command }, tool_use_id: data.callID || `shell-${row.id}` });
      items.push({
        kind: 'tool_result',
        timestamp: timestampMs((data.time || {}).completed) || ts,
        tool_use_id: data.callID || `shell-${row.id}`,
        content: String(data.output || ''),
        is_error: false,
      });
    } else if (row.type === 'assistant') {
      for (const entry of Array.isArray(data.content) ? data.content : []) {
        if (!entry || typeof entry !== 'object') continue;
        if (entry.type === 'text') {
          if (String(entry.text || '').trim()) items.push({ kind: 'assistant_text', timestamp: ts, text: String(entry.text) });
        } else if (entry.type === 'tool') {
          const state = entry.state || {}, callId = entry.id || '';
          items.push({ kind: 'tool_use', timestamp: ts, name: entry.name || 'tool', input: state.input || {}, tool_use_id: callId });
          const status = String(state.status || '');
          if (['completed', 'error'].includes(status)) {
            items.push({
              kind: 'tool_result',
              timestamp: timestampMs((entry.time || {}).completed) || ts,
              tool_use_id: callId,
              content: toolResultText(state),
              is_error: status === 'error',
            });
          }
        }
      }
    } else if (row.type === 'model-switched') {
      const ref = data.model || {};
      if (ref.modelID) model = ref.modelID;
      if (ref.providerID) provider = ref.providerID;
    } else if (row.type === 'agent-switched') {
      if (data.agent) agent = data.agent;
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, summaries, model, provider, agent };
}

function dbTodos(db, tables, sessionId) {
  if (!tables || !tables.has('todo')) return [];
  try {
    return db.prepare('SELECT content, status, priority FROM todo WHERE session_id=? ORDER BY position').all(sessionId)
      .map((row) => ({ content: row.content, status: row.status, priority: row.priority }));
  } catch (_) { return []; }
}

function loadDbSession(meta) {
  const db = openDb(meta.path);
  try {
    const tables = meta.tables || tableNames(db);
    let normalized = { items: [], summaries: [], model: meta.model || '', provider: '', agent: meta.agent || '' };
    if (tables.has('message') && tables.has('part')) {
      const messages = db.prepare('SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id')
        .all(meta.session_id).map((row) => ({ id: row.id, time_created: row.time_created, data: parseJson(row.data) }));
      const partsByMessage = new Map();
      for (const row of db.prepare('SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created, id').all(meta.session_id)) {
        if (!partsByMessage.has(row.message_id)) partsByMessage.set(row.message_id, []);
        partsByMessage.get(row.message_id).push({ id: row.id, time_created: row.time_created, data: parseJson(row.data) });
      }
      if (messages.length) {
        normalized = normalizeV1(messages, partsByMessage);
        if (!normalized.model && meta.model) normalized.model = meta.model;
        if (!normalized.agent && meta.agent) normalized.agent = meta.agent;
      }
    }
    if (!normalized.items.length && tables.has('session_message')) {
      const rows = db.prepare('SELECT id, type, seq, time_created, data FROM session_message WHERE session_id=?')
        .all(meta.session_id).map((row) => ({
          id: row.id, type: row.type, seq: row.seq, time_created: row.time_created, data: parseJson(row.data),
        }));
      if (rows.length) {
        const v2 = normalizeV2(rows);
        if (!v2.model && meta.model) v2.model = meta.model;
        if (!v2.agent && meta.agent) v2.agent = meta.agent;
        normalized = v2;
      }
    }
    return { normalized, todos: dbTodos(db, tables, meta.session_id) };
  } finally { db.close(); }
}

function stripLegacyNoise(text) {
  return String(text || '')
    .replace(LEGACY_BLOCK_RE, '')
    .replace(/\s+\n/g, '\n')
    .trim();
}

function legacyTextFromContent(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((block) => block && typeof block === 'object' && block.type === 'text' && block.text)
    .map((block) => block.text).join('\n');
}

function legacyFirstTask(messages) {
  for (const message of messages) {
    if (!message || message.role !== 'user') continue;
    let text = legacyTextFromContent(message.content);
    if (!text.trim()) continue;
    const task = text.match(LEGACY_TASK_RE);
    if (task) text = task[1];
    text = stripLegacyNoise(text);
    if (!text) continue;
    const first = text.split(/\r?\n/)[0] || '';
    return first.length > 120 ? first.slice(0, 120) : first;
  }
  return '';
}

// 旧版 VS Code 扩展任务：api_conversation_history.json 为 Anthropic 风格消息数组
function normalizeLegacy(messages) {
  const items = [], summaries = [];
  let lastAssistant = '';
  for (const message of messages) {
    if (!message || typeof message !== 'object') continue;
    const role = message.role || '';
    const content = message.content;
    if (role === 'user') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === 'object' && block.type === 'tool_result') {
            const text = typeof block.content === 'string'
              ? block.content
              : Array.isArray(block.content)
                ? block.content.map((entry) => (entry && typeof entry === 'object' ? String(entry.text || JSON.stringify(entry)) : String(entry ?? ''))).join('\n')
                : String(block.content ?? '');
            items.push({ kind: 'tool_result', timestamp: 0, tool_use_id: block.tool_use_id || '', content: text, is_error: !!block.is_error });
          }
        }
      }
      const text = stripLegacyNoise(legacyTextFromContent(content).replace(LEGACY_TASK_RE_G, '\n$1\n'));
      if (text) items.push({ kind: 'user_text', timestamp: 0, text });
    } else if (role === 'assistant') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== 'object') continue;
          if (block.type === 'text') {
            const text = String(block.text || '');
            if (text.trim()) { items.push({ kind: 'assistant_text', timestamp: 0, text }); lastAssistant = text; }
          } else if (block.type === 'tool_use') {
            items.push({ kind: 'tool_use', timestamp: 0, name: block.name || 'tool', input: block.input || {}, tool_use_id: block.id || '' });
          }
        }
      }
    }
  }
  return { items, summaries, model: '', provider: '', agent: 'code' };
}

function loadSession(meta) {
  let normalized, todos = [];
  if (meta.source.startsWith('sqlite:')) {
    ({ normalized, todos } = loadDbSession(meta));
  } else {
    normalized = normalizeLegacy(meta._history || []);
  }
  if (!normalized.model && meta.model) normalized.model = meta.model;
  if (!normalized.agent && meta.agent) normalized.agent = meta.agent;
  const timestamps = normalized.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  return {
    info: {
      session_id: meta.session_id, title: meta.title, directory: meta.directory,
      project_id: meta.project_id, project_name: meta.project_name, parent_id: meta.parent_id,
      version: meta.version, model: normalized.model, provider: normalized.provider, agent: normalized.agent,
      source: meta.source,
      first_ts: fmtTime(timestamps[0] || meta.created),
      last_ts: fmtTime(timestamps[timestamps.length - 1] || meta.updated),
      archived: !!meta.archived,
      cost: meta.cost || 0,
      tokens: meta.tokens || {},
      summary: { additions: meta.summary_additions, deletions: meta.summary_deletions, files: meta.summary_files },
    },
    normalized,
    todos,
  };
}

function toolFile(name, input) {
  for (const key of ['filePath', 'file_path', 'path', 'filename']) if (input[key]) return String(input[key]);
  if (['glob', 'grep', 'codesearch'].includes(name)) return String(input.pattern || input.query || '');
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
    else if (item.kind === 'patch') filesEdited.push(...(item.files || []).map(String));
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
    commands: dedupe(commands), test_results: testResults, todos: todos || [],
    last_user: lastUser, last_assistant: lastAssistant,
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
  const time = ts ? ` ${ts}` : '';
  if (item.kind === 'user_text') return [`### [用户]${time}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手]${time}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'}${time}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果]${item.is_error ? ' (错误)' : ''}${time}`, textBlock(item.content, maxChars)];
  if (item.kind === 'patch') return [`### [代码补丁]${time}`, (item.files || []).map((name) => `- ${name}`).join('\n')];
  if (item.kind === 'attachment') return [`### [附件]${time}`, item.text || ''];
  return [];
}

function formatTokens(tokens) {
  const parts = [];
  for (const [key, label] of [['input', '输入'], ['output', '输出'], ['reasoning', '推理'], ['cache_read', '缓存读']]) {
    if (tokens && tokens[key]) parts.push(`${label} ${tokens[key]}`);
  }
  return parts.join(' / ');
}

function renderSummary(session, state, recentN, maxChars) {
  const info = session.info, norm = session.normalized;
  const lines = [
    '# Resume-Kilo 会话接管摘要', '', '## 会话信息',
    `- 标题: ${info.title}`, `- 会话ID: ${info.session_id}`,
    `- 项目: ${info.directory || '(未知)'}`, `- 存储: ${info.source}`,
  ];
  if (info.agent) lines.push(`- Agent: ${info.agent}`);
  if (info.model) lines.push(`- 模型: ${info.provider ? info.provider + '/' : ''}${info.model}`);
  if (info.version) lines.push(`- Kilo 版本: ${info.version}`);
  if (info.parent_id) lines.push(`- 父会话: ${info.parent_id}`);
  if (info.archived) lines.push('- 状态: 已归档');
  if (info.cost) lines.push(`- 累计费用: $${Number(info.cost).toFixed(4)}`);
  const tokens = formatTokens(info.tokens);
  if (tokens) lines.push(`- Token 用量: ${tokens}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`, `- 消息条目数: ${norm.items.length}`, '');
  if (norm.summaries.length) {
    lines.push('## 历史摘要（原会话 compact）');
    for (const value of norm.summaries) lines.push(`- ${truncate(value, maxChars)}`);
    lines.push('');
  }
  lines.push('## 任务状态重建', '', '### 目标', textBlock(state.goal, maxChars) || '(未识别)', '');
  if (state.todos.length) {
    lines.push('### 任务清单（todo）');
    for (const todo of state.todos) {
      const mark = todo.status === 'completed' ? '[x]' : (todo.status === 'in_progress' ? '[~]' : '[ ]');
      lines.push(`- ${mark} ${truncate(todo.content, 200)}${todo.status && todo.status !== 'pending' ? `（${todo.status}）` : ''}`);
    }
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
    for (const item of olderTools.slice(-60)) lines.push(`- [${fmtTime(item.timestamp) || '(无时间)'}] ${toolBrief(item)}`);
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
  const selected = projectSessions(sessions, projectPath), shown = limit > 0 ? selected.slice(0, limit) : selected;
  console.log(`当前项目: ${projectPath}`);
  console.log(`找到 ${selected.length} 个会话${shown.length < selected.length ? `（仅显示最近 ${shown.length} 个）` : ''}：\n`);
  shown.forEach((meta, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    console.log(`${mark} ${fmtTime(meta.updated) || '(无时间)'}  ${meta.session_id.slice(0, 16)}  标题: ${meta.title}`);
  });
}

function printHelp() {
  console.log(`用法: node resume_kilo.js [选项]

读取 Kilo Code 本地会话，生成结构化接管摘要。

数据来源（按优先级）:
  1. Kilo 数据目录下的 kilo.db（CLI 与新版 VS Code 扩展，opencode 风格 SQLite）
  2. 旧版 VS Code 扩展任务目录 globalStorage/kilocode.kilo-code/tasks

选项:
  --list              仅列出当前项目会话
  --latest            取最近一个会话（默认）
  --session ID        指定会话 ID 或前缀；跨项目、跨来源查找
  --project PATH      项目路径，默认当前目录
  --kilo-dir DIR      Kilo 数据目录（含 kilo.db 的一级），默认 ~/.local/share/kilo
  --tasks-dir DIR     旧版任务目录或其上层（globalStorage），默认自动探测
  --recent N          近期条目数，默认 8
  --max-chars N       单条截断长度，默认 1500
  --limit N           --list 数量上限，0 不限制
  --json              输出 JSON
  --output FILE       将摘要写入文件
  -h, --help          显示帮助`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: process.cwd(), kiloDir: null, tasksDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
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
    else if (arg === '--kilo-dir') args.kiloDir = value(arg, i++);
    else if (arg.startsWith('--kilo-dir=')) args.kiloDir = arg.slice(11);
    else if (arg === '--tasks-dir') args.tasksDir = value(arg, i++);
    else if (arg.startsWith('--tasks-dir=')) args.tasksDir = arg.slice(12);
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
  const dataDirs = args.kiloDir ? [path.resolve(args.kiloDir)] : defaultDataDirCandidates();
  const hasKiloData = args.kiloDir
    || !!process.env.KILO_DB
    || dataDirs.some((dir) => isDir(dir))
    || legacyTaskRoots(args.tasksDir).some((dir) => isDir(dir));
  if (!hasKiloData) {
    console.error(`错误：未找到 Kilo Code 本地数据（已探测 ${dataDirs.join('、')} 与旧版扩展任务目录）。`);
    console.error('可用 --kilo-dir / --tasks-dir 指定。');
    process.exit(1);
  }
  const sessions = scanSessions(dataDirs, args.tasksDir);
  if (!sessions.length) {
    console.error('错误：未找到任何 Kilo 会话（已检查 kilo.db 与旧版扩展任务目录）。');
    process.exit(1);
  }
  const projectPath = path.resolve(args.project);
  if (args.list) {
    if (!projectSessions(sessions, projectPath).length) {
      console.error(`错误：未找到项目 ${projectPath} 的 Kilo 会话。可用 --session ID 跨项目查找。`);
      process.exit(1);
    }
    printList(sessions, projectPath, args.limit);
    return;
  }
  const target = pickSession(sessions, args.session, projectPath);
  if (!target) { console.error(`错误：未匹配到会话 '${args.session || '当前项目'}'。`); process.exit(1); }
  let session;
  try { session = loadSession(target); } catch (error) { console.error('错误：解析会话失败：' + error.message); process.exit(1); }
  const state = buildState(session.normalized.items, session.todos);
  const recentCount = Math.max(args.recent, 0);
  const recentItems = recentCount ? session.normalized.items.slice(-recentCount) : [];
  const output = args.json
    ? JSON.stringify({ info: session.info, state, summaries: session.normalized.summaries, recent_items: recentItems }, null, 2)
    : renderSummary(session, state, recentCount, Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output, 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
