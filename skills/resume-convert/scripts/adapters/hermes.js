#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/hermes: Hermes Agent（<hermes_home>/state.db SQLite）-> 归一化会话。
 *
 * 格式依据（与 resume-hermes 一致）：
 *   - HERMES_HOME（默认 %LOCALAPPDATA%\hermes、~/.hermes）下 state.db，表 sessions + messages
 *   - role ∈ session_meta（跳过）/user/assistant/tool/system
 *   - assistant.tool_calls 为 JSON 数组，arguments 可为 JSON 串或对象
 *   - tool.content 通常为 {"output":..., "exit_code":...} 或 {"error":...}
 *   - 压缩交接摘要（[CONTEXT COMPACTION 前缀 / _compressed_summary 标记）-> 转换注记
 *   - 会话链：parent_session_id + 父 end_reason='compression' 续链；branched/delegate 剔除
 * 标题：display_name 优先于 title；压缩续链取链尾标题。
 * 与 hermes.py 功能等价、输出一致。
 */

'use strict';

const os = require('os');
const path = require('path');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const cs = require('../claude_session.js');

// 压缩交接摘要的识别前缀（含历史版本），见 agent/context_compressor.py 的 SUMMARY_PREFIX
const COMPACTION_PREFIXES = ['[CONTEXT COMPACTION', '[CONTEXT SUMMARY]:'];
const SUMMARY_PREFIX_CUT = 'avoid repeating it:';
const SUMMARY_END_MARKER_RE = /^--- END OF CONTEXT SUMMARY[^\n]*---\s*$/gm;

function defaultDir() {
  if (process.env.HERMES_HOME) return process.env.HERMES_HOME;
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
    return path.join(local, 'hermes');
  }
  return path.join(os.homedir(), '.hermes');
}

function dbPath(dataDir) { return path.join(dataDir, 'state.db'); }

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(value) { try { return require('fs').statSync(value).isFile(); } catch (_) { return false; } }

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function isoZ(seconds) {
  // epoch 秒 -> 毫秒精度 UTC 'Z' 结尾 ISO（与 Python 版一致）；<=0 返回 ''
  const n = Number(seconds || 0);
  if (!n || n <= 0) return '';
  return new Date(n * 1000).toISOString();
}

// ---------------------------------------------------------------- SQLite 访问

function openDb(dbFile) {
  if (!DatabaseSync) {
    throw new cs.ConvertError('当前 Node 不支持 node:sqlite（需要 Node.js 22.5+），无法读取 Hermes state.db');
  }
  if (!isFile(dbFile)) throw new cs.ConvertError('数据库不存在：' + dbFile);
  return new DatabaseSync(dbFile, { readOnly: true });
}

function tableColumns(db, table) {
  return new Set(db.prepare(`PRAGMA table_info(${table})`).all().map((row) => row.name));
}

function fetchRows(db, table, wanted) {
  // 按实际存在的列取数，兼容新旧 schema 的列差异
  const available = tableColumns(db, table);
  const cols = wanted.filter((c) => available.has(c));
  return db.prepare(`SELECT ${cols.join(', ')} FROM ${table}`).all();
}

function fallbackTime(session) {
  return Number(session.ended_at || session.started_at || 0);
}

function buildEntry(session, lastActive) {
  return {
    id: session.id,
    session,
    model_config: parseJson(session.model_config),
    parent_session_id: session.parent_session_id || '',
    started_at: Number(session.started_at || 0),
    end_reason: session.end_reason || '',
    title: String(session.display_name || session.title || '').trim(),
    last_active: lastActive.get(session.id) || fallbackTime(session),
  };
}

function scanEntries(dataDir) {
  const file = dbPath(dataDir);
  if (!isFile(file)) return [];
  let db;
  try {
    db = openDb(file);
    const tables = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((row) => row.name));
    if (!['sessions', 'messages'].every((name) => tables.has(name))) return [];
    const sessions = fetchRows(db, 'sessions', [
      'id', 'source', 'session_key', 'chat_type', 'model', 'model_config',
      'parent_session_id', 'started_at', 'ended_at', 'end_reason',
      'message_count', 'tool_call_count', 'cwd', 'git_branch', 'git_repo_root',
      'title', 'display_name', 'archived',
    ]);
    const lastActive = new Map();
    for (const row of db.prepare('SELECT session_id, MAX(timestamp) AS ts FROM messages GROUP BY session_id').all()) {
      lastActive.set(row.session_id, row.ts || 0);
    }
    return sessions.map((session) => buildEntry(session, lastActive));
  } catch (_) { return []; }
  finally { if (db) db.close(); }
}

function loadMessages(dataDir, chainIds) {
  // 按链上会话 ID 取未压缩的活跃消息，段内按 id 排序
  const db = openDb(dbPath(dataDir));
  try {
    const available = tableColumns(db, 'messages');
    const optional = ['active', 'compacted', '_compressed_summary'].filter((c) => available.has(c));
    const cols = ['id', 'session_id', 'role', 'content', 'tool_call_id', 'tool_calls',
      'tool_name', 'timestamp', 'finish_reason'].concat(optional).join(', ');
    const rows = [];
    for (const sessionId of chainIds) {
      const where = ['session_id = ?'];
      if (optional.includes('active')) where.push('(active = 1 OR active IS NULL)');
      if (optional.includes('compacted')) where.push('(compacted = 0 OR compacted IS NULL)');
      rows.push(...db.prepare(`SELECT ${cols} FROM messages WHERE ${where.join(' AND ')} ORDER BY id`).all(sessionId));
    }
    return rows;
  } finally { db.close(); }
}

// ---------------------------------------------------------------- 行解析

function isSummaryRow(row) {
  if (row._compressed_summary) return true;
  const content = String(row.content || '').replace(/^\s+/, '');
  return COMPACTION_PREFIXES.some((prefix) => content.startsWith(prefix));
}

function cleanSummaryBody(content) {
  let body = content.trim();
  const cut = body.indexOf(SUMMARY_PREFIX_CUT);
  if (cut !== -1) body = body.slice(cut + SUMMARY_PREFIX_CUT.length);
  else for (const prefix of COMPACTION_PREFIXES) if (body.startsWith(prefix)) { body = body.slice(prefix.length); break; }
  body = body.replace(SUMMARY_END_MARKER_RE, '');
  return body.trim();
}

function toolResultContent(raw) {
  // tool 消息的 content 通常是 JSON（{"output":..., "exit_code":...} 等），抽取可读文本
  const data = parseJson(raw, null);
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    if ('output' in data) {
      const output = data.output;
      let text = typeof output === 'string' ? output : JSON.stringify(output);
      const error = data.error, exitCode = data.exit_code;
      const isError = !!error || (Number.isInteger(exitCode) && exitCode !== 0);
      if (error) text = text ? `${text}\n[error] ${error}` : String(error);
      return [text, isError];
    }
    if ('error' in data) return [String(data.error), true];
    return [JSON.stringify(data), false];
  }
  return [String(raw || ''), false];
}

function normalizeRows(rows) {
  // messages 行 -> 归一化 msgs；(timestamp, 行序) 稳定排序，严格保持源记录顺序
  const items = [];
  rows.forEach((row, order) => {
    const role = row.role || '';
    const content = row.content;
    const seconds = Number(row.timestamp || 0);
    const ts = isoZ(seconds);
    if (role === 'session_meta') return;
    if (isSummaryRow(row)) {
      const body = cleanSummaryBody(content || '');
      if (body) items.push([seconds, order, cs.mNote('历史压缩摘要：' + body, ts)]);
      return;
    }
    if (role === 'user') {
      const text = String(content || '').trim();
      if (text) items.push([seconds, order, cs.mMessage('user', text, ts)]);
    } else if (role === 'assistant') {
      const text = String(content || '').trim();
      if (text) items.push([seconds, order, cs.mMessage('assistant', text, ts)]);
      const calls = parseJson(row.tool_calls, null);
      if (Array.isArray(calls)) {
        for (const call of calls) {
          if (!call || typeof call !== 'object') continue;
          const fn = call.function || {};
          const name = fn.name || call.name || 'tool';
          const rawArgs = 'arguments' in fn ? fn.arguments : call.arguments;
          const input = rawArgs && typeof rawArgs === 'object' ? rawArgs : parseJson(rawArgs);
          items.push([seconds, order,
            cs.mToolUse(call.id || call.call_id || '', String(name), input, ts)]);
        }
      }
    } else if (role === 'tool') {
      const [text, isError] = toolResultContent(content);
      items.push([seconds, order,
        cs.mToolResult(row.tool_call_id || '', text, isError, ts)]);
    } else if (role === 'system') {
      const text = String(content || '').trim();
      if (text) items.push([seconds, order, cs.mNote('系统消息：' + text, ts)]);
    }
  });
  items.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
  return items.map((item) => item[2]);
}

// ---------------------------------------------------------------- 会话链

function projectFields(session) {
  // hermes 以 git_repo_root（缺失时退回 cwd）作为会话的项目归属
  const values = [];
  for (const key of ['git_repo_root', 'cwd']) {
    const value = String(session[key] || '').trim();
    if (value && !values.includes(value)) values.push(value);
  }
  return values;
}

function classifyChains(entries) {
  // 组织成逻辑会话：根会话（含分支子会话）+ 压缩续链；delegate 子 agent 会话整体剔除
  const byId = new Map(entries.map((entry) => [entry.id, entry]));
  const children = new Map();
  for (const entry of entries) {
    if (entry.parent_session_id) {
      if (!children.has(entry.parent_session_id)) children.set(entry.parent_session_id, []);
      children.get(entry.parent_session_id).push(entry);
    }
  }

  const isDelegate = (entry) => '_delegate_from' in entry.model_config;

  const isBranch = (entry) => {
    if ('_branched_from' in entry.model_config) return true;
    const parent = byId.get(entry.parent_session_id);
    return !!(parent && parent.end_reason === 'branched'
      && entry.started_at && parent.session.ended_at
      && entry.started_at >= Number(parent.session.ended_at));
  };

  const isCompressionChild = (entry) => {
    const parent = byId.get(entry.parent_session_id);
    return !!(parent && parent.end_reason === 'compression');
  };

  const results = [];
  for (const root of entries) {
    if (root.parent_session_id && !isBranch(root)) continue; // 压缩续链 / delegate 子会话由链首代为呈现
    if (isDelegate(root)) continue;
    const chain = [root];
    let tip = root;
    for (;;) {
      // 压缩续链可能因网关竞态出现多条，取最晚启动的一段
      const kids = (children.get(tip.id) || [])
        .filter((c) => isCompressionChild(c) && !isBranch(c) && !isDelegate(c));
      if (!kids.length) break;
      tip = kids.reduce((a, b) => (b.started_at > a.started_at || (b.started_at === a.started_at && b.id > a.id) ? b : a));
      chain.push(tip);
    }
    const paths = [];
    for (const item of chain) {
      for (const value of projectFields(item.session)) {
        if (!paths.includes(value)) paths.push(value);
      }
    }
    const titles = [chain[chain.length - 1].title, chain[0].title].filter(Boolean);
    results.push({
      chain_ids: chain.map((item) => item.id),
      root: chain[0],
      tip: chain[chain.length - 1],
      paths,
      title: titles[0] || '',
      last_active: Math.max(...chain.map((item) => item.last_active)),
    });
  }
  results.sort((a, b) => (b.last_active - a.last_active) || (a.root.id < b.root.id ? -1 : 1));
  return results;
}

function entryMatchesProject(entry, projectPath) {
  const target = normPath(projectPath);
  return entry.paths.some((value) => {
    const normed = normPath(value);
    return normed === target || (normed && target.startsWith(normed.replace(/[\\/]+$/, '') + '/'));
  });
}

function loadAndNormalize(dataDir, chainIds) {
  return normalizeRows(loadMessages(dataDir, chainIds));
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
  const dataDir = o.dir || defaultDir();
  let entries = classifyChains(scanEntries(dataDir));
  if (o.project) entries = entries.filter((e) => entryMatchesProject(e, o.project));
  return entries.slice(0, 50).map((e) => {
    let title = e.title;
    let count = 0;
    let lastTs = '';
    let msgs = [];
    try {
      msgs = loadAndNormalize(dataDir, e.chain_ids);
      count = msgs.length;
      lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
    } catch (err) { msgs = []; }
    if (!title) title = firstUserTitle(msgs, e.root.id);
    return {
      session_id: e.root.id,
      title,
      cwd: e.paths[0] || '',
      mtime: Number(e.last_active),
      path: dbPath(dataDir),
      count,
      last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const dataDir = o.dir || defaultDir();
  let entries = classifyChains(scanEntries(dataDir));
  if (o.project) entries = entries.filter((e) => entryMatchesProject(e, o.project));
  if (o.session_id) {
    const needle = String(o.session_id).trim().toLowerCase();
    entries = entries.filter((e) => e.chain_ids.some((sid) => {
      const id = sid.toLowerCase();
      return id === needle || id.startsWith(needle) || id.includes(needle);
    }));
  }
  if (!entries.length) {
    throw new cs.ConvertError(`未找到 Hermes 会话（dir=${dataDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const entry = entries[0];
  const msgs = loadAndNormalize(dataDir, entry.chain_ids);
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${dbPath(dataDir)}`);

  const root = entry.root, tip = entry.tip;
  const sid = root.id;
  const title = entry.title || firstUserTitle(msgs, sid);
  const endedAt = msgs[msgs.length - 1].ts || isoZ(fallbackTime(tip.session));
  const ref = cs.makeRef('hermes', sid, title, entry.paths[0] || '', {
    model: String(tip.session.model || root.session.model || ''),
    started_at: isoZ(root.started_at),
    ended_at: endedAt,
    source: dbPath(dataDir),
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
