# resume-zcode

接管/恢复 **ZCode** 会话：读取本机 `~/.zcode/cli/db/db.sqlite` SQLite 会话库，从 `session`、`message`、`part` 表解析消息、工具调用与结果，并结合 `todo` 表的任务清单，生成结构化「接管摘要」，供当前模型继续未完成的工作。

## 目录结构

```text
├── README.md              # 本文件
├── SKILL.md               # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_zcode.js    # Node.js 实现（默认）
    └── resume_zcode.py    # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_zcode.js --list

# 生成当前项目最近会话的接管摘要
node scripts/resume_zcode.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_zcode.js --session <会话ID或前缀>
```

Node 实现使用内置 `node:sqlite`，需要 Node.js 22.5+。较低版本 Node 请改用 Python，参数与输出一致：

```bash
python -X utf8 scripts/resume_zcode.py --list
```

完整参数：

```text
node scripts/resume_zcode.js [--list|--latest|--session ID] [--project PATH]
                            [--zcode-dir DIR] [--recent N] [--max-chars N]
                            [--limit N] [--json] [--output FILE]
```

默认依次读取 `$ZCODE_HOME`、`~/.zcode`，会话库位于 `<主目录>/cli/db/db.sqlite`。所有源会话均以只读方式访问。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
