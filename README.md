# skills-resume

一套跨 AI 编码工具的「会话接管（resume）」Skills 合集。当你的某个 AI 编码助手（Antigravity CLI、Claude Code、Codex、Copilot、Continue、Cursor、DeepSeek Harness、Grok、Hermes Agent、Kilo Code、Kimi Code、MiniMax Code、MiMo-Code、OpenClaw、OpenCode、Pi、Qoder、WorkBuddy、ZCode）的会话中断、或你想把进行中的任务**交接给另一个模型/工具**继续时，这些 Skill 会读取对应工具在本机的会话记录，解析消息、工具调用与结果，生成一份结构化的「接管摘要」，让当前模型带着完整上下文接续未完成的工作。

每个 Skill 都提供可移植的 Node.js 与 Python 等价实现，**不依赖任何模型专属 API**，因此任意 agent 都可以调用。

## 包含的 Skills

| Skill | 接管的会话来源 | 读取的本地数据 |
| --- | --- | --- |
| [resume-agy](skills/resume-agy/) | Antigravity CLI（`agy`） | `~/.gemini/antigravity/brain` 下的 transcript.jsonl 与任务产物 |
| [resume-claude](skills/resume-claude/) | Claude Code | `~/.claude/projects/<项目>/*.jsonl` |
| [resume-codex](skills/resume-codex/) | Codex CLI | `~/.codex` 下的 rollout 记录与 session_index |
| [resume-copilot](skills/resume-copilot/) | GitHub Copilot CLI | `~/.copilot/session-state` 下的 workspace.yaml + events.jsonl |
| [resume-continue](skills/resume-continue/) | Continue（VS Code / JetBrains 扩展、`cn` CLI） | `~/.continue/sessions` 下的会话 JSON（IDE 扩展与 CLI 共用；受 `CONTINUE_GLOBAL_DIR` 影响） |
| [resume-cursor](skills/resume-cursor/) | Cursor IDE Agent/Composer | Cursor 的 SQLite 会话库（state.vscdb） |
| [resume-dsh](skills/resume-dsh/) | DeepSeek Harness（`dsh`） | `~/.dsh` 下的多帧 session.jsonl.zstd 与 session_projcache.json |
| [resume-grok](skills/resume-grok/) | Grok Build CLI | `~/.grok` 下的 summary.json 与 chat_history.jsonl（回退 events/updates） |
| [resume-hermes](skills/resume-hermes/) | Hermes Agent（NousResearch hermes-agent） | Hermes 主目录（`%LOCALAPPDATA%\hermes` / `~/.hermes`）下 state.db 的 sessions/messages 表（含压缩交接摘要与压缩续链） |
| [resume-kilo](skills/resume-kilo/) | Kilo Code（VS Code 扩展 / Kilo CLI） | Kilo 数据目录下的 kilo.db（opencode 风格 SQLite，含 v2 投影与 todo）；旧版扩展任务目录 globalStorage/kilocode.kilo-code/tasks 下的 api_conversation_history.json |
| [resume-kimi](skills/resume-kimi/) | Kimi Code CLI | `~/.kimi-code` 下的 session_index、state.json 与 wire.jsonl |
| [resume-minimax](skills/resume-minimax/) | MiniMax Code（`mcode`） | `~/.minimax` 下的 SQLite 会话库（v2/sqlite/runtime-state.sqlite）与规范历史 JSONL（v2/sessions/**/messages.jsonl），兼容旧版 `~/.mavis` |
| [resume-mimo](skills/resume-mimo/) | MiMo-Code（小米 mimo CLI） | `~/.local/share/mimocode` 下的 mimocode.db SQLite 会话库（支持 MIMOCODE_HOME / 渠道库 / MIMOCODE_DB 覆盖） |
| [resume-opencode](skills/resume-opencode/) | OpenCode | `~/.local/share/opencode/opencode.db`（兼容旧版 storage JSON） |
| [resume-openclaw](skills/resume-openclaw/) | OpenClaw（原 Clawdbot/Moltbot） | `~/.openclaw` 下每 agent 的 openclaw-agent.sqlite（session_nodes / transcript_events / 活动分支投影），兼容旧版 sessions.json + JSONL transcript 与旧状态目录 `~/.clawdbot`（受 `OPENCLAW_STATE_DIR` 影响） |
| [resume-pi](skills/resume-pi/) | Pi Coding Agent（pi） | `~/.pi/agent/sessions` 下的 JSONL 会话树（含 compact / 分支摘要） |
| [resume-qoder](skills/resume-qoder/) | Qoder CLI | `~/.qoder/projects` 下的 JSONL transcript、state.json（兼容旧版 session 元数据） |
| [resume-workbuddy](skills/resume-workbuddy/) | WorkBuddy | `~/.workbuddy/projects` 下的 JSONL 会话记录、任务产物与子 agent 记录 |
| [resume-zcode](skills/resume-zcode/) | ZCode | `~/.zcode/cli/db/db.sqlite` SQLite 会话库（session/message/part/todo 表） |

## 目录结构

```
scripts/
├── install_skills.mjs        # 安装脚本（Node.js）：把 skills 装到各工具的 skills 目录
└── install_skills.py         # 安装脚本（Python，等价实现）
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
├── resume-continue/
├── resume-cursor/
├── resume-dsh/
├── resume-grok/
├── resume-hermes/
├── resume-kilo/
├── resume-kimi/
├── resume-minimax/
├── resume-mimo/
├── resume-openclaw/
├── resume-opencode/
├── resume-pi/
├── resume-qoder/
├── resume-workbuddy/
└── resume-zcode/
```

每个 Skill 都包含独立 `README.md`、`SKILL.md` 与 `scripts/` 下的 Node/Python 入口。

## 环境要求

- **Node.js**（推荐）或 **Python 3**
- 脚本无第三方依赖，开箱即用

## 安装

Skills 通过「把 Skill 目录放进 agent 的 skills 目录」来安装。每个 Skill 都是自包含目录（`SKILL.md` + `scripts/`），按需安装其中一个或多个即可。

### 方式一：安装脚本（推荐）

仓库自带两个等价的安装脚本：`scripts/install_skills.py`（Python）与 `scripts/install_skills.mjs`（Node.js），把 skills 一键安装/链接到各 AI 编码工具的 skills 目录。默认复制安装全部 skills 到全部支持的工具，**目标已存在时跳过（不覆盖）**：

```bash
# 预览将要执行的操作（不实际写入）
python scripts/install_skills.py -n
node scripts/install_skills.mjs -n

# 安装全部 skills 到全部支持的工具（默认复制；已存在则跳过）
python scripts/install_skills.py
node scripts/install_skills.mjs

# 只安装指定 skills（位置参数或 --skills，接受全名 resume-claude 或简写 claude）
node scripts/install_skills.mjs resume-claude resume-codex
python scripts/install_skills.py --skills claude,kimi --tool zcode

# 指定目标工具（--list-tools 查看支持列表，all=全部）
node scripts/install_skills.mjs --tool claude,codex

# 改用链接代替复制（Windows 下自动使用目录联接 junction，无需管理员权限）
node scripts/install_skills.mjs --link

# 已存在也强制覆盖
python scripts/install_skills.py --force

# 安装到任意指定目录（例如项目级 skills 目录）
node scripts/install_skills.mjs --dir .claude/skills resume-claude

# 查看可安装的 skills / 支持的目标工具及各自目录约定
node scripts/install_skills.mjs --list
node scripts/install_skills.mjs --list-tools
```

说明：

- 脚本内置了各工具用户级 skills 目录的约定映射（`--list-tools` 可查看），并以「目录是否存在」标注；个别工具的约定如有变化，可用 `--dir` 直接指定目标目录
- `--link` 模式建立的是指向本仓库的链接，仓库更新后各工具即时生效；配合 `--force` 可把已复制安装的目标改为链接
- 两个脚本参数与行为完全一致，任选其一即可（Node.js ≥ 16.7 / Python 3.8+，无第三方依赖）

### 方式二：克隆后复制/链接

```bash
git clone git@github.com:xiaosauros/skills-resume.git
```

然后把需要的 Skill 目录复制（或软链接）到你使用的 agent 的 skills 目录，例如：

- Kimi Code：`~/.kimi-code/skills/`（用户级）或项目内 `.kimi/skills/`
- Claude Code：`~/.claude/skills/`（用户级）或项目内 `.claude/skills/`
- 其他 agent：参照其各自的 skills 目录约定

Windows 下可用 `mklink /J` 创建目录联接，Linux/macOS 下用 `ln -s`。

### 方式三：让 agent 自己安装（自然语言）

不用手动执行任何命令，直接在当前使用的 agent 对话中提出安装请求，agent 会自动完成克隆、复制/链接到对应 skills 目录的全过程，例如：

- 「把 https://github.com/xiaosauros/skills-resume 里的 resume-claude 安装到你的 skills 目录」
- 「克隆 xiaosauros/skills-resume 这个仓库，把全部 19 个 Skill 安装到用户级 skills 目录」
- 「把 https://github.com/xiaosauros/skills-resume 里的 resume-codex 装成项目级的 Skill」

agent 会自行判断目标目录（用户级或项目级）、选择复制或软链接方式并完成安装。安装后可直接用自然语言验证：「列出你已安装的 skills」。

### 方式四：不用 Skill 系统，直接跑脚本

脚本可独立使用，不安装 Skill 也能工作（见下文「直接使用脚本」）。

## 使用方法

### 在 agent 中使用（安装 Skill 后）

直接在对话中提出接管请求，agent 会自动加载对应 Skill，例如：

- 「继续我之前的 Claude 会话，把没做完的任务完成」
- 「继续最近的 agy / Antigravity CLI 会话」
- 「接管 Codex 的会话，看看还剩什么没做」
- 「继续最近的 Continue 会话（VS Code/JetBrains 扩展或 cn CLI 都可以）」
- 「继续最近的 DSH / DeepSeek Harness 会话」
- 「继续最近的 Hermes 会话，看看修到哪了」
- 「继续最近的 Kilo / Kilo Code 会话」
- 「继续最近的 Kimi / Kimi Code 会话」
- 「继续最近的 MiniMax Code / mcode 会话」
- 「继续最近的 MiMo / MiMoCode 会话」
- 「把 Cursor 里那个调试到一半的会话交给当前模型继续」
- 「继续最近的 OpenCode 会话」
- 「继续最近的 OpenClaw / Clawdbot 会话」
- 「继续最近的 Pi / pi CLI 会话」
- 「把 Qoder 里的任务交给当前模型继续」
- 「继续最近的 WorkBuddy 会话，看看还有什么没做完」
- 「继续最近的 ZCode 会话，看看还有什么没做完」

已知会话 ID 时可以直接指定（支持 ID 前缀）：

- 「接管 Claude 会话 `a1b2c3d4`，继续之前的工作」
- 「用 resume-kimi 恢复会话 `9f8e7d6c-...` 的进度」

在支持 slash command 调用 Skill 的 agent 中，也可以用斜杠命令显式触发对应 Skill，再说明需求：

```
/resume-claude 列出当前项目的会话
/resume-agy --conversation a1b2c3d4
/resume-codex --session a1b2c3d4
/resume-continue 继续当前项目最近的会话
/resume-dsh 继续当前项目最近的会话
/resume-hermes 继续当前项目最近的会话
/resume-kilo 继续当前项目最近的会话
/resume-kimi 继续最近一个会话的未完成工作
/resume-minimax 继续当前项目最近的会话
/resume-mimo 继续当前项目最近的会话
/resume-opencode 继续当前项目最近的会话
/resume-openclaw 继续当前项目最近的会话
/resume-pi 继续当前项目最近的会话
/resume-qoder --session 9f8e7d6c
/resume-workbuddy 继续当前项目最近的会话
/resume-zcode 继续当前项目最近的会话
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

其余 Skill 同理，替换脚本路径即可（`resume-agy` / `resume-codex` / `resume-copilot` / `resume-continue` / `resume-cursor` / `resume-dsh` / `resume-grok` / `resume-hermes` / `resume-kilo` / `resume-kimi` / `resume-minimax` / `resume-mimo` / `resume-openclaw` / `resume-opencode` / `resume-pi` / `resume-qoder` / `resume-workbuddy` / `resume-zcode`）。

`resume-continue` 读取的是纯 JSON 会话文件，两套实现均无 SQLite / Zstandard 等额外要求，任意 Node.js 与 Python 3.7+ 版本均可运行。

OpenCode、Kilo（kilo.db）、MiniMax（v2/sqlite/runtime-state.sqlite）、MiMo（mimocode.db）、OpenClaw（openclaw-agent.sqlite）、ZCode 与 Hermes 当前版本均使用 SQLite，Node 实现需要 Node.js 22.5+ 的内置 `node:sqlite`；较低版本 Node 请直接运行对应 Python 脚本（`resume-minimax` 在低版本 Node 下会自动降级为仅读取会话 JSONL 并在提示中说明）。

DeepSeek Harness 的会话日志使用多帧 Zstandard；`resume-dsh` 的 Node 实现需要带标准库 Zstandard 支持的较新 Node.js，独立 Python 实现使用 Python 3.14+ 标准库 `compression.zstd`（较旧 Python 可安装 `zstandard` 包）。

## 接管摘要包含什么

- **会话信息**：标题、ID、项目路径、Git 分支、时间范围、消息数
- **历史摘要**：原会话中的 compact 摘要（若存在）
- **任务状态重建**：目标、已调查文件、执行过的命令、代码修改、测试结果、剩余问题
- **近期对话**：最近若干轮原始内容（含工具调用与结果）
- **接管建议**

各 Skill 的详细参数与输出说明见各自目录下的 `SKILL.md`。
