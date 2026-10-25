# 测试与部署

## 测试分层

| 层 | 位置 | 是否联网 | 说明 |
|---|---|---|---|
| 单元 / 集成 | `tests/` | 否 | 事件、投影、压缩、任务、控制、工具、锁、TUI、mock 循环，共 111 项 |
| 真实 API 冒烟 | `scripts/smoke_real.sh` | 是 | 限 2 轮，只读操作，验证 openai SDK + 工具调用 |
| 工具链路冒烟 | `scripts/smoke_tools.sh` | 是 | 限 3 轮，验证 spawn → wait_job → job_output |
| TUI 冒烟 | `scripts/smoke_tui.sh` | 否 | 渲染快照 + 在伪终端里启动一次真实 TUI |

安全约定：由于内核给模型全部权限，**真实模型相关测试只在隔离测试机上跑**。纯单元测试不执行模型、不联网，可以在任意环境跑，但仍建议统一在测试机上执行，避免环境差异。

## 本地跑单测

```bash
python -m pytest -q
```

只依赖 `pytest`。会用临时目录建存档，起几个 `echo` / `sleep` 子进程，不访问网络。

## 推送到隔离测试机执行

```bash
export FISHKERNEL_TEST_HOST=<测试机地址>
export FISHKERNEL_TEST_USER=<用户名>
# 二选一：
export FISHKERNEL_TEST_PASSWORD=<密码>     # SSH 密码 + sudo 密码
# 或者配好 SSH key 且 sudo 免密（脚本会用 sudo -n）

python scripts/deploy.py
```

脚本做四件事：打包（排除 `.git`/`.venv`）→ `scp` → 远端解包 → 建 venv、装依赖、`pytest`。

参数：

```text
--host / --user / --password     同环境变量
--remote-dir                    默认 /home/<user>/fishKernel
--real-api                      追加真实 API 联调
--api-key                       默认读 FISHKERNEL_KEY
```

## 真实 API 联调

```bash
export FISHKERNEL_KEY=sk-...
python scripts/deploy.py --real-api
```

`smoke_real.sh` 会：

1. 在 `/tmp/fk-smoke` 新建存档；
2. 在 `Agent.txt` 追加“只做只读操作、最多一次工具调用”的约束；
3. 跑 2 轮，打印 `status`；
4. 断言 `events.jsonl` 里出现 `assistant/message`，否则失败。

工具链路联调（手动）：

```bash
ssh <test-host> 'FISHKERNEL_KEY=sk-... bash ~/fishKernel/scripts/smoke_tools.sh'
```

它会跑 3 轮：`spawn("echo fishkernel-ok")` → 内核通知任务完成 → `wait_job` → `job_output`，最后检查 `Runtime/jobs` 里出现 `fishkernel-ok`。

## TUI 冒烟

```bash
bash scripts/smoke_tui.sh
```

先生成一份 mock 存档并跑 3 轮，再用 `--snapshot` 渲染一帧（肉眼检查布局），最后用 `script` 分配伪终端、真实启动 TUI 3 秒，确认能进入界面且不崩。

## 交班冒烟

```bash
bash scripts/smoke_handover.sh
```

启动一个 mock 内核 → `ctl restart` 触发交班 → 检查 `events.jsonl` 里出现
`session/handover → session/end(restart) → session/start(新 pid)`，并校验 seq 严格连续。

## 加测试

- 存档 fixture 在 `tests/conftest.py`，用 `tmp_path` 建临时存档。
- 模型用 `ScriptedClient`（`fishkernel.llm`）：预置 `Completion` / 异常 / 函数，按顺序消费。
- 循环测试把 `error_backoff_base=0`、`turn_delay=0`，否则会很慢。

示例：

```python
def test_xxx(archive):
    kernel = Kernel(archive.root, client=ScriptedClient([Completion(text="好")]), max_turns=1, echo=False, error_backoff_base=0)
    kernel.run()
```

## 建议的 CI（未接入）

```yaml
# 概念示例
- run: pip install -e '.[dev]'
- run: python -m pytest -q
```

真实 API 的测试不要放进公开 CI，密钥会暴露在日志里；需要的话用受保护的 secret 并只跑 `--max-turns 1`。
