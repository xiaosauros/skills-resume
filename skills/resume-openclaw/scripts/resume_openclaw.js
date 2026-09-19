#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-openclaw: 读取 OpenClaw（原 Clawdbot/Moltbot）本地会话，生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（OpenClaw / Claude Code / ZCode / Codex 等）均可直接调用：
 *   node resume_openclaw.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_openclaw.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 *
 * OpenClaw 会话存储（状态目录：OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot）：
 *   - 当前版本（每 agent SQLite）：agents/<agentId>/agent/openclaw-agent.sqlite
 *       · session_nodes：session_key 主键，entry_json 为会话元数据（SessionEntry）
 *       · session_windows：session_id <-> session_key 映射
 *       · transcript_events：(session_id, seq) -> event_json（与 JSONL 行同构的条目树）
 *       · session_transcript_active_events：活动分支投影（active_position -> event_seq）
 *   - 旧版（JSON）：agents/<agentId>/sessions/sessions.json（sessionKey -> SessionEntry 对象）
 *       与根级 sessions/sessions.json；会话正文为 sessions/<sessionId>.jsonl：
 *       首行 {type:"session",cwd,...} 头，其后为 {type,id,parentId,timestamp,...} 条目
 *   - 条目 message.message 为 {role:"user"|"assistant"|"toolResult", content, ...}，
 *     compaction 条目含 summary 与 firstKeptEntryId（其前内容已被压缩为摘要）
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const CST_OFFSET_MS = 8 * 3600 * 1000;

// OpenClaw 内置工具（src/agents/sessions/tools 与 src/agents/tools），键为小写名
const READ_TOOLS = new Set(['read', 'find', 'grep', 'ls', 'glob', 'codebase']);
const EDIT_TOOLS = new Set(['write', 'edit', 'apply_patch', 'applypatch', 'multiedit']);
const SHELL_TOOLS = new Set(['bash', 'exec', 'process', 'terminal', 'shell']);
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
// 未收录工具（browser/message/cron/image 等）按命名启发式归类
const READ_HINT_RE = /read|grep|search|glob|list|\bls\b|view|fetch|find|web|docs?/i;
const EDIT_HINT_RE = /edit|write|create|apply|patch|replace|insert|generate/i;
const SHELL_HINT_RE = /terminal|command|exec|shell|bash|\brun\b|process/i;

// ---------- 基础工具 ----------

function isDir(p) { try { return fs.statSync(p).isDirectory(); } catch (_) { return false; } }
function isFile(p) { try { return fs.statSync(p).isFile(); } catch (_) { return false; } }

function expandTilde(p) {
  if (!p) return p;
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

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

function truncate(value, limit) {
  const text = String(value);
  return text.length <= limit ? text : text.slice(0, limit) + '…';
}

function textBlock(value, limit) {
  const text = String(value || '').trim();
  return text.length <= limit ? text : text.slice(0, limit) + `\n…（已截断，原长 ${text.length} 字符）`;
}

function dedupe(values) {
  const seen = new Set(), output = [];
  for (const value of values) if (value && !seen.has(value)) { seen.add(value); output.push(value); }
  return output;
}

// ---------- 状态目录与会话源定位 ----------

// 状态目录候选：OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot（与 src/config/state-dir.ts 一致）
function stateDirCandidates(openclawDir) {
  if (openclawDir) return [expandTilde(openclawDir)];
  const candidates = [];
  if (process.env.OPENCLAW_STATE_DIR) candidates.push(expandTilde(process.env.OPENCLAW_STATE_DIR.trim()));
  candidates.push(path.join(os.homedir(), '.openclaw'));
  candidates.push(path.join(os.homedir(), '.clawdbot'));
  const seen = new Set();
  return candidates.filter((p) => {
    const key = path.resolve(p).toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// 会话源：每 agent 的 SQLite 库 + 每 agent 旧版 sessions.json + 根级旧版 sessions.json
function collectSources(stateDir) {
  const sources = [];
  const agentsRoot = path.join(stateDir, 'agents');
  let agentIds = [];
  try {
    agentIds = fs.readdirSync(agentsRoot).filter((name) => isDir(path.join(agentsRoot, name)));
  } catch (_) { /* agents 目录不存在时忽略 */ }
  for (const agentId of agentIds) {
    const agentDir = path.join(agentsRoot, agentId);
    const sqlite = path.join(agentDir, 'agent', 'openclaw-agent.sqlite');
    if (isFile(sqlite)) sources.push({ kind: 'sqlite', agentId, path: sqlite });
    const legacyStore = path.join(agentDir, 'sessions', 'sessions.json');
    if (isFile(legacyStore)) sources.push({ kind: 'legacy', agentId, path: legacyStore, sessionsDir: path.dirname(legacyStore) });
  }
  const rootLegacy = path.join(stateDir, 'sessions', 'sessions.json');
  if (isFile(rootLegacy)) sources.push({ kind: 'legacy', agentId: '', path: rootLegacy, sessionsDir: path.dirname(rootLegacy) });
  return sources;
}

function requireSqlite() {
  if (!DatabaseSync) {
    console.error('错误：当前 Node 不支持 node:sqlite（需要 Node.js 22.5+）。');
    console.error('请改用 Python：python -X utf8 scripts/resume_openclaw.py ...');
    process.exit(1);
  }
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

// sessions.json 为对象映射 sessionKey -> SessionEntry（与 legacy-store-inspection.ts 一致）
function legacyStoreEntries(source) {
  const data = parseJson(readTextFile(source.path), null);
  if (!data || typeof data !== 'object' || Array.isArray(data)) return [];
  const entries = [];
  for (const [sessionKey, value] of Object.entries(data)) {
    if (!value || typeof value !== 'object' || !value.sessionId) continue;
    entries.push({ sessionKey, entry: value });
  }
  return entries;
}

function readTextFile(filePath) {
  try { return fs.readFileSync(filePath, 'utf-8'); } catch (_) { return ''; }
}

function sqliteNodes(source) {
  let db;
  try {
    requireSqlite();
    db = new DatabaseSync(source.path, { readOnly: true });
  } catch (error) {
    console.error(`错误：无法打开 SQLite 库 ${source.path}：${error.message}`);
    process.exit(1);
  }
  try {
    const rows = db.prepare(`
      SELECT session_key, current_session_id, entry_json, label, display_name,
             created_at, updated_at, last_activity_at, archived_at, pinned_at, status,
             parent_session_key, spawned_by, project_id
      FROM session_nodes
    `).all();
    return rows.map((row) => ({
      session_key: row.session_key || '',
      current_session_id: row.current_session_id || '',
      entry: parseJson(row.entry_json, {}),
      row_label: row.label || '', row_display_name: row.display_name || '',
      created_at: row.created_at || 0, updated_at: row.updated_at || 0,
      last_activity_at: row.last_activity_at || 0,
      archived_at: row.archived_at || 0, pinned_at: row.pinned_at || 0,
      row_status: row.status || '',
      parent_session_key: row.parent_session_key || '', spawned_by: row.spawned_by || '',
      project_id: row.project_id || '',
    }));
  } catch (_) {
    // 低版本库列不全时退回基础列
    try {
      return db.prepare('SELECT session_key, current_session_id, entry_json, updated_at FROM session_nodes').all()
        .map((row) => ({
          session_key: row.session_key || '', current_session_id: row.current_session_id || '',
          entry: parseJson(row.entry_json, {}), row_label: '', row_display_name: '',
          created_at: 0, updated_at: row.updated_at || 0, last_activity_at: 0,
          archived_at: 0, pinned_at: 0, row_status: '', parent_session_key: '', spawned_by: '', project_id: '',
        }));
    } catch (_) { return []; }
  } finally { if (db) db.close(); }
}

function scanSessions(stateDirs) {
  const sessions = [];
  const seenIds = new Set();
  for (const stateDir of stateDirs) {
    for (const source of collectSources(stateDir)) {
      if (source.kind === 'sqlite') {
        for (const node of sqliteNodes(source)) {
          const entry = node.entry;
          const sessionId = String(entry.sessionId || node.current_session_id || '');
          if (!sessionId || seenIds.has(sessionId)) continue;
          seenIds.add(sessionId);
          const title = entryTitle(entry) || node.row_label || node.row_display_name;
          sessions.push({
            session_id: sessionId,
            session_key: node.session_key,
            agent_id: source.agentId,
            title,
            directory: entryDirectory(entry),
            model_provider: String(entry.modelProvider || ''),
            model: String(entry.modelOverride || entry.model || ''),
            chat_type: String(entry.chatType || ''),
            status: String(node.row_status || entry.status || ''),
            created: node.created_at || timestampMs(entry.createdAt),
            updated: Math.max(node.updated_at || 0, node.last_activity_at || 0) || timestampMs(entry.updatedAt),
            archived: !!(node.archived_at || entry.archivedAt),
            pinned: !!(node.pinned_at || entry.pinnedAt),
            source: 'sqlite:' + source.path,
            source_kind: 'sqlite',
            sqlite_path: source.path,
            tokens: {
              input: Number(entry.inputTokens) || 0,
              output: Number(entry.outputTokens) || 0,
              total: Number(entry.totalTokens) || 0,
            },
            cost: Number(entry.estimatedCostUsd) || 0,
            goal: (entry.goal && typeof entry.goal === 'object') ? entry.goal : null,
            compaction_count: Number(entry.compactionCount) || 0,
            _checkpoints: Array.isArray(entry.compactionCheckpoints) ? entry.compactionCheckpoints : [],
          });
        }
      } else {
        for (const { sessionKey, entry } of legacyStoreEntries(source)) {
          const sessionId = String(entry.sessionId || '');
          if (!sessionId || seenIds.has(sessionId)) continue;
          // 正文 JSONL 与 sessions.json 同目录（resolveSessionFilePathCore 的相对路径约定）
          const transcript = path.join(source.sessionsDir, `${sessionId}.jsonl`);
          const header = readJsonlHeader(transcript);
          seenIds.add(sessionId);
          sessions.push({
            session_id: sessionId,
            session_key: sessionKey,
            agent_id: source.agentId,
            title: entryTitle(entry),
            directory: entryDirectory(entry) || header.cwd,
            model_provider: String(entry.modelProvider || ''),
            model: String(entry.modelOverride || entry.model || ''),
            chat_type: String(entry.chatType || ''),
            status: String(entry.status || ''),
            created: timestampMs(entry.createdAt),
            updated: timestampMs(entry.updatedAt) || fileMtime(transcript),
            archived: !!entry.archivedAt,
            pinned: !!entry.pinnedAt,
            source: 'legacy:' + source.path,
            source_kind: 'legacy',
            transcript_path: transcript,
            tokens: {
              input: Number(entry.inputTokens) || 0,
              output: Number(entry.outputTokens) || 0,
              total: Number(entry.totalTokens) || 0,
            },
            cost: Number(entry.estimatedCostUsd) || 0,
            goal: (entry.goal && typeof entry.goal === 'object') ? entry.goal : null,
            compaction_count: Number(entry.compactionCount) || 0,
            _checkpoints: Array.isArray(entry.compactionCheckpoints) ? entry.compactionCheckpoints : [],
          });
        }
      }
    }
  }
  sessions.sort((a, b) => (b.updated || b.created) - (a.updated || a.created));
  return sessions;
}

function fileMtime(filePath) {
  try { return Math.trunc(fs.statSync(filePath).mtimeMs); } catch (_) { return 0; }
}

// JSONL 首行会话头 {type:"session", id, timestamp, cwd}
function readJsonlHeader(transcriptPath) {
  const content = readTextFile(transcriptPath);
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const parsed = parseJson(trimmed, null);
    if (parsed && typeof parsed === 'object' && parsed.type === 'session') return parsed;
  }
  return {};
}

// ---------- 正文加载 ----------

function sqliteEvents(sqlitePath, sessionId) {
  let db;
  requireSqlite();
  try { db = new DatabaseSync(sqlitePath, { readOnly: true }); } catch (error) {
    console.error(`错误：无法打开 SQLite 库 ${sqlitePath}：${error.message}`);
    process.exit(1);
  }
  try {
    // 优先读活动分支投影；无投影或表不存在时回退为按 seq 全量
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
  } catch (error) {
    console.error(`错误：读取会话 ${sessionId} 的 transcript_events 失败：${error.message}`);
    process.exit(1);
  } finally { if (db) db.close(); }
}

// JSONL：条目树（parentId + leaf 指针）。存在 leaf 时沿最后一条 leaf 的 targetId
// 回溯父链得到当前分支；否则按文件顺序全部采用（与旧版行为一致）。
function jsonlBranch(transcriptPath) {
  const content = readTextFile(transcriptPath);
  const entries = [];
  let header = null;
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const parsed = parseJson(trimmed, null);
    if (!parsed || typeof parsed !== 'object') continue;
    if (!header && parsed.type === 'session') { header = parsed; continue; }
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

// 条目归一化：消息 -> user/assistant 文本与工具调用；compaction/branch_summary -> 摘要。
// 摘要单独呈现于「历史摘要」，条目全量保留（近期对话取尾部窗口，其余进「更早活动」）。
function normalizeEntries(entries) {
  const items = [], summaries = [];
  for (const entry of entries) {
    const type = entry.type || '';
    const ts = timestampMs(entry.timestamp);
    if (type === 'message') {
      const message = (entry.message && typeof entry.message === 'object') ? entry.message : {};
      const role = message.role || '';
      if (role === 'user') {
        const text = messageContentText(message.content).trim();
        if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      } else if (role === 'assistant') {
        const blocks = Array.isArray(message.content) ? message.content : [];
        const text = blocks.filter((b) => b && b.type === 'text').map((b) => String(b.text || '')).join('\n').trim();
        if (text) items.push({ kind: 'assistant_text', timestamp: ts, text });
        for (const block of blocks) {
          if (!block || block.type !== 'toolCall') continue;
          let input = block.arguments;
          if (!input || typeof input !== 'object' || Array.isArray(input)) {
            input = (input == null || input === '') ? {} : { raw: input };
          }
          items.push({ kind: 'tool_use', timestamp: ts, name: String(block.name || 'tool'), input, tool_use_id: String(block.id || '') });
        }
        if (message.stopReason === 'error' && message.errorMessage) {
          items.push({ kind: 'error_text', timestamp: ts, text: String(message.errorMessage) });
        }
      } else if (role === 'toolResult') {
        items.push({
          kind: 'tool_result',
          timestamp: ts,
          tool_use_id: String(message.toolCallId || ''),
          content: messageContentText(message.content),
          is_error: !!message.isError,
        });
      }
    } else if (type === 'compaction') {
      if (typeof entry.summary === 'string' && entry.summary.trim()) {
        summaries.push({ summary: entry.summary.trim(), first_kept_entry_id: String(entry.firstKeptEntryId || ''), timestamp: ts });
      }
    } else if (type === 'branch_summary') {
      if (typeof entry.summary === 'string' && entry.summary.trim()) {
        summaries.push({ summary: entry.summary.trim(), first_kept_entry_id: String(entry.fromId || ''), timestamp: ts });
      }
    } else if (type === 'custom_message') {
      const text = messageContentText(entry.content).trim();
      if (entry.display !== false && text) items.push({ kind: 'extension_text', timestamp: ts, text });
    }
    // thinking 块、label/session_info/reset/model_change/thinking_level_change/custom/leaf 等不进入接管摘要
  }
  return { items, summaries };
}

function messageContentText(content) {
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

function loadSessionFull(meta) {
  let entries;
  if (meta.source_kind === 'sqlite') {
    entries = sqliteEvents(meta.sqlite_path, meta.session_id);
  } else {
    entries = jsonlBranch(meta.transcript_path).entries;
  }
  const normalized = normalizeEntries(entries);
  const timestamps = normalized.items.map((item) => item.timestamp).filter(Boolean);
  const checkpoints = meta._checkpoints || [];
  const checkpointSummaries = checkpoints
    .map((cp) => (cp && typeof cp.summary === 'string' ? cp.summary.trim() : ''))
    .filter(Boolean);
  const info = {
    session_id: meta.session_id,
    session_key: meta.session_key,
    agent: meta.agent_id,
    title: meta.title || meta.session_id,
    directory: meta.directory,
    chat_type: meta.chat_type,
    status: meta.status,
    model: meta.model,
    model_provider: meta.model_provider,
    tokens: meta.tokens,
    cost: meta.cost,
    archived: meta.archived,
    pinned: meta.pinned,
    created: fmtTime(meta.created),
    updated: fmtTime(meta.updated),
    first_ts: fmtTime(timestamps[0] || meta.created),
    last_ts: fmtTime(timestamps[timestamps.length - 1] || meta.updated),
    source: meta.source,
    compaction_count: meta.compaction_count + checkpointSummaries.length,
  };
  return { info, normalized, checkpoint_summaries: checkpointSummaries };
}

// ---------- 任务状态重建 ----------

function classifyTool(name) {
  const lowered = String(name || '').toLowerCase();
  if (!lowered) return 'other';
  if (SHELL_TOOLS.has(lowered) || SHELL_HINT_RE.test(lowered)) return 'shell';
  if (EDIT_TOOLS.has(lowered) || EDIT_HINT_RE.test(lowered)) return 'edit';
  if (READ_TOOLS.has(lowered) || READ_HINT_RE.test(lowered)) return 'read';
  return 'other';
}

function toolFile(name, input) {
  for (const key of ['path', 'file_path', 'filePath', 'file', 'filename', 'directory', 'url']) {
    const value = input[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  for (const key of ['pattern', 'query', 'search']) {
    if (input[key]) return String(input[key]);
  }
  for (const value of Object.values(input)) {
    if (typeof value === 'string' && (value.includes('/') || value.includes('\\')) && value.length < 260) return value;
  }
  return '';
}

function shellCommand(input) {
  for (const key of ['command', 'cmd', 'script']) {
    const value = input[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function buildState(items) {
  const filesRead = [], filesEdited = [], commands = [], testResults = [];
  const calls = new Map();
  let firstUser = '', lastUser = '', lastAssistant = '';
  for (const item of items) {
    const kind = item.kind;
    if (kind === 'user_text') {
      firstUser = firstUser || item.text;
      lastUser = item.text;
    } else if (kind === 'assistant_text') {
      lastAssistant = item.text;
    } else if (kind === 'error_text') {
      testResults.push({ command_hint: 'assistant error', is_error: true, content: item.text });
    } else if (kind === 'tool_use') {
      const name = String(item.name || '').toLowerCase();
      const input = item.input || {};
      calls.set(item.tool_use_id || `#${calls.size}`, [name, input]);
      const category = classifyTool(name);
      if (category === 'read') filesRead.push(toolFile(name, input));
      else if (category === 'edit') filesEdited.push(toolFile(name, input));
      else if (category === 'shell') commands.push(shellCommand(input));
    } else if (kind === 'tool_result') {
      const call = calls.get(item.tool_use_id) || ['', {}];
      const content = item.content || '';
      const command = SHELL_TOOLS.has(call[0]) ? shellCommand(call[1]) : '';
      if (content.trim() && (item.is_error || TEST_CMD_RE.test(command) || TEST_RESULT_RE.test(content.slice(0, 2000)))) {
        testResults.push({ command_hint: command || call[0], is_error: !!item.is_error, content });
      }
    }
  }
  return {
    goal: firstUser, files_read: dedupe(filesRead), files_edited: dedupe(filesEdited),
    commands: dedupe(commands), test_results: testResults, last_user: lastUser, last_assistant: lastAssistant,
  };
}

// ---------- Markdown 渲染 ----------

function toolBrief(item) {
  const name = item.name || 'tool';
  const input = item.input || {};
  const detail = shellCommand(input) || toolFile(String(name).toLowerCase(), input);
  return detail ? `${name}(${truncate(detail, 100)})` : `${name}(...)`;
}

function renderItem(item, maxChars) {
  const ts = fmtTime(item.timestamp);
  if (item.kind === 'user_text') return [`### [用户] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'error_text') return [`### [错误] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'extension_text') return [`### [插件消息] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'} ${ts}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果]${item.is_error ? ' (错误)' : ''} ${ts}`, textBlock(item.content, maxChars)];
  return [];
}

function formatTokens(tokens) {
  const parts = [];
  if (tokens.input) parts.push(`输入 ${tokens.input}`);
  if (tokens.output) parts.push(`输出 ${tokens.output}`);
  if (tokens.total && tokens.total !== (tokens.input + tokens.output)) parts.push(`总计 ${tokens.total}`);
  return parts.join(' / ');
}

function renderSummary(session, state, recentN, maxChars) {
  const info = session.info, norm = session.normalized;
  const lines = [
    '# Resume-OpenClaw 会话接管摘要', '', '## 会话信息',
    `- 标题: ${truncate(info.title, 120)}`,
    `- 会话ID: ${info.session_id}`,
    `- 会话Key: ${info.session_key || '(未知)'}${info.agent ? `（agent: ${info.agent}）` : ''}`,
    `- 项目: ${info.directory || '(未知)'}`,
    `- 存储: ${info.source}`,
  ];
  if (info.chat_type) lines.push(`- 聊天类型: ${info.chat_type}`);
  if (info.model) lines.push(`- 模型: ${info.model_provider ? info.model_provider + '/' : ''}${info.model}`);
  const tokenLine = formatTokens(info.tokens || {});
  if (tokenLine) lines.push(`- Token 用量: ${tokenLine}`);
  if (info.cost) lines.push(`- 累计费用: $${Number(info.cost).toFixed(4)}`);
  if (info.status) lines.push(`- 运行状态: ${info.status}`);
  if (info.archived) lines.push('- 状态: 已归档');
  if (info.pinned) lines.push('- 状态: 已置顶');
  lines.push(`- 时间范围: ${info.first_ts || info.created || '(未知)'} ~ ${info.last_ts || info.updated || '(未知)'}`);
  lines.push(`- 消息条目数: ${norm.items.length}`);
  lines.push('');
  const summaries = [
    ...norm.summaries.map((item) => ({ text: item.summary, source: 'transcript' })),
    ...session.checkpoint_summaries.map((text) => ({ text, source: 'checkpoint' })),
  ];
  if (summaries.length) {
    lines.push('## 历史摘要（原会话 compact）');
    for (const item of summaries.slice(-3)) lines.push(`- ${truncate(item.text, maxChars)}`);
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
      const first = result.content.trim().split(/\r?\n/)[0] || '';
      lines.push(`-${result.is_error ? ' [错误]' : ''} ${truncate(first, 200)}`);
    }
    lines.push('');
  }
  lines.push('### 最近用户消息', textBlock(state.last_user, maxChars) || '(无)', '', '### 最近助手消息', textBlock(state.last_assistant, maxChars) || '(无)', '');
  const recent = recentN ? norm.items.slice(-recentN) : [];
  lines.push(`## 近期对话（最近 ${recent.length} 条）`, '');
  for (const item of recent) lines.push(...renderItem(item, maxChars), '');
  const olderTools = recentN ? norm.items.slice(0, Math.max(norm.items.length - recentN, 0)).filter((item) => item.kind === 'tool_use') : [];
  if (olderTools.length) {
    lines.push('## 更早活动（工具调用，仅最近 60 条）');
    for (const item of olderTools.slice(-60)) lines.push(`- [${fmtTime(item.timestamp)}] ${toolBrief(item)}`);
    lines.push('');
  }
  lines.push(
    '## 接管建议',
    '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。',
    '- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。',
    '- 不要逐字复述历史；基于现状决定下一步动作。',
    '- 若想让 OpenClaw 自己原生续接：在同会话渠道继续对话即可（网关按 sessionKey 恢复上下文）；CLI 可用 `openclaw sessions` 查看会话。',
    '',
  );
  return lines.join('\n');
}

// ---------- CLI ----------

function projectSessions(sessions, projectPath) {
  const target = normPath(projectPath);
  return sessions.filter((meta) => normPath(meta.directory) === target);
}

function pickSession(sessions, sessionArg, projectPath) {
  if (sessionArg) {
    return sessions.find((meta) => meta.session_id.startsWith(sessionArg) || meta.session_id.includes(sessionArg)) || null;
  }
  return projectSessions(sessions, projectPath)[0] || null;
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
    const flags = [meta.archived ? '已归档' : '', meta.pinned ? '置顶' : ''].filter(Boolean).join('/');
    const agentLabel = meta.agent_id ? `[${meta.agent_id}] ` : '';
    const keyLabel = meta.session_key && meta.session_key !== meta.session_id ? ` key=${meta.session_key}` : '';
    console.log(`${mark} ${timeLabel}  ${meta.session_id.slice(0, 16)}  ${agentLabel}${flags ? flags + ' ' : ''}标题: ${meta.title || '(无标题)'}${keyLabel}`);
  });
}

function printHelp() {
  console.log(`用法: node resume_openclaw.js [选项]

读取 OpenClaw（原 Clawdbot/Moltbot）本地会话，生成结构化接管摘要。

选项:
  --list                 仅列出当前项目会话
  --latest               取最近一个会话（默认）
  --session ID           指定会话 ID 或前缀；跨项目查找
  --project PATH         项目路径，默认当前目录
  --openclaw-dir DIR     OpenClaw 状态目录（~/.openclaw 一级），默认自动探测
  --recent N             近期条目数，默认 8
  --max-chars N          单条截断长度，默认 1500
  --limit N              --list 数量上限，0 不限制
  --json                 输出 JSON
  --output FILE          将摘要写入文件
  -h, --help             显示帮助

数据来源:
  状态目录（OPENCLAW_STATE_DIR > ~/.openclaw > 旧版 ~/.clawdbot）下：
  - 当前版本：agents/<agentId>/agent/openclaw-agent.sqlite（session_nodes/transcript_events）
  - 旧版：agents/<agentId>/sessions/sessions.json + <sessionId>.jsonl、根级 sessions/sessions.json`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: process.cwd(), openclawDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
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
    else if (arg === '--openclaw-dir') args.openclawDir = expandTilde(value(arg, i++));
    else if (arg.startsWith('--openclaw-dir=')) args.openclawDir = expandTilde(arg.slice(15));
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

  const stateDirs = stateDirCandidates(args.openclawDir);
  if (!stateDirs.some((dir) => isDir(dir))) {
    console.error(`错误：未找到 OpenClaw 状态目录（已探测 ${stateDirs.join('、')}）。`);
    console.error('可用 --openclaw-dir 指定状态目录，或设置 OPENCLAW_STATE_DIR 环境变量。');
    process.exit(1);
  }
  const sessions = scanSessions(stateDirs);
  if (!sessions.length) {
    console.error('错误：状态目录下未找到任何 OpenClaw 会话（SQLite session_nodes 或 sessions.json）。');
    process.exit(1);
  }
  const projectPath = path.resolve(args.project);
  if (args.list) {
    if (!projectSessions(sessions, projectPath).length) {
      console.error(`错误：未找到项目 ${projectPath} 的 OpenClaw 会话。可用 --session ID 跨项目查找。`);
      process.exit(1);
    }
    printList(sessions, projectPath, args.limit);
    return;
  }
  const target = pickSession(sessions, args.session, projectPath);
  if (!target) { console.error(`错误：未匹配到会话 '${args.session || '当前项目'}'。`); process.exit(1); }
  let session;
  try { session = loadSessionFull(target); } catch (error) { console.error('错误：解析会话失败：' + error.message); process.exit(1); }
  const state = buildState(session.normalized.items);
  const recentCount = Math.max(args.recent, 0);
  const recentItems = recentCount ? session.normalized.items.slice(-recentCount) : [];
  const output = args.json
    ? JSON.stringify({
        info: session.info,
        state,
        summaries: [
          ...session.normalized.summaries.map((item) => item.summary),
          ...session.checkpoint_summaries,
        ],
        recent_items: recentItems,
      }, null, 2)
    : renderSummary(session, state, recentCount, Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output.endsWith('\n') ? output : output + '\n', 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
