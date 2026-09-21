#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/dsh: DeepSeek Harness（~/.dsh 的 zstd 压缩 JSONL 会话）-> 归一化会话。
 *
 * 格式依据（与 resume-dsh 一致）：
 *   - session.jsonl.zstd 由多个独立 Zstandard frame 追加组成（兼容明文 session.jsonl）
 *   - 按 surfaceOp（append/replace）重建当前 surface，只导出现在面上的消息
 *   - 推理（reasoning/thinking）跳过；compaction/summary -> 转换注记
 *   - 陈旧（已出 surface）的 tool/call 跳过，避免无配对调用
 * 标题：storages/session_projcache.json 的 title 优先，其次 session/title 事件。
 * 与 dsh.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const cs = require('../claude_session.js');

const ZSTD_MAGIC = 0xfd2fb528;

function defaultDir() {
  return path.resolve(process.env.DSH_HOME || path.join(os.homedir(), '.dsh'));
}

function normPath(value) {
  return String(value || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

function jsString(value) {
  if (value === null || value === undefined) return '';
  if (value === true) return 'true';
  if (value === false) return 'false';
  return String(value);
}

function numberOrZero(value) {
  if (typeof value === 'boolean' || value === null || value === undefined || value === '') return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function tsToIso(value) {
  // DSH 事件时间为 epoch 毫秒，转成毫秒精度 UTC ISO 串（claude_session 只认 ISO）
  if (value === undefined || value === null || value === '') return '';
  let millis;
  if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value))) {
    millis = Number(value);
  } else {
    let text = String(value);
    if (/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$/.test(text)) text += 'Z';
    millis = Date.parse(text);
  }
  if (!Number.isFinite(millis)) return '';
  return new Date(millis).toISOString();
}

// ---------------------------------------------------------------- zstd 多 frame 日志

function scanZstdFrames(buffer, maxFrames = Number.POSITIVE_INFINITY) {
  const frames = [];
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) return { frames, tornStart: start };
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) {
      throw new cs.ConvertError(`Zstandard 日志在字节 ${offset} 处的 frame magic 无效`);
    }
    offset += 4;
    if (offset === buffer.length) return { frames, tornStart: start };

    const descriptor = buffer.readUInt8(offset++);
    if ((descriptor & 0x18) !== 0) {
      throw new cs.ConvertError(`Zstandard 日志在字节 ${offset - 1} 处使用了保留的 frame header 位`);
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
      if (blockType === 3) throw new cs.ConvertError(`Zstandard 日志在字节 ${offset - 3} 处使用了保留 block 类型`);
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

function getDecompressor() {
  if (typeof zlib.zstdDecompressSync !== 'function') {
    throw new cs.ConvertError('当前 Node.js 不支持 zlib.zstdDecompressSync；请使用较新的 Node.js 运行时');
  }
  return (buf) => zlib.zstdDecompressSync(buf);
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
      throw new cs.ConvertError(`会话日志第 ${index + 1} 行不是有效 JSON：${error.message}`);
    }
  }
  return rows;
}

function readArtifact(file, firstFrameOnly = false) {
  if (String(file).endsWith('.zstd')) {
    let source;
    try {
      source = fs.readFileSync(file);
    } catch (error) {
      throw new cs.ConvertError(`无法读取会话日志：${error.message}`);
    }
    const scanned = scanZstdFrames(source, firstFrameOnly ? 1 : Number.POSITIVE_INFINITY);
    if (!scanned.frames.length) throw new cs.ConvertError('Zstandard 会话日志没有完整 frame');
    const decompress = getDecompressor();
    const chunks = [];
    for (const { start, end } of scanned.frames) {
      try {
        chunks.push(decompress(source.subarray(start, end)));
      } catch (error) {
        throw new cs.ConvertError(`Zstandard 日志在字节 ${start} 处的 frame 解压失败：${error.message}`);
      }
    }
    return {
      rows: parseJsonl(Buffer.concat(chunks).toString('utf8')),
      torn_tail: scanned.tornStart !== undefined,
      frame_count: scanned.frames.length,
    };
  }
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (error) {
    throw new cs.ConvertError(`无法读取会话日志：${error.message}`);
  }
  return { rows: parseJsonl(text), torn_tail: false, frame_count: 0 };
}

function findArtifact(sessionDir) {
  for (const name of ['session.jsonl.zstd', 'session.jsonl']) {
    const file = path.join(sessionDir, name);
    if (isFileSafe(file)) return file;
  }
  return null;
}

function isFileSafe(file) {
  try { return fs.statSync(file).isFile(); } catch (e) { return false; }
}

function loadProjectionCache(dshDir) {
  const file = path.join(dshDir, 'storages', 'session_projcache.json');
  if (!isFileSafe(file)) return {};
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && parsed.tables && parsed.tables.sessions ? parsed.tables.sessions : {};
  } catch (e) {
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
    throw new cs.ConvertError('首条记录不是有效的 DSH session header');
  }
  if (enforceVersion && header.version !== 0) {
    throw new cs.ConvertError(`不支持 DSH session format version ${String(header.version)}（当前解析器支持 0）`);
  }
  return header;
}

// ---------------------------------------------------------------- 会话扫描

function scanAllSessions(dshDir) {
  const root = path.join(dshDir, 'sessions');
  if (!isDirSafe(root)) return [];
  const cache = loadProjectionCache(dshDir);
  const sessions = [];
  let groups;
  try {
    groups = fs.readdirSync(root, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  } catch (e) {
    return [];
  }
  for (const group of groups) {
    if (!group.isDirectory()) continue;
    const groupDir = path.join(root, group.name);
    let items;
    try {
      items = fs.readdirSync(groupDir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
    } catch (e) {
      continue;
    }
    for (const item of items) {
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
        const stat = fs.statSync(artifact);
        const activityAt = numberOrZero(listMeta.lastPromptAt) || stat.mtimeMs || numberOrZero(header.createdAt);
        sessions.push({
          session_id: header.id,
          session_dir: sessionDir,
          artifact,
          header,
          projection: projected,
          cwd: header.cwd || '',
          title: typeof title === 'string' ? title : '',
          blank: listMeta.blank === true,
          count: numberOrZero(stats.turns),
          activity_at: activityAt,
          mtime: stat.mtimeMs,
          scan_error: header.version === 0 ? null
            : `不支持 DSH session format version ${String(header.version)}（当前解析器支持 0）`,
        });
      } catch (error) {
        sessions.push({
          session_id: item.name,
          session_dir: sessionDir,
          artifact,
          header: {},
          projection: cache[item.name] || {},
          cwd: '',
          title: '',
          blank: false,
          count: 0,
          activity_at: 0,
          mtime: 0,
          scan_error: error.message,
        });
      }
    }
  }
  sessions.sort((a, b) => (b.activity_at - a.activity_at) || (b.mtime - a.mtime));
  return sessions;
}

function isDirSafe(dir) {
  try { return fs.statSync(dir).isDirectory(); } catch (e) { return false; }
}

function pickSession(sessions, value) {
  const query = String(value).toLowerCase();
  return sessions.find((s) => s.session_id.toLowerCase().startsWith(query))
    || sessions.find((s) => s.session_id.toLowerCase().includes(query))
    || sessions.find((s) => (s.title || '').toLowerCase().includes(query))
    || null;
}

// ---------------------------------------------------------------- 事件解析

function textOf(value, includeReasoning = false) {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((item) => textOf(item, includeReasoning)).filter(Boolean).join('\n');
  if (typeof value !== 'object') return jsString(value);
  if (!includeReasoning && ['reasoning', 'thinking', 'reasoning-chunk'].includes(value.type)) return '';
  if (typeof value.text === 'string') return value.text;
  if (value.content !== undefined) return textOf(value.content, includeReasoning);
  if (value.output !== undefined) return textOf(value.output, includeReasoning);
  return '';
}

function parseArguments(value) {
  if (value === undefined || value === null || value === '') return {};
  if (typeof value === 'object') return value;
  try { return JSON.parse(String(value)); } catch (e) { return { _raw: String(value) }; }
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
      throw new cs.ConvertError(`会话事件 seq ${event.seq} 使用了未知 surfaceOp`);
    }
    const start = surface.indexOf(op.start);
    const end = surface.indexOf(op.end);
    if (start < 0 || end < start) {
      throw new cs.ConvertError(`会话事件 seq ${event.seq} 的 surface replace 范围无效`);
    }
    surface.splice(start, end - start + 1, event.seq);
  }
  return new Set(surface);
}

function toolResultCallId(data) {
  const message = (data && typeof data === 'object' && data.message) || {};
  if (message.source && message.source.callId) return jsString(message.source.callId);
  if (Array.isArray(message.content)) {
    for (const block of message.content) if (block && block.toolCallId) return jsString(block.toolCallId);
  }
  return '';
}

function normalizeEvents(rows) {
  const msgs = [];
  const meta = { title: '', model: '', provider: '' };
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
    const ts = tsToIso(event.time);

    if (type === 'session/title' && typeof data.title === 'string') {
      meta.title = data.title;
      continue;
    }
    if (type === 'request/header') {
      const config = data.header && data.header.config || {};
      if (config.model) meta.model = jsString(config.model);
      if (config.provider) meta.provider = jsString(config.provider);
      continue;
    }
    if (type === 'compaction/summary') {
      // 仅当随后紧跟 replace 进面的 user/message 时才是当前摘要，否则已被后续压缩覆盖
      const replacement = rows[rowIndex + 1];
      const isCurrent = replacement && replacement.type === 'user/message'
        && replacement.surfaceOp && typeof replacement.surfaceOp === 'object'
        && replacement.surfaceOp.op === 'replace'
        && surfaceSeqs.has(replacement.seq);
      const text = isCurrent ? textOf(data.summary).trim() : '';
      if (text) msgs.push(cs.mNote(`历史压缩摘要：${text}`, ts));
      continue;
    }

    if (type === 'user/message') {
      if (surfaceSeqs.has(event.seq) && isRealUserMessage(data)) {
        const text = textOf(data.content).trim();
        if (text) msgs.push(cs.mMessage('user', text, ts));
      }
    } else if (type === 'assistant/message') {
      if (surfaceSeqs.has(event.seq)) {
        const message = data.message || {};
        const text = textOf(message.content).trim();
        if (text) msgs.push(cs.mMessage('assistant', text, ts));
      }
    } else if (type === 'tool/call') {
      const name = jsString(data.name || '');
      const callId = jsString(data.callId || '');
      // 结果已存在但不在当前 surface -> 陈旧调用，跳过以免产生无配对的调用
      if (callId && allResultCallIds.has(callId) && !currentResultCallIds.has(callId)) continue;
      msgs.push(cs.mToolUse(callId, name,
        normalizeInput(name, parseArguments(data.arguments)), ts));
    } else if (type === 'tool/result') {
      if (surfaceSeqs.has(event.seq)) {
        const message = data.message || {};
        const callId = toolResultCallId(data);
        let isError = Boolean(data.error);
        if (Array.isArray(message.content)) {
          for (const block of message.content) {
            if (block && block.isError) isError = true;
          }
        }
        msgs.push(cs.mToolResult(callId, textOf(message.content), isError, ts));
      }
    }
  }
  return { msgs, meta };
}

function truncate(value, max) {
  const text = typeof value === 'string' ? value : jsString(value);
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

function resolveTitle(session, meta, msgs) {
  const projected = projectionValue(session.projection, 'title');
  if (typeof projected === 'string' && projected.trim()) return projected.trim();
  if (meta.title) return meta.title;
  for (const m of msgs) {
    if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
      const line = m.text.trim().split(/\r?\n/)[0];
      return truncate(line, 60) || session.session_id;
    }
  }
  return session.session_id;
}

function parseNormalized(session) {
  const rows = readArtifact(session.artifact, false).rows;
  const header = rows[0];
  if (!header || typeof header !== 'object' || header.type !== 'session') {
    throw new cs.ConvertError('会话缺少有效 header');
  }
  const { msgs, meta } = normalizeEvents(rows.slice(1));
  return { rows, header, msgs, meta };
}

// ---------------------------------------------------------------- 对外接口

function listSessions(opts) {
  const o = opts || {};
  const dshDir = path.resolve(o.dir || defaultDir());
  let sessions = scanAllSessions(dshDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  return sessions.slice(0, 50).map((s) => {
    let title = (s.title || '').trim();
    if (!title) {
      try {
        const parsed = parseNormalized(s);
        title = resolveTitle(s, parsed.meta, parsed.msgs);
      } catch (e) {
        title = '';
      }
    }
    return {
      session_id: s.session_id,
      title: title || s.session_id,
      cwd: s.cwd,
      mtime: s.mtime / 1000,
      path: s.artifact,
      count: s.count,
      last_ts: cs.fmtCst(tsToIso(s.activity_at)),
    };
  });
}

function loadSession(opts) {
  const o = opts || {};
  const dshDir = path.resolve(o.dir || defaultDir());
  let sessions = scanAllSessions(dshDir);
  if (o.project) {
    const norm = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.cwd) === norm);
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 DSH 会话（dir=${dshDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  let target;
  if (o.session_id) {
    target = pickSession(sessions, o.session_id);
    if (!target) {
      throw new cs.ConvertError(`未找到 DSH 会话（dir=${dshDir}`
        + (o.project ? `，project=${o.project}` : '')
        + `，session=${o.session_id}）`);
    }
  } else {
    // 默认取最近一个可转换的非空白会话（与 resume_dsh --latest 一致）
    target = sessions.find((s) => !s.blank && !s.scan_error)
      || sessions.find((s) => !s.scan_error)
      || sessions[0];
  }

  if (target.scan_error) {
    throw new cs.ConvertError(`会话 ${target.session_id} 无法解析：${target.scan_error}`);
  }

  let parsed;
  try {
    parsed = parseNormalized(target);
  } catch (error) {
    if (error instanceof cs.ConvertError) {
      throw new cs.ConvertError(`会话 ${target.session_id} 解析失败：${error.message}`);
    }
    throw error;
  }

  const { rows, header, msgs, meta } = parsed;
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.artifact}`);

  const sid = header.id || target.session_id;
  const title = resolveTitle(target, meta, msgs);
  const cwd = header.cwd || target.cwd || '';

  const times = [];
  for (const row of rows) {
    const t = numberOrZero(row && row.time);
    if (t) times.push(t);
  }
  const startedAt = tsToIso(header.createdAt) || tsToIso(times[0]);
  const endedAt = tsToIso(times[times.length - 1]) || (msgs[msgs.length - 1].ts || '');
  const model = [meta.provider, meta.model].filter(Boolean).join('/');

  const ref = cs.makeRef('dsh', sid, title, cwd, {
    model, started_at: startedAt, ended_at: endedAt, source: target.artifact,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
