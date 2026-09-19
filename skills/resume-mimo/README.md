# resume-mimo

接管/恢复 **MiMo-Code**（小米开源终端 AI 编码助手，命令 `mimo`）会话：读取本机 mimocode 数据目录下的 `mimocode.db` SQLite 会话库，解析消息、工具调用与结果、patch、compact 摘要、checkpoint 与子任务，生成结构化「接管摘要」，供当前模型带着完整上下文继续未完成的工作。

## 目录结构

```
├── README.md            # 本文件
├── SKILL.md             # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_mimo.js   # Node.js 实现（默认，需 Node 22.5+ 内置 node:sqlite）
    └── resume_mimo.py   # Python 实现（等价，仅标准库）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_mimo.js --list

# 生成最近一个会话的接管摘要
node scripts/resume_mimo.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_mimo.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：

```bash
python -X utf8 scripts/resume_mimo.py --list
```

完整参数：

```
node scripts/resume_mimo.js [--list|--latest|--session ID] [--project PATH] [--mimo-dir DIR] [--limit N] [--json] [--output FILE]
```

数据目录自动探测：`MIMOCODE_HOME/data` → `$XDG_DATA_HOME/mimocode` → `~/.local/share/mimocode` → `%LOCALAPPDATA%\mimocode`（Windows 回退）→ `~/Library/Application Support/mimocode`（macOS 回退）；也可用 `--mimo-dir` 直接指定。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
