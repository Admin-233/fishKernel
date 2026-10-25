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
