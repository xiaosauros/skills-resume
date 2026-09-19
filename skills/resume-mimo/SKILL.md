---
name: resume-mimo
description: 接管/恢复一个 MiMo-Code（小米 MiMo 终端 AI 编码助手）会话——读取本机 mimocode 数据目录（~/.local/share/mimocode）下的 mimocode.db SQLite 会话库，解析消息、文本、工具调用与结果、patch、compact 摘要、checkpoint 与子任务，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 MiMo 会话」「接管 MiMoCode/mimo 的任务」「把 MiMo 会话交给 Claude/ZCode/Codex/其他模型」或在当前项目中恢复 MiMo 历史进度时使用。
---

# resume-mimo

读取 MiMo-Code 本地会话记录，生成结构化「接管摘要」，再基于摘要和当前代码现场继续任务。

MiMo-Code 是小米的开源终端 AI 编码助手（OpenCode fork，命令 `mimo`），会话持久化在 SQLite 库中：从 `session`、`message`、`part` 表重建会话，解析 `text`、`tool`、`patch`、`file`、`compaction`（compact 摘要）、`checkpoint`、`subtask` 等 part，并可读取会话的 todo 任务清单。始终只读访问源数据。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_mimo.js --list
   ```

2. 生成当前项目最近会话的接管摘要：

   ```bash
   node scripts/resume_mimo.js
   ```

   已知会话 ID 时，指定完整 ID 或前缀并跨项目查找：

   ```bash
   node scripts/resume_mimo.js --session <会话ID或前缀>
   ```

3. 阅读「任务状态重建」「历史摘要」「近期对话」，确认目标、已修改文件、命令和错误。

4. 重新检查当前文件系统和 Git 状态，从最后一条用户消息或剩余问题处继续。不要逐字复述历史。

无 Node.js 22.5+ 环境时，改用 Python 标准库实现：

```bash
python -X utf8 scripts/resume_mimo.py [选项]
```

## CLI

两套脚本提供相同参数：

```text
--list                 仅列出当前项目会话
--latest               取最近一个会话（默认）
--session ID           指定会话 ID 或前缀；跨项目查找
--project PATH         项目路径，默认当前目录
--mimo-dir DIR         MiMo 数据目录（含 mimocode.db）
--recent N             近期条目数，默认 8
--max-chars N          单条内容截断长度，默认 1500
--limit N              --list 数量上限，0 不限制
--json                 输出机器可读 JSON
--output FILE          将摘要写入 UTF-8 文件
```

数据目录按序探测（取第一个含 `mimocode.db` 的目录）：

1. `MIMOCODE_HOME`（使用其下 `data/` 子目录，MiMo 官方覆盖机制）
2. `$XDG_DATA_HOME/mimocode`
3. `~/.local/share/mimocode`（Linux/Windows 默认，macOS 亦适用）
4. `%LOCALAPPDATA%\mimocode`（Windows 回退，README 提及）
5. `~/Library/Application Support/mimocode`（macOS 回退）

数据库文件探测顺序：`MIMOCODE_DB` 环境变量 > `mimocode.db` > `mimocode-<channel>.db`（其他发布渠道，取最近修改）。

## 输出内容

- 会话信息：标题、ID、项目、工作区、模型、Agent、MiMo 版本、时间范围、数据库文件
- 历史摘要：原会话 compact 摘要（compaction part 的 projection.summary 或 synthetic text）
- 任务状态：目标、已调查/修改文件、执行命令、测试与错误、todo 任务清单、最近用户/助手消息
- 近期对话：文本、工具输入/输出、补丁、附件、检查点边界与子任务委派
- 更早工具活动与接管建议

## 注意事项

- Node 实现使用内置 `node:sqlite`，需要 Node.js 22.5+；Python 实现只依赖标准库 `sqlite3`。
- `--session` 跨项目查找；未指定时只选择 `--project` 对应的会话，避免误接管其他项目。
- MiMo 的持久记忆（MEMORY.md、checkpoint）存储在数据目录与项目内，不在会话库中；如需延续其记忆，可在接管后另行查看 `~/.local/share/mimocode` 与项目 `.mimocode/` 目录。
- 迁移的是可序列化记录、文件系统和 Git 现场，不能迁移模型 KV cache、隐藏推理状态或运行中的进程。
- 续作是语义接续，不是逐 token 恢复；必要时重新读取关键文件验证历史结论。
