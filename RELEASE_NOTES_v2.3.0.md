# CPA-X v2.3.0 — Docker 监控修复与界面重构

## 主要改动

- **Docker 对接修复**：Compose 透传上游地址和管理 / 模型密钥；模型接口、连接测试正确使用配置端口；面板设置保存到 `data/` 数据卷。
- **版本状态可信**：独立检查管理接口构建响应头与 GitHub Release。关闭自动升级不再关闭版本发现。开发构建、认证失败、连接失败与版本未确认分别显示，不用更新历史冒充当前版本。
- **权限边界明确**：容器和远程模式禁用宿主服务控制、二进制更新；不要求特权容器或 Docker socket。面板环境资源不会标为远程上游资源。
- **采集不再拖住页面**：状态、资源、日志与健康诊断读取后台快照；探测单飞、超时、退避、资源上限与快照过期提示。
- **日志长期运行保护**：尾读最多 1 MiB，显示行最多 16 KiB；增量扫描设置字节 / 时间预算，处理轮转和不完整尾行，未追赶完成时禁止自动更新。
- **全新运维界面**：实体色深浅主题、正常页面滚动、上游诊断卡、折叠诊断与模型列表；保留计价、语录、更新设置与只读配置。日志关键字筛选，离开底部暂停更新；轮询错峰，隐藏标签页暂停。

## 升级

备份 `.env.docker`（或 `.env`）与整个 `data/`，拉取新源码后重建：

```bash
docker compose --env-file .env.docker up -d --build
```

请比较新版 `.env.docker.example` 并补齐上游密钥、地址。**不要覆盖已有秘密文件或删除数据卷。** 非空环境变量在重启后优先于 UI 保存值；需要 UI 管理该设置时将对应环境变量留空。

如果上游在另一个容器中，请使用同一私有网络中的服务名，而不是面板容器内的 `127.0.0.1`。日志建议挂载目录以支持轮转。详见 [运维指南](OPERATIONS.md)。

## 验证范围

- 53 项 pytest 回归通过；GitHub Actions 在 Ubuntu / Windows、Python 3.11 / 3.13 上通过。
- 真实 Docker 镜像构建与隔离网络测试通过：认证、版本头、模型列表、禁用宿主操作、数据卷重建、上游中断时 200 次状态请求与恢复。
- 浏览器 1440 / 1024 / 820 / 390 / 320px 布局、日志筛选与滚动保持、配置只读与校验经过检查。
- 测试上游为模拟服务器；未在用户生产环境执行上游升级，也不将短时故障注入测试等同于多天稳定性保证。

## English

v2.3 fixes Docker upstream configuration/port propagation and persistent panel settings, separates version discovery from auto-upgrade, and makes remote/container capabilities explicit. Background snapshots keep polling responsive during outages; bounded log scanning and stale-state labels avoid unbounded work and misleading status. The dashboard now uses solid surfaces, normal document scrolling, local CSS/JS assets, and scroll-safe log filtering.

Back up configuration and `data/` before rebuilding with Compose. Nonempty environment variables override saved UI settings at restart. Container mode never controls the host or replaces the upstream binary. Validation includes four Python/OS CI combinations and real Docker networking/recreation/outage tests against a simulated upstream—not a production upgrade or multi-day reliability guarantee.
