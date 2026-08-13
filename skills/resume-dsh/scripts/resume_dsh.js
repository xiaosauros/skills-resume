#!/usr/bin/env node
'use strict';

/**
 * resume-dsh: 读取 DeepSeek Harness 的 ~/.dsh 会话日志并生成接管摘要。
 *
 * DSH 的压缩日志由多个独立 Zstandard frame 追加组成。本脚本仅使用
 * Node.js 标准库，逐帧解压 session.jsonl.zstd；也兼容 session.jsonl。
 */

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const zlib = require('node:zlib');

const ZSTD_MAGIC = 0xfd2fb528;
const CST_OFFSET_MS = 8 * 60 * 60 * 1000;

function die(message) {
  console.error(`错误：${message}`);
  process.exit(1);
}

function isFileSafe(file) {
  try { return fs.statSync(file).isFile(); } catch { return false; }
}

function isDirSafe(dir) {
  try { return fs.statSync(dir).isDirectory(); } catch { return false; }
}

function normPath(value) {
  return String(value || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

function fmtTime(value) {
  if (value === undefined || value === null || value === '') return '';
  let millis;
  if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value))) {
    millis = Number(value);
  } else {
    millis = Date.parse(String(value));
  }
  if (!Number.isFinite(millis)) return String(value);
  const d = new Date(millis + CST_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

function truncate(value, max) {
  const text = String(value ?? '');
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function scanZstdFrames(buffer, maxFrames = Number.POSITIVE_INFINITY) {
  const frames = [];
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) return { frames, tornStart: start };
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) {
      throw new Error(`Zstandard 日志在字节 ${offset} 处的 frame magic 无效`);
    }
    offset += 4;
    if (offset === buffer.length) return { frames, tornStart: start };

    const descriptor = buffer.readUInt8(offset++);
    if ((descriptor & 0x18) !== 0) {
      throw new Error(`Zstandard 日志在字节 ${offset - 1} 处使用了保留的 frame header 位`);
    }
    const contentSizeFlag = descriptor >>> 6;
    const singleSegment = (descriptor & 0x20) !== 0;
    const checksum = (descriptor & 0x04) !== 0;
    const dictionaryFlag = descriptor & 0x03;
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag;
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : (1 << contentSizeFlag);
    const remainingHeader = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes;
    if (buffer.length - offset < remainingHeader) return { frames, tornStart: start };
    offset += remainingHeader;

    for (;;) {
      if (buffer.length - offset < 3) return { frames, tornStart: start };
      const blockHeader = buffer.readUIntLE(offset, 3);
      offset += 3;
      const lastBlock = (blockHeader & 1) !== 0;
      const blockType = (blockHeader >>> 1) & 3;
      const blockSize = blockHeader >>> 3;
      if (blockType === 3) throw new Error(`Zstandard 日志在字节 ${offset - 3} 处使用了保留 block 类型`);
      const payloadBytes = blockType === 1 ? 1 : blockSize;
      if (buffer.length - offset < payloadBytes) return { frames, tornStart: start };
      offset += payloadBytes;
      if (lastBlock) break;
    }
    if (checksum) {
      if (buffer.length - offset < 4) return { frames, tornStart: start };
      offset += 4;
    }
    frames.push({ start, end: offset });
    if (frames.length === maxFrames) return { frames };
  }
  return { frames };
}

function parseJsonl(text) {
  const rows = [];
  const rawLines = text.split(/\r?\n/);
  const hasTerminatingNewline = /(?:\r?\n)$/.test(text);
  for (let index = 0; index < rawLines.length; index++) {
    const raw = rawLines[index];
    const line = raw.trim();
    if (!line) continue;
    try {
      rows.push(JSON.parse(line));
    } catch (error) {
      if (index === rawLines.length - 1 && !hasTerminatingNewline) break;
      throw new Error(`会话日志第 ${index + 1} 行不是有效 JSON：${error.message}`);
    }
  }
  return rows;
}

function readArtifact(file, firstFrameOnly = false) {
  if (file.endsWith('.zstd')) {
    if (typeof zlib.zstdDecompressSync !== 'function') {
      throw new Error('当前 Node.js 不支持 zlib.zstdDecompressSync；请使用 DSH 自带的较新 Node.js 运行时');
    }
    const source = fs.readFileSync(file);
    const scanned = scanZstdFrames(source, firstFrameOnly ? 1 : Number.POSITIVE_INFINITY);
    if (!scanned.frames.length) throw new Error('Zstandard 会话日志没有完整 frame');
    const chunks = scanned.frames.map(({ start, end }) => zlib.zstdDecompressSync(source.subarray(start, end)));
    return {
      rows: parseJsonl(Buffer.concat(chunks).toString('utf8')),
      tornTail: scanned.tornStart !== undefined,
      frameCount: scanned.frames.length,
    };
  }
  return { rows: parseJsonl(fs.readFileSync(file, 'utf8')), tornTail: false, frameCount: 0 };
}

function findArtifact(sessionDir) {
  for (const name of ['session.jsonl.zstd', 'session.jsonl']) {
    const file = path.join(sessionDir, name);
    if (isFileSafe(file)) return file;
  }
  return null;
}

function loadProjectionCache(dshDir) {
  const file = path.join(dshDir, 'storages', 'session_projcache.json');
  if (!isFileSafe(file)) return {};
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && parsed.tables && parsed.tables.sessions ? parsed.tables.sessions : {};
  } catch {
    return {};
  }
}

function projectionValue(entry, name) {
  const row = entry && entry.rows && entry.rows[name];
  return row && Object.prototype.hasOwnProperty.call(row, 'val') ? row.val : null;
}

function readHeader(file, enforceVersion = true) {
  const { rows } = readArtifact(file, true);
  const header = rows[0];
  if (!header || header.type !== 'session' || typeof header.id !== 'string') {
    throw new Error('首条记录不是有效的 DSH session header');
  }
  if (enforceVersion && header.version !== 0) {
    throw new Error(`不支持 DSH session format version ${String(header.version)}（当前解析器支持 0）`);
  }
  return header;
}

function scanAllSessions(dshDir) {
  const root = path.join(dshDir, 'sessions');
  if (!isDirSafe(root)) return [];
  const cache = loadProjectionCache(dshDir);
  const sessions = [];
  for (const group of fs.readdirSync(root, { withFileTypes: true })) {
    if (!group.isDirectory()) continue;
    const groupDir = path.join(root, group.name);
    for (const item of fs.readdirSync(groupDir, { withFileTypes: true })) {
      if (!item.isDirectory()) continue;
      const sessionDir = path.join(groupDir, item.name);
      const artifact = findArtifact(sessionDir);
      if (!artifact) continue;
      try {
        const header = readHeader(artifact, false);
        const projected = cache[header.id] || cache[item.name] || {};
        const title = projectionValue(projected, 'title');
        const listMeta = projectionValue(projected, 'sessionListMetadata') || {};
        const stats = projectionValue(projected, 'sessionStats') || {};
        const fileStat = fs.statSync(artifact);
        const activityAt = Number(listMeta.lastPromptAt) || fileStat.mtimeMs || Number(header.createdAt) || 0;
        sessions.push({
          sessionId: header.id,
          sessionDir,
          artifact,
          header,
          projection: projected,
          cwd: header.cwd || '',
          title: typeof title === 'string' ? title : '',
          blank: listMeta.blank === true,
          count: Number(stats.turns) || 0,
          activityAt,
          mtime: fileStat.mtimeMs,
          scanError: header.version === 0 ? null : `不支持 DSH session format version ${String(header.version)}（当前解析器支持 0）`,
        });
      } catch (error) {
        sessions.push({
          sessionId: item.name,
          sessionDir,
          artifact,
          header: {},
          projection: cache[item.name] || {},
          cwd: '',
          title: '',
          blank: false,
          count: 0,
          activityAt: 0,
          mtime: 0,
          scanError: error.message,
        });
      }
    }
  }
  sessions.sort((a, b) => (b.activityAt - a.activityAt) || (b.mtime - a.mtime));
  return sessions;
}

function textOf(value, includeReasoning = false) {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((item) => textOf(item, includeReasoning)).filter(Boolean).join('\n');
  if (typeof value !== 'object') return String(value);
  if (!includeReasoning && ['reasoning', 'thinking', 'reasoning-chunk'].includes(value.type)) return '';
  if (typeof value.text === 'string') return value.text;
  if (value.content !== undefined) return textOf(value.content, includeReasoning);
  if (value.output !== undefined) return textOf(value.output, includeReasoning);
  return '';
}

function parseArguments(value) {
  if (value === undefined || value === null || value === '') return {};
  if (typeof value === 'object') return value;
  try { return JSON.parse(String(value)); } catch { return { _raw: String(value) }; }
}

function normalizeInput(name, input) {
  const source = input && typeof input === 'object' ? input : {};
  const pick = (...keys) => {
    for (const key of keys) if (source[key] !== undefined && source[key] !== '') return source[key];
    return '';
  };
  if (name === 'run_code') return { description: pick('description'), code: pick('code') };
  if (['pwsh', 'bash', 'shell', 'shell_command', 'exec'].includes(name)) {
    return { command: pick('command', 'cmd', 'script'), workdir: pick('workdir', 'cwd') };
  }
  if (['read', 'read_file', 'view_image'].includes(name)) return { path: pick('file_path', 'path') };
  if (['edit', 'write', 'write_file', 'str_replace'].includes(name)) return { path: pick('file_path', 'path') };
  if (['grep', 'glob', 'search'].includes(name)) return { pattern: pick('pattern', 'query'), path: pick('path', 'directory') };
  if (name.includes('web')) return { query: pick('query', 'q'), url: pick('url') };
  return source;
}

function isRealUserMessage(data) {
  const source = data && data.source;
  return !source || !source.kind || source.kind === 'user';
}

function currentSurfaceSeqs(rows) {
  const surface = [];
  const surfaceTypes = new Set(['user/message', 'assistant/message', 'tool/result']);
  for (const event of rows) {
    if (!event || !surfaceTypes.has(event.type) || !Number.isInteger(event.seq)) continue;
    const op = event.surfaceOp;
    if (!op || op === 'append') {
      surface.push(event.seq);
      continue;
    }
    if (typeof op !== 'object' || op.op !== 'replace') {
      throw new Error(`会话事件 seq ${event.seq} 使用了未知 surfaceOp`);
    }
    const start = surface.indexOf(op.start);
    const end = surface.indexOf(op.end);
    if (start < 0 || end < start) {
      throw new Error(`会话事件 seq ${event.seq} 的 surface replace 范围无效`);
    }
    surface.splice(start, end - start + 1, event.seq);
  }
  return new Set(surface);
}

function toolResultCallId(data) {
  const message = data && data.message || {};
  if (message.source && message.source.callId) return message.source.callId;
  if (Array.isArray(message.content)) {
    for (const block of message.content) if (block && block.toolCallId) return block.toolCallId;
  }
  return '';
}

function normalizeEvents(rows) {
  const items = [];
  const meta = { title: '', model: '', provider: '', todos: null, turnEnd: null };
  const surfaceSeqs = currentSurfaceSeqs(rows);
  const allResultCallIds = new Set();
  const currentResultCallIds = new Set();
  for (const event of rows) {
    if (!event || event.type !== 'tool/result') continue;
    const callId = toolResultCallId(event.data);
    if (!callId) continue;
    allResultCallIds.add(callId);
    if (surfaceSeqs.has(event.seq)) currentResultCallIds.add(callId);
  }
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
    const event = rows[rowIndex];
    if (!event || typeof event !== 'object') continue;
    const type = event.type || '';
    const data = event.data || {};
    const timestamp = event.time ?? '';

    if (type === 'session/title' && typeof data.title === 'string') meta.title = data.title;
    if (type === 'todo/write' && Array.isArray(data.todos)) meta.todos = data.todos;
    if (type === 'turn/end') meta.turnEnd = data.reason || null;
    if (type === 'compaction/summary') {
      const replacement = rows[rowIndex + 1];
      const isCurrent = replacement && replacement.type === 'user/message'
        && replacement.surfaceOp && replacement.surfaceOp.op === 'replace'
        && surfaceSeqs.has(replacement.seq);
      const text = isCurrent ? textOf(data.summary).trim() : '';
      if (text) items.push({ kind: 'compact_summary', timestamp, seq: event.seq, text });
      continue;
    }
    if (type === 'request/header') {
      const config = data.header && data.header.config || {};
      if (config.model) meta.model = config.model;
      if (config.provider) meta.provider = config.provider;
      continue;
    }

    if (type === 'user/message') {
      if (!surfaceSeqs.has(event.seq)) continue;
      if (!isRealUserMessage(data)) continue;
      const text = textOf(data.content).trim();
      if (text) items.push({ kind: 'user_text', timestamp, seq: event.seq, text });
    } else if (type === 'assistant/message') {
      if (!surfaceSeqs.has(event.seq)) continue;
      const message = data.message || {};
      const text = textOf(message.content).trim();
      if (text) items.push({ kind: 'assistant_text', timestamp, seq: event.seq, text });
    } else if (type === 'tool/call') {
      const name = data.name || '';
      const callId = data.callId || '';
      if (callId && allResultCallIds.has(callId) && !currentResultCallIds.has(callId)) continue;
      items.push({
        kind: 'tool_use', timestamp, seq: event.seq, name,
        call_id: callId, input: normalizeInput(name, parseArguments(data.arguments)),
      });
    } else if (type === 'tool/result') {
      if (!surfaceSeqs.has(event.seq)) continue;
      const message = data.message || {};
      const callId = toolResultCallId(data);
      let isError = Boolean(data.error);
      if (Array.isArray(message.content)) {
        for (const block of message.content) {
          if (block && block.isError) isError = true;
        }
      }
      items.push({
        kind: 'tool_result', timestamp, seq: event.seq, call_id: callId,
        content: textOf(message.content), is_error: isError,
      });
    }
  }
  return { items, meta };
}

function resolveTitle(session, normalized) {
  const projected = projectionValue(session.projection, 'title');
  if (typeof projected === 'string' && projected.trim()) return projected.trim();
  if (normalized.meta.title) return normalized.meta.title;
  for (const item of normalized.items) {
    if (item.kind === 'user_text') {
      const line = item.text.trim().split(/\r?\n/)[0];
      return truncate(line, 60) || session.sessionId;
    }
  }
  return session.sessionId;
}

function decodeJsString(raw) {
  try { return JSON.parse(`"${raw}"`); } catch { return raw.replace(/\\\\/g, '\\').replace(/\\"/g, '"'); }
}

function stringsAfterTool(code, tools, keys, maxDistance = 1600) {
  const toolPart = tools.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const keyPart = keys.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const re = new RegExp(`tools\\.(?:${toolPart})\\s*\\([\\s\\S]{0,${maxDistance}}?\\b(?:${keyPart})\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`, 'g');
  const values = [];
  let match;
  while ((match = re.exec(code)) !== null) values.push(decodeJsString(match[1]));
  return values;
}

function toolBrief(item) {
  const input = item.input || {};
  if (item.name === 'run_code') return `${item.name}: ${truncate(input.description || input.code || '', 180)}`;
  if (input.command) return `${item.name}: ${truncate(input.command, 180)}`;
  if (input.path) return `${item.name}: ${truncate(input.path, 180)}`;
  if (input.query) return `${item.name}: ${truncate(input.query, 180)}`;
  return `${item.name}: ${truncate(JSON.stringify(input), 180)}`;
}

function buildState(items, projection) {
  const state = {
    goal: '', files_read: [], files_edited: [], commands: [], test_results: [],
    last_user: '', last_assistant: '', history_summary: '',
    todos: projectionValue(projection, 'todos'),
  };
  const calls = new Map();
  for (const item of items) {
    if (item.kind === 'user_text') {
      if (!state.goal) state.goal = item.text;
      state.last_user = item.text;
    } else if (item.kind === 'assistant_text') {
      state.last_assistant = item.text;
    } else if (item.kind === 'compact_summary') {
      state.history_summary = item.text;
      if (!state.goal) state.goal = item.text;
      if (/(?:exit\s*(?:code)?\s*[:=]?\s*[0-9-]+|error|failed|passed|success|成功|失败|通过|finished)/i.test(item.text)) {
        state.test_results.push(truncate(item.text, 800));
      }
    } else if (item.kind === 'tool_use') {
      if (item.call_id) calls.set(item.call_id, item);
      const input = item.input || {};
      if (item.name === 'run_code') {
        const code = String(input.code || '');
        state.files_read.push(...stringsAfterTool(code, ['read', 'read_file', 'view_image', 'grep', 'glob'], ['file_path', 'path']));
        state.files_edited.push(...stringsAfterTool(code, ['edit', 'write', 'write_file', 'str_replace', 'apply_patch'], ['file_path', 'path']));
        state.commands.push(...stringsAfterTool(code, ['pwsh', 'bash', 'shell', 'shell_command', 'exec'], ['command'], 3000));
      } else if (['read', 'read_file', 'view_image', 'grep', 'glob'].includes(item.name) && input.path) {
        state.files_read.push(String(input.path));
      } else if (['edit', 'write', 'write_file', 'str_replace', 'apply_patch'].includes(item.name) && input.path) {
        state.files_edited.push(String(input.path));
      } else if (input.command) {
        state.commands.push(String(input.command));
      }
    } else if (item.kind === 'tool_result') {
      const content = String(item.content || '').trim();
      if (!content) continue;
      const call = calls.get(item.call_id);
      const directCommand = call && call.input && call.input.command;
      const code = call && call.name === 'run_code' ? String(call.input && call.input.code || '') : '';
      const verificationCode = /tools\.(?:pwsh|bash|shell|shell_command|exec|job_output)\s*\(|\b(?:test|check|build|lint|pytest|cargo|tsc)\b/i.test(code);
      const resultSignal = /(?:exit\s*(?:code)?\s*[:=]?\s*[0-9-]+|error|failed|passed|success|成功|失败|通过|finished)/i.test(content);
      if (item.is_error || directCommand || verificationCode || resultSignal) {
        state.test_results.push(truncate(content, 800));
      }
    }
  }
  const projectedGoal = projectionValue(projection, 'goal');
  if (projectedGoal) {
    if (typeof projectedGoal === 'string') state.goal = projectedGoal;
    else if (projectedGoal.objective) state.goal = projectedGoal.objective;
  }
  state.files_read = unique(state.files_read);
  state.files_edited = unique(state.files_edited);
  state.commands = unique(state.commands);
  state.test_results = unique(state.test_results).slice(-12);
  return state;
}

function renderItem(item, maxChars) {
  const when = item.timestamp === '' ? '' : ` (${fmtTime(item.timestamp)})`;
  if (item.kind === 'user_text') return `- 用户${when}: ${truncate(item.text, maxChars)}`;
  if (item.kind === 'assistant_text') return `- 助手${when}: ${truncate(item.text, maxChars)}`;
  if (item.kind === 'compact_summary') return `- 历史压缩摘要${when}: ${truncate(item.text, maxChars)}`;
  if (item.kind === 'tool_use') return `- 工具调用${when}: ${toolBrief(item)}`;
  const status = item.is_error ? '错误' : '结果';
  return `- 工具${status}${when}: ${truncate(item.content || '(空)', maxChars)}`;
}

function renderSummary(session, normalized, state, recentN, maxChars) {
  const header = session.header || {};
  const items = normalized.items;
  const title = resolveTitle(session, normalized);
  const eventTimes = session.rows.map((row) => Number(row && row.time)).filter(Number.isFinite);
  const firstTs = Number(header.createdAt) || eventTimes[0] || 0;
  const lastTs = eventTimes.length ? eventTimes[eventTimes.length - 1] : firstTs;
  const model = [normalized.meta.provider, normalized.meta.model].filter(Boolean).join('/') || '(未知)';
  const todos = normalized.meta.todos || state.todos;
  const recent = items.slice(-recentN);
  const olderTools = items.slice(0, Math.max(0, items.length - recent.length)).filter((item) => item.kind === 'tool_use').slice(-60);
  const lines = [
    '# DeepSeek Harness 会话接管摘要', '',
    '## 会话信息', '',
    `- 标题: ${title}`,
    `- 会话 ID: ${session.sessionId}`,
    `- 项目路径: ${header.cwd || session.cwd || '(未知)'}`,
    `- Agent preset: ${header.agentPreset || '(未知)'}`,
    `- 模型: ${model}`,
    `- 父会话: ${header.parentSession || '(无)'}`,
    `- 时间范围: ${fmtTime(firstTs)} ~ ${fmtTime(lastTs)}`,
    `- 日志: ${path.basename(session.artifact)}，${session.frameCount || '明文'} frame，规范化活动 ${items.length} 条${session.tornTail ? '（检测到未完成尾帧，已安全忽略）' : ''}`,
    '', '## 任务状态重建', '',
    '### 目标', '', truncate(state.goal || '(未能从会话中识别)', maxChars), '',
  ];

  if (state.history_summary) {
    lines.push('### 历史压缩摘要', '', truncate(state.history_summary, maxChars), '');
  }

  const addList = (heading, values, empty = '(无)') => {
    lines.push(`### ${heading}`, '');
    if (!values || !values.length) lines.push(`- ${empty}`);
    else for (const value of values) lines.push(`- ${truncate(value, maxChars)}`);
    lines.push('');
  };
  addList('已调查文件/路径', state.files_read);
  addList('已修改文件/路径', state.files_edited);
  addList('执行过的命令', state.commands);
  addList('测试、命令结果与错误线索', state.test_results);

  lines.push('### TODO / 计划', '');
  if (!Array.isArray(todos) || !todos.length) lines.push('- (会话未留下 TODO)');
  else for (const todo of todos) lines.push(`- [${todo.status === 'completed' ? 'x' : ' '}] ${todo.content || ''} (${todo.status || 'unknown'})`);
  lines.push('', '### 最后状态', '', `- 最近用户消息: ${truncate(state.last_user || '(无)', maxChars)}`, `- 最近助手消息: ${truncate(state.last_assistant || '(无)', maxChars)}`, '');

  lines.push(`## 近期活动（最后 ${recent.length} 条）`, '');
  if (!recent.length) lines.push('- (无可显示活动)');
  else for (const item of recent) lines.push(renderItem(item, maxChars));
  lines.push('');

  lines.push('## 更早工具活动（紧凑）', '');
  if (!olderTools.length) lines.push('- (无)');
  else for (const item of olderTools) lines.push(`- ${toolBrief(item)}`);
  lines.push('', '## 接管建议', '',
    '- 先读取当前 Git 状态与摘要提到的关键文件，确认磁盘现场是否晚于日志。',
    '- 优先处理未完成 TODO、最近错误和最后一条真实用户要求。',
    '- 不要假设原 DSH 的运行中进程仍存在；需要时重新执行验证命令。',
    '- 若日志格式版本与当前解析器不兼容，停止猜测并对照 deepseek-harness 上游实现。',
  );
  return lines.join('\n');
}

function loadSession(candidate) {
  const artifactData = readArtifact(candidate.artifact, false);
  const rows = artifactData.rows;
  const header = rows[0];
  if (!header || header.type !== 'session') throw new Error('会话缺少有效 header');
  const normalized = normalizeEvents(rows.slice(1));
  return {
    ...candidate,
    header,
    rows: rows.slice(1),
    frameCount: artifactData.frameCount,
    tornTail: artifactData.tornTail,
    normalized,
  };
}

function sessionTitleLite(session) {
  if (session.title) return session.title;
  const projected = projectionValue(session.projection, 'title');
  return typeof projected === 'string' && projected ? projected : session.sessionId;
}

function pickSession(sessions, value, preferNonBlank = true) {
  if (!sessions.length) return null;
  if (!value) {
    return (preferNonBlank && sessions.find((session) => !session.blank && !session.scanError))
      || sessions.find((session) => !session.scanError)
      || sessions[0];
  }
  const query = String(value).toLowerCase();
  return sessions.find((session) => session.sessionId.toLowerCase().startsWith(query))
    || sessions.find((session) => session.sessionId.toLowerCase().includes(query))
    || sessions.find((session) => sessionTitleLite(session).toLowerCase().includes(query))
    || null;
}

function listOutput(sessions, project, dshDir, limit, asJson) {
  const shown = limit > 0 ? sessions.slice(0, limit) : sessions;
  if (asJson) {
    return JSON.stringify({
      project, sessions: shown.map((session) => ({
        session_id: session.sessionId,
        title: sessionTitleLite(session),
        cwd: session.cwd,
        last_ts: fmtTime(session.activityAt),
        turns: session.count,
        blank: session.blank,
        error: session.scanError || null,
      })),
    }, null, 2);
  }
  const lines = [`当前项目: ${project}`, `会话根: ${path.join(dshDir, 'sessions')}`, `找到 ${sessions.length} 个会话${shown.length < sessions.length ? `（显示最近 ${shown.length} 个）` : ''}：`, ''];
  shown.forEach((session, index) => {
    const mark = index === 0 ? '[最近]' : '      ';
    const blank = session.blank ? ' [空白]' : '';
    const broken = session.scanError ? ` [解析失败: ${session.scanError}]` : '';
    lines.push(`${mark} ${fmtTime(session.activityAt) || '(无时间)'}  ${session.sessionId.slice(0, 28).padEnd(28)}  回合:${String(session.count).padEnd(3)} 标题: ${sessionTitleLite(session)}${blank}${broken}`);
  });
  return lines.join('\n');
}

function printHelp() {
  console.log(`用法: node resume_dsh.js [选项]

读取 DeepSeek Harness 本地 session.jsonl.zstd / session.jsonl，生成结构化接管摘要。

选项:
  --list              列出当前项目的会话
  --latest            取最近一个非空会话（默认）
  --session VALUE     指定 ID、ID 前缀或标题关键词（支持跨项目）
  --project PATH      项目路径，默认当前工作目录
  --dsh-dir DIR       DSH 数据目录，默认 ~/.dsh 或 $DSH_HOME
  --recent N          近期活动条目数，默认 10
  --max-chars N       单条内容截断长度，默认 1800
  --limit N           --list 数量上限，0 不限制
  --json              JSON 输出
  --output FILE       写入 UTF-8 文件
  -h, --help          显示帮助`);
}

function parseArgs(argv) {
  const args = { list: false, latest: false, session: null, project: null, dshDir: null, recent: 10, maxChars: 1800, limit: 0, json: false, output: null, help: false };
  const value = (flag, index) => {
    if (index + 1 >= argv.length) die(`${flag} 需要参数`);
    return argv[index + 1];
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--list') args.list = true;
    else if (arg === '--latest') args.latest = true;
    else if (arg === '--json') args.json = true;
    else if (arg === '-h' || arg === '--help') args.help = true;
    else if (arg === '--session') args.session = value(arg, i++);
    else if (arg.startsWith('--session=')) args.session = arg.slice(10);
    else if (arg === '--project') args.project = value(arg, i++);
    else if (arg.startsWith('--project=')) args.project = arg.slice(10);
    else if (arg === '--dsh-dir') args.dshDir = value(arg, i++);
    else if (arg.startsWith('--dsh-dir=')) args.dshDir = arg.slice(10);
    else if (arg === '--recent') args.recent = Number.parseInt(value(arg, i++), 10);
    else if (arg.startsWith('--recent=')) args.recent = Number.parseInt(arg.slice(9), 10);
    else if (arg === '--max-chars') args.maxChars = Number.parseInt(value(arg, i++), 10);
    else if (arg.startsWith('--max-chars=')) args.maxChars = Number.parseInt(arg.slice(12), 10);
    else if (arg === '--limit') args.limit = Number.parseInt(value(arg, i++), 10);
    else if (arg.startsWith('--limit=')) args.limit = Number.parseInt(arg.slice(8), 10);
    else if (arg === '--output') args.output = value(arg, i++);
    else if (arg.startsWith('--output=')) args.output = arg.slice(9);
    else die(`未知参数 '${arg}'，使用 --help 查看用法`);
  }
  for (const [name, number] of [['--recent', args.recent], ['--max-chars', args.maxChars], ['--limit', args.limit]]) {
    if (!Number.isInteger(number) || number < 0) die(`${name} 必须是非负整数`);
  }
  if (args.maxChars === 0) die('--max-chars 必须大于 0');
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) return printHelp();
  const dshDir = path.resolve(args.dshDir || process.env.DSH_HOME || path.join(os.homedir(), '.dsh'));
  const project = path.resolve(args.project || process.cwd());
  const all = scanAllSessions(dshDir);
  if (!all.length) die(`在 ${path.join(dshDir, 'sessions')} 下未找到 DSH 会话`);

  let candidates = all.filter((session) => normPath(session.cwd) === normPath(project));
  let target = null;
  if (args.session) {
    target = pickSession(all, args.session, false);
    if (!target) die(`未匹配到会话 '${args.session}'`);
    if (!args.list) candidates = all;
  }

  if (args.list) {
    if (args.session) candidates = all;
    if (!candidates.length) die(`未找到项目 ${project} 对应的 DSH 会话`);
    const output = listOutput(candidates, args.session ? '(全部项目)' : project, dshDir, args.limit, args.json);
    if (args.output) fs.writeFileSync(args.output, output, 'utf8'); else console.log(output);
    return;
  }

  if (!target) {
    if (!candidates.length) die(`未找到项目 ${project} 对应的 DSH 会话；可用 --session ID 跨项目选择`);
    target = pickSession(candidates, null, true);
  }
  if (target.scanError) die(`会话 ${target.sessionId} 无法解析：${target.scanError}`);

  const session = loadSession(target);
  const state = buildState(session.normalized.items, session.projection);
  let output;
  if (args.json) {
    output = JSON.stringify({
      info: {
        session_id: session.sessionId,
        title: resolveTitle(session, session.normalized),
        cwd: session.header.cwd || '',
        agent_preset: session.header.agentPreset || '',
        parent_session_id: session.header.parentSession || '',
        model: session.normalized.meta.model || '',
        provider: session.normalized.meta.provider || '',
        first_ts: fmtTime(session.header.createdAt),
        last_ts: fmtTime(session.rows.map((row) => Number(row.time)).filter(Number.isFinite).at(-1) || session.header.createdAt),
        artifact: session.artifact,
        frame_count: session.frameCount,
        torn_tail: session.tornTail,
      },
      state,
      recent_items: session.normalized.items.slice(-args.recent),
    }, null, 2);
  } else {
    output = renderSummary(session, session.normalized, state, args.recent, args.maxChars);
  }
  if (args.output) {
    fs.writeFileSync(args.output, output, 'utf8');
    console.error(`摘要已写入：${args.output}`);
  } else {
    process.stdout.write(output.endsWith('\n') ? output : `${output}\n`);
  }
}

try {
  main();
} catch (error) {
  die(error && error.message ? error.message : String(error));
}
