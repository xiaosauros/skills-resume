# resume-cursor

接管/恢复 **Cursor IDE** Agent/Composer 会话：读取本机 Cursor 的 SQLite 会话库（state.vscdb），解析 composer、bubble、工具调用与工具结果，生成结构化「接管摘要」，供当前模型带着完整上下文继续未完成的工作。

## 目录结构

```
├── README.md              # 本文件
├── SKILL.md               # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_cursor.js   # Node.js 实现（默认）
    └── resume_cursor.py   # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_cursor.js --list

# 生成最近一个会话的接管摘要
node scripts/resume_cursor.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_cursor.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：

```bash
python -X utf8 scripts/resume_cursor.py --list
```

完整参数：

```
node scripts/resume_cursor.js [--list|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
