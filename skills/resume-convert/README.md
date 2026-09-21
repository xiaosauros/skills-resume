# resume-convert

把其他 AI 编码工具的本地会话记录转换成 **Claude Code 原生会话 JSONL**：写入 `~/.claude/projects/<项目目录>/<会话ID>.jsonl` 后，用 Claude Code 原生 `/resume` 即可完整恢复对话（用户输入、助手回复、工具调用与结果全部保留）。

支持 18 种工具：Codex、Copilot CLI、DeepSeek Harness、Grok、Kimi Code、Qoder、ZCode、Antigravity、Continue、Cursor、Hermes、Kilo Code、MiMo-Code、MiniMax Code、OpenClaw、OpenCode、Pi、WorkBuddy。

## 目录结构

```
├── README.md                 # 本文件
├── SKILL.md                  # Skill 说明（agent 读取的指令）
└── scripts/
    ├── claude_session.js     # 共享转换工具类（Node.js）：归一化消息 -> Claude 原生 JSONL
    ├── claude_session.py     # 共享转换工具类（Python，等价实现）
    ├── export_to_claude.js   # 转换入口 CLI（Node.js，默认）
    ├── export_to_claude.py   # 转换入口 CLI（Python，等价）
    └── adapters/             # 每个工具一个薄适配器（源格式 -> 归一化消息）
        ├── codex.js / codex.py
        ├── copilot.js / copilot.py
        └── ...（每工具 .js/.py 一对）
```

## 架构

- **共享工具类 `claude_session`**：承担全部格式正确性——`parentUuid→uuid` 链、tool_use/tool_result 配对修复、孤儿结果丢弃、首消息必须 user、工具名合法化、长度截断、写盘前全量校验、原子写入（临时文件 + rename，拒绝覆盖已有会话）。适配器只做「源格式 → 归一化消息」一件事。
- **双语言等价**：Python 与 Node 实现逐函数对应，输出保持一致。
- **零第三方依赖**：SQLite 用内置 sqlite3 / node:sqlite 只读打开；zstd 用内置 compression.zstd / zlib.zstdDecompressSync。

## 使用方法

```bash
# 列出某工具可转换的会话
node scripts/export_to_claude.js --list --tool codex

# 试转换（只校验不写盘）
node scripts/export_to_claude.js --tool codex --session <ID或前缀> --dry-run

# 正式转换：写入 ~/.claude/projects，随后用原生 /resume 恢复
node scripts/export_to_claude.js --tool codex --session <ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：`python -X utf8 scripts/export_to_claude.py ...`。

常用参数：`--project <路径>` 按项目过滤；`--to-project <路径>` 指定落地项目；`--out-dir/--out-file` 导出到任意位置；`--degrade-tools` 工具调用降级为纯文本；`--no-prefix` 去掉标题来源前缀。

## 注意事项

- 只新增 JSONL 文件，不改不动已有会话；目标 ID 已存在时拒绝覆盖。
- 思考/推理块不跨模型迁移（签名失效），压缩摘要转为「转换注记」，超长工具输出截断并标注。
- 标题默认加 `[<工具名>] ` 前缀，便于在 /resume 列表中辨认来源。
