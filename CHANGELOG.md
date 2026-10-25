# 更新日志

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
