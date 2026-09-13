# CPA-X v2.3 升级审计

## 本次修复

- Compose 透传上游管理密钥、模型密钥、API 地址和挂载路径，并持久化 `data/panel_settings.json`；不挂载 Docker socket。
- Docker / 远程模式不再探测或伪造宿主 systemd 状态；服务控制与二进制升级明确禁用。
- 版本发现独立于自动升级开关，读取 CLIProxyAPI 管理响应头中的 `X-CPA-VERSION`、`X-CPA-COMMIT`、`X-CPA-BUILD-DATE`；连接失败、认证失败、开发构建、版本未确认分别展示。
- 版本探测加单飞锁、超时、退避、响应体上限和凭据变更后的响应丢弃；不会用历史更新记录冒充当前版本。
- 日志尾读上限 1 MiB、单行上限 16 KiB；增量扫描每轮最多 4 MiB / 50 ms，并识别旋转、追赶和不完整尾行。
- 状态、资源、日志和健康检查由后台快照采集，HTTP 请求不再直接执行慢采集；快照带采集时间和过期标记。
- 后台线程使用 shutdown event；Waitress 设置连接数、请求体和超时上限；Docker 设置持久化到数据卷。
- 前端拆出 `dashboard.css` / `dashboard.js`：取消玻璃模糊与浮动背景，改为正常文档滚动；日志仅在内容改变时重绘最多 80 条，支持关键字过滤，用户离开底部时暂停显示更新而保留最新缓存；轮询错峰并在隐藏页面暂停。
- 页面新增上游连接诊断、部署能力边界、资源范围和版本来源提示，避免把面板环境资源或远程服务误标为宿主状态。

## 验证

- `pytest -q`：53 项通过（Windows / Python 3.12）。
- `node --check static/dashboard.js`：通过。
- `ruff check --select E9,F63,F7,F82 app.py scripts tests`：通过。
- `bandit -q -ll -ii app.py`：通过。
- Compose YAML：使用 PyYAML 解析通过。
- 本地模拟上游：已验证版本响应头、开发构建、断网、HTTP 401/403/404/502、凭据切换、后台快照和浏览器桌面页面。
- 浏览器：1440 / 1024 / 820 / 390 / 320px 无横向溢出；正常文档滚动；离开日志底部刷新不改变已有文本或滚动位置；关键字筛选准确；模型读取成功，无 pageerror。
- 联调修复了模型接口和连接测试遗漏独立配置端口的问题。
- 本机没有 Docker CLI；新增 `tests/container_smoke.py` 和 CI 容器验收，尚待远端执行。`tests/preview_server.py` 只提供本地浏览器模拟，不替代 Docker 测试。
- 当前仍处于候选版本验收，未宣布生产部署成功。

## 已知边界

- 上游只返回 `dev` 时，面板显示开发构建，不会猜测 Release 版本。
- Docker 面板只能控制容器内自己的文件和资源；要升级 CLIProxyAPI 镜像，应在上游部署项目执行。
- 模拟测试不能证明生产环境长期无故障；真实上游升级与生产 Compose 部署需要目标环境和对应凭据。
