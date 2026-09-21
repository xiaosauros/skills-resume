#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/openclaw: OpenClaw（原 Clawdbot/Moltbot，~/.openclaw）-> 归一化会话。
 *
 * 格式依据（与 resume-openclaw 一致）：
 *   状态目录（OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot）下：
 *   - 当前版本（每 agent SQLite）：agents/<agentId>/agent/openclaw-agent.sqlite
 *       · session_nodes：session_key 主键，entry_json 为会话元数据（SessionEntry）；
 *         标题取 label > displayName > subject > autoLabel（行级 label/display_name 兜底），
 *         目录取 spawnedCwd > execCwd > worktree.repoRoot > sessionDiffBaseline.root
 *       · transcript_events：(session_id, seq) -> event_json（与 JSONL 行同构的条目树）
 *       · session_transcript_active_events：活动分支投影（优先按 active_position 读，
 *         表缺失/结构不符时回退按 seq 全量）
 *   - 旧版（JSON）：sessions/sessions.json（sessionKey -> SessionEntry 对象）+ 同目录
 *     <sessionId>.jsonl：首行 {type:"session",cwd} 头，其后 {type,id,parentId} 条目树，
 *     存在 leaf 指针时沿最后一条 leaf 的 targetId 回溯父链取当前分支
 * 条目转换语义（与 resume_openclaw 的 normalize 一致）：
 *   - user/assistant 文本消息 -> 对话消息；toolCall 块 -> 工具调用（真实 id）；
 *     toolResult -> 工具结果；stopReason=error -> 助手错误消息；
 *     compaction / branch_summary / custom_message -> 转换注记；
 *     thinking / label / session_info / reset / model_change / leaf 等跳过
 * 标题：SessionEntry 标题字段优先，缺省回退首条用户消息首行。
 * 与 openclaw.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

function defaultDir() {
  const envDir = process.env.OPENCLAW_STATE_DIR;
  if (envDir && envDir.trim()) return expandTilde(envDir.trim());
  const home = os.homedir();
  for (const name of ['.openclaw', '.clawdbot']) {
    const candidate = path.join(home, name);
    if (isDir(candidate)) return candidate;
  }
  return path.join(home, '.openclaw');
}

function stateDirsFor(openclawDir) {
  // 待扫描的状态目录列表：显式 dir 只扫该目录；否则按优先级取所有存在的候选
  if (openclawDir) return [openclawDir];
  const candidates = [];
  const envDir = process.env.OPENCLAW_STATE_DIR;
  if (envDir && envDir.trim()) candidates.push(expandTilde(envDir.trim()));
  const home = os.homedir();
  candidates.push(path.join(home, '.openclaw'));
  candidates.push(path.join(home, '.clawdbot'));
  const seen = new Set();
  const result = [];
  for (const candidate of candidates) {
    const key = normPath(candidate);
    if (seen.has(key)) continue;
    seen.add(key);
    if (isDir(candidate)) result.push(candidate);
  }
  return result;
}

function isDir(p) { try { return fs.statSync(p).isDirectory(); } catch (_) { return false; } }
function isFile(p) { try { return fs.statSync(p).isFile(); } catch (_) { return false; } }

function expandTilde(p) {
  if (!p) return p;
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function normPath(p) {
  return p ? path.resolve(String(p)).toLowerCase().replace(/\\/g, '/') : '';
}

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function timestampMs(value) {
  // resume_openclaw 的时间戳语义：秒/毫秒 epoch 兼容，ISO 串兜底，失败为 0
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

function fileMtime(filePath) {
  try { return Math.trunc(fs.statSync(filePath).mtimeMs); } catch (_) { return 0; }
}

function readTextFile(filePath) {
  try { return fs.readFileSync(filePath, 'utf-8'); } catch (_) { return ''; }
}

function messageContentText(content) {
  // 消息内容统一抽为纯文本：字符串原样；块列表取 text，image 记为 [图片]
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const part of content) {
    if (part && typeof part === 'object') {
      if (part.type === 'text') parts.push(String(part.text || ''));
      else if (part.type === 'image') parts.push('[图片]');
    } else if (typeof part === 'string') {
      parts.push(part);
    }
  }
  return parts.filter(Boolean).join('\n');
}

// ---------- 会话元数据 ----------

function entryTitle(entry) {
  for (const key of ['label', 'displayName', 'subject', 'autoLabel']) {
    const value = entry[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function entryDirectory(entry) {
  const worktree = (entry.worktree && typeof entry.worktree === 'object') ? entry.worktree : {};
  const baseline = (entry.sessionDiffBaseline && typeof entry.sessionDiffBaseline === 'object') ? entry.sessionDiffBaseline : {};
  for (const value of [entry.spawnedCwd, entry.execCwd, worktree.repoRoot, baseline.root]) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function collectSources(stateDir) {
  // 会话源：每 agent 的 SQLite 库 + 每 agent 旧版 sessions.json + 根级旧版 sessions.json
  const sources = [];
  const agentsRoot = path.join(stateDir, 'agents');
  let agentIds = [];
  try {
    agentIds = fs.readdirSync(agentsRoot)
      .filter((name) => isDir(path.join(agentsRoot, name))).sort();
  } catch (_) { /* agents 目录不存在时忽略 */ }
  for (const agentId of agentIds) {
    const agentDir = path.join(agentsRoot, agentId);
    const sqlite = path.join(agentDir, 'agent', 'openclaw-agent.sqlite');
    if (isFile(sqlite)) sources.push({ kind: 'sqlite', agentId, path: sqlite });
    const legacyStore = path.join(agentDir, 'sessions', 'sessions.json');
    if (isFile(legacyStore)) {
      sources.push({ kind: 'legacy', agentId, path: legacyStore, sessionsDir: path.dirname(legacyStore) });
    }
  }
  const rootLegacy = path.join(stateDir, 'sessions', 'sessions.json');
  if (isFile(rootLegacy)) {
    sources.push({ kind: 'legacy', agentId: '', path: rootLegacy, sessionsDir: path.dirname(rootLegacy) });
  }
  return sources;
}

function requireSqlite() {
  if (!DatabaseSync) {
    throw new cs.ConvertError('当前 Node 不支持 node:sqlite（需要 Node.js 22.5+），请改用 Python 版转换。');
  }
}

function openDbRo(sqlitePath) {
  // 只读打开（与 resume_openclaw 一致）
  requireSqlite();
  try {
    return new DatabaseSync(sqlitePath, { readOnly: true });
  } catch (e) {
    throw new cs.ConvertError(`无法打开 OpenClaw SQLite 库：${sqlitePath}（${e.message}）`);
  }
}

function sqliteNodes(sqlitePath) {
  // session_nodes 全表 -> 节点列表（entry_json 解析为 SessionEntry）
  const db = openDbRo(sqlitePath);
  try {
    let rows;
    try {
      rows = db.prepare(`
        SELECT session_key, current_session_id, entry_json, label, display_name,
               created_at, updated_at, last_activity_at, archived_at, pinned_at, status,
               parent_session_key, spawned_by, project_id
        FROM session_nodes
      `).all();
    } catch (_) {
      // 低版本库列不全时退回基础列（对齐 Python 侧的兜底字段形状）
      try {
        rows = db.prepare('SELECT session_key, current_session_id, entry_json, updated_at FROM session_nodes').all()
          .map((row) => ({
            session_key: row.session_key || '', current_session_id: row.current_session_id || '',
            entry_json: row.entry_json, label: '', display_name: '',
            created_at: 0, updated_at: row.updated_at || 0, last_activity_at: 0, status: '',
          }));
      } catch (_) { return []; }
    }
    return rows.map((row) => ({
      session_key: row.session_key || '',
      current_session_id: row.current_session_id || '',
      entry: parseJson(row.entry_json, {}),
      row_label: row.label || '',
      row_display_name: row.display_name || '',
      created_at: row.created_at || 0,
      updated_at: row.updated_at || 0,
      last_activity_at: row.last_activity_at || 0,
      row_status: row.status || '',
    }));
  } finally { db.close(); }
}

// sessions.json 为对象映射 sessionKey -> SessionEntry
function legacyStoreEntries(storePath) {
  const data = parseJson(readTextFile(storePath), null);
  if (!data || typeof data !== 'object' || Array.isArray(data)) return [];
  const entries = [];
  for (const [sessionKey, value] of Object.entries(data)) {
    if (!value || typeof value !== 'object' || !value.sessionId) continue;
    entries.push([sessionKey, value]);
  }
  return entries;
}

// JSONL 首行会话头 {type:"session", id, timestamp, cwd} 中的 cwd
function jsonlHeaderCwd(transcriptPath) {
  for (const line of readTextFile(transcriptPath).split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const parsed = parseJson(trimmed, null);
    if (parsed && typeof parsed === 'object' && parsed.type === 'session') {
      return String(parsed.cwd || '');
    }
  }
  return '';
}

// JSONL -> { header, entries }。存在 leaf 指针时沿最后一条 leaf 的 targetId
// 回溯父链得到当前分支；指针失效时回退为文件顺序（与 resume_openclaw 一致）
function jsonlTranscript(transcriptPath) {
  let header = null;
  const entries = [];
  for (const line of readTextFile(transcriptPath).split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const parsed = parseJson(trimmed, null);
    if (!parsed || typeof parsed !== 'object') continue;
    if (header === null && parsed.type === 'session') { header = parsed; continue; }
    if (typeof parsed.type !== 'string') continue;
    entries.push(parsed);
  }
  const byId = new Map();
  for (const entry of entries) if (entry.id) byId.set(entry.id, entry);
  const leaves = entries.filter((entry) => entry.type === 'leaf');
  if (leaves.length) {
    const branch = [];
    let cursor = leaves[leaves.length - 1].targetId;
    const guard = new Set();
    while (cursor && byId.has(cursor) && !guard.has(cursor)) {
      guard.add(cursor);
      const entry = byId.get(cursor);
      branch.push(entry);
      cursor = entry.parentId;
    }
    branch.reverse();
    // 指针失效（targetId 为空或链条断裂）时回退为文件顺序
    if (branch.length) return { header: header || {}, entries: branch };
  }
  return { header: header || {}, entries };
}

function sqliteEvents(sqlitePath, sessionId) {
  // transcript_events -> 条目列表：优先读活动分支投影，回退按 seq 全量
  const db = openDbRo(sqlitePath);
  try {
    try {
      return db.prepare(`
        SELECT te.event_json AS event_json
        FROM session_transcript_active_events ae
        JOIN transcript_events te ON te.session_id = ae.session_id AND te.seq = ae.event_seq
        WHERE ae.session_id = ?
        ORDER BY ae.active_position
      `).all(sessionId).map((row) => parseJson(row.event_json, null)).filter(Boolean);
    } catch (_) { /* 表不存在或库结构不同 */ }
    return db.prepare('SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq')
      .all(sessionId).map((row) => parseJson(row.event_json, null)).filter(Boolean);
  } catch (e) {
    throw new cs.ConvertError(`读取会话 ${sessionId} 的 transcript_events 失败：${e.message}`);
  } finally { db.close(); }
}

// ---------- 扫描与转换 ----------

function scanSessions(stateDirs) {
  // 跨项目扫描全部会话元信息（session_id 去重），按 updated 倒序
  const sessions = [];
  const seenIds = new Set();
  for (const stateDir of stateDirs) {
    for (const source of collectSources(stateDir)) {
      if (source.kind === 'sqlite') {
        for (const node of sqliteNodes(source.path)) {
          const entry = node.entry;
          const sessionId = String(entry.sessionId || node.current_session_id || '');
          if (!sessionId || seenIds.has(sessionId)) continue;
          seenIds.add(sessionId);
          sessions.push({
            session_id: sessionId,
            session_key: node.session_key,
            agent_id: source.agentId,
            title: entryTitle(entry) || node.row_label || node.row_display_name,
            directory: entryDirectory(entry),
            model_provider: String(entry.modelProvider || ''),
            model: String(entry.modelOverride || entry.model || ''),
            created: node.created_at || timestampMs(entry.createdAt),
            updated: Math.max(node.updated_at || 0, node.last_activity_at || 0)
              || timestampMs(entry.updatedAt),
            source_kind: 'sqlite',
            path: source.path,
            source: 'sqlite:' + source.path,
          });
        }
      } else {
        for (const [sessionKey, entry] of legacyStoreEntries(source.path)) {
          const sessionId = String(entry.sessionId || '');
          if (!sessionId || seenIds.has(sessionId)) continue;
          seenIds.add(sessionId);
          // 正文 JSONL 与 sessions.json 同目录（resolveSessionFilePathCore 约定）
          const transcript = path.join(source.sessionsDir, `${sessionId}.jsonl`);
          sessions.push({
            session_id: sessionId,
            session_key: sessionKey,
            agent_id: source.agentId,
            title: entryTitle(entry),
            directory: entryDirectory(entry) || jsonlHeaderCwd(transcript),
            model_provider: String(entry.modelProvider || ''),
            model: String(entry.modelOverride || entry.model || ''),
            created: timestampMs(entry.createdAt),
            updated: timestampMs(entry.updatedAt) || fileMtime(transcript),
            source_kind: 'legacy',
            path: transcript,
            source: 'legacy:' + source.path,
          });
        }
      }
    }
  }
  sessions.sort((a, b) => (b.updated || b.created) - (a.updated || a.created));
  return sessions;
}

function loadEntries(meta) {
  // 按存储类型读取会话正文条目
  if (meta.source_kind === 'sqlite') return sqliteEvents(meta.path, meta.session_id);
  return jsonlTranscript(meta.path).entries;
}

function entriesToMsgs(entries) {
  // 事件条目 -> 共享库归一化消息（严格保持源记录顺序）
  const msgs = [];
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') continue;
    const entryType = entry.type || '';
    const ts = msToIso(timestampMs(entry.timestamp));
    if (entryType === 'message') {
      const message = (entry.message && typeof entry.message === 'object') ? entry.message : {};
      const role = message.role || '';
      if (role === 'user') {
        const text = messageContentText(message.content).trim();
        if (text) msgs.push(cs.mMessage('user', text, ts));
      } else if (role === 'assistant') {
        const blocks = Array.isArray(message.content) ? message.content : [];
        const text = blocks.filter((b) => b && typeof b === 'object' && b.type === 'text')
          .map((b) => String(b.text || '')).join('\n').trim();
        if (text) msgs.push(cs.mMessage('assistant', text, ts));
        for (const block of blocks) {
          if (!block || typeof block !== 'object' || block.type !== 'toolCall') continue;
          let input = block.arguments;
          if (!input || typeof input !== 'object' || Array.isArray(input)) {
            input = (input == null || input === '') ? {} : { raw: input };
          }
          msgs.push(cs.mToolUse(String(block.id || ''), String(block.name || 'tool'), input, ts));
        }
        if (message.stopReason === 'error' && message.errorMessage) {
          msgs.push(cs.mMessage('assistant', String(message.errorMessage), ts));
        }
      } else if (role === 'toolResult') {
        msgs.push(cs.mToolResult(
          String(message.toolCallId || ''),
          messageContentText(message.content),
          !!message.isError, ts));
      }
    } else if (entryType === 'compaction') {
      const summary = entry.summary;
      if (typeof summary === 'string' && summary.trim()) {
        msgs.push(cs.mNote(`历史摘要（compact）：${summary.trim()}`, ts));
      }
    } else if (entryType === 'branch_summary') {
      const summary = entry.summary;
      if (typeof summary === 'string' && summary.trim()) {
        msgs.push(cs.mNote(`历史摘要（branch）：${summary.trim()}`, ts));
      }
    } else if (entryType === 'custom_message') {
      const text = messageContentText(entry.content).trim();
      if (entry.display !== false && text) {
        msgs.push(cs.mNote(`插件消息：${text}`, ts));
      }
    }
    // thinking / label / session_info / reset / model_change / custom / leaf 等跳过
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

function listSessions(opts) {
  const o = opts || {};
  const sessions = scanSessions(stateDirsFor(o.dir));
  let selected = sessions;
  if (o.project) {
    const norm = normPath(o.project);
    selected = sessions.filter((s) => normPath(s.directory) === norm);
  }
  return selected.slice(0, 50).map((meta) => {
    let title = meta.title;
    let count = 0;
    let lastTs = '';
    try {
      const msgs = entriesToMsgs(loadEntries(meta));
      count = msgs.length;
      lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
      if (!title) title = firstUserTitle(msgs, meta.session_id);
    } catch (e) {
      if (!title) title = meta.session_id;
    }
    return {
      session_id: meta.session_id, title, cwd: meta.directory,
      mtime: meta.updated, path: meta.path, count, last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  let sessions = scanSessions(stateDirsFor(o.dir));
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 OpenClaw 会话（dir=${o.dir || defaultDir()}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const meta = sessions[0];
  const msgs = entriesToMsgs(loadEntries(meta));
  if (!msgs.length) {
    throw new cs.ConvertError(`会话无可转换内容：${meta.path}（${meta.session_id}）`);
  }

  const timestamps = msgs.filter((m) => m.ts).map((m) => m.ts);
  const modelStr = meta.model_provider && meta.model
    ? `${meta.model_provider}/${meta.model}` : meta.model;
  const title = meta.title || firstUserTitle(msgs, meta.session_id);
  const ref = cs.makeRef('openclaw', meta.session_id, title, meta.directory, {
    model: modelStr,
    started_at: timestamps[0] || msToIso(meta.created),
    ended_at: timestamps[timestamps.length - 1] || msToIso(meta.updated),
    source: meta.source,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
