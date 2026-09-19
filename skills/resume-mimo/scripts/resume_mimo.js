#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-mimo: 读取 MiMo-Code（小米 MiMo，OpenCode fork）本地 SQLite 会话库，生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（MiMo / Claude Code / ZCode / Codex 等）均可直接调用：
 *   node resume_mimo.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_mimo.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 *
 * MiMo-Code 会话存储：
 *   - 数据目录：MIMOCODE_HOME 时为 <MIMOCODE_HOME>/data，否则 $XDG_DATA_HOME/mimocode，
 *     再否则 ~/.local/share/mimocode（README 亦提及 Windows %LOCALAPPDATA%\mimocode，均探测）
 *   - 数据库：<数据目录>/mimocode.db（latest/beta/prod 渠道）；其他渠道为 mimocode-<channel>.db；
 *     MIMOCODE_DB 可整体覆盖（绝对路径直接使用，相对路径相对数据目录，":memory:" 忽略）
 *   - 表：session / message / part（data 列为 JSON），另可读 todo 表
 *   - part 类型：text、tool、patch、file、compaction（projection.summary 为 compact 摘要）、
 *     checkpoint、subtask、reasoning、step-start/finish、snapshot、retry、agent 等
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (_) { DatabaseSync = null; }

const CST_OFFSET_MS = 8 * 3600 * 1000;
const READ_TOOLS = new Set(['read', 'glob', 'grep', 'list', 'codesearch']);
const EDIT_TOOLS = new Set(['write', 'edit', 'patch', 'apply_patch', 'multiedit']);
const SHELL_TOOLS = new Set(['bash', 'shell', 'shell_command', 'terminal']);
const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;

// ---------- 路径与数据库定位 ----------

function isDir(p) { try { return fs.statSync(p).isDirectory(); } catch (_) { return false; } }
function isFile(p) { try { return fs.statSync(p).isFile(); } catch (_) { return false; } }

function expandTilde(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

// MiMo 数据目录候选（按序探测；与 packages/shared/src/global.ts 的解析一致，含 README 提及的回退）。
function dataDirCandidates() {
  const candidates = [];
  if (process.env.MIMOCODE_HOME) candidates.push(path.join(expandTilde(process.env.MIMOCODE_HOME), 'data'));
  if (process.env.XDG_DATA_HOME) candidates.push(path.join(expandTilde(process.env.XDG_DATA_HOME), 'mimocode'));
  candidates.push(path.join(os.homedir(), '.local', 'share', 'mimocode'));
  if (process.env.LOCALAPPDATA) candidates.push(path.join(process.env.LOCALAPPDATA, 'mimocode'));
  candidates.push(path.join(os.homedir(), 'Library', 'Application Support', 'mimocode'));
  const seen = new Set();
  return candidates.filter((p) => {
    const key = path.resolve(p).toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// 数据目录内的数据库候选：MIMOCODE_DB > mimocode.db > mimocode-<channel>.db（按修改时间）。
function dbCandidates(dataDir) {
  const candidates = [];
  const envDb = process.env.MIMOCODE_DB;
  if (envDb && envDb !== ':memory:') {
    candidates.push(path.isAbsolute(envDb) ? envDb : path.join(dataDir, envDb));
  }
  candidates.push(path.join(dataDir, 'mimocode.db'));
  let channelDbs = [];
  try {
    channelDbs = fs.readdirSync(dataDir)
      .filter((name) => /^mimocode-.+\.db$/i.test(name))
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

function resolveDbPaths(mimoDir) {
  const dataDirs = mimoDir ? [mimoDir] : dataDirCandidates().filter(isDir);
  const dbs = [];
  const seen = new Set();
  for (const dir of dataDirs) {
    for (const db of dbCandidates(dir)) {
      const key = path.resolve(db).toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      dbs.push(db);
    }
  }
  return { dataDirs, dbs };
}

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

function requireSqlite() {
  if (!DatabaseSync) {
    console.error('错误：当前 Node 不支持 node:sqlite（需要 Node.js 22.5+）。');
    console.error('请改用 Python：python -X utf8 scripts/resume_mimo.py ...');
    process.exit(1);
  }
}

function parseJson(value, fallback = {}) {
  if (value && typeof value === 'object') return value;
  if (!value) return fallback;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function timestampMs(value) {
  if (value && typeof value === 'object') value = value.updated || value.completed || value.created;
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

// ---------- 会话扫描 ----------

function openDb(dbPath) {
  requireSqlite();
  return new DatabaseSync(dbPath, { readOnly: true });
}

function hasCurrentSchema(db) {
  const names = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((row) => row.name));
  return ['session', 'message', 'part'].every((name) => names.has(name));
}

function dbSessions(dbPath) {
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
      project_id: row.project_id,
      project_name: row.project_name || '',
      workspace_id: row.workspace_id || null,
      parent_id: row.parent_id || null,
      context_from: row.context_from || null,
      version: row.version || '',
      created: row.time_created,
      updated: row.time_updated,
      archived: row.time_archived,
      summary: { additions: row.summary_additions, deletions: row.summary_deletions, files: row.summary_files },
      source: 'sqlite',
      db_path: dbPath,
    }));
  } catch (_) { return []; }
  finally { if (db) db.close(); }
}

function scanSessions(dbPaths) {
  const sessions = [];
  for (const dbPath of dbPaths) sessions.push(...dbSessions(dbPath));
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

// ---------- 记录归一化 ----------

function messageTime(data, fallback) { return timestampMs((data.time || {}).created || fallback); }
function partTime(data, fallback) { return timestampMs((data.time || {}).start || fallback); }

function normalizeRecords(messages, partsByMessage, todos) {
  const items = [], summaries = [];
  let model = '', provider = '', agent = '', agentId = '';
  for (const message of messages) {
    const data = message.data, role = data.role || '', ts = messageTime(data, message.time_created);
    const modelInfo = data.model || {};
    model = data.modelID || modelInfo.modelID || model;
    provider = data.providerID || modelInfo.providerID || provider;
    agent = data.agent || agent;
    agentId = message.agent_id || agentId;
    if (role === 'assistant' && data.error) {
      const err = data.error;
      const detail = (err.data && err.data.message) || err.message || '';
      items.push({
        kind: 'error_text',
        timestamp: timestampMs((data.time || {}).completed) || ts,
        text: detail ? `${err.name || 'Error'}: ${detail}` : String(err.name || 'Error'),
      });
    }
    for (const part of partsByMessage.get(message.id) || []) {
      const pdata = part.data, type = pdata.type || '', pts = partTime(pdata, part.time_created || ts);
      if (type === 'text') {
        const text = pdata.text || '';
        if (!text.trim()) continue;
        if (pdata.synthetic || data.summary === true) summaries.push(text);
        else items.push({ kind: `${role}_text`, timestamp: pts, text });
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
        const label = pdata.filename || (pdata.source || {}).path || pdata.url || '附件';
        items.push({ kind: 'attachment', timestamp: pts, text: String(label) });
      } else if (type === 'compaction') {
        const summary = (pdata.projection || {}).summary || '';
        if (summary.trim()) summaries.push(summary);
      } else if (type === 'checkpoint') {
        items.push({
          kind: 'checkpoint', timestamp: pts,
          number: pdata.checkpointNumber, dir: pdata.checkpointDir || '',
        });
      } else if (type === 'subtask') {
        items.push({
          kind: 'subtask', timestamp: pts,
          agent: pdata.agent || '', description: pdata.description || '', prompt: pdata.prompt || '',
        });
      }
      // reasoning / step-start / step-finish / snapshot / retry / agent 等不进入接管摘要
    }
  }
  items.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return { items, summaries, model, provider, agent, agent_id: agentId, todos: todos || [] };
}

function loadSession(dbPath, meta) {
  const db = openDb(dbPath);
  try {
    // 旧版本库可能没有 message.agent_id 列（20260521 迁移才引入），回退到基础列。
    let messageRows;
    try {
      messageRows = db.prepare('SELECT id, agent_id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id')
        .all(meta.session_id);
    } catch (_) {
      messageRows = db.prepare('SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id')
        .all(meta.session_id);
    }
    const messages = messageRows.map((row) => ({ id: row.id, agent_id: row.agent_id || '', time_created: row.time_created, data: parseJson(row.data) }));
    const partsByMessage = new Map();
    for (const row of db.prepare('SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created, id').all(meta.session_id)) {
      if (!partsByMessage.has(row.message_id)) partsByMessage.set(row.message_id, []);
      partsByMessage.get(row.message_id).push({ id: row.id, time_created: row.time_created, data: parseJson(row.data) });
    }
    let todos = [];
    try {
      todos = db.prepare('SELECT content, status, priority, position FROM todo WHERE session_id=? ORDER BY position').all(meta.session_id)
        .map((row) => ({ content: row.content || '', status: row.status || '', priority: row.priority || '' }));
    } catch (_) { /* todo 表不存在时忽略 */ }
    return normalizeRecords(messages, partsByMessage, todos);
  } finally { db.close(); }
}

function loadSessionFull(meta) {
  const normalized = loadSession(meta.db_path, meta);
  const timestamps = normalized.items.filter((item) => item.timestamp).map((item) => item.timestamp);
  const info = {
    session_id: meta.session_id, title: meta.title, directory: meta.directory,
    project_id: meta.project_id, project_name: meta.project_name, workspace_id: meta.workspace_id,
    parent_id: meta.parent_id, context_from: meta.context_from,
    version: meta.version, model: normalized.model, provider: normalized.provider,
    agent: normalized.agent || normalized.agent_id, db: path.basename(meta.db_path),
    first_ts: fmtTime(timestamps[0] || meta.created),
    last_ts: fmtTime(timestamps[timestamps.length - 1] || meta.updated),
    archived: !!meta.archived,
    summary: meta.summary,
  };
  return { info, normalized };
}

// ---------- 任务状态重建 ----------

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

function buildState(items) {
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
    commands: dedupe(commands), test_results: testResults, last_user: lastUser, last_assistant: lastAssistant,
  };
}

// ---------- Markdown 渲染 ----------

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

function todoMark(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'completed') return '[x]';
  if (s === 'in_progress') return '[-]';
  return '[ ]';
}

function renderItem(item, maxChars) {
  const ts = fmtTime(item.timestamp);
  if (item.kind === 'user_text') return [`### [用户] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'assistant_text') return [`### [助手] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'error_text') return [`### [错误] ${ts}`, textBlock(item.text, maxChars)];
  if (item.kind === 'tool_use') return [`### [工具调用] ${item.name || 'tool'} ${ts}`, '```json', truncate(JSON.stringify(item.input || {}), maxChars), '```'];
  if (item.kind === 'tool_result') return [`### [工具结果]${item.is_error ? ' (错误)' : ''} ${ts}`, textBlock(item.content, maxChars)];
  if (item.kind === 'patch') return [`### [代码补丁] ${ts}`, (item.files || []).map((name) => `- ${name}`).join('\n')];
  if (item.kind === 'attachment') return [`### [附件] ${ts}`, item.text || ''];
  if (item.kind === 'checkpoint') return [`### [检查点] #${item.number == null ? '?' : item.number} ${ts}`, item.dir];
  if (item.kind === 'subtask') {
    const lines = [`### [子任务]${item.agent ? ` ${item.agent}` : ''} ${ts}`];
    if (item.description) lines.push(item.description);
    if (item.prompt) lines.push(blockOf(item.prompt, Math.min(maxChars, 600)));
    return lines;
  }
  return [];
}

function blockOf(text, limit) { return textBlock(text, limit); }

function renderSummary(session, state, recentN, maxChars) {
  const info = session.info, norm = session.normalized;
  const lines = [
    '# Resume-MiMo 会话接管摘要', '', '## 会话信息',
    `- 标题: ${info.title}`, `- 会话ID: ${info.session_id}`,
    `- 项目: ${info.directory || '(未知)'}`,
    `- 存储: ${info.db}`,
  ];
  if (info.model) lines.push(`- 模型: ${info.provider ? info.provider + '/' : ''}${info.model}`);
  if (info.agent) lines.push(`- Agent: ${info.agent}`);
  if (info.version) lines.push(`- MiMo 版本: ${info.version}`);
  if (info.workspace_id) lines.push(`- 工作区: ${info.workspace_id}`);
  if (info.parent_id) lines.push(`- 父会话: ${info.parent_id}`);
  if (info.context_from) lines.push(`- 上下文继承自: ${info.context_from}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`, `- 消息条目数: ${norm.items.length}`, '');
  if (norm.summaries.length) {
    lines.push('## 历史摘要（原会话 compact）');
    for (const value of norm.summaries) lines.push(`- ${truncate(value, maxChars)}`);
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
  if (norm.todos.length) {
    lines.push('### 任务清单（todo）');
    for (const todo of norm.todos) {
      lines.push(`- ${todoMark(todo.status)} ${truncate(todo.content, 200)}${todo.status ? `（${todo.status}）` : ''}`);
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
    for (const item of olderTools.slice(-60)) lines.push(`- [${fmtTime(item.timestamp)}] ${toolBrief(item)}`);
    lines.push('');
  }
  lines.push('## 接管建议', '- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。', '- 以「任务状态重建」和「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。', '- 不要逐字复述历史；基于现状决定下一步动作。', '');
  return lines.join('\n');
}

// ---------- CLI ----------

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
    console.log(`${mark} ${fmtTime(meta.updated) || '(无时间)'}  ${meta.session_id.slice(0, 12)}  标题: ${meta.title}`);
  });
}

function printHelp() {
  console.log(`用法: node resume_mimo.js [选项]

读取 MiMo-Code 本地会话（SQLite），生成结构化接管摘要。

选项:
  --list               仅列出当前项目会话
  --latest             取最近一个会话（默认）
  --session ID         指定会话 ID 或前缀；跨项目查找
  --project PATH       项目路径，默认当前目录
  --mimo-dir DIR       MiMo 数据目录（含 mimocode.db），默认自动探测
  --recent N           近期条目数，默认 8
  --max-chars N        单条截断长度，默认 1500
  --limit N            --list 数量上限，0 不限制
  --json               输出 JSON
  --output FILE        将摘要写入文件
  -h, --help           显示帮助

环境变量:
  MIMOCODE_HOME        MiMo 主目录（使用其下 data/ 作为数据目录）
  XDG_DATA_HOME        XDG 数据根目录（使用其下 mimocode/）
  MIMOCODE_DB          直接指定数据库文件（":memory:" 忽略）`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: process.cwd(), mimoDir: null, recent: 8, maxChars: 1500, limit: 0, json: false, output: null, help: false };
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
    else if (arg === '--mimo-dir') args.mimoDir = expandTilde(value(arg, i++));
    else if (arg.startsWith('--mimo-dir=')) args.mimoDir = expandTilde(arg.slice(11));
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

  const { dataDirs, dbs } = resolveDbPaths(args.mimoDir ? expandTilde(args.mimoDir) : null);
  if (dbs.length) requireSqlite();
  const sessions = scanSessions(dbs);
  if (!sessions.length) {
    const probed = args.mimoDir ? [args.mimoDir] : dataDirCandidates();
    console.error(`错误：未找到 MiMo-Code 会话（已探测数据目录：${probed.join('、') || '(无)'}）。`);
    console.error('可用 --mimo-dir 指定数据目录，或设置 MIMOCODE_HOME 环境变量。');
    process.exit(1);
  }
  const projectPath = path.resolve(args.project);
  if (args.list) {
    if (!projectSessions(sessions, projectPath).length) {
      console.error(`错误：未找到项目 ${projectPath} 的 MiMo 会话。可用 --session ID 跨项目查找。`);
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
    ? JSON.stringify({ info: session.info, state, todos: session.normalized.todos, summaries: session.normalized.summaries, recent_items: recentItems }, null, 2)
    : renderSummary(session, state, recentCount, Math.max(args.maxChars, 1));
  if (args.output) { fs.writeFileSync(args.output, output, 'utf-8'); console.error(`摘要已写入：${args.output}`); }
  else process.stdout.write(output + (output.endsWith('\n') ? '' : '\n'));
}

main();
