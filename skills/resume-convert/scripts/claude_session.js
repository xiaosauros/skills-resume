#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * claude_session: 把任意工具的归一化会话内容转换成 Claude Code 原生 JSONL 会话文件。
 *
 * 共享工具类，供同目录 export_to_claude.js 与各 adapters/<tool>.js 使用：
 *   - Msg / SessionRef 归一化模型（各工具适配器唯一的输出契约）
 *   - buildRecords(): 归一化消息 -> Claude Code JSONL 记录（uuid 链、工具配对、截断）
 *   - validateRecords(): 写盘前校验（JSON 可序列化、链完整性、tool_use/tool_result 配对）
 *   - writeSession(): 原子写入 ~/.claude/projects/<项目>/<sessionId>.jsonl
 *
 * 只写新文件、绝不覆盖已有会话；支持 --dry-run 与导出到任意目录（测试用）。
 * 与同目录 claude_session.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

// 写入记录时标记的 Claude Code 版本（信息性字段，与当前版本一致即可）
const CLAUDE_CODE_VERSION = '2.1.278';

// 默认截断长度：工具结果 / 普通文本 / 工具入参（buildRecords 运行期间会被临时覆写）
let DEFAULT_MAX_TOOL_OUTPUT = 20000;
let DEFAULT_MAX_TEXT = 200000;

// 转换注记前缀（compact 摘要等元信息以用户消息形式注入，明确标记非原始对话）
const NOTE_PREFIX = '[转换注记] ';


class ConvertError extends Error {
  constructor(msg) {
    super(msg);
    this.name = 'ConvertError';
  }
}


// ---------------------------------------------------------------- 归一化模型

function mMessage(role, text, ts) {
  return { kind: 'message', role, text: text == null ? '' : String(text), ts: ts || '' };
}

function mToolUse(callId, name, input, ts) {
  return { kind: 'tool_use', call_id: callId == null ? '' : String(callId), name, input, ts: ts || '' };
}

function mToolResult(callId, content, isError, ts) {
  return {
    kind: 'tool_result', call_id: callId == null ? '' : String(callId),
    content: content == null ? '' : String(content), is_error: !!isError, ts: ts || '',
  };
}

function mNote(text, ts) {
  return { kind: 'note', text: text == null ? '' : String(text), ts: ts || '' };
}

function makeRef(tool, sessionId, title, cwd, opts) {
  const o = opts || {};
  return {
    tool, session_id: sessionId || '', title: title || '', cwd: cwd || '',
    model: o.model || '', started_at: o.started_at || '', ended_at: o.ended_at || '',
    source: o.source || '',
  };
}


// ---------------------------------------------------------------- 基础工具

function getClaudeDir(explicit) {
  return path.resolve(explicit || process.env.CLAUDE_CONFIG_DIR
    || path.join(os.homedir(), '.claude'));
}

function projectDirName(cwd) {
  // Claude Code 的项目目录编码规则：非字母数字字符全部替换为 '-'
  return String(cwd).replace(/[^A-Za-z0-9]/g, '-');
}

function newUuid() {
  return crypto.randomUUID();
}

function newSessionId() {
  return crypto.randomUUID();
}

function newMsgId(tsIso) {
  const digits = String(tsIso || '').replace(/[^0-9]/g, '').slice(0, 17);
  return 'msg_' + digits + crypto.randomBytes(Math.ceil((24 - digits.length) / 2))
    .toString('hex').slice(0, 24 - digits.length);
}

function sanitizeSessionId(sid) {
  const s = String(sid || '').trim().replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 100);
  return s || newSessionId();
}

function sanitizeToolName(name) {
  const s = String(name || 'tool').replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64);
  return s || 'tool';
}

function toIsoZ(ts) {
  if (!ts) return '';
  let s = String(ts).trim();
  if (!s) return '';
  // 无时区标记的日期时间按 UTC 解析（与 Python 实现一致，避免随宿主机时区漂移）
  if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(s) && /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s)) {
    s += 'Z';
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) return '';
  return d.toISOString(); // 毫秒精度 UTC，形如 2026-06-02T01:38:18.005Z
}

function fmtCst(ts) {
  // 展示用时间：UTC -> UTC+8 'YYYY-MM-dd HH:mm:ss'（与 resume-* 一致）
  const iso = toIsoZ(ts);
  if (!iso) return '';
  const d = new Date(new Date(iso).getTime() + 8 * 3600 * 1000);
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
    + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

function truncateText(s, limit) {
  s = String(s);
  if (!(limit > 0) || s.length <= limit) return s;
  return s.slice(0, limit) + `\n…[已截断，原长 ${s.length} 字符]`;
}


// ---------------------------------------------------------------- 归一化 -> 记录

function normalizeMsgs(msgs, ref) {
  // 规整 Msg 列表：时间戳回填 + 单调化、工具名净化、call_id 去重、内容字符串化
  const out = [];
  const seenIds = new Set();
  for (let m of msgs) {
    if (!m || !m.kind) continue;
    m = Object.assign({}, m);
    m.ts = toIsoZ(m.ts || '');
    if (m.kind === 'tool_use') {
      m.name = sanitizeToolName(m.name);
      let inp = m.input;
      if (inp == null) inp = {};
      if (typeof inp !== 'object' || Array.isArray(inp)) {
        inp = { raw: typeof inp === 'string' ? inp : JSON.stringify(inp) };
      }
      m.input = inp;
      let cid = String(m.call_id || '');
      if (!cid || seenIds.has(cid)) {
        cid = `call_${seenIds.size + 1}_${crypto.randomBytes(4).toString('hex')}`;
      }
      seenIds.add(cid);
      m.call_id = cid;
    } else if (m.kind === 'tool_result') {
      let cid = String(m.call_id || '');
      if (!cid || !seenIds.has(cid)) {
        cid = cid || `call_orphan_${out.length}_${crypto.randomBytes(3).toString('hex')}`;
        if (!seenIds.has(cid)) seenIds.add(cid);
      }
      m.call_id = cid;
      m.content = m.content == null ? '' : String(m.content);
      m.is_error = !!m.is_error;
    } else if (m.kind === 'message' || m.kind === 'note') {
      m.text = m.text == null ? '' : String(m.text);
    } else {
      continue;
    }
    out.push(m);
  }

  // 时间戳单调化：无 ts 的用 started_at 兜底；所有 ts 保证非递减
  let last = '';
  const fallbackBase = toIsoZ(ref.started_at || '');
  for (const m of out) {
    if (!m.ts && fallbackBase) m.ts = fallbackBase;
    if (m.ts && last && m.ts < last) m.ts = last;
    last = m.ts || last;
  }
  return out;
}

function groupRecords(msgs, degradeTools) {
  // 把归一化消息按角色分组为记录：连续同角色合并为一条记录
  // assistant 记录块序：[text..., tool_use...]；user 记录块序：[tool_result..., text...]
  const records = [];
  for (let m of msgs) {
    let kind = m.kind;
    if (degradeTools && kind === 'tool_use') {
      const args = truncateText(JSON.stringify(m.input || {}), 2000);
      m = mMessage('assistant', `【工具调用】${m.name} ${args}`, m.ts);
      kind = 'message';
    } else if (degradeTools && kind === 'tool_result') {
      const tag = m.is_error ? '（错误）' : '';
      m = mMessage('user', `【工具结果】${tag}${m.content}`, m.ts);
      kind = 'message';
    } else if (kind === 'note') {
      m = mMessage('user', NOTE_PREFIX + (m.text || ''), m.ts);
      kind = 'message';
    }

    let role;
    let block;
    if (kind === 'message') {
      role = m.role === 'assistant' ? 'assistant' : 'user';
      block = { type: 'text', text: truncateText(m.text || '', DEFAULT_MAX_TEXT) };
    } else if (kind === 'tool_use') {
      block = {
        type: 'tool_use', id: m.call_id, name: m.name, input: m.input || {},
      };
      role = 'assistant';
    } else if (kind === 'tool_result') {
      block = {
        type: 'tool_result', tool_use_id: m.call_id,
        content: truncateText(m.content || '', DEFAULT_MAX_TOOL_OUTPUT),
        is_error: !!m.is_error,
      };
      role = 'user';
    } else {
      continue;
    }

    if (records.length && records[records.length - 1].role === role) {
      records[records.length - 1].blocks.push(block);
    } else {
      records.push({ role, blocks: [block], ts: m.ts || '' });
    }
  }

  // 块序整理
  for (const r of records) {
    if (r.role === 'assistant') {
      const texts = r.blocks.filter((b) => b.type === 'text');
      const tools = r.blocks.filter((b) => b.type === 'tool_use');
      r.blocks = texts.concat(tools);
    } else {
      const results = r.blocks.filter((b) => b.type === 'tool_result');
      const texts = r.blocks.filter((b) => b.type === 'text');
      r.blocks = results.concat(texts);
    }
  }
  return records;
}

function fixToolPairing(records, warnings) {
  // 保证每个 tool_use 都被紧随其后的 user 记录中的 tool_result 覆盖
  const fixed = [];
  for (const r of records) {
    fixed.push(r);
    if (r.role !== 'assistant') continue;
    const pending = r.blocks.filter((b) => b.type === 'tool_use').map((b) => b.id);
    if (!pending.length) continue;
    const nxt = fixed.length < records.length ? records[fixed.length] : null;
    if (nxt && nxt.role === 'user') {
      const covered = new Set(
        nxt.blocks.filter((b) => b.type === 'tool_result').map((b) => b.tool_use_id));
      const missing = pending.filter((c) => !covered.has(c));
      for (const cid of missing) {
        nxt.blocks.unshift({
          type: 'tool_result', tool_use_id: cid,
          content: '（转换器注记：原会话未记录该工具的返回结果）', is_error: false,
        });
        warnings.push(`工具调用 ${cid} 缺少结果记录，已合成空结果补齐`);
      }
      continue;
    }
    for (const cid of pending) {
      warnings.push(`工具调用 ${cid} 后无用户记录，已合成结果记录补齐`);
    }
    fixed.push({
      role: 'user', ts: r.ts,
      blocks: pending.map((cid) => ({
        type: 'tool_result', tool_use_id: cid,
        content: '（转换器注记：原会话未记录该工具的返回结果）', is_error: false,
      })),
    });
  }
  return fixed;
}

function dropOrphanResults(records, warnings) {
  // 没有对应 tool_use 的 tool_result 降级为普通文本
  const used = new Set();
  for (const r of records) {
    if (r.role === 'assistant') {
      for (const b of r.blocks) {
        if (b.type === 'tool_use') used.add(b.id);
      }
    }
  }
  for (const r of records) {
    if (r.role !== 'user') continue;
    const results = r.blocks.filter((b) => b.type === 'tool_result');
    const dropped = results.filter((b) => !used.has(b.tool_use_id));
    if (dropped.length) {
      for (const b of dropped) {
        warnings.push(`孤立的工具结果 ${b.tool_use_id} 已降级为文本保留`);
        const note = {
          type: 'text',
          text: NOTE_PREFIX + `孤立工具结果（${b.tool_use_id}）：\n${b.content}`,
        };
        const firstText = r.blocks.find((b2) => b2.type === 'text');
        if (firstText) firstText.text = note.text + '\n\n' + firstText.text;
        else r.blocks.push(note);
      }
    }
    r.blocks = r.blocks.filter((b) => b.type === 'tool_result' && used.has(b.tool_use_id))
      .concat(r.blocks.filter((b) => b.type === 'text'));
  }
  return records;
}

function buildRecords(ref, msgs, opts) {
  // 归一化消息 -> { records, warnings }；records 首条为 ai-title，其后为 uuid 链
  const o = opts || {};
  const degradeTools = !!o.degrade_tools;
  const maxToolOutput = o.max_tool_output || DEFAULT_MAX_TOOL_OUTPUT;
  const maxText = o.max_text || DEFAULT_MAX_TEXT;
  const warnings = [];
  const saved = { out: DEFAULT_MAX_TOOL_OUTPUT, txt: DEFAULT_MAX_TEXT };
  DEFAULT_MAX_TOOL_OUTPUT = maxToolOutput;
  DEFAULT_MAX_TEXT = maxText;
  try {
    msgs = normalizeMsgs(msgs, ref);

    const sid = sanitizeSessionId(ref.session_id || newSessionId());
    let title = (ref.title || '').trim();
    if (!title) {
      for (const m of msgs) {
        if (m.kind === 'message' && m.role === 'user' && (m.text || '').trim()) {
          const first = m.text.trim().split('\n')[0];
          title = first.slice(0, 60) + (first.length > 60 ? '…' : '');
          break;
        }
      }
    }
    if (!title) title = sid;

    let records = groupRecords(msgs, degradeTools);
    records = fixToolPairing(records, warnings);
    records = dropOrphanResults(records, warnings);

    if (!records.length) throw new ConvertError('会话内容为空，无法转换');
    if (records[0].role === 'assistant') {
      warnings.push('会话以助手消息开头，已前置一条用户注记（API 要求首条为 user）');
      records.unshift({
        role: 'user', ts: records[0].ts,
        blocks: [{
          type: 'text',
          text: NOTE_PREFIX + '历史会话由 ' + (ref.tool || '外部工具') + ' 转换而来',
        }],
      });
    }

    const claudeRecords = [{ type: 'ai-title', aiTitle: title, sessionId: sid }];
    let parent = null;
    for (const r of records) {
      const uid = newUuid();
      const ts = r.ts || toIsoZ(ref.started_at)
        || new Date().toISOString();
      let message;
      if (r.role === 'user') {
        const hasResult = r.blocks.some((b) => b.type === 'tool_result');
        if (hasResult) {
          message = {
            role: 'user',
            content: r.blocks.map((b) => (b.type === 'tool_result'
              ? {
                type: 'tool_result', tool_use_id: b.tool_use_id,
                content: b.content, is_error: !!b.is_error,
              }
              : { type: 'text', text: b.text })),
          };
        } else {
          message = { role: 'user', content: r.blocks.map((b) => b.text).join('\n\n') };
        }
      } else {
        message = {
          id: newMsgId(ts), type: 'message', role: 'assistant',
          model: ref.model || 'unknown',
          content: r.blocks.map((b) => (b.type === 'text'
            ? { type: 'text', text: b.text }
            : { type: 'tool_use', id: b.id, name: b.name, input: b.input || {} })),
          stop_reason: 'end_turn', stop_sequence: null,
          usage: { input_tokens: 0, output_tokens: 0 },
        };
      }
      claudeRecords.push({
        parentUuid: parent, isSidechain: false, type: r.role,
        message, uuid: uid, timestamp: ts,
        sessionId: sid, session_id: sid,
        userType: 'external', cwd: ref.cwd || '',
        version: CLAUDE_CODE_VERSION,
      });
      parent = uid;
    }
    return { records: claudeRecords, warnings };
  } finally {
    DEFAULT_MAX_TOOL_OUTPUT = saved.out;
    DEFAULT_MAX_TEXT = saved.txt;
  }
}


// ---------------------------------------------------------------- 校验

function validateRecords(records) {
  // 写盘前校验，返回 { errors, warnings }；errors 非空时禁止写盘
  const errors = [];
  const warnings = [];
  if (!records || !records.length) return { errors: ['记录为空'], warnings };

  if (records[0].type !== 'ai-title') errors.push('首条记录必须是 ai-title');
  const sid = records[0].sessionId;

  let parent = null;
  const seenUuids = new Set();
  let pendingCalls = [];
  let msgRecords = 0;
  for (let i = 1; i < records.length; i++) {
    const rec = records[i];
    try {
      JSON.stringify(rec);
    } catch (e) {
      errors.push(`第 ${i + 1} 行不可 JSON 序列化: ${e.message}`);
      continue;
    }
    if (rec.type !== 'user' && rec.type !== 'assistant') {
      errors.push(`第 ${i + 1} 行 type 非法: ${rec.type}`);
      continue;
    }
    msgRecords++;
    const uid = rec.uuid;
    if (!uid || seenUuids.has(uid)) {
      errors.push(`第 ${i + 1} 行 uuid 缺失或重复`);
      continue;
    }
    seenUuids.add(uid);
    if (rec.parentUuid !== parent) errors.push(`第 ${i + 1} 行 parentUuid 链断裂`);
    parent = uid;
    if (rec.sessionId !== sid) errors.push(`第 ${i + 1} 行 sessionId 与标题记录不一致`);
    if (!rec.timestamp) warnings.push(`第 ${i + 1} 行缺少 timestamp`);

    const content = (rec.message || {}).content;
    const blocks = Array.isArray(content) ? content
      : (typeof content === 'string' ? [{ type: 'text', text: content }] : []);
    if (rec.type === 'assistant') {
      for (const b of blocks) {
        if (b.type === 'tool_use') {
          if (!b.id || b.name == null) errors.push(`第 ${i + 1} 行 tool_use 缺少 id/name`);
          else pendingCalls.push(b.id);
        } else if (b.type !== 'text' && b.type !== 'thinking') {
          errors.push(`第 ${i + 1} 行 assistant 含非法块: ${b.type}`);
        }
      }
    } else {
      for (const b of blocks) {
        if (b.type === 'tool_result') {
          const idx = pendingCalls.indexOf(b.tool_use_id);
          if (idx >= 0) pendingCalls.splice(idx, 1);
          else errors.push(`第 ${i + 1} 行 tool_result 无配对 tool_use: ${b.tool_use_id}`);
        }
      }
    }
  }
  if (pendingCalls.length) errors.push(`存在未被结果的 tool_use: ${pendingCalls.join(',')}`);
  if (msgRecords < 1) errors.push('没有任何 user/assistant 消息记录');
  return { errors, warnings };
}


// ---------------------------------------------------------------- 写入

function decideTarget(ref, opts) {
  // 决定输出文件路径，返回 { path, sessionId, proj }
  const o = opts || {};
  const sid = sanitizeSessionId(o.session_id || ref.session_id || newSessionId());
  if (o.out_file) return { path: path.resolve(o.out_file), sessionId: sid, proj: '' };
  const cwd = o.project_override || ref.cwd || '';
  if (!cwd) {
    throw new ConvertError('会话缺少项目路径（cwd），无法定位 ~/.claude/projects 目标目录；'
      + '请用 --to-project 指定项目路径或 --out-file 指定输出文件');
  }
  const proj = projectDirName(cwd);
  const root = o.out_dir || path.join(getClaudeDir(o.claude_dir), 'projects', proj);
  return { path: path.join(path.resolve(root), `${sid}.jsonl`), sessionId: sid, proj };
}

function writeSession(ref, msgs, opts) {
  // 构建、校验并写入 Claude 会话文件，返回结果 dict
  const o = opts || {};
  ref = Object.assign({}, ref);
  if (o.title_prefix && ref.title) ref.title = o.title_prefix + ref.title;

  const built = buildRecords(ref, msgs, o);
  const records = built.records;
  const warnings = built.warnings;
  const v = validateRecords(records);
  const errors = v.errors;
  for (const w of v.warnings) warnings.push(w);

  const t = decideTarget(ref, o);
  const result = {
    ok: errors.length === 0, path: t.path, session_id: t.sessionId,
    title: records.length ? records[0].aiTitle : '',
    records: records.length, warnings, errors,
    dry_run: !!o.dry_run, wrote: false, project_dir: t.proj,
  };
  if (errors.length) return result;
  if (o.dry_run) return result;
  if (fs.existsSync(t.path) && !o.allow_overwrite) {
    result.errors = [`目标文件已存在，拒绝覆盖：${t.path}（换 --session-id 或删除后重试）`];
    result.ok = false;
    return result;
  }

  const body = records.map((r) => JSON.stringify(r)).join('\n') + '\n';
  fs.mkdirSync(path.dirname(t.path), { recursive: true });
  const tmp = `${t.path}.tmp-${crypto.randomBytes(3).toString('hex')}`;
  const fd = fs.openSync(tmp, 'w');
  try {
    fs.writeSync(fd, body, null, 'utf-8');
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, t.path);
  result.wrote = true;
  return result;
}


module.exports = {
  CLAUDE_CODE_VERSION,
  DEFAULT_MAX_TOOL_OUTPUT,
  DEFAULT_MAX_TEXT,
  NOTE_PREFIX,
  ConvertError,
  mMessage, mToolUse, mToolResult, mNote, makeRef,
  getClaudeDir, projectDirName, newUuid, newSessionId, newMsgId,
  sanitizeSessionId, sanitizeToolName, toIsoZ, fmtCst, truncateText,
  buildRecords, validateRecords, decideTarget, writeSession,
};
