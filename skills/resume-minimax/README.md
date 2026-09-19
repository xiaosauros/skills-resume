# resume-minimax

接管/恢复 **MiniMax Code**（CLI 名 `mcode`）会话：读取本机 `~/.minimax` 主目录下的 SQLite 会话库（`v2/sqlite/runtime-state.sqlite`）与规范历史 JSONL（`v2/sessions/**/messages.jsonl`），解析消息、工具调用与结果、压缩摘要与任务清单，生成结构化「接管摘要」，供当前模型继续未完成的工作。

## 目录结构

```text
├── README.md                 # 本文件
├── SKILL.md                  # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_minimax.js     # Node.js 实现（默认）
    └── resume_minimax.py     # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_minimax.js --list

# 生成当前项目最近会话的接管摘要
node scripts/resume_minimax.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_minimax.js --session <会话ID或前缀>
```

Node 实现读取 SQLite 时使用内置 `node:sqlite`，需要 Node.js 22.5+（较低版本自动降级为仅 JSONL 模式）。Python 版无此限制，参数与输出一致：

```bash
python -X utf8 scripts/resume_minimax.py --list
```

完整参数：

```text
node scripts/resume_minimax.js [--list|--latest|--session ID] [--project PATH]
                               [--minimax-dir DIR] [--recent N] [--max-chars N]
                               [--limit N] [--json] [--output FILE]
```

默认依次读取 `$MINIMAX_DATA_DIR`、`~/.minimax`（不存在时兼容旧版 `~/.mavis`）。SQLite 不可用时自动回退为扫描 `v2/sessions/**/manifest.json` + `messages.jsonl`。所有源会话均以只读方式访问。

若想让 MiniMax Code 原生续接（而非交给其他模型），可直接用 `mcode --resume <会话ID>`，或在对应项目目录下 `mcode -c` 继续最近会话。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
