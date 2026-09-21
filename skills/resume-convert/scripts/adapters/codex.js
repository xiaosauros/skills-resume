#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/codex: Codex CLI（~/.codex/sessions rollout JSONL）-> 归一化会话。
 *
 * 格式依据（与 resume-codex 一致）：
 *   - 用户真实输入：event_msg/user_message（response_item 的 role=user 多为环境注入，跳过）
 *   - 模型真实输出：response_item/message(role=assistant)
 *   - 工具调用：function_call(+output)、custom_tool_call(+output)
 *   - 推理（reasoning）不可跨模型迁移，跳过；compacted -> 转换注记
 * 标题：~/.codex/session_index.jsonl 的 thread_name 优先。
 * 与 codex.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

const SID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

function defaultDir() {
  return process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
}

function normPath(p) {
  return path.normalize(p).toLowerCase();
}

function sidFromFilename(fn) {
  const m = path.basename(fn).match(SID_RE);
  if (m) return m[0];
  return path.basename(fn).replace(/^rollout-/, '').replace(/\.jsonl$/i, '');
}

function peekCwd(p) {
  // session_meta 首行可能极长，用正则取首个 cwd 字段
  let fd;
  try {
    fd = fs.openSync(p, 'r');
    const buf = Buffer.alloc(32768);
    const n = fs.readSync(fd, buf, 0, 32768, 0);
    const m = buf.slice(0, n).toString('utf-8').match(/"cwd"\s*:\s*"([^"]+)"/);
    return m ? m[1] : null;
  } catch (e) {
    return null;
  } finally {
    if (fd) { try { fs.closeSync(fd); } catch (e) { /* ignore */ } }
  }
}

function walkJsonl(dir) {
  const out = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return out;
  }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walkJsonl(full));
    else if (ent.isFile() && ent.name.endsWith('.jsonl')) out.push(full);
  }
  return out;
}

function loadSessionIndex(codexDir) {
  const idx = path.join(codexDir, 'session_index.jsonl');
  const mapping = {};
  let text;
  try {
    text = fs.readFileSync(idx, 'utf-8');
  } catch (e) {
    return mapping;
  }
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      const o = JSON.parse(t);
      if (o.id && o.thread_name) mapping[o.id] = o.thread_name;
    } catch (e) { /* 坏行跳过 */ }
  }
  return mapping;
}

function parseRollout(p) {
  let text;
  try {
    text = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取 rollout 文件：${e.message}`);
  }
  const events = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      events.push(JSON.parse(t));
    } catch (e) { /* 坏行跳过 */ }
  }
  return events;
}

function outputToText(output) {
  if (output == null) return '';
  if (typeof output === 'string') return output;
  if (Array.isArray(output)) {
    return output.filter((b) => b && typeof b === 'object' && b.text)
      .map((b) => b.text).join('\n');
  }
  if (typeof output === 'object') return JSON.stringify(output);
  return String(output);
}

function extractExecCmd(inputStr) {
  if (typeof inputStr !== 'string') return null;
  const m = inputStr.match(/cmd\s*:\s*(['"`])((?:\\.|(?!\1)[\s\S])*?)\1/);
  if (!m) return null;
  return m[2]
    .replace(/\\"/g, '"').replace(/\\'/g, "'")
    .replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\\\/g, '\\');
}

function normalize(events) {
  const msgs = [];
  const meta = { session_id: null, cwd: null, model: null, started_at: '', ended_at: '' };
  let hasUserFromEvent = false;

  for (const ev of events) {
    if (ev.type === 'event_msg' && ev.payload && ev.payload.type === 'user_message') {
      hasUserFromEvent = true;
      break;
    }
  }

  for (const ev of events) {
    const top = ev.type;
    const p = ev.payload;
    if (!p || typeof p !== 'object') continue;
    const pt = p.type;
    const ts = ev.timestamp || '';
    if (ts) meta.ended_at = ts;

    if (top === 'session_meta') {
      if (!meta.session_id && p.id) meta.session_id = p.id;
      if (!meta.cwd && p.cwd) meta.cwd = p.cwd;
      if (ts && !meta.started_at) meta.started_at = ts;
      continue;
    }
    if (top === 'turn_context') {
      if (!meta.cwd && p.cwd) meta.cwd = p.cwd;
      if (!meta.model && p.model) meta.model = p.model;
      continue;
    }
    if (top === 'event_msg') {
      if (pt === 'user_message' && hasUserFromEvent) {
        const text = (p.message || '').trim();
        if (text) msgs.push(cs.mMessage('user', text, ts));
      } else if (pt === 'context_compacted') {
        msgs.push(cs.mNote('会话发生过上下文压缩（context_compacted）', ts));
      }
      continue;
    }
    if (top === 'compacted') {
      msgs.push(cs.mNote('会话发生过上下文压缩（compacted）', ts));
      continue;
    }
    if (top === 'response_item') {
      if (pt === 'message') {
        if (p.role === 'assistant') {
          const text = outputToText(p.content).trim();
          if (text) msgs.push(cs.mMessage('assistant', text, ts));
        } else if (p.role === 'user' && !hasUserFromEvent) {
          // 旧版 codex 无 event_msg/user_message 时，从 response_item 取用户消息；
          // 跳过 '<' 开头的环境注入内容
          const text = outputToText(p.content).trim();
          if (text && !text.startsWith('<')) msgs.push(cs.mMessage('user', text, ts));
        }
      } else if (pt === 'function_call') {
        let inputObj = {};
        const args = p.arguments;
        if (typeof args === 'string' && args) {
          try { inputObj = JSON.parse(args); } catch (e) { inputObj = { raw: args }; }
        } else if (args && typeof args === 'object' && !Array.isArray(args)) {
          inputObj = args;
        }
        msgs.push(cs.mToolUse(p.call_id || '', p.name || 'function', inputObj, ts));
      } else if (pt === 'custom_tool_call') {
        const cmd = extractExecCmd(p.input);
        msgs.push(cs.mToolUse(p.call_id || '', p.name || 'exec',
          cmd ? { cmd } : { raw: p.input }, ts));
      } else if (pt === 'function_call_output' || pt === 'custom_tool_call_output') {
        msgs.push(cs.mToolResult(p.call_id || '', outputToText(p.output), false, ts));
      } else if (pt === 'web_search_call') {
        msgs.push(cs.mToolUse('', 'web_search', { query: '' }, ts));
      }
      continue;
    }
  }

  if (msgs.length) meta.ended_at = msgs[msgs.length - 1].ts || meta.ended_at;
  return { meta, msgs };
}

function scanSessions(codexDir) {
  const root = path.join(codexDir, 'sessions');
  const sessions = [];
  for (const p of walkJsonl(root)) {
    let mtime = 0;
    try { mtime = fs.statSync(p).mtimeMs; } catch (e) { continue; }
    sessions.push({ session_id: sidFromFilename(p), path: p, cwd: peekCwd(p) || '', mtime });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
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
  const codexDir = o.dir || defaultDir();
  let sessions = scanSessions(codexDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  const index = loadSessionIndex(codexDir);
  return sessions.slice(0, 50).map((s) => {
    let title = index[s.session_id] || '';
    let count = 0;
    let lastTs = '';
    try {
      const msgs = normalize(parseRollout(s.path)).msgs;
      count = msgs.length;
      lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
      if (!title) title = firstUserTitle(msgs, s.session_id);
    } catch (e) {
      if (!title) title = s.session_id;
    }
    return {
      session_id: s.session_id, title, cwd: s.cwd, mtime: s.mtime, path: s.path,
      count, last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const codexDir = o.dir || defaultDir();
  let sessions = scanSessions(codexDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Codex 会话（dir=${codexDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const { meta, msgs } = normalize(parseRollout(target.path));
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const sid = meta.session_id || target.session_id;
  const index = loadSessionIndex(codexDir);
  const title = index[sid] || firstUserTitle(msgs, sid);

  const ref = cs.makeRef('codex', sid, title, meta.cwd || target.cwd || '', {
    model: meta.model || '', started_at: meta.started_at || '',
    ended_at: meta.ended_at || '', source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
