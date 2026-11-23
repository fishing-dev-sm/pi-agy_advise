# agy-advise-mcp

`agy-advise-mcp` 是 [`pi-agy_advise`](../README.md) 的**可选 Python MCP 配套**。它与 Pi 的 TypeScript 扩展共享同一个本机原生 `agy` CLI，但面向 Codex 和其他 MCP 客户端，提供跨多轮项目的决策治理能力。

它不是 Pi TypeScript 扩展的替代品，也不会随 Pi 扩展自动加载。只使用轻量问答时，无需安装这里的 Python 依赖。

## 两种配套实现的分工

- **Pi TypeScript 扩展**：轻量问答、审查立场、会话控制、模型与推理档位覆盖。
- **`agy-advise-mcp`**：全局问答之外，增加持久化项目账本、数值或定性门槛、派发门禁、HOLD 解封处方、三层外置记忆和调用审计。

两种实现使用同一个 `agy_advise` 工具名，但不共享会话、SQLite 项目状态或审计日志。

## 功能

- 默认全局审查，也可使用 `red_team` 或 `tactical` 立场。
- 数值指标越高越好、越低越好，以及无分数的定性里程碑。
- 停滞、单机制空转、代理指标漂移、保护项回归、阻塞、预算耗尽和周期复核触发器。
- `CONTINUE` / `PIVOT` 行动预算，`HOLD` 强制解封动作与最小证据，激活前保持禁止派发。
- L1 宪法、L2 蒸馏事实与路线尸检、L3 近期窗口；每次复盘使用干净会话。
- `invocations.jsonl` 调用审计、`feedback.jsonl` 事后质量打标和本地汇总报表。

## 前置条件

- Python 3.10 或更高版本。
- 已安装并登录原生 [Antigravity CLI](https://antigravity.google/docs/cli/install/)。当前实现使用 Python 3.10 与 agy 1.5.15 验证。

先确认原生无交互调用可用：

```bash
~/.local/bin/agy --print-timeout 30s --print "Reply with exactly: OK"
```

## 安装

```bash
git clone https://github.com/fishing-dev-sm/pi-agy_advise.git
cd pi-agy_advise/mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Codex 的 MCP 配置示例（将两个绝对路径替换为本机路径）：

```toml
[mcp_servers.agy-advisor]
command = "/absolute/path/to/pi-agy_advise/mcp/.venv/bin/python"
args = ["/absolute/path/to/pi-agy_advise/mcp/scripts/server.py", "/home/USER/.local/bin/agy"]
tool_timeout_sec = 270
```

其他 MCP 客户端可使用相同的 stdio 命令和参数。修改配置后重启对应客户端或 MCP 会话。

如需安装调用规范 Skill，可将本目录链接到 Codex 的 Skill 目录；不要覆盖已有安装：

```bash
ln -s /absolute/path/to/pi-agy_advise/mcp ~/.codex/skills/agy-advise
```

## 验证

在本目录运行：

```bash
.venv/bin/python -m unittest discover -s scripts -p 'test_*.py' -v
```

测试使用假 CLI 和临时数据库，不需要真实模型调用，也不会写入正式项目账本。

## 数据与隐私

默认状态目录为 `~/.local/share/codex-agy-advise/`：

- `projects.sqlite3`：项目目标、证据、路线、裁决和三层记忆。
- `invocations.jsonl`：以 `0600` 权限写入的调用元数据与截断预览。
- `feedback.jsonl`：可选的人工质量反馈。

环境变量：

- `AGY_ADVISE_DB`：覆盖 SQLite 路径，测试时用于隔离状态。
- `AGY_ADVISE_LOG`：设为 `off` 关闭审计日志，或指定自定义 JSONL 路径。
- `AGY_ADVISE_LOG_FULL=1`：记录完整问答，仅供明确授权的调试使用。
- `AGY_ADVISE_MODEL`：设置默认模型；单次调用仍可覆盖。

只有显式提交的 `question`、`context` 和项目复盘证据会发送给 Google。顾问 CLI 在临时目录中运行，文件、命令、浏览器和 MCP 工具均被禁用。不要发送密钥、凭据、授权码或未授权的敏感内容。

## 审计与反馈

```bash
python3 scripts/audit_report.py --days 14
python3 scripts/audit_report.py --recent 10
python3 scripts/audit_report.py feedback <invocation_id> helpful --note "简短复盘原因"
```

完整参数、项目动作与故障处置见 [`SKILL.md`](SKILL.md)。

## 合规与许可证

本组件只调用本机单账号的官方 headless CLI，不代理、不转发、不多账号轮换，也不绕过任何限额。使用者仍需确认所在账号类型的服务条款。

代码与基础传输、复核规范演化自 [`pi-agy_advise@dbc9ad2`](https://github.com/fishing-dev-sm/pi-agy_advise/commit/dbc9ad2954797cd0a95ccfac4de6440839641de8)，并作为可选配套在同一仓库发布。许可证见根目录的 [MIT License](../LICENSE)。
