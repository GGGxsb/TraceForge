# TraceForge

面向本地代码仓库的 Web Code Agent Harness，使用 **FastAPI + React + OpenAI Python SDK** 构建。

TraceForge 将需求澄清、工具执行、权限审核、会话分支和上下文交接组织成可观察、可回查的 Agent Loop。项目面向单用户使用，目前处于持续开发阶段。

## 主要能力

| 能力 | 当前实现 |
| --- | --- |
| 需求澄清 | 轻量 TaskBrief；可从仓库确认的信息先调查，需要用户决定的信息逐题询问 |
| 无损会话树 | 每会话一个追加式 JSONL；按对话轮展示树，支持历史节点分叉和切回 |
| 代码状态管理 | 轮次检查点、恢复预览和回滚；Git 会话分叉使用独立 worktree，包含可管理的未提交文件 |
| 上下文交接 | compact 由模型更新项目唯一 handoff；新会话收到读取提示，精确历史按需搜索 JSONL |
| 权限控制 | 请求批准、帮我批准、完全访问权限；应用策略、人工审核与 Docker/Bubblewrap 执行 |
| Skills | 渐进披露名称和描述，按需读取 SKILL.md；支持显式 /skill:name |
| 只读 Sub-Agent | 独立上下文与日志，动态委派调查/审查任务，回传结构化结论和原文证据 |
| Web 交互 | 中文三栏界面、Markdown 回复、可读推理流、工具侧栏、文件/diff/分支与用量统计 |
| 运行中补充指令 | 当前批次结束后插入，或本轮结束后继续；支持取消待发送消息 |
| 评测 | 澄清门控、固定 Git fixture 的代码任务、轨迹分析、Sub-Agent 历史回查 |

## 快速开始

### Docker Compose 一键运行（推荐用于服务器）

只需 Docker Engine / Docker Desktop 的 Linux containers，以及 Docker Compose 2.17+。镜像会自动构建前端，并安装 Python、Node.js、pnpm、Git、ripgrep 和 Bubblewrap：

```bash
git clone <你的仓库地址> TraceForge
cd TraceForge
docker compose up -d --build
```

访问 <http://127.0.0.1:8000>，在“模型设置”填写 API Key、模型和 Base URL。没有 `.env` 也能启动；已有 `.env` 中的模型与预算配置会传入容器。端口冲突时在 `.env` 设置 `TRACEFORGE_PORT=18000`。

首次启动自动注册空项目 `/projects/default`，可以直接新建会话。已有代码仓库的导入、服务器访问、持久化和更新方法见[服务器运行](#服务器运行)。容器不提供原生文件夹选择器。

共享配置见 [`compose.yaml`](compose.yaml)、[`Dockerfile`](Dockerfile) 和 [`.dockerignore`](.dockerignore)。Web 镜像与 Caddy 代理分别运行，默认仅发布本机端口；没有使用 privileged 模式，也没有挂载 Docker socket。公网域名使用额外的 [`compose.public.yaml`](compose.public.yaml)，包含登录认证与 HTTPS，见[公网域名与登录认证](#公网域名与登录认证)。

### 源码运行环境要求

- Python 3.12+、Git、Node.js 22+ 和 pnpm。
- Windows：Docker Desktop，使用 WSL2 Linux Engine。
- Linux：Bubblewrap；图形文件夹选择器需要 `python3-tk`。
- 一个提供 **OpenAI Responses API**、流式响应和 Function Calling 的模型服务。

仅兼容 Chat Completions 的服务不能直接使用当前适配器。思考展示取决于服务是否返回可读推理；加密推理项不会作为文本显示。

### Windows / PowerShell

在首次克隆的仓库根目录执行：

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"

pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build

docker build -t traceforge-runner:local infra/runner
.\.venv\Scripts\python -m traceforge
```

访问 <http://127.0.0.1:8000>：

1. 点击“模型设置”，填写 API Key、模型和可选 Base URL。
2. 点击“打开项目”，在运行后端的本机选择代码目录。
3. 在项目下创建会话，输入任务，并按需处理澄清与审批。

Runner 镜像包含 Bash、Git、Python、Node.js 和 ripgrep。容器内依赖与宿主环境分离；修改 `infra/runner/tool_worker.py` 后需要重新构建镜像。构建访问 Debian 官方源较慢时，可指定镜像源：

```powershell
docker build --build-arg DEBIAN_MIRROR=http://mirrors.aliyun.com/debian --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security -t traceforge-runner:local infra/runner
```

### Linux

先通过系统包管理器安装 Bubblewrap，以及需要时安装 `python3-tk`。随后在仓库根目录执行：

```bash
cp .env.example .env
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
.venv/bin/python -m traceforge
```

无图形桌面的服务器不能使用原生文件夹选择器，参见[服务器运行](#服务器运行)。

### 前后端分别开发

后端与前端各使用一个终端：

```powershell
# 后端：8000
.\.venv\Scripts\python -m uvicorn traceforge.main:app --reload

# 前端：5173，将 /api 和 WebSocket 代理至后端
pnpm --dir frontend dev
```

开发模式访问 <http://localhost:5173>。后端使用单 Worker；会话写锁与运行调度依赖进程内状态。

## 模型与运行配置

网页配置优先于环境变量，保存后新运行立即使用配置；运行中不能更换模型。Base URL 使用服务提供的 API 根地址，例如 `https://服务地址/v1`；留空使用环境变量或 SDK 默认地址。API Key 会发送至所选服务。

| 配置项 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | 密钥和主模型，也可在网页填写 |
| `OPENAI_BASE_URL` | 自定义 Responses API 地址 |
| `OPENAI_BRIEF_MODEL` | TaskBrief 模型，未指定时复用主模型 |
| `OPENAI_FALLBACK_MODEL` | 主模型在开始流式输出前请求失败时使用的备用模型 |
| `TRACEFORGE_CONTEXT_WINDOW` | 默认上下文窗口，默认 128000；网页可分别填写主/备用模型窗口 |
| `TRACEFORGE_RESERVE_TOKENS` | 预留输出空间，默认 16384 |
| `TRACEFORGE_MAX_AGENT_ROUNDS` | 主 Agent 工具轮次上限，默认 50 |
| `TRACEFORGE_MAX_RUN_TOKENS` / `TRACEFORGE_MAX_RUN_COST_USD` | 单轮用量/费用上限，0 为不限制 |
| `TRACEFORGE_INPUT_USD_PER_1M` / `TRACEFORGE_OUTPUT_USD_PER_1M` | 按实际价格估算费用；费用预算需先配置价格 |
| `TRACEFORGE_DATA_DIR` / `TRACEFORGE_CONFIG_DIR` | 运行数据和网页模型配置目录 |
| `TRACEFORGE_PORT` | Compose 发布的本机端口，默认 8000 |
| `TRACEFORGE_INITIAL_WORKSPACE` | Docker 启动项目目录，默认 `/projects/default`；由容器入口注册 |
| `TRACEFORGE_DOMAIN` / `TRACEFORGE_AUTH_USER` / `TRACEFORGE_AUTH_PASSWORD_HASH` | 公网 Compose 的域名、登录账号与 bcrypt 密码哈希；后端模型密钥与登录密码独立 |
| `TRACEFORGE_DOCKER_IMAGE` | Windows Runner 镜像，默认 `traceforge-runner:local` |
| `TRACEFORGE_COMMAND_TIMEOUT` / `TRACEFORGE_APPROVAL_TIMEOUT` | 命令/审批超时，默认 120/600 秒 |

基础模板见 [`.env.example`](.env.example)。窗口大小按服务实际限制填写；启用备用模型时按两个窗口中的较小值压缩。最后一次调用可能使累计用量略超预算阈值。

## Agent Loop 与上下文管理

```text
用户消息 → TaskBrief → 仓库调查 / 用户澄清
         → 构建上下文 → 模型响应 → 参数校验
         → 策略判定 → 必要时审批 → 执行并记录结果
         → 批次结束 Hook / 上下文检查 → 继续或结束
```

### 澄清与工具

普通问候直接回复。TaskBrief 只对会改变任务方向的缺口进行澄清，能从仓库发现的信息先调查。用户可点选建议答案或逐题填写回答。

工具包括列目录、搜索、分段读取、创建、精确补丁、移动、删除、命令、Git 状态/diff、Skill 与历史查询。参数按注册的 JSON Schema 校验；只读工具可并发执行，修改和命令按模型输出顺序执行。较长输出保存完整 artifact，模型收到裁剪预览。

### JSONL 与项目 handoff

- 消息、工具调用/结果、审批等事件只追加写入 JSONL，`id + parent_id` 连接会话树。
- 每次模型请求前，以及准备继续的完整工具批次结束后检查上下文用量。
- 接近上限时，`before_compaction / after_compaction` Hook 驱动模型更新项目唯一 `.traceforge/handoff.md`，成功后追加 `context_checkpoint`。
- 后续请求保留近期完整原文与 handoff 位置提示，**不自动注入 handoff 正文，不再生成滚动摘要**。
- 任务结束后交接尚未覆盖的新记录，没有新增则跳过；服务返回上下文超限时最多进行一次交接后裁剪重试。
- `search_history` 搜索当前会话或同项目其他会话的原文，`read_history_entry` 按实际 ID 分段读取。

handoff 不随单个会话删除而删除，`.traceforge/` 不参与代码检查点。流式模型请求发出后，不能在同一个请求中插入 compact。

## 会话分支、检查点与回滚

界面按对话轮展示树，底层保留逐事件日志。从历史轮次“分叉”或“切回”已有分支时，可选择是否将离开分支的结构化摘要回填；“仅切换”不调用摘要模型。

每轮开始/结束保存代码检查点，覆盖可管理的已跟踪及未忽略的未跟踪文件，包含未提交修改。Git 仓库新会话分支使用独立 worktree；普通目录使用单工作区恢复。切换前展示恢复预览并校验磁盘状态。选中节点还可只恢复代码，保留当前对话位置。

检查点跳过生成目录和敏感文件；链接或大小超限会使检查点失败。单独回滚时 Git HEAD 改变会拒绝跨提交恢复；会话分叉可在独立 worktree 中恢复历史提交。没有检查点的旧节点无法恢复当时文件。

多个会话共用工作区时，磁盘代码与会话检查点不同，会要求恢复代码或接纳当前文件并建立检查点。项目移动后可“重新定位”，已有独立 worktree 的项目暂不支持。归档保留历史；删除移除会话日志、artifact、子任务和检查点，并按 worktree 修改/提交状态处理代码保留。

## 权限模式与沙箱

| 模式 | 审批与执行方式 |
| --- | --- |
| 请求批准 | 项目外编辑和联网每次询问，其他检测到的风险操作也需审批 |
| 帮我批准 | 对检测到的风险操作询问，普通联网可在沙箱中执行 |
| 完全访问权限 | 确认切换后以当前用户身份在宿主机执行，可访问外部文件和互联网，不逐次审批 |

前两档的项目内修改/命令通过 Docker/Bubblewrap，沙箱不可用即拒绝，不自动回退宿主机；明确获批的项目外编辑只开放精确路径。完全访问权限明确使用宿主执行，项目外修改不进入当前项目检查点或 diff。

Docker 默认断网、非 root、只读根文件系统，仅挂载工作区和临时目录，移除 capabilities 并限制资源。Bubblewrap 使用 namespace 隔离。权限模式写入会话历史，运行中不能切换。

Compose 部署以非 root 应用容器作为外层边界，工具通过容器内 Bubblewrap 执行。应用容器可以联网调用模型 API，工具默认断网，按会话策略开放。此时“完全访问权限”的宿主是应用容器，只能访问容器可见文件、挂载目录和网络，不能访问未挂载的服务器文件。修改外层挂载范围仍需由使用者修改部署配置。

## Skills 与扩展

### 渐进加载 Skill

发现时只提供名称、描述和位置，模型按需用 `read_skill` 读取正文或参考文件。显式 `/skill:name 任务说明` 会记录完整内容和 SHA-256 快照；`disable-model-invocation: true` 限制为显式调用。

支持项目及其父目录的 `.pi/skills/`、`.agents/skills/`（到 Git 根停止），用户的 `~/.pi/agent/skills/`、`~/.agents/skills/`，以及 TraceForge 配置目录的 `skills/`。示例见[项目 Skill](.pi/skills/traceforge-review/SKILL.md)。

```markdown
---
name: review-code
description: Review code when asked for correctness or maintainability feedback.
---

# Review code
Read references/checklist.md, then inspect the relevant code and report concrete findings.
```

Skill 不注册可执行工具、不改变权限。`read_skill` 只读取目录内最多 64 KiB 的文本，脚本执行仍受现有工具与会话权限约束。全局 Skill 不直接挂载到沙箱。

### Hook 与自定义工具

`TRACEFORGE_HOOKS=package.module:HookClass` 注册受信任的本地 Python Hook，支持上下文注入、事件追加、工具缩减和风险升级，不能降低核心安全限制。

`TRACEFORGE_TOOL_PLUGINS=package.module:register_tools` 注册命令工具：

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

Hook/注册函数是宿主 Python 代码，应由使用者审查。模型参数作为独立 argv 传入，不能覆盖核心工具；执行遵守会话权限。

## 只读 Sub-Agent

主模型通过 `delegate_task(role, task, context_entry_ids)` 创建独立子任务：

- `explore`：调查代码或回查 JSONL 原文。
- `reviewer`：独立审查冻结代码与 Git diff。

角色固定，具体任务动态指定；使用同一套只读工具，提示词侧重点不同。主 Agent 通过 `await` 等待结果后继续模型循环。当前不支持后台协作或用户配置自定义角色。

子任务保留独立上下文、JSONL、同项目历史来源和代码快照。搜索覆盖多个会话/分支，返回真实 ID、父节点与 `on_active_branch`；报告只能引用已读取的节点或文件。主会话收到短报告，完整轨迹与引用原文在“工具”侧栏展开。

子 Agent 不能改文件、运行任意命令、联网、嵌套委派或更新唯一 handoff；父会话的完全访问权限不会扩大其权限。快照包含可管理的未提交文件，排除 `.env*`、凭据、Git 元数据和生成目录；代码通过固定 Tool Worker 在只读、断网沙箱中读取。主 Agent 行动前需核对当前代码。

每次主运行最多委派两次，服务同时只执行一个子任务。子任务最多 8 个批次、每批 16 次工具调用、120 秒、24K 累计 token，计入主运行预算。超限返回已读取证据并标记部分完成；取消/重启记录中断，不自动重放。各 JSONL 的 `parent_id` 只连接各自日志内节点，父子关系通过父会话、运行与委派入口 ID 关联。

## 数据与仓库提交

未配置环境变量时，默认目录为：

| 平台 | 运行数据 | 网页模型配置 |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\TraceForge` | `%LOCALAPPDATA%\TraceForge\config` |
| Linux | `$XDG_DATA_HOME/traceforge` 或 `~/.local/share/traceforge` | `$XDG_CONFIG_HOME/traceforge` 或 `~/.config/traceforge` |

`.env.example` 将数据目录设为仓库内 `.traceforge-data/`，该目录及其测试变体由 Git 忽略。网页配置包含 API Key，与代码分开存储，读取接口不回显密钥。

Compose 覆盖数据/配置目录为 `/data` 和 `/config`，并将项目放在 `/projects`；三个目录分别使用 Docker named volume，均不会写入镜像。

运行数据包含会话、artifact、检查点、worktree、子任务和 handoff 状态。`.gitignore` 排除环境文件、模型配置、运行数据、项目 handoff、评测产物、依赖和构建缓存；保留 `.env.example`、前端锁文件、Skill 示例、评测脚本与用例。

本机 `projects/`、`docker-data/`、`docker-config/` 与 Compose override 文件也由 Git 忽略。`.dockerignore` 只将构建所需源码、前端锁文件和 Tool Worker 放入构建上下文，排除 `.env`、密钥、Git 历史、会话数据、评测产物和本机依赖。

`evals/results/` 的报告、原始轨迹、补丁和截图保留在本地，不随仓库提交。自定义数据目录需额外配置忽略规则；忽略规则不会移除旧提交中的内容。公开评测结果需先复核和脱敏，放入独立文档目录。

## 测试与评测

```powershell
# 不需要 API Key 的自动测试
.\.venv\Scripts\python -m pytest
pnpm --dir frontend test
pnpm --dir frontend build

# Docker 可选隔离/工具回归，需要已构建的 Runner
$env:TRACEFORGE_TEST_DOCKER='1'
.\.venv\Scripts\python -m pytest backend/tests/test_subagents.py backend/tests/test_tool_recovery.py -q

```

真实模型评测使用网页或环境配置，会消耗模型用量：

```powershell
# 澄清门控及模拟回答复检
.\.venv\Scripts\python evals/run_clarification.py --followup

# 固定 fixture / 参考实现自检，不调用模型
.\.venv\Scripts\python evals/run_code_tasks.py --suite complex --validate-only

# 7 个跨模块复杂任务，每题重复 3 次；另有 3 个 smoke 任务
.\.venv\Scripts\python evals/run_code_tasks.py --suite complex --repeats 3

# 真实模型的子任务历史回查
.\.venv\Scripts\python evals/run_subagent_smoke.py
```

代码任务从固定 Git commit 开始，在独立评分仓库应用最终补丁，执行隐藏测试与现有测试，记录用量、耗时、工具错误和无关修改。子任务烟雾评测直接发起委派，不评测主模型是否自主选择委派。这些是初始回归基线，不代表公开 Benchmark 或真实项目总体成功率。

方法与参数见 [`evals/README.md`](evals/README.md)。Windows Docker 已做真实隔离测试；Linux Bubblewrap 有参数边界测试，实际隔离需在 Linux 验证。

## 服务器运行

### Docker Compose 部署

在服务器克隆仓库后执行 `docker compose up -d --build`。Compose 将端口固定发布在服务器的 `127.0.0.1`，本机模式不启用登录认证，通过 SSH 隧道从自己的电脑访问：

```powershell
ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 用户名@服务器地址
```

打开 <http://127.0.0.1:18000>。如果服务器设置了 `TRACEFORGE_PORT`，将命令最后的 `8000` 替换为对应端口。

启动后先查看状态：

```bash
docker compose ps
docker compose logs --tail=100 traceforge
docker compose exec traceforge python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/api/health').read().decode())"
```

健康检查验证 HTTP 与 Bubblewrap namespace probe，不要求已配置模型。填写模型后 `model_configured` 与 `sandbox.ready` 应均为 `true`。

#### 导入已有代码

在容器的持久化项目卷中克隆，然后通过 API 注册。以下导入命令在服务器的 Bash 执行：

```bash
docker compose exec traceforge git clone <目标项目仓库地址> /projects/my-repo
docker compose exec -T traceforge python - /projects/my-repo <<'PY'
import json, sys, urllib.request
request = urllib.request.Request(
    'http://127.0.0.1:8001/api/workspaces',
    data=json.dumps({'path': sys.argv[1]}).encode(),
    headers={'Content-Type': 'application/json'},
)
print(urllib.request.urlopen(request).read().decode())
PY
```

刷新网页后选择该项目。私有仓库的凭据由使用者单独配置，默认不会挂载服务器的 SSH 密钥或主目录。默认空项目是普通目录；需要 Git 分支和 worktree 时使用实际 Git 仓库。

如需编辑服务器已有目录，创建仅本机使用的 `compose.override.yaml`：

```yaml
services:
  traceforge:
    volumes:
      - /srv/projects/my-repo:/projects/my-repo
```

再执行 `docker compose up -d`，用上述 API 命令注册 `/projects/my-repo`。绑定目录必须允许容器 UID/GID `10001:10001` 读写；只调整目标代码目录的权限。已有 JSONL、检查点和 worktree 中保存的是容器路径，之后需保持挂载路径一致。

#### 数据、更新与停止

| 卷 | 容器路径 | 内容 |
| --- | --- | --- |
| `data` | `/data` | 会话 JSONL、artifact、检查点、worktree、Sub-Agent 与 handoff 元数据 |
| `config` | `/config` | 网页保存的模型配置，包括 API Key；全局 Skills |
| `projects` | `/projects` | 代码仓库与项目 `.traceforge/handoff.md` |
| `proxy_data` / `proxy_config` | Caddy 的 `/data` / `/config` | HTTPS 证书和代理运行状态 |

这些 named volume 在容器重建和普通 `docker compose down` 后保留。备份数据、配置、项目卷及单独绑定的代码目录，公网部署也需保留代理证书卷；恢复时保持 Compose 项目名和容器路径一致。`docker compose down -v` 会删除 named volume，包括项目代码，不能当作普通停止命令。

```bash
# 先结束正在运行的任务，再更新
git pull
docker compose up -d --build

# 重启 / 停止 / 查看日志
docker compose restart traceforge
docker compose down
docker compose logs -f --tail=100 traceforge
```

Debian 官方源访问较慢时可使用构建参数，不修改公共 Dockerfile：

```bash
docker compose build --build-arg DEBIAN_MIRROR=http://mirrors.aliyun.com/debian --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
docker compose up -d
```

#### 容器隔离与公网入口

应用容器以 UID 10001 运行，根文件系统只读，移除全部 capabilities，并设置内存、CPU、PID 限制。为允许 Bubblewrap 创建嵌套的非特权 namespace，Compose 将 seccomp 与 AppArmor 设为 `unconfined`；这是明确的隔离取舍，并不等价于 Docker 默认安全配置。没有启用 `privileged`、`SYS_ADMIN`，也不挂载 Docker socket 或服务器根目录。内核禁止 user namespace 时健康检查会失败；需要在目标服务器验证，不能静默回退为无沙箱执行。

Caddy 与应用共享网络 namespace，应用只监听其中的 `127.0.0.1:8001`；Caddy 转发到该本地地址，因此网页模型设置保留原有的本地连接限制。应用不接受代理请求头改写客户端地址，Caddy 管理 API 关闭。默认只发布代理的本机 8000 端口；公网模式仅额外发布代理的 80/443，后端 8001 不发布。

### 公网域名与登录认证

本方案用于自己或信任的人登录使用。TraceForge 仍是单用户应用：**所有登录者共享 API Key、项目、会话和权限**。登录认证阻止未授权访客访问 Agent，不提供每用户独立密钥或数据隔离。仅将“模型设置”页面隐藏，无法阻止别人调用使用服务器密钥的运行接口。

1. 将专用子域名（如 `agent.example.com`）的 DNS 指向服务器，放行 TCP 80/443。若设置 AAAA 记录，IPv6 也必须正确指向服务器。8000/8001 不需要放行公网。
2. 首次克隆时复制 `.env.example` 为 `.env`；已有 `.env` 时直接编辑，不覆盖已有模型配置。
3. 生成网站登录密码哈希；命令会交互读取密码，不把明文密码写入命令历史：

   ```bash
   docker run --rm -it caddy:2.10-alpine caddy hash-password
   ```

4. 在 `.env` 配置域名、账号和输出的完整哈希。域名只填写主机名，不包含协议或路径；**哈希外必须保留单引号**，避免其中的 `$` 被 Compose 当作变量展开：

   ```dotenv
   TRACEFORGE_DOMAIN=agent.example.com
   TRACEFORGE_AUTH_USER=owner
   TRACEFORGE_AUTH_PASSWORD_HASH='粘贴上一条命令生成的完整bcrypt哈希'
   ```

5. 校验配置并启动：

   ```bash
   docker compose -f compose.yaml -f compose.public.yaml config --quiet
   docker compose -f compose.yaml -f compose.public.yaml up -d --build
   ```

6. 打开 `https://agent.example.com`，浏览器先提示登录；通过后再配置模型或开始任务。Caddy 为域名申请并续期 HTTPS 证书，认证覆盖页面、全部 API 与 WebSocket 握手；代理会移除登录用的 Authorization 头再转发至应用。

不要直接运行不带 `--quiet` 的 `docker compose config` 并公开输出，它可能包含环境变量中的模型密钥和密码哈希。`.env`、本地 Compose override、运行数据和项目代码已由 Git 与构建上下文规则排除；镜像不包含模型密钥。

公网模式后续更新、重建、停止都使用同一组 `-f` 参数，例如：

```bash
git pull
docker compose -f compose.yaml -f compose.public.yaml up -d --build
docker compose -f compose.yaml -f compose.public.yaml logs -f --tail=100 proxy
docker compose -f compose.yaml -f compose.public.yaml down
```

修改域名、账号或密码哈希后执行相同的 `up -d` 命令，使代理按新环境变量重建。HTTPS 配置需要在实际域名和服务器上验证；本仓库没有自动部署到外部服务器。

若服务器已有占用 80/443 的反向代理，可以只运行本机 Compose 服务，再让现有入口代理本机 8000。必须在现有入口实现同等认证，覆盖 HTTP 与 WebSocket；不要同时启动会争用端口的公网 Compose 代理。

认证与证书行为参考 [Caddy basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth) 和 [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)。

Docker 配置参考：[Compose 环境变量](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)、[Docker seccomp](https://docs.docker.com/engine/security/seccomp/)、[Bubblewrap](https://github.com/containers/bubblewrap)。

### 不使用 Docker 的 Linux 源码部署

绑定 `127.0.0.1:8000`，通过 SSH 隧道访问：

```bash
# 在服务器项目根目录，以普通用户运行
.venv/bin/python -m uvicorn traceforge.main:app --host 127.0.0.1 --port 8000 --workers 1
```

```powershell
# 在自己的电脑建立隧道；18000 避免与本地服务的 8000 冲突
ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 用户名@服务器地址
```

随后访问 <http://127.0.0.1:18000>。服务只保留一个 Worker，部署时构建前端并由 FastAPI 托管，不运行 Vite 开发服务器。Linux 当前自动选择 Bubblewrap，不会因安装了 Docker 就自动改用 Docker。

无图形桌面时，“打开项目”依赖的 tkinter 对话框不可用。可暂时在服务器调用现有 API 登记服务器代码目录，再刷新页面：

```bash
curl --fail-with-body http://127.0.0.1:8000/api/workspaces \
  -H 'Content-Type: application/json' \
  --data '{"path":"/srv/projects/my-repo"}'
```

工作区属于服务器文件系统；电脑上的项目需要先推送并在服务器克隆，或另行同步。运行数据、模型配置、代码目录与项目 handoff 应持久化并备份。模型设置更新接口要求本地连接，SSH 隧道满足该条件。

公网域名部署仍需认证覆盖 HTTP 与 WebSocket、HTTPS、反向代理 WebSocket 转发与模型设置管理策略，并使用普通 OS 用户和进程管理服务。当前 `X-TraceForge-UI` 请求头不是登录认证，CORS 也不能代替访问控制。

SSH 转发参考 [OpenSSH 手册](https://man.openbsd.org/ssh.1)；沙箱要求参考 [Bubblewrap 项目](https://github.com/containers/bubblewrap)。具体安装命令与服务管理配置按服务器系统选择。

#### Ubuntu 24.04 单用户源码部署示例

以下假设以普通用户 `deploy` 登录，代码放在 `/home/deploy/TraceForge`；替换为自己的用户名与目录。安装系统依赖需要 sudo，应用和 Agent 以普通用户运行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv git bubblewrap ripgrep curl

# 先安装 Node.js 22+ 和 pnpm；检查版本后再继续
python3 --version
node --version
pnpm --version

git clone <你的仓库地址> /home/deploy/TraceForge
cd /home/deploy/TraceForge
python3 -m venv .venv
.venv/bin/python -m pip install -e .
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
cp .env.example .env
chmod 600 .env
mkdir -p /home/deploy/traceforge-data /home/deploy/traceforge-config
```

在 `.env` 中配置模型，或者通过 SSH 隧道进入网页后填写模型设置；同时设置独立的持久化目录：

```dotenv
TRACEFORGE_DATA_DIR=/home/deploy/traceforge-data
TRACEFORGE_CONFIG_DIR=/home/deploy/traceforge-config
```

先以前台方式运行上面的 Uvicorn 命令。在另一个服务器终端检查：

```bash
curl --fail-with-body http://127.0.0.1:8000/api/health
```

确认 `model_configured` 和 `sandbox.ready` 均为 `true`。若 namespace probe 失败，按返回原因检查服务器对非特权 user namespace 和 Bubblewrap 的支持，不能仅凭已安装 `bwrap` 判断可执行。

确认网页、项目注册和一次文件读取可用后，停止前台进程，将以下内容保存到 `/etc/systemd/system/traceforge.service`：

```ini
[Unit]
Description=TraceForge Web Code Agent
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/home/deploy/TraceForge
ExecStart=/home/deploy/TraceForge/.venv/bin/python -m uvicorn traceforge.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5
UMask=0077
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now traceforge
sudo systemctl status traceforge
journalctl -u traceforge -n 100 --no-pager
```

如果 Node.js/pnpm 安装在用户私有目录，需将实际可执行目录加入服务的 `Environment=PATH=...`，否则 Agent 的构建命令可能找不到它们。服务器上的目标项目还需要自己的运行时和依赖，TraceForge 安装完成不等于这些项目已经安装依赖。

更新代码时先结束正在运行的任务，再执行 `git pull`、`.venv/bin/python -m pip install -e .`、前端安装与构建，最后 `sudo systemctl restart traceforge`。运行数据和模型配置目录无需随代码更新覆盖。

## 项目结构与当前边界

```text
backend/traceforge/   Agent Loop、Hook、JSONL、上下文、权限、沙箱和 Sub-Agent
backend/tests/       后端机制与回归测试
frontend/src/        React 会话、工具、分支、diff 与配置界面
frontend/tests/      前端协议与展示逻辑测试
infra/runner/        Docker Runner 与固定 Tool Worker
infra/deploy/        容器入口与 Caddy 本机/公网代理配置
Dockerfile           Web 应用与前端的多阶段镜像构建
compose.yaml         单用户 Docker Compose 部署与持久化
compose.public.yaml  公网 HTTPS 与登录认证的 Compose 覆盖配置
evals/               评测 fixture、脚本与轨迹分析
.pi/skills/          项目 Skill 示例
```

核心入口：[`agent.py`](backend/traceforge/agent.py)、[`storage.py`](backend/traceforge/storage.py)、[`context.py`](backend/traceforge/context.py)、[`memory.py`](backend/traceforge/memory.py)、[`subagents.py`](backend/traceforge/subagents.py)。

当前没有 MCP client、应用内多用户账户与数据隔离、服务器文件夹选择 UI、交互式终端、macOS Seatbelt、后台子任务通信或多 Agent 并行修改代码。Docker Compose 提供单用户服务器部署模板，公网模式通过 Caddy 登录认证，仅供自己或信任的人使用。普通目录提供有限快照 diff；完整分支代码管理依赖 Git。
