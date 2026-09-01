---
name: resume-zcode
description: 接管/恢复一个 ZCode 会话--读取本机 ZCode 的 SQLite 会话库（~/.zcode/cli/db/db.sqlite），从 session、message、part 表重建对话，解析文本 part、工具调用与工具结果，并结合 todo 表的任务清单，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 ZCode 会话」「接管 ZCode 的任务」「把 ZCode 会话交给 Claude/Codex/OpenCode/其他模型」或在当前项目中恢复 ZCode 历史进度时使用。
---

# resume-zcode

读取 ZCode 本地会话记录，生成结构化「接管摘要」，再基于摘要和当前代码现场继续任务。

读取 `~/.zcode/cli/db/db.sqlite`：从 `session`、`message`、`part` 表重建会话，解析 `text`、`tool`、`file` part（忽略 `reasoning`、`step-start`/`step-finish`、`timeline` 等与接管无关的类型和 synthetic 系统注入文本），并读取 `todo` 表还原原会话的任务清单。始终只读访问源数据。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_zcode.js --list
   ```

2. 生成当前项目最近会话的接管摘要：

   ```bash
   node scripts/resume_zcode.js
   ```

   已知会话 ID 时，指定完整 ID 或前缀并跨项目查找：

   ```bash
   node scripts/resume_zcode.js --session <会话ID或前缀>
   ```

3. 阅读「任务清单」「任务状态重建」「近期对话」，确认目标、未完成项、已修改文件、命令和错误。

4. 重新检查当前文件系统和 Git 状态，从未完成的 Todo 项或最后一条用户消息处继续。不要逐字复述历史。

无 Node.js 22.5+ 环境时，改用 Python 标准库实现：

```bash
python -X utf8 scripts/resume_zcode.py [选项]
```

## CLI

两套脚本提供相同参数：

```text
--list                 仅列出当前项目会话
--latest               取最近一个会话（默认）
--session ID           指定会话 ID 或前缀；跨项目查找
--project PATH         项目路径，默认当前目录
--zcode-dir DIR        ZCode 主目录，默认 ~/.zcode
--recent N             近期条目数，默认 8
--max-chars N          单条内容截断长度，默认 1500
--limit N              --list 数量上限，0 不限制
--json                 输出机器可读 JSON
--output FILE          将摘要写入 UTF-8 文件
```

默认主目录为 `$ZCODE_HOME`；未设置时使用 `~/.zcode`，会话库位于 `<主目录>/cli/db/db.sqlite`。

## 输出内容

- 会话信息：标题、ID、项目、模型、Agent、ZCode 版本、会话类型、时间范围、存储格式
- 任务清单：原会话 TodoWrite 记录及各项状态（已完成/进行中/待办）
- 任务状态：目标、已调查/修改文件、执行命令、测试与错误、最近用户/助手消息
- 近期对话：文本、工具输入/输出、附件
- 更早工具活动与接管建议

## 注意事项

- Node 实现使用内置 `node:sqlite`，需要 Node.js 22.5+；Python 实现只依赖标准库 `sqlite3`。
- ZCode 的会话库与 OpenCode 的 `opencode.db` 结构同源（`session`/`message`/`part`），但任务清单在独立的 `todo` 表中，本 Skill 会一并读取。
- synthetic 文本是 ZCode 注入的系统提醒（如 TodoWrite 提示、任务通知），已从对话中过滤。
- `--session` 跨项目查找；未指定时只选择 `--project` 对应的会话，避免误接管其他项目。
- 迁移的是可序列化记录、文件系统和 Git 现场，不能迁移模型 KV cache、隐藏推理状态或运行中的进程。
- 续作是语义接续，不是逐 token 恢复；必要时重新读取关键文件验证历史结论。
