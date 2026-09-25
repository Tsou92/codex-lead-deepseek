# 历史会话恢复（离线）

`import-monitor-history` 是一个**纯离线**工具：它把 leadseek 任务在 Harness 里保存下来的压缩会话
（`session.v4.jsonl.zstd`）归一化成监控读得懂的 `telemetry.jsonl`。全程只读本机文件，不联网、不调用
模型、不启动 Harness，也不修改原始压缩文件。

它解决的问题：旧任务当时没有采集 `telemetry.jsonl`，但 Harness 的 session store 里还留着公开事件。
恢复工具用现有的 `plugins/observe.mjs` 里的 `createObserver` 做归一化、过滤与脱敏，因此不会另造一套过滤规则。

## 用法

```bash
# 恢复全部历史（只处理已结束、有 session_id 的 run）
bin/import-monitor-history

# 只恢复一个 run
bin/import-monitor-history --run-id 20260925-091205-2179da55
```

底层脚本也可直接用 Node 调用（自定义根目录时）：

```bash
node scripts/import-monitor-history.mjs --root /path/to/project [--run-id RUN_ID]
```

Python 入口是标准库 wrapper：它通过 `leadseek.configuration.load_config(root)` 取到配置里的 Node 24，
再执行 `scripts/import-monitor-history.mjs`，不会读取或改写系统凭据、也不会改动配置。

输出：stdout 打印一段简短 JSON 统计 `{imported, skipped, failed, runs, warnings}`，同时把同一份统计写到
`.state/monitor/history-import.json`（权限 0600）。失败项在 `reason` 里给出具体原因，不会用假成功掩盖。

退出码：`0` 全部成功（可能有 skipped），`1` 有 run 失败，`2` 配置/Node/脚本缺失，`3` 未预期的脚本错误。

## 恢复范围与规则

- 只处理 `.state/runs/<run_id>/result.json` 中 `status` 属于明确白名单 `completed, failed,
  needs_attention, cancelled, timed_out, scope_violation, log_limit_exceeded` 且带 `session_id` 的 run；
  运行中、准备中、未知状态或缺少 `session_id` 的 run 直接跳过，避免把未结束状态冻结成历史记录。
- 先扫描本机 `.state/harness-home/sessions/<workspace-key>/<session-id>/session.v4.jsonl.zstd` 的 header 索引，
  再只解压该根会话，以及通过 `header.parentSession` 归属到它名下的子会话。**无关联的其它 session 一律不写入。**
- 会话文件是多个独立 zstd frame 串联，工具会逐帧推进直到文件结束（不是只解第一帧）。
- 旧的 run 没有 `context.json` 时不伪造 Codex 关联。
- 目标 `<run>/telemetry.jsonl` 已存在且非空时**一律跳过**，绝不覆盖新采集到的真实数据。
- 所有路径组件拒绝 symlink；`run_id` 使用白名单正则；扫描数量、压缩大小、解压后大小分别有上限
  （压缩/解压各 32 MiB）。`.state/monitor`、锁文件、临时文件与 `history-import.json` 在 mkdir/写入前
  同样检查 symlink，临时文件以 `O_EXCL|O_NOFOLLOW` 创建。
- 不导入 `isSeeded=true` 的会话：这类会话继承父历史，缺少可靠继承边界时会重复计入父 usage，因此跳过并
  记录 warning，run 标记为 `partial`。若**根会话本身** `isSeeded=true`，整个 run 直接 `skipped`，不生成
  空 telemetry 去替代父历史流。
- 全局会话索引扫描出现告警（如某个 header 无法读取、扫描达到上限）时，无法保证子会话完整，因此所有相关
  run 都标记 `partial`，并把告警写进结果，避免漏了子会话却宣称完整。
- 损坏或半帧：根会话损坏 → 该 run 失败、不写 telemetry；子会话损坏 → 已恢复的部分保留，run 标记
  `partial` 并记录 warning，不伪装成完整成功。
- 输出达到 telemetry 字节上限被 `createObserver` 截断时，run 同样标记 `partial` 并记录「截断」warning。

## 写入内容

- 采用「先写临时文件、再原子改名」的方式，保留原压缩文件不变；同一 run 用 `.state/monitor/locks/` 下的
  锁文件避免并发导入，失败时清理自己创建的临时文件。
- 首条与末条是可见的 `telemetry_status`：说明这是**保存会话的恢复**、导入时间，以及 `complete` / `partial`。
  标记 `partial` 时，末条还会在 `text` 里写出具体缺失/损坏原因（限长、经 observe 脱敏），页面无需再去翻统计文件。
- 事件时间由保存事件里的原始 `time` 驱动（不是导入时间）；`agent_created / message / tool_call /
  tool_result / usage / agent_finished / subagent_*` 等字段由 `createObserver` 产出，`parent_id` 保留父子关系。
- 逐个代理的 `model` 只从 `request/context` 的 `provider/model` 安全字段读取，不序列化原始请求。
- 不读写、不输出 `system`/`developer` 指令以及 `thinking`/`reasoning`/`stream` 等私密思考内容。
- 每个代理每个 `turn.step` 的 usage 只计一次；父子代理分别计量，不把主代理与子代理相加，也不声称总账单精确。

## 「已采集」与「未保存恢复」的区别

- **已采集**：任务运行时由 observe 插件实时写入的 `telemetry.jsonl`，字段完整、时间真实，是首选数据。
  恢复工具不会触碰它。
- **未保存恢复**：任务当时没有留下 telemetry，只能从保存下来的压缩会话重放公开事件。它的覆盖范围受限于
  保存了什么（例如 seeded 会话、损坏子会话会缺失），因此可能标 `partial`，缺失项在 `warnings` 中列出。
- 恢复结果只还原**公开可观测事件**，不保证子代理用量完整，也不代表 Codex 的验收结论。展示时应区分
  「执行完成」与「验收通过」。
