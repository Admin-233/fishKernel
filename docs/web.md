# 网页控制台

`fishkernel web` 起一个只读写存档文件的网页控制台。它和 `fishkernel tui` 是同一条设计路线的两种界面：**不碰内核进程，不碰锁**，因此可以在内核运行时随便开、随便关、开多个。

```bash
fishkernel web ./archives/demo                 # http://127.0.0.1:8787/
fishkernel web ./archives/demo --port 9000     # 换端口
fishkernel web ./archives/demo --port 0        # 随机端口（启动时会打印实际端口）
fishkernel web ./archives/demo --host 0.0.0.0 --token s3cret   # 暴露到局域网 + 令牌
```

## 界面

- 顶栏：内核状态 / 轮次 / 控制状态 / 事件总数 / 累计花费，右侧是暂停、继续、唤醒、交班重启、停止。
- 主区：事件流，按类型着色（用户、助手、工具调用、失败、系统、压缩）。每秒增量拉取，滚动到底部时自动跟随。
- 右栏：最近的异步任务（id、命令、状态、退出码、stdout 字节数）。
- 底栏：给 Agent 发消息（等价 `fishkernel say`）。

## HTTP 接口

控制台本身是纯静态页面，所有能力都在下面四个接口上，可以直接用 curl 或自己写脚本：

| 方法与路径 | 作用 | 参数 |
|---|---|---|
| `GET /` | 页面 | – |
| `GET /api/snapshot` | 状态 + 增量事件 + 任务列表 | `after=<seq>` 增量；`tail=<n>` 只看最后 n 条（优先于 after） |
| `GET /api/job_output` | 读任务输出 | `job_id`、`offset`、`stream=stdout\|stderr` |
| `POST /api/say` | 投递用户消息 | `{"text": "..."}` |
| `POST /api/ctl` | 控制内核 | `{"action": "pause\|resume\|stop\|wake\|restart"}` 或 `{"action":"delay","value":2.5}` |

例：

```bash
curl -s 'http://127.0.0.1:8787/api/snapshot?tail=5' | jq '.state.control'
curl -s -X POST http://127.0.0.1:8787/api/ctl -d '{"action":"pause"}' -H 'Content-Type: application/json'
curl -s -X POST http://127.0.0.1:8787/api/say -d '{"text":"去看看 Tools 目录"}' -H 'Content-Type: application/json'
```

## 性能

长跑存档的事件日志会长到几十万条。控制台每秒轮询一次，所以做了两层优化：

1. **增量折用量**：`UsageCache` 记住「已折叠到哪个 seq」，只处理新增事件，而不是每次都把全部 `assistant/message` 重算一遍。
2. **二分定位**：增量取事件时用 `bisect` 定位起点，不为了末尾几十条去遍历全表。

20 万条事件（42 MB）实测：冷启动 554 ms，稳态轮询 9.8 ms（约 56 倍）。

## 安全

- 默认只监听 `127.0.0.1`。控制台等于「对存档的完全控制权」，不该默认暴露到局域网。
- 需要跨机访问时用 `--host 0.0.0.0 --token <令牌>`，请求要带 `X-Fish-Token` 头或 `?token=`。
- 页面本身不校验令牌（否则无法输入），它只提供 UI；真正的数据接口全部受保护。
- 没有 HTTPS：跨网络请套一层 SSH 隧道或反向代理。
  ```bash
  ssh -L 8787:127.0.0.1:8787 user@host   # 然后在本地打开 http://127.0.0.1:8787/
  ```

## 边界

控制台只做四件事：读事件、读内核状态、读任务、写控制文件。它**不会**：

- 启动或停止内核进程（那是 `fishkernel run` / `ctl stop` 的事）；
- 修改 `Agent.txt`、`LongTermMemory.txt`、`Digest.txt`（这些只有内核写）；
- 改写 `events.jsonl`（只追加，任何界面都不能改历史）。
