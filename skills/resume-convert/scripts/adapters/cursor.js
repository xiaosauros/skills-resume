#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/cursor: Cursor Composer（state.vscdb SQLite）-> 归一化会话。
 *
 * 格式依据（与 resume-cursor 一致）：
 *   <cursor_dir>/User/globalStorage/state.vscdb 的 cursorDiskKV 表：
 *     - composerData:<composerId> -> JSON：name 标题 / createdAt、lastUpdatedAt（epoch 毫秒）/
 *       workspaceIdentifier.uri.fsPath 项目 / fullConversationHeadersOnly bubble 顺序表
 *     - bubbleId:<composerId>:<bubbleId> -> JSON：type 1=用户 2=助手；
 *       capabilityType 15=工具调用（toolFormerData：params/result 为 JSON 字符串、
 *       toolCallId 为真实调用 id），30=思考跳过；createdAt 为 ISO 串
 *   兜底定位：User/workspaceStorage/<hash>/workspace.json + 工作区库 ItemTable 的
 *   composer.composerData（allComposers/selectedComposerIds/lastFocusedComposerIds）。
 * 转换语义：工具调用+结果在同一 bubble 内拆为一对 mToolUse/mToolResult；
 * 思考（capabilityType=30）不可跨模型迁移，跳过。
 * 标题：composerData.name 优先，兜底首条用户消息或会话 ID。
 * 与 cursor.py 功能等价、输出一致。Node 实现使用内置 node:sqlite（需 Node.js 22.5+）。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

// node:sqlite（Node 22.5+ 内置）。不可用时给出明确提示，引导使用 Python 实现。
let DatabaseSync = null;
try {
  ({ DatabaseSync } = require('node:sqlite'));
} catch (e) {
  DatabaseSync = null;
}

function requireSqlite() {
  if (!DatabaseSync) {
    throw new cs.ConvertError('当前 Node 不支持 node:sqlite（需要 Node.js 22.5+），请改用 Python 适配器 cursor.py');
  }
}


// ---------- 路径与数据库 ----------

function defaultDir() {
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

function openDb(dbPath) {
  // 只读方式打开，Cursor 运行时也可安全读取；文件不存在返回 null。
  if (!isFileSafe(dbPath)) return null;
  return new DatabaseSync(dbPath, { readOnly: true });
}


// ---------- JSON / 时间工具 ----------

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

function msToIso(ms) {
  // composerData 的 epoch 毫秒 -> 毫秒精度 UTC 'Z' 串（非法 / <=0 返回 ''）。
  const n = Math.floor(Number(ms));
  if (!isFinite(n) || n <= 0) return '';
  try {
    return new Date(n).toISOString();
  } catch (e) {
    return '';
  }
}

function bubbleTs(b) {
  // bubble.createdAt：ISO 串直接用，epoch 毫秒转 ISO（兼容 resume fmtTime 的双类型）。
  const ts = b.createdAt;
  if (!ts) return '';
  if (typeof ts === 'number') return msToIso(ts);
  return String(ts);
}


// ---------- composer 摘要（标题 / 项目 / 时间来源） ----------

function modelFromComposer(v) {
  // composerData.modelConfig.modelName 通常是 "default"，无意义；仅在非 default 时返回。
  const mc = v && v.modelConfig;
  if (mc && typeof mc === 'object' && !Array.isArray(mc)) {
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
  // 读取全局 state.vscdb 的 cursorDiskKV 表中所有 composerData:* 记录（按键序稳定输出）。
  const rows = db.prepare("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY key").all();
  const out = [];
  for (const row of rows) {
    let v;
    try {
      v = JSON.parse(row.value);
    } catch (e) {
      continue;
    }
    if (!v || typeof v !== 'object' || Array.isArray(v)) continue;
    out.push(composerSummary(v, row.key));
  }
  return out;
}

function decodeFolderUri(uri) {
  // workspace.json 的 folder 字段形如 file:///d%3A/workspace/x
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

function scanWorkspaceFallback(cursorDir, projectPath, db) {
  // 兜底路径：全局库 fsPath 匹配不到时，扫描 workspaceStorage。
  // 通过 workspace.json 的 folder 字段定位项目，再从该工作区库 ItemTable 的
  // composer.composerData 取 composerId（保持插入序），回查全局库的标题/时间。
  const wsRoot = path.join(cursorDir, 'User', 'workspaceStorage');
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
    const wsDb = openDb(wsDbPath);
    if (!wsDb) continue;
    const ids = [];
    const seen = new Set();
    try {
      const r = wsDb.prepare("SELECT value FROM ItemTable WHERE key='composer.composerData'").get();
      if (r) {
        const v = JSON.parse(r.value);
        if (Array.isArray(v.allComposers)) {
          for (const c of v.allComposers) {
            if (c && c.composerId && !seen.has(c.composerId)) {
              seen.add(c.composerId);
              ids.push(c.composerId);
            }
          }
        }
        for (const k of ['selectedComposerIds', 'lastFocusedComposerIds']) {
          if (Array.isArray(v[k])) {
            for (const id of v[k]) {
              if (id && !seen.has(id)) {
                seen.add(id);
                ids.push(id);
              }
            }
          }
        }
      }
    } catch (e) {
      /* ignore */
    }
    try { wsDb.close(); } catch (e) { /* ignore */ }

    for (const id of ids) {
      // 在全局库回查该 composer 的标题/时间（可能没有记录）。
      let summary = { session_id: id, name: null, status: '', subtitle: '', model: '', fs_path: '', lastUpdatedAt: 0, createdAt: 0, bubble_count: 0 };
      try {
        const gr = db.prepare('SELECT value FROM cursorDiskKV WHERE key=?').get('composerData:' + id);
        if (gr) {
          const v = JSON.parse(gr.value);
          if (v && typeof v === 'object' && !Array.isArray(v)) summary = composerSummary(v, 'composerData:' + id);
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


// ---------- 工具调用归一化 ----------

// Cursor 工具名 -> 归一化 input（仅保留关键字段，与 resume-cursor 一致）。
function normalizeToolInput(name, params) {
  const p = (params && typeof params === 'object' && !Array.isArray(params)) ? params : {};
  if (name === 'run_terminal_command_v2') {
    return { command: p.command || '', cwd: p.cwd || '' };
  }
  if (name === 'edit_file_v2' || name === 'delete_file') {
    return { file: p.relativeWorkspacePath || '' };
  }
  if (name === 'read_file_v2') {
    return { file: p.targetFile || '' };
  }
  if (name === 'ripgrep_raw_search') {
    return { pattern: p.pattern || '', path: p.path || '' };
  }
  if (name === 'glob_file_search') {
    return { glob: p.globPattern || '', directory: p.targetDirectory || '' };
  }
  if (name === 'semantic_search_full') {
    return { query: p.query || '' };
  }
  if (name === 'web_search') {
    return { query: p.searchTerm || '' };
  }
  if (name === 'web_fetch') {
    return { url: p.url || '' };
  }
  if (name === 'task_v2') {
    return { description: p.description || '', prompt: String(p.prompt || '').slice(0, 200) };
  }
  if (name === 'ask_question') {
    return { title: p.title || '' };
  }
  if (name === 'todo_write') {
    return {};
  }
  if (name && name.startsWith('mcp-')) {
    // params.tools = [{name, parameters(JSON 字符串), serverName}]
    const t = Array.isArray(p.tools) && p.tools[0] && typeof p.tools[0] === 'object' ? p.tools[0] : {};
    return {
      server: t.serverName || '',
      tool: t.name || '',
      parameters: parseJson(t.parameters) || {},
    };
  }
  return p; // 兜底：原始 params
}

// 工具结果 -> 纯文本内容（按工具类型抽取最有用的部分）。
function toolResultContent(name, result, additionalData) {
  const r = (result && typeof result === 'object' && !Array.isArray(result)) ? result : {};
  const ad = (additionalData && typeof additionalData === 'object') ? additionalData : {};
  if (name === 'run_terminal_command_v2') {
    return typeof r.output === 'string' ? r.output : pyJsonStringify(r);
  }
  if (name === 'read_file_v2') {
    const total = r.totalLinesInFile;
    const totalS = total != null ? total : '?';
    return r.contents
      ? `（文件内容，共 ${totalS} 行）`
      : `（空文件 / 未读取，共 ${totalS} 行）`;
  }
  if (name === 'ripgrep_raw_search') {
    let total = ad.totalMatches != null ? ad.totalMatches : ad.totalFiles;
    const files = Array.isArray(ad.topFiles)
      ? ad.topFiles.slice(0, 10)
        .map((f) => (f && typeof f === 'object' && f.uri) ? f.uri : '')
        .filter(Boolean)
      : [];
    if (files.length) return `匹配 ${total != null ? total : files.length}：\n` + files.join('\n');
    return total != null ? `匹配 ${total}` : '';
  }
  if (name === 'glob_file_search') {
    const dirs = Array.isArray(r.directories) ? r.directories : [];
    if (dirs.length) {
      return `命中 ${dirs.length} 项：\n` + dirs.slice(0, 10)
        .map((d) => (d && typeof d === 'object' && d.absPath) ? d.absPath : '')
        .filter(Boolean).join('\n');
    }
    return '';
  }
  if (name === 'edit_file_v2' || name === 'delete_file') {
    return Object.keys(r).length ? pyJsonStringify(r) : '';
  }
  // 默认 / MCP
  if (name && name.startsWith('mcp-') && typeof r.result === 'string') {
    const inner = parseJson(r.result);
    if (inner && typeof inner === 'object' && Array.isArray(inner.content)) {
      return inner.content
        .map((c) => (c && typeof c === 'object' && c.text) ? c.text : '')
        .filter(Boolean).join('\n');
    }
    return r.result;
  }
  return Object.keys(r).length ? pyJsonStringify(r) : '';
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
  // 按 fullConversationHeadersOnly 顺序拍平为消息流（严格保持 bubble 顺序）。
  const msgs = [];
  const headers = Array.isArray(composer.fullConversationHeadersOnly)
    ? composer.fullConversationHeadersOnly : [];
  for (const h of headers) {
    if (!h || typeof h !== 'object') continue;
    const b = bubbleMap.get(h.bubbleId);
    if (!b) continue;
    const ts = bubbleTs(b);
    const cap = b.capabilityType;

    if (b.type === 1) {
      // 用户消息
      const text = String(b.text || '').trim();
      if (text) msgs.push(cs.mMessage('user', text, ts));
    } else if (b.type === 2) {
      const tf = b.toolFormerData;
      if (cap === 15 && tf && typeof tf === 'object') {
        // 工具调用 + 结果（同一 bubble 内拆为一对消息）
        const name = tf.name || 'tool';
        const params = parseJson(tf.params) || {};
        const result = parseJson(tf.result) || {};
        const callId = tf.toolCallId || '';
        msgs.push(cs.mToolUse(callId, name, normalizeToolInput(name, params), ts));
        msgs.push(cs.mToolResult(callId,
          toolResultContent(name, result, tf.additionalData), toolIsError(tf), ts));
      } else if (cap === 30) {
        // thinking bubble：跳过（推理不可跨模型迁移）
      } else {
        // 助手文本消息
        const text = String(b.text || '').trim();
        if (text) msgs.push(cs.mMessage('assistant', text, ts));
      }
    }
  }
  return msgs;
}

function loadSessionData(db, cid) {
  // 读取 composerData 与全部 bubble，归一化为消息流。JSON 解包失败给出中文报错。
  const row = db.prepare('SELECT value FROM cursorDiskKV WHERE key=?').get('composerData:' + cid);
  if (!row) throw new cs.ConvertError('未找到 composerData:' + cid);
  let composer;
  try {
    composer = JSON.parse(row.value);
  } catch (e) {
    throw new cs.ConvertError('composerData JSON 解析失败：' + e.message);
  }
  if (!composer || typeof composer !== 'object' || Array.isArray(composer)) {
    throw new cs.ConvertError('composerData 结构异常（非 JSON 对象）：' + cid);
  }
  const bubbleMap = loadBubbles(db, cid);
  const msgs = normalizeSession(composer, bubbleMap);
  return { composer, msgs };
}


// ---------- 标题解析 ----------

function resolveTitle(name, msgs, sessionId) {
  // 主来源：composerData.name（Cursor 侧边栏标题）；兜底首条用户消息首行或会话 ID。
  if (name) return name;
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const t = m.text.trim().split('\n')[0];
      return t.length > 60 ? t.slice(0, 60) + '…' : (t || sessionId);
    }
  }
  return sessionId;
}

function composerCwd(composer) {
  // workspaceIdentifier.uri.fsPath（可能缺失）。
  const wi = composer.workspaceIdentifier;
  const uri = wi && typeof wi === 'object' ? wi.uri : null;
  const fsPath = uri && typeof uri === 'object' ? (uri.fsPath || '') : '';
  return fsPath || '';
}


// ---------- 对外接口 ----------

function listSessions(opts) {
  requireSqlite();
  const o = opts || {};
  const cursorDir = o.dir || defaultDir();
  const dbPath = globalDbPath(cursorDir);
  const db = openDb(dbPath);
  if (!db) return [];
  try {
    let sessions;
    if (o.project) {
      const norm = normPath(o.project);
      sessions = listComposers(db).filter((c) => c.fs_path && normPath(c.fs_path) === norm);
      if (!sessions.length) sessions = scanWorkspaceFallback(cursorDir, o.project, db);
    } else {
      sessions = listComposers(db);
    }
    sessions.sort((a, b) => (b.lastUpdatedAt || 0) - (a.lastUpdatedAt || 0));
    let mtime = 0;
    try { mtime = fs.statSync(dbPath).mtimeMs; } catch (e) { /* ignore */ }
    return sessions.slice(0, 50).map((s) => {
      let title = s.name || '';
      let count = 0;
      let lastTs = '';
      let msgs = [];
      try {
        msgs = loadSessionData(db, s.session_id).msgs;
        count = msgs.length;
        lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
      } catch (e) {
        msgs = [];
      }
      if (!title) title = resolveTitle('', msgs, s.session_id);
      if (!lastTs) lastTs = cs.fmtCst(msToIso(s.lastUpdatedAt));
      return {
        session_id: s.session_id, title, cwd: s.fs_path,
        mtime, path: dbPath, count, last_ts: lastTs,
      };
    });
  } finally {
    try { db.close(); } catch (e) { /* ignore */ }
  }
}

function loadSession(opts) {
  requireSqlite();
  const o = opts || {};
  const cursorDir = o.dir || defaultDir();
  const dbPath = globalDbPath(cursorDir);
  const db = openDb(dbPath);
  if (!db) {
    throw new cs.ConvertError(`未找到 Cursor 会话（dir=${cursorDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '')
      + `）：数据库不存在 ${dbPath}`);
  }
  try {
    let sessions;
    if (o.project) {
      const norm = normPath(o.project);
      sessions = listComposers(db).filter((c) => c.fs_path && normPath(c.fs_path) === norm);
      if (!sessions.length) sessions = scanWorkspaceFallback(cursorDir, o.project, db);
    } else {
      sessions = listComposers(db);
    }
    sessions.sort((a, b) => (b.lastUpdatedAt || 0) - (a.lastUpdatedAt || 0));
    if (o.session_id) {
      sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
        || s.session_id.includes(o.session_id));
    }
    if (!sessions.length) {
      throw new cs.ConvertError(`未找到 Cursor 会话（dir=${cursorDir}`
        + (o.project ? `，project=${o.project}` : '')
        + (o.session_id ? `，session=${o.session_id}` : '') + '）');
    }

    const target = sessions[0];
    const { composer, msgs } = loadSessionData(db, target.session_id);
    if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：composerData:${target.session_id}`);

    const sid = target.session_id;
    const title = resolveTitle(target.name, msgs, sid);
    const ref = cs.makeRef('cursor', sid, title, composerCwd(composer) || target.fs_path, {
      model: modelFromComposer(composer),
      started_at: msToIso(composer.createdAt) || (msgs[0].ts || ''),
      ended_at: msToIso(composer.lastUpdatedAt) || (msgs[msgs.length - 1].ts || ''),
      source: dbPath,
    });
    return { ref, msgs };
  } finally {
    try { db.close(); } catch (e) { /* ignore */ }
  }
}

module.exports = { listSessions, loadSession };
