# resume-qoder

接管/恢复 **Qoder CLI** 会话：读取本机 `~/.qoder/projects` 下的 JSONL transcript 与 `state.json`，解析标题、compact summary、用户/助手消息、工具调用与结果，并兼容旧版 `*-session.json` 元数据，生成结构化「接管摘要」，供当前模型继续未完成的工作。

## 目录结构

```text
├── README.md               # 本文件
├── SKILL.md                # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_qoder.js     # Node.js 实现（默认）
    └── resume_qoder.py     # Python 实现（等价）
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_qoder.js --list

# 生成当前项目最近会话的接管摘要
node scripts/resume_qoder.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node scripts/resume_qoder.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出一致：

```bash
python -X utf8 scripts/resume_qoder.py --list
```

完整参数：

```text
node scripts/resume_qoder.js [--list|--latest|--session ID] [--project PATH]
                             [--qoder-dir DIR] [--recent N] [--max-chars N]
                             [--limit N] [--json] [--output FILE]
```

旧版 `*-session.json` 若只保留元数据而没有主 transcript，摘要会明确说明限制，不会伪造缺失的对话。所有源会话均以只读方式访问。

详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
