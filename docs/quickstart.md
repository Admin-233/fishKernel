# 配置与快速开始

Linux / Python 3.10+，安装和创建步骤见 README。存档里的 `basic.instance` 使用 key=value：

```ini
name = demo
model = provider/any-model-id
api_base = https://api.example.com/v1
api_key = sk-...
context_window = 32768
max_output_tokens = 8192
max_tokens = 4096
turn_delay = 2
temperature = 0.7
thinking = auto
```

密钥优先读取配置；占位符会回退到 `FISHKERNEL_API_KEY`，旧 `DEEPSEEK_API_KEY` 仍兼容。
`max_tokens` 是每次请求的输出预算，受 `max_output_tokens` 限制；后者小于 context_window。
未知模型未配置窗口时采用 16384 的保守预算，并非对服务能力的判断。旧模型名的历史默认值保留，
切换服务后应显式填写真实窗口。URL 的 # 片段不会被误当作注释。

可选 `model-options.json` 定义服务参数能力。例如某服务接受 reasoning_effort 时：

```json
{
  "output_parameter": "max_completion_tokens",
  "send_temperature": false,
  "tools": true,
  "vision": false,
  "thinking_modes": {
    "on": {"reasoning_effort": "high"},
    "off": {"reasoning_effort": "none"}
  }
}
```

这些取值仅演示映射，必须以所用服务的实际参数为准；并非所有服务支持 none/high。
`thinking=on/off` 必须有对应映射；auto 不注入模式开关。旧 `effort` 是 reasoning_effort
的别名，值不设枚举。与模式映射冲突会报错，不能同时发送互相矛盾的参数。
另一种映射形式为 `{"extra_body":{"thinking":{"type":"enabled"}}}`；同样需服务支持。

`extra_body` 可传服务扩展字段，但不能覆盖 model、messages、tools、stream、输出预算等核心字段。
`reasoning_field` 可设为 reasoning_content 或 reasoning；默认不回传私有推理字段。
`require_reasoning=true` 会校验历史工具调用的完整推理数据；缺失/截断会明确报错，不能补造。
响应原始 reasoning 单独完整存储，显示截断不影响协议数据。

可选价格示例：`"prices":{"currency":"USD","input":1,"output":2,"cache_input":0.2}`，
单位为每百万 token。数字是示例，不代表任何模型售价。未配置时显示费用未知。

配置文件更新在下一请求边界生效，窗口或模型变更会使旧计量锚点失效。状态显示生效模型、
思考设置和配置 revision；auto 显示“服务端默认”。修复认证/参数错误后保存配置即可重新尝试。
