#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/grok: Grok Build CLI（~/.grok/sessions/...）-> 归一化会话。
 *
 * 格式依据（与 resume-grok 一致）：
 *   - summary.json：info.id/info.cwd、generated_title|title|session_summary（标题优先级）、
 *     created_at/updated_at（会话起止，行内消息无独立时间戳）
 *   - chat_history.jsonl（主 transcript，文件行序即时间序）：system/reasoning 行与
 *     synthetic_reason 注入消息跳过（compaction_meta 压缩摘要转转换注记）；
 *     user/assistant 文本（字符串或 content block 列表）；assistant.tool_calls
 *     （兼容嵌套 function 旧形态与顶层平铺 {id,name,arguments} 当前形态）；type=tool 行
 *   - chat_history 为空/缺失时兜底 events.jsonl / updates.jsonl（ACP 流）
 * 与 grok.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

function defaultDir() {
  return process.env.GROK_HOME || path.join(os.homedir(), '.grok');
}

// 与 resume-grok 一致：反斜杠转正斜杠、去尾斜杠、小写（跨实现可互换）
function normPath(p) {
  return String(p).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

function isFileSafe(p) {
  try {
    return fs.statSync(p).isFile();
  } catch (e) {
    return false;
  }
}

// Grok 把工作目录 URL-encode 后作为会话分组目录名；兜底解码（主来源仍是 summary.info.cwd）
function decodeCwdDir(name) {
  try {
    return decodeURIComponent(name);
  } catch (e) {
    return name;
  }
}

function readJsonl(p) {
  // 读 JSONL，坏行跳过；读不到返回空数组（chat_history 缺失属正常，走兜底）
  let content;
  try {
    content = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    return [];
  }
  const rows = [];
  for (const line of content.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      rows.push(JSON.parse(t));
    } catch (e) { /* 坏行跳过 */ }
  }
  return rows;
}

function loadSummary(sessionDir) {
  const p = path.join(sessionDir, 'summary.json');
  if (!isFileSafe(p)) return {};
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf-8'));
    return data && typeof data === 'object' ? data : {};
  } catch (e) {
    return {};
  }
}

// 会话所属工作目录：info.cwd > git_root_dir > .cwd 文件 > 解码分组目录名
function summaryCwd(summary, sessionDir, dirName) {
  if (summary.info && summary.info.cwd) return summary.info.cwd;
  if (summary.git_root_dir) return summary.git_root_dir;
  const cwdFile = path.join(sessionDir, '.cwd');
  if (isFileSafe(cwdFile)) {
    try {
      const s = fs.readFileSync(cwdFile, 'utf-8').trim();
      if (s) return s;
    } catch (e) { /* ignore */ }
  }
  return decodeCwdDir(dirName);
}

function parseIsoMs(s) {
  if (!s) return 0;
  const d = new Date(s);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}


// ---------- 文本抽取与工具入参 ----------

// content 可能是字符串、[{type,text},...] 列表或 {text} 对象，统一抽为纯文本
function textOf(content) {
  if (content === null || content === undefined) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => {
        if (!b || typeof b !== 'object') return '';
        if (typeof b.text === 'string') return b.text;
        if (typeof b.content === 'string') return b.content;
        return '';
      })
      .filter(Boolean)
      .join('\n');
  }
  if (typeof content === 'object') {
    if (typeof content.text === 'string') return content.text;
    return JSON.stringify(content);
  }
  return String(content);
}

function parseJsonArgs(args) {
  // OpenAI 风格的 tool_calls[].arguments 通常是 JSON 字符串
  if (args === null || args === undefined) return {};
  if (typeof args === 'string') {
    const s = args.trim();
    if (!s) return {};
    try { return JSON.parse(s); } catch (e) { return { _raw: s }; }
  }
  return args;
}

// 按工具名从 input 抽取简洁字段（与 resume-grok 一致）
function normalizeInput(name, input) {
  input = (input && typeof input === 'object' && !Array.isArray(input)) ? input : {};
  const pick = (...ks) => {
    for (const k of ks) {
      const v = input[k];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return '';
  };
  switch (name) {
    case 'bash':
    case 'Bash': return { command: pick('command', 'cmd', 'script') };
    case 'read_file':
    case 'Read': return { path: pick('path', 'file_path', 'filePath') };
    case 'write_file':
    case 'Write': return { path: pick('path', 'file_path', 'filePath') };
    case 'search_replace':
    case 'Edit': return { path: pick('path', 'file_path', 'filePath') };
    case 'list_dir': return { path: pick('path', 'dir', 'directory') };
    case 'grep_search':
    case 'Grep': return { pattern: pick('pattern', 'regex', 'query'), path: pick('path', 'directory', 'cwd') };
    case 'glob':
    case 'Glob': return { pattern: pick('pattern'), path: pick('path', 'directory') };
    case 'web_search':
    case 'WebSearch': return { query: pick('query', 'q', 'searchTerm') };
    case 'web_fetch':
    case 'FetchURL': return { url: pick('url', 'uri') };
    case 'monitor':
    case 'Monitor': return { command: pick('command', 'cmd') };
    case 'task':
    case 'Agent':
    case 'subagent': return { agent_name: pick('agent_name', 'subagent_type', 'agent', 'name'), prompt: pick('prompt', 'description', 'task', 'directive') };
    case 'todo_write': return { description: pick('description', 'content') };
    case 'memory_search': return { query: pick('query', 'q') };
    case 'memory_get': return { path: pick('path', 'file_path') };
    case 'image_gen':
    case 'image_edit': return { prompt: pick('prompt', 'description') };
    default: return input;
  }
}


// ---------- transcript 归一化 ----------

// chat_history.jsonl -> { msgs, model }。行内无时间戳，保持行序即时间序
function normalizeChatRows(rows) {
  const msgs = [];
  for (const msg of rows) {
    if (!msg || typeof msg !== 'object') continue;
    const type = msg.type || msg.role || '';

    // system（系统提示）与 reasoning（推理不可跨模型迁移）跳过
    if (type === 'system' || type === 'reasoning') continue;
    // 注入的合成消息（技能清单 / system-reminder 等）跳过；
    // 压缩摘要（compaction_meta）转成转换注记，恢复时保留先前上下文
    if (type === 'user' && msg.synthetic_reason) {
      if (msg.synthetic_reason === 'compaction_meta') {
        const text = textOf(msg.content).trim();
        if (text) msgs.push(cs.mNote('会话发生过上下文压缩，摘要如下：\n' + text, ''));
      }
      continue;
    }

    // OpenAI 风格：assistant 携带 tool_calls 数组
    if (type === 'assistant' && Array.isArray(msg.tool_calls)) {
      if (typeof msg.content === 'string' && msg.content.trim()) {
        msgs.push(cs.mMessage('assistant', msg.content, ''));
      }
      for (let tc of msg.tool_calls) {
        tc = tc && typeof tc === 'object' ? tc : {};
        // 兼容两种形态：嵌套 function{name,arguments}（resume-grok 解析的旧格式）
        // 与当前 CLI 实测的顶层平铺 {id,name,arguments}
        const fn = tc.function || {};
        const name = fn.name || tc.name || '';
        const args = fn.arguments !== undefined ? fn.arguments : tc.arguments;
        msgs.push(cs.mToolUse(tc.id || '', name, normalizeInput(name, parseJsonArgs(args)), ''));
      }
      continue;
    }
    // OpenAI 风格：type=tool 的工具结果
    if (type === 'tool') {
      msgs.push(cs.mToolResult(msg.tool_call_id || '', textOf(msg.content), false, ''));
      continue;
    }
    // Grok 实际写入的顶层工具结果行（{type:"tool_result", tool_call_id, content}）
    if (type === 'tool_result') {
      msgs.push(cs.mToolResult(msg.tool_call_id || '', textOf(msg.content),
        msg.is_error === true, ''));
      continue;
    }

    const content = msg.content;
    // Anthropic 风格 content block 列表
    if (Array.isArray(content)) {
      for (const b of content) {
        if (!b || typeof b !== 'object') continue;
        const bt = b.type;
        if (bt === 'text') {
          const text = b.text || '';
          if (!text.trim()) continue;
          msgs.push(cs.mMessage(type === 'user' ? 'user' : 'assistant', text, ''));
        } else if (bt === 'tool_use') {
          const name = b.name || '';
          msgs.push(cs.mToolUse(b.id || '', name, normalizeInput(name, b.input || {}), ''));
        } else if (bt === 'tool_result') {
          msgs.push(cs.mToolResult(b.tool_use_id || '', textOf(b.content), b.is_error === true, ''));
        }
        // thinking / image / 其他 block 跳过
      }
      continue;
    }
    // content 为字符串
    if (typeof content === 'string' && content.trim()) {
      msgs.push(cs.mMessage(type === 'user' ? 'user' : 'assistant', content, ''));
    }
  }
  return { msgs, model: null };
}

// events.jsonl / updates.jsonl（ACP 流）-> { msgs, model }，chat_history 缺失时兜底
function normalizeAcpRows(rows) {
  const msgs = [];
  let model = null;
  for (const o of rows) {
    if (!o || typeof o !== 'object') continue;
    let u = o;
    if (o.method === 'session/update' && o.params && o.params.update) {
      u = o.params.update;
    } else if (o.update && typeof o.update === 'object') {
      u = o.update;
    }
    const kind = u.sessionUpdate || u.type || '';
    if (!kind) continue;

    if (kind === 'agent_message_chunk') {
      const text = textOf(u.content);
      if (text.trim()) msgs.push(cs.mMessage('assistant', text, ''));
    } else if (kind === 'user_message_chunk') {
      const text = textOf(u.content);
      if (text.trim()) msgs.push(cs.mMessage('user', text, ''));
    } else if (kind === 'agent_thought_chunk') {
      // 推理片段，跳过
      continue;
    } else if (kind === 'tool_call' || kind === 'tool') {
      const name = u.tool || u.name || '';
      const input = u.arguments || u.rawInput || u.input || {};
      const callId = u.id || u.callId || u.toolCallId || '';
      msgs.push(cs.mToolUse(callId, name, normalizeInput(name, input), ''));
      // 完成态携带输出时，同步产出工具结果
      const state = u.state || '';
      const out = u.rawOutput ? textOf(u.rawOutput)
        : ((state === 'completed' || state === 'failed') ? textOf(u.content) : '');
      if (out && out.trim()) {
        msgs.push(cs.mToolResult(callId, out, state === 'failed', ''));
      }
    } else if (kind === 'tool_result' || kind === 'tool_call_result') {
      msgs.push(cs.mToolResult(u.toolUseId || u.tool_use_id || u.id || '',
        textOf(u.content || u.output),
        u.isError === true || u.is_error === true, ''));
    } else if (kind === 'config' || kind === 'config.update') {
      if (!model && (u.modelId || u.model)) model = u.modelId || u.model;
    }
    // plan / error / metadata 等跳过
  }
  return { msgs, model };
}

// 读会话 transcript：chat_history.jsonl 优先，为空时兜底 events/updates（ACP）
function loadTranscript(sessionDir) {
  const chat = normalizeChatRows(readJsonl(path.join(sessionDir, 'chat_history.jsonl')));
  if (chat.msgs.length) return chat;
  for (const name of ['events.jsonl', 'updates.jsonl']) {
    const p = path.join(sessionDir, name);
    if (!isFileSafe(p)) continue;
    const acp = normalizeAcpRows(readJsonl(p));
    if (acp.msgs.length) return { msgs: acp.msgs, model: acp.model || chat.model };
  }
  return chat;
}


// ---------- 标题解析 ----------

// 主来源：summary 的 generated_title / title / session_summary；兜底首条用户消息首行
function resolveTitle(summary, msgs, sessionId) {
  for (const k of ['generated_title', 'title', 'session_summary']) {
    const v = summary && summary[k];
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  for (const m of msgs || []) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const firstLine = m.text.trim().split(/\r\n|\r|\n/)[0];
      return firstLine.length > 60 ? firstLine.slice(0, 60) + '…' : firstLine;
    }
  }
  return sessionId;
}


// ---------- 会话扫描 ----------

// 跨项目扫描 ~/.grok/sessions 下全部会话，按 summary.updated_at 倒序
function scanSessions(grokDir) {
  const root = path.join(grokDir, 'sessions');
  const sessions = [];
  let groups;
  try {
    groups = fs.readdirSync(root, { withFileTypes: true });
  } catch (e) {
    return sessions;
  }
  for (const g of groups) {
    if (!g.isDirectory()) continue;
    const groupPath = path.join(root, g.name);
    let sids;
    try {
      sids = fs.readdirSync(groupPath, { withFileTypes: true });
    } catch (e) {
      continue;
    }
    for (const sd of sids) {
      if (!sd.isDirectory()) continue;
      const sessionDir = path.join(groupPath, sd.name);
      const summary = loadSummary(sessionDir);
      let sortKey = parseIsoMs(summary.updated_at || '');
      if (!sortKey) {
        try {
          sortKey = fs.statSync(sessionDir).mtimeMs;
        } catch (err) {
          sortKey = 0;
        }
      }
      sessions.push({
        session_id: (summary.info && summary.info.id) || sd.name,
        session_dir: sessionDir,
        cwd: summaryCwd(summary, sessionDir, g.name),
        summary,
        mtime: sortKey,
      });
    }
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}


// ---------- 对外接口 ----------

function listSessions(opts) {
  const o = opts || {};
  const grokDir = o.dir || defaultDir();
  let sessions = scanSessions(grokDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    const { msgs } = loadTranscript(s.session_dir);
    return {
      session_id: s.session_id,
      title: resolveTitle(s.summary, msgs, s.session_id),
      cwd: s.cwd,
      mtime: s.mtime,
      path: s.session_dir,
      count: msgs.length,
      last_ts: cs.fmtCst(s.summary.updated_at || ''),
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const grokDir = o.dir || defaultDir();
  let sessions = scanSessions(grokDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Grok 会话（dir=${grokDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const { msgs, model } = loadTranscript(target.session_dir);
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.session_dir}`);

  const summary = target.summary;
  const sid = target.session_id;
  const title = resolveTitle(summary, msgs, sid);
  const ref = cs.makeRef('grok', sid, title, target.cwd || '', {
    model: model || summary.current_model_id || '',
    started_at: summary.created_at || '',
    ended_at: summary.updated_at || '',
    source: target.session_dir,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
