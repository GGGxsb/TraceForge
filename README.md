# TraceForge

TraceForge 是一个面向本地代码仓库的 Web Code Agent。它使用 FastAPI、React 和 OpenAI Responses API，实现四个核心机制：

- Hook 驱动的轻量 TaskBrief 与需求澄清；
- JSONL 无损会话树和分层上下文投影；
- 应用策略、人工审核和 OS 沙箱三层防护；
- 基于最近公共祖先的 Branch Summarization。

## 本地启动

要求 Python 3.12+、Node.js 22+、pnpm。Windows 需要启动 Docker Desktop Linux Engine；Linux 需要安装 Bubblewrap。

```powershell
Copy-Item .env.example .env
# 可在网页右上角的“模型设置”填写 API Key 和模型；.env 中的 OPENAI_* 仍可作为默认值

python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
docker build -t traceforge-runner:local infra/runner
.\.venv\Scripts\python -m uvicorn traceforge.main:app --app-dir backend --reload
```

如果 Docker 构建访问 Debian 官方源较慢，可以指定镜像源：

```powershell
docker build --build-arg DEBIAN_MIRROR=http://mirrors.aliyun.com/debian --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security -t traceforge-runner:local infra/runner
```

另开终端启动前端：

```powershell
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev
```

访问 `http://localhost:5173`，点击左侧“打开项目”选择本地代码目录，再在项目下创建会话。

构建前端后，FastAPI 会直接托管 `frontend/dist`，此时可以只启动一个服务：

```powershell
pnpm --dir frontend build
.\.venv\Scripts\python -m traceforge
```

访问 `http://127.0.0.1:8000`。左侧列出已打开的项目，点击项目即可查看该项目的会话；“打开项目”在运行后端的本机选择文件夹。Linux 图形桌面需要安装 `python3-tk`。

点击右上角“模型设置”，填写 OpenAI API Key、主模型、可选的 Base URL 和 TaskBrief 模型。Base URL 通常形如 `https://服务地址/v1`；留空使用 `OPENAI_BASE_URL` 环境变量或 SDK 默认地址。自定义服务必须兼容 Responses API，API Key 会发送至所填地址。保存后，新运行立即使用该配置，无需重启；正在执行 Agent 时不能更改模型。API Key 不会通过读取接口或页面回显。网页配置保存在用户配置目录（Windows 为 `%LOCALAPPDATA%\TraceForge\config`，Linux 为 `$XDG_CONFIG_HOME/traceforge` 或 `~/.config/traceforge`），独立于工作区和会话数据目录；设置 `TRACEFORGE_CONFIG_DIR` 可覆盖。配置文件包含密钥，请勿将配置目录放入代码工作区。移除网页配置后恢复 `.env` 默认值。

Linux 将虚拟环境路径替换为 `.venv/bin/python`，并安装 `bubblewrap`。默认和“帮我批准”模式在沙箱不可用时会拒绝执行工具，不会自动回退到宿主机。只有用户明确将当前会话切到“完全访问”后，执行工具才会走宿主机。

每个会话的输入框下方可切换权限模式，选择会写入该会话的 JSONL 历史；运行期间不能切换。默认“请求审批”对需要审核的操作暂停并询问用户，核心禁止项始终拒绝。“帮我批准”自动批准可审核操作，仍使用工作区路径限制和 Docker/Bubblewrap 沙箱；核心禁止项仍拒绝。“完全访问”会在切换前显示确认对话框，此后执行工具以当前用户身份在宿主机运行，可访问项目外文件和宿主网络，且不逐次请求审批。全局模型密钥不会作为进程环境变量转发给宿主命令，但当前用户可访问的其他文件和环境仍可被工具访问。切回较低模式后，后续执行重新受相应限制。权限模式控制 Agent 工具执行，不改变已产生的文件或检查点。

## 数据目录

默认数据位置：

- Windows：`%LOCALAPPDATA%\TraceForge`
- Linux：`$XDG_DATA_HOME/traceforge` 或 `~/.local/share/traceforge`

设置 `TRACEFORGE_DATA_DIR` 可以覆盖。每个会话保存为独立 JSONL 文件，完整工具输出保存在 `artifacts/`；滚动摘要和分支摘要只改变发送给模型的上下文投影，不删除原始记录。

项目有稳定 ID，多个会话共享项目登记信息。移动项目目录后，点击项目下方的“重新定位”并选择新目录；已有会话会通过项目 ID 读取新路径，JSONL 历史头保持原样。为防止误把历史检查点用于其他目录，已有会话时必须先移走旧目录；已有独立 Git worktree 分支的项目暂不支持重新定位。

右侧“分支”页将每轮对话画成带父子连线的节点，可展开宽屏树图、拖动空白区域平移画布、点击节点查看信息。“分叉”从任意轮次（包括当前轮次）创建新路径；有已保存叶节点的历史分支会显示“切回”。每轮运行开始和结束时自动保存代码检查点。Git 仓库的新分叉会创建独立 Git worktree，并把目标检查点恢复到新目录；切回已有分支直接使用其原目录，原分支的代码不被覆盖。普通目录仍使用单工作区检查点恢复。切换前的对话框会列出要新增、修改和删除的文件，并在操作前重新校验工作区未发生变化。选中会话树节点可单独“恢复此轮代码”，对话仍留在当前分支。跨分支操作前，还可选择是否将离开分支的关键内容压缩成结构化摘要并回填到目标分支：选择“仅切换”不会新增摘要，也不会调用摘要模型。下一条消息会接在选定位置之后，旧分支和其代码检查点均保留。工具、审批和状态记录仍归入所属对话轮，运行开始前的状态记录不会额外占用节点。底层 JSONL 仍逐条保存完整记录；`/api/sessions/{id}/tree?granularity=entry` 可查看逐记录原始树。

检查点数据位于数据目录的 `checkpoints/<session-id>/`，原始会话仍只追加 JSONL。Git 仓库保存已跟踪及未忽略的未跟踪文件中可管理的代码文件；普通目录扫描可管理的代码文件。两者均跳过生成与依赖目录、敏感路径；符号链接或文件大小超限会使该检查点失败并在界面显示错误。单独回滚代码时，Git HEAD 已变化会拒绝跨提交恢复；会话分叉则在独立 worktree 中允许切到历史提交。旧版会话的历史节点没有代码检查点，无法推断并恢复当时的文件；继续运行后产生的新轮次可正常使用。`GET /api/sessions/{id}/checkpoints` 可查看检查点记录，`GET /api/sessions/{id}/branch-preview` 可预览恢复文件，`POST /api/sessions/{id}/rollback` 可只回滚代码。

多个会话可以指向同一个工作区。进入另一个会话后，若当前磁盘文件或 Git 暂存区与该会话的检查点不同，TraceForge 会暂停该会话的 Agent 运行与分支切换，并让用户选择“恢复此会话代码”或“使用当前文件并建立检查点”。后者会明确将当前文件状态接纳为此会话的新状态；不会偷偷覆盖原检查点。Git 的索引元数据刷新不会被误判为代码差异。服务意外中断于文件恢复中途时，启动恢复会先保存当时的文件，然后根据已持久化的切换记录完成或撤回恢复。

左侧会话列表显示对话轮数（用户消息和逐题澄清回答），不再显示 JSONL 底层事件数。归档会话会保留历史并禁止继续运行，可从“已归档”恢复；删除会话需要在页面确认，随后会永久移除该会话 JSONL 和工具日志。若独立 worktree 有未提交修改，删除会被拒绝；若有新提交，则先创建 `traceforge-preserved/` Git 引用再移除工作树。新运行会明确把所选工作区的宿主路径及沙箱中的 `/workspace` 映射告诉 Agent。对使用思考模式的 Responses 服务，模型返回的推理项会保存在 JSONL 并在工具调用后原样回传；同一次模型响应的所有工具调用先于其结果写入。DeepSeek 某轮未提供可回传推理项时，该轮原始记录仍在 JSONL，API 请求改用文本证据投影，避免后续请求因缺少推理项报 400。

Agent 的正式回复和流式回复在界面按 Markdown 渲染，支持标题、列表、链接、表格和带语法高亮的代码块；模型输出中的原始 HTML 不作为页面 HTML 执行。

每轮对话标题旁的“工具”入口会打开右侧工具面板；工具参数、结果和完整输出链接在该面板中按需展开，因此工具日志不会推动主对话滚动。DeepSeek 等模型提供可读推理时，思考过程在对应对话回合中以单行预览实时更新，点击可展开完整文本；右侧不再重复显示思考。响应结束后完整推理项仍保存在 JSONL 中。若服务只提供加密推理项，界面不会显示密文。

普通问候会直接由 Agent 回复。只有缺少会改变任务方向的关键信息时，TaskBrief 才生成逐题澄清卡片；每题可以点选建议答案或自行输入。全部问题回答后 Agent 自动继续，问题和答案分别保存在会话树中。可从仓库查明的缺口会先进行只读调查。

Agent 提供文件列表、代码搜索、分段读取、Git 状态与差异，以及创建、精确修改、移动和删除文件的工具。移动与删除需要人工审批；文件操作在工作区内按模型输出顺序执行。较长的读取和命令输出会保存完整 artifact，模型仅接收裁剪预览。运行中刷新页面可重新显示当前运行和停止按钮；后端异常重启会将中断的运行记为失败，保留原始记录。

## Skills（按需加载）

TraceForge 采用与 Pi 类似的渐进加载：扫描 Skill 时只把名称、描述和位置提供给模型，模型需要时调用 `read_skill` 读取完整 `SKILL.md` 或其 `references/` 等文本文件。右侧“技能”页展示已发现的 Skill；点击“使用”会在输入框填入 `/skill:name`，可在后面继续写任务，例如 `/skill:review-code 检查登录流程`。显式调用时，完整 Skill 内容及 SHA-256 快照写入会话 JSONL；滚动摘要后仍在当前分支上下文中。将 `disable-model-invocation: true` 写入 frontmatter 后，该 Skill 只接受显式调用。

支持的目录包括当前工作区及其父目录的 `.pi/skills/`、`.agents/skills/`（遇到 Git 根目录即停止继续上溯），全局 `~/.pi/agent/skills/`、`~/.agents/skills/`，以及 TraceForge 用户配置目录的 `skills/`。每个 Skill 是一个含 `SKILL.md` 的目录：

```markdown
---
name: review-code
description: Review code when asked for correctness, security, or maintainability feedback.
---

# Review code
Read references/checklist.md, then inspect the relevant code and report concrete findings.
```

Skill 仅提供工作流程和参考资料，不会注册可执行工具或自行改变权限模式；内附脚本仍需通过现有命令工具和当前会话的权限模式执行。项目 Skill 位于工作区内，可由沙箱中的命令访问；全局 Skill 位于工作区外，受限模式只能通过 `read_skill` 读取文本，不能直接在沙箱中运行其脚本。`read_skill` 只能读取该 Skill 目录内最多 64 KiB 的文本文件，路径穿越、符号链接逃逸和敏感文件均会被拒绝。修改 Skill 文件后刷新页面即可更新技能列表。

受信任的本地 Python Hook 可以通过 `TRACEFORGE_HOOKS=package.module:HookClass` 注册。Hook 只能缩减工具或提高风险等级；配置项会执行本地 Python 代码，因此只应加载自己审查过的模块。后端按单 Worker 运行，以保证每个会话的进程内异步写锁有效。

## 注册沙箱工具

`TRACEFORGE_TOOL_PLUGINS=package.module:register_tools` 可加载受信任的本地 Python 注册函数。函数接收 `ToolPluginRegistry`，注册 `SandboxTool`：

```python
from traceforge.tool_plugins import SandboxTool

def register_tools(registry):
    registry.register(SandboxTool(
        name="search_todos",
        description="Search TODO markers with ripgrep.",
        properties={"query": {"type": "string"}},
        argv=("rg", "-n", "{query}", "."),
    ))
```

模型会收到工具的严格 JSON Schema。参数作为单独的命令参数引用；默认模式下命令只在沙箱内运行，新工具需要人工审核，联网还需显式设置 `network=True`。“帮我批准”自动批准其审核请求但仍用沙箱；“完全访问”在宿主机直接执行注册工具的参数数组。核心工具名不能被覆盖。注册函数本身是受信任的宿主 Python 代码，只配置自己审查过的模块；Skill 文件不能注册工具。首版尚未实现 MCP 连接。

## 运行统计与代码任务评测

右侧“统计”页按当前会话分支展示模型返回的输入、输出、缓存和推理 token，以及工具成功率与运行耗时。模型未返回用量时不会猜测。可在 `.env` 设置 `TRACEFORGE_INPUT_USD_PER_1M` 和 `TRACEFORGE_OUTPUT_USD_PER_1M`，按所填每百万 token 美元单价估算费用；不同模型价格不同时，应分别配置或将估算视为近似值。`TRACEFORGE_MAX_RUN_TOKENS` 与 `TRACEFORGE_MAX_RUN_COST_USD` 可限制每轮累计模型用量或估算费用，达到后 Agent 停止并询问是否继续。费用预算需要先配置模型单价，且可能在最后一次模型调用后略微超出阈值。模型设置中的备用模型仅在主模型**开始流式输出前**请求失败时使用，避免重复工具调用；它需要与当前 Base URL 兼容。

`evals/run_code_tasks.py` 在全新 Git 仓库中运行真实 Agent 任务，在 Agent 停止后才加入隐藏测试，并通过沙箱执行测试。每个任务独立重置，报告完成率、修改文件、耗时、用量和工具错误。运行前需要可用的模型与 Docker/Bubblewrap：

```powershell
.\.venv\Scripts\python evals/run_code_tasks.py --repeats 3
```

评测脚本在沙箱不可用时会明确退出，不会在宿主机执行 Agent 修改的代码。它只自动批准隔离评测仓库内不涉及网络、凭据、安装和 Git 写入的操作；其他审核请求自动拒绝并计入结果。3 个内置任务只是最小基线，应继续加入真实项目任务并保留未公开的验收测试。

主要实现位置：

- `backend/traceforge/agent.py`：Agent 状态机、审批等待、Hook 生命周期与工具批次；
- `backend/traceforge/storage.py`：追加式 JSONL 树、崩溃恢复和 WebSocket 补播数据；
- `backend/traceforge/context.py`：上下文投影、滚动摘要、LCA 分支回填；
- `backend/traceforge/security.py` 与 `sandbox.py`：策略层和 Docker/Bubblewrap 执行层；
- `frontend/src/App.tsx`：三栏会话、问题树、审批、diff、分支与沙箱状态界面。

## 测试与构建

```powershell
.\.venv\Scripts\python -m pytest
pnpm --dir frontend build
```

测试不需要 OpenAI API Key。模型设置页保存配置时不会发送验证请求；真实模型调用只在运行 Agent 时发生。
