---
name: resume-continue
description: 接管/恢复一个 Continue（VS Code/JetBrains 扩展或 cn CLI）会话——读取本机 ~/.continue/sessions 下的会话 JSON（IDE 扩展与 CLI 共用），解析用户/助手消息、@引用上下文、工具调用（toolCallStates）与工具结果、压缩摘要（conversationSummary），生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Continue 会话」「接管 Continue 的任务」「把 Continue 里做到一半的任务交给当前模型」时使用。
---

# resume-continue

读取 Continue（开源 AI 代码助手，[continuedev/continue](https://github.com/continuedev/continue)）本地会话记录，解析其中的用户/助手消息、@引用上下文（contextItems）、工具调用（`toolCallStates`，含内置工具与 MCP 工具）与工具结果、压缩摘要（`conversationSummary`），生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

Continue 的 IDE 扩展（VS Code / JetBrains）与 CLI（`cn`）共用同一套会话存储：主目录 `~/.continue`（受 `CONTINUE_GLOBAL_DIR` 环境变量影响）下的 `sessions/` 目录，每个会话一个 `<UUID>.json` 文件（含 `sessionId`、`title`、`workspaceDirectory`、`history`、`mode`、`chatModelTitle`、`usage`），旁边另有 `sessions.json` 元数据列表。历史条目为 `{message, contextItems, toolCallStates, conversationSummary}` 结构，消息角色为 `user` / `assistant` / `thinking` / `system` / `tool`。全程只读访问。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_continue.js`，默认）与 **Python**（`scripts/resume_continue.py`）两套等价实现，**不依赖任何模型专属 API、无第三方依赖**，因此 Claude Code、Codex、ZCode 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 何时使用

- 用户想继续/恢复某个 Continue 会话的未完成工作
- 需要把一个进行中的 Continue 任务交接给当前（可能不同的）模型
- 在当前项目中查找 Continue 历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_continue.js --list
   ```

2. 生成接管摘要（默认取当前项目最近一个会话）：
   ```bash
   node scripts/resume_continue.js
   ```
   指定会话（支持 ID 前缀、跨项目全局查找）：
   ```bash
   node scripts/resume_continue.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题（含压缩前留存的摘要内容）。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续——必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_continue.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、ID、项目路径（workspaceDirectory）、模式（chat/agent/plan/background）、聊天模型、累计费用与 Token 用量、时间范围、消息条目数
- **历史摘要**：会话内压缩（compact）留下的最近摘要内容（若存在）
- **任务状态重建**：目标、已调查文件、代码修改（内置编辑工具 + 含 edit/create 语义的 MCP 工具）、执行命令、测试/错误结果、最近用户·助手消息
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**（含 Continue 原生续接方式提示）

## 工具识别

Continue 内置工具存在新版 snake_case（`read_file` / `edit_existing_file` / `run_terminal_command` 等）与旧版 camelCase（`readFile` / `editExistingFile` / `runTerminalCommand`）两套命名，脚本均能识别并归类为读取/编辑/终端三类；未收录的 MCP 等自定义工具按名称启发式（edit/terminal/read 等关键词）归类。

## 其他 agent 使用（Claude Code / Codex 等）

非 Continue 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_continue.js [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_continue.py [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

> `--session` 指定后会优先在所有会话文件中查找对应会话，无需关心是否跨项目。
> `--continue-dir` 可覆盖 Continue 主目录（指向 `~/.continue` 一级）或直接指向 `sessions` 目录；环境变量 `CONTINUE_DATA_DIR` 等效。

## 注意事项

- 该 skill 迁移的是**可序列化的外部记录**（会话 JSON + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- 会话条目本身**不带时间戳**，摘要中的时间范围来自会话文件的创建/修改时间（辅以 `sessions.json` 的 `dateCreated`），条目不标注时刻。
- Continue 的压缩（compact）只保留摘要文本，压缩前的逐条历史在 CLI 会话中不落盘；旧版 GUI 会话（`sessions.json` 中带 `session_id` 的旧格式条目）也能被兼容解析。
- `thinking` / `system` 角色的内容不进入摘要；`thinking` 属模型内部推理，无需接管。
- 若想让 Continue 自己原生续接会话：CLI 在原目录用 `cn --resume`（继续最近会话）或 `cn ls`（交互选择）；IDE 在 Continue 侧边栏的会话历史（History）中选中该会话。
- 两套实现均为纯标准库实现（Python 3.7+；Node.js 无版本要求，无需 `node:sqlite`），Windows 中文环境建议 `python -X utf8`。
