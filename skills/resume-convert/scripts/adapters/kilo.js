#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/kilo: Kilo Code（kilo.db SQLite 或旧版 VS Code 扩展任务目录）-> 归一化会话。
 *
 * 格式依据（与 resume-kilo 一致）：
 *   1. Kilo 数据目录下的 kilo.db（opencode 风格 SQLite）
 *      - v1 存储：message + part（text/reasoning/tool/patch/file 分片）
 *      - v2 投影：session_message（user/synthetic/compaction/shell/assistant 等）
 *   2. 旧版 VS Code 扩展任务目录 kilocode.kilo-code/tasks/<任务ID>/api_conversation_history.json
 * 转换语义：文本 -> mMessage；工具 -> mToolUse/mToolResult；reasoning 与 patch/附件跳过；
 * synthetic/compaction 摘要 -> mNote。标题：session.title/slug 或 history_item.task。
 * 与 kilo.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const cs = require('../claude_session.js');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

// 正式渠道 kilo.db，开发渠道 kilo-<channel>.db，回退 opencode-*.db
const DB_NAME_RE = /^(kilo|opencode)[^/\\]*\.db$/i;
// 旧版任务在用户消息外包裹的环境与系统块，不属于任务内容本身
const LEGACY_BLOCK_RE = /<(environment_details|system-reminder|fetch_instructions|notice|custom_instructions|rules)[\s\S]*?<\/\1>/g;
const LEGACY_TASK_RE = /<task>([\s\S]*?)<\/task>/;
const LEGACY_TASK_RE_G = /<task>([\s\S]*?)<\/task>/g;

// ---------------------------------------------------------------- 基础工具

function defaultDir() {
  if (process.env.KILO_DATA_DIR) return path.resolve(process.env.KILO_DATA_DIR);
  return path.join(os.homedir(), '.local', 'share', 'kilo');
}

function dataDirCandidates() {
  const candidates = [];
  if (process.env.KILO_DATA_DIR) candidates.push(path.resolve(process.env.KILO_DATA_DIR));
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
    for (const editor of editors) {
      roots.push(path.join(os.homedir(), 'Library', 'Application Support', editor, 'User', 'globalStorage'));
    }
  } else if (process.platform === 'win32') {
    const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    for (const editor of editors) roots.push(path.join(appData, editor, 'User', 'globalStorage'));
  } else {
    for (const editor of editors) {
      roots.push(path.join(os.homedir(), '.config', editor, 'User', 'globalStorage'));
    }
  }
  return roots;
}

function defaultTasksRoots() {
  const roots = [];
  if (process.env.KILO_TASKS_DIR) roots.push(path.resolve(process.env.KILO_TASKS_DIR));
  for (const root of editorGlobalStorageRoots()) {
    roots.push(path.join(root, 'kilocode.kilo-code', 'tasks'));
  }
  return roots;
}

function legacyTaskRoots(configured) {
  if (configured) {
    const resolved = path.resolve(String(configured));
    if (path.basename(resolved) === 'tasks') return [resolved];
    const nested = path.join(resolved, 'kilocode.kilo-code', 'tasks');
    if (isDir(nested)) return [nested];
    return [path.join(resolved, 'tasks')];
  }
  return defaultTasksRoots();
}

function normPath(p) {
  return path.normalize(String(p)).toLowerCase();
}

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (_) { return false; } }
function isDir(value) { try { return fs.statSync(value).isDirectory(); } catch (_) { return false; } }

function parseJson(value) {
  if (value && typeof value === 'object') return value;
  if (value === null || value === undefined || value === '') return {};
  if (typeof value === 'string' && !value.trim()) return {};
  try { return JSON.parse(value); } catch (_) { return {}; }
}

function timestampMs(value) {
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) return Math.abs(number) < 1e10 ? Math.trunc(number * 1000) : Math.trunc(number);
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function msToIso(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString();
}

// ---------------------------------------------------------------- 数据库来源

function openDb(dbPath) {
  if (!DatabaseSync) return null;
  if (!isFile(dbPath)) return null;
  return new DatabaseSync(dbPath, { readOnly: true });
}

function tableNames(db) {
  return new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((row) => row.name));
}

function findDatabases(dataDir) {
  if (process.env.KILO_DB) {
    const configured = path.isAbsolute(process.env.KILO_DB)
      ? process.env.KILO_DB
      : path.join(dataDir, process.env.KILO_DB);
    return isFile(configured) ? [configured] : [];
  }
  if (!isDir(dataDir)) return [];
  const candidates = fs.readdirSync(dataDir)
    .filter((name) => DB_NAME_RE.test(name))
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
  if (!isFile(dbPath)) return [];
  if (!DatabaseSync) {
    // node:sqlite 不可用（需 Node 22.5+）时跳过数据库来源，仅旧版任务目录可用
    return [];
  }
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
      directory: (row.directory || row.workspace_directory || row.project_worktree || ''),
      parent_id: row.parent_id || null,
      version: row.version || '',
      agent: row.agent || '',
      model: row.model || '',
      created: row.time_created != null ? row.time_created : 0,
      updated: row.time_updated != null ? row.time_updated : 0,
      archived: row.time_archived != null ? row.time_archived : 0,
      cost: row.cost != null ? row.cost : 0,
      source: `sqlite:${path.basename(dbPath)}`,
      path: dbPath,
      tables,
    }));
  } catch (_) { return []; }
  finally { if (db) db.close(); }
}

// ---------------------------------------------------------------- 旧版任务目录

// 与扩展迁移逻辑一致：迁移后的会话 ID = ses_migrated_<sha1(旧任务ID)[:26]>
function migratedSessionId(taskId) {
  return 'ses_migrated_' + crypto.createHash('sha1').update(String(taskId)).digest('hex').slice(0, 26);
}

function legacyTaskDate(id) {
  const value = Number(id);
  return Number.isFinite(value) && value > 1e12 ? value : 0;
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

function readIndexEntries(tasksRoot) {
  const entries = parseJson(readTextFile(path.join(tasksRoot, '_index.json')));
  const byId = new Map();
  for (const entry of (Array.isArray(entries.entries) ? entries.entries : [])) {
    if (entry && typeof entry === 'object' && typeof entry.id === 'string') byId.set(entry.id, entry);
  }
  return byId;
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
      const stored = parseJson(readTextFile(path.join(taskDir, 'history_item.json')));
      const indexed = indexById.get(name) || {};
      const messages = history.filter((entry) => entry && typeof entry === 'object');
      const directory = String((stored && stored.workspace) || indexed.workspace || '').trim();
      const created = timestampMs((stored && stored.ts) ?? indexed.ts) || legacyTaskDate(name);
      let updated = created;
      try { updated = Math.max(created, fs.statSync(taskDir).mtimeMs); } catch (_) { /* ignore */ }
      const sourceLabel = path.basename(path.dirname(path.dirname(tasksRoot)));
      sessions.push({
        session_id: name,
        title: String((stored && stored.task) || indexed.task || '').trim().slice(0, 120),
        directory,
        parent_id: null,
        version: '', agent: 'code', model: '',
        created, updated, archived: 0, cost: 0,
        source: `legacy-tasks:${sourceLabel}`,
        path: apiFile,
        tables: null,
        _history: messages,
      });
    }
  }
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

function scanSessions(dir) {
  let dataDirs, tasksRoots;
  if (dir) {
    dataDirs = [path.resolve(String(dir))];
    tasksRoots = legacyTaskRoots(dir);
  } else {
    dataDirs = dataDirCandidates();
    tasksRoots = defaultTasksRoots();
  }
  const seen = new Set();
  const sessions = [];
  for (const dataDir of dataDirs) {
    for (const dbPath of findDatabases(dataDir)) {
      for (const meta of dbSessions(dbPath)) {
        if (seen.has(meta.session_id)) continue;
        seen.add(meta.session_id);
        sessions.push(meta);
      }
    }
  }
  sessions.push(...legacySessions(tasksRoots, seen));
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

// ---------------------------------------------------------------- v1 归一化

function messageTime(data, fallback) { return timestampMs((data.time || {}).created) || timestampMs(fallback); }
function partTime(data, fallback) { return timestampMs((data.time || {}).start) || timestampMs(fallback); }

function normalizeV1(messages, partsByMessage) {
  const items = [], summaries = [];
  let model = '', provider = '', agent = '';
  for (const message of messages) {
    const data = message.data, role = data.role || '', ts = messageTime(data, message.time_created);
    const modelInfo = data.model && typeof data.model === 'object' ? data.model : {};
    if (data.modelID || modelInfo.modelID) { model = data.modelID || modelInfo.modelID; }
    else if (typeof data.model === 'string' && data.model) { model = data.model; }
    if (data.providerID || modelInfo.providerID) { provider = data.providerID || modelInfo.providerID; }
    if (data.agent) agent = data.agent;
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
        if (outputValue != null || error != null || status === 'completed' || status === 'error') {
          const output = typeof outputValue === 'string' ? outputValue
            : (outputValue == null ? '' : JSON.stringify(outputValue));
          items.push({
            kind: 'tool_result',
            timestamp: timestampMs((state.time || {}).end) || pts,
            tool_use_id: callId,
            content: error != null ? String(error) : output,
            is_error: status === 'error' || error != null,
          });
        }
      }
      // patch / file 为源工具元数据（代码补丁、附件），不进入对话
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, summaries, model, provider, agent };
}

// ---------------------------------------------------------------- v2 归一化

function toolResultText(state) {
  if (state.error != null) {
    return typeof state.error === 'string' ? state.error : JSON.stringify(state.error);
  }
  const output = state.output;
  if (output != null && typeof output !== 'object') return String(output);
  const content = state.content;
  if (!Array.isArray(content)) return output == null ? '' : JSON.stringify(output);
  return content.map((entry) => {
    if (!entry || typeof entry !== 'object') return entry == null ? '' : String(entry);
    if (entry.type === 'text') return String(entry.text || '');
    return JSON.stringify(entry);
  }).filter(Boolean).join('\n');
}

function normalizeV2(rows) {
  const items = [], summaries = [];
  let model = '', provider = '', agent = '';
  const sorted = rows.slice().sort((a, b) =>
    ((a.seq != null ? a.seq : timestampMs(a.time_created))
      - (b.seq != null ? b.seq : timestampMs(b.time_created)))
    || String(a.id).localeCompare(String(b.id)));
  for (const row of sorted) {
    const data = row.data || {}, ts = timestampMs((data.time || {}).created) || timestampMs(row.time_created);
    if (row.type === 'user') {
      const text = String(data.text || '').trim();
      if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      // user.files 附件为元数据，不进入对话
    } else if (row.type === 'synthetic' || row.type === 'compaction') {
      const text = String(data.summary || data.kilo_summary || data.text || '').trim();
      if (text) summaries.push(text);
    } else if (row.type === 'shell') {
      const command = String(data.command || '').trim();
      const callId = data.callID || `shell-${row.id}`;
      items.push({ kind: 'tool_use', timestamp: ts, name: 'bash', input: { command }, tool_use_id: callId });
      items.push({
        kind: 'tool_result',
        timestamp: timestampMs((data.time || {}).completed) || ts,
        tool_use_id: callId,
        content: String(data.output || ''),
        is_error: false,
      });
    } else if (row.type === 'assistant') {
      for (const entry of Array.isArray(data.content) ? data.content : []) {
        if (!entry || typeof entry !== 'object') continue;
        if (entry.type === 'text') {
          const text = String(entry.text || '');
          if (text.trim()) items.push({ kind: 'assistant_text', timestamp: ts, text });
        } else if (entry.type === 'tool') {
          const state = entry.state || {}, callId = entry.id || '';
          items.push({ kind: 'tool_use', timestamp: ts, name: entry.name || 'tool',
            input: state.input || {}, tool_use_id: callId });
          const status = String(state.status || '');
          if (status === 'completed' || status === 'error') {
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

// ---------------------------------------------------------------- 旧版归一化

function stripLegacyNoise(text) {
  return String(text || '').replace(LEGACY_BLOCK_RE, '').trim();
}

function legacyTextFromContent(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((block) => block && typeof block === 'object' && block.type === 'text' && block.text)
    .map((block) => block.text).join('\n');
}

// 旧版 VS Code 扩展任务：api_conversation_history.json 为 Anthropic 风格消息数组
function normalizeLegacy(messages) {
  const items = [];
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
                ? block.content
                  .map((entry) => (entry && typeof entry === 'object'
                    ? String(entry.text || JSON.stringify(entry))
                    : (entry == null ? '' : String(entry))))
                  .filter(Boolean).join('\n')
                : String(block.content ?? '');
            items.push({ kind: 'tool_result', timestamp: 0, tool_use_id: block.tool_use_id || '',
              content: text, is_error: !!block.is_error });
          }
        }
      }
      const text = stripLegacyNoise(
        legacyTextFromContent(content).replace(LEGACY_TASK_RE_G, '\n$1\n'));
      if (text) items.push({ kind: 'user_text', timestamp: 0, text });
    } else if (role === 'assistant') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== 'object') continue;
          if (block.type === 'text') {
            const text = String(block.text || '');
            if (text.trim()) items.push({ kind: 'assistant_text', timestamp: 0, text });
          } else if (block.type === 'tool_use') {
            items.push({ kind: 'tool_use', timestamp: 0, name: block.name || 'tool',
              input: block.input || {}, tool_use_id: block.id || '' });
          }
        }
      }
    }
  }
  return { items, summaries: [], model: '', provider: '', agent: 'code' };
}

// ---------------------------------------------------------------- 加载与转换

function loadDbSession(meta) {
  const db = openDb(meta.path);
  if (!db) throw new cs.ConvertError(`无法打开数据库：${meta.path}（Node 需 22.5+ 支持 node:sqlite）`);
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
      if (messages.length) normalized = normalizeV1(messages, partsByMessage);
    }
    if (!normalized.items.length && tables.has('session_message')) {
      const rows = db.prepare('SELECT id, type, seq, time_created, data FROM session_message WHERE session_id=?')
        .all(meta.session_id).map((row) => ({
          id: row.id, type: row.type, seq: row.seq, time_created: row.time_created, data: parseJson(row.data),
        }));
      if (rows.length) normalized = normalizeV2(rows);
    }
    if (!normalized.model && meta.model) normalized.model = meta.model;
    if (!normalized.agent && meta.agent) normalized.agent = meta.agent;
    return normalized;
  } finally { db.close(); }
}

function loadNormalized(meta) {
  // 单个会话元信息 -> 归一化结果（items/summaries/model/...）
  const normalized = String(meta.source).startsWith('sqlite:')
    ? loadDbSession(meta)
    : normalizeLegacy(meta._history || []);
  if (!normalized.model && meta.model) normalized.model = meta.model;
  if (!normalized.agent && meta.agent) normalized.agent = meta.agent;
  return normalized;
}

function itemsToMsgs(normalized) {
  // 摘要注记置于最前，其余严格保持源顺序
  const msgs = (normalized.summaries || []).map((text) => cs.mNote(text));
  for (const item of normalized.items || []) {
    const ts = msToIso(item.timestamp);
    if (item.kind === 'user_text') {
      msgs.push(cs.mMessage('user', item.text, ts));
    } else if (item.kind === 'assistant_text') {
      msgs.push(cs.mMessage('assistant', item.text, ts));
    } else if (item.kind === 'tool_use') {
      msgs.push(cs.mToolUse(item.tool_use_id || '', item.name || 'tool', item.input || {}, ts));
    } else if (item.kind === 'tool_result') {
      msgs.push(cs.mToolResult(item.tool_use_id || '', item.content || '', !!item.is_error, ts));
    }
    // patch / attachment 等元数据分支不产生对话消息
  }
  return msgs;
}

function firstUserTitle(msgs, fallback) {
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      return m.text.trim().split('\n')[0].slice(0, 60);
    }
  }
  return fallback;
}

// ---------------------------------------------------------------- 对外接口

function listSessions(opts) {
  const o = opts || {};
  let sessions = scanSessions(o.dir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  return sessions.slice(0, 50).map((meta) => {
    const title = meta.title || '';
    let count = 0;
    let lastTs = '';
    let finalTitle = title;
    try {
      const msgs = itemsToMsgs(loadNormalized(meta));
      count = msgs.length;
      lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
      if (!finalTitle) finalTitle = firstUserTitle(msgs, meta.session_id);
    } catch (e) {
      if (!finalTitle) finalTitle = meta.session_id;
    }
    return {
      session_id: meta.session_id, title: finalTitle, cwd: meta.directory || '',
      mtime: timestampMs(meta.updated) / 1000.0, path: String(meta.path),
      count, last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  let sessions = scanSessions(o.dir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Kilo 会话（dir=${o.dir || defaultDir()}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  let normalized;
  try {
    normalized = loadNormalized(target);
  } catch (e) {
    throw new cs.ConvertError(`解析会话失败：${e.message}`);
  }
  const msgs = itemsToMsgs(normalized);
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const sid = target.session_id;
  const title = target.title || firstUserTitle(msgs, sid);
  const ref = cs.makeRef('kilo', sid, title, target.directory || '', {
    model: normalized.model || '',
    started_at: msToIso(timestampMs(target.created)),
    ended_at: msToIso(timestampMs(target.updated)),
    source: String(target.path),
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
