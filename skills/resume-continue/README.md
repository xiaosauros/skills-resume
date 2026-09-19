# resume-continue

接管/恢复 **Continue**（[continuedev/continue](https://github.com/continuedev/continue)，VS Code / JetBrains 扩展或 `cn` CLI）会话：读取本机 `~/.continue/sessions` 下的会话 JSON（IDE 扩展与 CLI 共用），解析消息、@引用上下文、工具调用与结果、压缩摘要，生成结构化「接管摘要」，供当前模型继续未完成的工作。

## 目录结构

```text
├── README.md                 # 本文件
├── SKILL.md                  # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_continue.js    # Node.js 实现（默认）
    └── resume_continue.py    # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_continue.js --list

# 生成当前项目最近会话的接管摘要
node scripts/resume_continue.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_continue.js --session <会话ID或前缀>
```

两套实现均为纯标准库、无第三方依赖（Node 无版本要求，Python 3.7+），参数与输出一致：

```bash
python -X utf8 scripts/resume_continue.py --list
```

完整参数：

```text
node scripts/resume_continue.js [--list|--latest|--session ID] [--project PATH]
                                [--continue-dir DIR] [--recent N] [--max-chars N]
                                [--limit N] [--json] [--output FILE]
```

默认读取 `~/.continue/sessions`（受 `CONTINUE_GLOBAL_DIR` 环境变量影响），可用 `--continue-dir` 指向 Continue 主目录或 sessions 目录。会话文件以只读方式访问。

若想让 Continue 自己原生续接（而非交给其他模型）：CLI 在原项目目录用 `cn --resume` 继续最近会话，或 `cn ls` 交互选择；IDE 在 Continue 侧边栏的会话历史中选中该会话。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
