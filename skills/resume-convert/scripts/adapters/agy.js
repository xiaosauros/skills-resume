#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/agy: Antigravity CLI（~/.gemini/antigravity）-> 归一化会话。
 *
 * 格式依据（与 resume-agy 一致）：
 *   - 用户真实输入：source=USER_EXPLICIT 且 type=USER_INPUT；
 *     content 取 <USER_REQUEST> 内文并剔除 <ADDITIONAL_METADATA> 块
 *   - 模型真实输出：source=MODEL 的 PLANNER_RESPONSE 及其余内容事件
 *     （EPHEMERAL_MESSAGE 为内部瞬时内容，跳过）
 *   - 工具结果：type ∈ RESULT_TYPES，status=ERROR 视为出错；error 字段单独产出错误结果
 *   - 工具调用：事件 tool_calls 列表，call_id 用 step_index:序号；
 *     结果不携带 id，按源顺序与未配对调用 FIFO 配对，无配对传 ''
 *   - CHECKPOINT / CONVERSATION_HISTORY 历史摘要 -> 转换注记
 * 与 agy.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

const RESULT_TYPES = new Set([
  'LIST_DIRECTORY', 'GREP_SEARCH', 'VIEW_FILE', 'RUN_COMMAND', 'CODE_ACTION',
  'INVOKE_SUBAGENT', 'SEARCH_WEB', 'ASK_QUESTION', 'READ_URL_CONTENT',
]);
const TYPE_TOOL = {
  LIST_DIRECTORY: 'list_dir', GREP_SEARCH: 'grep_search', VIEW_FILE: 'view_file',
  RUN_COMMAND: 'run_command', CODE_ACTION: 'code_action', INVOKE_SUBAGENT: 'invoke_subagent',
  SEARCH_WEB: 'search_web', ASK_QUESTION: 'ask_question', READ_URL_CONTENT: 'read_url_content',
};
const SUMMARY_TYPES = new Set(['CHECKPOINT', 'CONVERSATION_HISTORY']);

function defaultDir() {
  return process.env.ANTIGRAVITY_HOME || path.join(os.homedir(), '.gemini', 'antigravity');
}

function normPath(p) {
  return p ? path.normalize(path.resolve(String(p))).toLowerCase() : '';
}

function isDir(p) {
  try { return fs.statSync(p).isDirectory(); } catch (e) { return false; }
}

function parseJsonl(p) {
  let text;
  try {
    text = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取 transcript 文件：${e.message}`);
  }
  const events = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try { events.push(JSON.parse(t)); } catch (e) { /* 坏行跳过 */ }
  }
  return events;
}

function valueText(value) {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function cleanVisibleText(value) {
  const text = String(value || '').trim();
  const match = text.match(/<USER_REQUEST>\s*([\s\S]*?)\s*<\/USER_REQUEST>/);
  if (match) return match[1].trim();
  return text.replace(/<ADDITIONAL_METADATA>[\s\S]*?<\/ADDITIONAL_METADATA>/g, '').trim();
}

function cleanPath(value) {
  let text = String(value || '').trim();
  if (text.length >= 2 && text.startsWith('"') && text.endsWith('"')) {
    try { const decoded = JSON.parse(text); if (typeof decoded === 'string') text = decoded; }
    catch (e) { text = text.slice(1, -1); }
  }
  return text;
}

function timestampMs(value) {
  // 秒/毫秒时间戳或 ISO 字符串 -> 毫秒；无法解析返回 0（与 resume-agy 一致）
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) {
    return Math.abs(number) < 10_000_000_000 ? Math.trunc(number * 1000) : Math.trunc(number);
  }
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function msToIso(ms) {
  if (!ms) return '';
  return new Date(ms).toISOString();
}

function isAbsoluteAny(value) {
  return path.isAbsolute(value) || /^[A-Za-z]:[\\/]/.test(value);
}

function toolPath(name, input) {
  // 按工具名挑选代表路径的入参键（与 resume-agy 一致）
  const byTool = {
    list_dir: ['DirectoryPath'], grep_search: ['SearchPath', 'Query'], view_file: ['AbsolutePath'],
    write_to_file: ['TargetFile'], replace_file_content: ['TargetFile'],
    multi_replace_file_content: ['TargetFile'],
  };
  for (const key of byTool[name] || ['TargetFile', 'AbsolutePath', 'DirectoryPath', 'SearchPath']) {
    if (input[key]) return cleanPath(input[key]);
  }
  return '';
}

function gitRoot(value) {
  // 向上找 .git 目录，找不到则返回路径本身（规范化小写，与 resume-agy 一致）
  let candidate = path.resolve(String(value));
  if (path.extname(candidate) && !isDir(candidate)) candidate = path.dirname(candidate);
  let current = candidate;
  while (true) {
    if (fs.existsSync(path.join(current, '.git'))) return normPath(current);
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return normPath(candidate);
}

function commonWindowsPath(values) {
  if (!values.length) return '';
  const parsed = values.map((value) => path.win32.normalize(value).split(/[\\/]+/));
  const first = parsed[0], common = [];
  for (let index = 0; index < first.length; index++) {
    if (parsed.every((parts) => String(parts[index] || '').toLowerCase()
      === String(first[index]).toLowerCase())) common.push(first[index]);
    else break;
  }
  return common.join('\\');
}

function inferDirectory(msgs) {
  // 从工具入参的 Cwd / 绝对路径推断项目目录（与 resume-agy 一致）
  const cwds = [], paths = [];
  for (const m of msgs) {
    if (m.kind !== 'tool_use') continue;
    const input = m.input || {}, cwd = cleanPath(input.Cwd);
    if (cwd && isAbsoluteAny(cwd)) cwds.push(path.win32.normalize(cwd));
    const candidate = toolPath(String(m.name || ''), input);
    if (candidate && isAbsoluteAny(candidate)
      && !candidate.replaceAll('/', '\\').toLowerCase().includes('.gemini\\antigravity\\brain')) {
      paths.push(path.win32.normalize(candidate));
    }
  }
  if (cwds.length) {
    const count = new Map();
    for (const cwd of cwds) count.set(cwd.toLowerCase(), (count.get(cwd.toLowerCase()) || 0) + 1);
    const winner = [...count].sort((a, b) => b[1] - a[1])[0][0];
    return gitRoot(cwds.find((cwd) => cwd.toLowerCase() === winner) || '');
  }
  const common = commonWindowsPath(paths);
  if (!common) return '';
  return gitRoot(common);
}

function titleFor(msgs, sessionId) {
  // 标题 = 第一条用户消息的首行，截取 60 字符；无用户消息用会话 ID
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user') {
      const line = String(m.text || '').trim().split(/\r?\n/)[0] || '';
      return line.length > 60 ? line.slice(0, 60) + '…' : (line || sessionId);
    }
  }
  return sessionId;
}

function normalize(events) {
  // 事件流 -> msgs；工具结果按源顺序与未配对调用 FIFO 配对
  const msgs = [];
  const pending = []; // 尚未等到结果的 tool_use call_id（源结果事件不携带 id）
  for (const ev of events) {
    if (!ev || typeof ev !== 'object') continue;
    const ts = msToIso(timestampMs(ev.created_at));
    const source = String(ev.source || ''), eventType = String(ev.type || '');
    const status = String(ev.status || '');
    const content = cleanVisibleText(valueText(ev.content));
    const error = valueText(ev.error).trim();

    if (source === 'USER_EXPLICIT' && eventType === 'USER_INPUT' && content) {
      msgs.push(cs.mMessage('user', content, ts));
    } else if (SUMMARY_TYPES.has(eventType) && content) {
      msgs.push(cs.mNote(`历史摘要（${eventType}）：\n${content}`, ts));
    } else if (source === 'MODEL' && eventType === 'PLANNER_RESPONSE' && content) {
      msgs.push(cs.mMessage('assistant', content, ts));
    } else if (RESULT_TYPES.has(eventType) && content) {
      const callId = pending.length ? pending.shift() : '';
      msgs.push(cs.mToolResult(callId, content, status === 'ERROR', ts));
    } else if (source === 'MODEL' && content && eventType !== 'EPHEMERAL_MESSAGE') {
      msgs.push(cs.mMessage('assistant', content, ts));
    }

    (Array.isArray(ev.tool_calls) ? ev.tool_calls : []).forEach((call, index) => {
      if (!call || typeof call !== 'object') return;
      const callId = `${ev.step_index || ''}:${index}`;
      msgs.push(cs.mToolUse(callId, String(call.name || 'tool'),
        call.args && typeof call.args === 'object' && !Array.isArray(call.args)
          ? call.args : {}, ts));
      pending.push(callId);
    });
    if (error) {
      const callId = pending.length ? pending.shift() : '';
      msgs.push(cs.mToolResult(callId, error, true, ts));
    }
  }
  return msgs;
}

function sessionUpdatedMs(msgs, mtimeMs) {
  // 会话最后活动时间：末条消息的毫秒时间戳，兜底文件 mtime
  for (let i = msgs.length - 1; i >= 0; i--) {
    const ms = timestampMs(msgs[i].ts);
    if (ms) return ms;
  }
  return Math.trunc(mtimeMs);
}

function scanSessions(agyDir) {
  // 扫描 brain/*/.system_generated/logs/transcript.jsonl，按最后活动倒序
  const brain = path.join(agyDir, 'brain');
  if (!isDir(brain)) return [];
  const sessions = [];
  let names = [];
  try { names = fs.readdirSync(brain); } catch (e) { return []; }
  for (const name of names) {
    const transcript = path.join(brain, name, '.system_generated', 'logs', 'transcript.jsonl');
    let mtimeMs = 0, msgs = [];
    try {
      if (!fs.statSync(transcript).isFile()) continue;
      mtimeMs = fs.statSync(transcript).mtimeMs;
      msgs = normalize(parseJsonl(transcript));
    } catch (e) { continue; }
    sessions.push({
      session_id: name, path: transcript, cwd: inferDirectory(msgs), mtime: mtimeMs,
      updated: sessionUpdatedMs(msgs, mtimeMs), title: titleFor(msgs, name),
      count: msgs.length, last_ts: msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : '',
    });
  }
  return sessions.sort((a, b) => b.updated - a.updated || b.mtime - a.mtime);
}

function listSessions(opts) {
  const o = opts || {};
  const agyDir = o.dir || defaultDir();
  let sessions = scanSessions(agyDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => s.cwd && normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => ({
    session_id: s.session_id, title: s.title, cwd: s.cwd, mtime: s.mtime,
    path: s.path, count: s.count, last_ts: s.last_ts,
  }));
}

function loadSession(opts) {
  const o = opts || {};
  const agyDir = o.dir || defaultDir();
  let sessions = scanSessions(agyDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => s.cwd && normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Antigravity 会话（dir=${agyDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const msgs = normalize(parseJsonl(target.path));
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const sid = target.session_id;
  const started = msgs.find((m) => m.ts) ? msgs.find((m) => m.ts).ts : '';
  const ended = (() => {
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].ts) return msgs[i].ts;
    return '';
  })();
  const ref = cs.makeRef('agy', sid, titleFor(msgs, sid), target.cwd, {
    started_at: started, ended_at: ended, source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
