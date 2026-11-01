# 运行维护与迁移

## 旧 Agent 升级

先停止旧内核及其外部定时 hook，再检查并复制迁移：

```bash
fishkernel migrate /path/to/old-agent
fishkernel migrate /path/to/old-agent --output /path/to/new-agent
fishkernel run /path/to/new-agent --mock --once
```

不带 output 为只读检查。output 必须是不存在、且位于源存档之外的目录。
源存档检查和复制持有实例锁；复制所有文件但不执行其中程序。
复制过程中若失败，保留部分副本供检查，选择另一个空目的地重新复制；不要覆盖不完整副本。
也可直接启动旧档，内核会在锁内执行相同的幂等准备步骤；推荐先验证副本。

首次准备建立 `Runtime/legacy-v1-baseline/`，保存人格、长期记忆、Digest、History、events
的原始字节；记录 format=2。记忆根据事件回放识别已应用水位，随后仅应用新增写入。
无法匹配的记忆会停止并报 MemoryConflict，保留原文件供人工确定基线，不猜测或覆盖。

`Memory/Archive-*.txt` 和未知目录保留。History 是阅读投影，启动时可重建；基线保存原文。
旧 Tools、ToolsRegister 和 Work/periodic.sh 不自动加载、不执行；旧插件需按统一接口重写。
旧档若已有 Plugins/enabled.json，先将其原文保存到迁移基线，再清空启用列表，避免误启动旧扩展。
迁移通知会说明这一变化。旧任务只有 PID 而无可靠身份时显示 historical，不自动恢复/终止。

不要让旧内核/同步脚本同时写新档；需要回退时使用升级前备份或迁移的源存档，
不要删除新 checkpoint 来强行回放。原始 tar 与 Git 历史不由迁移修改。
副本迁移拒绝核心数据路径的符号链接，避免副本通过链接修改源存档；需先准备独立的数据目录。

## 控制

```bash
fishkernel say AGENT '新消息'
fishkernel ctl AGENT pause
fishkernel ctl AGENT resume
fishkernel ctl AGENT wake
fishkernel ctl AGENT delay 2
fishkernel ctl AGENT restart
fishkernel ctl AGENT stop
fishkernel status AGENT --jobs
fishkernel cost AGENT --json
```

暂停时继续保存输入，不自动恢复生成。stop 针对当前实例，新启动会清除遗留 stop。
restart 请求启动接班人；旧内核完成事件和插件清理后释放锁，新实例才开始写事件。
新输入中断等待，不中断在途生成；前台取消不能撤销已经发生的外部副作用。

错误排查：查看 `Runtime/kernel.log`、`Runtime/events.jsonl`、`Runtime/plugins/status.json`
和插件日志。插件连续失败三次后隔离，可修复后手动 restart。输入 ACK 表示已持久接收，
不表示 Agent 已处理。磁盘写入错误需先恢复存储，再启动内核。
