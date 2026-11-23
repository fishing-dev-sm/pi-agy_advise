---
name: agy-advise-mcp
description: 用本机 Codex 的 agy_advise MCP 工具向 Google Antigravity 请求跨项目的独立全局技术判断与项目级决策治理（MCP 版；Pi 扩展版是另一个 skill，叫 agy-advise）。适用于任何项目（软件架构、系统重构、性能/排障卡点、ML/RL 实验、产品技术取舍）的全局审查、前提挑战、反方红队评审、阶段复盘与第二意见。
---

# Antigravity Advisor (`agy-advise-mcp`)

> 本文件只描述 **MCP 版** `agy_advise`。Pi 扩展版同名工具（skill `agy-advise`）行为不同：在 Pi 的工作目录里运行、可用只读工具、不写本地账本，且不共享会话与权限策略。先确认自己在调哪一个。

通过本机注册的 `agy_advise` MCP 工具，请 Google Antigravity 作为**独立全局技术审查员**，基于目标、硬约束与已证实证据给出不顺从局部惯性的判断。CLI 在临时目录的只读禁工具沙箱中运行，文件、命令、浏览器和 MCP 均被禁用，仅接收本次显式发送的文本。

本 Skill 提供两层无缝衔接的能力：
1. **全局问答模式（默认，零配置适用于任意项目）**：单次提问或短追问即可获得“先跳出框架审前提、再落到行动与止损”的全局意见。
2. **项目决策核心模式（`project` 模式）**：面向需要跨多轮迭代防跑偏的项目，持久化保存目标、冻结门槛、红线约束、路线历史与证据账本，在停滞、空转、代理指标漂移或保护项回归时自动拉停并触发无菌室全局复盘。

---

## 一、什么时候问

- 需要跨局部细节做**全局判断**：架构取舍、路线分叉、反复排查仍未收敛的卡点、破坏性改动前的风险与回滚评估、怀疑当前正在“修局部症状而非主矛盾”。
- 需要**独立第二意见或红队挑刺**：在投入大量开发/算力成本前，验证主假设是否成立、有哪些未证实前提和最便宜的证伪测试。
- 文件内容、API 签名、明显拼写、高频工具循环中的机械小问题应在本地直接解决，不调用本工具。

---

## 二、全局问答模式（适用于任何项目的即时咨询）

| 参数 | 说明 |
| --- | --- |
| `question` | 每次聚焦一个核心判断。写清终局目标、硬约束、已观察到的失败与待裁决事项。首问须自包含；不要诱导顾问“从我给的两个小补丁里挑一个”。 |
| `context` | 可省略。提供获授权分享的**高信噪比结构化事实**（建议按：目标 / 硬约束 / 已试方案与实测结果 / 关键未知组织），不限死 10 行但禁止粘贴冗长日志、未授权机密或 Codex 内部推理过程。 |
| `stance` | 顾问立场，默认 `"global"`：<br>• `global`（默认）：先拉高视角审计前提与主矛盾，区分**已证实事实 / 推断 / 关键未知**，有权否定当前提问框架或建议停掉沉没成本分支，再给出最高杠杆下一步与止损条件。<br>• `red_team`：纯反方红队审计，专门构造当前方案会失败的最强论据、暴露代理指标陷阱与单点风险，并给出最便宜的证伪测试。<br>• `tactical`：仅在大方向已锁定且只需要具体实现/实验细节时使用；仍会指出明显前提矛盾。 |
| `session_id` | 首次提问、换主题或换项目时省略。继续同一判断时传入上次返回的 ID，仅补充新证据。 |
| `timeout_seconds` | `1–240` 秒，默认 `240` 秒（含排队时间）。 |
| `effort` | 新会话可选推理档位（`low`/`medium`/`high`/`xhigh`/`max`），追问沿用原档位。 |
| `model` | 新会话可选模型（如 `gemini-4-neon-high`），未传时读取 `AGY_ADVISE_MODEL`。追问沿用原模型。 |

**提问原则**：
- 把 Codex 自己的执行限制留在本地，只把影响技术判断的真实约束发给顾问。
- 避免“帮我给这个局部微调定一个阈值（200字内）”这类框死视角的问法；应交代全局目标与此前失败，允许顾问回答“这个方向本身该停”。

---

## 三、项目决策核心模式（`project` 模式）

在同一 `agy_advise` 工具中传入 `project`，以 `(id, phase)` 标识项目阶段。支持**任意领域项目**——既支持数值指标（越高越好或越低越好），也支持无分数的定性里程碑（架构迁移、根因排查、功能交付）。

### 支持的动作（`project.action`）

| `action` | 作用与核心字段 |
| --- | --- |
| `init` | 绑定不可篡改的 `objective`、`frozen_gate`、`route`，可选 `domain`、`non_negotiables`（硬红线列表）、`initial_facts`（预置的跨轮硬事实写入 L2 记忆）。<br>• **数值门槛项目**：同时提供 `score_name`、`baseline_score`、`pass_score`，可选 `score_direction`（`"higher"` 默认 / `"lower"` 如延迟、错误率）和 `min_improvement`（过滤噪声抖动）。<br>• **定性里程碑项目**：直接省略上述三个分数字段，靠 `outcome` 推进，无需编造假分数。<br>• 可选防跑偏阈值：`stagnation_limit`（默认 `2`）、`same_mechanism_limit`（默认 `3`）、`proxy_limit`（默认 `3`）、`review_cadence`（周期强制体检步数）。 |
| `report` | 上报一次已结束的尝试：`trial_id`、`route`、`cycle`、`mechanism`（假设/手段类别）、`evidence`。<br>• 数值项目仅真实端到端门槛评测填 `score`；代理/离线/诊断证据留空 `score` 并可标 `evidence_kind: "proxy" \| "diagnostic"`。<br>• 定性项目不填 `score`，用 `outcome`（`progress` / `no_progress` / `disproved` / `blocked` / `qualified`）表达结果。<br>• 红线/保护项受损填 `regressions`；全部冻结门槛经本地核实通过才填 `qualified: true`（或 `outcome: "qualified"`）；可选 `artifacts` 记录产物路径/哈希（仅作元数据，工具不会读取本地文件）。<br>• 并行多条结果回收时，可设 `auto_review: false` 瞬时批量入账，最后统一调用一次 `review`，避免中间复盘因新证据到达而 `stale`。 |
| `memory` | 显式维护 L2 蒸馏记忆（不调 LLM、不改冻结门槛）：`add_facts` / `remove_facts` 增删跨轮硬事实（上限 12 条），`route_postmortems` 记录或补录已放弃路线的 1–2 句尸检结论（上限 12 条）。 |
| `status` | 仅读本地状态（含 `pending_unblock` 待核销清单与 `memory_counts`）；传 `include_history: true` 可导出完整三层记忆、路线统计与 `markdown_summary`（便于落盘到项目仓库人工审计）。 |
| `list` | 列出本地持久化库中的所有项目阶段（可传 `id` 过滤），仅读本地。 |
| `review` | 主动触发一次无菌室全局复盘，或重试先前超时/无效的复盘。常规解封/短验复查建议传 `effort: "medium"` 提速，重大停滞或换路复盘用默认/高档位。 |
| `activate` | 用当前 `decision_id` 激活 `CONTINUE` 或 `PIVOT` 裁决的新轮次、路线与次数预算，清空 `pending_unblock` 并重新开放 `dispatch_allowed`。 |

### 自动拉停与反死胡同触发器

满足以下任一条件时，工具先持久化入账，将 `project.dispatch_allowed` 置为 `false`，并自动发起复盘（除非 `auto_review: false` 延迟到手动 `review`）：
1. `stagnation`：达到 `stagnation_limit` 个不同 `mechanism` 均未实质推进门槛；
2. `same_mechanism_spin`：同一个 `mechanism` 连续空转达到 `same_mechanism_limit` 次（堵住单机制反复微调漏洞）；
3. `proxy_drift`：连续 `proxy_limit` 次只报代理/未评分证据、未跑真实门槛评测（堵住刷离线指标的伪进展）；
4. `protected_regression`：任一保护项/红线回归；
5. `blocker`：上报 `outcome: "blocked"`；
6. `budget_exhausted`：当前轮次达到已激活计划的 `max_trials`；
7. `review_cadence`：达到设定的周期性强制体检步数；
8. `qualified_result`：出现满足冻结门槛的验收结果，须复核后才可 `ADVANCE`。

### 三层外置记忆 + 无菌室复盘协议（Three-Tier Memory + Clean-Room）

- **绝不复用长聊天历史**：`project` 模式禁止传 `session_id` 和 `stance`，每次复盘必开干净会话，彻底避免沉没成本锚定与上下文滚雪球。
- **O(1) 有界三层外置记忆包**（存在本地 SQLite，不随项目运行半年而膨胀）：
  1. **L1 宪法层（永不压缩）**：`objective`、`frozen_gate`、`non_negotiables`、`active_plan` 及生效中的 `pending_unblock` 解封核销清单；
  2. **L2 蒸馏记忆层（长期核心认知，各限 12 条）**：`route_postmortems`（每条已放弃路线的 1–2 句证伪结论）、`verified_facts`（跨轮证实的硬事实）与 `route_stats`（各路线聚合统计），每次复盘自动沉淀且可用 `action: "memory"` 人工校正；
  3. **L3 近期工作记忆层（滑动窗口）**：最近 4 条裁决、最近 20 条试验摘要、最近 6 条详细证据。
- **截停即开处方 + 复审核销**：
  - `CONTINUE`/`PIVOT` 强制包含 `route`、`next_trial`、`expected_signal`、`stop_condition`、`max_trials`；
  - `HOLD` **代码级强制包含** `unblock_actions`（1–3 项具体修复/排查动作）与 `unblock_criteria`（重新申请放行的最小可验证证据），并挂入 `project.pending_unblock`；下次 `review` 时自动置顶，强制顾问逐项核销后才能解封；
  - 调用 `review` 时不要在 `question` 里替顾问提前定调（例如不要写“请裁决 HOLD”），只陈述新事实，把裁决权完整留给顾问。
- 每次派发新任务前检查 `project.dispatch_allowed`；为 `false` 时暂停新派发，但继续据实上报已完成的在途任务。超时或新证据并发到达时，待复盘状态保留且旧裁决自动作废（`stale`），重启 MCP 后可直接 `review` 续接。

### 跨领域调用示例

1. **定性项目（后端架构迁移 / 复杂排障，无需伪造分数）**：
```json
{
  "project": {
    "id": "order-service",
    "phase": "cutover-design",
    "action": "init",
    "domain": "architecture",
    "objective": "消除同一 SKU 并发扣减时的丢写并完成无停机迁移",
    "frozen_gate": "100 并发压测零丢写且旧版 API 契约全量回归通过",
    "non_negotiables": ["不破坏现有 HTTP API", "P99 延迟增幅 < 10ms"],
    "route": "optimistic-version-lock",
    "review_cadence": 4
  }
}
```

2. **量化项目（越低越好：P99 延迟优化；或越高越好：通过率）**：
```json
{
  "model": "gemini-4-neon-high",
  "project": {
    "id": "search-engine",
    "phase": "p99-latency",
    "action": "init",
    "domain": "performance",
    "objective": "将线上查询 P99 从 240ms 压至 <= 80ms",
    "frozen_gate": "真实回放流量 10 分钟 P99 <= 80ms 且召回率下降 < 0.2%",
    "score_name": "p99_ms",
    "baseline_score": 240,
    "pass_score": 80,
    "score_direction": "lower",
    "min_improvement": 5,
    "route": "index-pruning"
  }
}
```

3. **并行结果批量入账后统一复盘**：
```json
{
  "project": {
    "id": "robotics",
    "phase": "phase2",
    "action": "report",
    "trial_id": "side-drift-ablation",
    "route": "local-actor-refinement",
    "cycle": 0,
    "mechanism": "side-drift-reward",
    "score": 2,
    "evidence": "冻结动态仍为 2/8，侧漂失分增加。",
    "auto_review": false
  }
}
```

持久化库默认路径为 `$XDG_DATA_HOME/codex-agy-advise/projects.sqlite3`（未设 XDG 时为 `~/.local/share/codex-agy-advise/projects.sqlite3`），测试可用 `AGY_ADVISE_DB` 指定隔离库。

---

## 四、冷启动、追问与核验

- **冷启动与追问**：新会话提供完整背景；同一判断的追问复用 `session_id` 并只补新事实。空闲 10 分钟后子进程退出，仍可凭已有 `session_id` 续接；MCP 重启、换模型或换档位时省略 `session_id` 开新会话。
- **拿到建议后**：在本地核实建议依赖的代码、日志、假设与测试，再决定是否采纳。同一判断最多追问 3 次；仍有核心分歧时停止循环，向用户呈现证据、分歧与待验证事实。

---

## 五、调用故障处置

- **只给过程描述不给判断**：补齐判据或目标，要求直接输出结论与理由。
- **空回复或权限相关失败**：由本地先核查文件/命令事实，改写成纯文本可判断的问题后省略旧 `session_id` 重试。
- **超时**：检查 `queue_seconds` 与 `elapsed_seconds` 区分排队还是执行耗时；项目复盘超时会保留待复盘状态，可安全用 `action: "review"` 重试；普通问答超时应省略旧 `session_id` 精简重问。
- **登录或配额错误**：直接向用户报告工具返回的具体原因。安装或迁移后先用原生 `~/.local/bin/agy --print-timeout 30s --print "Reply with exactly: OK"` 验证登录与无交互模式；真实顾问联调走已注册的 `agy_advise` MCP 工具，不要在 Codex 命令沙箱子 shell 里嵌套启动 `server.py`/`agy` 代替，避免凭据与运行环境差异造成误判。
- **OAuth `Code Challenge must be base64 encoded`**：检查复制到浏览器的授权 URL 是否混入换行、缩进或空格，只使用完整单行 URL。授权码仅粘贴到本机 CLI，不发送到聊天或其他工具。
- **超时预防**：常规问答或 `HOLD` 解封复查优先传 `effort="medium"` 且 `timeout_seconds >= 120`；重大停滞或换路复盘留 `180–240` 秒。
- 连续两次仍无有效判断时停止重试并向用户报告现象与处置记录。

---

## 六、调用审计日志与反馈迭代

每次 `agy_advise` 调用（无论普通问答还是 `project` 模式、成功还是超时/报错）都会以 `0o600` 权限自动追加一条结构化记录到与 `projects.sqlite3` 同目录的 `invocations.jsonl`（隔离测试设 `AGY_ADVISE_DB` 时自动随库隔离，不污染生产日志；日志写入失败不会影响主调用）。

- **隐私与开关**：默认只记元数据（`invocation_id`、模式、动作、`stance`、耗时、排队、token、模型、触发原因、裁决、状态）与短预览（question 前 240 字 / advice 前 320 字）；需要深度提示词复盘时设 `AGY_ADVISE_LOG_FULL=1`；如需关闭日志设 `AGY_ADVISE_LOG=off`；自定义路径设 `AGY_ADVISE_LOG=/path/to/invocations.jsonl`。
- **查看统计与调优提示**：
  ```bash
  python3 ~/.codex/skills/agy-advise/scripts/audit_report.py --days 14
  python3 ~/.codex/skills/agy-advise/scripts/audit_report.py summary --project robotics --json
  ```
- **事后质量打标（用于持续优化提示词与触发阈值）**：
  ```bash
  python3 ~/.codex/skills/agy-advise/scripts/audit_report.py feedback <invocation_id> <helpful|rubber_stamp|missed_root_cause|false_alarm|bad_format> --note "简短复盘原因"
  ```

基础传输与复核规范演化自[上游 dbc9ad2](https://github.com/fishing-dev-sm/pi-agy_advise/commit/dbc9ad2954797cd0a95ccfac4de6440839641de8)。
