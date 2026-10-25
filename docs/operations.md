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
fishkernel ctl <目录> pause|resume|stop|delay [秒数]
fishkernel say <目录> "文本"
fishkernel status <目录> [--tail N] [--jobs]
```

实现上都是改存档里的文件：

| 文件 | 作用 |
|---|---|
| `Runtime/control/state.json` | `{"run": "run|pause|stop", "delay": 秒}`，原子写 |
| `Runtime/control/inbox.jsonl` | 用户消息队列，内核按偏移增量消费 |

因此控制器可以是任意进程、任意机器（只要共享目录或通过 ssh）。

## 观察

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
4. 从最后一个成功的 `usage` 恢复 token 锚点；如果之后发生过压缩，退回估算。

恢复后模型会看到一条 `[runtime]` 通知，知道上次中断和任务状态。

## 清理

```bash
du -sh archives/demo/Runtime
ls archives/demo/Runtime/jobs | wc -l
ls archives/demo/Runtime/spill | wc -l
ls archives/demo/Memory/Archive-*.txt | wc -l
```

- `jobs/` 只保留最近 100 个目录，自动清理更旧的。
- `spill/` 保留最近 200 个文件且总量 50 MB。
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

同一个存档**只能**有一个内核进程。当前没有加锁，同时跑两个会互相覆盖 `events.jsonl`。

需要并行就跑多个存档（各自目录、各自 Key/模型）。

## 资源与成本

- 默认每轮间隔 `turn_delay`（1 秒），并会调用一次模型。没有预算上限，长跑请自行控制 `--max-turns` 或及时 `stop`。
- `fishkernel status` 显示累计 prompt / completion token，可据此估算花费。
- 压缩会额外调用一次模型（分块时多次），日志里记在 `compaction/summary.usage`。
