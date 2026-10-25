# fishKernel

让大模型长时间自主运行的内核。

一句话概括：**存档目录是唯一状态，内核只是「读存档 → 调模型 → 执行工具 → 写回存档」的无限循环。**

- 状态全部落在文件里，进程崩了重启就能接着跑。
- 事件日志是事实来源，历史消息、上下文、界面展示都从它投影出来。
- 工具调用、异步进程、上下文压缩、用户控制都按可恢复的方式设计。

> **安全警告**：v1 有意给模型全部权限——`spawn` 可以执行任意命令。只在隔离的机器/容器里运行，不要放在有重要数据的机器上。

---

## 特性

| 能力 | 说明 |
|---|---|
| 无限循环 | 每轮：读控制 → 收消息 → 结算任务 → 构造上下文 → 调模型 → 执行工具 → 延时 |
| 事件日志 | `Runtime/events.jsonl` 只追加；崩溃后截断半行即可继续 |
| 对话投影 | 事件 → OpenAI `messages`，`tool_call` 与结果严格配对，缺失自动补 `TOOL_OUTCOME_UNKNOWN` |
| 异步进程 | `spawn` 起任务，输出落文件（不走管道，不会卡死），支持超时、杀进程组、重启接管 |
| 上下文压缩 | 阈值按模型窗口与输出预留推导；按消息边界选区；分块摘要；事务化提交 |
| token 计量 | 以 API 返回的 `usage` 为锚点，只对新增内容估算，并用实测比例校准 |
| 控制通道 | 暂停/恢复/停止/调延时/注入消息，全部通过存档内文件，任意进程或远程 shell 都能操作 |
| 单实例锁 | `flock`/`msvcrt` 锁住存档，第二个内核会被拒绝，不会写坏事件日志 |
| 交班重启 | `ctl restart`：旧进程先启动新进程，再释放锁并正常退出，不给自己发信号 |
| 唤醒 | `ctl wake` 或 TUI 的 `:wake`，让等待中的内核提前结束延时 |
| 自我保护 | 拒绝终止内核自身、父进程或同进程组的命令（只挡这一种，不是沙箱） |
| 终端界面 | `fishkernel tui`：上半屏实时事件流、下半屏输入框，分类着色，长工具调用自动压缩 |
| 崩溃恢复 | 未闭合的压缩事务、孤儿工具调用、孤儿进程都有明确处理路径 |
| 可插拔工具 | `Tools/` 目录登记命令行工具用法，模型通过 `spawn` 调用 |

## 快速开始

要求 Python ≥ 3.10（在 Debian 12 / Python 3.11 / aarch64 上验证过）。

```bash
git clone <repo-url> fishKernel
cd fishKernel
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

生成存档并启动：

```bash
fishkernel new ./archives/demo --name 小鱼 --api-key sk-...
# 可选：改 archives/demo/Memory/Agent.txt 写人格、规则、目标
fishkernel run ./archives/demo
```

另开一个终端观察和控制：

```bash
fishkernel status ./archives/demo --tail 20 --jobs
fishkernel tui ./archives/demo             # 实时观察 + 直接对话
fishkernel say ./archives/demo "去 Tools 目录看看有什么可用的"
fishkernel ctl ./archives/demo pause        # 暂停（不消耗 API）
fishkernel ctl ./archives/demo resume
fishkernel ctl ./archives/demo delay 5      # 每轮延时改为 5 秒
fishkernel ctl ./archives/demo wake         # 提前结束当前等待
fishkernel ctl ./archives/demo restart      # 交班重启
fishkernel ctl ./archives/demo stop
```

不花钱的离线自检（不调用 API）：

```bash
fishkernel run ./archives/demo --mock --max-turns 3
```

更完整的入门步骤见 [docs/quickstart.md](docs/quickstart.md)。

## 架构

```
                 ┌────────────── 存档目录（唯一状态）──────────────┐
                 │ basic.instance   配置：模型 / Key / max_tokens │
                 │ Memory/          Agent、长期记忆、摘要、历史    │
                 │ Tools/           工具登记 + 每个工具的 Usage   │
                 │ Runtime/         events.jsonl、控制文件、任务   │
                 └───────▲────────────────────────────┬───────────┘
                         │ 读                          │ 写
                 ┌───────┴────────────────────────────▼───────────┐
                 │                   内核 Kernel                   │
                 │  构造上下文 → 压缩判定 → 调 API → 执行工具 → 延时 │
                 └───────▲────────────────────────────┬───────────┘
                         │ messages + tools            │ 工具
                 ┌───────┴────────┐          ┌────────▼─────────────┐
                 │ DeepSeek API   │          │ 文件 / 异步进程 / 延时 │
                 │ openai SDK     │          │ 长期记忆              │
                 └────────────────┘          └──────────────────────┘
```

| 模块 | 职责 |
|---|---|
| `loop.py` | 主循环、错误自愈、信号处理 |
| `messages.py` | 事件 → `messages` 投影（纯函数） |
| `tokens.py` | token 计量：API 实测锚点 + 增量估算 + 比例校准 |
| `llm.py` | openai SDK 调用、重试、超限识别、mock 客户端 |
| `tools.py` | 内置工具分发与参数校验 |
| `jobs.py` | 异步进程：id、输出、超时、杀进程组、重启接管 |
| `compress.py` | 压缩：选区、摘要、分块、事务与恢复 |
| `control.py` | 存档内控制通道 |
| `archive.py` / `events.py` | 存档布局、事件日志、可读投影 |
| `cli.py` | `new / run / ctl / say / status` |

完整的协议与设计取舍见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 存档结构

```
archives/demo/
├── basic.instance              # 配置
├── Memory/
│   ├── Agent.txt               # 人格/规则（人写）
│   ├── LongTermMemory.txt      # 长期记忆（内核写，模型经 memory 工具发起）
│   ├── Digest.txt              # 最近一次压缩的概要（内核写）
│   ├── History.txt             # 人可读的对话投影
│   └── Archive-<时间>.txt      # 每次压缩一份被压缩区文本
├── Tools/
│   ├── ToolsRegister.txt       # 每行一个启用的工具目录名
│   └── <工具名>/Usage.txt      # 给模型看的用法说明
└── Runtime/                    # 运行时数据，不进 git
    ├── events.jsonl            # 唯一事实来源
    ├── kernel.json             # 内核自述：pid / 轮次 / 状态
    ├── kernel.log              # 运行日志
    ├── control/                # state.json + inbox.jsonl
    ├── jobs/<job-id>/          # meta.json + stdout.log + stderr.log
    └── spill/                  # 超长输出的完整副本
```

## 配置

`basic.instance` 是 `key = value` 纯文本，`#` 起始为注释：

```ini
name = 小鱼
model = deepseek-flash
api_base = https://api.deepseek.com
api_key = sk-...
max_tokens = 8192        # 单轮输出上限，同时参与压缩阈值计算
turn_delay = 1.0         # 每轮基础延时（秒），可被控制通道覆盖
temperature = 1.0
effort = high            # 可选：low / high / max，由服务端处理
```

密钥优先级：`api_key` → 环境变量 `FISHKERNEL_API_KEY` → `DEEPSEEK_API_KEY`。

模型参数以 DeepSeek-V4.1-Flash 为准：窗口 1,048,576、最大输出 393,216；默认输出上限 8192。

## 工具

内置 9 个工具：

| 工具 | 参数 | 说明 |
|---|---|---|
| `read_file` | `path, offset=0, limit=200` | 按行读；单次上限 256 KiB，大文件分段读 |
| `list_dir` | `path=".", limit=200` | 列目录 |
| `spawn` | `command, cwd?, label?, timeout_seconds=0` | 起异步进程，立即返回 `job_id` |
| `job_list` | – | 所有任务状态 |
| `job_output` | `job_id, offset=0, max_bytes=32768, stream=stdout/stderr` | 按字节读任务输出 |
| `job_kill` | `job_id` | 杀进程组 |
| `wait_job` | `job_id, timeout_seconds=30` | 有界等待，可被 stop 打断 |
| `sleep` | `seconds` | 可中断延时（≤3600） |
| `memory` | `mode=append/replace, content` | 写长期记忆（≤64 KiB） |

注册一个命令行工具：在 `Tools/<名字>/Usage.txt` 写清用法，并把名字加到 `Tools/ToolsRegister.txt`，下一轮模型就能看到。执行时模型用 `spawn` 调用它。

细节见 [docs/tools.md](docs/tools.md)。

## CLI

```text
fishkernel new   <目录> [--name ...] [--model ...] [--api-key ...] [--max-tokens ...] [--turn-delay ...]
fishkernel run   <目录> [--max-turns N] [--once] [--mock] [--quiet]
fishkernel tui   <目录> [--plain] [--snapshot] [--no-color] [--width N] [--height N] [--refresh 秒]
fishkernel ctl   <目录> pause|resume|stop|delay|wake|restart [秒数]
fishkernel say   <目录> <文本...>
fishkernel status <目录> [--tail N] [--jobs]
```

`status` 会显示模型、压缩阈值、内核状态、累计 token 用量、最近事件和任务列表。

## 上下文压缩与 token 计量

- 阈值 `floor(min(W×0.8, W−O−B))`，保留 `floor(0.16×(W−O))`；默认 `W=1048576, O=8192, B=65536` → 阈值 **838,860**、保留 **166,461**。
- 选区按不可分单元（assistant + 它的全部 tool 结果），不切调用对，不碰系统提示词。
- 摘要请求本身过大时先分块再合并；摘要不缩短输入就放弃本次压缩。
- 压力以最近一次响应的 `usage.prompt_tokens`（精确值）为锚点，只对锚点之后的新增内容估算，并用实测比例校准。DeepSeek 没有发送前计数接口。

详见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节。

## 崩溃恢复

| 崩溃位置 | 恢复动作 |
|---|---|
| assistant 已写、tool 结果未写 | 投影时补 `TOOL_OUTCOME_UNKNOWN`，模型知道结果未知 |
| 工具执行到一半 | 同上；异步进程由内核启动时接管或标记失败 |
| 压缩中 | 未闭合的 `compaction/start` 在启动时补 `compaction/abort{stale}` |
| 事件写一半 | 打开事件日志时截断到最后一个换行 |
| 控制文件写坏 | 视为缺省值继续运行 |

## 安全模型

- **全权限**：`spawn` 不设白名单，模型可以读写这台机器上的任何东西。这是 v1 的设计决定，不是遗漏。
- `selfguard` 只拒绝"终止内核自身/父进程/同进程组"这一类命令，不限制删文件、改系统配置等其它破坏性操作。
- 没有审批、没有预算上限、没有空闲退避（节奏只有 `turn_delay`）。
- 因此只能跑在一次性/隔离的测试机上；不要把宿主机、生产凭证或私人文件放进去。

## 测试

```bash
# 单测（111 项，不联网）
python -m pytest -q

# 推送隔离测试机 → 建 venv → 安装 → 跑全部测试
export FISHKERNEL_TEST_HOST=<test-host>
export FISHKERNEL_TEST_USER=<user>
export FISHKERNEL_TEST_PASSWORD=<password>   # 或配置 SSH key + NOPASSWD sudo
python scripts/deploy.py

# 追加真实 API 联调（限轮次，只读操作）
export FISHKERNEL_KEY=sk-...
python scripts/deploy.py --real-api
```

测试策略与脚本说明见 [docs/testing.md](docs/testing.md)。

## 项目结构

```
fishKernel/
├── src/fishkernel/        # 内核源码
├── tests/                 # pytest（111 项）
├── scripts/               # 部署与联调脚本
├── docs/                  # 使用文档
├── ARCHITECTURE.md        # 实现规范（协议、事务、恢复）
├── KernelDesign.md        # 最初的产品设计
└── pyproject.toml
```

## 已知限制

1. 一个存档只能跑一个内核实例：现在有文件锁强制，第二个会报 `LockError`（交班重启除外）。
2. `events.jsonl` 只增不减，长跑持续变大（压缩只写 `Archive-*.txt`，不改写日志）。
3. token 的新增部分仍是估算（误差 0.6%–5%），未接入官方离线分词器。
4. 异步任务不跨内核重启续期，只能接管观察；孤儿进程输出无人回收。
5. 非流式请求：单次调用最长等 300 秒，期间看不到进度。
6. 只在 Linux/arm64 上验证过；Windows 分支（`taskkill` 杀进程组）未测。
7. 桌面 GUI / 网页控制台未实现（控制面已全部落在文件上，接 UI 不需要改内核）。
8. 压缩只保证“更短”，不保证摘要信息不丢；摘要质量未评测。
9. TUI 只读事件流，不支持鼠标/搜索；长事件只显示摘要，完整内容看 `events.jsonl`。

## 文档

- [docs/quickstart.md](docs/quickstart.md) — 从零跑起第一个 Agent
- [docs/tui.md](docs/tui.md) — 终端界面：布局、颜色、按键、命令
- [docs/tools.md](docs/tools.md) — 工具协议与自定义工具
- [docs/operations.md](docs/operations.md) — 运行、监控、恢复、清理
- [docs/testing.md](docs/testing.md) — 测试与部署
- [ARCHITECTURE.md](ARCHITECTURE.md) — 实现规范
- [KernelDesign.md](KernelDesign.md) — 最初的设计稿
