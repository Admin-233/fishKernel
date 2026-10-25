# 工具

fishKernel 用 OpenAI 的 function calling（`tools` 参数）。模型返回 `tool_calls`，内核执行后把结果作为 `role=tool` 消息回填。

工具返回值约定：

```json
{"ok": true, "...": "..."}
{"ok": false, "error": {"code": "not_found", "message": "文件不存在：/tmp/x"}}
```

失败也必须是合法 JSON——模型需要读到错误码和原因，而不是让内核崩掉。

## 内置工具

### read_file

```json
{"path": "notes.txt", "offset": 0, "limit": 200}
```

- 相对路径相对于存档根目录，也接受绝对路径。
- 单次最多 2000 行、256 KiB；超出时返回 `has_more: true` 和 `next_offset`，让模型自己分段。
- 二进制文件（前 8 KiB 含 `\0`）直接报 `binary_file`。
- 单行过长会截断并提示改用 `spawn` + `head/awk`。

### list_dir

```json
{"path": ".", "limit": 200}
```

返回名称、类型、大小；超过 `limit` 时 `truncated: true`。

### spawn / job_*

```json
{"command": "make test", "cwd": "/path/to/repo", "label": "run tests", "timeout_seconds": 600}
```

- 立即返回 `job_id`（`proc-<12位hex>`）。
- stdout / stderr 直接写 `Runtime/jobs/<job_id>/{stdout,stderr}.log`，**不经过管道**，所以子进程不会因为管道写满而卡死。
- 进程以独立会话启动，`job_kill` 会杀掉整个进程组。

配套工具：

| 工具 | 用途 |
|---|---|
| `job_list` | 所有任务的状态、退出码、标签 |
| `job_output` | 按字节偏移读输出，`next_offset` 继续；`stream` 选 stdout/stderr |
| `wait_job` | 有界等待（默认 30 秒），可被 `stop` 打断 |
| `job_kill` | 终止任务及其子进程 |

任务状态：`running` → `completed` / `failed` / `killed` / `timeout`。内核每轮会结算已完成的任务，并以 `[runtime]` 消息通知模型。

### sleep

```json
{"seconds": 30}
```

可中断：等待期间收到 `pause` / `stop` 会提前返回。上限 3600 秒。

### memory

```json
{"mode": "append", "content": "用户希望最终产物放在 /srv/out"}
```

写 `Memory/LongTermMemory.txt`，上限 64 KiB。`append` 追加一段，`replace` 整份替换。这是唯一允许模型改长期记忆的入口，写者始终是内核。

## 长输出与溢出

工具结果超过 32 KiB 字符时，完整内容写到 `Runtime/spill/<时间>-<名字>.txt`，模型只看到首尾预览、字节数和路径，再用 `read_file(path, offset, limit)` 取回。

任务输出不主动溢出（它本来就按块读取，`job_output` 每次最多 32 KiB）。

## 注册命令行工具

`Tools/` 下每个目录代表一个可用的命令行工具，`Usage.txt` 是给模型看的说明。`ToolsRegister.txt` 每行一个目录名（`#` 注释）。

```
Tools/
├── ToolsRegister.txt
├── git/
│   └── Usage.txt
└── ffmpeg/
    └── Usage.txt
```

`Usage.txt` 建议包含：

```
名称：git
用途：查看状态、提交、切分支。
用法：
  spawn(command="git status", cwd="/srv/repo")
  spawn(command="git add -A && git commit -m 'msg'", cwd="/srv/repo")
取结果：job_output(job_id="proc-...")
限制：不要执行 force push；提交前先跑测试。
```

注册表读取是每轮实时的：加完下一轮就生效，不需要重启。

## 错误码

| code | 含义 |
|---|---|
| `not_found` | 文件/目录/任务不存在 |
| `is_dir` / `not_dir` | 路径类型不对 |
| `binary_file` | 试图按文本读二进制 |
| `invalid_arguments` | 参数不是合法 JSON 或类型错误 |
| `unknown_tool` | 没有这个工具 |
| `memory_too_large` | 长期记忆超过 64 KiB |
| `invalid_job` / `not_found` | job id 非法或不存在 |
| `TOOL_OUTCOME_UNKNOWN` | 历史里缺少该调用的结果（通常是崩溃残留） |
| `TOOL_CALL_LIMIT` | 单轮工具调用超过 16 个，多出的被拒绝 |

## 写工具/提示词的注意事项

- 让模型看到“怎么取结果”，否则它会 `spawn` 完就忘。
- 命令要幂等、可重试；崩溃后模型可能重复执行。
- 给长任务设 `timeout_seconds`，否则只能人工 `job_kill`。
- 输出尽量结构化（JSON、固定前缀），方便模型解析。
- 不要假设模型会读完整份大文件——提醒它用 `offset/limit`。
