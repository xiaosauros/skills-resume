---
name: resume-openclaw
description: 接管/恢复一个 OpenClaw（原 Clawdbot/Moltbot，开源个人 AI 助手）会话——读取本机 OpenClaw 状态目录（~/.openclaw）下每 agent 的 openclaw-agent.sqlite 会话库（session_nodes / transcript_events / 活动分支投影），兼容旧版 sessions.json + JSONL transcript 与更早的 ~/.clawdbot 目录，解析用户/助手消息、工具调用（toolCall）与工具结果（toolResult）、compaction 压缩摘要、goal 目标状态，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 OpenClaw 会话」「接管 OpenClaw/Clawdbot 的任务」「把 OpenClaw 会话交给 Claude/ZCode/Codex/其他模型」或在当前项目中恢复 OpenClaw 历史进度时使用。
---

# resume-openclaw

读取 OpenClaw 本地会话记录，生成结构化「接管摘要」，再基于摘要和当前代码现场继续任务。

OpenClaw 是开源的个人 AI 助手（[openclaw/openclaw](https://github.com/openclaw/openclaw)，原 Clawdbot / Moltbot），会话按 agent 持久化。当前版本使用每 agent 的 SQLite 库 `agents/<agentId>/agent/openclaw-agent.sqlite`：`session_nodes` 表存会话元数据（entry_json），`transcript_events` 表存树状 transcript（每条 event_json 带 id/parentId，`session_transcript_active_events` 投影指出活动分支），`session_windows` 表维护 session_id ↔ session_key。旧版本使用 JSON：`sessions/sessions.json`（sessionKey → SessionEntry 对象映射）与 `sessions/<sessionId>.jsonl`（首行 `{type:"session",cwd,...}` 会话头 + 条目树，leaf 条目指向当前分支）。全程只读访问。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_openclaw.js --list
   ```

2. 生成当前项目最近会话的接管摘要：

   ```bash
   node scripts/resume_openclaw.js
   ```

   已知会话 ID 时，指定完整 ID 或前缀并跨项目查找：

   ```bash
   node scripts/resume_openclaw.js --session <会话ID或前缀>
   ```

3. 阅读「任务状态重建」「历史摘要」「近期对话」，确认目标、已修改文件、命令和错误。

4. 重新检查当前文件系统和 Git 状态，从最后一条用户消息或剩余问题处继续。不要逐字复述历史。

无 Node.js 22.5+ 环境时，改用 Python 标准库实现：

```bash
python -X utf8 scripts/resume_openclaw.py [选项]
```

## CLI

两套脚本提供相同参数：

```text
--list                 仅列出当前项目会话
--latest               取最近一个会话（默认）
--session ID           指定会话 ID 或前缀；跨项目查找
--project PATH         项目路径，默认当前目录
--openclaw-dir DIR     OpenClaw 状态目录（~/.openclaw 一级）
--recent N             近期条目数，默认 8
--max-chars N          单条内容截断长度，默认 1500
--limit N              --list 数量上限，0 不限制
--json                 输出机器可读 JSON
--output FILE          将摘要写入 UTF-8 文件
```

状态目录按序探测：`OPENCLAW_STATE_DIR` 环境变量 > `~/.openclaw` > 旧版 `~/.clawdbot`。

会话来源（同一会话 ID 优先取 SQLite，避免迁移后重复）：

1. 每 agent SQLite：`agents/<agentId>/agent/openclaw-agent.sqlite`（`session_nodes` + `transcript_events`）
2. 每 agent 旧版：`agents/<agentId>/sessions/sessions.json` + `<sessionId>.jsonl`
3. 根级旧版：`sessions/sessions.json` + `<sessionId>.jsonl`

## 输出内容

- 会话信息：标题、会话 ID、sessionKey（渠道/来源，如 main、whatsapp:…）、agent、项目、模型、Token 用量与费用、运行状态、时间范围、存储位置
- 历史摘要：transcript 中的 compaction / branch_summary 摘要与元数据 compactionCheckpoints 摘要
- 任务状态：目标（首条用户消息）、已调查/修改文件、执行命令、测试与错误、goal 字段状态、最近用户/助手消息
- 近期对话：用户/助手文本、工具调用输入与结果（含错误）、助手报错、插件消息
- 更早工具活动与接管建议

## 注意事项

- Node 实现使用内置 `node:sqlite`，需要 Node.js 22.5+；Python 实现只依赖标准库 `sqlite3`。
- SQLite 会话正文优先按 `session_transcript_active_events` 活动分支投影读取（与模型实际上下文一致），投影缺失时回退为按 seq 全量读取；JSONL 则沿最后一条 leaf 指针回溯父链，指针失效时回退为文件顺序。
- OpenClaw 的项目匹配依赖会话元数据中的 cwd 痕迹（spawnedCwd / execCwd / worktree.repoRoot / sessionDiffBaseline.root / JSONL 会话头 cwd）；渠道会话（WhatsApp/Telegram 等）可能没有项目目录，此时用 `--session` 指定。
- 会话可能属于多个 agent 目录（多 agent 配置）；`--list` 输出中会标注来源 agent。
- 迁移的是可序列化记录、文件系统和 Git 现场，不能迁移模型 KV cache、隐藏推理状态或运行中的进程。
- 续作是语义接续，不是逐 token 恢复；必要时重新读取关键文件验证历史结论。
