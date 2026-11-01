# fishKernel

**DO NOT USE THIS FRAMEWORK, while its maintained completely by LLM, it's experimental and unstable, also lack of standardization.**

fishKernel 是一个持续运行的 Agent 内核：调用 OpenAI Chat Completions 格式的接口，
执行基础工具和独立进程插件，并持久保存人格、长期记忆和对话上下文。

当前支持 **Linux / Python 3.10+**。Windows 运行时暂不支持；平台相关能力集中在
`platform.py` 和终端后端，便于以后适配。模型 ID 不设白名单。

## 开始使用

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
fishkernel new ./archives/demo --name demo --model provider/model --api-base https://api.example.com/v1
export FISHKERNEL_API_KEY='你的密钥'
fishkernel run ./archives/demo --mock --once   # 离线检查
fishkernel run ./archives/demo               # 真实模型
```

另开终端运行 `fishkernel tui ./archives/demo` 或 `fishkernel web ./archives/demo`。
使用真实服务前，按其能力填写上下文窗口、输出限制和思考参数；见
[配置与快速开始](docs/quickstart.md)。`thinking=auto` 表示服务端默认，不保证开启思考。

## 扩展与兼容

插件放在存档的 `Plugins/<id>/`，显式启用后运行。插件可提供工具、输入消息、
订阅事件和定时行为；崩溃、超时或协议错误会被隔离并通知模型。
参考 [插件协议](docs/plugins.md) 和 [通用示例](examples/plugins/demo/)。

外部消息可以中断 sleep、工具等待和前台插件调用；在途模型生成不会被取消。
中断 `wait_job` 只结束等待，后台任务继续。

旧 Agent 的人格、记忆、摘要和事件历史保留。旧个人工具和 hook 不再自动加载或执行，
需要按新插件接口重新适配。建议先用 `fishkernel migrate OLD --output NEW` 创建迁移副本，
再运行新副本；详见 [运行维护](docs/operations.md)。不要对同一存档交替运行新旧内核。

## 文档

- [架构](ARCHITECTURE.md) / [改造契约](docs/refactor-v1.md)
- [基础工具](docs/tools.md) / [TUI](docs/tui.md) / [Web](docs/web.md)
- [归档](docs/trim.md) / [测试](docs/testing.md) / [变更记录](CHANGELOG.md)
- [初版设想](KernelDesign.md)：历史设计记录，不代表当前实现
