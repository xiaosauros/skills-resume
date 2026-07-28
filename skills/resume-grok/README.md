# resume-grok

接管/恢复 **Grok Build CLI** 会话：读取本机 `~/.grok` 下的 summary.json 与 chat_history.jsonl（必要时回退 events.jsonl / updates.jsonl 的 ACP 事件流），解析用户/助手消息、工具调用与工具结果，生成结构化「接管摘要」，供当前模型带着完整上下文继续未完成的工作。

## 目录结构

```
├── README.md            # 本文件
├── SKILL.md             # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_grok.js   # Node.js 实现（默认）
    └── resume_grok.py   # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_grok.js --list

# 生成最近一个会话的接管摘要
node scripts/resume_grok.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_grok.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：

```bash
python -X utf8 scripts/resume_grok.py --list
```

完整参数：

```
node scripts/resume_grok.js [--list|--latest|--session ID] [--project PATH] [--grok-dir DIR] [--limit N] [--json] [--output FILE]
```

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
