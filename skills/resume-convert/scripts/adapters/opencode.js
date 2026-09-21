#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/opencode: OpenCode（~/.local/share/opencode SQLite / 旧版 JSON storage）-> 归一化会话。
 *
 * 格式依据（与 resume-opencode 一致）：
 *   数据目录：$OPENCODE_DATA_DIR > $XDG_DATA_HOME/opencode > ~/.local/share/opencode
 *   当前格式 opencode.db（SQLite，需含 session/message/part 表），
 *   旧版格式 storage/session|message|part 下的分层 JSON 文件。
 *   part.type 分支：
 *     - text: 正文；synthetic 或所在消息 summary=true 视为压缩摘要（-> 转换注记）
 *     - tool: state.input/output/error + callID -> 工具调用/结果对
 *     - patch / file: 代码补丁 / 附件（-> 转换注记）
 * 标题：session.title || slug || id。
 * 与 opencode.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (e) { DatabaseSync = null; }

function defaultDir() {
  if (process.env.OPENCODE_DATA_DIR) return process.env.OPENCODE_DATA_DIR;
  if (process.env.XDG_DATA_HOME) return path.join(process.env.XDG_DATA_HOME, 'opencode');
  return path.join(os.homedir(), '.local', 'share', 'opencode');
}

function normPath(p) {
  return p ? path.resolve(String(p)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(p) { try { return fs.statSync(p).isFile(); } catch (e) { return false; } }
function isDir(p) { try { return fs.statSync(p).isDirectory(); } catch (e) { return false; } }

function parseJson(value, fallback) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback || {};
  try { return JSON.parse(value); } catch (e) { return fallback || {}; }
}

function toMs(value) {
  // 与 resume_opencode.js timestampMs 一致：<1e10 视为秒、否则毫秒；字符串走 Date 解析
  if (value && typeof value === 'object') value = value.updated || value.completed || value.created;
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) return Math.abs(number) < 1e10 ? Math.trunc(number * 1000) : Math.trunc(number);
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function isoFromMs(ms) {
  // 毫秒时间戳 -> UTC ISO（毫秒精度，Z 结尾，与 claude_session.toIsoZ 对齐）；0 视为无时间
  if (!ms) return '';
  return new Date(ms).toISOString();
}

function dumpCompact(value) {
  // 非字符串的 output/error 统一为紧凑 JSON（与 opencode.py 侧保持逐字节一致）
  return JSON.stringify(value);
}

// ---------------------------------------------------------------- SQLite / 旧版扫描

function openDb(dbPath) {
  if (!DatabaseSync) {
    throw new cs.ConvertError('当前 Node 不支持 node:sqlite（需要 Node.js 22.5+），'
      + '无法读取 opencode.db；请改用 Python 适配器。');
  }
  if (!isFile(dbPath)) throw new cs.ConvertError('数据库不存在：' + dbPath);
  return new DatabaseSync(dbPath, { readOnly: true });
}

function hasCurrentSchema(db) {
  const names = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'")
    .all().map((row) => row.name));
  return ['session', 'message', 'part'].every((name) => names.has(name));
}

function dbSessions(dataDir) {
  const dbPath = path.join(dataDir, 'opencode.db');
  if (!isFile(dbPath) || !DatabaseSync) return [];
  let db;
  try {
    db = openDb(dbPath);
    if (!hasCurrentSchema(db)) return [];
    return db.prepare(`
      SELECT s.*, p.worktree AS project_worktree, p.name AS project_name
      FROM session s LEFT JOIN project p ON p.id = s.project_id
      ORDER BY s.time_updated DESC
    `).all().map((row) => ({
      session_id: row.id,
      title: row.title || row.slug || row.id,
      directory: row.directory || row.project_worktree || '',
      created: row.time_created,
      updated: row.time_updated,
      source: 'sqlite',
      path: dbPath,
    }));
  } catch (e) {
    return [];
  } finally {
    if (db) { try { db.close(); } catch (e) { /* ignore */ } }
  }
}

function legacySessions(dataDir) {
  const root = path.join(dataDir, 'storage', 'session');
  const sessions = [];
  if (!isDir(root)) return sessions;
  for (const projectId of fs.readdirSync(root).sort()) {
    const projectDir = path.join(root, projectId);
    if (!isDir(projectDir)) continue;
    for (const file of fs.readdirSync(projectDir).sort().filter((v) => v.endsWith('.json'))) {
      const filePath = path.join(projectDir, file);
      try {
        const obj = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
        const stat = fs.statSync(filePath);
        const times = obj.time || {};
        const sessionId = obj.id || path.basename(file, '.json');
        sessions.push({
          session_id: sessionId,
          title: obj.title || obj.slug || sessionId,
          directory: obj.directory || '',
          created: times.created || stat.mtimeMs,
          updated: times.updated || stat.mtimeMs,
          source: 'legacy-json',
          path: filePath,
        });
      } catch (e) { /* 坏文件跳过 */ }
    }
  }
  sessions.sort((a, b) => toMs(b.updated) - toMs(a.updated));
  return sessions;
}

function scanSessions(dataDir) {
  // 当前 SQLite 优先，空结果时回退旧版 JSON（与 resume_opencode.scanSessions 一致）
  const current = dbSessions(dataDir);
  return current.length ? current : legacySessions(dataDir);
}

// ---------------------------------------------------------------- 归一化

function normalizeRecords(messages, partsByMessage) {
  // 与 resume_opencode.js normalizeRecords 的分支一致；note 即压缩摘要
  const items = [];
  let model = '', provider = '', agent = '';
  for (const message of messages) {
    const data = message.data;
    const role = data.role || '';
    const ts = toMs((data.time || {}).created || message.time_created);
    const modelInfo = data.model || {};
    model = data.modelID || modelInfo.modelID || model;
    provider = data.providerID || modelInfo.providerID || provider;
    agent = data.agent || agent;
    for (const part of partsByMessage.get(message.id) || []) {
      const pdata = part.data;
      const ptype = pdata.type || '';
      const pts = toMs((pdata.time || {}).start || part.time_created || ts);
      if (ptype === 'text') {
        const text = pdata.text || '';
        if (!String(text).trim()) continue;
        if (pdata.synthetic || data.summary === true) {
          items.push({ kind: 'note', timestamp: pts, text });
        } else {
          items.push({ kind: `${role}_text`, timestamp: pts, text });
        }
      } else if (ptype === 'tool') {
        const state = pdata.state || {};
        const name = pdata.tool || 'tool';
        const callId = pdata.callID || '';
        items.push({ kind: 'tool_use', timestamp: pts, name, input: state.input || {}, call_id: callId });
        const status = String(state.status || '');
        const outputValue = state.output;
        const error = state.error;
        if (outputValue !== undefined || error !== undefined
          || status === 'completed' || status === 'error') {
          const output = typeof outputValue === 'string'
            ? outputValue
            : (outputValue == null ? '' : dumpCompact(outputValue));
          const content = error !== undefined
            ? (typeof error === 'string' ? error : dumpCompact(error))
            : output;
          items.push({
            kind: 'tool_result',
            timestamp: toMs((state.time || {}).end) || pts,
            call_id: callId, content,
            is_error: status === 'error' || error !== undefined,
          });
        }
      } else if (ptype === 'patch') {
        items.push({ kind: 'patch', timestamp: pts, files: pdata.files || [] });
      } else if (ptype === 'file') {
        const label = pdata.filename || (pdata.source || {}).path || '附件';
        items.push({ kind: 'attachment', timestamp: pts, text: String(label) });
      }
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, model, provider, agent };
}

function itemsToMsgs(items) {
  // 归一化条目 -> cs 消息流；patch / 附件降级为转换注记
  const msgs = [];
  for (const item of items) {
    const ts = isoFromMs(item.timestamp);
    if (item.kind === 'user_text') msgs.push(cs.mMessage('user', item.text, ts));
    else if (item.kind === 'assistant_text') msgs.push(cs.mMessage('assistant', item.text, ts));
    else if (item.kind === 'note') msgs.push(cs.mNote(item.text, ts));
    else if (item.kind === 'tool_use') msgs.push(cs.mToolUse(item.call_id, item.name, item.input, ts));
    else if (item.kind === 'tool_result') {
      msgs.push(cs.mToolResult(item.call_id, item.content, item.is_error, ts));
    } else if (item.kind === 'patch') {
      const names = (item.files || []).map(String).join('、');
      msgs.push(cs.mNote(names ? `代码补丁涉及文件：${names}` : '代码补丁（无文件记录）', ts));
    } else if (item.kind === 'attachment') {
      msgs.push(cs.mNote(`会话附件：${item.text}`, ts));
    }
  }
  return msgs;
}

function loadDbSession(dataDir, meta) {
  const db = openDb(path.join(dataDir, 'opencode.db'));
  try {
    const messages = db.prepare(
      'SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id')
      .all(meta.session_id)
      .map((row) => ({ id: row.id, time_created: row.time_created, data: parseJson(row.data) }));
    const partsByMessage = new Map();
    for (const row of db.prepare(
      'SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created, id')
      .all(meta.session_id)) {
      if (!partsByMessage.has(row.message_id)) partsByMessage.set(row.message_id, []);
      partsByMessage.get(row.message_id).push({
        id: row.id, time_created: row.time_created, data: parseJson(row.data),
      });
    }
    return normalizeRecords(messages, partsByMessage);
  } finally {
    try { db.close(); } catch (e) { /* ignore */ }
  }
}

function loadLegacySession(dataDir, meta) {
  // 旧版 storage/message/<sid>/*.json + storage/part/<mid>/*.json（与 resume 一致）
  const sid = meta.session_id;
  const msgRoot = path.join(dataDir, 'storage', 'message', sid);
  const messages = [];
  const partsByMessage = new Map();
  if (isDir(msgRoot)) {
    for (const file of fs.readdirSync(msgRoot).sort().filter((v) => v.endsWith('.json'))) {
      const filePath = path.join(msgRoot, file);
      try {
        const data = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
        const mid = data.id || path.basename(file, '.json');
        messages.push({ id: mid, time_created: (data.time || {}).created, data });
        const partRoot = path.join(dataDir, 'storage', 'part', mid);
        if (isDir(partRoot)) {
          for (const partFile of fs.readdirSync(partRoot).sort().filter((v) => v.endsWith('.json'))) {
            try {
              const partPath = path.join(partRoot, partFile);
              const pdata = JSON.parse(fs.readFileSync(partPath, 'utf-8'));
              if (!partsByMessage.has(mid)) partsByMessage.set(mid, []);
              partsByMessage.get(mid).push({
                id: pdata.id || path.basename(partFile, '.json'),
                time_created: fs.statSync(filePath).mtimeMs,
                data: pdata,
              });
            } catch (e) { /* 坏文件跳过 */ }
          }
        }
      } catch (e) { /* 坏文件跳过 */ }
    }
  }
  messages.sort((a, b) => toMs((a.data.time || {}).created || a.time_created)
    - toMs((b.data.time || {}).created || b.time_created));
  return normalizeRecords(messages, partsByMessage);
}

function loadAny(dataDir, meta) {
  return meta.source === 'sqlite'
    ? loadDbSession(dataDir, meta)
    : loadLegacySession(dataDir, meta);
}

// ---------------------------------------------------------------- 对外接口

function listSessions(opts) {
  const o = opts || {};
  const dataDir = o.dir || defaultDir();
  let sessions = scanSessions(dataDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    let msgs = [];
    try {
      msgs = itemsToMsgs(loadAny(dataDir, s).items);
    } catch (e) { /* 读取失败按空会话展示 */ }
    return {
      session_id: s.session_id, title: s.title, cwd: s.directory,
      mtime: (toMs(s.updated) || 0) / 1000, path: s.path, count: msgs.length,
      last_ts: msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '',
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const dataDir = o.dir || defaultDir();
  let sessions = scanSessions(dataDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 OpenCode 会话（dir=${dataDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const normz = loadAny(dataDir, target);
  const msgs = itemsToMsgs(normz.items);
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const timestamps = normz.items.filter((it) => it.timestamp).map((it) => it.timestamp);
  const startedAt = isoFromMs(timestamps[0]) || isoFromMs(toMs(target.created));
  const endedAt = isoFromMs(timestamps[timestamps.length - 1]) || isoFromMs(toMs(target.updated));
  const ref = cs.makeRef('opencode', target.session_id, target.title, target.directory || '', {
    model: (normz.provider ? normz.provider + '/' : '') + normz.model,
    started_at: startedAt, ended_at: endedAt, source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
