# resume-openclaw

接管/恢复 **OpenClaw**（开源个人 AI 助手，[openclaw/openclaw](https://github.com/openclaw/openclaw)，原 Clawdbot / Moltbot）会话：读取本机 OpenClaw 状态目录下每 agent 的会话存储，解析用户/助手消息、工具调用与结果、compaction 压缩摘要与 goal 目标状态，生成结构化「接管摘要」，供当前模型带着完整上下文继续未完成的工作。

## 目录结构

```
├── README.md                 # 本文件
├── SKILL.md                  # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_openclaw.js    # Node.js 实现（默认，需 Node 22.5+ 内置 node:sqlite）
    └── resume_openclaw.py    # Python 实现（等价，仅标准库）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_openclaw.js --list

# 生成最近一个会话的接管摘要
node scripts/resume_openclaw.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_openclaw.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：

```bash
python -X utf8 scripts/resume_openclaw.py --list
```

完整参数：

```
node scripts/resume_openclaw.js [--list|--latest|--session ID] [--project PATH] [--openclaw-dir DIR] [--limit N] [--json] [--output FILE]
```

状态目录自动探测：`OPENCLAW_STATE_DIR` → `~/.openclaw` → 旧版 `~/.clawdbot`；也可用 `--openclaw-dir` 直接指定。会话来源按序兼容：当前版本每 agent SQLite（`agents/<agentId>/agent/openclaw-agent.sqlite`）、每 agent 旧版 JSON（`agents/<agentId>/sessions/sessions.json` + `<sessionId>.jsonl`）、根级旧版 JSON（`sessions/sessions.json`）。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
