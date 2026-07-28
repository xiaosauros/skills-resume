#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-kimi: 读取 Kimi Code CLI 本地会话记录（~/.kimi-code 下的
 * session_index.jsonl + sessions/<workspace>/<session>/state.json +
 * agents/main/wire.jsonl），生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（Claude Code / Codex / Grok 等）均可直接调用：
 *   node resume_kimi.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_kimi.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude/codex/cursor 一致）
const CST_OFFSET_MS = 8 * 3600 * 1000;


// ---------- 路径与项目定位 ----------

function getKimiDir() {
  return process.env.KIMI_HOME || path.join(os.homedir(), '.kimi-code');
}

// 归一化路径用于比对：反斜杠转正斜杠、去尾斜杠、小写。
// Node 与 Python 实现完全一致，保证跨实现可互换。
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

function isDirSafe(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch (e) {
    return false;
  }
}


// ---------- session_index（会话列表与 workDir 主来源） ----------

function loadSessionIndex(kimiDir) {
  // ~/.kimi-code/session_index.jsonl 每行 {"sessionId","sessionDir","workDir"}。
  // workDir 是会话所属项目路径，用于按 --project 匹配。
  const idxPath = path.join(kimiDir, 'session_index.jsonl');
  const out = [];
  if (!isFileSafe(idxPath)) return out;
  let content;
  try {
    content = fs.readFileSync(idxPath, 'utf-8');
  } catch (e) {
    return out;
  }
  for (const line of content.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      const o = JSON.parse(l);
      if (o.sessionId && o.sessionDir) {
        out.push({
          sessionId: o.sessionId,
          sessionDir: o.sessionDir,
          workDir: o.workDir || '',
        });
      }
    } catch (e) { /* skip */ }
  }
  return out;
}


// ---------- workspaces.json（workDir 兜底来源） ----------

function loadWorkspaces(kimiDir) {
  // ~/.kimi-code/workspaces.json: {workspaces: {wd_<name>_<hash>: {root,...}}}
  // 当 session_index 缺失时，用工作区 id（sessionDir 的父目录名）反查项目根。
  const wjPath = path.join(kimiDir, 'workspaces.json');
  const map = new Map();
  if (!isFileSafe(wjPath)) return map;
  let obj;
  try {
    obj = JSON.parse(fs.readFileSync(wjPath, 'utf-8'));
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


// ---------- 兜底：直接扫描 sessions 目录 ----------

function walkSessions(kimiDir, workspaces) {
  // session_index 缺失时兜底：遍历 sessions/<wd_id>/<session_id>/state.json。
  const sessionsRoot = path.join(kimiDir, 'sessions');
  const out = [];
  if (!isDirSafe(sessionsRoot)) return out;
  let wds;
  try {
    wds = fs.readdirSync(sessionsRoot, { withFileTypes: true });
  } catch (e) {
    return out;
  }
  for (const wd of wds) {
    if (!wd.isDirectory()) continue;
    const wdPath = path.join(sessionsRoot, wd.name);
    let sids;
    try {
      sids = fs.readdirSync(wdPath, { withFileTypes: true });
    } catch (e) {
      continue;
    }
    for (const sid of sids) {
      if (!sid.isDirectory()) continue;
      const sessionDir = path.join(wdPath, sid.name);
      const statePath = path.join(sessionDir, 'state.json');
      if (!isFileSafe(statePath)) continue;
      let workDir = '';
      try {
        const st = JSON.parse(fs.readFileSync(statePath, 'utf-8'));
        workDir = st.workDir || '';
      } catch (e) { /* ignore */ }
      if (!workDir) workDir = workspaces.get(wd.name) || '';
      out.push({ sessionId: sid.name, sessionDir, workDir });
    }
  }
  return out;
}


// ---------- state.json（标题与时间） ----------

function loadState(sessionDir) {
  const statePath = path.join(sessionDir, 'state.json');
  if (!isFileSafe(statePath)) return null;
  try {
    return JSON.parse(fs.readFileSync(statePath, 'utf-8'));
  } catch (e) {
    return null;
  }
}


// ---------- 时间格式化（同时支持 epoch 毫秒与 ISO 字符串） ----------

function fmtTime(ts) {
  if (ts === null || ts === undefined || ts === '') return '';
  let d;
  if (typeof ts === 'number') {
    d = new Date(ts);
  } else if (typeof ts === 'string') {
    const n = Number(ts);
    d = isNaN(n) ? new Date(ts) : new Date(n);
  } else {
    return String(ts);
  }
  if (isNaN(d.getTime())) return String(ts);
  const t = new Date(d.getTime() + CST_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())} ` +
         `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}:${pad(t.getUTCSeconds())}`;
}

function parseIsoMs(s) {
  if (!s) return 0;
  const d = new Date(s);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}


// ---------- wire.jsonl 解析 ----------

function parseWire(wirePath) {
  let content;
  try {
    content = fs.readFileSync(wirePath, 'utf-8');
  } catch (e) {
    return { events: [], err: e.message };
  }
  const events = [];
  for (const line of content.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      events.push(JSON.parse(l));
    } catch (e) { /* skip */ }
  }
  return { events, err: null };
}

function joinInputText(inp) {
  // turn.prompt.input 是 [{type,text},...]，抽取文本部分。
  if (!Array.isArray(inp)) return '';
  return inp
    .map((b) => (b && typeof b === 'object' && typeof b.text === 'string') ? b.text : '')
    .filter(Boolean)
    .join('');
}

function outputToText(out) {
  // tool.result.output 可能是字符串（错误信息）或 [{type,text},...] 列表。
  if (out === null || out === undefined) return '';
  if (typeof out === 'string') return out;
  if (Array.isArray(out)) {
    return out
      .map((b) => (b && typeof b === 'object' && typeof b.text === 'string') ? b.text : '')
      .filter(Boolean)
      .join('\n');
  }
  if (typeof out === 'object') return JSON.stringify(out);
  return String(out);
}

function normalizeInput(name, args, disp) {
  // 优先用 Kimi 自带的结构化 display 字段；缺失时按工具名从 args 抽取。
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

function normalizeEvents(events) {
  // 把 wire.jsonl 事件拍平为有序的归一化条目列表。
  // 顶层 type: metadata / config.update / turn.prompt / context.append_message /
  //   context.append_loop_event / llm.request / usage.record / tools.set_active_tools / ...
  // loop_event.event.type: step.begin / step.end / content.part / tool.call / tool.result
  const items = []; // kind ∈ user_text/assistant_text/tool_use/tool_result
  let model = null;
  let protocolVersion = null;

  let pendingText = '';
  let pendingTs = '';
  const flushText = () => {
    if (pendingText && pendingText.trim()) {
      items.push({ kind: 'assistant_text', timestamp: pendingTs, text: pendingText });
    }
    pendingText = '';
    pendingTs = '';
  };

  for (const o of events) {
    const t = o.type;
    const ts = (o.time !== undefined && o.time !== null) ? o.time : '';

    if (t === 'metadata') {
      if (!protocolVersion && o.protocol_version) protocolVersion = o.protocol_version;
      continue;
    }
    if (t === 'config.update') {
      if (!model && o.modelAlias) model = o.modelAlias;
      continue;
    }
    if (t === 'turn.prompt') {
      flushText();
      const text = joinInputText(o.input).trim();
      if (text) items.push({ kind: 'user_text', timestamp: ts, text });
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
        if (p && p.type === 'text') {
          if (!pendingText) pendingTs = ts;
          pendingText += p.text || '';
        }
        // think / 其他 part 跳过
      } else if (et === 'tool.call') {
        flushText();
        items.push({
          kind: 'tool_use',
          timestamp: ts,
          name: ev.name || '',
          input: normalizeInput(ev.name, ev.args, ev.display),
          call_id: ev.toolCallId || '',
        });
      } else if (et === 'tool.result') {
        flushText();
        const r = (ev.result && typeof ev.result === 'object') ? ev.result : {};
        let content = outputToText(r.output);
        if (!content.trim() && r.note) content = String(r.note);
        items.push({
          kind: 'tool_result',
          timestamp: ts,
          call_id: ev.toolCallId || ev.parentUuid || '',
          content,
          is_error: r.isError === true,
        });
      }
      // step.end / 其他跳过
      continue;
    }
    // context.append_message（多为 injection）、llm.request、usage.record 等跳过
  }
  flushText();

  return { items, model, protocol_version: protocolVersion };
}


// ---------- 标题解析 ----------

function resolveTitle(stateTitle, items, sessionId) {
  // 主来源：state.json 的 title（Kimi 侧边栏显示的标题）。
  // "New Session" 是未生成标题时的占位符，视为无标题走兜底。
  if (stateTitle && stateTitle.trim() && stateTitle.trim() !== 'New Session') {
    return stateTitle.trim();
  }
  for (const it of items) {
    if (it.kind === 'user_text') {
      const trimmed = it.text.trim();
      // 用 \r\n|\r|\n 切分（与 Python 实现一致），避免 CRLF 残留 \r 导致两端输出不一致。
      const firstLine = trimmed ? trimmed.split(/\r\n|\r|\n/)[0] : '';
      return cpLen(firstLine) > 60 ? cpSlice(firstLine, 60) + '…' : (firstLine || sessionId);
    }
  }
  return sessionId;
}


// ---------- 工具输入摘要 ----------

function truncate(s, n) {
  s = String(s);
  return cpLen(s) <= n ? s : cpSlice(s, n) + '…';
}

// 按码点（而非 UTF-16 码元）计数与切片，使非 BMP 字符（如 emoji）下与 Python 实现输出一致。
function cpLen(s) {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

function cpSlice(s, n) {
  // 返回前 n 个码点的子串（与 Python s[:n] 一致）。
  let out = '';
  let count = 0;
  for (const ch of s) {
    if (count >= n) break;
    out += ch;
    count++;
  }
  return out;
}

function toolBrief(name, inp) {
  // 单行紧凑描述一个工具调用（用于「更早活动」列表）。
  inp = inp || {};
  const c = (v, n = 80) => truncate(String(v || '').replace(/\n/g, ' '), n);
  if (name === 'Bash') return `Bash(${c(inp.command, 100)})`;
  if (name === 'Read' || name === 'ReadMediaFile') return `${name}(${c(inp.path, 80)})`;
  if (name === 'Write' || name === 'Edit') return `${name}(${c(inp.path, 80)})`;
  if (name === 'Grep') return `Grep(${c(inp.pattern, 60)})`;
  if (name === 'Glob') return `Glob(${c(inp.pattern, 60)})`;
  if (name === 'Agent' || name === 'AgentSwarm') return `${name}(${c(inp.agent_name, 40)})`;
  if (name === 'WebSearch') return `WebSearch(${c(inp.query, 60)})`;
  if (name === 'FetchURL') return `FetchURL(${c(inp.url, 60)})`;
  if (name === 'Skill') return `Skill(${c(inp.skill, 40)})`;
  for (const v of Object.values(inp)) {
    if (typeof v === 'string' && v) return `${name}(${c(v, 80)})`;
  }
  return `${name}(...)`;
}


// ---------- 任务状态重建 ----------

const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
const READ_TOOLS = new Set(['Read', 'ReadMediaFile', 'Grep', 'Glob']);
const EDIT_TOOLS = new Set(['Write', 'Edit']);

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
  const filesRead = [], filesEdited = [], commands = [], testResults = [];
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
      lastCommand = '';
      if (name === 'Bash' && inp.command) {
        const cmd = String(inp.command).trim();
        lastCommand = cmd;
        if (cmd) commands.push(cmd);
      } else if (READ_TOOLS.has(name)) {
        if (inp.path) filesRead.push(inp.path);
      } else if (EDIT_TOOLS.has(name)) {
        if (inp.path) filesEdited.push(inp.path);
      } else if (inp.operation === 'delete' && inp.path) {
        filesEdited.push(inp.path);
      }
    } else if (k === 'tool_result') {
      const content = it.content || '';
      const isTestCmd = lastToolName === 'Bash' && TEST_CMD_RE.test(lastCommand);
      const head = cpSlice(content, 2000);
      const looksTest = isTestCmd || (!!content && TEST_RESULT_RE.test(head));
      const isErr = it.is_error ||
                    (/(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)/i.test(head) &&
                     !/no error|0 failed/i.test(head));
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
    files_read: dedupe(filesRead),
    files_edited: dedupe(filesEdited),
    commands: dedupe(commands),
    test_results: testResults,
    last_user: lastUser,
    last_assistant: lastAssistant,
  };
}


// ---------- Markdown 渲染 ----------

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

function block(text, maxChars) {
  if (!text || !text.trim()) return '';
  text = text.trim();
  const fullLen = cpLen(text);
  if (fullLen > maxChars) {
    text = cpSlice(text, maxChars) + `\n…（已截断，原长 ${fullLen} 字符）`;
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

  lines.push('# Resume-Kimi 会话接管摘要');
  lines.push('');
  lines.push('## 会话信息');
  lines.push(`- 标题: ${info.title}`);
  lines.push(`- 会话ID: ${info.session_id}`);
  lines.push(`- 项目: ${info.cwd || '(未知)'}`);
  if (info.protocol_version) lines.push(`- 协议版本: ${info.protocol_version}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`);
  lines.push(`- 消息条目数: ${norm.items.length}`);
  lines.push('');

  lines.push('## 任务状态重建');
  lines.push('');
  lines.push('### 目标');
  lines.push(block(state.goal, maxChars) || '(未识别)');
  lines.push('');
  if (state.files_read.length) {
    lines.push('### 已调查文件');
    for (const f of state.files_read) lines.push(`- ${f}`);
    lines.push('');
  }
  if (state.files_edited.length) {
    lines.push('### 代码修改');
    for (const f of state.files_edited) lines.push(`- ${f}`);
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
      // 用 \r\n|\r|\n 切分（与 Python 实现一致），避免 CRLF 残留 \r 导致两端输出不一致。
      const firstLine = trimmed ? trimmed.split(/\r\n|\r|\n/)[0] : '';
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


// ---------- 会话扫描 ----------

function gatherSessionEntries(kimiDir) {
  // 收集所有会话条目（不分项目），优先读 session_index，缺失时扫描 sessions 目录。
  const workspaces = loadWorkspaces(kimiDir);
  let entries = loadSessionIndex(kimiDir);
  if (!entries.length) {
    entries = walkSessions(kimiDir, workspaces);
  }
  return entries;
}

function scanSessions(kimiDir, projectPath) {
  // 收集当前项目的会话，按 state.updatedAt 倒序。
  const norm = normPath(projectPath);
  const entries = gatherSessionEntries(kimiDir);
  const sessions = [];
  for (const e of entries) {
    if (!e.workDir || normPath(e.workDir) !== norm) continue;
    const state = loadState(e.sessionDir) || {};
    const updatedAt = state.updatedAt || '';
    let sortKey = parseIsoMs(updatedAt);
    if (!sortKey) {
      try {
        sortKey = fs.statSync(e.sessionDir).mtimeMs;
      } catch (err) {
        sortKey = 0;
      }
    }
    sessions.push({
      sessionId: e.sessionId,
      sessionDir: e.sessionDir,
      workDir: e.workDir,
      stateTitle: state.title || '',
      updatedAt,
      mtime: sortKey,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function scanAllSessions(kimiDir) {
  // 跨项目扫描：收集所有会话条目，不按 workDir 过滤。按 state.updatedAt 倒序。
  const entries = gatherSessionEntries(kimiDir);
  const sessions = [];
  for (const e of entries) {
    const state = loadState(e.sessionDir) || {};
    const updatedAt = state.updatedAt || '';
    let sortKey = parseIsoMs(updatedAt);
    if (!sortKey) {
      try {
        sortKey = fs.statSync(e.sessionDir).mtimeMs;
      } catch (err) {
        sortKey = 0;
      }
    }
    sessions.push({
      sessionId: e.sessionId,
      sessionDir: e.sessionDir,
      workDir: e.workDir,
      stateTitle: state.title || '',
      updatedAt,
      mtime: sortKey,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}


// ---------- 会话加载（完整） ----------

function loadSession(sessionDir, sessionId, workDir) {
  const state = loadState(sessionDir) || {};
  const wirePath = path.join(sessionDir, 'agents', 'main', 'wire.jsonl');
  const { events, err } = parseWire(wirePath);
  if (err) return { session: null, err };

  const norm = normalizeEvents(events);
  const items = norm.items;
  const title = resolveTitle(state.title, items, sessionId);
  const cwd = workDir || state.workDir || '';
  const createdAt = state.createdAt || '';
  const updatedAt = state.updatedAt || '';
  const itemTs = items.filter((it) => it.timestamp !== '' && it.timestamp !== undefined && it.timestamp !== null)
                      .map((it) => it.timestamp);
  const info = {
    session_id: sessionId,
    title,
    cwd,
    protocol_version: norm.protocol_version || '',
    model: norm.model || '',
    first_ts: createdAt ? fmtTime(createdAt) : (itemTs.length ? fmtTime(itemTs[0]) : ''),
    last_ts: updatedAt ? fmtTime(updatedAt) : (itemTs.length ? fmtTime(itemTs[itemTs.length - 1]) : ''),
  };
  return { session: { info, normalized: norm, events }, err: null };
}


// ---------- 会话扫描（轻量，用于 --list） ----------

function sessionMetaLite(s) {
  // 轻量解析：标题取 state.json，时间取 state.updatedAt，条目数取 wire。
  const state = loadState(s.sessionDir) || {};
  const wirePath = path.join(s.sessionDir, 'agents', 'main', 'wire.jsonl');
  const { events, err } = parseWire(wirePath);
  let count = 0;
  let firstUserText = '';
  if (!err) {
    const norm = normalizeEvents(events);
    count = norm.items.length;
    for (const it of norm.items) {
      if (it.kind === 'user_text') { firstUserText = it.text; break; }
    }
  }
  const title = resolveTitle(state.title || s.stateTitle, err ? [] : [{ kind: 'user_text', text: firstUserText }], s.sessionId);
  return {
    session_id: s.sessionId,
    title,
    last_ts: state.updatedAt ? fmtTime(state.updatedAt) : '',
    count,
  };
}

function printSessionList(sessions, projectPath, kimiDir, limit = 0) {
  const total = sessions.length;
  const shown = limit && limit > 0 ? sessions.slice(0, limit) : sessions;
  console.log(`当前项目: ${projectPath}`);
  console.log(`会话根: ${path.join(kimiDir, 'sessions')}`);
  if (limit && limit > 0 && limit < total) {
    console.log(`找到 ${total} 个会话（仅显示最近 ${shown.length} 个）：\n`);
  } else {
    console.log(`找到 ${total} 个会话：\n`);
  }
  shown.forEach((s, i) => {
    const meta = sessionMetaLite(s) || {};
    const mark = i === 0 ? '[最近]' : '      ';
    const title = meta.title || '(无标题)';
    const lastTs = meta.last_ts || '(无时间)';
    const count = meta.count || 0;
    console.log(`${mark} ${lastTs}  ${s.sessionId.slice(0, 20)}  消息数:${String(count).padEnd(4)} 标题: ${title}`);
  });
}


// ---------- 选择会话 ----------

function pickSession(sessions, sessionArg) {
  if (!sessionArg) return sessions[0];
  const arg = sessionArg.toLowerCase();
  // 先按 sessionId 前缀/包含匹配
  for (const s of sessions) {
    const sid = s.sessionId.toLowerCase();
    if (sid.startsWith(arg) || sid.includes(arg)) return s;
  }
  // 再按标题关键词匹配
  for (const s of sessions) {
    const title = (s.stateTitle || '').toLowerCase();
    if (title && title.includes(arg)) return s;
  }
  return null;
}


// ---------- CLI ----------

function printHelp() {
  console.log(`用法: node resume_kimi.js [选项]

读取 Kimi Code CLI 本地会话记录（session_index + state.json + wire.jsonl），
生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID 或前缀（支持跨项目全局查找，也匹配标题关键词）
  --project PATH      项目路径，默认当前工作目录
  --kimi-dir DIR      Kimi Code 配置目录，默认 ~/.kimi-code 或 \$KIMI_HOME
  --recent N          近期对话保留条目数，默认 8
  --max-chars N       单条内容截断长度，默认 1500
  --limit N           --list 返回的会话数量上限，0 不限制（默认 0）
  --json              以 JSON 输出（机器可读）
  --output FILE       将摘要写入文件
  -h, --help          显示此帮助
`);
}

function parseArgs(argv) {
  const args = {
    list: false,
    latest: false,
    session: null,
    project: null,
    'kimi-dir': null,
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
    else if (a === '--kimi-dir') args['kimi-dir'] = needValue(a, i++);
    else if (a.startsWith('--kimi-dir=')) args['kimi-dir'] = a.slice('--kimi-dir='.length);
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

  const kimiDir = args['kimi-dir'] || getKimiDir();
  const projectPathArg = path.resolve(args.project || process.cwd());

  let target = null;
  let projectPath = projectPathArg;
  let sessions = [];

  // 若指定了 session，优先跨项目全局查找。
  if (args.session) {
    sessions = scanAllSessions(kimiDir);
    target = pickSession(sessions, args.session);
    if (target && target.workDir) projectPath = target.workDir;
  }

  // 未指定 session 或全局未找到时，回退到基于项目路径查找。
  if (!target) {
    sessions = scanSessions(kimiDir, projectPathArg);
    if (!sessions.length) {
      console.error(`错误：未找到项目 ${projectPathArg} 对应的 Kimi Code 会话。`);
      console.error(`已查找：${path.join(kimiDir, 'sessions')}`);
      process.exit(1);
    }
    target = pickSession(sessions, args.session);
    if (!target) {
      console.error(`错误：未匹配到会话 '${args.session}'。使用 --list 查看可用会话。`);
      process.exit(1);
    }
  }

  if (args.list) {
    printSessionList(sessions, projectPath, kimiDir, args.limit);
    return;
  }

  const { session, err } = loadSession(target.sessionDir, target.sessionId, target.workDir);
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
        files_read: state.files_read,
        files_edited: state.files_edited,
        commands: state.commands,
        test_results: state.test_results,
        last_user: state.last_user,
        last_assistant: state.last_assistant,
      },
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
