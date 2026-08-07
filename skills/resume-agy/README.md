# resume-agy

接管/恢复 **Antigravity CLI（`agy`）** 会话：读取本机 `~/.gemini/antigravity/brain` 下的 JSONL transcript 与任务产物，解析可见消息、工具调用与结果，生成结构化「接管摘要」，供当前模型继续未完成的工作。

## 目录结构

```text
├── README.md              # 本文件
├── SKILL.md               # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_agy.js      # Node.js 实现（默认）
    └── resume_agy.py      # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_agy.js --list

# 生成当前项目最近会话的接管摘要
node scripts/resume_agy.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_agy.js --session <会话ID或前缀>

# 与 agy CLI 命名一致的别名
node scripts/resume_agy.js --conversation <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出一致：

```bash
python -X utf8 scripts/resume_agy.py --list
```

完整参数：

```text
node scripts/resume_agy.js [--list|--latest|--session ID|--conversation ID]
                           [--project PATH] [--agy-dir DIR] [--recent N]
                           [--max-chars N] [--limit N] [--json] [--output FILE]
```

默认读取 `$ANTIGRAVITY_HOME`，未设置时读取 `~/.gemini/antigravity`。`--max-chars` 只控制 Markdown 摘要的展示长度，`--json` 保留完整字段。所有源会话均以只读方式访问，且不会输出 transcript 中的隐藏 `thinking` 字段。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
