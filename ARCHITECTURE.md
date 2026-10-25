# fishKernel 架构（v1）

本文是实现规范。产品意图以 [KernelDesign.md](KernelDesign.md) 为准；凡是那里没写清、或写了但实现不了的细节，以本文为准。

## 1. 范围

v1 交付：一个能长时间自主运行的内核 + 命令行控制面。

- `fishkernel new` 生成存档；`fishkernel run` 跑循环；`fishkernel ctl` / `say` / `status` 做控制与观察。
- 桌面 GUI、网页控制台后置。控制与观察全部落在存档文件上，所以将来接任何 UI 都不用改内核。
- 权限：按设计意图，模型拥有全部权限（`spawn` 可执行任意命令）。因此**只允许在一次性或隔离的机器/容器里运行**。

## 2. 相对原设计的五个关键改动

| 原设计 | v1 | 原因 |
|---|---|---|
| `History.txt` 是历史消息本体，靠追加文本维护 | `Runtime/events.jsonl` 是唯一事实来源；`History.txt` 只是给人看的投影 | 纯文本拼接无法还原 `tool_call` 与 `tool_result` 的配对，API 会报错；事件日志可回放、可审计、可崩溃恢复 |
| 每轮把文件内容拼成一大段文本发给模型 | 事件投影成 `messages` 数组（system / user / assistant / tool） | 模型必须能分清"谁说的"；tool 结果要按 `tool_call_id` 配对 |
| 摘要由模型返回、内核照抄；长期记忆"模型自己维护" | `Digest.txt` 只由内核在压缩事务里写；`LongTermMemory.txt` 只由内核写，模型通过 `memory` 工具提出请求 | 两个写者 = 内容互相覆盖。见 KernelDesign 3.2 / 3.3 |
| 85% 触发、保留 16%；按字符切 | 阈值 `min(W×0.8, W−O−B)`、保留 `0.16×(W−O)`；按消息边界切，不拆调用对，不碰系统提示词 | 数字可推导；避免切出孤儿 tool 结果 |
| `multiprocessing` 共享变量 + 软件中断 | 存档内的控制文件 + 事件日志（轮询式） | 跨进程/跨机器可用，重启不丢，UI 无需与内核同父进程 |

## 3. 存档布局

```
<archive>/
├── basic.instance              # key = value 配置
├── Memory/
│   ├── Agent.txt               # 人格/设定（人写）
│   ├── LongTermMemory.txt      # 长期记忆（内核写，模型经 memory 工具发起）
│   ├── Digest.txt              # 最近一次压缩的概要（内核写）
│   ├── History.txt             # 投影：人可读的对话记录
│   └── Archive-<时间>.txt      # 每次压缩一份被压缩区的可读文本
├── Tools/
│   ├── ToolsRegister.txt       # 每行一个已启用工具名，# 注释
│   └── <工具名>/Usage.txt      # 该工具给模型看的说明
├── Runtime/
    ├── events.jsonl            # 唯一事实来源，只追加
    ├── kernel.json             # 内核自述：pid / 启动时间 / 轮次 / 状态
    ├── kernel.lock             # 单实例锁（flock / msvcrt）
    ├── kernel.log              # 运行日志
    ├── control/
    │   ├── state.json          # {"run": "run|pause|stop", "delay": 1.0}
    │   ├── inbox.jsonl         # 用户消息队列（控制器追加）
    │   ├── wake                # 唤醒标记（读后删除）
    │   └── restart             # 交班请求（读后删除）
    ├── jobs/<job-id>/          # 异步进程：meta.json, stdout.log, stderr.log
    └── spill/<name>.txt        # 超长输出的完整副本
```

`basic.instance` 示例：

```ini
name = 小鱼
model = deepseek-flash
api_base = https://api.deepseek.com
api_key = sk-...
max_tokens = 8192          # 单轮输出上限，同时参与压缩阈值计算
turn_delay = 1.0           # 每轮基础延时（秒）
```

密钥优先级：`api_key` 配置项 → 环境变量 `FISHKERNEL_API_KEY` → `DEEPSEEK_API_KEY`。

## 4. 事件协议

`Runtime/events.jsonl`，一行一个 JSON 对象，字段 `seq`（自增）、`ts`（ISO8601）、`type`、`data`。

| type | data | 说明 |
|---|---|---|
| `session/start` | `{model, context_window}` | 内核启动 |
| `session/handover` | `{target, pid, reason}` | 交班：新进程 pid 已启动，旧进程即将释放锁退出 |
| `session/end` | `{reason}` | 退出（stop / max-turns / restart / 信号） |
| `turn/start` | `{turn}` | 一轮开始 |
| `turn/end` | `{turn, reason}` | 一轮结束 |
| `user/message` | `{text, source}` | 用户/控制通道输入 |
| `assistant/message` | `{text, reasoning?, tool_calls[], usage, prompt_seq, prompt_tokens_actual, pressure_tokens, finish_reason}` | 模型回复；`usage`/`prompt_tokens_actual` 是 API 的精确用量，`prompt_seq` 是这次请求覆盖到的最后一个事件序号（下次压缩判定的锚点），`pressure_tokens` 是发出前算出的压力 |
| `tool/result` | `{call_id, name, ok, output, spill?, job_id?, duration_ms}` | 工具执行结果 |
| `job/start` | `{job_id, command, cwd}` | 异步进程已启动 |
| `job/settle` | `{job_id, status, exit_code, duration_ms}` | 异步进程终止 |
| `delay` | `{seconds, reason}` | 一轮结束后的实际等待时长（reason: elapsed/wake/pause/stop） |
| `system/message` | `{text, kind}` | 运行时通知（如任务完成），投影为用户可见消息 |
| `compaction/start` | `{trigger, threshold, pressure}` | 压缩事务开始（持久锁） |
| `compaction/summary` | `{summary, from_seq, to_seq, archived, freed_tokens}` | 压缩成果 |
| `compaction/end` | `{ok}` | 事务结束 |
| `compaction/abort` | `{reason}` | 放弃或恢复 |
| `error` | `{stage, message, retryable}` | 可恢复错误，循环继续 |

规则：
- 只追加，不原地改。崩溃后从末尾截断半行即可继续。
- `seq` 连续；读取时容忍最后一行不完整。
- 事件是审计与恢复的唯一依据，日志文本没有额外权力。

## 5. 上下文投影

`build_messages()` 是纯函数：事件序列 + 存档文本 → `[system] + 对话消息`。

1. 系统提示词 = `Agent.txt` + 已登记工具的 `Usage.txt`（带编号标题）+ 非空 `LongTermMemory.txt` + 非空 `Digest.txt`。
2. 从最近的 `compaction/summary` 之后开始投影；该概要投影为一条 `<compacted-summary>…</compacted-summary>` 的 user 消息。
3. `assistant/message` 带 `tool_calls` 时，必须紧跟每个调用对应的 `tool/result`。若事件流里缺失（崩溃残留），投影时补一条 `TOOL_OUTCOME_UNKNOWN` 的 tool 结果，保证配对完整。
4. `system/message` 投影为带 `[runtime]` 前缀的 user 消息；只有**双方都带该前缀**的连续消息才合并，真实用户发言不会被吞。
5. `delay` 投影为 `[runtime] 等待 N 秒`，让模型知道时间过去了。

## 6. 主循环

```
acquire lock          # 失败则报 LockError；交班时新进程在这里等待
recover()             # 接管/清理遗留异步进程；修复未闭合的压缩事务；清理 spill
session/start
if 历史上没有 user/message: 补欢迎语 user/message(source="init")
loop:
  ctl = 读 state.json
  if stop: break
  if restart 请求: _handover()  # 起新进程 → session/handover → break（锁在 finally 释放）
  if pause: 睡 0.5s 后重读（期间仍响应 stop/restart）
  收 inbox 新消息 → user/message
  jobs.tick()        # 结超时/完成的进程 → job/settle + system/message
  if 压力 > 阈值: compress()          # 步骤见 §9
  messages = build_messages()
  resp = llm.complete(messages, tools) # 重试、计费记录
  assistant/message
  对每个 tool_call（每轮上限 16）:
      result = 执行 → tool/result
  若无 tool_call: system/message "唤醒"（不伪造用户发言）
  delay = 控制通道覆盖值 或 配置默认值；可中断睡眠（stop/pause/wake）
  if slept > 0: delay
  turn/end
release lock
session/end
```

异常处理：任何模块抛异常都记 `error` 事件并继续下一轮；连续失败按 2→4→8…→60s 退避。这属于"活下来"，不作为节奏刹车。

## 7. 工具协议

用 OpenAI `tools`（function calling）。返回值统一是 JSON 字符串，且**永远是合法 JSON**：

```json
{"ok": true, "result": ...}
{"ok": false, "error": {"code": "not_found", "message": "..."}}
```

超过 32 KiB 时不会切字符串，而是把超长字符串字段替换成溢出指针 `{locator, bytes, preview, hint}`；若仍超限，整份结果落 spill 并返回 `{ok, truncated, spill}`。

| 工具 | 参数 | 行为 |
|---|---|---|
| `read_file` | `path, offset=0, limit=200` | 按行读；单次上限 256 KiB，超出时剩余内容写入 spill 并在 payload 里返回 `spill` 对象 |
| `list_dir` | `path=".", limit=200` | 列目录；超出提示截断 |
| `spawn` | `command, cwd=null, label=null, timeout_seconds=0` | 起异步进程，立即返回 `job_id`；stdout/stderr 落文件 |
| `job_list` | – | 全部任务的状态投影 |
| `job_output` | `job_id, offset=0, max_bytes=32768` | 按字节偏移读任务输出；超长自动溢出 |
| `job_kill` | `job_id` | 杀进程组（POSIX `killpg`） |
| `wait_job` | `job_id, timeout_seconds=30` | 有界等待；可被 stop 打断 |
| `sleep` | `seconds` | 可中断延时（≤3600） |
| `memory` | `mode=append\|replace, content` | 写 `LongTermMemory.txt`（上限 64 KiB） |

`spawn` 是权限面：不设命令白名单——这是设计决定，不是遗漏。`selfguard` 只额外拒绝"终止内核自身/父进程/同进程组"的命令与 `job_kill` 目标。

## 8. 异步任务与长输出

- 任务 id：`proc-<12 位十六进制>`。目录 `Runtime/jobs/<id>/`，`meta.json` 原子更新。
- 启动时 `start_new_session=True`（POSIX），便于整组终止。
- 输出只写文件，不经过管道：进程不会被"管道写满"卡死。
- 内核重启：读 `meta.json`，`pid` 已死 → 标 `failed`（`orphan`）；仍活着 → 标 `running` 并接管观察，退出码记为 `unknown`。
- 每轮 `tick()` 结算超时与退出。
- 超长输出：写入 `Runtime/spill/<name>.txt`，模型只看到首尾 + 路径 + 字节数，用 `read_file(path, offset, limit)` 自己取。

## 9. 压缩

参数（`W=1048576, O=8192, B=65536`）：阈值 `838860`，保留 `166461`。

1. 压力 = API 实测锚点 + 锚点之后的新增量估算：
   - 每次成功响应把 `usage.prompt_tokens`（精确值）连同当时的 `prompt_seq` 记进 `assistant/message`；
   - 下一次请求的压力 = 该精确值 + 新增事件投影后的估算 + 系统提示词/工具 schema 的增量；
   - 新增量用「实测 / 估算」的比例做 EMA 校准（0.7 旧 + 0.3 新，钳在 0.3~3.0）；
   - 压缩会改写历史，锚点随之失效，退回全量估算。
   DeepSeek 没有发送前计数接口（`/tokenize`、`/count_tokens` 均 404），因此“精确到 token”只能这样取得：已发出的部分用 API 精确值，未发出的部分用估算。
2. 选区：从最早的可压缩单元（一个 user 消息，或一条 assistant 消息 + 其全部 tool 结果）开始，累计直到"剩余尾部"刚好不超过保留预算；选区不得包含系统提示词。
3. 摘要调用：单独一次请求，不带工具；输入是选区消息 + 固定摘要指令；输出上限 `min(8192, 保留预算/8)`。
4. 校验：摘要 token < 选区 token，否则重试一次；仍失败则 `compaction/abort`，本轮不压缩。
5. 提交：`compaction/start` → 写 `Archive-<ts>.txt` → 原子写 `Digest.txt` → `compaction/summary` → `compaction/end`。
6. 崩溃恢复：启动时若发现未配对的 `compaction/start`，追加 `compaction/abort{reason:"stale"}`，历史保持原样。
7. 溢出兜底：API 报上下文超限时，用"最小保留预算"再压一次，然后重试一次请求。

## 10. 控制通道

| 文件 | 写者 | 读者 | 协议 |
|---|---|---|---|
| `control/state.json` | `ctl` | 内核（每轮 + 睡眠中） | `{"run":"run|pause|stop","delay":1.0}`，原子写 |
| `control/inbox.jsonl` | `say` | 内核（按偏移增量读） | 每行 `{"ts","text"}`；偏移存 `control/inbox.offset` |
| `control/wake` | `ctl wake` | 内核 | 存在即消费，睡眠与 `wait_job` 每 0.2s 检查 |
| `control/restart` | `ctl restart` | 内核 | 存在即交班：启动新进程、释放锁、旧进程退出 |

内核在延时期间每 0.2s 检查一次 `stop` / `pause` / `wake`，保证最多 0.2s 响应。`pause` 不消耗 API。

## 11. 崩溃点与恢复

| 崩溃位置 | 现象 | 恢复动作 |
|---|---|---|
| 写完 assistant 消息、未写 tool 结果 | 孤儿 tool_call | 投影补 `TOOL_OUTCOME_UNKNOWN`；日志可查 |
| 工具执行到一半 | 无结果事件 | 同上；异步进程由 `jobs.recover()` 接管 |
| 压缩中 | 未闭合 `compaction/start` | 启动时 `compaction/abort{stale}` |
| 事件写一半 | 末行非法 JSON | 读取时跳过末行；追加前截断到最后一个换行；`_last_seq` 反向分块扫描，末行超大也能读对 |
| 控制文件写一半 | JSON 解析失败 | 视为缺省，继续运行 |
| 内核崩溃 | 锁未显式释放 | `flock`/`msvcrt.locking` 由操作系统释放，不会假占用 |
| 交班中途崩溃 | 新进程起不来 | 旧进程记录 error 并继续运行；锁仍归旧进程 |

## 12. 模型

| 项 | 值 |
|---|---|
| 模型 id | `deepseek-flash` |
| 名称 | DeepSeek-V4.1-Flash（1017） |
| 上下文窗口 | 1,048,576 |
| 最大输出 | 393,216（内核默认 8,192，可配） |
| 推理档 | low / high / max（默认 high，由服务端） |
| 接口 | OpenAI SDK，`/chat/completions`，`tools` 可用 |

`reasoning_content` 只记录、不回传（下一轮请求不包含它）。

## 13. 测试与部署

- **真实模型相关测试只在隔离测试机上跑**（参考环境：Debian 12 / Python 3.11 / aarch64）。
- 单元测试不联网、不执行模型，可以在任意环境跑。
- `scripts/deploy.py`：打包 → scp → 解包 → 建 venv → `pip install -e .[dev]` → `pytest`；测试机地址与凭据由环境变量提供。
- mock provider 覆盖：循环、工具、压缩、控制、崩溃恢复；真实 API 用 `--max-turns` 限轮次联调。
- 当前 111 项测试；交班（`session/handover` → `session/end` → `session/start`）在测试机上手工验证。

## 14. 已知边界

- token 数：已发送部分以 API `usage` 精确值为锚点，未发送的增量按启发式估算（ASCII 0.3 / 中文 0.6 / 其他 0.5，官方文档比例），并用实测比例校准。官方离线分词器（`deepseek_v4_tokenizer.zip`）可进一步做到全量精确，但需要 `transformers` 依赖，v1 未引入。
- 异步任务不跨内核重启续期；只接管已启动的进程。
- 无审批、无预算上限、无退避刹车——按 v1 决定。
- `selfguard` 只拦"杀内核自己"这一类命令，不限制删文件、改系统配置等其它破坏性操作。
- 事件日志不自动轮转；压缩会写入 Archive，但 `events.jsonl` 本身持续增长。
