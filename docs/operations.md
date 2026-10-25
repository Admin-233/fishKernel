# 运行与运维

## 启动

```bash
fishkernel run <存档目录> [--max-turns N] [--once] [--mock] [--quiet]
```

- 默认无限循环。
- `--max-turns N` 跑 N 轮后正常退出（联调、评估用）。
- `--mock` 用离线脚本客户端，不调用 API。
- `--quiet` 不在终端打印，仍写 `Runtime/kernel.log`。

建议用 `tmux` / `screen` / systemd 托管，避免终端断开把进程带走。

## 控制

```bash
fishkernel ctl <目录> pause|resume|stop|delay|wake|restart [秒数]
fishkernel say <目录> "文本"
fishkernel status <目录> [--tail N] [--jobs]
```

- `wake`：写一个 `Runtime/control/wake` 标记，正在 `sleep` 或 `wait_job` 的内核会在 0.2 秒内醒来。
- `restart`：交班重启。旧进程启动新进程 → 写 `session/handover` → 写完 `session/end` 后在 `finally` 里释放实例锁 → 自己退出；新进程在锁上等待后接管。全程不给自己发信号。

实现上都是改存档里的文件：

| 文件 | 作用 |
|---|---|
| `Runtime/control/state.json` | `{"run": "run|pause|stop", "delay": 秒}`，原子写 |
| `Runtime/control/inbox.jsonl` | 用户消息队列，内核按偏移增量消费 |
| `Runtime/control/wake` | 唤醒标记，内核读取后删除（只生效一次） |
| `Runtime/control/restart` | 交班请求，内核读取后删除 |

因此控制器可以是任意进程、任意机器（只要共享目录或通过 ssh）。

## 观察

最直观的方式是 TUI：

```bash
fishkernel tui ./archives/demo
```

上半屏实时事件流、下半屏输入框，见 [tui.md](tui.md)。不想进界面时用文件：

| 位置 | 内容 |
|---|---|
| `Runtime/kernel.log` | 每轮压力、token 实测/估算、工具结果、错误栈 |
| `Runtime/events.jsonl` | 全部事件（事实来源），可 grep / jq |
| `Runtime/kernel.json` | pid、轮次、状态 |
| `Memory/History.txt` | 人可读对话 |
| `Runtime/jobs/*/` | 异步任务元数据与输出 |
| `Runtime/spill/` | 超长输出的完整副本 |

常用：

```bash
fishkernel status ./archives/demo --jobs
grep -E 'token：|上下文压力|ERROR' archives/demo/Runtime/kernel.log | tail -20
jq -c 'select(.type=="tool/result")|{name,ok,duration_ms}' archives/demo/Runtime/events.jsonl | tail
```

## 崩溃与重启

直接重新 `fishkernel run <目录>` 即可，内核会：

1. 截断事件日志里未写完的最后一行；
2. 结算 pid 已不存在的异步任务（标记 `failed`），接管仍活着的（标记 `adopted`）；
3. 给未闭合的压缩事务补 `compaction/abort{stale}`；
4. 从最后一个成功的 `usage` 恢复 token 锚点；如果之后发生过压缩，退回估算；
5. 清理过旧的 spill 文件（保留最近 200 个、总量 50 MB）。

首次启动会补一条欢迎语 `user/message(source="init")`；重启或交班不会重复写。

`Runtime/kernel.lock` 是单实例锁，进程退出时由操作系统自动释放，不会留下"假占用"。

### 交班重启

```bash
fishkernel ctl archives/demo restart
```

事件序列（判断是否成功）：

```
session/handover → session/end(reason=restart) → session/start(新 pid) → turn/start
```

新进程通过 `python -m fishkernel run <目录>` 启动，stdout/stderr 丢弃，日志仍写 `Runtime/kernel.log`。交班失败（比如新进程起不来）时旧进程会记录错误并继续运行。

## 清理

```bash
du -sh archives/demo/Runtime
ls archives/demo/Runtime/jobs | wc -l
ls archives/demo/Runtime/spill | wc -l
ls archives/demo/Memory/Archive-*.txt | wc -l
```

- `jobs/` 只保留最近 100 个目录，自动清理更旧的。
- `spill/` 保留最近 200 个文件且总量 50 MB，内核启动时自动清理。
- `Archive-*.txt`、`events.jsonl`、`Memory/` **不会自动清理**；长跑需要手动归档或自己加轮转。

安全清理顺序：先 `stop` 并确认进程退出，再删 `Runtime/` 下的旧文件。不要删 `events.jsonl` 之外还在被引用的 Archive。

## 备份

存档即全部状态。停掉内核后整目录拷贝即可：

```bash
fishkernel ctl archives/demo stop
tar czf demo-backup-$(date +%F).tgz archives/demo
```

`basic.instance` 里有 API Key，备份时注意存放位置。

## 升级

```bash
git pull
. .venv/bin/activate
pip install -e '.[dev]'
```

事件格式向前兼容：新增字段不影响旧事件投影。跨大版本升级前先备份存档。

## 单实例约束

同一个存档**只能**有一个内核进程。`Runtime/kernel.lock` 用 `flock`（Windows 用 `msvcrt.locking`）强制这一点：第二个进程会等待最多 5 秒，然后报 `LockError` 退出。

需要并行就跑多个存档（各自目录、各自 Key/模型）。要换进程用 `ctl restart` 交班，不要直接 kill 再起。

## 资源与成本

- 默认每轮间隔 `turn_delay`（1 秒），并会调用一次模型。没有预算上限，长跑请自行控制 `--max-turns` 或及时 `stop`。
- `fishkernel status` 显示累计 prompt / completion token，可据此估算花费。
- 压缩会额外调用一次模型（分块时多次），日志里记在 `compaction/summary.usage`。
