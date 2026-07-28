#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-grok: 读取 Grok Build CLI 本地会话记录（~/.grok 下的
 * sessions/<encoded-cwd>/<session-id>/summary.json + chat_history.jsonl
 * [+ events.jsonl / updates.jsonl]），生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（Claude Code / Codex / Kimi 等）均可直接调用：
 *   node resume_grok.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_grok.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude/codex/cursor/kimi 一致）
const CST_OFFSET_MS = 8 * 3600 * 1000;


// ---------- 路径与项目定位 ----------

function getGrokDir() {
  return process.env.GROK_HOME || path.join(os.homedir(), '.grok');
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

// Grok 把工作目录 URL-encode 后作为会话分组目录名；超 255 字节时改用 slug+hash
// 并在同目录写一个 .cwd 文件记录原始路径。这里做兜底解码（主来源仍是 summary.info.cwd）。
function decodeCwdDir(name) {
  try {
    return decodeURIComponent(name);
  } catch (e) {
    return name;
  }
}


// ---------- summary.json（会话元数据 / 索引） ----------

function loadSummary(sessionDir) {
  const p = path.join(sessionDir, 'summary.json');
  if (!isFileSafe(p)) return null;
  try {
    return JSON.parse(fs.readFileSync(p, 'utf-8'));
  } catch (e) {
    return null;
  }
}

// 从 summary 解析会话所属工作目录（匹配 --project 的主来源）。
function summaryCwd(summary, sessionDir, dirName) {
  if (summary && summary.info && summary.info.cwd) return summary.info.cwd;
  if (summary && summary.git_root_dir) return summary.git_root_dir;
  // 兜底 1：同目录的 .cwd 文件（编码名超长时 Grok 写入）
  const cwdFile = path.join(sessionDir, '.cwd');
  if (isFileSafe(cwdFile)) {
    try {
      const s = fs.readFileSync(cwdFile, 'utf-8').trim();
      if (s) return s;
    } catch (e) { /* ignore */ }
  }
  // 兜底 2：解码分组目录名
  return decodeCwdDir(dirName);
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


// ---------- JSONL 读取 ----------

function readJsonl(p) {
  let content;
  try {
    content = fs.readFileSync(p, 'utf-8');
  } catch (e) {
    return { rows: [], err: e.message };
  }
  const rows = [];
  for (const line of content.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      rows.push(JSON.parse(l));
    } catch (e) { /* skip */ }
  }
  return { rows, err: null };
}


// ---------- 文本抽取 ----------

// content 可能是字符串、[{type,text},...] 列表或 {text} 对象，统一抽为纯文本。
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
  // OpenAI 风格的 tool_call.function.arguments 通常是 JSON 字符串。
  if (args === null || args === undefined) return {};
  if (typeof args === 'string') {
    const s = args.trim();
    if (!s) return {};
    try { return JSON.parse(s); } catch (e) { return { _raw: s }; }
  }
  return args;
}


// ---------- 工具输入归一化 ----------

// 按工具名从 input 抽取简洁字段（与 resume-kimi 的 normalizeInput 思路一致）。
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


// ---------- chat_history.jsonl 解析（主 transcript 来源） ----------

// Grok 的 chat_history.jsonl 是发给模型的原始消息（Anthropic 风格 content block）。
// 每行：{type:"system"|"user"|"assistant"|..., content:string|[block...], synthetic_reason?}
function normalizeChat(rows) {
  const items = []; // kind ∈ user_text/assistant_text/tool_use/tool_result
  for (const msg of rows) {
    if (!msg || typeof msg !== 'object') continue;
    const type = msg.type || msg.role || '';

    // system 消息（系统提示）跳过
    if (type === 'system') continue;
    // 注入的合成消息（技能清单 / system-reminder 等）跳过，不计入 goal / transcript
    if (type === 'user' && msg.synthetic_reason) continue;

    // OpenAI 风格：assistant 携带 tool_calls 数组
    if ((type === 'assistant') && Array.isArray(msg.tool_calls)) {
      if (typeof msg.content === 'string' && msg.content.trim()) {
        items.push({ kind: 'assistant_text', timestamp: '', text: msg.content });
      }
      for (const tc of msg.tool_calls) {
        const fn = (tc && tc.function) || {};
        const name = fn.name || '';
        const input = parseJsonArgs(fn.arguments);
        items.push({ kind: 'tool_use', timestamp: '', name, input: normalizeInput(name, input), call_id: tc.id || '' });
      }
      continue;
    }
    // OpenAI 风格：role "tool" 的工具结果
    if (type === 'tool') {
      items.push({ kind: 'tool_result', timestamp: '', call_id: msg.tool_call_id || '', content: textOf(msg.content), is_error: false });
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
          items.push({ kind: type === 'user' ? 'user_text' : 'assistant_text', timestamp: '', text });
        } else if (bt === 'tool_use') {
          const name = b.name || '';
          items.push({ kind: 'tool_use', timestamp: '', name, input: normalizeInput(name, b.input || {}), call_id: b.id || '' });
        } else if (bt === 'tool_result') {
          items.push({
            kind: 'tool_result',
            timestamp: '',
            call_id: b.tool_use_id || '',
            content: textOf(b.content),
            is_error: b.is_error === true,
          });
        }
        // thinking / image / 其他 block 跳过
      }
      continue;
    }
    // content 为字符串
    if (typeof content === 'string' && content.trim()) {
      items.push({ kind: type === 'user' ? 'user_text' : 'assistant_text', timestamp: '', text: content });
    }
  }
  return { items, model: null };
}


// ---------- events.jsonl / updates.jsonl 解析（ACP 流，兜底 transcript） ----------

// 每行是一个 ACP session/update 事件：可能是完整 JSON-RPC 通知
//   {method:"session/update", params:{update:{sessionUpdate,...}}}
// 也可能是裸 update 对象 {sessionUpdate:"agent_message_chunk", content:{text}}。
function normalizeAcp(rows) {
  const items = [];
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
      if (text.trim()) items.push({ kind: 'assistant_text', timestamp: '', text });
    } else if (kind === 'user_message_chunk') {
      const text = textOf(u.content);
      if (text.trim()) items.push({ kind: 'user_text', timestamp: '', text });
    } else if (kind === 'agent_thought_chunk') {
      // 推理片段，跳过（与 kimi 跳过 think / claude 跳过 thinking 一致）
      continue;
    } else if (kind === 'tool_call' || kind === 'tool') {
      const name = u.tool || u.name || '';
      const input = u.arguments || u.rawInput || u.input || {};
      const callId = u.id || u.callId || u.toolCallId || '';
      items.push({ kind: 'tool_use', timestamp: '', name, input: normalizeInput(name, input), call_id: callId });
      // 完成态携带输出时，同步产出工具结果
      const state = u.state || '';
      const out = u.rawOutput ? textOf(u.rawOutput) : ((state === 'completed' || state === 'failed') ? textOf(u.content) : '');
      if (out && out.trim()) {
        items.push({ kind: 'tool_result', timestamp: '', call_id: callId, content: out, is_error: state === 'failed' });
      }
    } else if (kind === 'tool_result' || kind === 'tool_call_result') {
      items.push({
        kind: 'tool_result',
        timestamp: '',
        call_id: u.toolUseId || u.tool_use_id || u.id || '',
        content: textOf(u.content || u.output),
        is_error: u.isError === true || u.is_error === true,
      });
    } else if (kind === 'config' || kind === 'config.update') {
      if (!model && (u.modelId || u.model)) model = u.modelId || u.model;
    }
    // plan / error / metadata 等跳过
  }
  return { items, model };
}


// ---------- 标题解析 ----------

function resolveTitle(summary, items, sessionId) {
  // 主来源：summary 的 generated_title / title / session_summary（Grok 模型生成的标题与摘要）。
  for (const k of ['generated_title', 'title', 'session_summary']) {
    const v = summary && summary[k];
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  // 兜底：首条用户消息首行（按码点截断 60 字符）或会话 ID
  for (const it of items) {
    if (it.kind === 'user_text') {
      const trimmed = it.text.trim();
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
  if (name === 'bash' || name === 'Bash') return `bash(${c(inp.command, 100)})`;
  if (name === 'read_file' || name === 'Read') return `${name}(${c(inp.path, 80)})`;
  if (name === 'write_file' || name === 'Write' || name === 'search_replace' || name === 'Edit') return `${name}(${c(inp.path, 80)})`;
  if (name === 'list_dir') return `list_dir(${c(inp.path, 80)})`;
  if (name === 'grep_search' || name === 'Grep') return `${name}(${c(inp.pattern, 60)})`;
  if (name === 'glob' || name === 'Glob') return `${name}(${c(inp.pattern, 60)})`;
  if (name === 'task' || name === 'Agent' || name === 'subagent') return `${name}(${c(inp.agent_name, 40)})`;
  if (name === 'web_search' || name === 'WebSearch') return `${name}(${c(inp.query, 60)})`;
  if (name === 'web_fetch' || name === 'FetchURL') return `${name}(${c(inp.url, 60)})`;
  if (name === 'monitor' || name === 'Monitor') return `${name}(${c(inp.command, 60)})`;
  for (const v of Object.values(inp)) {
    if (typeof v === 'string' && v) return `${name}(${c(v, 80)})`;
  }
  return `${name}(...)`;
}


// ---------- 任务状态重建 ----------

const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
const READ_TOOLS = new Set(['read_file', 'Read', 'list_dir', 'grep_search', 'Grep', 'glob', 'Glob']);
const EDIT_TOOLS = new Set(['write_file', 'Write', 'search_replace', 'Edit']);

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
  // 并行工具调用时，多个 tool_use 先于各自 tool_result 出现；按 call_id 精确归属结果，
  // call_id 缺失时回退到最近一次 tool_use（与 resume-kimi 的顺序启发式一致）。
  const callTool = new Map();

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
      let cmd = '';
      if ((name === 'bash' || name === 'Bash') && inp.command) {
        cmd = String(inp.command).trim();
        lastCommand = cmd;
        if (cmd) commands.push(cmd);
      } else if (READ_TOOLS.has(name)) {
        if (inp.path) filesRead.push(inp.path);
      } else if (EDIT_TOOLS.has(name)) {
        if (inp.path) filesEdited.push(inp.path);
      }
      if (it.call_id) callTool.set(it.call_id, { name, command: cmd });
    } else if (k === 'tool_result') {
      const content = it.content || '';
      const meta = (it.call_id && callTool.get(it.call_id)) || { name: lastToolName, command: lastCommand };
      const tName = meta.name, tCmd = meta.command;
      const isTestCmd = (tName === 'bash' || tName === 'Bash') && TEST_CMD_RE.test(tCmd);
      const head = cpSlice(content, 2000);
      const looksTest = isTestCmd || (!!content && TEST_RESULT_RE.test(head));
      const isErr = it.is_error ||
                    (/(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)/i.test(head) &&
                     !/no error|0 failed/i.test(head));
      if ((looksTest || isErr) && content.trim()) {
        testResults.push({
          command_hint: tCmd || tName,
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

  lines.push('# Resume-Grok 会话接管摘要');
  lines.push('');
  lines.push('## 会话信息');
  lines.push(`- 标题: ${info.title}`);
  lines.push(`- 会话ID: ${info.session_id}`);
  lines.push(`- 项目: ${info.cwd || '(未知)'}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  if (info.agent_name) lines.push(`- Agent: ${info.agent_name}`);
  if (info.reasoning_effort) lines.push(`- 推理强度: ${info.reasoning_effort}`);
  if (info.git_branch) lines.push(`- Git分支: ${info.git_branch}`);
  if (info.parent_session_id) lines.push(`- 派生自: ${info.parent_session_id}`);
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

function scanSessions(grokDir, projectPath) {
  // 收集当前项目的会话，按 summary.updated_at 倒序。
  const norm = normPath(projectPath);
  const sessionsRoot = path.join(grokDir, 'sessions');
  const sessions = [];
  if (!isDirSafe(sessionsRoot)) return sessions;

  let groups;
  try {
    groups = fs.readdirSync(sessionsRoot, { withFileTypes: true });
  } catch (e) {
    return sessions;
  }
  for (const g of groups) {
    if (!g.isDirectory()) continue;
    const groupPath = path.join(sessionsRoot, g.name);
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
      const cwd = summaryCwd(summary, sessionDir, g.name);
      if (!cwd || normPath(cwd) !== norm) continue;
      const updatedAt = (summary && summary.updated_at) || '';
      let sortKey = parseIsoMs(updatedAt);
      if (!sortKey) {
        try {
          sortKey = fs.statSync(sessionDir).mtimeMs;
        } catch (err) {
          sortKey = 0;
        }
      }
      sessions.push({
        sessionId: (summary && summary.info && summary.info.id) || sd.name,
        sessionDir,
        cwd,
        summary,
        updatedAt,
        mtime: sortKey,
      });
    }
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function scanAllSessions(grokDir) {
  // 跨项目扫描：遍历 ~/.grok/sessions 下所有会话，不按 cwd 过滤。按 summary.updated_at 倒序。
  const sessionsRoot = path.join(grokDir, 'sessions');
  const sessions = [];
  if (!isDirSafe(sessionsRoot)) return sessions;

  let groups;
  try {
    groups = fs.readdirSync(sessionsRoot, { withFileTypes: true });
  } catch (e) {
    return sessions;
  }
  for (const g of groups) {
    if (!g.isDirectory()) continue;
    const groupPath = path.join(sessionsRoot, g.name);
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
      const cwd = summaryCwd(summary, sessionDir, g.name);
      const updatedAt = (summary && summary.updated_at) || '';
      let sortKey = parseIsoMs(updatedAt);
      if (!sortKey) {
        try {
          sortKey = fs.statSync(sessionDir).mtimeMs;
        } catch (err) {
          sortKey = 0;
        }
      }
      sessions.push({
        sessionId: (summary && summary.info && summary.info.id) || sd.name,
        sessionDir,
        cwd,
        summary,
        updatedAt,
        mtime: sortKey,
      });
    }
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}


// ---------- 会话加载（完整） ----------

function loadSession(sessionDir, sessionId) {
  const summary = loadSummary(sessionDir) || {};

  // 主 transcript：chat_history.jsonl；为空时兜底 events.jsonl / updates.jsonl（ACP 流）。
  let norm = { items: [], model: null };
  let source = 'none';
  const chPath = path.join(sessionDir, 'chat_history.jsonl');
  if (isFileSafe(chPath)) {
    const { rows } = readJsonl(chPath);
    const r = normalizeChat(rows);
    if (r.items.length) { norm = r; source = 'chat_history.jsonl'; }
  }
  if (!norm.items.length) {
    for (const name of ['events.jsonl', 'updates.jsonl']) {
      const p = path.join(sessionDir, name);
      if (!isFileSafe(p)) continue;
      const { rows } = readJsonl(p);
      const r = normalizeAcp(rows);
      if (r.items.length) { norm = r; source = name; break; }
    }
  }

  const items = norm.items;
  const title = resolveTitle(summary, items, sessionId);
  const cwd = (summary.info && summary.info.cwd) || '';
  const createdAt = summary.created_at || '';
  const updatedAt = summary.updated_at || '';
  const itemTs = items.filter((it) => it.timestamp !== '' && it.timestamp !== undefined && it.timestamp !== null)
                      .map((it) => it.timestamp);
  const info = {
    session_id: sessionId,
    title,
    cwd,
    model: norm.model || summary.current_model_id || '',
    agent_name: summary.agent_name || '',
    reasoning_effort: summary.reasoning_effort || '',
    git_branch: summary.head_branch || '',
    parent_session_id: summary.parent_session_id || '',
    first_ts: createdAt ? fmtTime(createdAt) : (itemTs.length ? fmtTime(itemTs[0]) : ''),
    last_ts: updatedAt ? fmtTime(updatedAt) : (itemTs.length ? fmtTime(itemTs[itemTs.length - 1]) : ''),
  };
  return { session: { info, normalized: norm, source }, err: null };
}


// ---------- 会话扫描（轻量，用于 --list） ----------

function sessionMetaLite(s) {
  // 轻量解析：标题取 summary，时间取 updated_at，条目数取 chat_history（兜底 events）。
  const summary = s.summary || loadSummary(s.sessionDir) || {};
  let count = 0;
  let firstUserText = '';
  const chPath = path.join(s.sessionDir, 'chat_history.jsonl');
  if (isFileSafe(chPath)) {
    const { rows } = readJsonl(chPath);
    const r = normalizeChat(rows);
    count = r.items.length;
    for (const it of r.items) {
      if (it.kind === 'user_text') { firstUserText = it.text; break; }
    }
  }
  if (!count) {
    for (const name of ['events.jsonl', 'updates.jsonl']) {
      const p = path.join(s.sessionDir, name);
      if (!isFileSafe(p)) continue;
      const { rows } = readJsonl(p);
      const r = normalizeAcp(rows);
      if (r.items.length) {
        count = r.items.length;
        for (const it of r.items) {
          if (it.kind === 'user_text') { firstUserText = it.text; break; }
        }
        break;
      }
    }
  }
  const title = resolveTitle(summary, firstUserText ? [{ kind: 'user_text', text: firstUserText }] : [], s.sessionId);
  return {
    session_id: s.sessionId,
    title,
    last_ts: summary.updated_at ? fmtTime(summary.updated_at) : '',
    count,
  };
}

function printSessionList(sessions, projectPath, grokDir, limit = 0) {
  const total = sessions.length;
  const shown = limit && limit > 0 ? sessions.slice(0, limit) : sessions;
  console.log(`当前项目: ${projectPath}`);
  console.log(`会话根: ${path.join(grokDir, 'sessions')}`);
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
    const title = resolveTitle(s.summary, [], s.sessionId).toLowerCase();
    if (title && title.includes(arg)) return s;
  }
  return null;
}


// ---------- CLI ----------

function printHelp() {
  console.log(`用法: node resume_grok.js [选项]

读取 Grok Build CLI 本地会话记录（summary.json + chat_history.jsonl [+ events.jsonl]），
生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID 或前缀（支持跨项目全局查找，也匹配标题关键词）
  --project PATH      项目路径，默认当前工作目录
  --grok-dir DIR      Grok 配置目录，默认 ~/.grok 或 \$GROK_HOME
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
    'grok-dir': null,
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
    else if (a === '--grok-dir') args['grok-dir'] = needValue(a, i++);
    else if (a.startsWith('--grok-dir=')) args['grok-dir'] = a.slice('--grok-dir='.length);
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

  const grokDir = args['grok-dir'] || getGrokDir();
  const projectPathArg = path.resolve(args.project || process.cwd());

  let target = null;
  let projectPath = projectPathArg;
  let sessions = [];

  // 若指定了 session，优先跨项目全局查找。
  if (args.session) {
    sessions = scanAllSessions(grokDir);
    target = pickSession(sessions, args.session);
    if (target && target.cwd) projectPath = target.cwd;
  }

  // 未指定 session 或全局未找到时，回退到基于项目路径查找。
  if (!target) {
    sessions = scanSessions(grokDir, projectPathArg);
    if (!sessions.length) {
      console.error(`错误：未找到项目 ${projectPathArg} 对应的 Grok 会话。`);
      console.error(`已查找：${path.join(grokDir, 'sessions')}`);
      process.exit(1);
    }
    target = pickSession(sessions, args.session);
    if (!target) {
      console.error(`错误：未匹配到会话 '${args.session}'。使用 --list 查看可用会话。`);
      process.exit(1);
    }
  }

  if (args.list) {
    printSessionList(sessions, projectPath, grokDir, args.limit);
    return;
  }

  const { session, err } = loadSession(target.sessionDir, target.sessionId);
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
