# 快速开始

从零创建一个 Agent 并让它跑起来。全程大约 5 分钟。

## 0. 准备

- Python ≥ 3.10
- 能访问 `https://api.deepseek.com`
- 一个 DeepSeek API Key（`sk-` 开头）

> 安全提醒：fishKernel 给模型全部权限（可以执行任意命令）。请在一台一次性或隔离的机器/容器里操作。

## 1. 安装

```bash
git clone <repo-url> fishKernel
cd fishKernel
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

确认命令可用：

```bash
fishkernel --help
```

## 2. 生成存档

```bash
fishkernel new ./archives/demo --name 小鱼 --api-key sk-你的key
```

会得到：

```
archives/demo/
├── basic.instance
├── Memory/Agent.txt
├── Memory/LongTermMemory.txt
├── Memory/Digest.txt
├── Memory/History.txt
├── Tools/ToolsRegister.txt
└── Tools/shell/Usage.txt
```

密钥也可以不写进文件：生成时省略 `--api-key`，运行时用环境变量 `FISHKERNEL_API_KEY` 或 `DEEPSEEK_API_KEY`。

## 3. 写人格与目标

编辑 `archives/demo/Memory/Agent.txt`，这是每轮都会进入系统提示词的内容。建议写清：

- 你是谁、在什么环境里；
- 你的目标（长期目标 + 当前阶段）；
- 收尾标准（什么时候算完成，什么时候该 `sleep` 等待）；
- 不要做什么（避免它乱删东西）。

## 4. 先离线跑通

不花钱、不联网，验证存档与循环是好的：

```bash
fishkernel run ./archives/demo --mock --max-turns 3
```

看到 `turn/start`、`assistant/message`、`session/end` 就算通了。

## 5. 真正跑起来

```bash
fishkernel run ./archives/demo
```

默认无限循环。想先限轮次观察：

```bash
fishkernel run ./archives/demo --max-turns 5
```

## 6. 观察

最直观的方式是 TUI（实时事件流 + 输入框）：

```bash
fishkernel tui ./archives/demo
```

不想进界面就看文件：

```bash
fishkernel status ./archives/demo --tail 20 --jobs
```

或者直接看文件：

```bash
tail -f archives/demo/Runtime/kernel.log      # 内核日志：每轮压力、token、工具结果
tail -f archives/demo/Runtime/events.jsonl    # 全部事件（事实来源）
tail -f archives/demo/Memory/History.txt      # 人可读对话
```

## 7. 控制

```bash
fishkernel say ./archives/demo "先看看 Tools 里有什么"
fishkernel ctl ./archives/demo pause
fishkernel ctl ./archives/demo resume
fishkernel ctl ./archives/demo delay 3
fishkernel ctl ./archives/demo stop
```

`pause` 不消耗 API；`stop` 会让它打印收尾并退出。

## 8. 加一个自己的工具

假设要让它用 `git`：

```bash
mkdir -p archives/demo/Tools/git
cat > archives/demo/Tools/git/Usage.txt <<'EOF'
名称：git
用途：查看仓库状态、提交、切分支。
用法：spawn(command="git status", cwd="/path/to/repo")，再用 job_output(job_id) 读结果。
注意：提交前先用 spawn 跑测试。
EOF
echo "git" >> archives/demo/Tools/ToolsRegister.txt
```

下一轮它就能看到这段说明，并通过 `spawn` 调用。

## 常见问题

**401 / 鉴权失败**
Key 错或没设置。检查 `basic.instance` 或环境变量；密钥优先级：配置项 → `FISHKERNEL_API_KEY` → `DEEPSEEK_API_KEY`。

**一直不调用工具，在原地说话**
这是 v1 没有“刹车”的表现。把规则写进 `Agent.txt`（例如“没有进展时先 sleep 60 再重试”），或用 `ctl pause` 人工介入。

**上下文什么时候压缩？**
压力达到 `min(W×0.8, W−O−B)` 时自动压缩。默认窗口 1,048,576、输出上限 8192 → 阈值 838,860 tokens。日志里能看到每轮压力值。

**重启后会不会丢状态？**
不会。事件日志是事实来源；重启后会接管异步任务、修复未闭合的压缩事务。`Memory/` 与 `Runtime/` 一起备份即可。

**能同时跑两个实例吗？**
同一个存档不行（没有并发锁，会写坏事件日志）。不同存档可以，各用各的目录。
