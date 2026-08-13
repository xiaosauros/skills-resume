---
name: resume-dsh
description: 接管/恢复一个 DeepSeek Harness（dsh）会话——只读扫描本机 ~/.dsh/sessions 下的 session.jsonl.zstd（兼容未压缩 session.jsonl），结合 ~/.dsh/storages/session_projcache.json，解析用户/助手消息、工具调用与结果、TODO、标题和模型信息，生成结构化「接管摘要」供当前模型继续未完成的工作。当用户要求「继续之前的 DSH/DeepSeek Harness 会话」「接管 dsh 的任务」「把 DeepSeek Harness 会话交给 Claude/Codex/Kimi/其他模型」或恢复当前项目的 DSH 历史进度时使用。
---

# resume-dsh

以只读方式读取 DeepSeek Harness 本地会话记录，生成可供任意 agent 接续工作的结构化摘要。处理 DSH 追加写入的多帧 Zstandard 日志，并从投影缓存补充标题、TODO 与会话统计。

## 使用步骤

1. 列出当前项目的会话：

   ```bash
   node scripts/resume_dsh.js --list
   ```

2. 生成最近一个非空会话的接管摘要：

   ```bash
   node scripts/resume_dsh.js
   ```

   指定会话 ID、ID 前缀或标题关键词（支持跨项目查找）：

   ```bash
   node scripts/resume_dsh.js --session <会话ID、前缀或标题关键词>
   ```

3. 阅读输出中的目标、文件、命令、验证结果、TODO 与近期对话。

4. 对照当前文件系统和 Git 状态确认现场，然后从未完成处继续。不要只复述历史。

Python 与 Node 是相互独立的等价实现，参数与输出一致。按当前环境任选其一：

```bash
python -X utf8 scripts/resume_dsh.py ...
```

## 参数

```text
--list              列出会话
--latest            选择最近会话（默认）
--session VALUE     按 ID、ID 前缀或标题关键词选择，支持跨项目
--project PATH      项目路径，默认当前工作目录
--dsh-dir DIR       DSH 数据目录，默认 $DSH_HOME 或 ~/.dsh
--recent N          近期活动条目数，默认 10
--max-chars N       单条内容最大字符数，默认 1800
--limit N           --list 最大返回数，0 表示不限制
--json              输出机器可读 JSON
--output FILE       将结果写入 UTF-8 文件
```

## 数据与兼容性

- 主日志：`~/.dsh/sessions/<project-key>/<session-id>/session.jsonl.zstd`
- 明文兼容：同目录 `session.jsonl`
- 投影缓存：`~/.dsh/storages/session_projcache.json`
- Zstandard 日志是多个独立 frame 的拼接，脚本逐帧扫描与解压，并忽略尚未写完的尾帧。
- Node 实现要求带标准库 Zstandard 支持的较新 Node.js（当前 DSH 运行时满足此条件）。
- Python 实现不调用 Node 或 JS；使用 Python 3.14+ 标准库 `compression.zstd`，较旧 Python 可安装 `zstandard` 包作为回退。
- DSH 处于 developer preview，格式可能破坏性变化；遇到未知必需格式时应报告并重新核对上游实现，不要猜测修复日志。
- 不读取 `.credentials.yaml`，也不修改任何 DSH 文件。

## 接管边界

- 迁移的是会话日志、投影缓存、文件系统和 Git 可重建的上下文。
- 不迁移模型 KV cache、隐藏推理状态、运行中进程或未持久化的内存状态。
- 摘要会跳过 reasoning/chunk 原始流和系统注入消息，保留最终助手消息、真实用户消息、工具调用/结果与任务状态。
