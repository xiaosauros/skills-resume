# resume-dsh

接管/恢复 **DeepSeek Harness**（`dsh`）会话：只读解析 `~/.dsh/sessions` 下的多帧 `session.jsonl.zstd`（兼容 `session.jsonl`），结合 `~/.dsh/storages/session_projcache.json`，生成结构化「接管摘要」。

## 目录结构

```text
├── README.md                 # 本文件
├── SKILL.md                  # Skill 说明（agent 读取的指令）
└── scripts/
    ├── resume_dsh.js         # Node.js 实现（默认）
    └── resume_dsh.py         # Python 独立等价实现
```

## 使用方法

```bash
# 列出当前项目的会话
node scripts/resume_dsh.js --list

# 生成当前项目最近非空会话的接管摘要
node scripts/resume_dsh.js

# 指定会话（支持 ID 前缀、标题关键词和跨项目查找）
node scripts/resume_dsh.js --session <会话ID、前缀或标题关键词>
```

完整参数：

```text
node scripts/resume_dsh.js [--list|--latest|--session VALUE] [--project PATH]
                           [--dsh-dir DIR] [--recent N] [--max-chars N]
                           [--limit N] [--json] [--output FILE]
```

默认读取 `$DSH_HOME` 或 `~/.dsh`。Node 实现需要带 `zlib.zstdDecompressSync` 的较新 Node.js；DeepSeek Harness 当前运行时已满足此条件。Python 是不调用 Node/JS 的独立等价实现，Python 3.14+ 直接使用标准库 `compression.zstd`，较旧 Python 可安装 `zstandard` 包：

```bash
python -X utf8 scripts/resume_dsh.py --list
```

所有源会话均以只读方式访问，不读取 `.credentials.yaml`。详细说明见 [SKILL.md](SKILL.md)，项目整体介绍见[根目录 README](../../README.md)。
