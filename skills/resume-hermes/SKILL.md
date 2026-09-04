---
name: resume-hermes
description: 接管/恢复一个 Hermes Agent（NousResearch hermes-agent，CLI/桌面/网关）会话——读取本机 Hermes 主目录（%LOCALAPPDATA%\hermes 或 ~/.hermes）下 state.db 中的会话与消息，解析消息、工具调用、工具结果、压缩交接摘要与压缩续链，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Hermes 会话」「接管 hermes 的任务」「把 hermes 的会话交给其他模型」或在当前项目中恢复历史进度时使用。
---

# resume-hermes

读取 Hermes Agent 本地会话库（`<Hermes主目录>/state.db` 的 `sessions`/`messages` 表），解析其中的消息、工具调用、工具结果、压缩交接摘要与压缩续链，生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_hermes.js`，默认，需 Node.js 22.5+ 的内置 `node:sqlite`）与 **Python**（`scripts/resume_hermes.py`）两套等价实现，**不依赖任何模型专属 API**，因此 hermes、Claude Code、Grok、Codex 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 何时使用

- 用户想继续/恢复某个 hermes 会话的未完成工作
- 需要把一个进行中的 hermes 会话交接给当前（可能不同的）模型
- 在当前项目中查找 hermes 的历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_hermes.js --list
   ```
   全部会话（含微信/Telegram 等网关来源、无项目路径的会话）：
   ```bash
   node scripts/resume_hermes.js --list-all
   ```

2. 生成接管摘要（默认取当前项目最近一个会话）：
   ```bash
   node scripts/resume_hermes.js
   ```
   指定会话（支持 ID 前缀、跨项目全局查找，可命中压缩续链上的任意一段）：
   ```bash
   node scripts/resume_hermes.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续——必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_hermes.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、根/当前会话 ID、压缩续链段数、来源（cli/desktop/weixin/…）、项目、Git 分支、模型、时间范围、消息数
- **历史摘要**：原会话中的压缩交接摘要（CONTEXT COMPACTION，若存在）
- **任务状态重建**：目标、已调查文件、执行命令、代码修改、测试结果、剩余问题
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**

## 会话链与压缩语义

hermes 的一个「逻辑会话」可能是 SQLite 中的多行记录：

- **压缩续链**：上下文压缩后新会话经 `parent_session_id` 链到原会话（父会话 `end_reason='compression'`）。脚本沿链折叠——列表中显示为一项（标注续链次数），摘要覆盖整条链；`--session` 用根 ID 或链尾 ID 都能命中。
- **分支会话**（`/branch`，`model_config._branched_from`）：作为独立会话列出。
- **delegate 子 agent 会话**（`model_config._delegate_from`）：子 agent 的运行记录，不进入列表与摘要。
- 被压缩掉的原始消息（`compacted=1` / `active=0`）不展开，仅统计条数；压缩交接摘要本身会归入「历史摘要」。

## 其他 agent 使用（Grok / Codex 等）

非 hermes 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_hermes.js [--list|--list-all|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_hermes.py [--list|--list-all|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

> 网关/IM 来源的会话（微信、Telegram 等）没有项目路径，`--list` 找不到时用 `--list-all` 或 `--session ID` 定位。

## 注意事项

- Hermes 主目录解析与官方一致：环境变量 `HERMES_HOME`，否则 Windows 为 `%LOCALAPPDATA%\hermes`，macOS/Linux 为 `~/.hermes`；也可用 `--hermes-dir` 覆盖。
- 该 skill 迁移的是**可序列化的外部记录**（state.db + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程；hermes 自身的记忆（MEMORY.md 等）如与任务相关需另行读取。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- 压缩交接摘要按 hermes 的约定**仅作背景参考**：以最后一条真实用户消息为准，勿把摘要里的历史待办当作当前任务。
- 若想在 Hermes 原生环境继续，可直接运行 `hermes --resume <会话ID>`；本 skill 面向的是跨工具接管。
- 兼容新旧 schema：脚本按实际存在的列查询（旧版无 `_compressed_summary`/`display_name` 等列时自动降级）。
