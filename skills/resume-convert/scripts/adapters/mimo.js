#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/mimo: MiMo-Code（小米 MiMo，OpenCode fork）SQLite 会话 -> 归一化会话。
 *
 * 格式依据（与 resume-mimo 一致）：
 *   - 数据目录下 mimocode.db（渠道库 mimocode-<channel>.db 按修改时间兜底，MIMOCODE_DB 可覆盖），
 *     表 session / message / part（data 列为 JSON）
 *   - 用户/助手文本：part(type=text)；synthetic 或 message.summary=true 视为压缩摘要
 *   - 工具调用：part(type=tool)，callID + state.input/output/error
 *   - 推理（reasoning/step-start/step-finish/snapshot/retry/agent/checkpoint）跳过
 *   - 压缩摘要、助手消息级错误、patch、file 附件、subtask -> 转换注记（mNote）
 * 标题：session.title，缺省回退 slug、id；cwd：session.directory，缺省回退 project.worktree。
 * 记录顺序严格保持源序。与 mimo.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

// 与 resume_mimo 的 timestamp_ms 一致；注意 output/error 判空统一用 != null
// （undefined 与 null 都视为"无"，与 Python 的 is not None 对齐）。


function defaultDir() {
  const candidates = [];
  if (process.env.MIMOCODE_HOME) {
    candidates.push(path.join(expandTilde(process.env.MIMOCODE_HOME), 'data'));
  }
  if (process.env.XDG_DATA_HOME) {
    candidates.push(path.join(expandTilde(process.env.XDG_DATA_HOME), 'mimocode'));
  }
  candidates.push(path.join(os.homedir(), '.local', 'share', 'mimocode'));
  if (process.env.LOCALAPPDATA) {
    candidates.push(path.join(process.env.LOCALAPPDATA, 'mimocode'));
  }
  candidates.push(path.join(os.homedir(), 'Library', 'Application Support', 'mimocode'));
  for (const c of candidates) {
    if (isDir(c)) return c;
  }
  return candidates[0];
}

function normPath(p) {
  return path.normalize(p).toLowerCase();
}

function isDir(p) { try { return fs.statSync(p).isDirectory(); } catch (_) { return false; } }
function isFile(p) { try { return fs.statSync(p).isFile(); } catch (_) { return false; } }

function expandTilde(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function parseJson(value) {
  if (value && typeof value === 'object') return value;
  if (!value) return {};
  try { return JSON.parse(value); } catch (_) { return {}; }
}

function timestampMs(value) {
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) {
    return Math.abs(number) < 1e10 ? Math.trunc(number * 1000) : Math.trunc(number);
  }
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function toIso(value) {
  // 源时间值 -> UTC ISO 毫秒字符串（供 cs 消费），无法解析返回 ''
  const ms = timestampMs(value);
  if (!ms) return '';
  return new Date(ms).toISOString();
}

// ---------------------------------------------------------------- 数据库定位

function dbCandidates(dataDir) {
  // MIMOCODE_DB > mimocode.db > mimocode-<channel>.db（按修改时间）
  const candidates = [];
  const envDb = process.env.MIMOCODE_DB;
  if (envDb && envDb !== ':memory:') {
    candidates.push(path.isAbsolute(envDb) ? envDb : path.join(dataDir, envDb));
  }
  candidates.push(path.join(dataDir, 'mimocode.db'));
  let channelDbs = [];
  try {
    channelDbs = fs.readdirSync(dataDir)
      .filter((name) => name.startsWith('mimocode-') && name.endsWith('.db'))
      .map((name) => {
        const full = path.join(dataDir, name);
        let mtime = 0;
        try { mtime = fs.statSync(full).mtimeMs; } catch (_) { /* skip */ }
        return { full, mtime };
      })
      .sort((a, b) => b.mtime - a.mtime)
      .map((entry) => entry.full);
  } catch (_) { /* skip */ }
  candidates.push(...channelDbs);
  const seen = new Set();
  return candidates.filter((p) => {
    const key = path.resolve(p).toLowerCase();
    if (seen.has(key) || !isFile(p)) return false;
    seen.add(key);
    return true;
  });
}

function openDb(dbPath) {
  if (!DatabaseSync) {
    throw new cs.ConvertError('当前 Node 不支持 node:sqlite（需要 Node.js 22.5+），请改用 Python 版适配器');
  }
  return new DatabaseSync(dbPath, { readOnly: true });
}

function hasCurrentSchema(db) {
  const names = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'")
    .all().map((row) => row.name));
  return ['session', 'message', 'part'].every((name) => names.has(name));
}

function dbSessions(dbPath) {
  // 读取单个库内的全部会话元信息（title/directory 与 resume 同源同回退）
  let db;
  try {
    db = openDb(dbPath);
    if (!hasCurrentSchema(db)) return [];
    return db.prepare(`
      SELECT s.*, p.worktree AS project_worktree
      FROM session s LEFT JOIN project p ON p.id = s.project_id
      ORDER BY s.time_updated DESC
    `).all().map((row) => ({
      session_id: row.id,
      title: row.title || row.slug || row.id,
      cwd: row.directory || row.project_worktree || '',
      created: row.time_created,
      updated: row.time_updated,
      db_path: dbPath,
    }));
  } catch (_) {
    return [];
  } finally {
    if (db) { try { db.close(); } catch (_) { /* ignore */ } }
  }
}

function scanSessions(mimoDir) {
  const sessions = [];
  for (const dbPath of dbCandidates(mimoDir)) sessions.push(...dbSessions(dbPath));
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

// ---------------------------------------------------------------- 记录归一化

function loadRows(dbPath, sessionId) {
  // 读取 message / part 原始行（源序：time_created, id）
  const db = openDb(dbPath);
  try {
    const messages = db.prepare(
      'SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id'
    ).all(sessionId).map((row) => ({
      id: row.id,
      time_created: row.time_created,
      data: parseJson(row.data),
    }));
    const partsByMessage = new Map();
    for (const row of db.prepare(
      'SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created, id'
    ).all(sessionId)) {
      if (!partsByMessage.has(row.message_id)) partsByMessage.set(row.message_id, []);
      partsByMessage.get(row.message_id).push({
        id: row.id,
        time_created: row.time_created,
        data: parseJson(row.data),
      });
    }
    return { messages, partsByMessage };
  } finally {
    db.close();
  }
}

function modelProvider(messages) {
  // 取会话最后生效的 modelID / providerID（与 resume 同源：顶层优先于 model 对象）
  let model = '';
  let provider = '';
  for (const message of messages) {
    const data = message.data;
    const info = data.model || {};
    model = data.modelID || info.modelID || model;
    provider = data.providerID || info.providerID || provider;
  }
  return { model, provider };
}

function normalize(messages, partsByMessage) {
  // message/part 行 -> msgs；严格保持源记录顺序
  const msgs = [];
  for (const message of messages) {
    const data = message.data;
    const role = data.role || '';
    const ts = toIso((data.time || {}).created || message.time_created);
    // 助手消息级错误：元信息，转注记（resume 的 error_text 分支）
    if (role === 'assistant' && data.error) {
      const err = data.error;
      const detail = (err.data && err.data.message) || err.message || '';
      const name = err.name || 'Error';
      const ets = toIso((data.time || {}).completed) || ts;
      msgs.push(cs.mNote(`助手执行出错：${detail ? `${name}: ${detail}` : String(name)}`, ets));
    }
    for (const part of partsByMessage.get(message.id) || []) {
      const pdata = part.data;
      const ptype = pdata.type || '';
      const pts = toIso((pdata.time || {}).start || part.time_created || ts);
      if (ptype === 'text') {
        const text = pdata.text || '';
        if (!text.trim()) continue;
        if (pdata.synthetic || data.summary === true) {
          msgs.push(cs.mNote(`历史摘要（compact）：${text}`, pts));
        } else if (role === 'user' || role === 'assistant') {
          msgs.push(cs.mMessage(role, text, pts));
        }
      } else if (ptype === 'tool') {
        const state = pdata.state || {};
        const name = pdata.tool || 'tool';
        const callId = pdata.callID || '';
        msgs.push(cs.mToolUse(callId, name, state.input || {}, pts));
        const status = String(state.status || '');
        const output = state.output;
        const error = state.error;
        // 结果产出条件与 resume 一致：有 output / error，或 status 已完结
        if (output != null || error != null || status === 'completed' || status === 'error') {
          const outputText = typeof output === 'string' ? output
            : (output != null ? JSON.stringify(output) : '');
          const rts = toIso((state.time || {}).end) || pts;
          msgs.push(cs.mToolResult(
            callId,
            error != null ? String(error) : outputText,
            status === 'error' || error != null,
            rts));
        }
      } else if (ptype === 'patch') {
        const files = pdata.files || [];
        if (files.length) {
          msgs.push(cs.mNote(`代码补丁涉及文件：${files.map(String).join('、')}`, pts));
        }
      } else if (ptype === 'file') {
        const label = pdata.filename || (pdata.source || {}).path || pdata.url || '附件';
        msgs.push(cs.mNote(`用户附件：${label}`, pts));
      } else if (ptype === 'compaction') {
        const summary = (pdata.projection || {}).summary || '';
        if (summary.trim()) {
          msgs.push(cs.mNote(`历史摘要（compact）：${summary}`, pts));
        }
      } else if (ptype === 'subtask') {
        const agent = pdata.agent || '';
        const desc = pdata.description || '';
        const head = agent ? `子任务[${agent}]` : '子任务';
        msgs.push(cs.mNote(desc ? `${head}：${desc}` : head, pts));
      }
      // reasoning / step-start / step-finish / snapshot / retry / agent /
      // checkpoint 等不进入转换
    }
  }
  return msgs;
}

// ---------------------------------------------------------------- 对外接口

function listSessions(opts) {
  const o = opts || {};
  const mimoDir = o.dir || defaultDir();
  let sessions = scanSessions(mimoDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    let msgs = [];
    try {
      const rows = loadRows(s.db_path, s.session_id);
      msgs = normalize(rows.messages, rows.partsByMessage);
    } catch (_) { /* 单会话读取失败按空处理 */ }
    return {
      session_id: s.session_id, title: s.title, cwd: s.cwd,
      mtime: timestampMs(s.updated) / 1000, path: s.db_path,
      count: msgs.length, last_ts: msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '',
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const mimoDir = o.dir || defaultDir();
  let sessions = scanSessions(mimoDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 MiMo 会话（dir=${mimoDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const rows = loadRows(target.db_path, target.session_id);
  const msgs = normalize(rows.messages, rows.partsByMessage);
  if (!msgs.length) {
    throw new cs.ConvertError(`会话无可转换内容：${target.session_id}（${target.db_path}）`);
  }

  const { model, provider } = modelProvider(rows.messages);
  const ref = cs.makeRef('mimo', target.session_id, target.title, target.cwd || '', {
    model: provider ? `${provider}/${model}` : model,
    started_at: toIso(target.created),
    ended_at: msgs[msgs.length - 1].ts || toIso(target.updated),
    source: target.db_path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
