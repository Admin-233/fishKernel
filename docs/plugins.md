# 插件协议 v1

插件是独立 Linux 进程，通过 stdin/stdout 交换一行一个 JSON 对象。
协议通道最大 512 KiB/帧，收发队列分别最多 256 帧且累计不超过 4 MiB。stderr 是诊断输出。
同一存档最多启用 32 个插件，每插件最多声明 32 个工具。

## 创建和启用

在存档的 `Plugins/<id>/` 创建 `plugin.json` 和程序；可以另放 `config.json`。
manifest 示例见仓库 `examples/plugins/demo/`。ID 使用小写字母、数字、下划线，
以字母开始，最长 24 字符。`command` 是 argv 数组，`{python}` 替换为内核 Python。
`protocol` 固定为 1，`timeout` 是前台调用超时（0–300 秒，不含 0）。
可选 version 字段显示插件版本；generation 是宿主为每次启动分配的实例标识。

工具在 manifest 的 tools 数组声明 name、description、parameters；parameters 为
JSON Schema 2020-12 object，不支持远程引用。公开名为 `plugin_<id>__<name>`。

模型可以用基础工具写入文件，调用 `plugin_manage` 的 validate、enable、reload。
CLI 对应 `fishkernel plugin <存档> validate|enable|disable|restart|reload <id>`；
启停命令进入控制队列，由内核在模型请求之间执行，命令输出给出结果文件路径。
inspect/validate 可离线执行；list 读取最近运行状态。

创建目录不会启动插件。成功登记的启用配置保存在 `Plugins/enabled.json`。
旧 ToolsRegister、Work/periodic.sh 和其他个人 hook 不作为插件入口。

## 消息

| 方向 | type | 必需字段/含义 |
|---|---|---|
| 宿主→插件 | initialize | protocol、generation、config |
| 插件→宿主 | ready | 回传同一 protocol 和 generation；5 秒内完成 |
| 宿主→插件 | ping | 保活请求，每约 5 秒 |
| 插件→宿主 | pong | 保活应答；就绪后 20 秒无有效响应视为失联 |
| 宿主→插件 | call | id、name（插件内部名）、arguments |
| 插件→宿主 | result | id，以及 result 或 error；只能完成自己的待处理请求 |
| 宿主→插件 | cancel | id；1 秒内响应，否则终止/隔离该插件 |
| 插件→宿主 | input | id、text，可选 external_id、metadata |
| 宿主→插件 | ack | id、ok；成功包含 input_id，失败包含 error |
| 插件→宿主 | subscribe | events（事件类型列表，可用 *）、after_seq |
| 插件→宿主 | log | message；level 可为 debug/info/warning/error，宿主脱敏并限长写日志 |
| 插件→宿主 | health | status（最多 200 字符）；写入插件状态视图 |
| 宿主→插件 | event | event（原事件对象）；插件保存自己的已处理游标 |

input 的 ACK 表示已经持久化进入内核收件箱，不代表模型已阅读或完成任务。
同一插件相同 external_id 去重；断线重试必须复用相同 ID。
正文最多 256 KiB，整个输入信封最多 512 KiB，待处理收件箱最多 16 MiB。
队列满不确认成功，插件应退避重试。订阅是至少一次消费模型；重连时提交
已处理 seq，业务副作用由插件自行去重。活动事件日志被 trim 后的旧事件不保证
通过订阅重放，需按归档策略另行读取。

取消已执行的外部操作不等于撤销。结果无法确定时返回明确错误；只有确定没有
执行时才设置 `not_executed: true`，否则宿主不自动重放。插件崩溃、超时和协议错误
写日志和模型运行通知，连续失败三次后停止自动重启；手动 restart 可重新尝试。
一次模型请求绑定当时的工具世代。生成期间插件重启后，旧调用返回 PLUGIN_CHANGED，
不会交给新实例执行。reload 先校验 manifest/config；校验失败保留旧实例，
切换后启动失败则明确禁用。启动时的自动恢复采用有限退避，不无限拉起故障进程。

## Python 辅助库

`fishkernel.plugin_sdk.Plugin` 提供 framing、握手、保活和工具执行单元。
`Plugin({"echo": echo}, on_start=..., on_event=..., on_ack=...).run()` 启动服务。
工具函数接收 dict、返回 JSON 可序列化结果；须定义为可被 multiprocessing spawn
导入的模块顶层函数。stdout 留给协议，普通打印在工具执行单元中转到 stderr。
不要在模块导入时运行服务，使用 `if __name__ == "__main__"`。

`plugin.input(text, external_id=..., metadata=...)` 提交外部消息；在 on_ack 中处理
确认与失败。`plugin.subscribe([...], after_seq=...)` 订阅事件。
生命周期/事件回调应短小；长期接收器放在插件自己的线程或进程中。
示例的 wait 工具展示前台取消，定时 input 展示消息唤醒，不包含任何专用聊天平台。

工具调用错误由宿主转换成结果；插件退出不保证它调用的外部服务没有产生副作用。
进程隔离不构成权限沙箱。不要直接修改核心事件、记忆投影或收件箱确认位置。
日志限制为当前文件及上一份，config 中 key/token/password/secret 字段会被日志脱敏；
插件仍应避免主动输出凭据。
