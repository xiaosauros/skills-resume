---
name: resume-claude
description: 接管/恢复一个 Claude Code 会话——读取本机 ~/.claude/projects 下的 JSONL 会话记录，解析消息、工具调用、工具结果与摘要，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Claude 会话」「接管 Claude 的任务」「把 Claude 的会话交给 Grok/其他模型」或在当前项目中恢复历史进度时使用。
---

# resume-claude

读取 Claude Code 本地会话记录（`~/.claude/projects/<项目>/*.jsonl`），解析其中的消息、工具调用、工具结果与摘要，生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_claude.js`，默认）与 **Python**（`scripts/resume_claude.py`）两套等价实现，**不依赖任何模型专属 API**，因此 Claude Code、Grok、Codex 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 何时使用

- 用户想继续/恢复某个 Claude Code 会话的未完成工作
- 需要把一个进行中的 Claude 会话交接给当前（可能不同的）模型
- 在当前项目中查找历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_claude.js --list
   ```

2. 生成接管摘要（默认取最近一个会话）：
   ```bash
   node scripts/resume_claude.js
   ```
   指定会话（支持跨项目全局查找）：
   ```bash
   node scripts/resume_claude.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续——必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_claude.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、ID、项目路径、Git 分支、时间范围、消息数
- **历史摘要**：原会话中的 compact 摘要（若存在）
- **任务状态重建**：目标、已调查文件、执行命令、代码修改、测试结果、剩余问题
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**

## 会话标题解析

按优先级解析会话名称：

1. `custom-title`（手动设置标题）
2. `ai-title`（自动生成标题）
3. `agent-name`（agent 名称）
4. 兜底：首条用户消息摘要或会话 ID

## 其他 agent 使用（Grok / Codex 等）

非 Claude Code 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_claude.js [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_claude.py [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

> `--session` 指定后会优先在 `~/.claude/projects` 下所有项目中查找对应会话，无需关心是否跨项目。

## 注意事项

- 该 skill 迁移的是**可序列化的外部记录**（transcript + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- 默认读取 `~/.claude`，可通过 `CLAUDE_CONFIG_DIR` 环境变量或 `--claude-dir` 覆盖。
