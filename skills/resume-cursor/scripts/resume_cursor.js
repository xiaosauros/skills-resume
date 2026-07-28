#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-cursor: 读取 Cursor IDE 本地 Agent/Composer 会话记录（SQLite state.vscdb），
 * 生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（Claude Code / Codex / Grok 等）均可直接调用：
 *   node resume_cursor.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_cursor.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 *
 * 注意：Node 实现使用内置 node:sqlite（需 Node.js 22.5+）。更低版本请改用 Python 实现。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// node:sqlite（Node 22.5+ 内置）。不可用时给出明确提示，引导使用 Python 实现。
let DatabaseSync = null;
try {
  ({ DatabaseSync } = require('node:sqlite'));
} catch (e) {
  DatabaseSync = null;
}

// 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude / resume-codex 一致）
const CST_OFFSET_MS = 8 * 3600 * 1000;


// ---------- 路径与项目定位 ----------

function getCursorDir() {
  if (process.env.CURSOR_HOME) return process.env.CURSOR_HOME;
  const p = process.platform;
  if (p === 'win32') {
    return path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Cursor');
  }
  if (p === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'Cursor');
  }
  return path.join(os.homedir(), '.config', 'Cursor');
}

function globalDbPath(cursorDir) {
  return path.join(cursorDir, 'User', 'globalStorage', 'state.vscdb');
}

function workspaceStorageDir(cursorDir) {
  return path.join(cursorDir, 'User', 'workspaceStorage');
}

function normPath(p) {
  // 统一小写 + 正斜杠，便于跨平台/跨分隔符匹配。
  return path.normalize(p).toLowerCase().replace(/\\/g, '/');
}

function isFileSafe(p) {
  try {
    return fs.statSync(p).isFile();
  } catch (e) {
    return false;
  }
}

function isDirSafe(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch (e) {
    return false;
  }
}

function decodeFolderUri(uri) {
  // workspace.json 的 folder 字段形如 file:///d%3A/workspace/xiaosauros/bot
  // 解码为文件系统路径（Windows 去掉盘符前的 /）。
  let p;
  try {
    p = decodeURIComponent(uri);
  } catch (e) {
    p = uri;
  }
  p = p.replace(/^file:\/\//, '');
  p = p.replace(/^\/([a-zA-Z]:)/, '$1');
  return p;
}


// ---------- 数据库 ----------

function requireSqlite() {
  if (!DatabaseSync) {
    console.error('错误：当前 Node 不支持 node:sqlite（需要 Node.js 22.5+）。');
    console.error('请改用 Python 实现：python -X utf8 scripts/resume_cursor.py ...');
    process.exit(1);
  }
}

function openDb(dbPath) {
  if (!isFileSafe(dbPath)) {
    throw new Error('数据库不存在：' + dbPath);
  }
  return new DatabaseSync(dbPath, { readOnly: true });
}


// ---------- composer 列表（标题 / 项目 / 时间来源） ----------

function modelFromComposer(v) {
  // composerData.modelConfig.modelName 通常是 "default"，无意义；仅在非 default 时返回。
  const mc = v && v.modelConfig;
  if (mc && typeof mc === 'object') {
    const name = mc.modelName;
    if (name && name !== 'default') return String(name);
  }
  return '';
}

function composerSummary(v, key) {
  // 从 composerData JSON 提取轻量摘要（不加载 bubble）。
  const cid = (v && v.composerId) || (key ? key.slice('composerData:'.length) : '');
  const uri = v && v.workspaceIdentifier && v.workspaceIdentifier.uri;
  const fsPath = uri ? uri.fsPath || '' : '';
  const headers = v && Array.isArray(v.fullConversationHeadersOnly) ? v.fullConversationHeadersOnly : [];
  return {
    session_id: cid,
    name: (v && v.name) || null,
    status: (v && v.status) || '',
    subtitle: (v && v.subtitle) || '',
    model: modelFromComposer(v),
    fs_path: fsPath,
    lastUpdatedAt: (v && v.lastUpdatedAt) || 0,
    createdAt: (v && v.createdAt) || 0,
    bubble_count: headers.length,
  };
}

function listComposers(db) {
  // 读取全局 state.vscdb 的 cursorDiskKV 表中所有 composerData:* 记录。
  const rows = db.prepare("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'").all();
  const out = [];
  for (const row of rows) {
    let v;
    try {
      v = JSON.parse(row.value);
    } catch (e) {
      continue;
    }
    if (!v || typeof v !== 'object') continue;
    out.push(composerSummary(v, row.key));
  }
  return out;
}

function scanSessions(db, projectPath) {
  // 主路径：按 composerData.workspaceIdentifier.uri.fsPath 匹配当前项目，按 lastUpdatedAt 倒序。
  const norm = normPath(projectPath);
  const sessions = listComposers(db)
    .filter((c) => c.fs_path && normPath(c.fs_path) === norm);
  sessions.sort((a, b) => (b.lastUpdatedAt || 0) - (a.lastUpdatedAt || 0));
  return sessions;
}

function scanAllSessions(db) {
  // 跨项目扫描：返回全局库中所有 composer，不按项目过滤。按 lastUpdatedAt 倒序。
  const sessions = listComposers(db);
  sessions.sort((a, b) => (b.lastUpdatedAt || 0) - (a.lastUpdatedAt || 0));
  return sessions;
}

function scanWorkspaceFallback(cursorDir, projectPath, db) {
  // 兜底路径：当全局库的 workspaceIdentifier 匹配不到时，扫描 workspaceStorage。
  // 通过 workspace.json 的 folder 字段定位项目，再从该工作区库的 composer.composerData 取 composerId。
  const wsRoot = workspaceStorageDir(cursorDir);
  if (!isDirSafe(wsRoot)) return [];
  const norm = normPath(projectPath);
  const sessions = [];
  let dirs;
  try {
    dirs = fs.readdirSync(wsRoot, { withFileTypes: true });
  } catch (e) {
    return [];
  }
  for (const d of dirs) {
    if (!d.isDirectory()) continue;
    const wj = path.join(wsRoot, d.name, 'workspace.json');
    let folder;
    try {
      folder = JSON.parse(fs.readFileSync(wj, 'utf-8')).folder;
    } catch (e) {
      continue;
    }
    if (!folder || normPath(decodeFolderUri(folder)) !== norm) continue;

    const wsDbPath = path.join(wsRoot, d.name, 'state.vscdb');
    if (!isFileSafe(wsDbPath)) continue;
    let wsDb;
    try {
      wsDb = openDb(wsDbPath);
    } catch (e) {
      continue;
    }
    const ids = new Set();
    try {
      const r = wsDb.prepare("SELECT value FROM ItemTable WHERE key='composer.composerData'").get();
      if (r) {
        const v = JSON.parse(r.value);
        if (Array.isArray(v.allComposers)) {
          for (const c of v.allComposers) if (c && c.composerId) ids.add(c.composerId);
        }
        if (Array.isArray(v.selectedComposerIds)) for (const id of v.selectedComposerIds) if (id) ids.add(id);
        if (Array.isArray(v.lastFocusedComposerIds)) for (const id of v.lastFocusedComposerIds) if (id) ids.add(id);
      }
    } catch (e) {
      /* ignore */
    }
    try { wsDb.close(); } catch (e) { /* ignore */ }

    for (const id of ids) {
      // 在全局库回查该 composer 的标题/时间。
      let summary = { session_id: id, name: null, status: '', subtitle: '', model: '', fs_path: '', lastUpdatedAt: 0, createdAt: 0, bubble_count: 0 };
      try {
        const gr = db.prepare('SELECT value FROM cursorDiskKV WHERE key=?').get('composerData:' + id);
        if (gr) {
          const v = JSON.parse(gr.value);
          if (v && typeof v === 'object') summary = composerSummary(v, 'composerData:' + id);
        }
      } catch (e) {
        /* ignore */
      }
      sessions.push(summary);
    }
  }
  sessions.sort((a, b) => (b.lastUpdatedAt || 0) - (a.lastUpdatedAt || 0));
  return sessions;
}


// ---------- 时间格式化 ----------

function fmtTime(ts) {
  // ts 可能是 ISO 字符串（bubble.createdAt）或 epoch 毫秒（composerData.lastUpdatedAt）。
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts);
  const t = new Date(d.getTime() + CST_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())} ` +
         `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}:${pad(t.getUTCSeconds())}`;
}


// ---------- JSON 工具 ----------

function parseJson(s) {
  if (s == null) return null;
  if (typeof s !== 'string') return s;
  try {
    return JSON.parse(s);
  } catch (e) {
    return null;
  }
}

// 模仿 Python json.dumps 默认格式（分隔 ", " / ": "，键双引号），使两实现输出一致。
function pyJsonStringify(obj) {
  if (obj === null || obj === undefined) return 'null';
  if (typeof obj === 'string') return JSON.stringify(obj);
  if (typeof obj === 'number' || typeof obj === 'boolean') return String(obj);
  if (Array.isArray(obj)) {
    return '[' + obj.map(pyJsonStringify).join(', ') + ']';
  }
  if (typeof obj === 'object') {
    const keys = Object.keys(obj);
    return '{' + keys.map((k) => JSON.stringify(k) + ': ' + pyJsonStringify(obj[k])).join(', ') + '}';
  }
  return JSON.stringify(obj);
}


// ---------- 工具调用归一化 ----------

// Cursor 工具名 -> 归一化 input（仅保留关键字段，便于展示与状态重建）。
function normalizeToolInput(name, params) {
  const p = (params && typeof params === 'object') ? params : {};
  switch (name) {
    case 'run_terminal_command_v2':
      return { command: p.command || '', cwd: p.cwd || '' };
    case 'edit_file_v2':
    case 'delete_file':
      return { file: p.relativeWorkspacePath || '' };
    case 'read_file_v2':
      return { file: p.targetFile || '' };
    case 'ripgrep_raw_search':
      return { pattern: p.pattern || '', path: p.path || '' };
    case 'glob_file_search':
      return { glob: p.globPattern || '', directory: p.targetDirectory || '' };
    case 'semantic_search_full':
      return { query: p.query || '' };
    case 'web_search':
      return { query: p.searchTerm || '' };
    case 'web_fetch':
      return { url: p.url || '' };
    case 'task_v2':
      return { description: p.description || '', prompt: String(p.prompt || '').slice(0, 200) };
    case 'ask_question':
      return { title: p.title || '' };
    case 'todo_write':
      return {};
    default:
      if (name && name.startsWith('mcp-')) {
        // params.tools = [{name, parameters(JSON 字符串), serverName}]
        const t = Array.isArray(p.tools) && p.tools[0] ? p.tools[0] : {};
        return {
          server: t.serverName || '',
          tool: t.name || '',
          parameters: parseJson(t.parameters) || {},
        };
      }
      return p; // 兜底：原始 params（展示时会截断）
  }
}

// 工具结果 -> 纯文本内容（按工具类型抽取最有用的部分）。
function toolResultContent(name, result, additionalData) {
  const r = (result && typeof result === 'object') ? result : {};
  const ad = (additionalData && typeof additionalData === 'object') ? additionalData : {};
  switch (name) {
    case 'run_terminal_command_v2':
      return typeof r.output === 'string' ? r.output : pyJsonStringify(r);
    case 'read_file_v2':
      return r.contents
        ? `（文件内容，共 ${r.totalLinesInFile != null ? r.totalLinesInFile : '?'} 行）`
        : `（空文件 / 未读取，共 ${r.totalLinesInFile != null ? r.totalLinesInFile : '?'} 行）`;
    case 'ripgrep_raw_search': {
      const total = ad.totalMatches != null ? ad.totalMatches : (ad.totalFiles != null ? ad.totalFiles : null);
      const files = Array.isArray(ad.topFiles)
        ? ad.topFiles.slice(0, 10).map((f) => f && f.uri).filter(Boolean)
        : [];
      if (files.length) return `匹配 ${total != null ? total : files.length}：\n` + files.join('\n');
      return total != null ? `匹配 ${total}` : '';
    }
    case 'glob_file_search': {
      const dirs = Array.isArray(r.directories) ? r.directories : [];
      return dirs.length
        ? `命中 ${dirs.length} 项：\n` + dirs.slice(0, 10).map((d) => d.absPath || '').filter(Boolean).join('\n')
        : '';
    }
    case 'edit_file_v2':
    case 'delete_file':
      return Object.keys(r).length ? pyJsonStringify(r) : '';
    default:
      if (name && name.startsWith('mcp-') && typeof r.result === 'string') {
        const inner = parseJson(r.result);
        if (inner && Array.isArray(inner.content)) {
          return inner.content.map((c) => (c && c.text) || '').filter(Boolean).join('\n');
        }
        return r.result;
      }
      return Object.keys(r).length ? pyJsonStringify(r) : '';
  }
}

function toolIsError(tf) {
  if (tf.status && tf.status !== 'completed') return true;
  const r = parseJson(tf.result) || {};
  if (r.rejected === true) return true;
  const ad = tf.additionalData;
  if (ad && (ad.status === 'error' || ad.status === 'failed')) return true;
  return false;
}


// ---------- 会话加载与事件归一化 ----------

function loadBubbles(db, cid) {
  // 一次 LIKE 查询取出该 composer 的全部 bubble，构建 bubbleId -> value 映射。
  const map = new Map();
  const prefix = 'bubbleId:' + cid + ':';
  let rows;
  try {
    rows = db.prepare('SELECT key, value FROM cursorDiskKV WHERE key LIKE ?').all(prefix + '%');
  } catch (e) {
    return map;
  }
  for (const row of rows) {
    const bid = row.key.slice(prefix.length);
    let b;
    try {
      b = JSON.parse(row.value);
    } catch (e) {
      continue;
    }
    if (b) map.set(bid, b);
  }
  return map;
}

function normalizeSession(composer, bubbleMap) {
  // 按 fullConversationHeadersOnly 顺序拍平为归一化条目流。
  const items = [];
  const headers = Array.isArray(composer.fullConversationHeadersOnly) ? composer.fullConversationHeadersOnly : [];
  for (const h of headers) {
    const b = bubbleMap.get(h.bubbleId);
    if (!b) continue;
    const ts = b.createdAt || '';
    const cap = b.capabilityType;

    if (b.type === 1) {
      // 用户消息
      const text = String(b.text || '').trim();
      if (text) items.push({ kind: 'user_text', timestamp: ts, text });
    } else if (b.type === 2) {
      if (cap === 15 && b.toolFormerData) {
        // 工具调用 + 结果（同一 bubble 内）
        const tf = b.toolFormerData;
        const name = tf.name || 'tool';
        const params = parseJson(tf.params) || {};
        const result = parseJson(tf.result) || {};
        const callId = tf.toolCallId || '';
        items.push({
          kind: 'tool_use',
          timestamp: ts,
          name,
          input: normalizeToolInput(name, params),
          call_id: callId,
        });
        items.push({
          kind: 'tool_result',
          timestamp: ts,
          call_id: callId,
          tool_name: name,
          content: toolResultContent(name, result, tf.additionalData),
          is_error: toolIsError(tf),
        });
      } else if (cap === 30) {
        // thinking bubble：跳过（与 codex 跳过 reasoning、claude 跳过 thinking 一致）
      } else {
        // 助手文本消息
        const text = String(b.text || '').trim();
        if (text) items.push({ kind: 'assistant_text', timestamp: ts, text });
      }
    }
  }
  return items;
}


// ---------- 标题解析 ----------

function resolveTitle(name, items, sessionId) {
  // 主来源：composerData.name（Cursor 侧边栏显示的标题）；兜底首条用户消息或会话 ID。
  if (name) return name;
  for (const it of items) {
    if (it.kind === 'user_text') {
      const trimmed = it.text.trim();
      const firstLine = trimmed ? trimmed.split('\n')[0] : '';
      return firstLine.length > 60 ? firstLine.slice(0, 60) + '…' : (firstLine || sessionId);
    }
  }
  return sessionId;
}


// ---------- 工具输入摘要 ----------

function truncate(s, n) {
  s = String(s);
  return s.length <= n ? s : s.slice(0, n) + '…';
}

function toolBrief(name, inp) {
  // 单行紧凑描述一个工具调用。
  const i = inp || {};
  switch (name) {
    case 'run_terminal_command_v2':
      return `terminal(${truncate(String(i.command || '').replace(/\n/g, ' '), 100)})`;
    case 'edit_file_v2':
      return `edit_file(${truncate(i.file || '', 100)})`;
    case 'delete_file':
      return `delete_file(${truncate(i.file || '', 100)})`;
    case 'read_file_v2':
      return `read_file(${truncate(i.file || '', 100)})`;
    case 'ripgrep_raw_search':
      return `grep(${truncate(i.pattern || '', 40)} @ ${truncate(i.path || '', 60)})`;
    case 'glob_file_search':
      return `glob(${truncate(i.glob || '', 40)} @ ${truncate(i.directory || '', 60)})`;
    case 'semantic_search_full':
      return `semantic_search(${truncate(i.query || '', 80)})`;
    case 'web_search':
      return `web_search(${truncate(i.query || '', 80)})`;
    case 'web_fetch':
      return `web_fetch(${truncate(i.url || '', 80)})`;
    case 'task_v2':
      return `task(${truncate(i.description || '', 80)})`;
    case 'ask_question':
      return `ask_question(${truncate(i.title || '', 80)})`;
    case 'todo_write':
      return 'todo_write(...)';
    default:
      if (name && name.startsWith('mcp-')) {
        return `mcp:${i.tool || name}`;
      }
      for (const v of Object.values(i)) {
        if (typeof v === 'string' && v) return `${name}(${truncate(v, 80)})`;
      }
      return `${name}(...)`;
  }
}


// ---------- 任务状态重建 ----------

const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
const ERR_RE = /(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)/i;
const ERR_NEG_RE = /no error|0 failed/i;

function dedupe(seq) {
  const seen = new Set();
  const out = [];
  for (const x of seq) {
    if (x && !seen.has(x)) {
      seen.add(x);
      out.push(x);
    }
  }
  return out;
}

function buildState(items) {
  // 从归一化条目提取结构化任务状态。
  const commands = [], testResults = [], filesEdited = [], filesInvestigated = [];
  let firstUser = '', lastUser = '', lastAssistant = '';
  let lastToolName = '', lastCommand = '';

  for (const it of items) {
    const k = it.kind;
    if (k === 'user_text') {
      if (!firstUser) firstUser = it.text;
      lastUser = it.text;
    } else if (k === 'assistant_text') {
      lastAssistant = it.text;
    } else if (k === 'tool_use') {
      const name = it.name, inp = it.input || {};
      lastToolName = name;
      if (name === 'run_terminal_command_v2' && inp.command) {
        const cmd = String(inp.command).trim();
        lastCommand = cmd;
        if (cmd) commands.push(cmd);
      } else {
        lastCommand = '';
      }
      if ((name === 'edit_file_v2' || name === 'delete_file') && inp.file) filesEdited.push(inp.file);
      if (name === 'read_file_v2' && inp.file) filesInvestigated.push(inp.file);
      if (name === 'ripgrep_raw_search' && inp.path) filesInvestigated.push(inp.path);
      if (name === 'glob_file_search' && inp.directory) filesInvestigated.push(inp.directory);
    } else if (k === 'tool_result') {
      const content = it.content || '';
      const isTestCmd = lastToolName === 'run_terminal_command_v2' && TEST_CMD_RE.test(lastCommand);
      const looksTest = isTestCmd || (!!content && TEST_RESULT_RE.test(content.slice(0, 2000)));
      const head = content.slice(0, 2000);
      const isErr = ERR_RE.test(head) && !ERR_NEG_RE.test(head);
      if ((looksTest || isErr) && content.trim()) {
        testResults.push({
          command_hint: lastCommand || lastToolName,
          is_error: isErr,
          content,
        });
      }
    }
  }

  return {
    goal: firstUser,
    files_edited: dedupe(filesEdited),
    files_investigated: dedupe(filesInvestigated),
    commands: dedupe(commands),
    test_results: testResults,
    last_user: lastUser,
    last_assistant: lastAssistant,
  };
}


// ---------- Markdown 渲染 ----------

function block(text, maxChars) {
  if (!text || !text.trim()) return '';
  text = text.trim();
  if (text.length > maxChars) {
    text = text.slice(0, maxChars) + `\n…（已截断，原长 ${text.length} 字符）`;
  }
  return text;
}

function renderItem(it, maxChars) {
  const k = it.kind;
  const ts = fmtTime(it.timestamp);
  if (k === 'user_text') return [`### [用户] ${ts}`, block(it.text, maxChars)];
  if (k === 'assistant_text') return [`### [助手] ${ts}`, block(it.text, maxChars)];
  if (k === 'tool_use') {
    return [`### [工具调用] ${it.name} ${ts}`, '```', truncate(pyJsonStringify(it.input), maxChars), '```'];
  }
  if (k === 'tool_result') {
    const tag = it.is_error ? ' (错误)' : '';
    return [`### [工具结果]${tag} ${ts}`, block(it.content || '', maxChars)];
  }
  return [];
}

function renderSummary(session, state, recentN = 8, maxChars = 1500, olderLimit = 60) {
  const lines = [];
  const info = session.info;
  const norm = session.normalized;

  lines.push('# Resume-Cursor 会话接管摘要');
  lines.push('');
  lines.push('## 会话信息');
  lines.push(`- 标题: ${info.title}`);
  lines.push(`- 会话ID: ${info.session_id}`);
  lines.push(`- 项目: ${info.cwd || '(未知)'}`);
  if (info.status) lines.push(`- 状态: ${info.status}`);
  if (info.subtitle) lines.push(`- 副标题: ${truncate(info.subtitle, 200)}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`);
  lines.push(`- 消息条目数: ${norm.items.length}`);
  lines.push('');

  lines.push('## 任务状态重建');
  lines.push('');
  lines.push('### 目标');
  lines.push(block(state.goal, maxChars) || '(未识别)');
  lines.push('');
  if (state.files_investigated.length) {
    lines.push('### 已调查文件');
    for (const f of state.files_investigated) lines.push(`- ${truncate(f, 200)}`);
    lines.push('');
  }
  if (state.files_edited.length) {
    lines.push('### 代码修改');
    for (const f of state.files_edited) lines.push(`- ${truncate(f, 200)}`);
    lines.push('');
  }
  if (state.commands.length) {
    lines.push('### 执行命令');
    for (const c of state.commands) lines.push(`- \`${truncate(c, 200)}\``);
    lines.push('');
  }
  if (state.test_results.length) {
    lines.push('### 测试 / 错误结果');
    for (const tr of state.test_results.slice(-5)) {
      const tag = tr.is_error ? ' [错误]' : '';
      const trimmed = tr.content.trim();
      const firstLine = trimmed ? trimmed.split('\n')[0] : '';
      lines.push(`-${tag} ${truncate(firstLine, 200)}`);
    }
    lines.push('');
  }
  lines.push('### 最近用户消息');
  lines.push(block(state.last_user, maxChars) || '(无)');
  lines.push('');
  lines.push('### 最近助手消息');
  lines.push(block(state.last_assistant, maxChars) || '(无)');
  lines.push('');

  const items = norm.items;
  const recent = items.length > recentN ? items.slice(-recentN) : items;
  lines.push(`## 近期对话（最近 ${recent.length} 条）`);
  lines.push('');
  for (const it of recent) {
    lines.push(...renderItem(it, maxChars));
    lines.push('');
  }

  const older = items.length > recentN ? items.slice(0, -recentN) : [];
  const olderTools = older.filter((it) => it.kind === 'tool_use');
  if (olderTools.length) {
    lines.push(`## 更早活动（工具调用，仅最近 ${olderLimit} 条）`);
    for (const it of olderTools.slice(-olderLimit)) {
      lines.push(`- [${fmtTime(it.timestamp)}] ${toolBrief(it.name, it.input)}`);
    }
    lines.push('');
  }

  lines.push('## 接管建议');
  lines.push('- 先确认当前文件系统与 Git 状态与会话末尾一致（必要时重新读取相关文件）。');
  lines.push('- 以「任务状态重建」+「近期对话」为上下文，从最后一条用户消息或剩余问题处接续。');
  lines.push('- 不要逐字复述历史；基于现状决定下一步动作。');
  lines.push('');

  return lines.join('\n');
}


// ---------- 会话加载 ----------

function loadSession(db, summary) {
  const cid = summary.session_id;
  const row = db.prepare('SELECT value FROM cursorDiskKV WHERE key=?').get('composerData:' + cid);
  if (!row) {
    return { session: null, err: '未找到 composerData:' + cid };
  }
  let composer;
  try {
    composer = JSON.parse(row.value);
  } catch (e) {
    return { session: null, err: 'composerData JSON 解析失败：' + e.message };
  }
  const bubbleMap = loadBubbles(db, cid);
  const items = normalizeSession(composer, bubbleMap);
  const info = {
    session_id: cid,
    title: resolveTitle(summary.name, items, cid),
    cwd: summary.fs_path || (composer.workspaceIdentifier && composer.workspaceIdentifier.uri && composer.workspaceIdentifier.uri.fsPath) || '',
    status: summary.status,
    subtitle: summary.subtitle,
    model: summary.model,
    first_ts: fmtTime(summary.createdAt),
    last_ts: fmtTime(summary.lastUpdatedAt),
  };
  return {
    session: { info, normalized: { items, summaries: [] }, composer },
    err: null,
  };
}

function pickSession(sessions, sessionArg) {
  if (!sessionArg) return sessions[0];
  for (const s of sessions) {
    if (s.session_id.startsWith(sessionArg) || s.session_id.includes(sessionArg)) return s;
  }
  // 标题包含也允许匹配
  for (const s of sessions) {
    if (s.name && s.name.includes(sessionArg)) return s;
  }
  return null;
}


// ---------- --list 渲染 ----------

function printSessionList(sessions, projectPath, cursorDir, limit = 0) {
  const total = sessions.length;
  const shown = limit && limit > 0 ? sessions.slice(0, limit) : sessions;
  console.log(`当前项目: ${projectPath}`);
  console.log(`全局库: ${globalDbPath(cursorDir)}`);
  if (limit && limit > 0 && limit < total) {
    console.log(`找到 ${total} 个会话（仅显示最近 ${shown.length} 个）：\n`);
  } else {
    console.log(`找到 ${total} 个会话：\n`);
  }
  shown.forEach((s, i) => {
    const mark = i === 0 ? '[最近]' : '      ';
    const title = s.name || '(无标题)';
    const lastTs = fmtTime(s.lastUpdatedAt) || '(无时间)';
    const count = s.bubble_count || 0;
    console.log(`${mark} ${lastTs}  ${s.session_id.slice(0, 12)}  消息数:${String(count).padEnd(4)} 标题: ${title}`);
  });
}


// ---------- CLI ----------

function printHelp() {
  console.log(`用法: node resume_cursor.js [选项]

读取 Cursor IDE 本地 Agent/Composer 会话记录（SQLite state.vscdb），生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID 或前缀（支持跨项目全局查找，也匹配标题关键词）
  --project PATH      项目路径，默认当前工作目录
  --cursor-dir DIR    Cursor 配置目录，默认按平台推断或 \$CURSOR_HOME
  --recent N          近期对话保留条目数，默认 8
  --max-chars N       单条内容截断长度，默认 1500
  --limit N           --list 返回的会话数量上限，0 不限制（默认 0）
  --json              以 JSON 输出（机器可读）
  --output FILE       将摘要写入文件
  -h, --help          显示此帮助

注意：Node 实现使用内置 node:sqlite，需要 Node.js 22.5+。更低版本请改用
      python -X utf8 scripts/resume_cursor.py ...（参数与输出完全相同）。
`);
}

function parseArgs(argv) {
  const args = {
    list: false,
    latest: false,
    session: null,
    project: null,
    'cursor-dir': null,
    recent: 8,
    'max-chars': 1500,
    limit: 0,
    json: false,
    output: null,
    help: false,
  };
  const needValue = (flag, i) => {
    if (i + 1 >= argv.length) {
      console.error(`错误：${flag} 需要一个参数。`);
      process.exit(1);
    }
    return argv[i + 1];
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--list') args.list = true;
    else if (a === '--latest') args.latest = true;
    else if (a === '--json') args.json = true;
    else if (a === '-h' || a === '--help') args.help = true;
    else if (a === '--session') args.session = needValue(a, i++);
    else if (a.startsWith('--session=')) args.session = a.slice('--session='.length);
    else if (a === '--project') args.project = needValue(a, i++);
    else if (a.startsWith('--project=')) args.project = a.slice('--project='.length);
    else if (a === '--cursor-dir') args['cursor-dir'] = needValue(a, i++);
    else if (a.startsWith('--cursor-dir=')) args['cursor-dir'] = a.slice('--cursor-dir='.length);
    else if (a === '--recent') args.recent = parseInt(needValue(a, i++), 10);
    else if (a.startsWith('--recent=')) args.recent = parseInt(a.slice('--recent='.length), 10);
    else if (a === '--max-chars') args['max-chars'] = parseInt(needValue(a, i++), 10);
    else if (a.startsWith('--max-chars=')) args['max-chars'] = parseInt(a.slice('--max-chars='.length), 10);
    else if (a === '--limit') args.limit = parseInt(needValue(a, i++), 10);
    else if (a.startsWith('--limit=')) args.limit = parseInt(a.slice('--limit='.length), 10);
    else if (a === '--output') args.output = needValue(a, i++);
    else if (a.startsWith('--output=')) args.output = a.slice('--output='.length);
    else {
      console.error(`错误：未知参数 '${a}'。使用 --help 查看用法。`);
      process.exit(1);
    }
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  requireSqlite();

  const cursorDir = args['cursor-dir'] || getCursorDir();
  const projectPathArg = path.resolve(args.project || process.cwd());

  const gdbPath = globalDbPath(cursorDir);
  let db;
  try {
    db = openDb(gdbPath);
  } catch (e) {
    console.error(`错误：${e.message}`);
    console.error(`已查找：${gdbPath}`);
    process.exit(1);
  }

  let target = null;
  let projectPath = projectPathArg;
  let sessions = [];

  // 若指定了 session，优先跨项目全局查找。
  if (args.session) {
    sessions = scanAllSessions(db);
    target = pickSession(sessions, args.session);
    if (target && target.fs_path) projectPath = target.fs_path;
  }

  // 未指定 session 或全局未找到时，回退到基于项目路径查找。
  if (!target) {
    sessions = scanSessions(db, projectPathArg);
    if (!sessions.length) {
      // 兜底：扫描 workspaceStorage
      sessions = scanWorkspaceFallback(cursorDir, projectPathArg, db);
    }

    if (!sessions.length) {
      console.error(`错误：未找到项目 ${projectPathArg} 对应的 Cursor 会话。`);
      console.error(`已查找：${gdbPath}`);
      try { db.close(); } catch (e) { /* ignore */ }
      process.exit(1);
    }

    target = pickSession(sessions, args.session);
    if (!target) {
      console.error(`错误：未匹配到会话 '${args.session}'。使用 --list 查看可用会话。`);
      try { db.close(); } catch (e) { /* ignore */ }
      process.exit(1);
    }
  }

  if (args.list) {
    printSessionList(sessions, projectPath, cursorDir, args.limit);
    try { db.close(); } catch (e) { /* ignore */ }
    return;
  }

  const { session, err } = loadSession(db, target);
  try { db.close(); } catch (e) { /* ignore */ }
  if (err) {
    console.error(`错误：解析会话失败：${err}`);
    process.exit(1);
  }

  const state = buildState(session.normalized.items);

  let out;
  if (args.json) {
    out = JSON.stringify({
      info: session.info,
      state: {
        goal: state.goal,
        files_investigated: state.files_investigated,
        files_edited: state.files_edited,
        commands: state.commands,
        test_results: state.test_results,
        last_user: state.last_user,
        last_assistant: state.last_assistant,
      },
      summaries: session.normalized.summaries,
      recent_items: session.normalized.items.slice(-args.recent),
    }, null, 2);
  } else {
    out = renderSummary(session, state, args.recent, args['max-chars']);
  }

  if (args.output) {
    fs.writeFileSync(args.output, out, 'utf-8');
    console.error(`摘要已写入：${args.output}`);
  } else {
    process.stdout.write(out);
    if (!out.endsWith('\n')) process.stdout.write('\n');
  }
}

main();
