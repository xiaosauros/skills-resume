---
name: resume-kimi
description: 接管/恢复一个 Kimi Code CLI 会话--读取本机 ~/.kimi-code 下的 session_index、state.json 与 wire.jsonl 会话记录，解析用户/助手消息、工具调用与工具结果，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Kimi 会话」「接管 Kimi Code 的任务」「把 Kimi 的会话交给 Claude/Grok/Codex/其他模型」或在当前项目中恢复 Kimi Code 历史进度时使用。
---

# resume-kimi

读取 Kimi Code CLI 本地会话记录（`~/.kimi-code/session_index.jsonl` + `~/.kimi-code/sessions/<workspace>/<session>/state.json` + `agents/main/wire.jsonl`），解析其中的用户/助手消息、工具调用（`tool.call`）与工具结果（`tool.result`），生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

Kimi Code CLI 把会话按工作区组织在 `sessions/<wd_id>/<session_id>/` 下，每条会话的元数据（标题、时间）在 `state.json`，实际对话流（wire 协议事件）在 `agents/main/wire.jsonl`；`session_index.jsonl` 给出每个会话的目录与所属项目路径，用于按项目定位会话。本 skill 以只读方式读取这些文件。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_kimi.js`，默认）与 **Python**（`scripts/resume_kimi.py`）两套等价实现，**不依赖任何模型专属 API**，因此 Claude Code、Codex、Grok 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 何时使用

- 用户想继续/恢复某个 Kimi Code CLI 会话的未完成工作
- 需要把一个进行中的 Kimi Code 会话交接给当前（可能不同的）模型
- 在当前项目中查找 Kimi Code 历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_kimi.js --list
   ```

2. 生成接管摘要（默认取最近一个会话）：
   ```bash
   node scripts/resume_kimi.js
   ```
   指定会话（支持跨项目全局查找）：
   ```bash
   node scripts/resume_kimi.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续--必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_kimi.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、ID、项目路径、协议版本、模型、时间范围、消息数
- **任务状态重建**：目标、已调查文件、代码修改、执行命令、测试/错误结果、最近用户·助手消息
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**

## 会话标题解析

按优先级解析会话名称：

1. `state.json` 的 `title`（Kimi Code 侧边栏显示的会话标题）
2. 兜底：首条用户消息摘要或会话 ID

`"New Session"` 是未生成标题时的占位符，视为无标题走兜底。

## 其他 agent 使用（Claude Code / Grok / Codex 等）

非 Kimi Code 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_kimi.js [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_kimi.py [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

> `--session` 指定后会优先在 `~/.kimi-code` 下所有项目中查找对应会话，无需关心是否跨项目。

## 注意事项

- 该 skill 迁移的是**可序列化的外部记录**（transcript + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- 默认读取 `~/.kimi-code`，可通过 `KIMI_HOME` 环境变量或 `--kimi-dir` 覆盖。
- 以只读方式访问会话文件，Kimi Code 运行时也可安全读取。
- Node 实现零第三方依赖（仅用内置模块，任意 Node.js 版本即可）；Python 实现仅用标准库（Python 3.7+，Windows 中文环境建议 `python -X utf8`）。
