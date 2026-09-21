---
name: resume-convert
description: 把其他 AI 编码工具（Codex、Copilot、Cursor、Kimi、Qoder、ZCode 等 18 种）的本地会话记录转换成 Claude Code 原生会话 JSONL，写入 ~/.claude/projects 对应项目目录后，即可用 Claude Code 原生 /resume 命令完整恢复对话（含工具调用与结果）。当用户要求「把 Codex/Cursor/其他工具的会话导入 Claude」「转换会话格式以便 /resume 恢复」「迁移历史会话到 Claude Code」时使用。
---

# resume-convert

把其他 AI 编码工具的本地会话记录，无损转换成 **Claude Code 原生会话 JSONL**，写入 `~/.claude/projects/<项目目录>/<会话ID>.jsonl` 后，用 Claude Code 原生 `/resume` 命令即可恢复完整对话——包括用户输入、助手回复、工具调用与工具结果。

核心转换逻辑抽象在共享工具类中（`scripts/claude_session.py` / `claude_session.js`，两套等价实现），每个工具一个薄适配器（`scripts/adapters/<tool>.py` / `.js`）只负责「源格式 → 归一化消息」。全部零第三方依赖。

## 转换原理

Claude Code 会话是 JSONL：每行一条记录，`user`/`assistant` 记录通过 `parentUuid → uuid` 链成树，`/resume` 沿链回放。API 层有硬约束（转换器负责保证）：

- 每条 `tool_use` 必须由紧随其后的 user 记录中同 `tool_use_id` 的 `tool_result` 回答
- user 记录内 `tool_result` 块先于文本块；assistant 记录内 `tool_use` 块最后
- 首条消息必须是 user 角色；工具名只允许 `[A-Za-z0-9_-]{1,64}`

源会话里缺失结果/乱序的工具调用，转换器自动修复（补合成结果、丢弃孤儿结果、必要时前置一条注记），写盘前全量校验，**校验不过不写盘**（原子写入：临时文件 + rename）。跨模型迁移时思考/推理块一律丢弃（签名无法迁移），压缩摘要转为「转换注记」文本。

## 支持的工具（18 种）

| 工具 | 本地存储 | 读取位置 |
| --- | --- | --- |
| Codex CLI | rollout JSONL | `~/.codex/sessions/**` + `session_index.jsonl` |
| GitHub Copilot CLI | JSONL+YAML | `~/.copilot/session-state` |
| DeepSeek Harness | zstd 压缩 JSONL | `~/.dsh` 下 session.jsonl.zstd + projcache |
| Grok Build CLI | JSONL+JSON | `~/.grok` 下 summary.json + chat_history.jsonl |
| Kimi Code CLI | JSONL | `~/.kimi-code` 下 session_index / state.json / wire.jsonl |
| Qoder CLI | JSONL | `~/.qoder/projects` 下 transcript + state.json |
| ZCode | SQLite | `~/.zcode/cli/db/db.sqlite` |
| Antigravity CLI | JSONL | `~/.gemini/antigravity/brain` 下 transcript.jsonl |
| Continue | JSON | `~/.continue/sessions` |
| Cursor IDE | SQLite | `state.vscdb`（ItemTable / composerData） |
| Hermes Agent | SQLite | state.db（sessions / messages 表） |
| Kilo Code | SQLite+JSON | kilo.db；旧版 tasks 目录 api_conversation_history.json |
| MiMo-Code | SQLite | `~/.local/share/mimocode/mimocode.db` |
| MiniMax Code | SQLite+JSONL | `~/.minimax` runtime-state.sqlite + messages.jsonl |
| OpenClaw | SQLite+JSON | `~/.openclaw` 各 agent sqlite；旧版 sessions.json |
| OpenCode | SQLite+JSON | `~/.local/share/opencode/opencode.db` |
| Pi Coding Agent | JSONL | `~/.pi/agent/sessions` 会话树 |
| WorkBuddy | JSONL | `~/.workbuddy/projects` |

SQLite 一律只读打开；`--list` 列不出会话或报「未找到」的工具，说明本机无该工具数据或格式不兼容，不要强行写入。

## 使用步骤

1. 列出某工具本机可转换的会话：
   ```bash
   node scripts/export_to_claude.js --list --tool codex
   # 全部工具：--list（不带 --tool）；Python 等价：python -X utf8 scripts/export_to_claude.py --list
   ```

2. 试转换（--dry-run 只校验不写盘，先看有没有错误/警告）：
   ```bash
   node scripts/export_to_claude.js --tool codex --session <会话ID或前缀> --dry-run
   ```

3. 正式转换（默认写入 `~/.claude/projects/<源会话cwd对应目录>/<会话ID>.jsonl`）：
   ```bash
   node scripts/export_to_claude.js --tool codex --session <会话ID或前缀>
   ```

4. 在 Claude Code 中运行原生 `/resume`，选择刚导入的会话（标题带 `[codex]` 等来源前缀）。

常用参数：`--project <路径>` 按项目过滤；`--to-project <路径>` 落到指定项目目录（默认继承源会话 cwd）；`--out-dir/--out-file` 导出到任意位置（不进 ~/.claude）；`--session-id` 覆盖会话 ID；`--degrade-tools` 把工具调用降级为纯文本（目标端不支持工具时）；`--no-prefix` 去掉标题来源前缀；`--limit N` 配合 --list。无 Node 环境用 Python，参数完全相同。

## 验证状态

- **双语言夹具（开发期验证，18/18 全部通过）**：按真实存储格式构造的合成夹具上，Python/Node 两套实现输出指纹（剔除 uuid/时间戳/随机 id 后）逐行一致，且写盘前校验零错误。
- **真实数据端到端（7/7 通过）**：本机有真实会话数据的 codex、copilot、dsh、grok、kimi、qoder、zcode 全部通过真实数据双语言转换验证；其中 codex、copilot、qoder、zcode、kimi 另做过「写入 → 原生 resume-claude 解析回读」全链路验证。
- 其余 11 种工具（agy、continue、cursor、hermes、kilo、mimo、minimax、openclaw、opencode、pi、workbuddy）本机无真实会话数据，验证级别为双语言夹具一致（解析逻辑与对应 resume-<tool> 脚本逐分支对齐）。

## 注意事项

- 转换只**新增** JSONL 文件，不修改/删除任何已有会话；目标会话 ID 已存在时拒绝覆盖（除非显式换 `--session-id`）。
- 思考/推理内容不迁移（跨模型签名失效），压缩摘要转注记；超长工具输出按阈值截断并标注。
- 标题默认加 `[<工具名>] ` 前缀，便于在 /resume 列表中辨认来源。
