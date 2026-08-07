---
name: resume-agy
description: 接管/恢复一个 Antigravity CLI（agy）会话--读取本机 ~/.gemini/antigravity/brain 下的 transcript.jsonl 与任务产物，解析用户/助手消息、工具调用、命令、文件操作、工具结果和错误，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 agy/Antigravity 会话」「接管 Antigravity CLI 的任务」「把 agy 会话交给 Claude/Grok/Codex/其他模型」或在当前项目中恢复 Antigravity 历史进度时使用。
---

# resume-agy

读取 Antigravity CLI（命令名 `agy`）的本地会话记录，生成结构化「接管摘要」，再基于摘要和当前代码现场继续任务。

主数据源是 `~/.gemini/antigravity/brain/<会话ID>/.system_generated/logs/transcript.jsonl`。同时读取会话目录中的 `task.md`、`implementation_plan.md`、`walkthrough.md`（存在时）。始终只读访问 Antigravity 源数据；不要输出模型的隐藏 `thinking` 字段。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_agy.js --list
   ```

2. 生成当前项目最近会话的接管摘要：

   ```bash
   node scripts/resume_agy.js
   ```

   已知会话 ID 时，指定完整 ID 或前缀并跨项目查找：

   ```bash
   node scripts/resume_agy.js --session <会话ID或前缀>
   ```

   `--conversation` 是 `--session` 的等价别名，与 `agy --conversation <ID>` 的命名一致。

3. 阅读「任务状态重建」「Antigravity 任务产物」「近期对话」，确认目标、已修改文件、命令、错误和剩余工作。

4. 重新检查当前文件系统和 Git 状态，从最后一条用户消息或未完成事项处继续。不要逐字复述历史。

无 Node.js 环境时，改用 Python 标准库实现：

```bash
python -X utf8 scripts/resume_agy.py [选项]
```

## CLI

两套脚本提供相同参数：

```text
--list                 仅列出当前项目会话
--latest               取最近一个会话（默认）
--session ID           指定会话 ID 或前缀；跨项目查找
--conversation ID      --session 的别名
--project PATH         项目路径，默认当前目录
--agy-dir DIR          Antigravity 数据目录
--recent N             近期条目数，默认 8
--max-chars N          Markdown 摘要单条内容截断长度，默认 1500
--limit N              --list 数量上限，0 不限制
--json                 输出机器可读 JSON
--output FILE          将摘要写入 UTF-8 文件
```

默认数据目录为 `$ANTIGRAVITY_HOME`；未设置时使用 `~/.gemini/antigravity`。

## 输出内容

- 会话信息：标题、ID、推断项目、时间范围、原始事件数
- Antigravity 任务产物：`task.md`、`implementation_plan.md`、`walkthrough.md`
- 任务状态：目标、已调查/修改文件、执行命令、测试与错误、最近用户/助手消息
- 近期对话：可见文本、工具输入/输出和错误（不包含隐藏 thinking）
- 更早工具活动与接管建议

## 注意事项

- Antigravity 会话 protobuf 文件不是本 Skill 的必要输入；脚本直接解析可读的 transcript JSONL。
- transcript 没有独立项目索引时，脚本会从 `run_command.Cwd` 及绝对文件路径推断项目。无法推断的会话仍可用 `--session` 接管。
- `--session` 跨项目查找；未指定时只选择 `--project` 对应的会话，避免误接管其他项目。
- `--json` 保留机器处理所需的完整字段；`--max-chars` 只控制 Markdown 摘要的展示长度。
- 迁移的是可序列化记录、任务产物、文件系统和 Git 现场，不能迁移模型 KV cache、隐藏推理状态或运行中的进程。
- 续作是语义接续，不是逐 token 恢复；必要时重新读取关键文件验证历史结论。
