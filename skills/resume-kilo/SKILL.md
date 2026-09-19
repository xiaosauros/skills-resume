---
name: resume-kilo
description: 接管/恢复一个 Kilo Code（VS Code 扩展或 Kilo CLI）会话——读取本机 Kilo 数据目录下的 kilo.db（opencode 风格 SQLite）与旧版 VS Code 扩展任务目录（api_conversation_history.json），解析消息、工具调用、工具结果、compact 摘要与 todo，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Kilo 会话」「接管 kilo 的任务」「把 Kilo Code 里做到一半的任务交给当前模型」时使用。
---

# resume-kilo

读取 Kilo Code 本地会话记录，解析其中的消息、工具调用、工具结果、compact 摘要与 todo，生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_kilo.js`，默认）与 **Python**（`scripts/resume_kilo.py`）两套等价实现，**不依赖任何模型专属 API**，因此 Kilo、Claude Code、ZCode、Codex 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 数据来源（按优先级）

1. **kilo.db（SQLite，当前格式）**：Kilo CLI 与新版 VS Code 扩展共用的会话库，位于 Kilo 数据目录（默认 `~/.local/share/kilo`，或 `$XDG_DATA_HOME/kilo`、`$KILO_DATA_DIR`）下。库内同时兼容 v1（`message`/`part` 表）与 v2（`session_message` 投影）两种消息存储，并读取 `todo` 任务清单。数据库文件按 `kilo.db`、`kilo-<channel>.db`、`opencode-<channel>.db` 探测（与 Kilo 自身 `KILO_DB` / 安装渠道的命名规则一致），多库时取最近修改的。
2. **旧版 VS Code 扩展任务目录（legacy）**：`<编辑器 globalStorage>/kilocode.kilo-code/tasks/<任务ID>/api_conversation_history.json`（Anthropic 风格消息数组，可含 `history_item.json`、`_index.json` 元数据）。自动探测 VS Code、VS Code Insiders、VSCodium、Cursor、Windsurf、Trae 的 globalStorage 目录。若某旧任务已迁移进 kilo.db（迁移会话 ID 为 `ses_migrated_<sha1>`），会自动去重跳过。

## 何时使用

- 用户想继续/恢复某个 Kilo Code 会话的未完成工作
- 需要把一个进行中的 Kilo 任务交接给当前（可能不同的）模型
- 在当前项目中查找历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_kilo.js --list
   ```

2. 生成接管摘要（默认取最近一个会话）：
   ```bash
   node scripts/resume_kilo.js
   ```
   指定会话（支持 ID 前缀、跨项目、跨来源查找）：
   ```bash
   node scripts/resume_kilo.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续——必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_kilo.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、ID、项目路径、Agent、模型、Kilo 版本、累计费用与 Token 用量、归档状态、时间范围、消息数
- **历史摘要**：原会话中的 compact 摘要（若存在）
- **任务状态重建**：目标、todo 任务清单、已调查文件、执行命令、代码修改、测试结果、剩余问题
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**

## 其他 agent 使用（Claude Code / Codex 等）

非 Kilo 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_kilo.js [--list|--latest|--session ID] [--project PATH] [--kilo-dir DIR] [--tasks-dir DIR] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_kilo.py [--list|--latest|--session ID] [--project PATH] [--kilo-dir DIR] [--tasks-dir DIR] [--limit N] [--json] [--output FILE]
```

> `--session` 指定后会优先在所有数据来源中查找对应会话，无需关心是否跨项目。
> `--kilo-dir` 覆盖 Kilo 数据目录（指向含 kilo.db 的一级）；`--tasks-dir` 覆盖旧版任务目录（可指向 `tasks` 目录本身或其上层 globalStorage）。

## 注意事项

- 该 skill 迁移的是**可序列化的外部记录**（会话数据库/JSON + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- Node 实现需要 Node.js 22.5+ 的内置 `node:sqlite`；较低版本 Node 请直接运行 Python 脚本。
- 旧版任务目录的消息不带时间戳，摘要中的时间范围来自任务 ID（创建时刻）与目录修改时间，条目不标注时刻。
- 旧版任务的用户消息中由 Kilo/Cline 注入的 `<environment_details>`、`<system-reminder>` 等环境块会被剔除，`<task>` 包裹的任务正文会被解出。
