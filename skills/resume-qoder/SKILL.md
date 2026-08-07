---
name: resume-qoder
description: 接管/恢复一个 Qoder CLI 会话--读取本机 ~/.qoder/projects 下按项目保存的 JSONL transcript、state.json 与 compact/子代理相关事件，兼容旧版 *-session.json 元数据，解析用户/助手消息、工具调用与工具结果，生成结构化「接管摘要」作为上下文，供当前模型继续未完成的工作。当用户要求「继续之前的 Qoder 会话」「接管 Qoder 的任务」「把 Qoder 会话交给 Claude/OpenCode/Codex/其他模型」或在当前项目中恢复 Qoder 历史进度时使用。
---

# resume-qoder

读取 Qoder CLI 本地会话记录，生成结构化「接管摘要」，再基于摘要和当前代码现场继续任务。

读取当前格式 `~/.qoder/projects/<编码项目>/<会话ID>.jsonl`，结合 `<会话ID>/state.json` 定位工作区，解析 `runtime-config`、标题、compact summary、用户/助手消息、`tool_use` 与 `tool_result`。同时识别旧版 `*-session.json`；旧版文件若只保留元数据、文件快照和工具结果，明确提示主 transcript 不可用，不伪造对话。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_qoder.js --list
   ```

2. 生成当前项目最近会话的接管摘要：

   ```bash
   node scripts/resume_qoder.js
   ```

   已知会话 ID 时，指定完整 ID 或前缀并跨项目查找：

   ```bash
   node scripts/resume_qoder.js --session <会话ID或前缀>
   ```

3. 阅读「任务状态重建」「历史摘要」「近期对话」，确认目标、已修改文件、命令和错误。

4. 重新检查当前文件系统和 Git 状态，从最后一条用户消息或剩余问题处继续。不要逐字复述历史。

无 Node.js 环境时，改用 Python 标准库实现：

```bash
python -X utf8 scripts/resume_qoder.py [选项]
```

## CLI

两套脚本提供相同参数：

```text
--list              仅列出当前项目会话
--latest            取最近一个会话（默认）
--session ID        指定会话 ID 或前缀；跨项目查找
--project PATH      项目路径，默认当前目录
--qoder-dir DIR     Qoder 用户数据目录，默认 ~/.qoder 或 $QODER_HOME
--recent N          近期条目数，默认 8
--max-chars N       单条内容截断长度，默认 1500
--limit N           --list 数量上限，0 不限制
--json              输出机器可读 JSON
--output FILE       将摘要写入 UTF-8 文件
```

## 输出内容

- 会话信息：标题、ID、项目、模型、推理强度、context window、Git 分支、入口、版本与存储格式
- 原会话 compact 摘要（存在时）
- 任务状态：目标、已调查/修改文件、执行命令、测试与错误、最近用户/助手消息
- 近期对话：文本、工具输入与工具结果
- 更早工具活动与接管建议

## 格式兼容

- 当前 JSONL：完整解析主 transcript；忽略 thinking/redacted_thinking，不迁移隐藏推理内容。
- `state.json`：读取 `workspaceDirectories` 作为工作区定位回退。
- `compression-v2/state.json`：它只保存压缩算法状态；实际 compact 内容从带 `isCompactSummary` 的 transcript 事件读取。
- 旧版 `*-session.json`：读取标题、工作目录、时间和计数；本地没有主 transcript 时只生成兼容性说明。
- 子代理记录：主 transcript 中的工具调用和通知会保留；不把 `subagents/` 下独立记录混入主会话。

## 注意事项

- `--session` 跨项目查找；未指定时只选择 `--project` 对应的会话，避免误接管其他项目。
- 两套实现都只读访问 `~/.qoder`；Node 零第三方依赖，Python 仅用标准库。
- 迁移的是可序列化记录、文件系统和 Git 现场，不能迁移模型 KV cache、隐藏推理状态或运行中的进程。
- 续作是语义接续，不是逐 token 恢复；必要时重新读取关键文件验证历史结论。
