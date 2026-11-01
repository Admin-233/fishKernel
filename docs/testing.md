# 测试

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

完整验收必须在 Linux。测试包含真实文件锁、多进程、插件崩溃/超时、后台任务接管、
SIGKILL 后恢复和真实 PTY；可移植单测不能替代这些验收。浏览器逻辑测试使用 Node.js
执行页面中的实际脚本，验证令牌、单轮询链、分页和发送失败保留输入。

CI 使用 Linux / Python 3.10–3.13，并安装 Node.js。测试使用合成存档、脚本客户端和
本机模拟 HTTP 服务，不需要真实模型密钥。端到端真实 API 冒烟为显式手动操作，
设置 FISHKERNEL_API_KEY、FISHKERNEL_MODEL、FISHKERNEL_API_BASE 后再运行 smoke_real.sh。

私有旧档验证：`python scripts/check_agent_archive.py /path/to/fish.tar.gz --resume`。
仅从 tar 读取允许的数据文件，复制到临时目录；Linux 下 --resume 使用无网络客户端
完成迁移、续跑、再次启动，校验原事件前缀及人格/记忆字节；不执行 tar 中程序。
工具不输出记忆正文、密钥，不将私有样例提交仓库。

`scripts/check_memory_sync.py AGENT` 只读检查记忆基线；冲突非零退出。
`scripts/check_coverage.sh` 使用当前 Python 环境的 coverage，测试失败不掩盖。
`scripts/deploy.py` 仅打包 Git 已跟踪文件，通过用户现有 SSH 认证发送到独立测试目录，
不保存密码、不执行 sudo。当前实施只提交本地 Git，部署脚本不会自行运行。
