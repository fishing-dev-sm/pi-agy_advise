# pi-agy_advise

把本机的 [Antigravity CLI](https://antigravity.google/docs/cli)（`agy`）接成一个 Pi 工具：**`agy_advise`**。
Pi 在需要方案、设计意见或第二次审查时问它一句，agy 在自己的会话里读代码、给建议，把文本返回给 Pi。

以执行模式默认值提供规划和审查建议（不传 `--mode`）。文件和命令访问由 Antigravity 的权限设置控制。

> A Pi extension that exposes the local `agy` (Antigravity CLI) as a planning advisor.

## 为什么不是一个简单的 `agy -p`

每次都 `agy -p` 等于每轮从零开始：重新读文件、重新建立上下文、重复计费。本扩展改成**一个长驻的 `agy` 进程**讲 stream-json：

```
Pi ──agy_advise(question)──▶ 长驻 agy 进程（默认模式, stream-json）
                                  │  自己维护会话与 prompt cache
   ◀──────── 建议文本 ─────────────┘
```

- Pi 侧只发 `question`（可选一条 `context`），**不重传 Pi 的对话历史**。历史在 agy 那边。
- 连续提问复用同一个 `conversation_id`，agy 的 prompt cache 因此持续命中。实测同一会话第二轮：`cache_read_tokens: 32973 / total_tokens: 36090`（约 91% 命中）。
- 请求串行化，不会把 agy 的 stdin 写乱。
- 每轮成功后开始计算空闲时间，空闲满 10 分钟回收进程。切换工作目录、传 `freshSession: true`、切换 `model`/`effort`、空闲回收、失败、超时或进程退出后，下次调用会创建新会话。
- 在同一目录里只切换权限时，扩展会重启活跃进程并沿用会话 ID。

## 安装

需要本机已安装并登录 `agy`（`agy --version` 能出结果即可）。

**方式一：放进全局扩展目录**

```bash
git clone https://github.com/fishing-dev-sm/pi-agy_advise
cp pi-agy_advise/index.ts ~/.pi/agent/extensions/
```

然后在 Pi 里 `/reload`。

**方式二：作为 Pi 包安装**

```bash
pi install git:github.com/fishing-dev-sm/pi-agy_advise
```

## 前置：把 agy 的权限开关打开

headless 模式没法弹确认框。只要有一条命令需要批准，`agy` 就会取消整轮，返回状态为 `SUCCESS` 的空回复。扩展把空回复当错误抛出，并附上 stderr 诊断。

维护逐条 `allow` 白名单是错的方向：每换一个任务就要重新扫一遍命令，漏一条就是一次空响应。改用一个总开关加一份危险操作黑名单：

```json
{
  "toolPermission": "always-proceed",
  "permissions": {
    "deny": [
      "command(rm)", "command(sudo)", "command(dd)",
      "command(chmod)", "command(chown)", "command(mkfs)", "command(shutdown)"
    ]
  }
}
```

写进 `~/.gemini/antigravity-cli/settings.json`。官方对 `always-proceed` 的定义是：终端命令、网页请求、MCP 工具都不询问，**但命中 ask 或 deny 规则的仍然拦**。所以黑名单在每种模式下都生效（官方 [settings](https://antigravity.google/docs/settings)、[Agent permissions](https://antigravity.google/docs/permissions)）。

三个必须知道的边界：

1. `toolPermission` 是 **global** 的，你手工用 `agy` 时它也不再询问写文件和跑命令。
2. 它不管范围：`objdump /any/path` 一样放行；范围靠工作区之外默认不可访问（`allowNonWorkspaceAccess: false`）。
3. `deny` 只能拦首 token：`bash -c "rm -rf x"` 首 token 是 `bash`，不会命中 `command(rm)`。要硬隔离请开 `enableTerminalSandbox`，并改用 `proceed-in-sandbox`——但 Linux 上内核对 unprivileged user namespace 的限制会让它退化成 `request-review`，先验证再用。

不想全局放开，可以保留默认的 `request-review`，但那就得回到逐条 `allow`，并接受漏列即空响应。

## 用法

参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `question` | ✅ | 目标、相关失败现象和开放问题。首轮应自包含，同一活跃会话的追问可增量提供 |
| `context` | — | agy 看不到、但回答必需的最小事实。省略可省钱 |
| `stance` | — | 审查立场：`global`（默认，先审前提再落到细节）、`red_team`（反方红队审计，找最可能的失败点与最便宜证伪检查）、`tactical`（大方向已锁定时的聚焦实现建议） |
| `freshSession` | — | 设为 `true` 时丢弃活跃会话、发起无菌室新会话，适合换主题或需要无沉没成本偏见的独立复盘 |
| `model` | — | 可选模型覆盖（未传时读取 `AGY_ADVISE_MODEL` 或沿用 CLI 默认）；切换模型会新开会话 |
| `effort` | — | 可选推理档位（`low`/`medium`/`high`/`xhigh`/`max`）；切换档位会新开会话 |
| `timeoutMs` | — | 单次问答超时（含排队等待），默认 240000 |
| `skipPermissions` | — | 传 `--dangerously-skip-permissions`。仅当 `agy` 侧没有放开 `toolPermission` 时需要，仍需用户明确授权本次调用 |

在 Pi 里直接说人话即可，Pi 会自己决定何时调用，例如：

```
用 agy_advise 看下 /path/to/src/reconcile.py 的分层是否合理
```

返回内容是一段建议文本，同时在 `details` 里带回 `conversationId`、`seconds`、`queueSeconds`、`elapsedSeconds`、`model`、`stance`、`freshSession` 和 `usage`。

## 怎么问才不浪费

安装扩展时请同时安装配套的 [`agy-advise` skill](skills/agy-advise/SKILL.md)。它包含提问、验证建议和故障排查的完整用法。

skill 随本仓库发布在 `skills/agy-advise/SKILL.md`：

- 用 `pi install` 装包时，它由 `package.json` 的 `pi.skills` 一起装入，不用管
- 手动拷贝 `index.ts` 的话，记得同样拷一份：

  ```bash
  mkdir -p ~/.agents/skills/agy-advise
  cp skills/agy-advise/SKILL.md ~/.agents/skills/agy-advise/
  ```

装完后 `/reload`，技能名就是 `agy-advise`；需要强制时用 `/skill:agy-advise`。

给 agy 一个开放问题，让它根据目标、约束和已观察到的现象找方案。

- 不要先把问题坍缩成 A/B/C 再让它选。给目标、约束、已尝试、失败现象，让它自己划定方案空间。
- 不要先给结论再问“对不对”。那只会得到一个 yes。
- 不要把 Pi 自己的推理链粘进 `context`。只给 agy 看不到的事实。
- 事实类问题（某个函数有没有加锁）自己 `grep`，别花 100 秒让它重读。

另外，扩展会在每次提问末尾追加一句固定要求：若问题里已带候选方案，不要简单挑一个，而是自己推导最佳解并说明理由。这是对“被喂选择题”的对冲，不是给 agy 加人设。

## 行为与已知限制

- **不传 `--mode`，更不用 `--mode plan`**。plan 模式会让 agy 先写 `implementation_plan.md` 再等复核；headless 无法回应复核，回合就停在「please approve」上，顾问永远交不出答案。不传时 CLI 用它自己的 headless 默认值（v1.5.7+ 是 `auto`，不可用则 `default`）。
- **别显式传 `--mode auto`**。本机账号实测返回 `warning: --mode auto is not available for this account; continuing in the default mode.`（该模式排除企业与商业账号），每次启动都多一行 stderr 噪声。
- 写保护靠权限设置（把 `write_file(*)` 加进 `deny`），不靠模式：`default` 模式的交互式 diff 复核在 headless 里没人能批，效果是写文件动作被 soft-deny。
- 任何需要批准但拿不到的工具动作都会被 soft-deny：整轮取消，退出码仍是 0。扩展把空 response 当错误抛出并附 stderr。
- **会话内不能发斜杠命令**。`/model`、`/usage` 之类在 stream-json 会话里会直接报错退出（exit 2）。
- **成本**：一次多文件分析实测约 12 万 token 以内、100 秒上下；简单问答 20–40 秒。agy 会话独立于 Pi，第一条问题务必自包含。
- **`agy` 的输出不会流式显示**，工具返回时一次性给出完整文本。

## 合规

本项目只做**本机单账号**的 CLI 调用：起 `agy` 子进程、读它的 stdout，不代理、不转发、不对外提供接口、不多账号轮换、不绕过任何限额。

Google 的 [Antigravity 服务条款](https://antigravity.google/terms)禁止把服务"用于非我们提供的产品"以及用第三方软件访问服务（例如用 OpenClaw 走 Antigravity OAuth）。用第三方 harness 驱动官方 headless CLI 属于该 CLI 的既有用途（`--output-format text` 就是默认格式），但把 `agy` 变成他人可用的后端服务不属于。使用时请自行确认符合你所在账号类型的条款。

另请注意官方对数据的说明：交互内容会被记录并可能被 Google 员工与承包商查看。不要把机密信息喂给它。

## 可选配套：agy-advise-mcp

需要跨多轮项目治理时，可以使用 [`mcp/`](mcp/README.md) 中的 **agy-advise-mcp**：一个独立的 Python MCP 服务，提供项目账本、派发门禁、HOLD 解封处方、三层外置记忆和调用审计。

它是可选配套，不会随 Pi TypeScript 扩展自动加载；只使用本扩展的轻量问答时，不需要安装 Python 依赖。

## License

MIT
