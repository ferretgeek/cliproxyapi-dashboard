# CPA-X 运维指南 / Operations Guide

## 架构 / Architecture

CPA-X 是一个 Flask 管理面板：浏览器只连接 CPA-X，CPA-X 再读取 CLIProxyAPI 管理接口、日志、systemd 状态和本地配置。运行状态以 CLIProxyAPI 为事实源；面板自己的兼容快照、增量日志统计和更新状态保存在仓库或部署目录下的 `data/`。`.env` 保存部署路径与可选密钥，必须留在目标机器并限制读取权限。

CPA-X is a Flask dashboard. The browser talks only to CPA-X, while CPA-X reads the CLIProxyAPI management API, logs, systemd state, and local configuration. CLIProxyAPI remains the operational source of truth; the dashboard's compatibility snapshots, incremental log state, and updater state live under `data/`. `.env` contains deployment paths and optional secrets and must remain access-restricted on the target machine.

本地模式默认监听 `127.0.0.1:8080`。服务器模式可经反向代理提供，但任何非回环监听都必须设置至少 32 字符的 `CLIPROXY_PANEL_PANEL_ACCESS_KEY`；反向代理还应终止 TLS、限制来源和请求速率，并只把应用端口暴露给代理。不要直接公开 CLIProxyAPI 管理端口。Docker Compose 默认也只映射到宿主回环地址，显式远程部署时才修改。

Local mode binds to `127.0.0.1:8080`. A server deployment may sit behind a reverse proxy, but every non-loopback bind requires a `CLIPROXY_PANEL_PANEL_ACCESS_KEY` of at least 32 characters. Terminate TLS, restrict sources and request rate, expose the app port only to the proxy, and never publish the CLIProxyAPI management port directly. Docker Compose also maps to host loopback by default.

## 安装与升级 / Install and upgrade

- 本机开发或 Windows 使用 README 的虚拟环境命令；Linux 长期运行优先 `scripts/install.sh` + systemd，Windows 使用 `scripts/install.ps1`；容器使用 `docker-compose.yml`。
- 首次启动前从 `.env.example` 创建 `.env`，或运行 `python scripts/doctor.py --write-env` 探测非秘密路径。doctor 不会生成管理密钥、模型 Key 或面板访问密钥。
- 升级前按下一节备份，停止 CPA-X 进程/服务，取得已验证的新源码并重新安装锁定依赖，再启动。不要覆盖 `.env` 和 `data/`，也不要把示例值当生产配置。
- 升级后同时验证 `/api/healthz`、带认证的 `/api/health`、概览统计、日志增量和一次服务状态读取。启用自动更新时，成功必须同时满足 systemd active 与带认证真实管理接口 HTTP 200；失败版本会退避并从二进制备份回滚。

- Use the README's virtual environment for development or Windows, `scripts/install.sh` plus systemd for long-running Linux, `scripts/install.ps1` on Windows, or `docker-compose.yml` for containers.
- Create `.env` from the example or run `python scripts/doctor.py --write-env` for non-secret path discovery. Doctor never invents management keys, model keys, or panel access keys.
- Before upgrade, back up as below, stop the process/service, obtain verified source, reinstall pinned dependencies, and restart. Never overwrite `.env` or `data/`, and never treat examples as production values.
- Verify `/api/healthz`, authenticated `/api/health`, overview statistics, incremental logs, and service status. Auto-update succeeds only after systemd is active and the authenticated real management endpoint returns HTTP 200; failed versions back off and restore the binary backup.

## Docker v2.3 部署与验收 / Container verification

```bash
cp .env.docker.example .env.docker
# 编辑 .env.docker：填入至少 32 字符面板密钥、上游明文管理密钥及地址。
# 上游为另一个容器时，使用同一网络的服务名，不要使用容器内的 127.0.0.1。
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker up -d --build
curl --fail http://127.0.0.1:8080/api/healthz
```

- 上游管理接口要允许来自面板的访问（例如 `remote-management.allow-remote: true`）；用防火墙 / Docker 私有网络限制来源，不要直接公开管理端口。`host.docker.internal` 无法访问只监听宿主 `127.0.0.1` 的服务，应在受限网络上配置可达地址。
- API 基址不带端口时使用 `_CLIPROXY_API_PORT`；显式 URL 端口优先。HTTPS 反代可填写 `https://example.internal:443`，路径前缀会保留。
- `data/panel_settings.json` 保存面板设置和可能的明文管理密钥，权限为 0600；数据卷和备份应按秘密文件管理。非空环境变量在重启后覆盖 UI 保存值；需要由 UI 管理密钥 / 计价开关时，将对应环境变量留空。
- 日志和上游 YAML 需要可读挂载。建议挂载整个日志目录并将 `_CLIPROXY_LOG` 指向其中的 `main.log`，避免单文件挂载在日志轮转后停留在旧 inode。
- `unknown` 是未确认；`dev` 是上游未注入 Release 标签的开发构建；认证失败、不可达和 HTTP 错误单独显示。关闭自动升级不会停止版本检查。
- 状态 / 资源约每 5 秒后台采集，完整健康诊断每 60 秒采集；读取 API 不触发即时慢检查。启动时允许短暂 `checking`，超过新鲜度期限会显示过期状态。
- Docker 仅监控。面板禁止启停宿主服务和替换上游二进制；升级上游镜像请在其部署目录执行。资源采样属于面板运行环境（某些容器指标可反映宿主机），不是远程上游资源。

For container deployments, use the Compose command above, provide reachable upstream networking and its **plaintext** management key, and protect the data volume. Nonempty environment variables override UI-persisted settings after restart. Mount the upstream log **directory** to observe file rotation. `dev` is a development build, not an invented release; discovery continues with auto-upgrade disabled. Status APIs serve background snapshots; diagnostics may lag by one minute. Container mode never controls the host or upgrades the upstream image.

开发验收（不接触真实上游） / Developer smoke test:

```bash
docker build -t cpax-panel:ci .
python tests/container_smoke.py
```

该测试使用真实 Docker 网络和数据卷，但上游是测试服务器；覆盖版本头、模型接口、认证、禁用宿主操作、重建保留设置、上游中断时 200 次并发读取及恢复。不能替代真实生产升级或长期压力测试。

## 备份与恢复 / Backup and restore

1. 停止 CPA-X，避免复制到一半的原子状态文件。
2. 以受限权限备份 `.env` 和整个 `data/`。如果面板被授权写回 CLIProxyAPI 配置，还要用 CLIProxyAPI 自己的备份流程保护 `CLIPROXY_PANEL_CLIPROXY_CONFIG`、认证目录和业务数据；不要假设 CPA-X 的 `data/` 包含它们。
3. 二进制自动更新生成的 `.bak.<时间>` 只用于短期回滚，并受数量、天数和容量上限约束，不能替代独立备份。
4. 恢复到同版本或兼容的新版本目录，先放回 `.env` 和 `data/`、收紧文件权限，再启动并执行健康检查。若只丢失 `data/`，统计兼容历史可能不可恢复，但 CLIProxyAPI 本身不应受影响。

1. Stop CPA-X so no atomic state file is copied mid-replacement.
2. Back up `.env` and all of `data/` with restricted access. If configuration writeback was explicitly enabled, protect `CLIPROXY_PANEL_CLIPROXY_CONFIG`, the authentication directory, and operational data through CLIProxyAPI's own backup process; CPA-X `data/` does not contain them.
3. `.bak.<timestamp>` files created by binary auto-update are short-term rollback points subject to count, age, and size caps—not independent backups.
4. Restore into the same or a compatible release, put `.env` and `data/` back before startup, tighten permissions, and run health checks. Losing only `data/` may lose compatibility history but should not damage CLIProxyAPI itself.

## 健康检查与故障 / Health and troubleshooting

- 无认证存活探针：`GET /api/healthz`，只返回最小信息；完整诊断使用带 `X-Panel-Key` 的 `GET /api/health`。
- 页面为空：检查 CLIProxyAPI 地址、端口、管理密钥、日志路径和文件权限。旧 usage 管理接口已经移除，实时数量来自日志增量统计。
- 容器：确认绑定仍为回环或受保护的代理网络、`.env`/数据卷已挂载、健康检查通过，重建容器后 `data/` 保持。
- systemd 操作在 Windows 不可用是平台边界，不影响面板本身；Windows 用进程管理和面板健康接口验证。
- 更新失败：阅读脱敏后的更新历史，确认 checksum、服务名、二进制路径和真实管理接口；不要关闭校验或缩短健康门禁来强行通过。

- `GET /api/healthz` is the unauthenticated minimal liveness probe; use authenticated `GET /api/health` for full diagnostics.
- Empty UI: check CLIProxyAPI address/port, management key, log path, and permissions. Current live totals come from incremental logs rather than removed legacy usage endpoints.
- Containers: keep loopback/protected proxy-network exposure, mount configuration/data, pass health checks, and verify `data/` survives recreation.
- systemd actions are unavailable on Windows by design; validate the Windows process and panel health instead.
- Failed update: inspect redacted update history and verify checksum, service, binary path, and real management endpoint. Never disable verification or weaken the health gate to force success.

## 卸载 / Uninstall

先停止并禁用对应 systemd/进程或移除 Compose 服务，保存所需的 `.env`、`data/` 与 CLIProxyAPI 独立备份，再删除 CPA-X 程序目录和虚拟环境。确认没有其他服务引用该路径后，才删除 systemd 单元和反向代理站点。卸载 CPA-X 不应删除 CLIProxyAPI 二进制、配置、认证目录、日志或业务数据；这些属于另一个产品，只有明确决定同时卸载时才按其文档处理。

Stop and disable the related systemd/process or remove the Compose service. Retain the required `.env`, `data/`, and separate CLIProxyAPI backup before deleting CPA-X and its virtual environment. Remove systemd and reverse-proxy entries only after confirming no other service uses them. Uninstalling CPA-X must not delete CLIProxyAPI binaries, configuration, authentication directory, logs, or operational data unless that separate product is also explicitly being removed.
