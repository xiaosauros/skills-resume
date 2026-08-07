# skills-resume

一套跨 AI 编码工具的「会话接管（resume）」Skills 合集。当你的某个 AI 编码助手（Antigravity CLI、Claude Code、Codex、Copilot、Cursor、Grok、Kimi Code、OpenCode、Qoder）的会话中断、或你想把进行中的任务**交接给另一个模型/工具**继续时，这些 Skill 会读取对应工具在本机的会话记录，解析消息、工具调用与结果，生成一份结构化的「接管摘要」，让当前模型带着完整上下文接续未完成的工作。

每个 Skill 的核心逻辑都是可移植脚本（Node.js + Python 双实现，输出一致），**不依赖任何模型专属 API**，因此任意 agent 都可以调用。

## 包含的 Skills

| Skill | 接管的会话来源 | 读取的本地数据 |
| --- | --- | --- |
| [resume-agy](skills/resume-agy/) | Antigravity CLI（`agy`） | `~/.gemini/antigravity/brain` 下的 transcript.jsonl 与任务产物 |
| [resume-claude](skills/resume-claude/) | Claude Code | `~/.claude/projects/<项目>/*.jsonl` |
| [resume-codex](skills/resume-codex/) | Codex CLI | `~/.codex` 下的 rollout 记录与 session_index |
| [resume-copilot](skills/resume-copilot/) | GitHub Copilot CLI | `~/.copilot/session-state` 下的 workspace.yaml + events.jsonl |
| [resume-cursor](skills/resume-cursor/) | Cursor IDE Agent/Composer | Cursor 的 SQLite 会话库（state.vscdb） |
| [resume-grok](skills/resume-grok/) | Grok Build CLI | `~/.grok` 下的 summary.json 与 chat_history.jsonl（回退 events/updates） |
| [resume-kimi](skills/resume-kimi/) | Kimi Code CLI | `~/.kimi-code` 下的 session_index、state.json 与 wire.jsonl |
| [resume-opencode](skills/resume-opencode/) | OpenCode | `~/.local/share/opencode/opencode.db`（兼容旧版 storage JSON） |
| [resume-qoder](skills/resume-qoder/) | Qoder CLI | `~/.qoder/projects` 下的 JSONL transcript、state.json（兼容旧版 session 元数据） |

## 目录结构

```
skills/
├── resume-agy/
│   ├── README.md             # Skill 简介与用法
│   ├── SKILL.md              # Skill 说明（agent 读取的指令）
│   └── scripts/
│       ├── resume_agy.js     # Node.js 实现（默认）
│       └── resume_agy.py     # Python 实现（等价）
├── resume-claude/
├── resume-codex/
├── resume-copilot/
├── resume-cursor/
├── resume-grok/
├── resume-kimi/
├── resume-opencode/
└── resume-qoder/
```

每个 Skill 都包含独立 `README.md`、`SKILL.md` 与 `scripts/` 下的 Node/Python 双实现。

## 环境要求

- **Node.js**（推荐）或 **Python 3**
- 脚本无第三方依赖，开箱即用

## 安装

Skills 通过「把 Skill 目录放进 agent 的 skills 目录」来安装。每个 Skill 都是自包含目录（`SKILL.md` + `scripts/`），按需安装其中一个或多个即可。

### 方式一：克隆后复制/链接

```bash
git clone git@github.com:xiaosauros/skills-resume.git
```

然后把需要的 Skill 目录复制（或软链接）到你使用的 agent 的 skills 目录，例如：

- Kimi Code：`~/.kimi-code/skills/`（用户级）或项目内 `.kimi/skills/`
- Claude Code：`~/.claude/skills/`（用户级）或项目内 `.claude/skills/`
- 其他 agent：参照其各自的 skills 目录约定

Windows 下可用 `mklink /J` 创建目录联接，Linux/macOS 下用 `ln -s`。

### 方式二：让 agent 自己安装（自然语言）

不用手动执行任何命令，直接在当前使用的 agent 对话中提出安装请求，agent 会自动完成克隆、复制/链接到对应 skills 目录的全过程，例如：

- 「把 https://github.com/xiaosauros/skills-resume 里的 resume-claude 安装到你的 skills 目录」
- 「克隆 xiaosauros/skills-resume 这个仓库，把全部 9 个 Skill 安装到用户级 skills 目录」
- 「把 https://github.com/xiaosauros/skills-resume 里的 resume-codex 装成项目级的 Skill」

agent 会自行判断目标目录（用户级或项目级）、选择复制或软链接方式并完成安装。安装后可直接用自然语言验证：「列出你已安装的 skills」。

### 方式三：不用 Skill 系统，直接跑脚本

脚本可独立使用，不安装 Skill 也能工作（见下文「直接使用脚本」）。

## 使用方法

### 在 agent 中使用（安装 Skill 后）

直接在对话中提出接管请求，agent 会自动加载对应 Skill，例如：

- 「继续我之前的 Claude 会话，把没做完的任务完成」
- 「继续最近的 agy / Antigravity CLI 会话」
- 「接管 Codex 的会话，看看还剩什么没做」
- 「把 Cursor 里那个调试到一半的会话交给当前模型继续」
- 「继续最近的 OpenCode 会话」
- 「把 Qoder 里的任务交给当前模型继续」

已知会话 ID 时可以直接指定（支持 ID 前缀）：

- 「接管 Claude 会话 `a1b2c3d4`，继续之前的工作」
- 「用 resume-kimi 恢复会话 `9f8e7d6c-...` 的进度」

在支持 slash command 调用 Skill 的 agent 中，也可以用斜杠命令显式触发对应 Skill，再说明需求：

```
/resume-claude 列出当前项目的会话
/resume-agy --conversation a1b2c3d4
/resume-codex --session a1b2c3d4
/resume-kimi 继续最近一个会话的未完成工作
/resume-opencode 继续当前项目最近的会话
/resume-qoder --session 9f8e7d6c
```

Skill 会引导 agent：列出会话 → 生成接管摘要 → 基于摘要与当前代码现场（文件系统 + Git）继续工作。

### 直接使用脚本

每个 Skill 的脚本都支持相同的用法（以 resume-claude 为例）：

```bash
# 列出当前项目的会话
node skills/resume-claude/scripts/resume_claude.js --list

# 生成最近一个会话的接管摘要
node skills/resume-claude/scripts/resume_claude.js

# 指定会话（支持 ID 前缀、跨项目全局查找）
node skills/resume-claude/scripts/resume_claude.js --session <会话ID或前缀>
```

无 Node.js 环境时改用 Python，参数与输出完全一致：

```bash
python -X utf8 skills/resume-claude/scripts/resume_claude.py --list
```

其余 Skill 同理，替换脚本路径即可（`resume-agy` / `resume-codex` / `resume-copilot` / `resume-cursor` / `resume-grok` / `resume-kimi` / `resume-opencode` / `resume-qoder`）。

OpenCode 当前版本使用 SQLite，Node 实现需要 Node.js 22.5+ 的内置 `node:sqlite`；较低版本 Node 请直接运行对应 Python 脚本。

## 接管摘要包含什么

- **会话信息**：标题、ID、项目路径、Git 分支、时间范围、消息数
- **历史摘要**：原会话中的 compact 摘要（若存在）
- **任务状态重建**：目标、已调查文件、执行过的命令、代码修改、测试结果、剩余问题
- **近期对话**：最近若干轮原始内容（含工具调用与结果）
- **接管建议**

各 Skill 的详细参数与输出说明见各自目录下的 `SKILL.md`。
