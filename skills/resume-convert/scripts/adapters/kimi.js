#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/kimi: Kimi Code CLI（~/.kimi-code）-> 归一化会话。
 *
 * 格式依据（与 resume-kimi 一致）：
 *   - session_index.jsonl（缺失时兜底扫描 sessions/，workspaces.json 反查项目根）
 *   - <sessionDir>/state.json：title（标题主来源，"New Session" 为占位符）/
 *     createdAt / updatedAt / workDir
 *   - <sessionDir>/agents/main/wire.jsonl：turn.prompt 用户输入；
 *     content.part(text) 助手输出（分片累加合并）；tool.call/tool.result 工具配对；
 *     content.part(think) 等推理内容跳过；context.apply_compaction 压缩摘要 -> 注记
 *   - wire 事件 time 为 epoch 毫秒，state.json 时间为 ISO 字符串，统一转 UTC ISO 'Z'
 * 与 kimi.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

function defaultDir() {
  return process.env.KIMI_HOME || path.join(os.homedir(), '.kimi-code');
}

// 归一化路径用于比对（与 resume-kimi 一致）：反斜杠转正斜杠、去尾斜杠、小写
function normPath(p) {
  return String(p).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

function isFileSafe(p) {
  try { return fs.statSync(p).isFile(); } catch (e) { return false; }
}

function isDirSafe(p) {
  try { return fs.statSync(p).isDirectory(); } catch (e) { return false; }
}


// ---------- 时间：epoch 毫秒 / ISO 字符串 -> UTC ISO 'Z' ----------

function toIsoZ(ts) {
  // 解析规则与 resume_kimi.js 的 fmtTime 一致：数字/纯数字串按 epoch 毫秒，
  // 其余按 ISO 解析（无时区时 new Date 按本地时区，与 Python 端一致）
  if (ts === null || ts === undefined || ts === '' || typeof ts === 'boolean') return '';
  let d;
  if (typeof ts === 'number') {
    d = new Date(ts);
  } else if (typeof ts === 'string') {
    const s = ts.trim();
    if (!s) return '';
    const n = Number(s);
    d = isNaN(n) ? new Date(s) : new Date(n);
  } else {
    return '';
  }
  return isNaN(d.getTime()) ? '' : d.toISOString();
}

function parseMs(ts) {
  // updatedAt -> epoch 毫秒（排序用）；解析失败返回 0
  const iso = toIsoZ(ts);
  if (!iso) return 0;
  return new Date(iso).getTime();
}


// ---------- session_index / workspaces / 兜底扫描 ----------

function loadSessionIndex(kimiDir) {
  // session_index.jsonl -> [{sessionId, sessionDir, workDir}]（坏行跳过）
  const idx = path.join(kimiDir, 'session_index.jsonl');
  const out = [];
  if (!isFileSafe(idx)) return out;
  let content;
  try {
    content = fs.readFileSync(idx, 'utf-8');
  } catch (e) {
    return out;
  }
  for (const line of content.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      const o = JSON.parse(l);
      if (o.sessionId && o.sessionDir) {
        out.push({ sessionId: o.sessionId, sessionDir: o.sessionDir, workDir: o.workDir || '' });
      }
    } catch (e) { /* skip */ }
  }
  return out;
}

function loadWorkspaces(kimiDir) {
  // workspaces.json -> Map(工作区id -> 项目根)（session_index 缺失时兜底反查）
  const wj = path.join(kimiDir, 'workspaces.json');
  const map = new Map();
  if (!isFileSafe(wj)) return map;
  let obj;
  try {
    obj = JSON.parse(fs.readFileSync(wj, 'utf-8'));
  } catch (e) {
    return map;
  }
  const ws = obj && obj.workspaces;
  if (ws && typeof ws === 'object') {
    for (const id of Object.keys(ws)) {
      const root = ws[id] && ws[id].root;
      if (root) map.set(id, root);
    }
  }
  return map;
}

function loadState(sessionDir) {
  const statePath = path.join(sessionDir, 'state.json');
  if (!isFileSafe(statePath)) return null;
  try {
    return JSON.parse(fs.readFileSync(statePath, 'utf-8'));
  } catch (e) {
    return null;
  }
}

function walkSessions(kimiDir, workspaces) {
  // session_index 缺失时兜底：遍历 sessions/<工作区>/<会话>/state.json
  const root = path.join(kimiDir, 'sessions');
  const out = [];
  if (!isDirSafe(root)) return out;
  let wds;
  try {
    wds = fs.readdirSync(root, { withFileTypes: true });
  } catch (e) {
    return out;
  }
  for (const wd of wds) {
    if (!wd.isDirectory()) continue;
    const wdPath = path.join(root, wd.name);
    let sids;
    try {
      sids = fs.readdirSync(wdPath, { withFileTypes: true });
    } catch (e) {
      continue;
    }
    for (const sid of sids) {
      if (!sid.isDirectory()) continue;
      const sessionDir = path.join(wdPath, sid.name);
      if (!isFileSafe(path.join(sessionDir, 'state.json'))) continue;
      let workDir = '';
      try {
        workDir = JSON.parse(fs.readFileSync(path.join(sessionDir, 'state.json'), 'utf-8')).workDir || '';
      } catch (e) { /* ignore */ }
      if (!workDir) workDir = workspaces.get(wd.name) || '';
      out.push({ sessionId: sid.name, sessionDir, workDir });
    }
  }
  return out;
}

function wirePathOf(sessionDir) {
  return path.join(sessionDir, 'agents', 'main', 'wire.jsonl');
}


// ---------- wire.jsonl 解析 ----------

function parseWire(p) {
  let content;
  try {
    content = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取 wire.jsonl：${e.message}`);
  }
  const events = [];
  for (const line of content.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      events.push(JSON.parse(l));
    } catch (e) { /* 坏行跳过 */ }
  }
  return events;
}

function joinInputText(inp) {
  // turn.prompt.input 是 [{type,text},...]，抽取文本部分
  if (!Array.isArray(inp)) return '';
  return inp
    .map((b) => (b && typeof b === 'object' && typeof b.text === 'string') ? b.text : '')
    .filter(Boolean)
    .join('');
}

// 模仿 Python json.dumps 默认格式（分隔 ", " / ": "，键双引号），使两实现输出一致
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

function outputToText(out) {
  // tool.result.output 可能是字符串、[{type,text},...] 列表或对象
  if (out === null || out === undefined) return '';
  if (typeof out === 'string') return out;
  if (Array.isArray(out)) {
    return out
      .map((b) => (b && typeof b === 'object' && typeof b.text === 'string') ? b.text : '')
      .filter(Boolean)
      .join('\n');
  }
  if (typeof out === 'object') return pyJsonStringify(out);
  return String(out);
}

function normalizeInput(name, args, disp) {
  // 优先用 Kimi 自带的结构化 display 字段；缺失时按工具名从 args 抽取
  args = (args && typeof args === 'object' && !Array.isArray(args)) ? args : {};
  if (disp && typeof disp === 'object' && !Array.isArray(disp)) {
    const k = disp.kind;
    if (k === 'command') return { command: disp.command || '', cwd: disp.cwd || '' };
    if (k === 'file_io') return { operation: disp.operation || '', path: disp.path || '' };
    if (k === 'agent_call') return { agent_name: disp.agent_name || '', prompt: disp.prompt || '' };
    if (k === 'skill_call') return { skill: disp.skill_name || '', args: disp.args || '' };
    if (k === 'url_fetch') return { url: disp.url || '' };
  }
  const pick = (...ks) => {
    for (const k of ks) {
      const v = args[k];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return '';
  };
  switch (name) {
    case 'Bash': return { command: pick('command', 'cmd') };
    case 'Read':
    case 'ReadMediaFile': return { path: pick('path', 'file_path', 'targetFile') };
    case 'Write': return { path: pick('path', 'file_path') };
    case 'Edit': return { path: pick('path', 'file_path') };
    case 'Grep': return { pattern: pick('pattern'), path: pick('path') };
    case 'Glob': return { pattern: pick('pattern'), path: pick('path', 'targetDirectory') };
    case 'WebSearch': return { query: pick('query', 'searchTerm') };
    case 'FetchURL': return { url: pick('url') };
    case 'Skill': return { skill: pick('skill', 'name'), args: pick('args') };
    case 'Agent':
    case 'AgentSwarm': return { agent_name: pick('agent_name', 'subagent_type', 'name'), prompt: pick('prompt', 'description', 'task') };
    default: return args;
  }
}


// ---------- 事件归一化 ----------

function normalize(events) {
  // wire 事件 -> { meta, msgs }，严格保持源时间顺序
  const msgs = [];
  const meta = { model: '' };

  // 相邻 content.part(text) 累加为同一条助手消息，遇边界事件统一 flush
  let pendingText = '';
  let pendingTs = null;
  const flushText = () => {
    if (pendingText.trim()) {
      msgs.push(cs.mMessage('assistant', pendingText, toIsoZ(pendingTs)));
    }
    pendingText = '';
    pendingTs = null;
  };

  for (const o of events) {
    if (!o || typeof o !== 'object' || Array.isArray(o)) continue;
    const t = o.type;
    const ts = (o.time !== undefined && o.time !== null) ? o.time : '';

    if (t === 'config.update') {
      if (!meta.model && o.modelAlias) meta.model = o.modelAlias;
      continue;
    }
    if (t === 'turn.prompt') {
      flushText();
      const text = joinInputText(o.input).trim();
      if (text) msgs.push(cs.mMessage('user', text, toIsoZ(ts)));
      continue;
    }
    if (t === 'context.apply_compaction') {
      // 全量压缩：压缩摘要本身是接续上下文的关键信息，转注记保留
      flushText();
      const summary = String(o.summary || '').trim();
      let text = '会话发生过上下文压缩（context.apply_compaction）';
      if (summary) text += '，压缩摘要：\n' + summary;
      msgs.push(cs.mNote(text, toIsoZ(ts)));
      continue;
    }
    if (t === 'context.append_loop_event') {
      const ev = o.event;
      if (!ev || typeof ev !== 'object') continue;
      const et = ev.type;
      if (et === 'step.begin') {
        flushText();
      } else if (et === 'content.part') {
        const p = ev.part;
        if (p && typeof p === 'object' && p.type === 'text') {
          if (!pendingText) pendingTs = ts;
          pendingText += p.text || '';
        }
        // part.type=think 等推理内容不可跨模型迁移，跳过
      } else if (et === 'tool.call') {
        flushText();
        msgs.push(cs.mToolUse(ev.toolCallId || '', ev.name || '',
          normalizeInput(ev.name, ev.args, ev.display), toIsoZ(ts)));
      } else if (et === 'tool.result') {
        flushText();
        const r = (ev.result && typeof ev.result === 'object') ? ev.result : {};
        let content = outputToText(r.output);
        if (!content.trim() && r.note) content = String(r.note);
        msgs.push(cs.mToolResult(ev.toolCallId || ev.parentUuid || '',
          content, r.isError === true, toIsoZ(ts)));
      }
      // step.end / 其他 loop 事件跳过
      continue;
    }
    // metadata / full_compaction.begin|complete / llm.request / usage.record /
    // context.append_message（注入）等跳过
  }
  flushText();

  return { meta, msgs };
}


// ---------- 标题解析 ----------

// 按码点（而非 UTF-16 码元）计数与切片，使非 BMP 字符（如 emoji）下与 Python 实现一致
function cpLen(s) {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

function cpSlice(s, n) {
  let out = '';
  let count = 0;
  for (const ch of s) {
    if (count >= n) break;
    out += ch;
    count++;
  }
  return out;
}

function resolveTitle(stateTitle, msgs, sid) {
  // 主来源 state.title（Kimi 侧边栏显示的标题）；"New Session" 占位符视为无标题：
  // 首条用户消息首行（<=60 字符，超出加省略号），再兜底会话 ID
  const t = String(stateTitle || '').trim();
  if (t && t !== 'New Session') return t;
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const trimmed = m.text.trim();
      const first = trimmed.split(/\r\n|\r|\n/)[0];
      return cpLen(first) > 60 ? cpSlice(first, 60) + '…' : (first || sid);
    }
  }
  return sid;
}


// ---------- 会话扫描 ----------

function scanSessions(kimiDir) {
  // 跨项目收集全部会话，按 state.updatedAt（缺省目录 mtime）倒序
  const workspaces = loadWorkspaces(kimiDir);
  let entries = loadSessionIndex(kimiDir);
  if (!entries.length) entries = walkSessions(kimiDir, workspaces);
  const sessions = [];
  for (const e of entries) {
    const state = loadState(e.sessionDir) || {};
    const updatedAt = state.updatedAt || '';
    let sortKey = parseMs(updatedAt);
    if (!sortKey) {
      try { sortKey = fs.statSync(e.sessionDir).mtimeMs; } catch (err) { sortKey = 0; }
    }
    sessions.push({
      session_id: e.sessionId,
      session_dir: e.sessionDir,
      wire_path: wirePathOf(e.sessionDir),
      cwd: e.workDir || state.workDir || '',
      state_title: state.title || '',
      created_at: state.createdAt || '',
      updated_at: updatedAt,
      mtime: sortKey,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;

}

function listSessions(opts) {
  const o = opts || {};
  const kimiDir = o.dir || defaultDir();
  let sessions = scanSessions(kimiDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    let msgs = [];
    try {
      msgs = normalize(parseWire(s.wire_path)).msgs;
    } catch (e) { /* 轻量解析失败按空处理 */ }
    return {
      session_id: s.session_id,
      title: resolveTitle(s.state_title, msgs, s.session_id),
      cwd: s.cwd,
      mtime: s.mtime,
      path: s.wire_path,
      count: msgs.length,
      last_ts: msgs.length ? cs.fmtCst(msgs[msgs.length - 1].ts) : cs.fmtCst(toIsoZ(s.updated_at)),
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const kimiDir = o.dir || defaultDir();
  let sessions = scanSessions(kimiDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Kimi 会话（dir=${kimiDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  const { meta, msgs } = normalize(parseWire(target.wire_path));
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.wire_path}`);

  const state = loadState(target.session_dir) || {};
  const sid = target.session_id;
  const title = resolveTitle(target.state_title, msgs, sid);
  const ref = cs.makeRef('kimi', sid, title, target.cwd, {
    model: meta.model,
    started_at: toIsoZ(state.createdAt) || msgs[0].ts,
    ended_at: toIsoZ(state.updatedAt) || msgs[msgs.length - 1].ts,
    source: target.wire_path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
