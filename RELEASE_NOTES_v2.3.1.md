# CPA-X v2.3.1 — 子路径部署与收藏图标

- CSS、JavaScript、ICO / SVG 图标改为相对页面路径；API 基址从当前脚本 URL 推导，支持 `/cpax/` 等反代子路径，无需占用域名根 `/api/`。
- 收藏图标统一为新版页头的蓝色层叠标志；提供 16 / 32 / 48px ICO、SVG 与 180px Apple Touch Icon，并添加版本参数更新缓存。
- 反代需将 `/cpax/` 转发到面板 `/`，并将 `/cpax` 重定向到 `/cpax/`。升级时删除旧的 nginx `sub_filter` 样式注入，避免覆盖新版界面。

```nginx
location = /cpax { return 308 /cpax/; }
location /cpax/ {
    proxy_pass http://127.0.0.1:8081/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

图标可使用 `python scripts/build_icons.py` 重新生成（仅开发时需要 Pillow）。

English: Fix reverse-proxy subpath deployment by resolving assets relative to the page and API paths relative to the loaded script. Add coordinated SVG/ICO/Apple bookmark icons. Keep a trailing-slash redirect and remove legacy CSS injection when upgrading.
