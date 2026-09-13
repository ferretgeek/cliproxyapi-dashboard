# CLIProxyAPI 管理面板

中文 · [English](README_EN.md)

在浏览器里查看 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的运行状态和请求日志，并管理服务升级。CPA-X 是它的配套管理工具。

**使用前提：** 已部署 CLIProxyAPI，需要 Python 3.11+ 和管理接口密钥。服务控制与自动升级需要 Linux + systemd；Windows、Docker 主要用于监控和查看。

[开始安装](#三步跑起来) · [部署与运维](OPERATIONS.md) · [查看界面](#界面)

实时请求数来自日志；Token 和成本数据来自已有的本地历史快照，新版 CLIProxyAPI 不再提供旧的用量接口。v2.3 面板使用后台快照采集，Docker / 远程模式会明确显示权限边界，不会假装控制宿主机。

## 界面

| 深色 | 浅色 |
|---|---|
| ![深色主题](docs/images/preview-dark.png) | ![浅色主题](docs/images/preview-light.png) |

<p align="center">
  <img src="docs/images/preview-mobile.png" alt="手机端布局" width="320" />
</p>

## 它能做什么

- **看状态** — 服务是否在跑、CPU / 内存 / 磁盘、上游版本与本地版本、各账号可用性。
- **看用量和花费** — 实时请求量来自日志；已有的 Token 消耗与成本历史从本地兼容快照读取，支持按模型和账号查看。估算单价可从 OpenRouter 自动同步，也可以关闭同步后手动填写。
- **看日志** — 增量解析、按级别与关键字筛选，不用 SSH 进去 `tail`。
- **管升级** — 检查新版本、下载、校验、替换、重启、健康确认，失败自动回滚。
- **管服务** — Linux 上直接启停重启 systemd 服务（Windows 与 Docker 下这部分会明确禁用，而不是假装成功）。
- **看配置** — 配置区默认只读、只做校验。回写主配置需要你自己开开关。
- **换主题** — 天空、薄荷、玫瑰、沙色四套浅色配色 + `#17191d` 深灰暗色，右上角切换并记住选择；手机上是完整可用而不只是能打开。

## 三步跑起来

需要 Python 3.11+，以及能访问 CLIProxyAPI 的管理接口（默认 `http://127.0.0.1:8317`）。**推荐 Linux**——服务控制和自动升级依赖 systemd。

```bash
# Linux：一条命令装好并注册 systemd 服务
bash scripts/install.sh

# 让它自己探测 CLIProxyAPI 的目录、日志和端口，写进 .env
python3 scripts/doctor.py --write-env
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

然后打开 `http://127.0.0.1:8080`。

手动安装、Docker Compose、反向代理、全部环境变量与常见问题，见 [部署与运维手册](OPERATIONS.md)。

<details>
<summary>手动安装（不想用脚本的话）</summary>

<br />

```bash
git clone https://github.com/ferretgeek/cliproxyapi-dashboard.git
cd cliproxyapi-dashboard

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade "pip>=26.1.2"
python -m pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
# 编辑 .env，至少填对 CLIProxyAPI 的目录、日志路径和管理接口地址

python app.py
```

关键变量（完整列表在 `.env.example` 里都有注释）：

| 变量 | 作用 |
|---|---|
| `CLIPROXY_PANEL_CLIPROXY_DIR` / `_CONFIG` / `_LOG` | CLIProxyAPI 的安装目录、配置和日志在哪 |
| `CLIPROXY_PANEL_CLIPROXY_API_BASE` / `_API_PORT` | 管理接口地址与端口 |
| `CLIPROXY_PANEL_MANAGEMENT_KEY` / `_MODELS_API_KEY` | 上游启用了鉴权时需要 |
| `CLIPROXY_PANEL_CLIPROXY_SERVICE` / `_BINARY` | 自动升级必须知道服务名和二进制路径 |
| `CLIPROXY_PANEL_LOG_TIMEZONE` | 默认 `auto`，也接受 `UTC`、`+08:00` 或 `Asia/Shanghai` |
| `CLIPROXY_PANEL_PANEL_ACCESS_KEY` | 非回环监听时**必填**，至少 32 位 |
| `CLIPROXY_PANEL_CONFIG_WRITE_ENABLED` | 默认 `false`；只有你明确接受风险才打开主配置回写 |

</details>

<details>
<summary>Docker（适合只做监控和只读运维）</summary>

<br />

容器里一般没有 systemd 和宿主权限，所以**自动升级和服务控制在 Docker 下不可用**；状态、统计、模型、日志和配置读取都正常。Compose 会透传管理密钥与 API 地址，并把 `data/` 持久化。版本探测仍然工作；若上游镜像只返回 `dev`，面板会如实显示开发构建。

```bash
cp .env.docker.example .env.docker
# 给 CLIPROXY_PANEL_PANEL_ACCESS_KEY 生成一个至少 32 位的随机值
docker compose --env-file .env.docker up -d --build
```

Compose 默认只把端口发布在 `127.0.0.1`，访问密钥为空时直接拒绝启动。远程访问请保持回环发布，前面挂一层 TLS 反向代理，不要把容器端口直接暴露到公网。

</details>

## 技术上值得一提的地方

**升级不会假成功。** 这是这个项目最花心思的一块，起因是一次间歇性 `502` 生产故障。现在的流程是：在线准备 → 校验 SHA-256 → 原子替换 → 重启 → **真的去打一次带认证的管理接口，拿到 HTTP 200 才算成功**。失败的版本会进入持久化的指数退避（重启面板也不会忘），并回滚到上一个可用版本。匿名的 GitHub 版本检查优先走稳定的 Release 跳转，避免频繁触发速率限制。

**日志是增量解析的。** 不重读整个文件，长期运行下内存和 CPU 都是平的；状态持久化走原子写入，上游不可用时退避重试，备份按数量、时长和体积三重上限自动淘汰。

**时区是推断出来的，不是猜的。** CLIProxyAPI 的日志时间可能不带偏移量，宿主机和容器时区还可能不一致。面板会反推出正确的时刻，所有 API 统一输出 UTC / RFC 3339，界面再按你设定的时区渲染。

**默认关掉危险入口。** 前端所有导出入口已移除（避免通过浏览器下载链接泄露敏感数据）；主配置回写默认关闭；跨域访问默认禁止；回环模式只接受字面 localhost / 回环 Host 并拒绝跨站变更请求；Linux 安装器从 root 所有的 `/opt/cliproxy-panel/releases/` 快照运行 systemd 服务。

**顺手做了给 AI Agent 的部署手册。** [`AI_DEPLOY_CN.md`](AI_DEPLOY_CN.md) 和 [`AGENTS.md`](AGENTS.md) 是写给编码助手看的：目录约定、环境变量、验证命令和失败处理都写成了可执行步骤，你可以直接让 Claude Code / Codex 照着装。人工部署照上面的三步走就行，两条路都完整。

## 它不做什么

- 不是 CLIProxyAPI 的替代品或分发渠道——你得先自己装好 CLIProxyAPI。
- 不代理、不改写、不记录你的模型请求内容。它只读日志和管理接口。
- 不帮你注册账号、不绕过任何额度限制。
- Windows 和 Docker 下没有服务控制与自动升级（依赖 systemd）。这一点在界面里会明确禁用，不会静默失败。

## 常见问题

**页面打开了但没数据。** 先确认 CLIProxyAPI 在跑，再检查 `.env` 里的 `CLIPROXY_PANEL_CLIPROXY_API_BASE` / `_PORT` 指向对不对。

> 新版 CLIProxyAPI 已经不再提供旧的 usage 管理接口。CPA-X 不再后台轮询它——实时请求量来自增量日志解析，升级前已存下的 token / 成本历史仍可从本地兼容快照读取，不会反复向上游发 `404`。

**健康检查超时。** 容器和负载均衡的存活探测请用不需要鉴权的 `/api/healthz`；`/api/health` 是完整诊断，比较重。

**systemd 相关功能没反应。** 那是 Linux 专属能力，Windows 上会优雅失败，不影响面板启动。

## 安全须知

- **不要把 `.env` 提交进仓库**（已在 `.gitignore` 中）。管理密钥和模型密钥只放 `.env`。
- 默认监听 `127.0.0.1`。任何非回环监听都必须同时设置至少 32 位的 `CLIPROXY_PANEL_PANEL_ACCESS_KEY`，否则进程拒绝启动。
- 配了访问密钥后，`/api/*` 只接受 `X-Panel-Key` 请求头；浏览器首次配置走不会被传输的 `#panel_key=...` fragment，从不使用查询参数。
- 公网部署请保持回环监听 + TLS 反向代理。

## 开发

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
ruff check --select E9,F63,F7,F82 app.py scripts tests
```

## 更多文档

[部署与运维](OPERATIONS.md) · [版本变更](CHANGELOG.md) · [参与开发](CONTRIBUTING.md) · [安全策略](SECURITY.md) · [获取支持](SUPPORT.md) · [行为准则](CODE_OF_CONDUCT.md) · [提交问题](https://github.com/ferretgeek/cliproxyapi-dashboard/issues/new/choose) · [讨论区](https://github.com/ferretgeek/cliproxyapi-dashboard/discussions)

## 许可与声明

MIT License，见 [`LICENSE`](LICENSE)。

这是独立的社区项目，与 OpenAI、Anthropic、Google 和 CLIProxyAPI 上游项目均无隶属、授权或背书关系，也不绕过任何额度限制。相关商标归其权利人所有。
