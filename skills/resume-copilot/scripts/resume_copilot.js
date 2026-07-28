#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * resume-copilot: 读取 GitHub Copilot CLI 本地会话记录（~/.copilot/session-state 下的
 * workspace.yaml + events.jsonl），生成结构化「接管摘要」。
 *
 * 零第三方依赖，任意 agent（Claude Code / Codex / Grok / Kimi 等）均可直接调用：
 *   node resume_copilot.js [--list|--latest|--session ID] [--project PATH] [--json] [--output FILE]
 *
 * 与同目录 resume_copilot.py 功能等价、输出可互换。用法见同目录 SKILL.md。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// 时间固定 UTC+8，格式 YYYY-MM-dd HH:mm:ss（与 resume-claude / resume-codex 一致）
const CST_OFFSET_MS = 8 * 3600 * 1000;

// Session ID（UUID）正则
const SID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

// ---------- 路径与项目定位 ----------

function getCopilotDir() {
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

// ---------- workspace.yaml 解析 ----------

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
    const text = fs.readFileSync(p, 'utf-8');
    return parseWorkspaceYaml(text);
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

function scanSessions(copilotDir, projectPath) {
  const norm = normPath(projectPath);
  const sessions = [];
  for (const s of listSessionDirs(copilotDir)) {
    const ws = loadWorkspace(s.dir);
    if (!ws) continue;
    const cwd = ws.cwd || '';
    if (!cwd || normPath(cwd) !== norm) continue;
    sessions.push({
      session_id: s.session_id,
      dir: s.dir,
      cwd,
      mtime: getMtime(s.dir),
      workspace: ws,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
}

function scanAllSessions(copilotDir) {
  const sessions = [];
  for (const s of listSessionDirs(copilotDir)) {
    const ws = loadWorkspace(s.dir);
    const cwd = ws && ws.cwd ? ws.cwd : '';
    sessions.push({
      session_id: s.session_id,
      dir: s.dir,
      cwd,
      mtime: getMtime(s.dir),
      workspace: ws,
    });
  }
  sessions.sort((a, b) => b.mtime - a.mtime);
  return sessions;
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


// ---------- events.jsonl 解析 ----------

function parseEvents(jsonlPath) {
  let content;
  try {
    content = fs.readFileSync(jsonlPath, 'utf-8');
  } catch (e) {
    return { events: [], err: e.message };
  }
  const events = [];
  for (const line of content.split(/\r?\n/)) {
    const l = line.trim();
    if (!l) continue;
    try {
      events.push(JSON.parse(l));
    } catch (e) { /* skip */ }
  }
  return { events, err: null };
}


// ---------- 事件归一化 ----------

function outputToText(output) {
  // 把工具结果统一为可展示的文本。
  if (output == null) return '';
  if (typeof output === 'string') return output;
  if (typeof output === 'object') {
    try {
      return JSON.stringify(output, null, 2);
    } catch (e) {
      return String(output);
    }
  }
  return String(output);
}

function extractShellCmd(arguments_) {
  // bash 工具的 arguments.command 直接就是命令字符串。
  if (typeof arguments_ === 'string') return arguments_;
  if (arguments_ && typeof arguments_ === 'object') {
    if (typeof arguments_.command === 'string') return arguments_.command;
    if (typeof arguments_.cmd === 'string') return arguments_.cmd;
  }
  return null;
}

function normalizeEvents(events) {
  const items = [];     // kind ∈ user_text/assistant_text/tool_use/tool_result
  const summaries = []; // session 级摘要/提示
  let cwd = null;
  let sessionId = null;
  let copilotVersion = null;
  let model = null;
  let mode = null;

  const pendingToolResults = new Map(); // call_id -> content

  for (const ev of events) {
    const type = ev.type;
    const data = ev.data || {};
    const ts = ev.timestamp || '';

    if (type === 'session.start') {
      if (!sessionId && data.sessionId) sessionId = data.sessionId;
      if (!copilotVersion && data.copilotVersion) copilotVersion = data.copilotVersion;
      if (!cwd && data.context && data.context.cwd) cwd = data.context.cwd;
      continue;
    }
    if (type === 'session.model_change') {
      if (data.newModel) model = data.newModel;
      if (data.previousModel && !model) model = data.previousModel;
      continue;
    }
    if (type === 'session.mode_changed') {
      if (data.newMode) mode = data.newMode;
      continue;
    }
    if (type === 'session.info') {
      if (data.message) summaries.push(data.message);
      continue;
    }
    if (type === 'session.error') {
      if (data.message) summaries.push(`[会话错误] ${data.message}`);
      continue;
    }

    if (type === 'user.message') {
      const text = String(data.content || '').trim();
      if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      continue;
    }

    if (type === 'assistant.message') {
      const content = String(data.content || '').trim();
      if (content) items.push({ kind: 'assistant_text', timestamp: ts, text: content });
      if (model && data.model) model = data.model;
      if (Array.isArray(data.toolRequests)) {
        for (const tr of data.toolRequests) {
          const name = tr.name || '';
          const args = tr.arguments || {};
          items.push({
            kind: 'tool_use',
            timestamp: ts,
            name,
            input: args,
            call_id: tr.toolCallId || '',
            intention: tr.intentionSummary || '',
          });
        }
      }
      continue;
    }

    if (type === 'tool.execution_start') {
      // 与 assistant.message 中的 toolRequests 重复，跳过。
      continue;
    }

    if (type === 'tool.execution_complete') {
      const content = outputToText(data.result);
      items.push({
        kind: 'tool_result',
        timestamp: ts,
        call_id: data.toolCallId || '',
        content,
        is_error: data.success === false,
      });
      continue;
    }

    if (type === 'permission.requested') {
      const pr = data.permissionRequest || {};
      if (pr.kind === 'shell' && pr.fullCommandText) {
        items.push({
          kind: 'tool_use',
          timestamp: ts,
          name: 'bash',
          input: { command: pr.fullCommandText },
          call_id: data.requestId || '',
          intention: pr.intention || '',
        });
      }
      continue;
    }
    if (type === 'permission.completed') {
      pendingToolResults.set(data.toolCallId || data.requestId, {
        kind: 'tool_result',
        timestamp: ts,
        call_id: data.toolCallId || data.requestId,
        content: outputToText(data.result),
        is_error: false,
      });
      continue;
    }

    // 其他事件忽略。
  }

  // 把 permission.completed 结果合并进 items（如果有尚未写入的）。
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === 'tool_use' && it.call_id && pendingToolResults.has(it.call_id)) {
      items.splice(i + 1, 0, pendingToolResults.get(it.call_id));
      pendingToolResults.delete(it.call_id);
      i++;
    }
  }
  // 剩余 permission 结果追加到末尾。
  for (const it of pendingToolResults.values()) {
    items.push(it);
  }

  return { items, summaries, cwd, session_id: sessionId, copilot_version: copilotVersion, model, mode };
}


// ---------- 标题解析 ----------

function resolveTitle(workspace, items, sessionId) {
  if (workspace && workspace.name) return workspace.name;
  for (const it of items) {
    if (it.kind === 'user_text') {
      const firstLine = it.text.split('\n')[0].trim();
      if (firstLine) return firstLine.length > 60 ? firstLine.slice(0, 60) + '…' : firstLine;
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
  if (name === 'bash' || name === 'shell') {
    const cmd = extractShellCmd(inp) || (inp && inp.raw) || '';
    return `bash(${truncate(String(cmd).replace(/\n/g, ' '), 100)})`;
  }
  for (const v of Object.values(inp || {})) {
    if (typeof v === 'string' && v) return `${name}(${truncate(v, 80)})`;
  }
  return `${name}(...)`;
}


// ---------- 任务状态重建 ----------

const TEST_CMD_RE = /\b(pytest|unittest|jest|vitest|mocha|npm\s+test|yarn\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|deno\s+test)\b/i;
const TEST_RESULT_RE = /(✓|✗|\bPASS\b|\bFAIL\b|\b\d+\s*(passed|failed|tests?)\b|\b(passed|failed)\s*\d+\b|\b(failures?|errors?)\s*[:=]\s*\d)/i;
const FILE_EDIT_RE = /\b(edit|create|apply_patch)\b/i;

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

function isFilePathLike(s) {
  return typeof s === 'string' && (
    s.startsWith('/') ||
    s.startsWith('./') ||
    s.startsWith('../') ||
    /^[a-zA-Z]:[\\\/]/.test(s)
  );
}

function collectFilesFromToolInput(name, input) {
  const files = [];
  if (!input || typeof input !== 'object') return files;
  if (name === 'view' || name === 'read') {
    if (typeof input.path === 'string') files.push(input.path);
    if (Array.isArray(input.paths)) {
      for (const p of input.paths) if (typeof p === 'string') files.push(p);
    }
  } else if (name === 'edit') {
    if (typeof input.path === 'string') files.push(input.path);
  } else if (name === 'create') {
    if (typeof input.path === 'string') files.push(input.path);
  } else if (name === 'apply_patch') {
    if (typeof input.path === 'string') files.push(input.path);
    if (typeof input.patch === 'string') {
      // 从 patch 头提取文件路径（简单正则）。
      const m = input.patch.match(/---\s+(.+)/);
      if (m) files.push(m[1].trim().replace(/^a\//, ''));
    }
  }
  return files;
}

function buildState(items) {
  const commands = [];
  const testResults = [];
  const filesRead = [];
  const filesEdited = [];
  let firstUser = '';
  let lastUser = '';
  let lastAssistant = '';
  let lastToolName = '';
  let lastCommand = '';

  for (const it of items) {
    const k = it.kind;
    if (k === 'user_text') {
      if (!firstUser) firstUser = it.text;
      lastUser = it.text;
    } else if (k === 'assistant_text') {
      lastAssistant = it.text;
    } else if (k === 'tool_use') {
      const name = it.name;
      const input = it.input || {};
      lastToolName = name;
      if (name === 'bash' || name === 'shell') {
        const cmd = extractShellCmd(input);
        if (cmd) {
          lastCommand = cmd;
          commands.push(cmd);
        }
      } else {
        lastCommand = '';
        if (FILE_EDIT_RE.test(name)) {
          filesEdited.push(...collectFilesFromToolInput(name, input));
        } else if (['view', 'read'].includes(name)) {
          filesRead.push(...collectFilesFromToolInput(name, input));
        }
      }
    } else if (k === 'tool_result') {
      const content = it.content || '';
      const isTestCmd = lastToolName === 'bash' && TEST_CMD_RE.test(lastCommand);
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
    files_read: dedupe(filesRead),
    files_edited: dedupe(filesEdited),
    commands: dedupe(commands),
    test_results: testResults,
    last_user: lastUser,
    last_assistant: lastAssistant,
  };
}


// ---------- Markdown 渲染 ----------

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
    const extra = it.intention ? ` # ${it.intention}` : '';
    return [`### [工具调用] ${it.name}${extra} ${ts}`, '```', truncate(pyJsonStringify(it.input), maxChars), '```'];
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

  lines.push('# Resume-Copilot 会话接管摘要');
  lines.push('');
  lines.push('## 会话信息');
  lines.push(`- 标题: ${info.title}`);
  lines.push(`- 会话ID: ${info.session_id}`);
  lines.push(`- 项目: ${info.cwd || '(未知)'}`);
  if (info.copilot_version) lines.push(`- Copilot CLI 版本: ${info.copilot_version}`);
  if (info.model) lines.push(`- 模型: ${info.model}`);
  if (info.mode) lines.push(`- 模式: ${info.mode}`);
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


// ---------- 轻量列表 ----------

function sessionMetaLite(s) {
  const sid = s.session_id;
  const ws = s.workspace || {};
  const eventsPath = path.join(s.dir, 'events.jsonl');
  const { events, err } = parseEvents(eventsPath);
  if (err) {
    return { session_id: sid, title: ws.name || sid, first_ts: '', last_ts: '', count: 0 };
  }
  const norm = normalizeEvents(events);
  const items = norm.items;
  const timestamps = items.filter((it) => it.timestamp).map((it) => it.timestamp);
  return {
    session_id: sid,
    title: resolveTitle(ws, items, sid),
    first_ts: timestamps.length ? fmtTime(timestamps[0]) : '',
    last_ts: timestamps.length ? fmtTime(timestamps[timestamps.length - 1]) : '',
    count: items.length,
  };
}

function printSessionList(sessions, projectPath, copilotDir, limit = 0) {
  const total = sessions.length;
  const shown = limit && limit > 0 ? sessions.slice(0, limit) : sessions;
  console.log(`当前项目: ${projectPath}`);
  console.log(`会话根: ${path.join(copilotDir, 'session-state')}`);
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
    console.log(`${mark} ${lastTs}  ${s.session_id.slice(0, 12)}  消息数:${String(count).padEnd(4)} 标题: ${title}`);
  });
}


// ---------- 主流程 ----------

function loadSession(sessionDir, workspace) {
  const eventsPath = path.join(sessionDir, 'events.jsonl');
  const { events, err } = parseEvents(eventsPath);
  if (err) return { session: null, err };
  const norm = normalizeEvents(events);
  const items = norm.items;
  const sid = workspace.id || norm.session_id || path.basename(sessionDir);
  const timestamps = items.filter((it) => it.timestamp).map((it) => it.timestamp);
  const info = {
    session_id: sid,
    title: resolveTitle(workspace, items, sid),
    cwd: workspace.cwd || norm.cwd || '',
    git_root: workspace.git_root || '',
    branch: workspace.branch || '',
    copilot_version: norm.copilot_version || '',
    model: norm.model || '',
    mode: norm.mode || '',
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
  console.log(`用法: node resume_copilot.js [选项]

读取 GitHub Copilot CLI 本地会话记录（~/.copilot/session-state 下的 workspace.yaml + events.jsonl），
生成结构化接管摘要。

选项:
  --list              仅列出当前项目的会话
  --latest            取最近一个会话（默认行为）
  --session ID        指定会话 ID 或前缀（支持跨项目全局查找）
  --project PATH      项目路径，默认当前工作目录
  --copilot-dir DIR   Copilot CLI 配置目录，默认 ~/.copilot 或 \$COPILOT_HOME
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
    'copilot-dir': null,
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
    else if (a === '--copilot-dir') args['copilot-dir'] = needValue(a, i++);
    else if (a.startsWith('--copilot-dir=')) args['copilot-dir'] = a.slice('--copilot-dir='.length);
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

  const copilotDir = args['copilot-dir'] || getCopilotDir();
  const projectPathArg = path.resolve(args.project || process.cwd());

  let target = null;
  let projectPath = projectPathArg;
  let sessions = [];

  // 若指定了 session，优先跨项目全局查找。
  if (args.session) {
    sessions = scanAllSessions(copilotDir);
    target = pickSession(sessions, args.session);
    if (target && target.cwd) projectPath = target.cwd;
  }

  // 未指定 session 或全局未找到时，回退到基于项目路径查找。
  if (!target) {
    sessions = scanSessions(copilotDir, projectPathArg);
    if (!sessions.length) {
      console.error(`错误：未找到项目 ${projectPathArg} 对应的 Copilot CLI 会话。`);
      console.error(`已查找：${path.join(copilotDir, 'session-state')}`);
      process.exit(1);
    }
    target = pickSession(sessions, args.session);
    if (!target) {
      console.error(`错误：未匹配到会话 '${args.session}'。使用 --list 查看可用会话。`);
      process.exit(1);
    }
  }

  if (args.list) {
    printSessionList(sessions, projectPath, copilotDir, args.limit);
    return;
  }

  const ws = loadWorkspace(target.dir);
  const { session, err } = loadSession(target.dir, ws || {});
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
