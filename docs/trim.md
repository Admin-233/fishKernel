# 事件日志归档（`fishkernel trim`）

`Runtime/events.jsonl` 只追加，长跑的存档会一直变大。`fishkernel trim` 把**已经被压缩掉**的老事件挪到 `Memory/` 下的归档文件里，让热日志保持小体积。

```bash
fishkernel trim ./archives/demo --dry-run   # 只看计划，不写盘
fishkernel trim ./archives/demo             # 交互确认后执行
fishkernel trim ./archives/demo --yes       # 跳过确认（脚本里用）
```

## 什么能被归档

压缩之后，`compaction/summary` 事件里的 `to_seq` 之前的所有事件**已经不再参与上下文投影**——`build_messages()` 只从最近一次 summary 的 `to_seq` 之后开始投影。这些事件只剩审计价值，可以从热路径挪走。

因此归档的切分点是：**最近一次 `compaction/summary` 的 `to_seq`**。

```
seq:  1 ......... to_seq | summary | end | 新对话 ...
     └──── 归档 ────┘    └────── 保留 ──────┘
```

## 安全原则：先验证，后提交

归档是不可逆的破坏性操作，所以工具不信任「理论上等价」，而是真的算一遍：

1. 算出归档前 `build_messages()` 的结果；
2. 在内存里算出归档后的结果；
3. **逐字节比较**。不一致就抛 `TrimError`，一个字节都不写。

这是唯一的正确性保证。测试 `test_refuses_when_projection_would_change` 专门守着它——把这段校验删掉，那条测试立刻失败。

## 它会拒绝的情况

| 情况 | 原因 |
|---|---|
| 存档还没完成过压缩 | 没有 summary 就没有安全切分点 |
| 事件日志为空 | 没东西可归档 |
| `compaction/summary` 缺少合法 `to_seq` | 无法确定边界 |
| summary 之后有未闭合的 `compaction/start` | 事务不完整，此时动日志可能破坏恢复 |
| 归档后投影会变化 | 说明有未知依赖，必须人工调查 |

拒绝时**不会**修改 `events.jsonl`，也不会留下归档文件。

## 它不会做什么

- **不重编号 `seq`**。seq 是事件身份，重编号会让 `prompt_seq` 锚点、`TokenMeter.restore()`、历史记录全部失效。归档只删除，不改写。
- 不删除 `compaction/summary` 本身——它是投影起点，删了等于丢历史。
- 不碰 `Runtime/spill/`（那是另一套清理逻辑）。

## 归档文件

写在 `Memory/Archive-events-<时间戳>.jsonl`，第一行是元信息头：

```json
{"_trim": true, "stamp": "20261027-130434", "cutoff_seq": 60, "archived_events": 60}
```

其余每行就是原始事件，格式与 `events.jsonl` 完全一致，可以直接 `jq` 查询。**被归档的事件没有丢失，只是换了位置。**

## 实测

一次典型归档（72 条事件 / 80 KB）：

```
归档计划：
  切分点        seq 60（最近一次 compaction/summary 的 to_seq）
  归档            60 条 / 30,841 字节
  保留            12 条
  ✓ 已校验：归档后上下文投影逐字节一致

events.jsonl：80,635 → 1,434 字节（省 79,201，98.2%）
```

## 什么时候该跑

- 存档跑了很多轮、`events.jsonl` 超过几十 MB；
- TUI / 网页控制台加载变慢；
- 备份存档觉得太大。

它天然幂等：没有可归档的事件时会直接告诉你「压缩点之前已经是空的」，不会重复归档。
