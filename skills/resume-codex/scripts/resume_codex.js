#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-codex: 读取 Codex CLI 本地会话记录（rollout JSONL + session_index），
 * 生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（Claude Code / Codex / Grok 等）均可直接调用：
 *   node resume_codex.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_codex.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude 一致）
const CST_OFFSET_MS = 8 * 3600 * 1000;

// Session ID（UUID）正则，用于从 rollout 文件名提取会话 ID
const SID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;


// ---------- 路径与项目定位 ----------

function getCodexDir() {
  return process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
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

function sidFromFilename(fn) {
  const m = path.basename(fn).match(SID_RE);
  if (m) return m[0];
  return path.basename(fn).replace(/^rollout-/, '').replace(/\.jsonl$/i, '');
}

function peekHead(filePath, bytes = 32768) {
  // 只读文件开头若干字节，用于快速 peek cwd，避免整文件读取。
  let fd;
  try {
    fd = fs.openSync(filePath, 'r');
    const buf = Buffer.alloc(bytes);
    const n = fs.readSync(fd, buf, 0, bytes, 0);
    return buf.slice(0, n).toString('utf-8');
  } catch (e) {
    return '';
  } finally {
    if (fd) {
      try { fs.closeSync(fd); } catch (e) { /* ignore */ }
    }
  }
}

function peekCwd(filePath) {
  // session_meta 首行可能极长（含 base_instructions），无法整行 JSON.parse；
  // 直接用正则取首个 "cwd":"..." 字段（session_meta.cwd 出现在 base_instructions 之前）。
  const head = peekHead(filePath);
  const m = head.match(/"cwd"\s*:\s*"([^"]+)"/);
  return m ? m[1] : null;
}

function walkJsonl(dir) {
  // 递归收集目录下所有 .jsonl 文件（codex 按 sessions/YYYY/MM/DD/ 组织）。
  const out = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return out;
  }
  for (const e of entries) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      out.push(...walkJsonl(full));
    } else if (e.isFile() && e.name.endsWith('.jsonl')) {
      out.push(full);
    }
  }
  return out;
}

function scanSessions(codexDir, projectPath) {
  // 扫描 ~/.codex/sessions/**/*.jsonl，按 cwd 匹配当前项目，按 mtime 倒序。
  const sessionsRoot = path.join(codexDir, 'sessions');
  const norm = normPath(projectPath);
  const sessions = [];
  for (const p of walkJsonl(sessionsRoot)) {
    const cwd = peekCwd(p);
    if (!cwd || normPath(cwd) !== norm) continue;
    let stat;
    try {
      stat = fs.statSync(p);
    } catch (e) {
      continue;
    }
    sessions.push({
      session_id: sidFromFilename(p),
      path: p,
      cwd,
      mtime: stat.mtimeMs,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function scanAllSessions(codexDir) {
  // 跨项目扫描：遍历 ~/.codex/sessions 下所有 .jsonl，不按 cwd 过滤。按 mtime 倒序。
  const sessionsRoot = path.join(codexDir, 'sessions');
  const sessions = [];
  for (const p of walkJsonl(sessionsRoot)) {
    const cwd = peekCwd(p);
    let stat;
    try {
      stat = fs.statSync(p);
    } catch (e) {
      continue;
    }
    sessions.push({
      session_id: sidFromFilename(p),
      path: p,
      cwd: cwd || '',
      mtime: stat.mtimeMs,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}


// ---------- session_index（标题来源） ----------

function loadSessionIndex(codexDir) {
  // ~/.codex/session_index.jsonl 每行 {"id","thread_name","updated_at"}。
  // thread_name 是 codex resume 实际展示/按名恢复 Session 用的标题字段。
  const idxPath = path.join(codexDir, 'session_index.jsonl');
  const map = new Map(); // session_id -> thread_name
  if (!isFileSafe(idxPath)) return map;
  let content;
  try {
    content = fs.readFileSync(idxPath, 'utf-8');
  } catch (e) {
    return map;
  }
  for (const line of content.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      const o = JSON.parse(l);
      if (o.id && o.thread_name) map.set(o.id, o.thread_name);
    } catch (e) { /* skip */ }
  }
  return map;
}


// ---------- 时间格式化 ----------

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
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

function outputToText(output) {
  // custom_tool_call_output.output 是 [{type,text}] 列表；
  // function_call_output.output 通常是 JSON 字符串。统一抽为纯文本。
  if (output == null) return '';
  if (typeof output === 'string') return output;
  if (Array.isArray(output)) {
    return output
      .map((b) => (b && typeof b === 'object') ? (b.text || '') : '')
      .filter(Boolean)
      .join('\n');
  }
  if (typeof output === 'object') return JSON.stringify(output);
  return String(output);
}

function extractExecCmd(input) {
  // exec 工具的 input 是 JS 源码串，形如：
  //   const r = await tools.exec_command({ cmd: "cat foo", workdir: "..." }); text(r.output);
  // 正则提取 cmd 字面量内容并反转义。
  if (typeof input !== 'string') return null;
  const m = input.match(/cmd\s*:\s*(['"`])((?:\\.|(?!\1)[\s\S])*?)\1/);
  if (!m) return null;
  return m[2]
    .replace(/\\"/g, '"')
    .replace(/\\'/g, "'")
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\\\/g, '\\');
}

function normalizeEvents(events) {
  // 把 codex rollout 事件拍平为有序的归一化条目列表。
  // 每行: {timestamp, type, payload}；顶层 type ∈ session_meta/event_msg/response_item/turn_context/world_state/...
  const items = []; // kind ∈ user_text/assistant_text/tool_use/tool_result
  const summaries = []; // compact 标记
  const changedFiles = []; // patch_apply_end 修改的文件
  let cwd = null, sessionId = null, cliVersion = null, model = null;

  for (const ev of events) {
    const top = ev.type;
    const p = ev.payload;
    if (!p || typeof p !== 'object') continue;
    const pt = p.type;
    const ts = ev.timestamp || '';

    // 元信息：cwd / session id / 版本 / 模型
    if (top === 'session_meta') {
      if (!sessionId && p.id) sessionId = p.id;
      if (!cwd && p.cwd) cwd = p.cwd;
      if (!cliVersion && p.cli_version) cliVersion = p.cli_version;
      continue;
    }
    if (top === 'turn_context') {
      if (!cwd && p.cwd) cwd = p.cwd;
      if (!model && p.model) model = p.model;
      continue;
    }

    // 事件消息
    if (top === 'event_msg') {
      if (pt === 'user_message') {
        const text = (p.message || '').trim();
        if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      } else if (pt === 'agent_message') {
        const text = (p.message || '').trim();
        if (text) items.push({ kind: 'assistant_text', timestamp: ts, text });
      } else if (pt === 'context_compacted') {
        summaries.push('（会话发生过上下文压缩 context_compacted）');
      } else if (pt === 'patch_apply_end') {
        const changes = p.changes;
        if (changes && typeof changes === 'object') {
          for (const f of Object.keys(changes)) changedFiles.push(f);
        }
        if (p.stdout && typeof p.stdout === 'string') {
          // patch_apply_end 的 stdout 形如 "Success. Updated the following files:\nM /path"
          const m = p.stdout.match(/^Success\. Updated the following files:\n([\s\S]*)/);
          if (m) {
            for (const line of m[1].split('\n')) {
              const f = line.replace(/^[AM]\s+/, '').trim();
              if (f) changedFiles.push(f);
            }
          }
        }
      }
      // token_count / task_started / task_complete / agent_reasoning / sub_agent_activity 等跳过
      continue;
    }

    // 响应项
    if (top === 'response_item') {
      if (pt === 'message') {
        // role=assistant 的输出文本作为助手消息（event_msg/agent_message 已覆盖多数，此处补齐）
        if (p.role === 'assistant') {
          const text = outputToText(p.content);
          if (text && text.trim()) items.push({ kind: 'assistant_text', timestamp: ts, text });
        }
        // role=developer（系统指令）、role=user（多为 environment_context 等注入）跳过
      } else if (pt === 'custom_tool_call') {
        // name=exec 的工具调用
        const cmd = extractExecCmd(p.input);
        items.push({
          kind: 'tool_use',
          timestamp: ts,
          name: p.name || 'exec',
          input: cmd ? { cmd } : { raw: p.input },
          call_id: p.call_id || '',
        });
      } else if (pt === 'function_call') {
        // spawn_agent / send_message / wait 等，arguments 是 JSON 字符串
        let input = {};
        if (typeof p.arguments === 'string' && p.arguments) {
          try { input = JSON.parse(p.arguments); } catch (e) { input = { raw: p.arguments }; }
        } else if (p.arguments && typeof p.arguments === 'object') {
          input = p.arguments;
        }
        items.push({
          kind: 'tool_use',
          timestamp: ts,
          name: p.name || 'function',
          input,
          call_id: p.call_id || '',
        });
      } else if (pt === 'custom_tool_call_output' || pt === 'function_call_output') {
        items.push({
          kind: 'tool_result',
          timestamp: ts,
          call_id: p.call_id || '',
          content: outputToText(p.output),
          is_error: false,
        });
      }
      // reasoning 等跳过
      continue;
    }

    // compacted / world_state / inter_agent_communication_metadata 等跳过
  }

  return { items, summaries, changedFiles, cwd, session_id: sessionId, cli_version: cliVersion, model };
}


// ---------- 标题解析 ----------

function resolveTitle(threadName, items, sessionId) {
  // 主来源：session_index 的 thread_name；兜底首条用户消息或 session id。
  if (threadName) return threadName;
  for (const it of items) {
    if (it.kind === 'user_text') {
      const trimmed = it.text.trim();
      const firstLine = trimmed ? trimmed.split('\n')[0] : '';
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

function toolBrief(name, inp) {
  // 单行紧凑描述一个工具调用。
  if (name === 'exec') {
    const cmd = (inp && inp.cmd) || (inp && inp.raw) || '';
    return `exec(${truncate(String(cmd).replace(/\n/g, ' '), 100)})`;
  }
  if (['spawn_agent', 'followup_task'].includes(name)) {
    return `${name}(${truncate(inp.task_name || inp.message || '', 80)})`;
  }
  if (name === 'send_message') return `send_message(${truncate(inp.message || inp.recipient || '', 80)})`;
  for (const v of Object.values(inp || {})) {
    if (typeof v === 'string' && v) return `${name}(${truncate(v, 80)})`;
  }
  return `${name}(...)`;
}


// ---------- 任务状态重建 ----------

const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b/i;
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

function buildState(items, changedFiles) {
  // 从归一化条目提取结构化任务状态。
  const commands = [], testResults = [];
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
      if (name === 'exec' && inp.cmd) {
        const cmd = String(inp.cmd).trim();
        lastCommand = cmd;
        if (cmd) commands.push(cmd);
      } else {
        lastCommand = '';
      }
    } else if (k === 'tool_result') {
      const content = it.content || '';
      const isTestCmd = lastToolName === 'exec' && TEST_CMD_RE.test(lastCommand);
      const looksTest = isTestCmd || (!!content && TEST_RESULT_RE.test(content.slice(0, 2000)));
      const isErr = /(\berror\b|\bfailed\b|\btraceback\b|\bexception\b)/i.test(content.slice(0, 2000)) &&
                    !/no error|0 failed/i.test(content.slice(0, 2000));
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
    files_edited: dedupe(changedFiles),
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

  lines.push('# Resume-Codex 会话接管摘要');
  lines.push('');
  lines.push('## 会话信息');
  lines.push(`- 标题: ${info.title}`);
  lines.push(`- 会话ID: ${info.session_id}`);
  lines.push(`- 项目: ${info.cwd || '(未知)'}`);
  if (info.cli_version) lines.push(`- Codex 版本: ${info.cli_version}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  lines.push(`- 时间范围: ${info.first_ts} ~ ${info.last_ts}`);
  lines.push(`- 消息条目数: ${norm.items.length}`);
  lines.push('');

  if (norm.summaries.length) {
    lines.push('## 历史摘要');
    for (const s of norm.summaries) lines.push(`- ${truncate(s, maxChars)}`);
    lines.push('');
  }

  lines.push('## 任务状态重建');
  lines.push('');
  lines.push('### 目标');
  lines.push(block(state.goal, maxChars) || '(未识别)');
  lines.push('');
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
      const firstLine = trimmed ? trimmed.split('\n')[0] : '';
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


// ---------- 会话扫描（轻量，用于 --list） ----------

function sessionMetaLite(p, indexMap) {
  // 轻量解析：仅取标题/时间范围/条目数。标题优先取 session_index 的 thread_name。
  const sid = sidFromFilename(p);
  const threadName = indexMap.get(sid) || null;
  const { events, err } = parseEvents(p);
  if (err) {
    return { session_id: sid, title: threadName || sid, first_ts: '', last_ts: '', count: 0 };
  }
  const norm = normalizeEvents(events);
  const items = norm.items;
  const timestamps = items.filter((it) => it.timestamp).map((it) => it.timestamp);
  return {
    session_id: sid,
    title: resolveTitle(threadName, items, sid),
    first_ts: timestamps.length ? fmtTime(timestamps[0]) : '',
    last_ts: timestamps.length ? fmtTime(timestamps[timestamps.length - 1]) : '',
    count: items.length,
  };
}

function printSessionList(sessions, indexMap, projectPath, codexDir, limit = 0) {
  const total = sessions.length;
  const shown = limit && limit > 0 ? sessions.slice(0, limit) : sessions;
  console.log(`当前项目: ${projectPath}`);
  console.log(`会话根: ${path.join(codexDir, 'sessions')}`);
  if (limit && limit > 0 && limit < total) {
    console.log(`找到 ${total} 个会话（仅显示最近 ${shown.length} 个）：\n`);
  } else {
    console.log(`找到 ${total} 个会话：\n`);
  }
  shown.forEach((s, i) => {
    const meta = sessionMetaLite(s.path, indexMap) || {};
    const mark = i === 0 ? '[最近]' : '      ';
    const title = meta.title || '(无标题)';
    const lastTs = meta.last_ts || '(无时间)';
    const count = meta.count || 0;
    console.log(`${mark} ${lastTs}  ${s.session_id.slice(0, 12)}  消息数:${String(count).padEnd(4)} 标题: ${title}`);
  });
}


// ---------- 主流程 ----------

function loadSession(jsonlPath, indexMap) {
  const { events, err } = parseEvents(jsonlPath);
  if (err) return { session: null, err };
  const norm = normalizeEvents(events);
  const items = norm.items;
  const sid = norm.session_id || sidFromFilename(jsonlPath);
  const threadName = indexMap.get(sid) || null;
  const timestamps = items.filter((it) => it.timestamp).map((it) => it.timestamp);
  const info = {
    session_id: sid,
    title: resolveTitle(threadName, items, sid),
    cwd: norm.cwd,
    cli_version: norm.cli_version || '',
    model: norm.model || '',
    first_ts: timestamps.length ? fmtTime(timestamps[0]) : '',
    last_ts: timestamps.length ? fmtTime(timestamps[timestamps.length - 1]) : '',
  };
  return { session: { info, normalized: norm, events }, err: null };
}

function pickSession(sessions, sessionArg) {
  if (!sessionArg) return sessions[0];
  for (const s of sessions) {
    if (s.session_id.startsWith(sessionArg) || s.session_id.includes(sessionArg)) return s;
  }
  return null;
}


// ---------- CLI ----------

function printHelp() {
  console.log(`用法: node resume_codex.js [选项]

读取 Codex CLI 本地会话记录（rollout JSONL + session_index），生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID 或前缀（支持跨项目全局查找）
  --project PATH      项目路径，默认当前工作目录
  --codex-dir DIR     Codex 配置目录，默认 ~/.codex 或 \$CODEX_HOME
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
    'codex-dir': null,
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
    else if (a === '--codex-dir') args['codex-dir'] = needValue(a, i++);
    else if (a.startsWith('--codex-dir=')) args['codex-dir'] = a.slice('--codex-dir='.length);
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

  const codexDir = args['codex-dir'] || getCodexDir();
  const projectPathArg = path.resolve(args.project || process.cwd());

  const indexMap = loadSessionIndex(codexDir);

  let target = null;
  let projectPath = projectPathArg;
  let sessions = [];

  // 若指定了 session，优先跨项目全局查找。
  if (args.session) {
    sessions = scanAllSessions(codexDir);
    target = pickSession(sessions, args.session);
    if (target && target.cwd) projectPath = target.cwd;
  }

  // 未指定 session 或全局未找到时，回退到基于项目路径查找。
  if (!target) {
    sessions = scanSessions(codexDir, projectPathArg);
    if (!sessions.length) {
      console.error(`错误：未找到项目 ${projectPathArg} 对应的 Codex 会话。`);
      console.error(`已查找：${path.join(codexDir, 'sessions')}`);
      process.exit(1);
    }
    target = pickSession(sessions, args.session);
    if (!target) {
      console.error(`错误：未匹配到会话 '${args.session}'。使用 --list 查看可用会话。`);
      process.exit(1);
    }
  }

  if (args.list) {
    printSessionList(sessions, indexMap, projectPath, codexDir, args.limit);
    return;
  }

  const { session, err } = loadSession(target.path, indexMap);
  if (err) {
    console.error(`错误：解析会话失败：${err}`);
    process.exit(1);
  }

  const state = buildState(session.normalized.items, session.normalized.changedFiles);

  let out;
  if (args.json) {
    out = JSON.stringify({
      info: session.info,
      state: {
        goal: state.goal,
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
