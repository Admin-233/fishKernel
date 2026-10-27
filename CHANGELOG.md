## v0.4.1

- 修复 `jobs.tick()` 的超时判定在非 +0800 机器上失效：

  ```python
  started = time.mktime(time.strptime(job.started_at, "%Y-%m-%dT%H:%M:%S%z"))
  ```

  `%z` 只让 `strptime` 认出偏移，`mktime` 却一律按**进程所在时区**解释那串 wall clock，
  tzinfo 被静默丢掉。本机时区恰好是 +0800，所以测试全绿；换到 UTC 的服务器上，
  每个任务的起始时刻都被算成 8 小时后 → 超时**永远不触发**（负偏移的机器上则会立刻误杀）。
  改为 `datetime.strptime(..., %z).timestamp()`，保留 tzinfo；解析失败返回 `None`，
  此时宁可让任务继续跑也不误杀。

- 新增 4 项回归测试，全部在真实切换 `TZ` 下运行（`Asia/Shanghai` / `UTC` / `America/New_York`）：
  同一时刻的不同写法必须解析成同一时间戳；刚启动的任务不得被判超时；真正过期的任务必须照杀。
  已用「临时还原 bug」验证过这些测试确实会失败。

- 174 -> 178 passed。

## v0.4.0

- 新增 `fishkernel web`：零依赖（stdlib `http.server`）网页控制台。事件流增量拉取、任务列表、暂停/继续/唤醒/交班重启/停止、发消息；与 TUI 同一套边界——只读事件日志、只写控制文件，不碰内核进程和锁。默认只监听 `127.0.0.1`，可用 `--host` / `--token` 开放。
- 网页接口：`GET /api/snapshot`、`GET /api/job_output`、`POST /api/say`、`POST /api/ctl`，可直接用 curl 驱动。
- 性能：20 万条事件（42 MB）下，冷启动快照 554 ms → 稳态轮询 9.8 ms。
  - `UsageCache` 增量折叠 usage（`tokens.fold_usage_into`），不再每次重算全量；
  - 增量取事件改用 `bisect` 二分定位，不再全表扫描。
- 界面细节：事件流里超过 240 字的内容（工具结果、长命令）折叠成一行摘要，点开看全文；任务列表按 `started_at` 排序（`JobManager.list()` 是按 `proc-<hex>` 目录名排的，与时间无关）。
- 新增 `tests/test_web.py`（30 项：投影、快照、真实 HTTP 往返、token 鉴权、usage 缓存一致性、任务排序）、`scripts/smoke_web.sh`、`docs/web.md`。
- 144 -> 174 passed。

# 更新日志

## v0.3.0

- 修复工具返回值超长时被切成非法 JSON：改为把超长字符串字段换成溢出指针，任何情况下都返回合法 JSON。
- 修复超长末行导致重启后 seq 回退：`_last_seq` 改为反向分块扫描（最多 64 MB）。
- `read_file` 超过 256 KiB 时把剩余内容写入 `Runtime/spill/`，不再只给一句提示。
- 新增单实例锁 `Runtime/kernel.lock`（POSIX `flock` / Windows `msvcrt.locking`），第二个内核会报 `LockError`。
- 新增交班重启：`ctl restart` / TUI `:restart`，旧进程启动新进程、释放锁后正常退出。
- 新增唤醒：`ctl wake` / TUI `:wake`，可打断 `sleep` 与 `wait_job`。
- 新增 `selfguard`：拒绝终止内核自身、父进程或同进程组的命令。
- 修复真实用户消息被并进运行时通知；修复 `reasoning=None` 导致整轮丢失。
- 新增 `delay` 事件、启动时清理 spill、`effort` 配置真正下发给 API。
- 首次启动补欢迎语（`source=init`，只写一次）。
- TUI：非终端环境改为持续跟随输出；放宽单类消息的行数上限；新增 `:wake` / `:restart`。

## v0.2.0

- 新增 `fishkernel tui`：实时事件流（上半屏）+ 输入框（下半屏），分类着色，长工具调用压缩显示。
- 支持滚动回看、输入历史、`:pause/:resume/:stop/:delay` 命令；只读事件日志、只写控制文件。
- 非交互模式：`--snapshot` 渲染一帧、`--plain` 按行输出；非终端环境自动退化。
- 零新增依赖：POSIX 用 termios/select，Windows 用 msvcrt + VT。
- 新增 `scripts/smoke_tui.sh`（快照 + 伪终端冒烟）。

## v0.1.1

- token 计量改为「API 实测锚点 + 增量估算 + 比例校准」：以响应的 `usage.prompt_tokens` 为准，只估算新增内容。
- 修正中文 token 比例（官方：1 字符 ≈ 0.6 token）。
- 事件中记录 `prompt_seq`、`prompt_tokens_actual`、`pressure_tokens`；`status` 显示累计用量。
- 压缩支持分块摘要与合并，避免摘要请求自身超窗口。
- 修复压缩后投影边界：保留尾部按 `to_seq` 取，不会再丢失。
- 暂停改为 0.5 秒轮询，不再空转烧 CPU。

## v0.1.0

- 首个可用版本：事件日志、消息投影、工具调用、异步任务、上下文压缩、控制通道、崩溃恢复。
- CLI：`new / run / ctl / say / status`。
- 测试：67 项，含 mock 端到端。
