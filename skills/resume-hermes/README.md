# resume-hermes

接管/恢复 **Hermes Agent**（NousResearch [hermes-agent](https://github.com/NousResearch/hermes-agent)，CLI/桌面/网关通用）会话：读取本机 Hermes 主目录下的 `state.db`（`sessions`/`messages` 表），解析消息、工具调用、工具结果、压缩交接摘要与压缩续链，生成结构化「接管摘要」，供当前模型带着完整上下文继续未完成的工作。

## 目录结构

```
├── README.md            # 本文件
├── SKILL.md             # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_hermes.js # Node.js 实现（默认，需 Node.js 22.5+）
    └── resume_hermes.py # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_hermes.js --list

# 列出全部会话（含网关/IM 来源等无项目路径的会话）
node scripts/resume_hermes.js --list-all

# 生成最近一个会话的接管摘要
node scripts/resume_hermes.js

# 指定会话（支持 ID 前缀、跨项目全局查找，可命中压缩续链上的任意一段）
node scripts/resume_hermes.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：

```bash
python -X utf8 scripts/resume_hermes.py --list
```

完整参数：

```
node scripts/resume_hermes.js [--list|--list-all|--latest|--session ID] [--project PATH] [--limit N] [--json] [--output FILE]
```

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
