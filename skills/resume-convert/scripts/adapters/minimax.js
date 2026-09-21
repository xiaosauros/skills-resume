#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/minimax: MiniMax Code（~/.minimax，兼容 ~/.mavis）-> 归一化会话。
 *
 * 格式依据（与 resume-minimax 一致）：
 *   数据源两级，会话列表取 SQLite、消息读 JSONL 优先（resume_minimax 的读取顺序）：
 *   1. SQLite 会话库 <主目录>/v2/sqlite/runtime-state.sqlite（local_runtime_sessions /
 *      local_runtime_message_rows / local_runtime_messages）
 *   2. 规范历史 JSONL <主目录>/v2/sessions/<年>/<月>/<日>/<时间>-session_<id>/
 *      messages.jsonl（信封 {message_id, turn_id, message{role,content,timestamp}}）
 *   消息语义：
 *   - 用户文本：message(role=user) 的 content（字符串或 text 块拼接；
 *     archonCompaction 标记消息是压缩边界，不按用户文本处理）
 *   - 助手文本/工具调用：content 的 text / toolCall 块（toolCall.id 为源真实调用 id）
 *   - 工具结果：message(role=toolResult)，toolCallId 配对、isError 标记错误
 *   - 附件（image 块 / attachments 列表）-> 转换注记（保留位置信息）
 *   - 压缩：archonCompaction / compactionSummary / 行 kind=compaction -> 转换注记
 *   时间戳为毫秒 epoch（兼容秒/ISO 兜底），统一转成 UTC ISO 'Z' 串；条目按
 * resume_minimax 的语义：源顺序展开 + 时间戳稳定排序。
 * todoState 任务清单只服务于 resume 摘要，不进入会话流。
 * 与 minimax.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

// 会话库中需要跳过的非对话会话类型（与 resume_minimax 一致）
const SKIP_KINDS = new Set(['peek', 'channel']);

function defaultDir() {
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

function normPath(p) {
  return p ? path.resolve(String(p)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (_) { return false; } }

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function parseJsonString(value) {
  // 工具结果 JSON 串 -> 展示文本（对象转紧凑 JSON）
  if (typeof value !== 'string' || !value) return '';
  try {
    const parsed = JSON.parse(value);
    if (typeof parsed === 'string') return parsed;
    return JSON.stringify(parsed);
  } catch (_) { return value; }
}

function timestampMs(value) {
  // resume_minimax 的时间戳语义：秒/毫秒 epoch 兼容，ISO 串兜底，失败为 0
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

function openDb(dbFile) {
  return new DatabaseSync(dbFile, { readOnly: true });
}

// ---------------------------------------------------------------- 会话列表

function sessionsDirCandidates(dataDir) {
  // v2/sessions 下 年/月/日/时间 四层目录里的会话目录
  const root = historyRoot(dataDir);
  let years;
  try { years = fs.readdirSync(root, { withFileTypes: true }); } catch (_) { return []; }
  const dirs = [];
  for (const year of years) {
    if (!year.isDirectory()) continue;
    const yearPath = path.join(root, year.name);
    let months;
    try { months = fs.readdirSync(yearPath, { withFileTypes: true }); } catch (_) { continue; }
    for (const month of months) {
      if (!month.isDirectory()) continue;
      const monthPath = path.join(yearPath, month.name);
      let days;
      try { days = fs.readdirSync(monthPath, { withFileTypes: true }); } catch (_) { continue; }
      for (const day of days) {
        if (!day.isDirectory()) continue;
        const dayPath = path.join(monthPath, day.name);
        let times;
        try { times = fs.readdirSync(dayPath, { withFileTypes: true }); } catch (_) { continue; }
        for (const time of times) {
          if (!time.isDirectory()) continue;
          dirs.push(path.join(dayPath, time.name));
        }
      }
    }
  }
  return dirs;
}

function manifestOf(sessionDir) {
  const file = path.join(sessionDir, 'manifest.json');
  if (!isFile(file)) return null;
  let value;
  try { value = parseJson(fs.readFileSync(file, 'utf8'), null); } catch (_) { return null; }
  if (!value || typeof value.sessionId !== 'string' || !value.sessionId) return null;
  return value;
}

function scanSessionsFiles(dataDir) {
  // 无会话库时的兜底：按 manifest.json 扫描（无法得知 cwd，标题交给首条用户消息）
  const metas = [];
  for (const sessionDir of sessionsDirCandidates(dataDir)) {
    const manifest = manifestOf(sessionDir);
    if (!manifest) continue;
    const messagesFile = path.join(sessionDir, 'messages.jsonl');
    let updated = timestampMs(manifest.updatedAtMs);
    if (!updated) {
      try { updated = Math.trunc(fs.statSync(messagesFile).mtimeMs); } catch (_) { updated = 0; }
    }
    metas.push({
      session_id: manifest.sessionId,
      title: '',
      directory: '',
      model: '',
      archived: false,
      created: timestampMs(manifest.createdAtMs),
      updated,
      source: 'files',
      history_dir: sessionDir,
      path: messagesFile,
    });
  }
  metas.sort((a, b) => (b.updated || 0) - (a.updated || 0));
  return metas;
}

function scanSessionsDb(dataDir) {
  // 会话库扫描（跳过 hidden 与 peek/channel）；库缺失或不可读返回 null 走文件兜底
  const file = dbPath(dataDir);
  if (!isFile(file) || !DatabaseSync) return null;
  let db;
  try {
    db = openDb(file);
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
        model: record.effectiveModel || '',
        archived: !!row.archived,
        created: timestampMs(row.created_at_ms ?? record.createdAtMs),
        updated: timestampMs(row.updated_at_ms ?? record.updatedAtMs),
        source: 'sqlite',
        history_dir: row.history_relative_dir
          ? path.join(historyRoot(dataDir), row.history_relative_dir) : '',
        path: file,
      });
    }
    metas.sort((a, b) => ((a.archived ? 1 : 0) - (b.archived ? 1 : 0))
      || ((b.updated || 0) - (a.updated || 0)));
    return metas;
  } catch (_) {
    return null; // WAL 遗留等只读打开失败时退回文件扫描
  } finally { if (db) { try { db.close(); } catch (_) { /* 已关闭 */ } } }
}

function scanSessions(dataDir) {
  const fromDb = scanSessionsDb(dataDir);
  if (fromDb !== null) return fromDb;
  return scanSessionsFiles(dataDir);
}

// ---------------------------------------------------------------- 消息条目

function userTextOf(message) {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n');
}

function normalizeEnvelopes(envelopes) {
  // 信封消息 -> 统一条目，语义与 resume_minimax.normalizeEnvelopes 一致
  const items = [];
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
        // 压缩边界消息：摘要转注记；todoState 仅供 resume 摘要，不转换
        compactionCount += 1;
        if (typeof marker.summary === 'string') latestCompaction = marker.summary;
        items.push({ kind: 'compaction', timestamp: ts, text: marker.summary || '(压缩边界)' });
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
      const content = message.content;
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== 'object') continue;
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
      const content = message.content;
      let text;
      if (Array.isArray(content)) {
        text = content
          .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
          .map((block) => block.text)
          .join('\n');
      } else if (typeof content === 'string') {
        text = content;
      } else {
        text = '';
      }
      items.push({
        kind: 'tool_result',
        timestamp: ts,
        tool_use_id: message.toolCallId || '',
        content: text,
        is_error: !!message.isError,
      });
    } else if (role === 'compactionSummary') {
      compactionCount += 1;
      if (typeof message.summary === 'string') latestCompaction = message.summary;
      items.push({ kind: 'compaction', timestamp: ts, text: message.summary || '(压缩摘要)' });
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, model, compaction_count: compactionCount, latest_compaction: latestCompaction };
}

function normalizeRows(rows) {
  // 会话库 message 行（display message）-> 统一条目，语义与 resume_minimax 一致
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
          const name = (attachment && ((attachment.meta || {}).fileName
            || attachment.fileName || attachment.filePath)) || `#${index}`;
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
          // 库行把结果内联在调用上：status 2 成功 / 3 失败
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
    } else if (data.kind === 'compaction' && typeof data.msg_content === 'string'
      && data.msg_content.trim()) {
      compactionCount += 1;
      latestCompaction = data.msg_content;
      items.push({ kind: 'compaction', timestamp: ts, text: data.msg_content });
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, model: '', compaction_count: compactionCount, latest_compaction: latestCompaction };
}

function readEnvelopes(messagesFile) {
  let content;
  try {
    content = fs.readFileSync(messagesFile, 'utf8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取会话历史文件：${messagesFile}（${e.message}）`);
  }
  const envelopes = [];
  for (const line of content.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    const envelope = parseJson(t, null);
    if (envelope && envelope.message && typeof envelope.message === 'object') {
      envelopes.push(envelope);
    }
  }
  return envelopes;
}

function findSessionDir(dataDir, sessionId) {
  for (const sessionDir of sessionsDirCandidates(dataDir)) {
    const manifest = manifestOf(sessionDir);
    if (manifest && manifest.sessionId === sessionId) return sessionDir;
  }
  return '';
}

function loadMessagesJsonl(dataDir, meta) {
  let file = meta.history_dir ? path.join(meta.history_dir, 'messages.jsonl') : '';
  if (!file || !isFile(file)) {
    const sessionDir = findSessionDir(dataDir, meta.session_id);
    file = sessionDir ? path.join(sessionDir, 'messages.jsonl') : '';
  }
  if (!file || !isFile(file)) return null;
  return normalizeEnvelopes(readEnvelopes(file));
}

function loadMessagesRows(dataDir, sessionId) {
  const file = dbPath(dataDir);
  if (!isFile(file) || !DatabaseSync) return null;
  let db;
  try {
    db = openDb(file);
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
        const rowsLike = [];
        display.forEach((message, index) => {
          if (!message || typeof message !== 'object') return;
          rowsLike.push({
            msg_id: message.msg_id || String(index),
            role: message.role,
            created_at_ms: timestampMs(message.timestamp ?? message.created_at),
            data_json: JSON.stringify(message),
          });
        });
        return normalizeRows(rowsLike);
      }
    }
    return normalized;
  } catch (_) {
    return null;
  } finally { if (db) { try { db.close(); } catch (_) { /* 已关闭 */ } } }
}

function loadNormalized(dataDir, meta) {
  // 消息条目：JSONL 优先；缺失且会话来自 SQLite 时回退会话库（与 resume_minimax 一致）
  let normalized = loadMessagesJsonl(dataDir, meta);
  if (normalized === null && meta.source === 'sqlite') {
    normalized = loadMessagesRows(dataDir, meta.session_id);
  }
  if (normalized === null) {
    normalized = { items: [], model: '', compaction_count: 0, latest_compaction: '' };
  }
  return normalized;
}

// ---------------------------------------------------------------- 转换

function itemsToMsgs(items) {
  // 条目 -> 共享库归一化消息（保持 resume_minimax 排序后的源顺序）
  const msgs = [];
  for (const item of items) {
    const ts = msToIso(item.timestamp);
    if (item.kind === 'user_text') msgs.push(cs.mMessage('user', item.text, ts));
    else if (item.kind === 'assistant_text') msgs.push(cs.mMessage('assistant', item.text, ts));
    else if (item.kind === 'tool_use') {
      msgs.push(cs.mToolUse(item.tool_use_id || '', item.name || 'tool', item.input || {}, ts));
    } else if (item.kind === 'tool_result') {
      msgs.push(cs.mToolResult(item.tool_use_id || '', item.content || '', !!item.is_error, ts));
    } else if (item.kind === 'compaction') {
      msgs.push(cs.mNote(item.text, ts));
    } else if (item.kind === 'attachment') {
      // 附件（图片/文件）本体不跨工具迁移，转换为注记保留其位置信息
      msgs.push(cs.mNote(item.text, ts));
    }
  }
  return msgs;
}

function deriveTitle(meta, normalized) {
  // 标题链与 resume_minimax 一致：库标题 -> 首条用户消息 -> 最近压缩摘要 -> 会话ID
  let title = meta.title;
  if (!title) {
    const firstUser = normalized.items.find((item) => item.kind === 'user_text');
    title = firstUser ? firstUser.text : (normalized.latest_compaction || meta.session_id);
  }
  return title.split(/\r?\n/)[0].trim() || meta.session_id;
}

function listSessions(opts) {
  const o = opts || {};
  const dataDir = path.resolve(o.dir || defaultDir());
  let sessions = scanSessions(dataDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  return sessions.slice(0, 50).map((meta) => {
    let count = 0;
    let lastTs = '';
    let normalized = null;
    try {
      normalized = loadNormalized(dataDir, meta);
      const msgs = itemsToMsgs(normalized.items);
      count = msgs.length;
      lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
    } catch (e) { /* 单会话失败不阻塞列表 */ }
    return {
      session_id: meta.session_id,
      title: deriveTitle(meta, normalized || { items: [], latest_compaction: '' }),
      cwd: meta.directory,
      mtime: meta.updated || 0,
      path: meta.path,
      count,
      last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const dataDir = path.resolve(o.dir || defaultDir());
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
    throw new cs.ConvertError(`未找到 MiniMax Code 会话（dir=${dataDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const meta = sessions[0];
  let normalized;
  try {
    normalized = loadNormalized(dataDir, meta);
  } catch (e) {
    if (e instanceof cs.ConvertError) throw e;
    throw new cs.ConvertError(`解析会话失败：${meta.path}（${e.message}）`);
  }
  const msgs = itemsToMsgs(normalized.items);
  if (!msgs.length) {
    throw new cs.ConvertError(`会话无可转换内容：${meta.path}（${meta.session_id}）`);
  }

  const title = deriveTitle(meta, normalized);
  const timestamps = normalized.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  const ref = cs.makeRef('minimax', meta.session_id, title, meta.directory, {
    model: meta.model || normalized.model,
    started_at: msToIso(timestamps[0] || meta.created),
    ended_at: msToIso(timestamps[timestamps.length - 1] || meta.updated),
    source: String(meta.path),
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
