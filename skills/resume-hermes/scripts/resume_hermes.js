#!/usr/bin/env node
// -*- coding: utf-8 -*-
/** 读取 Hermes Agent 本地 SQLite 会话库（<hermes_home>/state.db），生成接管摘要。 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const CST_OFFSET_MS = 8 * 3600 * 1000;
// 压缩交接摘要的识别前缀（含历史版本），见 agent/context_compressor.py 的 SUMMARY_PREFIX
const COMPACTION_PREFIXES = ['[CONTEXT COMPACTION', '[CONTEXT SUMMARY]:'];
const SUMMARY_PREFIX_CUT = 'avoid repeating it:';
const SUMMARY_END_MARKER_RE = /^--- END OF CONTEXT SUMMARY[^\n]*---\s*$/gm;
const READ_TOOLS = new Set([
  'read_file', 'search_files', 'session_search', 'skill_view', 'skills_list',
  'web_search', 'web_extract', 'vision_analyze', 'browser_navigate',
]);
const EDIT_TOOLS = new Set(['write_file', 'patch']);
const SHELL_TOOLS = new Set(['terminal', 'process', 'execute_code']);
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;

function defaultHermesDir() {
  // 与 hermes_constants.get_hermes_home 的平台默认一致
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

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (_) { return false; } }

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function sqlQuote(value) { return "'" + String(value).replace(/'/g, "''") + "'"; }

function fmtTime(value) {
  if (!value) return '';
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '';
  const d = new Date(seconds * 1000 + CST_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

function requireSqlite() {
  if (!DatabaseSync) {
    console.error('错误：当前 Node 不支持 node:sqlite（需要 Node.js 22.5+）。');
    console.error('请改用 Python：python -X utf8 scripts/resume_hermes.py ...');
    process.exit(1);
  }
}

function openDb(dbFile) {
  requireSqlite();
  if (!isFile(dbFile)) throw new Error('数据库不存在：' + dbFile);
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

function buildEntry(session, lastActive, activeCounts) {
  return {
    id: session.id,
    session,
    source: session.source || '',
    chat_type: session.chat_type || '',
    session_key: session.session_key || '',
    model_config: parseJson(session.model_config),
    parent_session_id: session.parent_session_id || '',
    started_at: Number(session.started_at || 0),
    end_reason: session.end_reason || '',
    title: String(session.display_name || session.title || '').trim(),
    archived: !!session.archived,
    last_active: lastActive.get(session.id) || fallbackTime(session),
    active_messages: activeCounts.get(session.id) || 0,
  };
}

function scanSessions(dataDir) {
  const file = dbPath(dataDir);
  if (!isFile(file) || !DatabaseSync) return [];
  let db;
  try {
    db = openDb(file);
    const tables = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((row) => row.name));
    if (!['sessions', 'messages'].every((name) => tables.has(name))) return [];
    const messagesCols = tableColumns(db, 'messages');
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
    const activeWhere = messagesCols.has('active') ? '(active = 1 OR active IS NULL)' : '1=1';
    const activeCounts = new Map();
    for (const row of db.prepare(`SELECT session_id, COUNT(*) AS n FROM messages WHERE ${activeWhere} GROUP BY session_id`).all()) {
      activeCounts.set(row.session_id, row.n);
    }
    return sessions.map((session) => buildEntry(session, lastActive, activeCounts));
  } catch (_) { return []; }
  finally { if (db) db.close(); }
}

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
      chain,
      root: chain[0],
      tip: chain[chain.length - 1],
      chain_ids: chain.map((item) => item.id),
      chain_length: chain.length,
      compaction_count: Math.max(chain.length - 1, chain.filter((item) => item.end_reason === 'compression').length),
      paths,
      title: titles[0] || '',
      last_active: Math.max(...chain.map((item) => item.last_active)),
      active_messages: chain.reduce((sum, item) => sum + item.active_messages, 0),
      archived: chain.some((item) => item.archived),
    });
  }
  results.sort((a, b) => (b.last_active - a.last_active) || (a.root.id < b.root.id ? -1 : 1));
  return results;
}

function loadMessages(dataDir, chainIds) {
  const db = openDb(dbPath(dataDir));
  try {
    const available = tableColumns(db, 'messages');
    const optional = ['active', 'compacted', '_compressed_summary'].filter((c) => available.has(c));
    const cols = ['id', 'session_id', 'role', 'content', 'tool_call_id', 'tool_calls',
      'tool_name', 'timestamp', 'finish_reason'].concat(optional).join(', ');
    let compactedCount = 0;
    const rows = [];
    for (const sessionId of chainIds) {
      const where = [`session_id = ${sqlQuote(sessionId)}`];
      if (optional.includes('active')) where.push('(active = 1 OR active IS NULL)');
      if (optional.includes('compacted')) where.push('(compacted = 0 OR compacted IS NULL)');
      rows.push(...db.prepare(`SELECT ${cols} FROM messages WHERE ${where.join(' AND ')} ORDER BY id`).all()
        .map((row) => ({ ...row })));
      if (optional.includes('compacted')) {
        compactedCount += db.prepare(`SELECT COUNT(*) AS n FROM messages WHERE session_id = ${sqlQuote(sessionId)} AND compacted = 1`).get().n;
      }
    }
    return [rows, compactedCount];
  } finally { db.close(); }
}

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

function normalizeMessages(rows) {
  const items = [];
  const summaries = [];
  for (const row of rows) {
    const role = row.role || '';
    const content = row.content;
    const timestamp = Number(row.timestamp || 0);
    if (role === 'session_meta') continue;
    if (isSummaryRow(row)) {
      const body = cleanSummaryBody(content || '');
      if (body) summaries.push({ timestamp, text: body });
      continue;
    }
    if (role === 'user') {
      const text = String(content || '').trim();
      if (text) items.push({ kind: 'user_text', timestamp, text });
    } else if (role === 'assistant') {
      const text = String(content || '').trim();
      if (text) items.push({ kind: 'assistant_text', timestamp, text });
      const calls = parseJson(row.tool_calls, null);
      if (Array.isArray(calls)) {
        for (const call of calls) {
          if (!call || typeof call !== 'object') continue;
          const fn = call.function || {};
          const name = fn.name || call.name || 'tool';
          const rawArgs = 'arguments' in fn ? fn.arguments : call.arguments;
          const input = rawArgs && typeof rawArgs === 'object' ? rawArgs : parseJson(rawArgs);
          items.push({
            kind: 'tool_use',
            timestamp,
            name: String(name),
            input,
            tool_use_id: call.id || call.call_id || '',
          });
        }
      }
    } else if (role === 'tool') {
      const [text, isError] = toolResultContent(content);
      items.push({
        kind: 'tool_result',
        timestamp,
        tool_use_id: row.tool_call_id || '',
        name: row.tool_name || '',
        content: text,
        is_error: isError,
      });
    } else if (role === 'system') {
      const text = String(content || '').trim();
      if (text) items.push({ kind: 'system_text', timestamp, text });
    }
  }
  items.sort((a, b) => a.timestamp - b.timestamp);
  return [items, summaries];
}

function toolFile(name, input) {
  for (const key of ['path', 'filePath', 'file_path', 'filename']) {
    if (input[key]) return String(input[key]);
  }
  if (name === 'search_files') return String(input.pattern || input.query || '');
  if (name === 'web_search') return String(input.query || '');
  if (name === 'web_extract') return Array.isArray(input.urls) ? input.urls.map(String).join(' ') : String(input.urls || '');
  if (name === 'browser_navigate') return String(input.url || '');
  return '';
}

function shellCommand(input) {
  for (const key of ['command', 'cmd', 'code']) {
    if (input[key]) return String(input[key]).replace(/[\r\n]+/g, ' ; ').trim(); // execute_code 等工具的代码是多行文本，折叠换行保持列表可读
  }
  if (input.action) return `${input.action} ${input.id || ''}`.trim();
  return '';
}

function dedupe(values) {
  const seen = new Set(), output = [];
  for (const value of values) if (value && !seen.has(value)) { seen.add(value); output.push(value); }
  return output;
}

function buildState(items) {
  const filesRead = [], filesEdited = [], commands = [], testResults = [];
  let firstUser = '', lastUser = '', lastAssistant = '', pendingCommand = '';
  for (const item of items) {
    if (item.kind === 'user_text') { firstUser = firstUser || item.text; lastUser = item.text; }
    else if (item.kind === 'assistant_text') lastAssistant = item.text;
    else if (item.kind === 'tool_use') {
      const name = String(item.name || '').toLowerCase(), input = item.input || {};
      pendingCommand = SHELL_TOOLS.has(name) ? shellCommand(input) : '';
      if (READ_TOOLS.has(name)) filesRead.push(toolFile(name, input));
      else if (EDIT_TOOLS.has(name)) filesEdited.push(toolFile(name, input));
      else if (SHELL_TOOLS.has(name) && pendingCommand) commands.push(pendingCommand);
    } else if (item.kind === 'tool_result') {
      const content = item.content || '';
      const name = String(item.name || '').toLowerCase();
      if ((item.is_error || TEST_CMD_RE.test(pendingCommand) || TEST_RESULT_RE.test(content.slice(0, 2000))) && content.trim()) {
        testResults.push({ command_hint: pendingCommand || name, is_error: !!item.is_error, content });
      }
    }
  }
  return {
    goal: firstUser, files_read: dedupe(filesRead), files_edited: dedupe(filesEdited),
    commands: dedupe(commands), test_results: testResults, last_user: lastUser, last_assistant: lastAssistant,
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
  const detail = shellCommand(input) || toolFile(String(name).toLowerCase(), input)
    || String(input.url || input.query || input.pattern || '');
  return detail ? `${name}(${truncate(detail, 100)})` : `${name}(...)`;
}

function renderItem(item, maxChars) {
  const ts = fmtTime(item.timestamp);
  if (item.kind === 'user_text') return [`### [用户] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'system_text') return [`### [系统] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'} ${ts}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果]${item.is_error ? ' (错误)' : ''} ${ts}`, textBlock(item.content, maxChars)];
  return [];
}

function buildInfo(entry, summaries, compactedRows, dataDir) {
  const root = entry.root, tip = entry.tip;
  const items = entry._items || [];
  const timestamps = items.filter((item) => item.timestamp).map((item) => item.timestamp);
  return {
    session_id: root.id,
    tip_id: tip.id,
    chain_length: entry.chain_length,
    compaction_count: entry.compaction_count,
    title: entry.title || root.id,
    source: root.source,
    chat_type: root.chat_type,
    session_key: root.session_key,
    parent_session_id: root.parent_session_id,
    project: entry.paths[0] || '',
    cwd: root.session.cwd || '',
    git_branch: tip.session.git_branch || root.session.git_branch || '',
    git_repo_root: root.session.git_repo_root || '',
    model: tip.session.model || root.session.model || '',
    archived: entry.archived,
    end_reason: tip.end_reason,
    started: fmtTime(root.started_at),
    first_ts: fmtTime(timestamps[0] || root.started_at),
    last_ts: fmtTime(timestamps[timestamps.length - 1] || entry.last_active),
    message_count: entry.active_messages,
    item_count: items.length,
    compaction_summaries: summaries.length,
    compacted_rows: compactedRows,
    hermes_home: String(dataDir),
    db_path: dbPath(dataDir),
  };
}

function renderSummary(info, state, summaries, recentItems, olderItems, maxChars) {
  const lines = [
    '# Resume-Hermes 会话接管摘要', '', '## 会话信息',
    `- 标题: ${info.title}`, `- 根会话ID: ${info.session_id}`,
  ];
  if (info.tip_id !== info.session_id) lines.push(`- 当前会话ID: ${info.tip_id}`);
  if (info.chain_length > 1) lines.push(`- 会话链: 共 ${info.chain_length} 段（${info.compaction_count} 次压缩续链）`);
  let source = info.source || '(未知)';
  if (info.chat_type) source += `（${info.chat_type}）`;
  lines.push(`- 来源: ${source}`, `- 项目: ${info.project || '(未知)'}`);
  if (info.git_branch) lines.push(`- Git 分支: ${info.git_branch}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  lines.push(`- 时间范围: ${info.first_ts || '(无)'} ~ ${info.last_ts || '(无)'}`, `- 消息条目数: ${info.item_count}`);
  if (info.compacted_rows) lines.push(`- 已压缩原始消息: ${info.compacted_rows} 条（不再展开）`);
  if (info.archived) lines.push('- 状态: 已归档');
  lines.push('');
  if (summaries.length) {
    lines.push(`## 历史摘要（原会话压缩交接，共 ${summaries.length} 份）`, '');
    summaries.forEach((summary, index) => {
      lines.push(`### 摘要 ${index + 1} · ${fmtTime(summary.timestamp) || '(无时间)'}`, textBlock(summary.text, maxChars), '');
    });
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
    lines.push('### 测试 / 错误结果', '');
    for (const result of state.test_results.slice(-5)) {
      const first = result.content.trim().split(/\r?\n/)[0] || '';
      lines.push(`-${result.is_error ? ' [错误]' : ''} ${truncate(first, 200)}`);
    }
    lines.push('');
  }
  lines.push('### 最近用户消息', textBlock(state.last_user, maxChars) || '(无)', '', '### 最近助手消息', textBlock(state.last_assistant, maxChars) || '(无)', '');
  lines.push(`## 近期对话（最近 ${recentItems.length} 条）`, '');
  for (const item of recentItems) lines.push(...renderItem(item, maxChars), '');
  if (olderItems.length) {
    lines.push('## 更早活动（工具调用，仅最近 60 条）', '');
    for (const item of olderItems.slice(-60)) lines.push(`- [${fmtTime(item.timestamp)}] ${toolBrief(item)}`);
    lines.push('');
  }
  lines.push(
    '## 接管建议',
    '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。',
    '- 「历史摘要」中的压缩交接内容仅作背景参考：hermes 约定以最后一条真实用户消息为准，勿把历史待办当作当前任务。',
    '- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续；不要逐字复述历史。',
    '- 如需回到 Hermes 原生环境继续，可运行 `hermes --resume <会话ID>`。',
    '',
  );
  return lines.join('\n');
}

function entryMatchesProject(entry, projectPath) {
  const target = normPath(projectPath);
  return entry.paths.some((value) => {
    const normed = normPath(value);
    return normed === target || (normed && target.startsWith(normed.replace(/[\\/]+$/, '') + '/'));
  });
}

function projectEntries(entries, projectPath) {
  return entries.filter((entry) => entryMatchesProject(entry, projectPath));
}

function pickEntry(entries, sessionArg, projectPath) {
  if (sessionArg) {
    const needle = sessionArg.trim().toLowerCase();
    for (const entry of entries) {
      for (const sessionId of entry.chain_ids) {
        const id = sessionId.toLowerCase();
        if (id === needle || id.startsWith(needle) || id.includes(needle)) return entry;
      }
    }
    return null;
  }
  return entries.find((entry) => entryMatchesProject(entry, projectPath)) || null;
}

function entryTitleLine(entry) {
  const title = entry.title || entry.root.id;
  const chainTag = entry.chain_length > 1 ? `（${entry.chain_length - 1} 次压缩续链）` : '';
  const archivedTag = entry.archived ? '（已归档）' : '';
  return `${truncate(title, 60)}${chainTag}${archivedTag}`;
}

function listLine(entry) {
  const source = entry.root.source || '?';
  const project = entry.paths[0] || '(无项目路径)';
  return `${fmtTime(entry.last_active) || '(无时间)'}  ${entry.root.id}  [${source}] ${entry.tip.active_messages}条  项目: ${truncate(project, 40)}  标题: ${entryTitleLine(entry)}`;
}

function printList(entries, projectPath, limit) {
  const selected = projectEntries(entries, projectPath), shown = limit > 0 ? selected.slice(0, limit) : selected;
  console.log(`当前项目: ${projectPath}`);
  console.log(`找到 ${selected.length} 个会话${shown.length < selected.length ? `（仅显示最近 ${shown.length} 个）` : ''}：\n`);
  shown.forEach((entry, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    console.log(`${mark} ${listLine(entry)}`);
  });
}

function printListAll(entries, limit) {
  const shown = limit > 0 ? entries.slice(0, limit) : entries;
  console.log(`共 ${entries.length} 个会话${shown.length < entries.length ? `（仅显示最近 ${shown.length} 个）` : ''}：\n`);
  shown.forEach((entry, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    console.log(`${mark} ${listLine(entry)}`);
  });
}

function printHelp() {
  console.log(`用法: node resume_hermes.js [选项]

读取 Hermes Agent 本地会话，生成结构化接管摘要。

选项:
  --list                 仅列出当前项目会话
  --list-all             列出全部会话（含无项目路径的网关/IM 会话）
  --latest               取最近一个会话（默认）
  --session ID           指定会话 ID 或前缀；匹配压缩续链上任意一段；跨项目查找
  --project PATH         项目路径，默认当前目录
  --hermes-dir DIR       Hermes 主目录（默认 HERMES_HOME 或 %LOCALAPPDATA%\\hermes、~/.hermes）
  --recent N             近期条目数，默认 8
  --max-chars N          单条截断长度，默认 1500
  --limit N              --list 数量上限，0 不限制
  --json                 输出 JSON
  --output FILE          将摘要写入文件
  -h, --help             显示帮助`);
}

function parseArgs(argv) {
  const args = { list: false, listAll: false, latest: false, session: null, project: process.cwd(), hermesDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
  const value = (flag, index) => { if (index + 1 >= argv.length) throw new Error(`${flag} 需要一个参数`); return argv[index + 1]; };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--list') args.list = true;
    else if (arg === '--list-all') args.listAll = true;
    else if (arg === '--latest') args.latest = true;
    else if (arg === '--json') args.json = true;
    else if (arg === '-h' || arg === '--help') args.help = true;
    else if (arg === '--session') args.session = value(arg, i++);
    else if (arg.startsWith('--session=')) args.session = arg.slice(10);
    else if (arg === '--project') args.project = value(arg, i++);
    else if (arg.startsWith('--project=')) args.project = arg.slice(10);
    else if (arg === '--hermes-dir') args.hermesDir = value(arg, i++);
    else if (arg.startsWith('--hermes-dir=')) args.hermesDir = arg.slice(13);
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
  const dataDir = path.resolve(args.hermesDir || defaultHermesDir());
  if (isFile(dbPath(dataDir))) requireSqlite();
  const entries = classifyChains(scanSessions(dataDir));
  if (!entries.length) { console.error(`错误：在 ${dataDir} 未找到 Hermes 会话（已检查 state.db）。`); process.exit(1); }
  const projectPath = path.resolve(args.project);
  if (args.list) {
    if (!projectEntries(entries, projectPath).length) {
      console.error(`错误：未找到项目 ${projectPath} 的 Hermes 会话。可用 --list-all 查看全部，或 --session ID 跨项目查找。`);
      process.exit(1);
    }
    printList(entries, projectPath, args.limit);
    return;
  }
  if (args.listAll) { printListAll(entries, args.limit); return; }
  const entry = pickEntry(entries, args.session, projectPath);
  if (!entry) { console.error(`错误：未匹配到会话 '${args.session || '当前项目'}'。`); process.exit(1); }
  let rows, compactedRows;
  try { [rows, compactedRows] = loadMessages(dataDir, entry.chain_ids); } catch (error) { console.error('错误：解析会话失败：' + error.message); process.exit(1); }
  const [items, summaries] = normalizeMessages(rows);
  entry._items = items;
  const state = buildState(items);
  const info = buildInfo(entry, summaries, compactedRows, dataDir);
  const recentCount = Math.max(args.recent, 0);
  const recentItems = recentCount ? items.slice(-recentCount) : [];
  const olderItems = (recentCount ? items.slice(0, -recentCount) : items).filter((item) => item.kind === 'tool_use');
  const output = args.json
    ? JSON.stringify({ info, state, summaries, recent_items: recentItems }, null, 2)
    : renderSummary(info, state, summaries, recentItems, olderItems, Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output, 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
