#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * adapters/qoder: Qoder CLI（~/.qoder/projects 会话 JSONL）-> 归一化会话。
 *
 * 格式依据（与 resume-qoder 一致）：
 *   - 用户输入：type=user 且非 isMeta/isVisibleInTranscriptOnly 的文本
 *     （剔除 <system-reminder> 包裹内容）
 *   - 模型输出：type=assistant 的 text 块；tool_use/tool_result 块按对转换
 *   - 推理（thinking/redacted_thinking）不可跨模型迁移，跳过
 *   - isCompactSummary / system(compact_summary|summary) -> 转换注记
 *   - isSidechain 事件按原始顺序作为普通消息保留（与 resume-qoder 语义一致）
 *   - <sid>/state.json 的 workspaceDirectories 兜底 cwd；旧版 *-session.json
 *     仅元数据快照，列出但不可转换
 * 标题：custom-title > ai-title > agent-name > 首条用户消息首行。
 * 与 qoder.py 功能等价、输出一致。
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cs = require('../claude_session.js');

const SYS_REMINDER_RE = /<system-reminder>[\s\S]*?<\/system-reminder>/g;

function defaultDir() {
  return process.env.QODER_HOME || path.join(os.homedir(), '.qoder');
}

function normPath(p) {
  return p ? path.resolve(String(p)).toLowerCase().replace(/\\/g, '/') : '';
}

function timestampMs(value) {
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (Number.isFinite(number)) return Math.abs(number) < 1e10 ? Math.trunc(number * 1000) : Math.trunc(number);
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function tsToIso(value) {
  const ms = timestampMs(value);
  if (!ms) return '';
  const d = new Date(ms);
  return isNaN(d.getTime()) ? '' : d.toISOString();
}

function parseJsonl(filePath) {
  let text;
  try {
    text = fs.readFileSync(filePath, 'utf-8');
  } catch (e) {
    throw new cs.ConvertError(`无法读取 Qoder 会话文件：${e.message}`);
  }
  const events = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      events.push(JSON.parse(t));
    } catch (e) { /* 坏行跳过 */ }
  }
  return events;
}

function extractText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((b) => b && typeof b === 'object' && b.type === 'text' && b.text)
    .map((b) => b.text).join('\n');
}

function resultText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((b) => {
      if (b && typeof b === 'object') return String(b.text || b.content || '');
      return b == null ? '' : String(b);
    }).filter(Boolean).join('\n');
  }
  if (content === null || content === undefined) return '';
  return typeof content === 'object' ? JSON.stringify(content) : String(content);
}

function asText(value) {
  // 非字符串值统一为 JSON 文本（保证 py/js 序列化一致）
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function stripReminders(value) {
  return String(value || '').replace(SYS_REMINDER_RE, '').trim();
}

function normalize(events) {
  // 与 resume-qoder 的 normalizeEvents 一致；差异：compact 摘要作为 note
  // 条目进入主时间线，严格保持源顺序
  const items = [];
  const titles = { custom: '', ai: '', agent: '' };
  let cwd = '', gitBranch = '', model = '', reasoningEffort = '', contextWindow = '';
  let entrypoint = '', version = '';
  let isSidechain = false;
  for (const event of events) {
    const eventType = event.type;
    if (eventType === 'custom-title') { titles.custom = event.customTitle || titles.custom; continue; }
    if (eventType === 'ai-title') { titles.ai = event.aiTitle || titles.ai; continue; }
    if (eventType === 'agent-name') { titles.agent = event.agentName || titles.agent; continue; }
    if (eventType === 'runtime-config') {
      model = event.model || model;
      reasoningEffort = event.reasoningEffort || reasoningEffort;
      contextWindow = event.contextWindow || contextWindow;
      continue;
    }
    cwd = cwd || event.cwd || '';
    gitBranch = gitBranch || event.gitBranch || '';
    entrypoint = entrypoint || event.entrypoint || '';
    version = version || event.version || '';
    isSidechain = isSidechain || !!event.isSidechain;
    if (event.isCompactSummary) {
      const compact = event.summary || event.content || extractText((event.message || {}).content);
      if (compact) {
        items.push({ kind: 'note', timestamp: timestampMs(event.timestamp), text: asText(compact) });
      }
      continue;
    }
    if (eventType === 'system') {
      if (['compact_summary', 'summary'].includes(event.subtype)) {
        const compact = event.summary || event.content;
        if (compact) {
          items.push({ kind: 'note', timestamp: timestampMs(event.timestamp), text: asText(compact) });
        }
      }
      continue;
    }
    if (!['user', 'assistant'].includes(eventType)) continue;
    const message = event.message || {};
    const content = message.content;
    const ts = timestampMs(event.timestamp);
    if (message.model) model = message.model;
    if (eventType === 'user') {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === 'object' && block.type === 'tool_result') {
            items.push({
              kind: 'tool_result', timestamp: ts, tool_use_id: block.tool_use_id || '',
              content: resultText(block.content), is_error: !!block.is_error,
            });
          }
        }
      }
      if (!event.isMeta && !event.isVisibleInTranscriptOnly) {
        const text = stripReminders(extractText(content));
        if (text) items.push({ kind: 'user_text', timestamp: ts, text });
      }
    } else {
      const text = extractText(content);
      if (text.trim()) items.push({ kind: 'assistant_text', timestamp: ts, text });
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === 'object' && block.type === 'tool_use') {
            items.push({
              kind: 'tool_use', timestamp: ts, name: block.name || 'tool',
              input: block.input || {}, tool_use_id: block.id || '',
            });
          }
        }
      }
      if (event.error && !text.trim()) {
        items.push({
          kind: 'assistant_text', timestamp: ts,
          text: '[API 错误] ' + resultText(event.errorDetails || event.error),
        });
      }
    }
  }
  return {
    items, titles, cwd, git_branch: gitBranch, model,
    reasoning_effort: reasoningEffort, context_window: contextWindow,
    entrypoint, version, is_sidechain: isSidechain,
  };
}

function titleFor(norm, sessionId) {
  // 标题：自定义 > AI > 子代理名 > 首条用户消息首行（60 字截断）
  for (const key of ['custom', 'ai', 'agent']) if (norm.titles[key]) return norm.titles[key];
  for (const item of norm.items) {
    if (item.kind === 'user_text') {
      const first = item.text.trim().split(/\r?\n/)[0] || '';
      return first.length > 60 ? first.slice(0, 60) + '…' : (first || sessionId);
    }
  }
  return sessionId;
}

function stateWorkspaces(jsonlPath) {
  // <session-id>/state.json 的 workspaceDirectories（cwd 兜底）
  const statePath = path.join(jsonlPath.slice(0, -'.jsonl'.length), 'state.json');
  try {
    const data = JSON.parse(fs.readFileSync(statePath, 'utf-8'));
    return data && typeof data === 'object' && !Array.isArray(data)
      ? (Array.isArray(data.workspaceDirectories) ? data.workspaceDirectories.map(String) : [])
      : [];
  } catch (e) { return []; }
}

function currentMeta(filePath) {
  let events, mtime;
  try {
    events = parseJsonl(filePath);
    mtime = fs.statSync(filePath).mtimeMs; // 毫秒
  } catch (e) {
    return null;
  }
  const norm = normalize(events);
  const timestamps = norm.items.filter((i) => i.timestamp).map((i) => i.timestamp);
  const sessionId = path.basename(filePath, '.jsonl');
  const workspaces = stateWorkspaces(filePath);
  const fallback = Math.trunc(mtime);
  return {
    session_id: sessionId,
    title: titleFor(norm, sessionId),
    directory: norm.cwd || workspaces[0] || '',
    created: timestamps[0] || fallback,
    updated: timestamps[timestamps.length - 1] || fallback,
    count: norm.items.length,
    source: 'jsonl',
    path: filePath,
    mtime,
    norm,
  };
}

function legacyMeta(filePath) {
  // 旧版 *-session.json：只有元数据快照，没有可读 transcript
  let obj, mtime;
  try {
    obj = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    mtime = fs.statSync(filePath).mtimeMs; // 毫秒
  } catch (e) {
    return null;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  const name = path.basename(filePath);
  const sid = String(obj.id || name.slice(0, -'-session.json'.length));
  const fallback = Math.trunc(mtime);
  return {
    session_id: sid,
    title: obj.title || sid,
    directory: obj.working_dir || '',
    created: obj.created_at || fallback,
    updated: obj.updated_at || fallback,
    count: obj.message_count || 0,
    source: 'legacy-metadata',
    path: filePath,
    mtime,
  };
}

function scanSessions(qoderDir) {
  // 扫描 ~/.qoder/projects 全部会话，按最后活动时间倒序；
  // 旧版 *-session.json 与当前 JSONL 同 ID 时以当前为准
  const root = path.join(qoderDir, 'projects');
  const sessions = [];
  const currentIds = new Set();
  let projects;
  try {
    projects = fs.readdirSync(root).sort();
  } catch (e) {
    return sessions;
  }
  for (const name of projects) {
    const projectDir = path.join(root, name);
    let entries;
    try {
      if (!fs.statSync(projectDir).isDirectory()) continue;
      entries = fs.readdirSync(projectDir).sort();
    } catch (e) {
      continue;
    }
    // 先扫当前格式（与 resume-qoder 的 glob 顺序一致），再补旧版元数据
    for (const file of entries) {
      if (!file.endsWith('.jsonl')) continue;
      const meta = currentMeta(path.join(projectDir, file));
      if (meta) { sessions.push(meta); currentIds.add(meta.session_id); }
    }
    for (const file of entries) {
      if (!file.endsWith('-session.json')) continue;
      const meta = legacyMeta(path.join(projectDir, file));
      if (meta && !currentIds.has(meta.session_id)) sessions.push(meta);
    }
  }
  sessions.sort((a, b) => timestampMs(b.updated) - timestampMs(a.updated));
  return sessions;
}

function listSessions(opts) {
  const o = opts || {};
  const qoderDir = o.dir || defaultDir();
  let sessions = scanSessions(qoderDir);
  if (o.project) {
    const target = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === target);
  }
  return sessions.map((s) => ({
    session_id: s.session_id, title: s.title, cwd: s.directory,
    mtime: s.mtime, path: s.path,
    count: s.count, last_ts: cs.fmtCst(tsToIso(s.updated)),
  }));
}

function loadSession(opts) {
  const o = opts || {};
  const qoderDir = o.dir || defaultDir();
  let sessions = scanSessions(qoderDir);
  if (o.project) {
    const target = normPath(o.project);
    sessions = sessions.filter((s) => normPath(s.directory) === target);
  }
  if (o.session_id) {
    sessions = sessions.filter((s) => s.session_id.startsWith(o.session_id)
      || s.session_id.includes(o.session_id));
  }
  if (!sessions.length) {
    throw new cs.ConvertError(`未找到 Qoder 会话（dir=${qoderDir}`
      + (o.project ? `，project=${o.project}` : '')
      + (o.session_id ? `，session=${o.session_id}` : '') + '）');
  }

  const target = sessions[0];
  if (target.source !== 'jsonl') {
    throw new cs.ConvertError(
      `旧版 Qoder 会话只保留本地元数据快照，没有可转换的对话记录：${target.path}`);
  }
  const norm = target.norm;
  const msgs = [];
  for (const item of norm.items) {
    const ts = tsToIso(item.timestamp);
    if (item.kind === 'user_text') {
      msgs.push(cs.mMessage('user', item.text, ts));
    } else if (item.kind === 'assistant_text') {
      msgs.push(cs.mMessage('assistant', item.text, ts));
    } else if (item.kind === 'tool_use') {
      msgs.push(cs.mToolUse(item.tool_use_id || '', item.name || 'tool', item.input || {}, ts));
    } else if (item.kind === 'tool_result') {
      msgs.push(cs.mToolResult(item.tool_use_id || '', item.content || '', !!item.is_error, ts));
    } else if (item.kind === 'note') {
      msgs.push(cs.mNote(item.text || '', ts));
    }
  }
  if (!msgs.length) throw new cs.ConvertError(`会话无可转换内容：${target.path}`);

  const sid = target.session_id;
  const timestamps = norm.items.filter((i) => i.timestamp).map((i) => i.timestamp);
  const ref = cs.makeRef('qoder', sid, titleFor(norm, sid), norm.cwd || target.directory || '', {
    model: norm.model || '',
    started_at: timestamps.length ? tsToIso(timestamps[0]) : '',
    ended_at: timestamps.length ? tsToIso(timestamps[timestamps.length - 1]) : '',
    source: target.path,
  });
  return { ref, msgs };
}

module.exports = { listSessions, loadSession };
