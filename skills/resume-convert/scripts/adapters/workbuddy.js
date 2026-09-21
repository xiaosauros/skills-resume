#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/workbuddy: WorkBuddy（腾讯 AI 智能体工作站）-> 归一化会话。
 *
 * 格式依据（与 resume-workbuddy 一致）：
 *   - 布局：<workbuddy_dir>/projects/<编码项目名>/<session_id>.jsonl
 *     workbuddy_dir 默认 $WORKBUDDY_HOME 或 ~/.workbuddy（dir 参数等价 --workbuddy-dir）
 *   - 项目目录名：项目绝对路径中的 : \ / 替换为 - 并转为小写
 *   - 用户真实输入：message(role=user)，优先提取 <user_query>，剥离 system-reminder
 *   - 模型真实输出：message(role=assistant)，providerData 提供模型名
 *   - 工具调用：function_call + function_call_result（callId 配对，缺省回退 id）
 *   - 推理（reasoning）/文件快照跳过；timestamp 为毫秒级 epoch
 * 标题：ai-title 事件的 aiTitle 优先，否则取首条用户消息首行（60 字符截断）。
 * 与 workbuddy.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

function defaultDir() {
  return process.env.WORKBUDDY_HOME || path.join(os.homedir(), '.workbuddy');
}

function encodeProjectPath(p) {
  // WorkBuddy 把项目绝对路径中的 : \ / 替换为 - 并转为小写
  return p.replace(/[:\\\/]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase();
}

function normPath(p) {
  return path.normalize(p).toLowerCase();
}

function toIsoMs(ts) {
  // WorkBuddy 的 timestamp 为毫秒级 epoch，统一转成 UTC ISO（Z 结尾）
  if (ts == null || ts === '') return '';
  const s = String(ts).trim();
  if (!s) return '';
  const n = Number(s);
  if (!isNaN(n) && isFinite(n)) return new Date(n).toISOString();
  return cs.toIsoZ(ts); // 兜底：已是 ISO 字符串则直接规整
}

function peekCwd(jsonlPath, maxLines = 30) {
  // 取文件前若干行里首个 cwd 字段（与 resume-workbuddy.peekCwd 一致）
  let content;
  try {
    content = fs.readFileSync(jsonlPath, 'utf-8');
  } catch (e) {
    return null;
  }
  const lines = content.split('\n');
  for (let i = 0; i < Math.min(maxLines, lines.length); i++) {
    const line = lines[i].trim();
    if (!line) continue;
    try {
      const obj = JSON.parse(line);
      if (obj && typeof obj === 'object' && !Array.isArray(obj) && obj.cwd) return obj.cwd;
    } catch (e) { /* skip */ }
  }
  return null;
}

function parseEvents(p) {
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

function extractUserText(content) {
  let raw = '';
  if (typeof content === 'string') {
    raw = content;
  } else if (Array.isArray(content)) {
    raw = content
      .filter((b) => b && typeof b === 'object' && (b.type === 'input_text' || b.type === 'text'))
      .map((b) => b.text || '')
      .join('\n');
  }

  // 优先提取 <user_query>
  const match = raw.match(/<user_query>([\s\S]*?)<\/user_query>/);
  if (match && match[1].trim()) return match[1].trim();

  // 兜底：剥离 system-reminder 及其他环境前缀
  const stripped = raw.replace(/<system-reminder[\s\S]*?<\/system-reminder>/gi, '').trim();
  return stripped || raw.trim();
}

function extractAssistantText(content) {
  if (typeof content === 'string') return content.trim();
  if (Array.isArray(content)) {
    return content
      .filter((b) => b && typeof b === 'object' && (b.type === 'output_text' || b.type === 'text'))
      .map((b) => b.text || '')
      .filter((t) => t)
      .join('\n')
      .trim();
  }
  return '';
}

function pyJsonStringify(obj) {
  // 与 py json.dumps(ensure_ascii=False) 输出保持一致
  if (obj === null || obj === undefined) return 'null';
  if (typeof obj === 'string') return JSON.stringify(obj);
  if (typeof obj === 'number' || typeof obj === 'boolean') return String(obj);
  if (Array.isArray(obj)) return '[' + obj.map(pyJsonStringify).join(', ') + ']';
  if (typeof obj === 'object') {
    return '{' + Object.keys(obj)
      .map((k) => JSON.stringify(k) + ': ' + pyJsonStringify(obj[k])).join(', ') + '}';
  }
  return JSON.stringify(obj);
}

function extractResultContent(output) {
  if (output == null) return '';
  if (typeof output === 'string') return output;
  if (Array.isArray(output)) return pyJsonStringify(output);
  if (typeof output === 'object') {
    if (typeof output.text === 'string') return output.text;
    if (typeof output.content === 'string') return output.content;
    return pyJsonStringify(output);
  }
  return String(output);
}

function normalize(events) {
  // 事件流 -> { meta, msgs }；meta 含 cwd/model/ai_title/started_at/ended_at
  const msgs = [];
  const meta = {
    cwd: null, model: null, ai_title: null, started_at: '', ended_at: '',
  };

  for (const ev of events) {
    if (!ev || typeof ev !== 'object' || Array.isArray(ev)) continue;
    if (ev.cwd && !meta.cwd) meta.cwd = ev.cwd;

    const etype = ev.type;
    if (etype === 'ai-title') {
      if (ev.aiTitle) meta.ai_title = ev.aiTitle;
      continue;
    }
    if (etype === 'reasoning' || etype === 'file-history-snapshot') {
      // 内部推理 / 文件快照，不可迁移，跳过
      continue;
    }

    const ts = toIsoMs(ev.timestamp == null ? '' : ev.timestamp);

    if (etype === 'message') {
      const role = ev.role;
      if (role === 'user') {
        const text = extractUserText(ev.content);
        if (text) msgs.push(cs.mMessage('user', text, ts));
      } else if (role === 'assistant') {
        if (!meta.model && ev.providerData) {
          const m = ev.providerData.model;
          const reqM = ev.providerData.requestModelName;
          meta.model = (m && reqM) ? `${m} (${reqM})` : (m || reqM || null);
        }
        const text = extractAssistantText(ev.content);
        if (text) msgs.push(cs.mMessage('assistant', text, ts));
      }
      continue;
    }

    if (etype === 'function_call') {
      let inp = {};
      const raw = ev.arguments;
      if (typeof raw === 'string') {
        try { inp = JSON.parse(raw); } catch (e) { inp = { raw }; }
      } else if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        inp = raw;
      }
      msgs.push(cs.mToolUse(ev.callId || ev.id || '', ev.name || '', inp, ts));
      continue;
    }

    if (etype === 'function_call_result') {
      const isErr = ev.status === 'error' || Boolean(ev.is_error);
      const text = extractResultContent(ev.output);
      msgs.push(cs.mToolResult(ev.callId || '', text, isErr, ts));
      continue;
    }
  }

  // 时间范围：取首末条消息时间（与 resume-workbuddy 的 first_ts/last_ts 一致）
  const tss = msgs.map((m) => m.ts).filter(Boolean);
  meta.started_at = tss.length ? tss[0] : '';
  meta.ended_at = tss.length ? tss[tss.length - 1] : '';
  return { meta, msgs };
}

function resolveTitle(aiTitle, msgs, sessionId) {
  // 与 resume-workbuddy.resolveTitle 一致：aiTitle 优先，其次首条用户消息首行
  if (aiTitle) return aiTitle;
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const firstLine = m.text.trim().split('\n')[0];
      return firstLine.length > 60 ? firstLine.slice(0, 60) + '…'
        : (firstLine || sessionId);
    }
  }
  return sessionId;
}

function scanSessions(workbuddyDir) {
  // 扫描 projects/<项目目录>/*.jsonl，按 mtime 倒序
  const projectsRoot = path.join(workbuddyDir, 'projects');
  const sessions = [];
  let dirs;
  try {
    dirs = fs.readdirSync(projectsRoot);
  } catch (e) {
    return sessions;
  }
  for (const d of dirs) {
    const full = path.join(projectsRoot, d);
    let entries;
    try {
      if (!fs.statSync(full).isDirectory()) continue;
      entries = fs.readdirSync(full);
    } catch (e) {
      continue;
    }
    for (const fn of entries) {
      if (!fn.endsWith('.jsonl')) continue;
      const p = path.join(full, fn);
      let mtime;
      try { mtime = fs.statSync(p).mtimeMs; } catch (e) { continue; }
      sessions.push({
        session_id: fn.slice(0, -'.jsonl'.length),
        path: p,
        cwd: peekCwd(p) || '',
        mtime,
      });
    }
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function listSessions(opts) {
  const o = opts || {};
  const workbuddyDir = o.dir || defaultDir();
  let sessions = scanSessions(workbuddyDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => s.cwd && normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    let title = s.session_id;
    let count = 0;
    let lastTs = '';
    try {
      const { meta, msgs } = normalize(parseEvents(s.path));
      title = resolveTitle(meta.ai_title, msgs, s.session_id);
      count = msgs.length;
      lastTs = msgs.length && msgs[msgs.length - 1].ts
        ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
    } catch (e) { /* 读取失败时退回会话 ID */ }
    return {
      session_id: s.session_id, title, cwd: s.cwd, mtime: s.mtime, path: s.path,
      count, last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const workbuddyDir = o.dir || defaultDir();
  let sessions = scanSessions(workbuddyDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => s.cwd && normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 WorkBuddy 会话（dir=${workbuddyDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const { meta, msgs } = normalize(parseEvents(target.path));
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const sid = target.session_id;
  const title = resolveTitle(meta.ai_title, msgs, sid);
  const ref = cs.makeRef('workbuddy', sid, title, meta.cwd || target.cwd || '', {
    model: meta.model || '', started_at: meta.started_at || '',
    ended_at: meta.ended_at || '', source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
