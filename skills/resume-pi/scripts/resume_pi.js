#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-pi: 读取 Pi Coding Agent 本地会话 JSONL，生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（Pi / Claude Code / Grok / Codex 等）均可直接调用：
 *   node resume_pi.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_pi.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 *
 * pi 会话格式（v3）：
 *   - 存储于 ~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl
 *   - <encoded-cwd> = `--` + cwd 去掉开头分隔符、把 / \ : 替换为 - + `--`
 *   - 首行为 {type:"session"} 头（id/cwd/timestamp），其后条目经 id/parentId 构成树
 *   - 当前对话 = 从最后一个叶子条目沿 parentId 回溯到根的路径
 *   - 条目类型：message（user/assistant/toolResult/bashExecution/custom 等 role）、
 *     model_change、thinking_level_change、compaction、branch_summary、
 *     session_info（自定义标题）、custom_message、label、custom
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// AGENTS.md 约定：时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss
const CST_OFFSET_MS = 8 * 3600 * 1000;

// pi 内置工具分类（小写匹配），用于「已调查文件 / 代码修改」归类
const READ_TOOLS = new Set(['read', 'ls', 'find', 'grep']);
const EDIT_TOOLS = new Set(['write', 'edit']);
const SHELL_TOOLS = new Set(['bash', 'powershell']);


// ---------- 路径与项目定位 ----------

// pi 的 agent 目录：默认 ~/.pi/agent，可用 PI_CODING_AGENT_DIR 覆盖。
function getDefaultAgentDir() {
  const env = process.env.PI_CODING_AGENT_DIR;
  if (env) return expandTilde(env);
  return path.join(os.homedir(), '.pi', 'agent');
}

function expandTilde(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function isDirSafe(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch (e) {
    return false;
  }
}

// 把 --pi-dir（~/.pi 这一级）或 PI_CODING_AGENT_DIR（agent 这一级）归一为 sessions 根目录。
function resolveSessionsRoot(piDir) {
  const candidates = piDir
    ? [path.join(piDir, 'agent', 'sessions'), path.join(piDir, 'sessions')]
    : [path.join(getDefaultAgentDir(), 'sessions')];
  for (const c of candidates) {
    if (isDirSafe(c)) return c;
  }
  return candidates[0];
}

// pi 把 cwd 编码为会话目录名：--<去掉开头分隔符、[/\\:] → -> 的 cwd>--。
function encodeProjectPath(p) {
  const resolved = path.resolve(p);
  const stripped = resolved.replace(/^[/\\]/, '');
  return '--' + stripped.replace(/[/\\:]/g, '-') + '--';
}

function peekCwd(jsonlPath, maxLines = 10) {
  // 读取会话文件首行的 {type:"session"} 头，取 cwd 字段用于兜底匹配项目。
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
      if (obj.cwd) return obj.cwd;
    } catch (e) { /* skip */ }
  }
  return null;
}

function normPath(p) {
  return path.normalize(p).toLowerCase();
}

function findProjectDir(sessionsRoot, projectPath) {
  // 定位当前项目对应的 <sessionsRoot>/<encoded-cwd> 目录。
  if (!isDirSafe(sessionsRoot)) return null;

  const encoded = encodeProjectPath(projectPath);

  // 1. 精确匹配
  const cand = path.join(sessionsRoot, encoded);
  if (isDirSafe(cand)) return cand;

  let dirs;
  try {
    dirs = fs.readdirSync(sessionsRoot);
  } catch (e) {
    return null;
  }

  // 2. 大小写无关匹配
  const encLow = encoded.toLowerCase();
  for (const d of dirs) {
    if (d.toLowerCase() === encLow) {
      const full = path.join(sessionsRoot, d);
      if (isDirSafe(full)) return full;
    }
  }

  // 3. 兜底：扫描会话头中的 cwd 字段（限量，避免过慢）
  const norm = normPath(projectPath);
  let scanned = 0;
  for (const d of dirs.slice().sort()) {
    const full = path.join(sessionsRoot, d);
    if (!isDirSafe(full)) continue;
    let files;
    try {
      files = fs.readdirSync(full);
    } catch (e) {
      continue;
    }
    for (const fn of files) {
      if (!fn.endsWith('.jsonl')) continue;
      scanned++;
      if (scanned > 300) break;
      const cwd = peekCwd(path.join(full, fn));
      if (cwd && normPath(cwd) === norm) return full;
    }
    if (scanned > 300) break;
  }
  return null;
}


// ---------- 时间格式化 ----------

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  // 加 UTC+8 偏移后用 getUTC* 取分量，等价于 Python astimezone(CST)
  const t = new Date(d.getTime() + CST_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())} ` +
         `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}:${pad(t.getUTCSeconds())}`;
}


// ---------- JSONL 解析 ----------

function parseEvents(jsonlPath) {
  let content;
  try {
    content = fs.readFileSync(jsonlPath, 'utf-8');
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

// 从文件名推断会话 UUID：<timestamp>_<uuid>.jsonl → uuid。
function sessionIdFromFilename(p) {
  const base = path.basename(p).replace(/\.jsonl$/i, '');
  const idx = base.lastIndexOf('_');
  return idx >= 0 ? base.slice(idx + 1) : base;
}

// 沿最后一个叶子条目回溯 parentId 到根，得到当前激活的对话路径（支持 pi 会话分支）。
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

function shellCommand(inp) {
  return inp.command || inp.script || '';
}

function normalizeEvents(events) {
  // 把激活路径上的条目拍平为有序的归一化条目列表。
  const items = []; // kind ∈ user_text/assistant_text/error_text/custom_text/tool_use/tool_result
  const summaries = [];
  let title = null;           // session_info.name（手动 /cmd:name 设置的会话名）
  let model = null;           // provider/modelId
  let thinkingLevel = null;
  const calls = new Map();    // toolCall id → {name, input}

  // session 头没有 id，不在激活路径上，直接从整个文件取首个 {type:"session"} 条目。
  let header = null;
  for (const ev of events) {
    if (ev && ev.type === 'session') { header = ev; break; }
  }

  let path = activePath(events);
  if (!path.length) path = events; // 兼容无 id/parentId 的旧格式：按文件顺序线性处理

  for (const ev of path) {
    if (!ev || typeof ev !== 'object') continue;
    const etype = ev.type;

    if (etype === 'session') continue;
    if (etype === 'session_info') {
      if (ev.name) title = ev.name;
      continue;
    }
    if (etype === 'compaction' || etype === 'branch_summary') {
      if (ev.summary) summaries.push(ev.summary);
      continue;
    }
    if (etype === 'model_change') {
      if (ev.modelId) model = `${ev.provider || ''}${ev.provider && ev.modelId ? '/' : ''}${ev.modelId}`;
      continue;
    }
    if (etype === 'thinking_level_change') {
      if (ev.thinkingLevel) thinkingLevel = ev.thinkingLevel;
      continue;
    }
    if (etype !== 'message' && etype !== 'custom_message') continue;

    const ts = ev.timestamp || '';

    if (etype === 'custom_message') {
      const text = extractText(ev.content);
      if (text.trim()) {
        items.push({ kind: 'custom_text', timestamp: ts, text, custom_type: ev.customType || '' });
      }
      continue;
    }

    const msg = ev.message || {};
    const role = msg.role;

    if (role === 'user') {
      const text = extractText(msg.content);
      if (text.trim()) items.push({ kind: 'user_text', timestamp: ts, text });
    } else if (role === 'assistant') {
      const content = Array.isArray(msg.content) ? msg.content : [];
      let textOnly = '';
      for (const block of content) {
        if (!block || typeof block !== 'object') continue;
        if (block.type === 'text') {
          if (block.text) textOnly += (textOnly ? '\n' : '') + block.text;
        } else if (block.type === 'toolCall') {
          const input = block.arguments || {};
          calls.set(block.id, { name: block.name || '', input });
          items.push({ kind: 'tool_use', timestamp: ts, name: block.name || '', input, call_id: block.id || '' });
        }
        // thinking block 不进入接管摘要
      }
      if (textOnly.trim()) items.push({ kind: 'assistant_text', timestamp: ts, text: textOnly });
      if (msg.errorMessage) items.push({ kind: 'error_text', timestamp: ts, text: String(msg.errorMessage) });
    } else if (role === 'toolResult') {
      const call = calls.get(msg.toolCallId) || null;
      const name = msg.toolName || (call ? call.name : '');
      let commandHint = '';
      if (call && SHELL_TOOLS.has(String(name).toLowerCase())) {
        commandHint = shellCommand(call.input);
      }
      items.push({
        kind: 'tool_result',
        timestamp: ts,
        tool_call_id: msg.toolCallId || '',
        tool_name: name || '',
        content: extractText(msg.content),
        is_error: !!(msg.isError || msg.is_error),
        command_hint: commandHint,
      });
    } else if (role === 'bashExecution') {
      // 用户在 TUI 里以 ! / !! 直接执行的 shell 命令
      const cmd = msg.command || '';
      items.push({ kind: 'tool_use', timestamp: ts, name: 'bash', input: { command: cmd }, call_id: '' });
      const isErr = !!msg.cancelled || ((msg.exitCode == null ? 0 : msg.exitCode) !== 0);
      items.push({
        kind: 'tool_result',
        timestamp: ts,
        tool_call_id: '',
        tool_name: 'bash',
        content: msg.output || '',
        is_error: isErr,
        command_hint: cmd,
      });
    } else if (role === 'custom') {
      const text = extractText(msg.content);
      if (text.trim()) {
        items.push({ kind: 'custom_text', timestamp: ts, text, custom_type: msg.customType || '' });
      }
    } else if (role === 'compactionSummary' || role === 'branchSummary') {
      if (msg.summary) summaries.push(msg.summary);
    }
    // image / 其他 role 忽略
  }

  return {
    items,
    summaries,
    title,
    cwd: (header && header.cwd) || null,
    parent_session: (header && header.parentSession) || null,
    model,
    thinking_level: thinkingLevel,
  };
}


// ---------- 标题解析 ----------

function resolveTitle(title, items, sessionId) {
  // session_info.name > 首条用户消息摘要 > 会话 ID。
  if (title) return title;
  for (const it of items) {
    if (it.kind === 'user_text') {
      const trimmed = it.text.trim();
      const firstLine = firstLineOf(trimmed);
      return firstLine.length > 60 ? firstLine.slice(0, 60) + '…' : (firstLine || sessionId);
    }
  }
  return sessionId;
}


// ---------- 工具输入摘要 ----------

function truncate(s, n) {
  s = String(s);
  return s.length <= n ? s : s.slice(0, n) + '…';
}

// 与 Python str.splitlines() 对齐的「取首行」，保证两实现输出一致（\v \f \x1c-\x1e \x85 等也算换行）。
const LINE_BREAK_RE = /[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

function firstLineOf(s) {
  return String(s).split(LINE_BREAK_RE)[0];
}

function toolFile(name, inp) {
  const n = String(name).toLowerCase();
  if (n === 'read' || n === 'write' || n === 'edit' || n === 'ls') return inp.path || inp.file_path;
  if (n === 'find' || n === 'grep') return inp.pattern || inp.path;
  return null;
}

function toolBrief(name, inp) {
  // 单行紧凑描述一个工具调用。
  const n = String(name).toLowerCase();
  if (n === 'bash' || n === 'powershell') {
    const cmd = shellCommand(inp).trim().replace(/\n/g, ' ');
    return `${name}(${truncate(cmd, 100)})`;
  }
  const f = toolFile(name, inp);
  if (f) return `${name}(${truncate(f, 100)})`;
  for (const v of Object.values(inp)) {
    if (typeof v === 'string' && v) return `${name}(${truncate(v, 80)})`;
  }
  return `${name}(...)`;
}


// ---------- 任务状态重建 ----------

const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b/i;
// 只匹配明确的测试摘要标记，避免把含 error/test/fail 的普通代码文件误判为测试结果。
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;

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
  const calls = new Map(); // call_id → tool_use 条目
  let lastCommand = '';

  for (const it of items) {
    const k = it.kind;
    if (k === 'user_text') {
      if (!firstUser) firstUser = it.text;
      lastUser = it.text;
    } else if (k === 'assistant_text') {
      lastAssistant = it.text;
    } else if (k === 'tool_use') {
      const name = String(it.name).toLowerCase();
      const inp = it.input || {};
      if (it.call_id) calls.set(it.call_id, it);
      if (READ_TOOLS.has(name)) {
        const f = toolFile(it.name, inp);
        if (f) filesRead.push(f);
      } else if (EDIT_TOOLS.has(name)) {
        const f = toolFile(it.name, inp);
        if (f) filesEdited.push(f);
      } else if (SHELL_TOOLS.has(name)) {
        const cmd = shellCommand(inp).trim();
        if (cmd) { commands.push(cmd); lastCommand = cmd; }
      }
    } else if (k === 'tool_result') {
      const call = it.tool_call_id ? calls.get(it.tool_call_id) || null : null;
      const name = String(it.tool_name || (call ? call.name : '')).toLowerCase();
      const cmd = it.command_hint
        || (call && SHELL_TOOLS.has(String(call.name).toLowerCase()) ? shellCommand(call.input) : '')
        || (SHELL_TOOLS.has(name) ? lastCommand : '');
      const content = it.content || '';
      const isErr = !!it.is_error;
      const isTestCmd = SHELL_TOOLS.has(name) && TEST_CMD_RE.test(cmd || '');
      const looksTest = isTestCmd || (!!content && TEST_RESULT_RE.test(content.slice(0, 2000)));
      if ((looksTest || isErr) && content.trim()) {
        testResults.push({
          command_hint: cmd || it.tool_name || (call ? call.name : ''),
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
  if (text.length > maxChars) {
    text = text.slice(0, maxChars) + `\n…（已截断，原长 ${text.length} 字符）`;
  }
  return text;
}

function renderItem(it, maxChars) {
  const k = it.kind;
  const ts = fmtTime(it.timestamp);
  if (k === 'user_text') return [`### [用户] ${ts}`, block(it.text, maxChars)];
  if (k === 'assistant_text') return [`### [助手] ${ts}`, block(it.text, maxChars)];
  if (k === 'error_text') return [`### [错误] ${ts}`, block(it.text, maxChars)];
  if (k === 'custom_text') {
    const tag = it.custom_type ? `（${it.custom_type}）` : '';
    return [`### [扩展消息]${tag} ${ts}`, block(it.text, maxChars)];
  }
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

  lines.push('# Resume-Pi 会话接管摘要');
  lines.push('');
  lines.push('## 会话信息');
  lines.push(`- 标题: ${info.title}`);
  lines.push(`- 会话ID: ${info.session_id}`);
  lines.push(`- 项目: ${info.cwd || '(未知)'}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  if (info.thinking_level) lines.push(`- 思考级别: ${info.thinking_level}`);
  if (info.parent_session) lines.push(`- 父会话: ${info.parent_session}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`);
  lines.push(`- 消息条目数: ${norm.items.length}`);
  lines.push('');

  if (norm.summaries.length) {
    lines.push('## 历史摘要（原会话 compact / 分支摘要）');
    for (const s of norm.summaries) lines.push(`- ${truncate(s, maxChars)}`);
    lines.push('');
  }

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
      const firstLine = firstLineOf(tr.content.trim());
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

function scanSessions(projectDir) {
  // 轻量扫描：仅按文件名与修改时间列出会话，不解析内容。按 mtime 倒序。
  const sessions = [];
  let files;
  try {
    files = fs.readdirSync(projectDir);
  } catch (e) {
    return sessions;
  }
  for (const fn of files) {
    if (!fn.endsWith('.jsonl')) continue;
    const p = path.join(projectDir, fn);
    let stat;
    try {
      stat = fs.statSync(p);
    } catch (e) {
      continue;
    }
    if (!stat.isFile()) continue;
    sessions.push({
      session_id: sessionIdFromFilename(p),
      path: p,
      mtime: stat.mtimeMs,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function scanAllSessions(sessionsRoot) {
  // 跨项目扫描：遍历 sessions 根目录下所有项目目录的会话。按 mtime 倒序。
  if (!isDirSafe(sessionsRoot)) return [];
  let dirs;
  try {
    dirs = fs.readdirSync(sessionsRoot);
  } catch (e) {
    return [];
  }
  const sessions = [];
  for (const d of dirs) {
    const full = path.join(sessionsRoot, d);
    if (!isDirSafe(full)) continue;
    sessions.push(...scanSessions(full));
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function sessionMeta(p) {
  // 解析单个会话，返回标题/时间范围/条目数（用于 --list 展示）。
  const { events, err } = parseEvents(p);
  if (err) return null;
  const norm = normalizeEvents(events);
  const items = norm.items;
  const sid = (function () {
    for (const ev of events) {
      if (ev && ev.type === 'session' && ev.id) return ev.id;
    }
    return sessionIdFromFilename(p);
  })();
  const timestamps = items.filter((it) => it.timestamp).map((it) => it.timestamp);
  return {
    session_id: sid,
    title: resolveTitle(norm.title, items, sid),
    first_ts: timestamps.length ? fmtTime(timestamps[0]) : '',
    last_ts: timestamps.length ? fmtTime(timestamps[timestamps.length - 1]) : '',
    count: items.length,
  };
}

function printSessionList(sessions, projectDir, projectPath, limit = 0) {
  const total = sessions.length;
  const shown = limit && limit > 0 ? sessions.slice(0, limit) : sessions;
  console.log(`当前项目: ${projectPath}`);
  console.log(`项目目录: ${path.basename(projectDir)}`);
  if (limit && limit > 0 && limit < total) {
    console.log(`找到 ${total} 个会话（仅显示最近 ${shown.length} 个）：\n`);
  } else {
    console.log(`找到 ${total} 个会话：\n`);
  }
  shown.forEach((s, i) => {
    const meta = sessionMeta(s.path) || {};
    const mark = i === 0 ? '[最近]' : '      ';
    const title = meta.title || '(无标题)';
    const lastTs = meta.last_ts || '(无时间)';
    const count = meta.count || 0;
    console.log(`${mark} ${lastTs}  ${s.session_id.slice(0, 12)}  消息数:${String(count).padEnd(4)} 标题: ${title}`);
  });
}


// ---------- 主流程 ----------

function loadSession(jsonlPath) {
  const { events, err } = parseEvents(jsonlPath);
  if (err) return { session: null, err };
  const norm = normalizeEvents(events);
  const items = norm.items;
  let sid = sessionIdFromFilename(jsonlPath);
  for (const ev of events) {
    if (ev && ev.type === 'session' && ev.id) { sid = ev.id; break; }
  }
  const timestamps = items.filter((it) => it.timestamp).map((it) => it.timestamp);
  const info = {
    session_id: sid,
    title: resolveTitle(norm.title, items, sid),
    cwd: norm.cwd,
    model: norm.model,
    thinking_level: norm.thinking_level,
    parent_session: norm.parent_session,
    first_ts: timestamps.length ? fmtTime(timestamps[0]) : '',
    last_ts: timestamps.length ? fmtTime(timestamps[timestamps.length - 1]) : '',
  };
  return { session: { info, normalized: norm, events }, err: null };
}

function pickSession(sessions, sessionArg) {
  if (!sessionArg) return sessions[0];
  for (const s of sessions) {
    const fname = path.basename(s.path).replace(/\.jsonl$/i, '');
    if (s.session_id.startsWith(sessionArg) || s.session_id.includes(sessionArg)) return s;
    if (fname.startsWith(sessionArg) || fname.includes(sessionArg)) return s;
  }
  return null;
}


// ---------- CLI ----------

function printHelp() {
  console.log(`用法: node resume_pi.js [选项]

读取 Pi Coding Agent 本地会话 JSONL，生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID / ID 前缀 / 文件名片段（支持跨项目全局查找）
  --project PATH      项目路径，默认当前工作目录
  --pi-dir DIR        Pi 数据目录，默认 ~/.pi（会话位于 <pi-dir>/agent/sessions）
  --recent N          近期对话保留条目数，默认 8
  --max-chars N       单条内容截断长度，默认 1500
  --limit N           --list 返回的会话数量上限，0 不限制（默认 0）
  --json              以 JSON 输出（机器可读）
  --output FILE       将摘要写入文件
  -h, --help          显示此帮助

环境变量:
  PI_CODING_AGENT_DIR  pi 的 agent 目录（~/.pi/agent 这一级），--pi-dir 未指定时生效
`);
}

function parseArgs(argv) {
  const args = {
    list: false,
    latest: false,
    session: null,
    project: null,
    'pi-dir': null,
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
    else if (a === '--pi-dir') args['pi-dir'] = expandTilde(needValue(a, i++));
    else if (a.startsWith('--pi-dir=')) args['pi-dir'] = expandTilde(a.slice('--pi-dir='.length));
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

  const sessionsRoot = resolveSessionsRoot(args['pi-dir']);
  const projectPathArg = path.resolve(args.project || process.cwd());

  let target = null;
  let projectDir = null;
  let projectPath = projectPathArg;

  // 若指定了 session，优先跨项目全局查找。
  if (args.session) {
    const allSessions = scanAllSessions(sessionsRoot);
    target = pickSession(allSessions, args.session);
    if (target) {
      projectDir = path.dirname(target.path);
      const cwd = peekCwd(target.path);
      if (cwd) projectPath = cwd;
    }
  }

  // 未指定 session 或全局未找到时，回退到基于项目路径查找。
  if (!target) {
    projectDir = findProjectDir(sessionsRoot, projectPathArg);
    if (!projectDir) {
      console.error(`错误：未找到项目 ${projectPathArg} 对应的会话目录。`);
      console.error(`已查找：${sessionsRoot}`);
      process.exit(1);
    }
    const sessions = scanSessions(projectDir);
    if (!sessions.length) {
      console.error(`错误：项目目录 ${projectDir} 下没有 .jsonl 会话文件。`);
      process.exit(1);
    }
    target = pickSession(sessions, args.session);
    if (!target) {
      console.error(`错误：未匹配到会话 '${args.session}'。使用 --list 查看可用会话。`);
      process.exit(1);
    }
  }

  if (args.list) {
    const sessions = scanSessions(projectDir);
    if (!sessions.length) {
      console.error(`错误：项目目录 ${projectDir} 下没有 .jsonl 会话文件。`);
      process.exit(1);
    }
    printSessionList(sessions, projectDir, projectPath, args.limit);
    return;
  }

  const { session, err } = loadSession(target.path);
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
      summaries: session.normalized.summaries,
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
