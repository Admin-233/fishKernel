# TUI（终端界面）

`fishkernel tui` 提供一个实时刷新的终端界面：上半屏是 Agent 正在做什么，下半屏是输入框。

它只读存档、只写控制文件，**不会干扰正在运行的内核**，可以同时开多个观察窗口。

## 启动

```bash
# 终端 1：跑内核
fishkernel run ./archives/demo

# 终端 2：看它 + 跟它说话
fishkernel tui ./archives/demo
```

内核和 TUI 必须指向同一个存档目录。通过 SSH 使用时记得加 `-t` 分配终端：

```bash
ssh -t user@host 'cd ~/fishKernel && .venv/bin/fishkernel tui ~/archives/demo'
```

## 界面

```
 fishKernel · demo · 运行中 · 第 12 轮 · deepseek-flash · 压力 15.4k/838.9k · 用量 152k · 任务 1
 20:15:01 ── 第 12 轮 ──
 20:15:01 你     看看 Tools 里有什么
 20:15:02 助手   我先列一下目录。
 20:15:02   → list_dir(path=.)
 20:15:02 ✓ list_dir · 12ms · 4 项
 20:15:03 助手   目录里有 git 和 ffmpeg 两个工具说明。
 20:15:03 ── 第 13 轮 ──
 20:15:04 系统   任务 proc-a1b2c3d4（build）已结束：completed，退出码 0。
──────────────────────────────────────────────────────────────────────────────
> 先读一下 ffmpeg 的用法_
Enter 发送 · :pause :resume :stop :delay N · PgUp/PgDn 滚动 · Ctrl-C 退出
```

- **状态条**：内核名、运行状态、轮次、模型、上下文压力 / 压缩阈值、累计 prompt token、运行中的任务数。
- **事件流**：用户消息、模型回复、工具调用与结果、任务结算、压缩、错误。
- **输入区**：一行输入 + 一行快捷键提示。

## 颜色

| 颜色 | 内容 |
|---|---|
| 亮青 | 你发的消息 |
| 绿色 | 助手回复 |
| 黄色 | 工具调用请求（`→ name(args)`） |
| 青色 | 工具成功（`✓`） |
| 亮红 | 工具失败（`✗`）、错误 |
| 蓝色 | 异步任务结算 |
| 灰色 | 系统通知、轮次分隔、被截断提示 |
| 品红 | 上下文压缩 |
| 蓝底白字 | 顶部状态条 |

设置环境变量 `NO_COLOR=1` 或加 `--no-color` 可关闭颜色。

## 按键

| 按键 | 作用 |
|---|---|
| `Enter` | 发送输入框内容 |
| `Backspace` / `Delete` | 删除字符 |
| `←` / `→` | 移动光标 |
| `Home` / `End` | 输入框有内容时：行首 / 行尾；输入框为空时：事件流顶部 / 底部 |
| `↑` / `↓` | 历史输入 |
| `PgUp` / `PgDn` | 向上 / 向下滚动事件流 |
| `Ctrl-U` | 清空输入 |
| `Ctrl-W` | 删除一个词 |
| `Ctrl-C` | 退出 TUI（**不会**停止内核） |
| `Esc` | 清空输入 |

## 命令

在输入框里以 `:` 开头：

| 命令 | 作用 |
|---|---|
| `:pause` | 暂停内核（不再调用 API，但进程还在） |
| `:resume` | 恢复 |
| `:stop` | 让内核收尾退出 |
| `:delay N` | 把每轮延时改成 N 秒 |
| `:help` | 在事件流里显示帮助 |
| `:quit` | 退出 TUI |

注意：`:stop` 是停止 Agent，`Ctrl-C` 只是退出这个观察窗口。

## 工具调用的压缩规则

- 工具调用只显示一行：`→ 工具名(key=value, ...)`，参数值超过 40 字符会截断。
- 工具结果按类型压成一句话：
  - `read_file` → 返回多少行、是否还有更多
  - `list_dir` → 多少项
  - `spawn` → 启动的 job id
  - `job_output` → 读了多少字符、是否 EOF
  - `wait_job` → 任务状态与退出码
  - 失败 → 错误码 + 原因
- 模型回复最多显示 14 行、系统通知最多 4 行、错误最多 6 行，超出显示 `…（还有 N 行）`。
- "没有工具调用，内核唤醒你"这类空转提醒不显示（事件日志里仍有）。
- 完整内容永远可以从 `Runtime/events.jsonl` 查。

## 非交互模式

```bash
# 渲染一帧就退出，适合截图、排障、CI
fishkernel tui ./archives/demo --snapshot --width 100 --height 24 --no-color

# 不进入备用屏，按行输出；stdin 输入消息
fishkernel tui ./archives/demo --plain
```

其它参数：

| 参数 | 说明 |
|---|---|
| `--width` / `--height` | 固定尺寸（默认取终端大小） |
| `--refresh 0.2` | 刷新间隔（秒） |
| `--no-color` | 关闭颜色 |

当 stdout 不是终端（比如重定向到文件）时，会自动退化为 `--plain`。

## 工作原理

- 增量跟随 `Runtime/events.jsonl`（初始最多读末尾 256 KB）。
- 每 0.5 秒刷新状态条，每 1 秒统计一次任务数。
- 输入通过 `Control.push_message()` 写入 `Runtime/control/inbox.jsonl`；
  命令写 `Runtime/control/state.json`。
- 写者仍然只有内核：TUI 不碰 `events.jsonl`，也不碰 `Memory/`。

## 排障

**界面错乱 / 出现乱码**
确认 `TERM` 正常（`xterm-256color`），先用 `--snapshot --no-color` 看内容是否正常；再试 `--plain` 排除终端兼容问题。

**颜色不显示**
`NO_COLOR` 环境变量、`--no-color`、或终端/`TERM=dumb` 都会关闭颜色。

**Windows**
原生控制台已启用 VT 转义；若在旧终端里异常，改用 Windows Terminal 或 `--plain`。

**中文宽度不对**
按 Unicode East Asian Width 计算（全角按 2 格）。个别终端字体不一致时可能出现错位，可改小 `--width` 或使用 `--plain`。

**滚动看不到最新**
按 `End` 回到底部；有未读时提示行会显示"已向上滚动 N 行"。

## 限制

- 只支持单行输入；不支持鼠标、搜索、复制粘贴的块选择。
- 事件缓冲 5000 行、事件对象 2000 条，超出丢弃最旧的。
- 只显示启动之后的增量事件；更早的历史用 `History.txt` 或 `Archive-*.txt`。
- 不提供编辑存档的入口，避免与内核抢写。
