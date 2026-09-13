# Changelog / 更新历史

CPA-X follows semantic versioning. Detailed notes and downloadable packages are published on the [GitHub Releases](https://github.com/ferretgeek/cliproxyapi-dashboard/releases) page.

CPA-X 遵循语义化版本。详细说明和可下载发行包统一发布在 GitHub Releases 页面。

## Unreleased

- v2.3 reliability pass: Docker configuration propagation and persistent panel settings; bounded background snapshots; explicit remote/container capability boundaries; version discovery from CLIProxyAPI build headers; log scan budgets and rotation handling; and a no-blur responsive dashboard with scroll-safe incremental log rendering.
- v2.3 稳定性升级：补齐 Docker 配置透传与面板设置持久化；后台快照与资源边界；读取 CLIProxyAPI 构建响应头；日志扫描预算与轮转处理；以及取消昂贵模糊效果、支持正常滚动和增量日志渲染的新面板。

- Raise the development test floor to `pytest 9.0.3` and bootstrap `pip 26.1.2+` so fresh local and CI environments avoid currently known toolchain vulnerabilities.
- 将开发测试下限提升到 `pytest 9.0.3`，并在本地与 CI 安装流程中先升级到 `pip 26.1.2+`，避免已知工具链漏洞。
- Harden loopback Host validation and browser mutation checks, move one-time panel-key setup from query strings to URL fragments, and stage Linux systemd runtime files in a root-owned release directory.
- 加固回环 Host 与浏览器修改请求校验，把一次性面板密钥从查询参数迁移到 URL fragment，并让 Linux systemd 只运行 root 所有的发布快照。

## [2.2.1] - 2026-07-27

Production repair release for intermittent `502` amplification: real management-endpoint update health checks, durable failed-version backoff, anonymous GitHub release fallback, and removal of deprecated background usage polling.

修复间歇 `502` 被更新流程放大的长期运行问题：更新后验证真实管理接口、失败版本持久退避、匿名 GitHub Release 稳定回退，并停止废弃 usage 后台轮询。

- [Release](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v2.2.1)
- [Full notes](RELEASE_NOTES_v2.2.1.md)
- [Compare v2.2.0...v2.2.1](https://github.com/ferretgeek/cliproxyapi-dashboard/compare/v2.2.0...v2.2.1)

## [2.2.0] - 2026-07-22

Comprehensive repair release covering cross-time-zone log handling, transactional auto-update, bounded backups, v6/v7 usage accounting, atomic persistence, performance, security hardening, and a complete responsive UI redesign.

完整修复跨时区日志、事务式自动更新、备份膨胀、v6/v7 用量累计、原子持久化、性能与响应式界面。

- [Release](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v2.2.0)
- [Full notes](RELEASE_NOTES_v2.2.0.md)
- [Compare v2.1.2...v2.2.0](https://github.com/ferretgeek/cliproxyapi-dashboard/compare/v2.1.2...v2.2.0)

## [2.1.2] - 2026-05-26

- [Release](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v2.1.2)
- [Full notes](RELEASE_NOTES_v2.1.2.md)

## [2.1.1] - 2026-03-18

- [Release](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v2.1.1)
- [Full notes](RELEASE_NOTES_v2.1.1.md)

## Earlier releases / 更早版本

- [v2.1.0](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v2.1.0)
- [v2.0.0](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v2.0.0)
- [v1.0.0](https://github.com/ferretgeek/cliproxyapi-dashboard/releases/tag/v1.0.0)
