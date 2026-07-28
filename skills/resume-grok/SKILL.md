---
name: resume-grok
description: 接管/恢复一个 Grok Build CLI 会话--读取本机 ~/.grok 下的 summary.json 与 chat_history.jsonl（必要时回退 events.jsonl / updates.jsonl 的 ACP 事件流），解析用户/助手消息、工具调用与工具结果，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Grok 会话」「接管 Grok Build 的任务」「把 Grok 的会话交给 Claude/Kimi/Codex/其他模型」或在当前项目中恢复 Grok 历史进度时使用。
---

# resume-grok

读取 Grok Build CLI 本地会话记录（`~/.grok/sessions/<encoded-cwd>/<session-id>/summary.json` + `chat_history.jsonl`，必要时回退 `events.jsonl` / `updates.jsonl` 的 ACP 事件流），解析其中的用户/助手消息、工具调用（`tool_use`）与工具结果（`tool_result`），生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

Grok Build CLI 把会话按工作目录（URL 编码后的目录名）分组，每个会话是一个 UUID 目录。`summary.json` 保存会话元数据（标题、模型、时间、Git 信息、Agent 等），实际对话流在 `chat_history.jsonl`（发给模型的原始消息，Anthropic 风格 content block）；当 `chat_history.jsonl` 为空或缺失时，回退到 ACP 事件流 `events.jsonl`（或 `updates.jsonl`）。本 skill 以只读方式读取这些文件。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_grok.js`，默认）与 **Python**（`scripts/resume_grok.py`）两套等价实现，**不依赖任何模型专属 API**，因此 Claude Code、Kimi、Codex 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 何时使用

- 用户想继续/恢复某个 Grok Build CLI 会话的未完成工作
- 需要把一个进行中的 Grok Build 会话交接给当前（可能不同的）模型
- 在当前项目中查找 Grok Build 历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_grok.js --list
   ```

2. 生成接管摘要（默认取最近一个会话）：
   ```bash
   node scripts/resume_grok.js
   ```
   指定会话（支持跨项目全局查找）：
   ```bash
   node scripts/resume_grok.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续--必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_grok.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、ID、项目路径、模型、Agent、推理强度、Git 分支、时间范围、消息数
- **任务状态重建**：目标、已调查文件、代码修改、执行命令、测试/错误结果、最近用户·助手消息
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**

## 会话标题解析

按优先级解析会话名称：

1. `summary.json` 的 `generated_title`（Grok 模型生成的标题）
2. `summary.json` 的 `title` / `session_summary`
3. 兜底：首条用户消息摘要或会话 ID

## 其他 agent 使用（Claude Code / Kimi / Codex 等）

非 Grok Build 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_grok.js [--list|--latest|--session ID] [--project PATH] [--grok-dir DIR] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_grok.py [--list|--latest|--session ID] [--project PATH] [--grok-dir DIR] [--limit N] [--json] [--output FILE]
```

> `--session` 指定后会优先在 `~/.grok/sessions` 下所有项目中查找对应会话，无需关心是否跨项目。

## 注意事项

- 该 skill 迁移的是**可序列化的外部记录**（transcript + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- 默认读取 `~/.grok`，可通过 `GROK_HOME` 环境变量或 `--grok-dir` 覆盖。
- 以只读方式访问会话文件，Grok Build 运行时也可安全读取。
- Node 实现零第三方依赖（仅用内置模块，任意 Node.js 版本即可）；Python 实现仅用标准库（Python 3.7+，Windows 中文环境建议 `python -X utf8`）。
