#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/zcode: ZCode（~/.zcode/cli/db/db.sqlite）-> 归一化会话。
 *
 * 格式依据（与 resume-zcode 一致）：
 *   - 用户/助手文本：part(type=text)，synthetic 系统注入提醒跳过
 *   - 工具调用：part(type=tool)，callID 为源真实 id；有 output/error 或 status 为
 *     completed/error 时产出结果；error 为 JSON null 视作无错误（resume_zcode.py 的语义）
 *   - 附件：part(type=file) -> 转换注记；reasoning / step-start / step-finish /
 *     timeline 跳过；session.time_compacting -> 压缩注记
 *   时间戳一律为毫秒 epoch，统一转成 UTC ISO 'Z' 串；条目按 SQL sequence 展开 +
 * 时间戳稳定排序（resume_zcode 的排序语义）。
 * todo 清单只服务于 resume_zcode 的摘要，不进入会话流。
 * 与 zcode.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const COMPACT_NOTE = '会话发生过上下文压缩（compact）';

function defaultDir() {
  if (process.env.ZCODE_HOME) return process.env.ZCODE_HOME;
  return path.join(os.homedir(), '.zcode');
}

function dbPath(dataDir) { return path.join(dataDir, 'cli', 'db', 'db.sqlite'); }

function normPath(p) {
  return p ? path.resolve(String(p)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (_) { return false; } }

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function timestampMs(value) {
  // resume_zcode 的时间戳语义：秒/毫秒 epoch 兼容，ISO 串兜底，失败为 0
  if (value && typeof value === 'object') value = value.updated || value.completed || value.created;
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) return Math.abs(number) < 1e10 ? Math.trunc(number * 1000) : Math.trunc(number);
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function msToIso(ms) {
  // 毫秒 epoch -> UTC ISO 'Z' 串（共享库 toIsoZ 可解析）
  if (!ms) return '';
  try { return new Date(Number(ms)).toISOString(); } catch (_) { return ''; }
}

function jsonText(value) { return JSON.stringify(value); }

function requireSqlite() {
  if (!DatabaseSync) {
    throw new cs.ConvertError('当前 Node 不支持 node:sqlite（需要 Node.js 22.5+），请改用 Python 版转换。');
  }
}

function openDb(dbFile) {
  requireSqlite();
  if (!isFile(dbFile)) {
    throw new cs.ConvertError(`无法打开 ZCode 数据库：${dbFile}（文件不存在）`);
  }
  try {
    return new DatabaseSync(dbFile, { readOnly: true });
  } catch (e) {
    throw new cs.ConvertError(`无法打开 ZCode 数据库：${dbFile}（${e.message}）`);
  }
}

function hasCurrentSchema(db) {
  const names = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((row) => row.name));
  return ['session', 'message', 'part'].every((name) => names.has(name));
}

function scanSessions(zcodeDir) {
  const file = dbPath(zcodeDir);
  if (!isFile(file) || !DatabaseSync) return [];
  let db;
  try {
    db = openDb(file);
    if (!hasCurrentSchema(db)) return [];
    return db.prepare(`
      SELECT id, title, directory,
             time_created, time_updated, time_compacting, time_archived
      FROM session ORDER BY time_updated DESC
    `).all().map((row) => ({
      session_id: row.id,
      title: row.title || row.id,
      directory: row.directory || '',
      created: row.time_created,
      updated: row.time_updated,
      compact: row.time_compacting,
      archived: row.time_archived,
      path: file,
    }));
  } catch (_) { return []; }
  finally { if (db) db.close(); }
}

function normalizeRecords(db, sessionId, compactMs) {
  // message/part 表 -> 条目列表（毫秒时间戳），语义与 resume_zcode 一致
  const items = [];
  let model = '', provider = '';
  const msgRows = db.prepare('SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY sequence, time_created, id')
    .all(sessionId);
  const partsByMessage = new Map();
  for (const row of db.prepare('SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY sequence, time_created, id').all(sessionId)) {
    if (!partsByMessage.has(row.message_id)) partsByMessage.set(row.message_id, []);
    partsByMessage.get(row.message_id).push({ id: row.id, time_created: row.time_created, data: parseJson(row.data) });
  }

  for (const row of msgRows) {
    const data = parseJson(row.data);
    const role = data.role || '';
    const ts = timestampMs(((data.time || {}).created) || row.time_created);
    const modelInfo = data.model || {};
    model = data.modelID || modelInfo.modelID || model;
    provider = data.providerID || modelInfo.providerID || provider;
    for (const part of partsByMessage.get(row.id) || []) {
      const pdata = part.data, type = pdata.type || '';
      const pts = timestampMs(((pdata.time || {}).start) || (part.time_created || ts));
      if (type === 'text') {
        // synthetic 文本是系统注入的提醒/通知（如 TodoWrite 提示），不属于真实对话
        if (pdata.synthetic) continue;
        const text = pdata.text || '';
        if (!text.trim()) continue;
        items.push({ kind: `${role}_text`, timestamp: pts, text });
      } else if (type === 'tool') {
        const state = pdata.state || {}, name = pdata.tool || 'tool', callId = pdata.callID || '';
        items.push({ kind: 'tool_use', timestamp: pts, name, input: state.input || {}, tool_use_id: callId });
        const status = String(state.status || ''), outputValue = state.output, error = state.error;
        // JSON null 与缺字段同样视作"无错误"（resume_zcode.py 的 None 语义）
        const hasError = error !== undefined && error !== null;
        if (outputValue != null || hasError || ['completed', 'error'].includes(status)) {
          const output = typeof outputValue === 'string' ? outputValue
            : (outputValue == null ? '' : jsonText(outputValue));
          items.push({
            kind: 'tool_result',
            timestamp: timestampMs((state.time || {}).end) || pts,
            tool_use_id: callId,
            content: hasError ? (typeof error === 'string' ? error : jsonText(error)) : output,
            is_error: status === 'error' || hasError,
          });
        }
      } else if (type === 'file') {
        let label = pdata.filename || (pdata.source || {}).path || '';
        if (!label) {
          // 工具产出的内嵌文件（如截图）没有文件名，按 mime 生成可读描述
          const mime = pdata.mime || '';
          label = mime.startsWith('image/') ? '内嵌图片' : (mime || '内嵌附件');
        }
        items.push({ kind: 'attachment', timestamp: pts, text: String(label) });
      }
      // reasoning / step-start / step-finish / timeline 等类型与对话迁移无关，跳过
    }
  }

  if (compactMs) {
    // 会话级上下文压缩标记（session.time_compacting），按时间点排进事件流
    items.push({ kind: 'compact_note', timestamp: timestampMs(compactMs), text: COMPACT_NOTE });
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, model, provider };
}

function itemsToMsgs(items) {
  // resume_zcode 条目 -> 共享库归一化消息（严格保持源时间顺序）
  const msgs = [];
  for (const item of items) {
    const ts = msToIso(item.timestamp);
    if (item.kind === 'user_text') msgs.push(cs.mMessage('user', item.text, ts));
    else if (item.kind === 'assistant_text') msgs.push(cs.mMessage('assistant', item.text, ts));
    else if (item.kind === 'tool_use') {
      msgs.push(cs.mToolUse(item.tool_use_id || '', item.name || 'tool', item.input || {}, ts));
    } else if (item.kind === 'tool_result') {
      msgs.push(cs.mToolResult(item.tool_use_id || '', item.content || '', !!item.is_error, ts));
    } else if (item.kind === 'attachment') {
      msgs.push(cs.mNote(`附件：${item.text}`, ts));
    } else if (item.kind === 'compact_note') {
      msgs.push(cs.mNote(item.text, ts));
    }
    // 其他角色文本（system 等）不映射为对话消息，跳过
  }
  return msgs;
}

function buildRef(meta, items, model, provider, dbFile) {
  const timestamps = items.filter((item) => item.timestamp).map((item) => item.timestamp);
  const modelStr = provider && model ? `${provider}/${model}` : (model || provider);
  return cs.makeRef('zcode', meta.session_id, meta.title, meta.directory, {
    model: modelStr,
    started_at: msToIso(timestamps[0] || timestampMs(meta.created)),
    ended_at: msToIso(timestamps[timestamps.length - 1] || timestampMs(meta.updated)),
    source: String(dbFile),
  });
}

function listSessions(opts) {
  const o = opts || {};
  const zcodeDir = o.dir || defaultDir();
  let sessions = scanSessions(zcodeDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  return sessions.slice(0, 50).map((meta) => {
    let count = 0, lastTs = '';
    try {
      const db = openDb(meta.path);
      try {
        const msgs = itemsToMsgs(normalizeRecords(db, meta.session_id, meta.compact).items);
        count = msgs.length;
        lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
      } finally { db.close(); }
    } catch (e) { /* 单会话失败不阻塞列表 */ }
    return {
      session_id: meta.session_id, title: meta.title, cwd: meta.directory,
      mtime: timestampMs(meta.updated), path: meta.path, count, last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const zcodeDir = o.dir || defaultDir();
  let sessions = scanSessions(zcodeDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 ZCode 会话（dir=${zcodeDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const meta = sessions[0];
  const db = openDb(meta.path);
  let normalized;
  try {
    normalized = normalizeRecords(db, meta.session_id, meta.compact);
  } finally { db.close(); }
  const msgs = itemsToMsgs(normalized.items);
  if (!msgs.length) {
    throw new cs.ConvertError(`会话无可转换内容：${meta.path}（${meta.session_id}）`);
  }
  return { ref: buildRef(meta, normalized.items, normalized.model, normalized.provider, meta.path), msgs };
}

module.exports = { listSessions, loadSession };
