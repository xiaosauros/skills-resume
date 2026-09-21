#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/pi: Pi Coding Agent（~/.pi/agent/sessions 树状 JSONL）-> 归一化会话。
 *
 * 格式依据（与 resume-pi 一致）：
 *   ~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl
 *   首行为 {type:"session"} 头（id/cwd/timestamp），其后条目经 id/parentId 构成树，
 *   当前对话 = 从最后一个叶子条目沿 parentId 回溯到根的激活路径（分支会话取主线）。
 *   - 用户/助手消息：message 条目 role=user / assistant（assistant 的 content 为块列表，
 *     text 块为文本、toolCall 块为工具调用；thinking 块推理不可跨模型迁移，跳过）
 *   - 工具结果：role=toolResult（toolCallId / content / isError）
 *   - TUI 内以 ! 直接执行的命令：role=bashExecution -> bash 工具调用 + 结果
 *   - 压缩/分支摘要：compaction / branch_summary 条目与 compactionSummary / branchSummary role
 *   - 扩展消息：custom_message 条目与 role=custom -> 转换注记（非对话内容）
 *   - session_info.name 为会话标题；model_change 提供 provider/modelId
 * dir 参数兼容 ~/.pi 这一级或 ~/.pi/agent 这一级（与 resume-pi 的 --pi-dir 一致）。
 * 与 pi.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

// 与 Python str.splitlines() 对齐的「取首行」换行集合（换行/回车/垂直与换页制表/
// 文件与组分隔符/NEL，及 U+2028、U+2029 两个行分隔符），与 resume_pi.py /
// resume_pi.js 保持一致；控制字符用 fromCharCode 生成，避免源码里出现不可见行终止符。
const LINE_BREAK_RE = new RegExp('['
  + String.fromCharCode(0x0a, 0x0d, 0x0b, 0x0c, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029)
  + ']');

function expandTilde(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

// pi 的 agent 目录：默认 ~/.pi/agent，可用 PI_CODING_AGENT_DIR 覆盖。
function defaultDir() {
  const env = process.env.PI_CODING_AGENT_DIR;
  if (env) return expandTilde(env);
  return path.join(os.homedir(), '.pi', 'agent');
}

function isDirSafe(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch (e) {
    return false;
  }
}

// 把 dir（~/.pi 这一级或 agent 这一级）归一为 sessions 根目录。
function resolveSessionsRoot(piDir) {
  const candidates = piDir
    ? [path.join(piDir, 'agent', 'sessions'), path.join(piDir, 'sessions')]
    : [path.join(defaultDir(), 'sessions')];
  for (const c of candidates) {
    if (isDirSafe(c)) return c;
  }
  return candidates[0];
}

function normPath(p) {
  return path.normalize(p).toLowerCase();
}

// 从文件名推断会话 UUID：<timestamp>_<uuid>.jsonl -> uuid。
function sessionIdFromFilename(p) {
  const base = path.basename(p).replace(/\.jsonl$/i, '');
  const idx = base.lastIndexOf('_');
  return idx >= 0 ? base.slice(idx + 1) : base;
}

// 读取会话文件前几行，取 {type:"session"} 头的 id 与 cwd。
function peekHeader(p) {
  let sid = null;
  let cwd = null;
  let content;
  try {
    content = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    return [sid, cwd];
  }
  const lines = content.split('\n');
  for (let i = 0; i < Math.min(10, lines.length); i++) {
    const line = lines[i].trim();
    if (!line) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch (e) { continue; }
    if (obj.id && !sid) sid = obj.id;
    if (obj.cwd && !cwd) cwd = obj.cwd;
    if (sid && cwd) break;
  }
  return [sid, cwd];
}

// 读会话 JSONL，返回事件列表（坏行跳过）。
function parseJsonl(p) {
  let text;
  try {
    text = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取会话文件：${e.message}`);
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

function extractText(content) {
  // content 可能是 str 或 block 列表，只取 text block。
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    const parts = [];
    for (const block of content) {
      if (block && typeof block === 'object' && block.type === 'text') {
        parts.push(block.text || '');
      }
    }
    return parts.filter((p) => p).join('\n');
  }
  return '';
}

function firstLine(s) {
  return String(s).split(LINE_BREAK_RE)[0];
}

// 沿最后一个叶子条目回溯 parentId 到根，得到当前激活的对话路径（支持分支）。
function activePath(events) {
  const byId = new Map();
  for (const ev of events) {
    if (ev && ev.id) byId.set(ev.id, ev);
  }
  let leaf = null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i] && events[i].id) { leaf = events[i]; break; }
  }
  if (!leaf) return [];
  const chain = [];
  const seen = new Set();
  let cur = leaf;
  while (cur && cur.id && !seen.has(cur.id)) {
    seen.add(cur.id);
    chain.push(cur);
    cur = cur.parentId ? byId.get(cur.parentId) || null : null;
  }
  chain.reverse();
  return chain;
}

// 扩展消息统一转成转换注记（保留 customType 便于识别来源）。
function customNote(text, customType) {
  const tag = customType ? `（${customType}）` : '';
  return `扩展消息${tag}：${text}`;
}

// 激活路径上的条目 -> { meta, msgs }。
function normalize(events) {
  const msgs = [];
  const meta = { session_id: null, title: null, cwd: null, model: null,
    started_at: '', ended_at: '' };

  // session 头没有 parentId，不在激活路径上，直接取全文件首个 {type:"session"} 条目。
  let header = null;
  for (const ev of events) {
    if (ev && ev.type === 'session') { header = ev; break; }
  }

  let entries = activePath(events);
  if (!entries.length) entries = events; // 兼容无 id/parentId 的旧格式：按文件顺序线性处理

  for (const ev of entries) {
    if (!ev || typeof ev !== 'object') continue;
    const etype = ev.type;

    if (etype === 'session') continue;
    if (etype === 'session_info') {
      if (ev.name) meta.title = ev.name;
      continue;
    }
    if (etype === 'compaction' || etype === 'branch_summary') {
      if (ev.summary) msgs.push(cs.mNote(`历史摘要：${ev.summary}`, ev.timestamp || ''));
      continue;
    }
    if (etype === 'model_change') {
      if (ev.modelId) {
        meta.model = ev.provider ? `${ev.provider}/${ev.modelId}` : ev.modelId;
      }
      continue;
    }
    if (etype === 'thinking_level_change') continue;
    if (etype !== 'message' && etype !== 'custom_message') continue; // label / custom 等忽略

    const ts = ev.timestamp || '';

    if (etype === 'custom_message') {
      const text = extractText(ev.content);
      if (text.trim()) msgs.push(cs.mNote(customNote(text, ev.customType || ''), ts));
      continue;
    }

    const msg = ev.message || {};
    const role = msg.role;

    if (role === 'user') {
      const text = extractText(msg.content);
      if (text.trim()) msgs.push(cs.mMessage('user', text, ts));
    } else if (role === 'assistant') {
      const content = Array.isArray(msg.content) ? msg.content : [];
      let textOnly = '';
      for (const block of content) {
        if (!block || typeof block !== 'object') continue;
        if (block.type === 'text') {
          if (block.text) textOnly += (textOnly ? '\n' : '') + block.text;
        } else if (block.type === 'toolCall') {
          // call_id 用源真实 id，没有传 ''
          msgs.push(cs.mToolUse(block.id || '', block.name || '',
            block.arguments || {}, ts));
        }
        // thinking block（推理）不可跨模型迁移，跳过
      }
      if (textOnly.trim()) msgs.push(cs.mMessage('assistant', textOnly, ts));
      if (msg.errorMessage) msgs.push(cs.mNote(`助手错误信息：${msg.errorMessage}`, ts));
    } else if (role === 'toolResult') {
      msgs.push(cs.mToolResult(msg.toolCallId || '', extractText(msg.content),
        !!(msg.isError || msg.is_error), ts));
    } else if (role === 'bashExecution') {
      // 用户在 TUI 里以 ! / !! 直接执行的 shell 命令
      const cmd = msg.command || '';
      msgs.push(cs.mToolUse('', 'bash', { command: cmd }, ts));
      const isErr = !!msg.cancelled || ((msg.exitCode == null ? 0 : msg.exitCode) !== 0);
      msgs.push(cs.mToolResult('', msg.output || '', isErr, ts));
    } else if (role === 'custom') {
      const text = extractText(msg.content);
      if (text.trim()) msgs.push(cs.mNote(customNote(text, msg.customType || ''), ts));
    } else if (role === 'compactionSummary' || role === 'branchSummary') {
      if (msg.summary) msgs.push(cs.mNote(`历史摘要：${msg.summary}`, ts));
    }
    // image / 其他 role 忽略
  }

  if (header) {
    meta.session_id = header.id || null;
    meta.cwd = header.cwd || null;
    meta.started_at = header.timestamp || '';
  }
  if (msgs.length) meta.ended_at = msgs[msgs.length - 1].ts || meta.started_at;
  return { meta, msgs };
}

// session_info.name > 首条用户消息摘要 > 会话 ID。
function resolveTitle(title, msgs, sid) {
  if (title) return title;
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const t = firstLine(m.text.trim());
      return t.length > 60 ? t.slice(0, 60) + '…' : (t || sid);
    }
  }
  return sid;
}

// 跨项目扫描 sessions 根目录下所有项目目录的会话，按 mtime 倒序。
function scanSessions(root) {
  const sessions = [];
  if (!isDirSafe(root)) return sessions;
  let dirs;
  try {
    dirs = fs.readdirSync(root);
  } catch (e) {
    return sessions;
  }
  for (const d of dirs) {
    const full = path.join(root, d);
    if (!isDirSafe(full)) continue;
    let files;
    try {
      files = fs.readdirSync(full);
    } catch (e) {
      continue;
    }
    for (const fn of files) {
      if (!fn.endsWith('.jsonl')) continue;
      const p = path.join(full, fn);
      let stat;
      try {
        stat = fs.statSync(p);
      } catch (e) {
        continue;
      }
      if (!stat.isFile()) continue;
      const [sid, cwd] = peekHeader(p);
      sessions.push({
        session_id: sid || sessionIdFromFilename(p),
        path: p,
        cwd: cwd || '',
        mtime: stat.mtimeMs,
      });
    }
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function listSessions(opts) {
  const o = opts || {};
  const root = resolveSessionsRoot(o.dir);
  let sessions = scanSessions(root);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => s.cwd && normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    let meta = {};
    let msgs = [];
    try {
      const r = normalize(parseJsonl(s.path));
      meta = r.meta;
      msgs = r.msgs;
    } catch (e) { /* 解析失败按空会话处理 */ }
    const sid = meta.session_id || s.session_id;
    return {
      session_id: sid,
      title: resolveTitle(meta.title, msgs, sid),
      cwd: s.cwd,
      mtime: s.mtime,
      path: s.path,
      count: msgs.length,
      last_ts: msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '',
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const root = resolveSessionsRoot(o.dir);
  let sessions = scanSessions(root);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => s.cwd && normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Pi 会话（dir=${root}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const { meta, msgs } = normalize(parseJsonl(target.path));
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const sid = meta.session_id || target.session_id;
  const title = resolveTitle(meta.title, msgs, sid);
  const ref = cs.makeRef('pi', sid, title, meta.cwd || target.cwd || '', {
    model: meta.model || '', started_at: meta.started_at || '',
    ended_at: meta.ended_at || '', source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
