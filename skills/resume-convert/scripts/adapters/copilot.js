#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/copilot: GitHub Copilot CLI（~/.copilot/session-state）-> 归一化会话。
 *
 * 格式依据（与 resume-copilot 一致）：
 *   - 用户真实输入：user.message 的 content（transformedContent 含注入上下文，不用）
 *   - 模型真实输出：assistant.message 的 content + toolRequests（工具调用）
 *   - 工具结果：tool.execution_complete 为权威结果；permission.completed 仅当该调用
 *     没有执行结果时作兜底（真实数据里两者同现，都记会造成同 call_id 双结果，无法配对）
 *   - 推理（reasoningText / reasoningOpaque）不可跨模型迁移，跳过
 *   - session.compaction_complete -> 转换注记（携带压缩摘要）
 * 标题：workspace.yaml 的 name 优先，其次首条用户消息首行。
 * 与 copilot.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

const SID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

function defaultDir() {
  return process.env.COPILOT_HOME || path.join(os.homedir(), '.copilot');
}

function normPath(p) {
  return path.normalize(p).toLowerCase();
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

function listSessionDirs(copilotDir) {
  const root = path.join(copilotDir, 'session-state');
  const out = [];
  if (!isDirSafe(root)) return out;
  for (const name of fs.readdirSync(root)) {
    const full = path.join(root, name);
    if (!isDirSafe(full)) continue;
    const m = name.match(SID_RE);
    if (!m) continue;
    out.push({ session_id: m[0], dir: full });
  }
  return out;
}

function parseWorkspaceYaml(text) {
  // 字段少、无嵌套，简单按行解析即可，不引入 yaml 依赖。
  const obj = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const idx = line.indexOf(':');
    if (idx === -1) continue;
    const key = line.slice(0, idx).trim();
    let val = line.slice(idx + 1).trim();
    if (val === 'true') val = true;
    else if (val === 'false') val = false;
    else if (/^\d+$/.test(val)) val = parseInt(val, 10);
    obj[key] = val;
  }
  return obj;
}

function loadWorkspace(sessionDir) {
  const p = path.join(sessionDir, 'workspace.yaml');
  if (!isFileSafe(p)) return null;
  try {
    return parseWorkspaceYaml(fs.readFileSync(p, 'utf-8'));
  } catch (e) {
    return null;
  }
}

function getMtime(sessionDir) {
  // 优先用 events.jsonl 的 mtime，回退 workspace.yaml，再回退目录本身。
  const candidates = [
    path.join(sessionDir, 'events.jsonl'),
    path.join(sessionDir, 'workspace.yaml'),
    sessionDir,
  ];
  for (const p of candidates) {
    try {
      return fs.statSync(p).mtimeMs;
    } catch (e) { /* ignore */ }
  }
  return 0;
}

function scanSessions(copilotDir) {
  // 扫描全部会话目录（workspace.yaml 缺失也保留，cwd 记空串），按 mtime 倒序。
  const sessions = [];
  for (const s of listSessionDirs(copilotDir)) {
    const ws = loadWorkspace(s.dir);
    sessions.push({
      session_id: s.session_id,
      dir: s.dir,
      path: path.join(s.dir, 'events.jsonl'),
      cwd: (ws || {}).cwd || '',
      mtime: getMtime(s.dir),
      workspace: ws,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function parseEvents(jsonlPath) {
  let text;
  try {
    text = fs.readFileSync(jsonlPath, 'utf-8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取 events.jsonl：${e.message}`);
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
  // 工具结果统一抽为纯文本；图片块不可跨工具迁移，转占位符说明。
  if (output == null) return '';
  if (typeof output === 'string') return output;
  if (typeof output === 'object') {
    if (Array.isArray(output)) {
      return output.map(outputToText).filter(Boolean).join('\n');
    }
    if (String(output.type || '') === 'image') return '[图片已省略]';
    try {
      return JSON.stringify(output, null, 2);
    } catch (e) {
      return String(output);
    }
  }
  return String(output);
}

function firstUserTitle(msgs, fallback) {
  // 首条用户消息首行作为标题（与 resume-copilot 的截断规则一致）。
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const first = m.text.trim().split('\n')[0];
      return first.length <= 60 ? first : first.slice(0, 60) + '…';
    }
  }
  return fallback;
}

function normalize(events) {
  // events.jsonl 事件 -> { meta, msgs }。msgs 严格保持源事件时间顺序。
  const msgs = [];
  const meta = { session_id: null, cwd: null, model: null, started_at: '', ended_at: '' };

  // 第一遍：收集 assistant.message 已声明的工具调用 id，用于判断 permission
  // 流程是否为某个 shell 调用的唯一记录（旧格式兜底）。
  const requestCallIds = new Set();
  for (const ev of events) {
    if (ev.type === 'assistant.message') {
      for (const tr of (ev.data || {}).toolRequests || []) {
        if (tr && typeof tr === 'object' && tr.toolCallId) requestCallIds.add(tr.toolCallId);
      }
    }
  }

  const pendingPermResults = new Map(); // permission.completed 兜底结果：call_id -> {content, ts}
  const openCalls = new Map();          // 原始 call_id -> 该调用待配对的派生 id 队列（FIFO）
  const usedCallIds = new Set();        // 已分配出去的 call_id（含派生 id）
  const emittedResultIds = new Set();   // 已产出结果的原始 call_id（重复结果事件去重）

  // Copilot 在上下文压缩后会重置工具调用计数器，同一 call_id 会被
  // 完全不同的调用复用（真实数据实测）；这里给复用的 id 派生唯一后缀，
  // 保证每个调用与结果能一一配对。
  function emitToolUse(base, name, input, ts) {
    let cid = base || '';
    if (cid) {
      if (usedCallIds.has(cid)) {
        let n = 2;
        while (usedCallIds.has(`${cid}-r${n}`)) n++;
        cid = `${cid}-r${n}`;
      }
      usedCallIds.add(cid);
      if (!openCalls.has(base)) openCalls.set(base, []);
      openCalls.get(base).push(cid);
    }
    msgs.push(cs.mToolUse(cid, name, input, ts));
  }

  // 结果按 FIFO 配对到最早的未决调用；无未决调用时交由共享库作孤立结果降级。
  function emitToolResult(base, content, isError, ts) {
    const queue = base ? openCalls.get(base) : null;
    if ((!queue || !queue.length) && base && emittedResultIds.has(base)) {
      return; // 同一调用的重复结果事件，跳过
    }
    const cid = queue && queue.length ? queue.shift() : (base || '');
    if (base) emittedResultIds.add(base);
    msgs.push(cs.mToolResult(cid, content, isError, ts));
  }

  for (const ev of events) {
    const etype = ev.type;
    let data = ev.data || {};
    if (typeof data !== 'object' || Array.isArray(data)) data = {};
    const ts = ev.timestamp || '';
    if (ts) meta.ended_at = ts;

    if (etype === 'session.start') {
      if (!meta.session_id && data.sessionId) meta.session_id = data.sessionId;
      const ctx = data.context || {};
      if (!meta.cwd && ctx.cwd) meta.cwd = ctx.cwd;
      if (ts && !meta.started_at) meta.started_at = ts;
      continue;
    }

    if (etype === 'session.model_change') {
      if (data.newModel) meta.model = data.newModel;
      else if (data.previousModel && !meta.model) meta.model = data.previousModel;
      continue;
    }

    if (etype === 'session.info' || etype === 'session.error') {
      const message = String(data.message || '').trim();
      if (message) {
        const prefix = etype === 'session.error' ? '[会话错误] ' : '';
        msgs.push(cs.mNote(prefix + message, ts));
      }
      continue;
    }

    if (etype === 'session.compaction_complete') {
      const summary = String(data.summaryContent || '').trim();
      if (summary) {
        msgs.push(cs.mNote('会话发生过上下文压缩，摘要如下：\n' + summary, ts));
      } else {
        msgs.push(cs.mNote('会话发生过上下文压缩', ts));
      }
      continue;
    }

    if (etype === 'user.message') {
      const text = String(data.content || '').trim();
      if (text) msgs.push(cs.mMessage('user', text, ts));
      continue;
    }

    if (etype === 'assistant.message') {
      const content = String(data.content || '').trim();
      if (content) msgs.push(cs.mMessage('assistant', content, ts));
      if (data.model) meta.model = data.model;
      for (const tr of data.toolRequests || []) {
        if (!tr || typeof tr !== 'object') continue;
        emitToolUse(tr.toolCallId || '', tr.name || '', tr.arguments || {}, ts);
      }
      continue;
    }

    if (etype === 'tool.execution_complete') {
      emitToolResult(data.toolCallId || '',
        outputToText(data.result), data.success === false, ts);
      continue;
    }

    if (etype === 'permission.requested') {
      const pr = data.permissionRequest || {};
      const cid = pr.toolCallId || data.requestId || '';
      // 仅当该 shell 调用未出现在 assistant.message.toolRequests 时补一条
      // bash 调用（旧格式兜底；新格式里它与 toolRequests 重复，跳过）。
      if (pr.kind === 'shell' && pr.fullCommandText
        && cid && !requestCallIds.has(cid)) {
        emitToolUse(cid, 'bash', { command: pr.fullCommandText }, ts);
      }
      continue;
    }

    if (etype === 'permission.completed') {
      const cid = data.toolCallId || data.requestId || '';
      if (cid && !pendingPermResults.has(cid)) {
        pendingPermResults.set(cid, { content: outputToText(data.result), ts });
      }
      continue;
    }

    // tool.execution_start 与 toolRequests 重复；assistant.turn_* / system.message /
    // session.shutdown / session.usage_checkpoint / session.task_complete /
    // session.mode_changed / session.compaction_start 为遥测或非对话内容，跳过。
  }

  // permission.completed 兜底结果：仅当该调用没有任何执行结果时补上，
  // 保持时间顺序语义（真实数据里它通常与 tool.execution_complete 同现，跳过）。
  const merged = [];
  const consumed = new Set();
  for (const m of msgs) {
    merged.push(m);
    if (m.kind === 'tool_use' && m.call_id && !consumed.has(m.call_id)) {
      if (pendingPermResults.has(m.call_id) && !emittedResultIds.has(m.call_id)) {
        const pend = pendingPermResults.get(m.call_id);
        pendingPermResults.delete(m.call_id);
        consumed.add(m.call_id);
        merged.push(cs.mToolResult(m.call_id, pend.content, false, pend.ts));
      }
    }
  }
  // 剩余兜底结果无可配对的调用（或已被执行结果覆盖），丢弃。

  if (merged.length) meta.ended_at = merged[merged.length - 1].ts || meta.ended_at;
  return { meta, msgs: merged };
}

function listSessions(opts) {
  const o = opts || {};
  const copilotDir = o.dir || defaultDir();
  let sessions = scanSessions(copilotDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    const sid = s.session_id;
    const ws = s.workspace || {};
    let title = ws.name || '';
    let count = 0;
    let lastTs = '';
    try {
      const { msgs } = normalize(parseEvents(s.path));
      count = msgs.length;
      lastTs = msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '';
      if (!title) title = firstUserTitle(msgs, '');
    } catch (e) { /* events 读取失败：保留 sid 兜底标题 */ }
    if (!title) title = sid;
    return {
      session_id: sid, title, cwd: s.cwd, mtime: s.mtime, path: s.path,
      count, last_ts: lastTs,
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const copilotDir = o.dir || defaultDir();
  let sessions = scanSessions(copilotDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Copilot CLI 会话（dir=${copilotDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const { meta, msgs } = normalize(parseEvents(target.path));
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const ws = target.workspace || {};
  const sid = ws.id || meta.session_id || target.session_id;
  const title = ws.name || firstUserTitle(msgs, '') || sid;
  const cwd = ws.cwd || meta.cwd || target.cwd || '';

  const ref = cs.makeRef('copilot', sid, title, cwd, {
    model: meta.model || '', started_at: meta.started_at || '',
    ended_at: meta.ended_at || '', source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
