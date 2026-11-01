# 基础工具

当前请求中的 JSON Schema 是工具参数的权威定义，见 `src/fishkernel/tools.py`。

| 工具 | 用途 |
|---|---|
| read_file / list_dir | 有界读取文件、图片和目录；大内容分页或 spill |
| spawn | Linux 异步命令，返回 job_id；最多 32 个活跃任务 |
| wait_job | 有界等待；新输入/暂停/停止只结束等待，后台任务继续 |
| job_output / job_list / job_kill | 分页读取输出、查询任务、显式终止验证过身份的进程组 |
| memory | append/replace 长期记忆；事件提交后立即投影并记录水位 |
| sleep | 可被持久输入或控制指令中断的等待 |
| plugin_manage | list/inspect/validate/enable/disable/restart/reload |

工具返回区分成功、错误、执行前中断和副作用结果不确定。单轮最多执行 16 个调用，
超额调用仍补齐协议结果。生成期间收到输入后，该响应中未执行调用不会继续旧计划。

spawn 的 stdout/stderr 各最多 16 MiB，由独立任务进程限制，超限终止任务；
输出不依赖内核读取线程，交接后继续可查。job_output 支持字节偏移和读取上限。
内核仅自动清理已终止且活动事件不再引用的旧任务；历史记录不自动变成新任务。

插件不是内核内 import 的 Python 模块，编写与注册方法见 [插件协议](plugins.md)。
基础工具允许在当前系统用户权限内执行命令，并非权限沙箱。
