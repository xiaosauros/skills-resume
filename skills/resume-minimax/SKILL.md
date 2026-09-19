---
name: resume-minimax
description: 接管/恢复一个 MiniMax Code（mcode）会话--读取本机 ~/.minimax 下的 SQLite 会话库（v2/sqlite/runtime-state.sqlite）与规范历史 JSONL（v2/sessions/**/messages.jsonl），解析用户/助手消息、工具调用与工具结果、压缩摘要与任务清单，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 MiniMax 会话」「接管 mcode 的任务」「把 MiniMax Code 的会话交给 Claude/Grok/Codex/其他模型」或在当前项目中恢复 MiniMax Code 历史进度时使用。
---

# resume-minimax

读取 MiniMax Code（CLI 名 `mcode`）本地会话记录，解析其中的用户/助手消息、工具调用（`toolCall`）与工具结果（`toolResult`）、压缩摘要（`compactionSummary` / `archonCompaction`）与任务清单（`todoState`），生成一份结构化「接管摘要」，供当前模型作为上下文继续工作。

MiniMax Code 把数据存放在主目录 `~/.minimax`（旧版安装为 `~/.mavis`，会自动迁移并保留兼容链接；profile 变体为 `~/.minimax-<profile>`）：

- **SQLite 会话库** `v2/sqlite/runtime-state.sqlite`：`local_runtime_sessions` 表给出每个会话的标题、项目工作区（`workspace_dir`）、状态与时间；`local_runtime_message_rows` 表保存逐条显示消息；会话内压缩过的历史以 `archonCompaction` 标记（含 `todoState` 任务清单）留存。
- **规范历史 JSONL** `v2/sessions/<年/月/日>/<时间>-session_<ID>/messages.jsonl`：每行一个信封（`message_id` / `turn_id` / `message` / `turn_config`），消息角色为 `user` / `assistant` / `toolResult` / `compactionSummary`，是会话历史的持久化真源。

本 skill 优先从 SQLite 列出会话、从 JSONL 读取消息；SQLite 不可用时回退为扫描 `manifest.json` + `messages.jsonl`（此时无法判断会话所属项目）。全程只读访问。

核心逻辑在可移植脚本中，提供 **Node.js**（`scripts/resume_minimax.js`，默认）与 **Python**（`scripts/resume_minimax.py`）两套等价实现，**不依赖任何模型专属 API**，因此 Claude Code、Codex、Grok 等任意 agent 均可直接调用。两套实现输出完全一致，可互换；按目标平台选择其一即可。

## 何时使用

- 用户想继续/恢复某个 MiniMax Code（mcode）会话的未完成工作
- 需要把一个进行中的 MiniMax Code 会话交接给当前（可能不同的）模型
- 在当前项目中查找 MiniMax Code 历史进度并接续

## 使用步骤

1. （可选）列出当前项目的会话：
   ```bash
   node scripts/resume_minimax.js --list
   ```

2. 生成接管摘要（默认取当前项目最近一个会话）：
   ```bash
   node scripts/resume_minimax.js
   ```
   指定会话（支持 ID 前缀、跨项目全局查找）：
   ```bash
   node scripts/resume_minimax.js --session <会话ID或前缀>
   ```

3. 阅读脚本输出的「接管摘要」，理解目标、已完成的工作、剩余问题（含压缩前留存的任务清单）。

4. 基于摘要与当前代码现场（文件系统 + Git）继续完成任务。不要逐字复述历史，而是从当前状态接续--必要时重新读取相关文件确认现状，再决定下一步。

> 无 Node.js 环境时改用 Python：`python -X utf8 scripts/resume_minimax.py ...`，参数与输出完全相同。

## 输出说明

摘要包含：

- **会话信息**：标题、ID、项目路径、Agent、模型、会话类型、状态、时间范围、消息条目数
- **历史摘要**：会话内压缩（compact）留下的最近一次摘要内容（若存在）
- **任务状态重建**：目标、任务清单（来自压缩快照的 `todoState`）、已调查文件、代码修改、执行命令、测试/错误结果、最近用户·助手消息
- **近期对话**：最近若干轮原始内容（含工具调用与结果，已截断）
- **更早活动**：超出近期窗口的工具调用紧凑列表
- **接管建议**（含 mcode 原生续接命令提示）

## 会话标题解析

按优先级解析会话名称：

1. SQLite `local_runtime_sessions.title`（MiniMax Code 侧边栏显示的会话标题，通常由模型自动生成）
2. 兜底：会话内首条用户消息摘要；若该会话以压缩边界开头，则取压缩摘要文本
3. 最终兜底：会话 ID

## 其他 agent 使用（Claude Code / Grok / Codex 等）

非 MiniMax Code 的 agent 无需 skill 机制，直接运行脚本，将 stdout 作为上下文喂给模型即可：

```bash
node scripts/resume_minimax.js [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

无 Node.js 时改用 Python（参数完全相同）：

```bash
python -X utf8 scripts/resume_minimax.py [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

> `--session` 指定后会优先在 `~/.minimax` 下所有项目中查找对应会话，无需关心是否跨项目。

## 注意事项

- 该 skill 迁移的是**可序列化的外部记录**（transcript + 文件系统 + Git 状态），无法迁移模型 KV cache、内部推理状态或运行中的子进程。
- 续作是**语义上的接续**，不是逐 token 精确恢复；不同模型继续后决策可能不同。
- 默认读取 `~/.minimax`（不存在时尝试旧版 `~/.mavis`），可通过 `MINIMAX_DATA_DIR` 环境变量或 `--minimax-dir` 覆盖。
- 若想让 MiniMax Code 自己原生续接会话，可用 `mcode --resume <会话ID>` 或在对应目录下 `mcode -c`（继续最近会话）。
- 以只读方式访问会话库与 JSONL，MiniMax Code 运行时也可安全读取；SQLite 库为 WAL 模式，若只读打开失败脚本会自动回退到 JSONL 扫描。
- Node 实现使用 SQLite 时需要 Node.js 22.5+ 的内置 `node:sqlite`（较低版本会自动降级为仅 JSONL 模式并在提示中说明）；Python 实现仅用标准库（Python 3.7+，Windows 中文环境建议 `python -X utf8`）。
- 文件（JSONL-only）模式下无标题与项目路径信息，`--list` 会显示全部会话并给出提示。
