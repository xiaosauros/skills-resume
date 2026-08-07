---
name: resume-opencode
description: 接管/恢复一个 OpenCode 会话--读取本机 OpenCode 的 SQLite 会话库（opencode.db），兼容旧版 storage/session、message、part JSON 目录，解析消息、文本 part、工具调用与工具结果，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 OpenCode 会话」「接管 OpenCode 的任务」「把 OpenCode 会话交给 Claude/Qoder/Codex/其他模型」或在当前项目中恢复 OpenCode 历史进度时使用。
---

# resume-opencode

读取 OpenCode 本地会话记录，生成结构化「接管摘要」，再基于摘要和当前代码现场继续任务。

优先读取当前格式 `~/.local/share/opencode/opencode.db`：从 `session`、`message`、`part` 表重建会话，并解析 `text`、`tool`、`patch`、`file` part。数据库不存在时，回退读取旧版 `storage/session`、`storage/message`、`storage/part` JSON 目录。始终只读访问源数据。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_opencode.js --list
   ```

2. 生成当前项目最近会话的接管摘要：

   ```bash
   node scripts/resume_opencode.js
   ```

   已知会话 ID 时，指定完整 ID 或前缀并跨项目查找：

   ```bash
   node scripts/resume_opencode.js --session <会话ID或前缀>
   ```

3. 阅读「任务状态重建」「历史摘要」「近期对话」，确认目标、已修改文件、命令和错误。

4. 重新检查当前文件系统和 Git 状态，从最后一条用户消息或剩余问题处继续。不要逐字复述历史。

无 Node.js 22.5+ 环境时，改用 Python 标准库实现：

```bash
python -X utf8 scripts/resume_opencode.py [选项]
```

## CLI

两套脚本提供相同参数：

```text
--list                 仅列出当前项目会话
--latest               取最近一个会话（默认）
--session ID           指定会话 ID 或前缀；跨项目查找
--project PATH         项目路径，默认当前目录
--opencode-dir DIR     OpenCode 数据目录
--recent N             近期条目数，默认 8
--max-chars N          单条内容截断长度，默认 1500
--limit N              --list 数量上限，0 不限制
--json                 输出机器可读 JSON
--output FILE          将摘要写入 UTF-8 文件
```

默认数据目录为 `$OPENCODE_DATA_DIR`；未设置时使用 `$XDG_DATA_HOME/opencode`；两者都未设置时使用 `~/.local/share/opencode`。

## 输出内容

- 会话信息：标题、ID、项目、模型、Agent、OpenCode 版本、时间范围、存储格式
- 原会话 compact 摘要（存在时）
- 任务状态：目标、已调查/修改文件、执行命令、测试与错误、最近用户/助手消息
- 近期对话：文本、工具输入/输出、补丁与附件
- 更早工具活动与接管建议

## 注意事项

- Node 实现使用内置 `node:sqlite`，需要 Node.js 22.5+；Python 实现只依赖标准库 `sqlite3`。
- `--session` 跨项目查找；未指定时只选择 `--project` 对应的会话，避免误接管其他项目。
- 迁移的是可序列化记录、文件系统和 Git 现场，不能迁移模型 KV cache、隐藏推理状态或运行中的进程。
- 续作是语义接续，不是逐 token 恢复；必要时重新读取关键文件验证历史结论。
