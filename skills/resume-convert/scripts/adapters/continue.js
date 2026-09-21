#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/continue: Continue（VS Code/JetBrains 扩展与 cn CLI 共用）-> 归一化会话。
 *
 * 格式依据（与 resume-continue 一致）：
 *   - ~/.continue/sessions/<会话>.json（sessions.json 为索引文件，扫描时跳过；
 *     目录受 CONTINUE_DATA_DIR / CONTINUE_GLOBAL_DIR 环境变量影响）
 *   - 用户/助手文本：message.content（字符串，或 text/imageUrl 片段列表）
 *   - 工具调用：toolCallStates（新，toolCall.function + output）与
 *     message.toolCalls（旧，仅调用无结果）；结果来自 state.output 或独立
 *     role:"tool" 消息（后者已给出时跳过 state.output，避免重复）
 *   - 推理/思考（thinking/system 角色）不可迁移，跳过
 *   - conversationSummary（原会话 compact）-> 转换注记
 *   - contextItems 附件折叠进对应用户消息文本（[附件] 前缀）
 * 标题：title 为空或占位（New Session 等）时回退首条用户消息首行。
 * 时间：条目本身无时间戳，会话级时间取会话文件的创建/修改时间。
 * 与 continue.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

// 占位标题（与 resume-continue 的 UNTITLED_TITLES 一致，比较时不区分大小写）
const UNTITLED_TITLES = new Set(['', 'untitled session', 'new session', '新会话', '未命名会话']);
const TOOL_STATUS_ERROR = new Set(['errored', 'error', 'failed']);

function defaultDir() {
  if (process.env.CONTINUE_DATA_DIR) return path.resolve(process.env.CONTINUE_DATA_DIR);
  const base = process.env.CONTINUE_GLOBAL_DIR || path.join(os.homedir(), '.continue');
  return path.join(path.resolve(base), 'sessions');
}

function resolveSessionsDir(configured) {
  // dir_ 兼容多种形态：sessions 目录本身、Continue 主目录（含 sessions/）、空（默认）
  if (!configured) return defaultDir();
  const resolved = path.resolve(String(configured));
  if (path.basename(resolved) === 'sessions' && isDir(resolved)) return resolved;
  const nested = path.join(resolved, 'sessions');
  return isDir(nested) ? nested : resolved;
}

function normPath(value) {
  return value ? path.resolve(String(value)).toLowerCase().replace(/\\/g, '/') : '';
}

function isFile(value) { try { return fs.statSync(value).isFile(); } catch (e) { return false; } }
function isDir(value) { try { return fs.statSync(value).isDirectory(); } catch (e) { return false; } }

function fileTimes(p) {
  const stat = fs.statSync(p);
  const created = (stat.birthtimeMs && stat.birthtimeMs > 0)
    ? Math.trunc(stat.birthtimeMs) : Math.trunc(stat.ctimeMs);
  return [created, Math.trunc(stat.mtimeMs)];
}

function msToIso(ms) {
  ms = Math.trunc(Number(ms) || 0);
  return ms ? new Date(ms).toISOString() : '';
}

function readSession(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf-8')); } catch (e) { return null; }
}

function parseJson(value) {
  // 宽松 JSON 解析：对象/数组原样返回，空值与坏串返回 {}
  if (value && typeof value === 'object') return value;
  if (value == null || (typeof value === 'string' && !value.trim())) return {};
  try { return JSON.parse(value); } catch (e) { return {}; }
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null) return value;
  }
  return null;
}

function messageContentText(content) {
  // message.content -> 纯文本：text 片段拼接，imageUrl 记为 [图片] 占位
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const part of content) {
    if (part && typeof part === 'object') {
      if (part.type === 'text') parts.push(String(part.text || ''));
      else if (part.type === 'imageUrl') parts.push('[图片]');
    } else if (typeof part === 'string') {
      parts.push(part);
    }
  }
  return parts.filter(Boolean).join('\n');
}

function attachmentLines(contextItems) {
  // contextItems -> [附件] 标签行（名称 + uri.value）
  const lines = [];
  for (const context of Array.isArray(contextItems) ? contextItems : []) {
    if (!context || typeof context !== 'object') continue;
    let label = String(context.name || '').trim();
    const uri = (context.uri && typeof context.uri === 'object') ? context.uri : {};
    const value = String(uri.value || '').trim();
    if (value && value !== label) label = label ? `${label} (${value})` : value;
    if (label) lines.push(`[附件] ${label}`);
  }
  return lines;
}

function contextItemsText(output) {
  // toolCallStates[].output（contextItems 形态）-> 纯文本
  if (output == null) return '';
  if (Array.isArray(output)) {
    const parts = [];
    for (const entry of output) {
      if (!entry || typeof entry !== 'object') parts.push(entry == null ? '' : String(entry));
      else if (entry.content) parts.push(String(entry.content));
      else if (entry.description) parts.push(String(entry.description));
    }
    return parts.filter(Boolean).join('\n');
  }
  if (typeof output === 'object') return JSON.stringify(output);
  return String(output);
}

function toolInput(value) {
  // parsedArgs / processedArgs / arguments 统一为 dict；非 dict 以 {"raw": ...} 包装
  if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  if (Array.isArray(value)) return value.length ? { raw: value } : {};
  return value ? { raw: value } : {};
}

function toolStatusIsError(status) {
  return TOOL_STATUS_ERROR.has(String(status || '').toLowerCase());
}

function firstUserText(history) {
  // 首条用户消息的首行，作为无标题会话的展示标题
  for (const entry of history) {
    if (!entry || typeof entry !== 'object') continue;
    const message = (entry.message && typeof entry.message === 'object') ? entry.message : {};
    if (message.role !== 'user') continue;
    const text = messageContentText(message.content).trim();
    if (!text) continue;
    return text.split('\n')[0].slice(0, 120);
  }
  return '';
}

function resolveTitle(meta) {
  const title = (meta.title || '').trim();
  if (!UNTITLED_TITLES.has(title.toLowerCase())) return title;
  return meta.first_user || meta.session_id;
}

function normalizeHistory(history) {
  // history 条目 -> 归一化消息列表（分支与 resume-continue 的 normalizeHistory 一致）
  const msgs = [];
  const stateOutputs = new Set(); // 已由 role:"tool" 消息给出结果，避免与 toolCallStates.output 重复
  for (const entry of history) {
    if (!entry || typeof entry !== 'object') continue;
    const message = (entry.message && typeof entry.message === 'object') ? entry.message : {};
    const role = message.role || '';
    const summary = entry.conversationSummary;
    if (typeof summary === 'string' && summary.trim()) {
      msgs.push(cs.mNote(`原会话压缩摘要：${summary.trim()}`, ''));
    }
    if (role === 'user') {
      const parts = [messageContentText(message.content).trim()];
      for (const line of attachmentLines(entry.contextItems)) parts.push(line);
      const text = parts.filter(Boolean).join('\n\n');
      if (text) msgs.push(cs.mMessage('user', text, ''));
    } else if (role === 'assistant') {
      const text = messageContentText(message.content).trim();
      if (text) msgs.push(cs.mMessage('assistant', text, ''));
      const states = Array.isArray(entry.toolCallStates) ? entry.toolCallStates : [];
      const stateIds = new Set();
      for (const state of states) {
        if (!state || typeof state !== 'object') continue;
        const call = (state.toolCall && typeof state.toolCall === 'object') ? state.toolCall : {};
        const fn = (call.function && typeof call.function === 'object') ? call.function : {};
        const name = String(fn.name || state.toolCallId || 'tool');
        const callId = String(call.id || state.toolCallId || '');
        stateIds.add(callId);
        const input = toolInput(firstDefined(state.parsedArgs, state.processedArgs, parseJson(fn.arguments)));
        msgs.push(cs.mToolUse(callId, name, input, ''));
        const status = state.status;
        if (!stateOutputs.has(callId) && ((state.output !== undefined && state.output !== null) || toolStatusIsError(status))) {
          msgs.push(cs.mToolResult(callId, contextItemsText(state.output), toolStatusIsError(status), ''));
        }
      }
      // 旧版本仅在 message.toolCalls 上记录调用（无状态对象）
      for (const call of Array.isArray(message.toolCalls) ? message.toolCalls : []) {
        if (!call || typeof call !== 'object') continue;
        const fn = (call.function && typeof call.function === 'object') ? call.function : {};
        const callId = String(call.id || '');
        if (callId && stateIds.has(callId)) continue;
        msgs.push(cs.mToolUse(callId, String(fn.name || 'tool'), toolInput(parseJson(fn.arguments)), ''));
      }
    } else if (role === 'tool') {
      const callId = String(message.toolCallId || message.tool_call_id || '');
      if (callId) stateOutputs.add(callId);
      msgs.push(cs.mToolResult(callId, messageContentText(message.content), false, ''));
    }
    // thinking / system 角色不迁移，跳过
  }
  return msgs;
}

function scanSessions(sessionsDir) {
  // 扫描 sessions 目录（跳过 sessions.json 索引），按修改时间倒序
  const sessions = [];
  const entries = [];
  let names;
  try {
    names = fs.readdirSync(sessionsDir);
  } catch (e) {
    return sessions;
  }
  for (const name of names) {
    if (!name.endsWith('.json') || name === 'sessions.json') continue;
    const full = path.join(sessionsDir, name);
    if (!isFile(full)) continue;
    try {
      entries.push({ mtime: fs.statSync(full).mtimeMs, full });
    } catch (e) { /* ignore */ }
  }
  entries.sort((a, b) => a.mtime - b.mtime);
  for (const { full } of entries) {
    const data = readSession(full);
    if (!data || typeof data !== 'object' || !Array.isArray(data.history)) continue;
    let created = 0, updated = 0;
    try { [created, updated] = fileTimes(full); } catch (e) { /* ignore */ }
    sessions.push({
      session_id: String(data.sessionId || data.session_id || path.basename(full, '.json')),
      title: String(data.title || '').trim(),
      first_user: firstUserText(data.history),
      directory: String(data.workspaceDirectory || data.workspace || '').trim(),
      chat_model_title: String(data.chatModelTitle || '').trim(),
      created, updated,
      path: full,
      _history: data.history,
    });
  }
  sessions.sort((a, b) => (b.updated || b.created) - (a.updated || a.created));
  return sessions;
}

function listSessions(opts) {
  const o = opts || {};
  const sessionsDir = resolveSessionsDir(o.dir);
  let sessions = scanSessions(sessionsDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    const msgs = normalizeHistory(s._history);
    // Continue 条目本身无时间戳，last_ts 以会话文件修改时间为准
    return {
      session_id: s.session_id,
      title: resolveTitle(s),
      cwd: s.directory,
      mtime: s.updated,
      path: s.path,
      count: msgs.length,
      last_ts: cs.fmtCst(msToIso(s.updated)),
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const sessionsDir = resolveSessionsDir(o.dir);
  let sessions = scanSessions(sessionsDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Continue 会话（dir=${sessionsDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const msgs = normalizeHistory(target._history);
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const ref = cs.makeRef('continue', target.session_id, resolveTitle(target), target.directory, {
    model: target.chat_model_title || '',
    started_at: msToIso(target.created),
    ended_at: msToIso(target.updated),
    source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
