
        // Resolve against this script, not the domain root: works at / and /cpax/.
        const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
        const inflight = {
            refreshStatus: false,
            refreshResources: false,
            refreshCliLogs: false,
            loadModelsFromAPI: false,
            runHealthCheck: false,
            refreshUpdateHistory: false,
            saveManagementKey: false
        };
        let pricingDirty = false;
        let pricingInitialized = false;
        let updateSettingsDirty = false;
        let configWriteEnabled = false;
        let logDisplayClearedAt = 0;
        let lastStatusKey = '';
        let lastLogsKey = '';
        let cachedLogs = [];
        let capabilities = {};
        let followLogs = true;
        let managementFormOpen = false;

        function setHtml(element, html) {
            if (element.innerHTML !== html) element.innerHTML = html;
        }

        function setPanelKey() {
            const value = prompt('输入面板访问密钥（仅保存在当前浏览器）');
            if (!value?.trim()) return;
            localStorage.setItem('panel_key', value.trim());
            document.getElementById('panel-auth-banner').classList.remove('show');
            refreshAll();
        }

        function showManagementKeyForm() {
            managementFormOpen = true;
            document.getElementById('management-key-banner').classList.add('show');
            document.getElementById('management-key-input').focus();
        }

        function renderConnection(d) {
            capabilities = d.capabilities || {};
            const up = d.upstream || {};
            const collection = d.collection || {};
            const stale = collection.stale || !collection.at;
            const stateLabels = {running:'管理接口可用',auth_required:'需要管理认证',not_found:'管理接口未开放',upstream_error:'上游接口异常',unreachable:'上游无法连接',checking:'正在检查',unknown:'状态未知',stopped:'已停止'};
            const status = stale ? 'unknown' : d.service?.status;
            const label = stateLabels[status] || status || '状态未知';
            const kind = status === 'running' ? 'success' : ['unreachable','upstream_error','stopped'].includes(status) ? 'danger' : 'warning';
            document.getElementById('connection-message').textContent = up.message || '等待上游连接检查';
            document.getElementById('deployment-mode').textContent = `${capabilities.mode || '识别中'} · ${capabilities.service_control ? '本机管理' : '远程监控'}`;
            const meta = [up.http_status ? `HTTP ${up.http_status}` : '', up.commit ? `构建 ${up.commit.slice(0,12)}` : '', up.build_date || '', capabilities.reason || '可控制本机 systemd 服务'];
            document.getElementById('connection-detail').textContent = meta.filter(Boolean).join(' · ');
            const sample = document.getElementById('collection-status');
            sample.className = `badge ${stale ? 'warning' : 'info'}`;
            sample.textContent = stale ? '数据待确认 · 后台重试中' : `最近采集 ${formatDateTimeCn(collection.at)}`;
            const sources = {management_header:'管理接口响应头',management_payload:'管理接口构建元数据',unknown:'未确认'};
            const version = d.version || {};
            const devNote = version.current === 'dev' ? '开发构建（上游未注入 Release 标签）' : '';
            document.getElementById('version-note').textContent = [sources[version.source] || version.source || '未确认', devNote, version.stale ? '上次识别值，当前未确认' : '', version.error || ''].filter(Boolean).join(' · ');
            document.getElementById('update-capability-note').textContent = capabilities.binary_update ? '检查版本 → 等待空闲 → 校验升级' : '仅检查版本；镜像升级请在部署端执行';
            document.getElementById('service-capability-note').textContent = capabilities.reason || '可控制本机 systemd。停止和重启会中断上游请求。';
            document.querySelectorAll('button[onclick^="triggerUpdate"], #auto-switch').forEach(btn => {btn.disabled = !capabilities.binary_update || !!d.update?.in_progress; btn.title = capabilities.reason || '';});
            document.querySelectorAll('button[onclick^="serviceAction"]').forEach(btn => {btn.disabled = !capabilities.service_control || !!d.update?.in_progress; btn.title = capabilities.reason || '';});
            const badge = document.getElementById('health-service-status');
            badge.className = `badge ${kind}`;
            setHtml(badge, `<span class="badge-dot"></span>${escapeHtml(label)}`);
            document.getElementById('header-status').textContent = label;
            document.getElementById('header-status-dot').className = 'header-stat-dot ' + (kind === 'success' ? 'green' : kind === 'danger' ? 'red' : 'orange');
            document.getElementById('usage-source-note').textContent = `请求：${d.requests?.log_catching_up ? '日志追赶中，计数暂不完整' : d.requests?.log_available ? '日志采集' : '无日志挂载 / 历史保留值'} · Token / 成本：本地历史快照${d.requests?.log_partial ? ' · 仅统计已扫描日志' : ''}`;
        }

        // 日志过滤设置
        function initLogFilter() {
            const saved = localStorage.getItem('hideLocalhost');
            // 默认开启（saved 为 null 或 'true' 时都开启）
            const hide = saved === null || saved === 'true';
            document.getElementById('hide-localhost').checked = hide;
        }

        function saveLogFilter() {
            const hide = document.getElementById('hide-localhost').checked;
            localStorage.setItem('hideLocalhost', hide ? 'true' : 'false');
        }

        function shouldHideLog(message) {
            const hide = document.getElementById('hide-localhost').checked;
            if (!hide) return false;
            // 匹配 127.0.0.1 的日志
            return /127\.0\.0\.1/.test(message);
        }

        // 主题
        const LIGHT_PALETTES = new Set(['sky', 'mint', 'rose', 'sand']);

        function initTheme() {
            const saved = localStorage.getItem('theme') === 'light' ? 'light' : 'dark';
            const paletteValue = localStorage.getItem('theme_palette');
            const palette = LIGHT_PALETTES.has(paletteValue) ? paletteValue : 'sky';
            document.body.setAttribute('data-theme', saved);
            document.body.setAttribute('data-palette', palette);
            updateThemeIcon();
        }

        function setLightPalette(value) {
            const palette = LIGHT_PALETTES.has(value) ? value : 'sky';
            document.body.setAttribute('data-palette', palette);
            document.body.setAttribute('data-theme', 'light');
            localStorage.setItem('theme_palette', palette);
            localStorage.setItem('theme', 'light');
            updateThemeIcon();
        }

        function toggleTheme() {
            const current = document.body.getAttribute('data-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            document.body.setAttribute('data-theme', next);
            localStorage.setItem('theme', next);
            updateThemeIcon();
        }

        function updateThemeIcon() {
            const isDark = document.body.getAttribute('data-theme') === 'dark';
            const palette = document.body.getAttribute('data-palette') || 'sky';
            const select = document.getElementById('theme-select');
            if (select) select.value = palette;
            const themeColor = document.getElementById('theme-color');
            if (themeColor) themeColor.setAttribute('content', isDark ? '#17191d' : getComputedStyle(document.body).getPropertyValue('--accent').trim());
            document.getElementById('theme-icon').innerHTML = isDark
                ? '<path d="M12 7a5 5 0 100 10 5 5 0 000-10zM12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>'
                : '<path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/>';
        }

        function setSwitchState(element, enabled) {
            if (!element) return;
            element.classList.toggle('on', !!enabled);
            element.setAttribute('aria-pressed', enabled ? 'true' : 'false');
        }

	        // API 调用
	        async function api(endpoint, opts = {}) {
	            const headers = { Accept: 'application/json', ...(opts.headers || {}) };
	            if (opts.body != null && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
	            if (opts.method && !['GET', 'HEAD'].includes(String(opts.method).toUpperCase())) headers['X-Panel-CSRF'] = '1';
	            const panelKey = (localStorage.getItem('panel_key') || '').trim();
	            if (panelKey) headers['X-Panel-Key'] = panelKey;

	            const controller = new AbortController();
	            const timeoutMs = typeof opts.timeout === 'number' ? opts.timeout : 15000;
	            const timer = setTimeout(() => controller.abort(), timeoutMs);

	            try {
	                const res = await fetch(API + endpoint, { ...opts, headers, signal: controller.signal });
	                if (res.status === 401) {
                        document.getElementById('panel-auth-banner').classList.add('show');
                        return null;
                    }
	                return await res.json().catch(() => null);
	            } catch (e) {
	                if (e && e.name === 'AbortError') {
	                    console.error('请求超时:', endpoint);
	                } else {
                    console.error(e);
                }
                return null;
            } finally {
                clearTimeout(timer);
	            }
	        }

	        function initPanelKeyFromURL() {
	            try {
	                const url = new URL(window.location.href);
	                const fragment = new URLSearchParams(url.hash.replace(/^#/, ''));
	                const key = (fragment.get('panel_key') || '').trim();
	                if (!key) return;
	                localStorage.setItem('panel_key', key);
	                fragment.delete('panel_key');
	                const hash = fragment.toString();
	                history.replaceState({}, '', url.pathname + url.search + (hash ? `#${hash}` : ''));
	            } catch (e) {}
	        }

        function pickTokenUnit(value) {
            const num = Math.max(0, Number(value || 0));
            // 需求：随数值变大自动切换（百万 -> 千万 -> 亿；超过亿后固定用“亿”）
            if (num >= 100000000) return { scale: 100000000, unit: '亿Tokens' };
            if (num >= 10000000) return { scale: 10000000, unit: '千万Tokens' };
            return { scale: 1000000, unit: '百万Tokens' };
        }

        function formatTokens(value, decimals = 2) {
            const num = Number(value || 0);
            const { scale, unit } = pickTokenUnit(num);
            const formatted = (num / scale).toFixed(decimals);
            return `<span class="req-inline-pair"><span class="req-token-number">${formatted}</span><span class="req-token-unit">${unit}</span></span>`;
        }

        function formatTokensShort(value, decimals = 1) {
            const num = Number(value || 0);
            const { scale } = pickTokenUnit(num);
            return `${(num / scale).toFixed(decimals)}`;
        }

        function formatUsd(value) {
            const num = Number(value || 0);
            const formatted = num.toFixed(2);
            return `<span class="req-inline-pair"><span class="req-cost-number">${formatted}</span><span class="req-cost-unit">美元</span></span>`;
        }

        function formatUsdShort(value) {
            const num = Number(value || 0);
            return `${num.toFixed(1)}`;
        }

        function getPricingBasisText(data) {
            const basis = data && data.pricing_basis ? data.pricing_basis : null;
            return (basis && basis.text) ? basis.text : '美元/百万Tokens';
        }

        function formatPricingNumber(value, digits = 3) {
            return Number(value || 0).toFixed(digits);
        }

        function formatTokenAmountByBasis(value, data, decimals = 2) {
            const basis = data && data.pricing_basis ? data.pricing_basis : null;
            const scale = Number(basis && basis.tokens ? basis.tokens : 1000000);
            const label = basis && basis.label ? basis.label : '百万Tokens';
            return `${(Number(value || 0) / scale).toFixed(decimals)} ${label}`;
        }

        function formatDurationCn(value) {
            const total = Math.max(0, Math.round(Number(value || 0)));
            if (total <= 0) return '0秒';
            const days = Math.floor(total / 86400);
            const hours = Math.floor((total % 86400) / 3600);
            const minutes = Math.floor((total % 3600) / 60);
            const seconds = total % 60;
            if (days > 0) return `${days}天${hours}小时`;
            if (hours > 0) return `${hours}小时${minutes}分钟`;
            if (minutes > 0) return `${minutes}分钟${seconds}秒`;
            return `${seconds}秒`;
        }

        function formatDateTimeCn(value) {
            if (!value) return '-';
            try {
                const dt = new Date(value);
                if (Number.isNaN(dt.getTime())) return value;
                return dt.toLocaleString('zh-CN', { hour12: false });
            } catch (e) {
                return value;
            }
        }

        function applySummarySizing(el) {
            if (!el) return;
            const text = (el.textContent || '').trim();
            const digits = text.replace(/[^0-9]/g, '');
            el.classList.remove('summary-medium', 'summary-small');
            if (digits.length >= 5) {
                el.classList.add('summary-small');
            } else if (digits.length >= 4) {
                el.classList.add('summary-medium');
            }
        }

        function setPricingValue(id, value, force = false) {
            const input = document.getElementById(id);
            if (!input) return;
            if (!force && document.activeElement === input) return;
            input.value = value ?? 0;
        }

        let quoteTimer = null;
        const quoteFontDefaults = { text: 17, author: 14 };
        const quoteFontLimits = { min: 12, max: 32 };
        const quoteFontState = { text: quoteFontDefaults.text, author: quoteFontDefaults.author };

        function updateQuoteFontButtons() {
            const textBtn = document.getElementById('quote-size-btn');
            const authorBtn = document.getElementById('quote-author-size-btn');
            if (textBtn) textBtn.textContent = `语录：${quoteFontState.text} px`;
            if (authorBtn) authorBtn.textContent = `作者：${quoteFontState.author} px`;
        }

        function setQuoteFontSizes(textSize, authorSize) {
            if (!Number.isFinite(textSize) || !Number.isFinite(authorSize)) return;
            quoteFontState.text = Math.max(quoteFontLimits.min, Math.min(quoteFontLimits.max, textSize));
            quoteFontState.author = Math.max(quoteFontLimits.min, Math.min(quoteFontLimits.max, authorSize));
            document.documentElement.style.setProperty('--quote-font-size', `${quoteFontState.text}px`);
            document.documentElement.style.setProperty('--quote-author-size', `${quoteFontState.author}px`);
            updateQuoteFontButtons();
        }

        function initQuoteFontSizes() {
            const savedText = parseFloat(localStorage.getItem('quote_font_size'));
            const savedAuthor = parseFloat(localStorage.getItem('quote_author_font_size'));
            const textSize = Number.isFinite(savedText) ? savedText : quoteFontDefaults.text;
            const authorSize = Number.isFinite(savedAuthor) ? savedAuthor : quoteFontDefaults.author;
            setQuoteFontSizes(textSize, authorSize);
        }

        function adjustQuoteFont(type) {
            const isAuthor = type === 'author';
            const label = isAuthor ? '作者' : '语录';
            const current = isAuthor ? quoteFontState.author : quoteFontState.text;
            const raw = prompt(`请输入${label}字号(px)`, String(current));
            if (raw === null) return;
            const value = parseFloat(raw);
            if (!Number.isFinite(value)) {
                toast('请输入有效数字', 'error');
                return;
            }
            const clamped = Math.max(quoteFontLimits.min, Math.min(quoteFontLimits.max, value));
            if (isAuthor) {
                quoteFontState.author = clamped;
            } else {
                quoteFontState.text = clamped;
            }
            localStorage.setItem('quote_font_size', quoteFontState.text);
            localStorage.setItem('quote_author_font_size', quoteFontState.author);
            setQuoteFontSizes(quoteFontState.text, quoteFontState.author);
        }

        async function loadQuote() {
            const d = await api('/api/quote');
            if (!d) return;
            const text = d.text || '-';
            const author = d.author || '-';
            const textEl = document.getElementById('quote-text');
            const authorEl = document.getElementById('quote-author');
            textEl.textContent = text;
            authorEl.textContent = author;
        }

        function refreshQuote() {
            loadQuote();
        }

        async function addQuote() {
            const line = prompt('请输入语录（格式：内容 出自：作者）');
            if (!line) return;
            const r = await api('/api/quote', {
                method: 'POST',
                body: JSON.stringify({ line })
            });
            if (r?.success) {
                toast('语录已添加', 'success');
                loadQuote();
            } else {
                toast(r?.error || '添加失败', 'error');
            }
        }

        function updateQuoteInterval() {
            const input = document.getElementById('quote-interval-input');
            if (!input) return;
            const parsed = parseInt(input.value || '5', 10);
            const minutes = Number.isFinite(parsed) ? Math.max(1, Math.min(1440, parsed)) : 5;
            input.value = minutes;
            localStorage.setItem('quote_refresh_minutes', minutes);
            if (quoteTimer) clearInterval(quoteTimer);
            quoteTimer = setInterval(() => { if (!document.hidden) loadQuote(); }, minutes * 60 * 1000);
        }

        function initQuote() {
            const input = document.getElementById('quote-interval-input');
            const saved = parseInt(localStorage.getItem('quote_refresh_minutes') || '5', 10);
            if (input) {
                input.value = isNaN(saved) ? 5 : saved;
                input.addEventListener('change', updateQuoteInterval);
            }
            initQuoteFontSizes();
            loadQuote();
            updateQuoteInterval();
        }

        function initPricingInputs() {
            ['pricing-input', 'pricing-output', 'pricing-cache'].forEach((id) => {
                const input = document.getElementById(id);
                if (!input) return;
                input.addEventListener('input', () => { pricingDirty = true; });
                input.addEventListener('change', () => { pricingDirty = true; });
            });
        }

        function renderManagementKeyBanner(auth) {
            const banner = document.getElementById('management-key-banner');
            const messageEl = document.getElementById('management-key-message');
            if (!banner || !messageEl) return;
            const shouldShow = managementFormOpen || !!(auth && (!auth.configured || auth.locked || auth.consecutive_failures > 0));
            if (!shouldShow) {
                banner.classList.remove('show');
                return;
            }
            banner.classList.add('show');
            messageEl.textContent = auth?.message || '请输入上游的明文管理密钥（不是配置文件中的 bcrypt 哈希）。';
        }

        async function saveManagementKey() {
            if (inflight.saveManagementKey) return;
            const input = document.getElementById('management-key-input');
            const key = (input?.value || '').trim();
            if (!key) {
                toast('请输入 CPA 管理密钥', 'error');
                return;
            }
            inflight.saveManagementKey = true;
            try {
                const r = await api('/api/config/management-key', {
                    method: 'POST',
                    body: JSON.stringify({ management_key: key })
                });
                if (r?.success) {
                    if (input) input.value = '';
                    managementFormOpen = false;
                    lastStatusKey = '';
                    renderManagementKeyBanner(r.management_auth);
                    toast('CPA 管理密钥已保存', 'success');
                    refreshStatus();
                    runHealthCheck();
                } else {
                    toast(r?.error || '保存密钥失败', 'error');
                }
            } finally {
                inflight.saveManagementKey = false;
            }
        }

        function initUpdateSettingsInputs() {
            ['idle-input', 'check-interval-input'].forEach((id) => {
                const input = document.getElementById(id);
                if (!input) return;
                input.addEventListener('input', () => { updateSettingsDirty = true; });
                input.addEventListener('change', () => { updateSettingsDirty = true; });
                input.addEventListener('focus', () => { updateSettingsDirty = true; });
            });
            const managementKeyInput = document.getElementById('management-key-input');
            if (managementKeyInput) {
                managementKeyInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') saveManagementKey();
                });
            }
        }

        // 从面板后端获取模型列表
        async function loadModelsFromAPI() {
            if (inflight.loadModelsFromAPI) return;
            inflight.loadModelsFromAPI = true;
            const list = document.getElementById('model-list');
            const count = document.getElementById('model-count');
            const headerModels = document.getElementById('header-models');
            if (!list || !count || !headerModels) {
                inflight.loadModelsFromAPI = false;
                return;
            }

            list.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;">加载中...</div>';

            try {
                const data = await api('/api/models');

                if (data && data.success && data.models && data.models.length > 0) {
                    const models = data.models;
                    count.textContent = `共 ${models.length} 个模型`;
                    headerModels.textContent = models.length;

                    const groups = {
                        Gemini: [],
                        GPT: [],
                        Claude: [],
                        Qwen: [],
                        其他: [],
                    };

                    models.forEach(m => {
                        const id = (m.id || m.name || 'unknown');
                        const lower = id.toLowerCase();
                        if (lower.includes('gemini')) groups.Gemini.push(m);
                        else if (lower.includes('gpt')) groups.GPT.push(m);
                        else if (lower.includes('claude')) groups.Claude.push(m);
                        else if (lower.includes('qwen')) groups.Qwen.push(m);
                        else groups.其他.push(m);
                    });

                    list.innerHTML = Object.entries(groups).map(([groupName, items]) => {
                        if (!items.length) return '';
                        const itemsHtml = items.map(m => `
                            <div class="model-item">
                                <div>
                                    <div class="model-name">${escapeHtml(m.id || m.name || 'unknown')}</div>
                                    <div class="model-provider">Owner: ${escapeHtml(m.owned_by || m.provider || 'unknown')}</div>
                                </div>
                                <span class="badge info">${escapeHtml(m.object || 'model')}</span>
                            </div>
                        `).join('');
                        return `
                            <details class="model-group" open>
                                <summary>${groupName}（${items.length}）</summary>
                                <div class="model-group-list">${itemsHtml}</div>
                            </details>
                        `;
                    }).join('');
                } else {
                    list.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;">未找到模型</div>';
                    count.textContent = '共 0 个模型';
                    headerModels.textContent = '0';
                }
            } catch (e) {
                console.error('获取模型失败:', e);
                list.innerHTML = '<div style="text-align:center;color:var(--danger);padding:20px;">获取模型失败</div>';
                count.textContent = '共 0 个模型';
                headerModels.textContent = '0';
            } finally {
                inflight.loadModelsFromAPI = false;
            }
        }

        // 状态刷新
        async function refreshStatus() {
            if (inflight.refreshStatus) return;
            inflight.refreshStatus = true;
            try {
            const d = await api('/api/status');
            if (!d) {
                document.getElementById('collection-status').textContent = '面板连接中断 · 显示上次数据';
                document.getElementById('collection-status').className = 'badge warning';
                return;
            }
            configWriteEnabled = d.config?.write_enabled === true;
            renderConnection(d);
            document.getElementById('config-save').disabled = !configWriteEnabled;
            if (!d.collection?.at) return;
            const renderKey = JSON.stringify({...d, collection:undefined});
            if (renderKey === lastStatusKey) return;
            lastStatusKey = renderKey;
            const pidEl = document.getElementById('health-service-pid');
            if (pidEl) pidEl.textContent = d.service.pid || '-';
            const uptimeEl = document.getElementById('health-service-uptime');
            if (uptimeEl) uptimeEl.textContent = d.service.uptime || '-';

            // Header
            document.getElementById('header-version').textContent = d.version.current;
            document.getElementById('header-requests').textContent = d.requests.count;
            if (d.panel) {
                const badge = document.getElementById('panel-version-badge');
                if (badge && d.panel.version) badge.textContent = d.panel.version;
                if (d.panel.name && d.panel.version) {
                    document.title = `${d.panel.name} 管理面板 ${d.panel.version}`;
                } else if (d.panel.name) {
                    document.title = `${d.panel.name} 管理面板`;
                }
            }

            const healthDot = document.getElementById('header-health-dot');
            const healthColors = { healthy: 'green', degraded: 'orange', unhealthy: 'red' };
            healthDot.className = 'header-stat-dot ' + (healthColors[d.health] || 'blue');
            document.getElementById('header-health').textContent = d.health === 'healthy' ? '正常' : d.health === 'degraded' ? '警告' : d.health === 'unhealthy' ? '异常' : d.health;

            // 版本
            document.getElementById('current-ver').textContent = d.version.current;
            document.getElementById('latest-ver').textContent = d.version.latest;

            // 更新横幅
            if (d.version.has_update) {
                document.getElementById('update-banner').classList.add('show');
                document.getElementById('banner-current').textContent = d.version.current;
                document.getElementById('banner-latest').textContent = d.version.latest;
            } else {
                document.getElementById('update-banner').classList.remove('show');
            }

            renderManagementKeyBanner(d.management_auth);

            // 请求统计
            document.getElementById('req-count').textContent = d.requests.count;
            const reqSuccess = document.getElementById('req-success');
            if (reqSuccess) reqSuccess.textContent = d.requests.success || 0;
            const reqFailed = document.getElementById('req-failed');
            if (reqFailed) reqFailed.textContent = d.requests.failed || 0;
            const idleEl = document.getElementById('idle-status');
            if (idleEl) {
                const logUnavailable = d.requests.idle_reason === 'log_unavailable';
                const invalidTimestamp = d.requests.idle_reason === 'invalid_timestamp';
                idleEl.className = 'badge ' + (d.requests.is_idle ? 'info' : 'warning');
                idleEl.textContent = logUnavailable ? '日志不可用' : (invalidTimestamp ? '时间无效' : (d.requests.is_idle ? '空闲中' : '处理中'));
                idleEl.title = (logUnavailable || invalidTimestamp) ? '无法可靠判断空闲状态，自动更新会保持等待' : '';
            }

            const inputTokens = d.requests.input_tokens || 0;
            const billableInputTokens = d.requests.billable_input_tokens || 0;
            const outputTokens = d.requests.output_tokens || 0;
            const reasoningTokens = d.requests.reasoning_tokens || 0;
            const billedOutputTokens = Number(outputTokens) + Number(reasoningTokens);
            const cacheTokens = d.requests.cached_tokens || 0;
            const totalTokens = d.requests.total_tokens || (Number(inputTokens) + billedOutputTokens);
            setHtml(document.getElementById('req-input-tokens'), `输入（含缓存）：${formatTokens(inputTokens)}`);
            setHtml(document.getElementById('req-output-tokens'), `${reasoningTokens ? '输出（含推理）' : '输出'}：${formatTokens(billedOutputTokens)}`);
            setHtml(document.getElementById('req-cache-tokens'), `缓存：${formatTokens(cacheTokens)}`);
            document.getElementById('req-total-tokens').textContent = formatTokensShort(totalTokens);
            const unitEl = document.getElementById('req-total-tokens-unit');
            if (unitEl) unitEl.textContent = pickTokenUnit(totalTokens).unit;

            if (d.usage_costs) {
                setHtml(document.getElementById('req-input-cost'), formatUsd(d.usage_costs.input || 0));
                setHtml(document.getElementById('req-output-cost'), formatUsd(d.usage_costs.output || 0));
                setHtml(document.getElementById('req-cache-cost'), formatUsd(d.usage_costs.cache || 0));
                document.getElementById('req-total-cost').textContent = formatUsdShort(d.usage_costs.total || 0);
            }
            const pricingBasisText = getPricingBasisText(d);
            const usageCostBasisInfoEl = document.getElementById('usage-cost-basis-info');
            if (usageCostBasisInfoEl) {
                usageCostBasisInfoEl.textContent = `费用按 ${pricingBasisText} 计算`;
            }
            const usageCostBreakdownInfoEl = document.getElementById('usage-cost-breakdown-info');
            if (usageCostBreakdownInfoEl) {
                usageCostBreakdownInfoEl.textContent = `当前计费输入：${formatTokenAmountByBasis(billableInputTokens, d)}；缓存命中：${formatTokenAmountByBasis(cacheTokens, d)}（单独按缓存价计算）`;
            }
            const pricingCurrentTitleEl = document.getElementById('pricing-current-title');
            if (pricingCurrentTitleEl) {
                pricingCurrentTitleEl.textContent = `当前生效价格（${pricingBasisText}）`;
            }
            const effectiveInputEl = document.getElementById('effective-pricing-input');
            if (effectiveInputEl) effectiveInputEl.textContent = formatPricingNumber(d.pricing?.input);
            const effectiveOutputEl = document.getElementById('effective-pricing-output');
            if (effectiveOutputEl) effectiveOutputEl.textContent = formatPricingNumber(d.pricing?.output);
            const effectiveCacheEl = document.getElementById('effective-pricing-cache');
            if (effectiveCacheEl) effectiveCacheEl.textContent = formatPricingNumber(d.pricing?.cache);
            const pricingBasisInfoEl = document.getElementById('pricing-basis-info');
            if (pricingBasisInfoEl) {
                pricingBasisInfoEl.textContent = `计价口径：${pricingBasisText}（固定，不随上方 Token 单位切换）`;
            }

            applySummarySizing(document.getElementById('req-count'));
            applySummarySizing(document.getElementById('req-total-tokens'));
            applySummarySizing(document.getElementById('req-total-cost'));

	            if (d.pricing) {
	                if (!pricingDirty) {
	                    setPricingValue('pricing-input', d.pricing.input);
	                    setPricingValue('pricing-output', d.pricing.output);
	                    setPricingValue('pricing-cache', d.pricing.cache);
	                }
	                pricingInitialized = true;
	            }
	            // 价格自动同步状态与来源信息
	            const pmeta = d.pricing_meta || {};
	            const pricingAutoEnabled = !!pmeta.auto_enabled;
	            const pSw = document.getElementById('pricing-auto-switch');
	            const pLabel = document.getElementById('pricing-auto-label');
	            if (pSw && pLabel) {
	                setSwitchState(pSw, pricingAutoEnabled);
	                pLabel.textContent = pricingAutoEnabled ? '已开启' : '已关闭';
	            }
	            const pInfo = document.getElementById('pricing-source-info');
	            if (pInfo) {
	                const mode = pmeta.mode || (pricingAutoEnabled ? 'auto' : 'manual');
	                const src = pmeta.source || (pricingAutoEnabled ? (pmeta.auto_source || 'openrouter') : 'manual');
	                const model = pmeta.model || pmeta.auto_model || '';
	                if (!pricingAutoEnabled) {
	                    pInfo.textContent = '来源：手动（自动同步已关闭）';
	                } else if (mode === 'manual') {
	                    pInfo.textContent = '来源：手动（自动同步已开启）';
	                } else if (mode === 'mixed') {
	                    pInfo.textContent = `来源：混合（${src}${model ? '：' + model : ''}；手动值优先）`;
	                } else {
	                    pInfo.textContent = `来源：${src}${model ? '：' + model : ''}`;
	                }
	            }

            // 自动更新开关
            const sw = document.getElementById('auto-switch');
            const label = document.getElementById('auto-label');
            setSwitchState(sw, d.update.auto_enabled);
            label.textContent = d.update.auto_enabled ? '已开启' : '已关闭';
            // 自动更新配置（避免每 5 秒刷新把用户正在输入的值“打回原值”）
            const idleInputEl = document.getElementById('idle-input');
            const checkIntervalEl = document.getElementById('check-interval-input');
            const editingIdle = document.activeElement === idleInputEl;
            const editingCheck = document.activeElement === checkIntervalEl;
            if (!updateSettingsDirty && !editingIdle && idleInputEl && typeof d.config.idle_threshold === 'number') {
                idleInputEl.value = Math.round(d.config.idle_threshold / 60);
            }
            if (!updateSettingsDirty && !editingCheck && checkIntervalEl && typeof d.config.check_interval === 'number') {
                checkIntervalEl.value = Math.round(d.config.check_interval / 60);
            }
            // 如果用户输入的值与后端一致，则自动解除 dirty，恢复后端刷新
            if (updateSettingsDirty && !editingIdle && !editingCheck && idleInputEl && checkIntervalEl
                && typeof d.config.idle_threshold === 'number' && typeof d.config.check_interval === 'number') {
                const idleLocal = parseInt(idleInputEl.value || '0', 10);
                const checkLocal = parseInt(checkIntervalEl.value || '0', 10);
                const idleServer = Math.round(d.config.idle_threshold / 60);
                const checkServer = Math.round(d.config.check_interval / 60);
                if (!isNaN(idleLocal) && !isNaN(checkLocal) && idleLocal === idleServer && checkLocal === checkServer) {
                    updateSettingsDirty = false;
                }
            }

            const autoStatus = d.update?.status || {};
            const autoIdle = autoStatus.idle || {};
            const autoStatusLine = document.getElementById('auto-status-line');
            const autoStatusDetail = document.getElementById('auto-status-detail');
            if (autoStatusLine) {
                let statusText = autoStatus.summary || '等待状态更新';
                let statusClass = 'info';
                const phase = autoStatus.phase || 'unknown';
                if (phase === 'disabled') {
                    statusClass = 'warning';
                } else if (phase === 'updating' || d.update.in_progress) {
                    statusClass = 'warning';
                } else if (phase === 'no_update') {
                    statusClass = 'success';
                } else if (phase === 'wait_idle' || phase === 'backoff') {
                    statusClass = 'warning';
                } else if (phase === 'ready') {
                    statusClass = 'success';
                }
                autoStatusLine.className = `badge ${statusClass}`;
                autoStatusLine.textContent = statusText;
            }
            if (autoStatusDetail) {
                const detailParts = [];
                if (autoStatus.has_update) {
                    detailParts.push(`发现新版本：${escapeHtml(d.version.current)} → ${escapeHtml(d.version.latest)}`);
                } else {
                    detailParts.push('当前没有可安装的新版本');
                }
                if (autoStatus.phase === 'backoff' && autoStatus.failure_count > 0) {
                    detailParts.push(
                        `版本 ${escapeHtml(autoStatus.failed_version || d.version.latest)} 已失败 `
                        + `${escapeHtml(autoStatus.failure_count)} 次，自动更新将在退避结束后重试；手动强制更新仍可立即执行`
                    );
                }
                if (autoIdle.reason === 'log_unavailable') {
                    detailParts.push('请求日志路径不可用；为避免中断流量，自动更新已保持等待');
                } else if (autoIdle.reason === 'invalid_timestamp') {
                    detailParts.push('请求日志时间戳无法解析；为避免中断流量，自动更新已保持等待');
                } else if (autoIdle.last_request_time) {
                    const lastRequestText = escapeHtml(formatDateTimeCn(autoIdle.last_request_time));
                    if (autoIdle.is_idle) {
                        detailParts.push(`最近请求在 ${lastRequestText}，已空闲 ${renderAutoTime(formatDurationCn(autoIdle.idle_for_seconds || 0))}`);
                    } else {
                        detailParts.push(`最近请求在 ${lastRequestText}，还需空闲 ${renderAutoTime(formatDurationCn(autoIdle.idle_wait_seconds || 0))}`);
                    }
                } else {
                    detailParts.push('暂无请求记录，当前视为空闲');
                }
                if (d.update.auto_enabled && typeof autoStatus.next_check_in_seconds === 'number') {
                    detailParts.push(`下一次自动检查约在 ${renderAutoTime(formatDurationCn(autoStatus.next_check_in_seconds))} 后`);
                }
                if (autoStatus.last_check_time) {
                    detailParts.push(`上次自动检查时间：${escapeHtml(formatDateTimeCn(autoStatus.last_check_time))}`);
                }
                setHtml(autoStatusDetail, detailParts.join('；'));
            }

            const healthOverallEl = document.getElementById('health-overall');
            if (healthOverallEl) {
                healthOverallEl.textContent = d.health === 'healthy' ? '健康' : d.health === 'degraded' ? '警告' : d.health === 'unhealthy' ? '异常' : d.health;
            }

            } catch (error) {
                console.error('状态渲染失败:', error);
            } finally {
                inflight.refreshStatus = false;
            }
        }

        async function refreshResources() {
            if (inflight.refreshResources) return;
            inflight.refreshResources = true;
            try {
            const d = await api('/api/resources');
            if (!d || d.error || d.pending) return;
            document.getElementById('resource-scope-note').textContent = `资源范围：${d.scope === 'host' ? '本机资源' : '面板运行环境（可能包含宿主机），不是远程上游'}${d.stale ? ' · 数据已过期' : ''}`;

            // CPU
            document.getElementById('cpu-percent').textContent = d.cpu.percent.toFixed(1) + '%';
            document.getElementById('cpu-bar').style.width = d.cpu.percent + '%';
            document.getElementById('cpu-bar').className = 'progress-fill ' + (d.cpu.percent > 80 ? 'red' : d.cpu.percent > 60 ? 'orange' : 'blue');

            // CPU详细信息
            const cpuFreq = d.cpu.freq_current ? Math.round(d.cpu.freq_current) + ' MHz' : '-';
            const cpuDetailText = `${d.cpu.cores || '-'} 核心 | ${cpuFreq}`;
            document.getElementById('cpu-detail').textContent = cpuDetailText;
            document.getElementById('cpu-detail').title = cpuDetailText;
            document.getElementById('cpu-detail-inline').textContent = cpuDetailText;
            document.getElementById('cpu-detail-inline').title = cpuDetailText;
            document.getElementById('cpu-load-1m').textContent = (d.cpu.load_1m || 0).toFixed(2);
            document.getElementById('cpu-load-5m').textContent = (d.cpu.load_5m || 0).toFixed(2);
            document.getElementById('cpu-load-15m').textContent = (d.cpu.load_15m || 0).toFixed(2);

            if (d.system) {
                const cpuModelText = `CPU 型号：${d.system.cpu_model || '-'}`;
                const cloudVendorText = `云厂商：${d.system.cloud_vendor || '-'}`;
                const osVersionText = `系统版本：${d.system.os_version || '-'}`;
                document.getElementById('cpu-model').textContent = cpuModelText;
                document.getElementById('cpu-model').title = cpuModelText;
                document.getElementById('cloud-vendor').textContent = cloudVendorText;
                document.getElementById('cloud-vendor').title = cloudVendorText;
                document.getElementById('os-version').textContent = osVersionText;
                document.getElementById('os-version').title = osVersionText;
            }

            // 内存
            const memUsed = (d.memory.used / 1024 / 1024 / 1024).toFixed(2);
            const memTotal = (d.memory.total / 1024 / 1024 / 1024).toFixed(2);
            document.getElementById('mem-percent').textContent = d.memory.percent.toFixed(1) + '%';
            const memDetailText = `${memUsed} GB / ${memTotal} GB`;
            document.getElementById('mem-detail').textContent = memDetailText;
            document.getElementById('mem-detail').title = memDetailText;
            document.getElementById('mem-detail-inline').textContent = memDetailText;
            document.getElementById('mem-detail-inline').title = memDetailText;
            document.getElementById('mem-bar').style.width = d.memory.percent + '%';
            document.getElementById('mem-bar').className = 'progress-fill ' + (d.memory.percent > 80 ? 'red' : d.memory.percent > 60 ? 'orange' : 'purple');

            // 内存详细信息
            const memAvail = (d.memory.available / 1024 / 1024 / 1024).toFixed(2) + ' GB';
            const memCached = d.memory.cached ? ((d.memory.cached / 1024 / 1024 / 1024).toFixed(2) + ' GB') : '-';
            const swapPercent = d.memory.swap_percent || 0;
            const swapTotal = d.memory.swap_total || 0;
            const swapTotalGB = (swapTotal / 1024 / 1024 / 1024).toFixed(2);
            document.getElementById('mem-available').textContent = memAvail;
            document.getElementById('mem-available').title = memAvail;
            document.getElementById('mem-cached').textContent = memCached;
            document.getElementById('mem-cached').title = memCached;
            const memSwapText = swapTotal === 0 ? '未配置' : `${swapPercent.toFixed(1)}% / ${swapTotalGB} GB`;
            document.getElementById('mem-swap').textContent = memSwapText;
            document.getElementById('mem-swap').title = memSwapText;

            if (d.cliproxy && capabilities.service_control) {
                const clipCpu = d.cliproxy.cpu_percent || 0;
                const clipMem = d.cliproxy.memory_bytes || 0;
                const clipCpuText = `${clipCpu.toFixed(1)}%`;
                const clipMemText = `${(clipMem / 1024 / 1024).toFixed(1)} MB`;
                document.getElementById('cliproxy-cpu').textContent = clipCpuText;
                document.getElementById('cliproxy-cpu').title = clipCpuText;
                document.getElementById('cliproxy-mem').textContent = clipMemText;
                document.getElementById('cliproxy-mem').title = clipMemText;
            }

            if (!capabilities.service_control) {
                document.getElementById('cliproxy-cpu').textContent = '不适用';
                document.getElementById('cliproxy-mem').textContent = '不适用';
            }
            // 磁盘
            const diskUsed = (d.disk.used / 1024 / 1024 / 1024).toFixed(1);
            const diskTotal = (d.disk.total / 1024 / 1024 / 1024).toFixed(1);
            document.getElementById('disk-percent').textContent = d.disk.percent.toFixed(1) + '%';
            const diskDetailText = `${diskUsed}GB / ${diskTotal}GB (${d.disk.path || '/'})`;
            document.getElementById('disk-detail').textContent = diskDetailText;
            document.getElementById('disk-detail').title = diskDetailText;
            document.getElementById('disk-detail-inline').textContent = diskDetailText;
            document.getElementById('disk-detail-inline').title = diskDetailText;
            document.getElementById('disk-bar').style.width = d.disk.percent + '%';
            document.getElementById('disk-bar').className = 'progress-fill ' + (d.disk.percent > 80 ? 'red' : d.disk.percent > 60 ? 'orange' : 'orange');

            } catch (error) {
                console.error('资源渲染失败:', error);
            } finally {
                inflight.refreshResources = false;
            }
        }

        async function clearStats() {
            if (!confirm('确定要清空所有请求统计数据吗？')) return;
            try {
                const r = await api('/api/stats/clear', { method: 'POST' });
                if (r && r.success) {
                    toast('统计数据已清空', 'success');
                    refreshStatus();
                } else {
                    toast('清空失败: ' + (r && r.message ? r.message : '未知错误'), 'error');
                }
            } catch (e) {
                console.error('clearStats error:', e);
                toast('清空失败: ' + e.message, 'error');
            }
        }

        async function savePricing() {
            const input = parseFloat(document.getElementById('pricing-input').value || '0');
            const output = parseFloat(document.getElementById('pricing-output').value || '0');
            const cachePrice = parseFloat(document.getElementById('pricing-cache').value || '0');
            const r = await api('/api/pricing', {
                method: 'POST',
                body: JSON.stringify({ input, output, cache: cachePrice })
            });
            if (r?.success) {
                pricingDirty = false;
                pricingInitialized = true;
                const pricing = r.pricing || { input, output, cache: cachePrice };
                setPricingValue('pricing-input', pricing.input, true);
                setPricingValue('pricing-output', pricing.output, true);
                setPricingValue('pricing-cache', pricing.cache, true);
                toast('价格已保存', 'success');
                refreshStatus();
            } else {
                toast('保存失败', 'error');
            }
        }

        async function runHealthCheck() {
            if (inflight.runHealthCheck) return;
            inflight.runHealthCheck = true;
            const d = await api('/api/health');
            if (!d) { inflight.runHealthCheck = false; return; }

            const healthOverallEl = document.getElementById('health-overall');
            if (healthOverallEl) {
                healthOverallEl.textContent = d.pending || d.stale ? '待采集' : d.overall === 'healthy' ? '健康' : d.overall === 'degraded' ? '警告' : '异常';
            }

            const renderHealthItems = (checksMap) => {
                const container = document.getElementById('health-details');
                if (!container || !checksMap) return;

                const getIcon = (status) => {
                    if (status === 'pass') return { cls: 'pass', symbol: '✓' };
                    if (status === 'warn') return { cls: 'warn', symbol: '!' };
                    return { cls: 'fail', symbol: '✗' };
                };

                const labelMap = {
                    service: '服务进程',
                    config: '配置文件',
                    disk: '磁盘空间',
                    memory: '内存使用',
                    auth: '认证文件',
                    api_port: 'API端口',
                    management_key: '管理密钥',
                    usage_statistics: '用量统计'
                };

                const order = ['service', 'config', 'usage_statistics', 'disk', 'memory', 'auth', 'api_port', 'management_key'];
                const keys = Object.keys(checksMap);
                const ordered = order.filter(k => keys.includes(k)).concat(keys.filter(k => !order.includes(k)));

                container.innerHTML = ordered.map((key) => {
                    const check = checksMap[key];
                    if (!check) return '';
                    const icon = getIcon(check.status);
                    const label = escapeHtml(labelMap[key] || check.name || key);
                    const color = check.status === 'pass' ? 'var(--success)' : check.status === 'warn' ? 'var(--warning)' : 'var(--danger)';
                    const title = escapeHtml(check.message || (check.status === 'pass' ? '正常' : '异常'));
                    return `
                        <div class="health-item" title="${title}">
                            <span class="health-name">${label}</span>
                            <span class="health-mark" style="color:${color}">${icon.symbol}</span>
                        </div>
                    `;
                }).join('');
            };

            // 优先使用后端 checks_map
            if (d.checks_map) {
                renderHealthItems(d.checks_map);
                inflight.runHealthCheck = false;
                return;
            }

            // 兼容旧结构：从 checks 数组生成
            if (Array.isArray(d.checks)) {
                const fallbackMap = {};
                d.checks.forEach((check) => {
                    if (!check || !check.name) return;
                    const nameKey = check.name;
                    if (nameKey.includes('服务')) fallbackMap.service = check;
                    else if (nameKey.includes('配置')) fallbackMap.config = check;
                    else if (nameKey.includes('磁盘')) fallbackMap.disk = check;
                    else if (nameKey.includes('内存')) fallbackMap.memory = check;
                    else if (nameKey.includes('认证')) fallbackMap.auth = check;
                    else if (nameKey.includes('API') || nameKey.includes('端口')) fallbackMap.api_port = check;
                });
                if (Object.keys(fallbackMap).length > 0) {
                    renderHealthItems(fallbackMap);
                }
            }

            inflight.runHealthCheck = false;
        }

        function clearLogDisplay() {
            logDisplayClearedAt = Date.now();
            followLogs = true;
            renderCachedLogs();
            toast('已清空当前显示，原始日志仍安全保留', 'success');
        }

        // 解析服务器时间并转换为本地时间
        function parseServerTime(timeStr) {
            if (!timeStr) return new Date();
            // 服务器返回UTC时间（带Z后缀），浏览器会自动转换为本地时间
            let date = new Date(timeStr);
            if (!isNaN(date.getTime())) return date;
            return new Date();
        }

        async function refreshCliLogs() {
            if (inflight.refreshCliLogs) return;
            inflight.refreshCliLogs = true;
            try {
                const d = await api('/api/cliproxy-logs');
                if (!d || !Array.isArray(d.logs)) return;
                cachedLogs = d.logs.slice(-200);
                if (followLogs) renderCachedLogs();
                else document.getElementById('log-follow').textContent = '已暂停 · 跟随最新';
            } finally {
                inflight.refreshCliLogs = false;
            }
        }

        function resumeLogFollow() {
            followLogs = true;
            renderCachedLogs();
            const el = document.getElementById('cli-logs');
            el.scrollTop = el.scrollHeight;
            document.getElementById('log-follow').textContent = '跟随最新';
        }

        function renderCachedLogs() {
            const el = document.getElementById('cli-logs');
            const query = document.getElementById('log-search').value.trim().toLowerCase();
            const filteredLogs = cachedLogs.filter((l) => {
                const timestamp = Date.parse(l.time);
                return !shouldHideLog(l.message) && (!logDisplayClearedAt || timestamp > logDisplayClearedAt)
                    && String(l.message).toLowerCase().includes(query);
            }).slice(-80);
            const renderKey = JSON.stringify([filteredLogs,query,logDisplayClearedAt]);
            if (renderKey === lastLogsKey) return;
            lastLogsKey = renderKey;
            if (!filteredLogs.length) {
                setHtml(el, '<div class="log-entry"><span class="log-msg">暂无匹配日志（清空只影响当前浏览器显示）</span></div>');
                return;
            }
            const html = filteredLogs.map(l => {
                // 解析状态码来决定颜色
                let logClass = '';
                let statusHtml = '';
                const statusMatch = l.message.match(/\[gin_logger\.go:\d+\]\s+([1-5]\d{2})\s*\|/)
                    || l.message.match(/\b([1-5]\d{2})\b/);
                if (statusMatch) {
                    const code = parseInt(statusMatch[1]);
                    if (code >= 200 && code < 300) {
                        logClass = 'log-success';
                        statusHtml = `<span class="log-status">[${code}]</span> `;
                    } else if (code >= 400) {
                        logClass = 'log-error';
                        statusHtml = `<span class="log-status">[${code}]</span> `;
                    } else if (code >= 300) {
                        logClass = 'log-warning';
                        statusHtml = `<span class="log-status">[${code}]</span> `;
                    }
                }
                // 检查是否有错误关键词
                if (!logClass && /error|fail|panic|fatal/i.test(l.message)) {
                    logClass = 'log-error';
                } else if (!logClass && /warn|warning/i.test(l.message)) {
                    logClass = 'log-warning';
                }

                const localTime = parseServerTime(l.time);
                return `<div class="log-entry ${logClass}"><span class="log-time">${localTime.toLocaleTimeString()}</span>${statusHtml}<span class="log-msg">${escapeHtml(l.message)}</span></div>`;
            }).join('');
            setHtml(el, html);
            if (followLogs) el.scrollTop = el.scrollHeight;
        }

        async function getRouting() {
            const d = await api('/api/config/routing');
            if (d && d.success) {
                document.getElementById('current-routing').textContent = d.strategy;
                document.getElementById('routing-select').value = d.strategy;
            }
        }

        function escapeHtml(t) {
            return String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        }

        function renderAutoTime(text) {
            return `<span class="time-emphasis">${escapeHtml(text)}</span>`;
        }

        function formatHoursAgoCn(hoursAgo, compact = false) {
            if (hoursAgo === null || typeof hoursAgo !== 'number' || Number.isNaN(hoursAgo)) return '-';
            if (hoursAgo < 1) return Math.round(hoursAgo * 60) + (compact ? '分钟前' : ' 分钟前');
            if (hoursAgo < 24) return hoursAgo.toFixed(1) + (compact ? '小时前' : ' 小时前');
            return (hoursAgo / 24).toFixed(1) + (compact ? '天前' : ' 天前');
        }

        function refreshAll() {
            refreshStatus();
            refreshCliLogs();
            refreshResources();
            refreshUpdateHistory();
        }

        // 获取更新历史
        async function refreshUpdateHistory() {
            if (inflight.refreshUpdateHistory) return;
            inflight.refreshUpdateHistory = true;
            const lastUpdateEl = document.getElementById('last-update-time');
            const recentUpdatesEl = document.getElementById('recent-updates');
            const d = await api('/api/update-history');
            if (!d || !d.success) {
                lastUpdateEl.innerHTML = '<span class="update-history-label">上次更新</span><span class="update-history-empty">暂无记录</span>';
                recentUpdatesEl.innerHTML = '';
                inflight.refreshUpdateHistory = false;
                return;
            }

            const history = d.history || [];
            if (history.length === 0) {
                lastUpdateEl.innerHTML = '<span class="update-history-label">上次更新</span><span class="update-history-empty">暂无记录</span>';
                recentUpdatesEl.innerHTML = '';
                inflight.refreshUpdateHistory = false;
                return;
            }

            const lastUpdate = history[history.length - 1];
            const lastTime = formatHoursAgoCn(lastUpdate.hours_ago);
            lastUpdateEl.innerHTML = `
                <span class="update-history-label">上次更新</span>
                <span class="update-history-main-time">${escapeHtml(lastTime)}</span>
                <span class="update-history-main-version">${escapeHtml(lastUpdate.version || '-')}</span>
            `;

            const recent = history.slice(-6).reverse();
            if (recent.length > 1) {
                const recentItems = recent.slice(1).map((entry) => {
                    const timeText = formatHoursAgoCn(entry.hours_ago, true);
                    return `
                        <div class="update-history-item">
                            <span class="update-history-dot"></span>
                            <span class="update-history-item-version">${escapeHtml(entry.version || '-')}</span>
                            <span class="update-history-item-time">${escapeHtml(timeText)}</span>
                        </div>
                    `;
                }).join('');
                recentUpdatesEl.innerHTML = `
                    <div class="update-history-section-title">历史记录</div>
                    <div class="update-history-list">${recentItems}</div>
                `;
            } else {
                recentUpdatesEl.innerHTML = '<div class="update-history-empty">历史记录暂时只有这一条</div>';
            }

            inflight.refreshUpdateHistory = false;
        }

        // 操作函数
        async function serviceAction(action) {
            const labels = { start: '启动', stop: '停止', restart: '重启' };
            if (!capabilities.service_control) return;
            if (action !== 'start' && !confirm(`确认${labels[action]}上游服务？当前请求可能中断。`)) return;
            const r = await api(`/api/service/${action}`, { method: 'POST' });
            toast(r?.success ? `${labels[action]}成功` : `${labels[action]}失败`, r?.success ? 'success' : 'error');
            refreshStatus();
        }

        async function checkUpdate() {
            const r = await api('/api/check-update');
            if (r?.checking) { toast('版本检查已排队，后台完成后将自动显示', 'success'); return; }
            if (!r || !r.latest || r.latest === 'unknown') {
                toast('暂时无法获取上游版本，请检查 GitHub 网络连接', 'error');
                return;
            }
            toast(r.has_update ? `发现新版本: ${r.latest}` : '检查完成，未确认有可升级版本', r.has_update ? 'warning' : 'success');
            refreshStatus();
        }

        async function triggerUpdate(force) {
            if (!capabilities.binary_update) { toast('当前为监控模式，请在上游部署端升级', 'warning'); return; }
            if (!confirm(force ? '强制升级会重启上游并跳过空闲等待，确认继续？' : '更新可能中断上游请求，确认继续？')) return;
            const r = await api('/api/update', { method: 'POST', body: JSON.stringify({ force }) });
            if (!r) {
                toast('更新请求失败，请检查面板连接', 'error');
                return;
            }
            toast(r.message || (r.success ? '更新请求已发送' : '更新未启动'), r.success ? 'success' : 'warning');
            setTimeout(refreshStatus, 3000);
        }

	        async function toggleAutoUpdate() {
	            const sw = document.getElementById('auto-switch');
	            const on = sw.classList.contains('on');
	            const r = await api('/api/config/auto-update', { method: 'POST', body: JSON.stringify({ enabled: !on }) });
	            if (r?.success) {
	                const enabled = typeof r.auto_update_enabled === 'boolean' ? r.auto_update_enabled : !on;
	                setSwitchState(sw, enabled);
	                document.getElementById('auto-label').textContent = enabled ? '已开启' : '已关闭';
	            }
	        }

	        async function togglePricingAuto() {
	            const sw = document.getElementById('pricing-auto-switch');
	            if (!sw) return;
	            const on = sw.classList.contains('on');
	            const r = await api('/api/config/pricing-auto', { method: 'POST', body: JSON.stringify({ enabled: !on }) });
	            if (r?.success) {
	                const enabled = typeof r.pricing_auto_enabled === 'boolean' ? r.pricing_auto_enabled : !on;
	                setSwitchState(sw, enabled);
	                const label = document.getElementById('pricing-auto-label');
	                if (label) label.textContent = enabled ? '已开启' : '已关闭';
	                // 切换后立即刷新，让价格与来源信息同步
	                refreshStatus();
	            } else {
	                toast('切换失败', 'error');
	            }
	        }

        async function saveUpdateSettings() {
            const idleMinutes = parseInt(document.getElementById('idle-input').value);
            const checkMinutes = parseInt(document.getElementById('check-interval-input').value);

            if (isNaN(idleMinutes) || idleMinutes < 1) {
                toast('空闲阈值必须大于等于1分钟', 'error');
                return;
            }
            if (isNaN(checkMinutes) || checkMinutes < 1) {
                toast('检查间隔必须大于等于1分钟', 'error');
                return;
            }

            const idleSeconds = idleMinutes * 60;
            const checkSeconds = checkMinutes * 60;

            // 保存空闲阈值
            const r1 = await api('/api/config/idle-threshold', {
                method: 'POST',
                body: JSON.stringify({ threshold: idleSeconds })
            });

            // 保存检查间隔
            const r2 = await api('/api/config/check-interval', {
                method: 'POST',
                body: JSON.stringify({ interval: checkSeconds })
            });

            const idleSaved = r1?.success;
            const checkSaved = r2?.success;

            if (idleSaved && typeof r1?.idle_threshold === 'number') {
                document.getElementById('idle-input').value = Math.round(r1.idle_threshold / 60);
            }
            if (checkSaved && typeof r2?.check_interval === 'number') {
                document.getElementById('check-interval-input').value = Math.round(r2.check_interval / 60);
            }

            if (idleSaved && checkSaved) {
                updateSettingsDirty = false;
                toast('设置已保存', 'success');
            } else if (idleSaved || checkSaved) {
                const reason = (!idleSaved ? (r1?.error || r1?.message || '空闲阈值保存失败') : '')
                    + (!checkSaved ? ((idleSaved ? '；' : '') + (r2?.error || r2?.message || '检查间隔保存失败')) : '');
                toast('部分设置已保存: ' + reason, 'warning');
            } else {
                toast('保存失败: ' + (r1?.error || r2?.error || r1?.message || r2?.message || '未知错误'), 'error');
            }

            refreshStatus();
        }

        async function setRouting() {
            if (!configWriteEnabled) {
                toast('当前面板已禁用配置写入，路由策略不会再写回线上配置', 'warning');
                return;
            }
            const strategy = document.getElementById('routing-select').value;
            const r = await api('/api/config/routing', { method: 'POST', body: JSON.stringify({ strategy }) });
            toast(r?.success ? r.message : '设置失败', r?.success ? 'success' : 'error');
            getRouting();
        }

        async function reloadConfig() {
            const r = await api('/api/config/reload', { method: 'POST' });
            toast(r?.success ? r.message : '重载失败: ' + (r?.message || r?.error), r?.success ? 'success' : 'error');
            refreshStatus();
        }

        async function validateConfig() {
            const r = await api('/api/config/validate', { method: 'POST', body: JSON.stringify({}) });
            showValidateResult(r);
        }

        async function validateConfigContent() {
            const content = document.getElementById('config-editor').value;
            const r = await api('/api/config/validate', { method: 'POST', body: JSON.stringify({ content }) });
            showValidateResult(r);
        }

        function showValidateResult(r) {
            const el = document.getElementById('validate-result');
            if (!r) {
                el.innerHTML = '<div style="color:var(--danger);">验证请求失败</div>';
            } else if (r.valid) {
                el.innerHTML = `<div style="color:var(--success);font-size:18px;text-align:center;padding:20px;">✓ 配置格式有效</div>` +
                    ((r.warnings || []).length ? `<div style="margin-top:10px;"><div style="color:var(--warning);font-weight:600;">警告:</div>${r.warnings.map(w => `<div style="font-size:12px;margin-top:4px;">• ${escapeHtml(w)}</div>`).join('')}</div>` : '');
            } else {
                el.innerHTML = `<div style="color:var(--danger);font-size:18px;text-align:center;padding:20px;">✗ 配置格式无效</div>` +
                    `<div style="margin-top:10px;"><div style="color:var(--danger);font-weight:600;">错误:</div>${(r.errors || []).map(e => `<div style="font-size:12px;margin-top:4px;">• ${escapeHtml(e)}</div>`).join('')}</div>`;
            }
            document.getElementById('validate-modal').classList.add('active');
        }

        function closeValidateModal(e) {
            if (e && e.target !== e.currentTarget) return;
            document.getElementById('validate-modal').classList.remove('active');
        }

        async function loadConfig() {
            const r = await api('/api/config');
            if (r?.success) {
                const editor = document.getElementById('config-editor');
                if (editor) {
                    editor.value = r.content;
                    editor.readOnly = !configWriteEnabled;
                }
                document.getElementById('config-modal').classList.add('active');
                if (!configWriteEnabled) {
                    toast('当前配置区为只读模式，只能查看，不能写回线上配置', 'warning');
                }
            } else {
                toast('加载配置失败', 'error');
            }
        }

        async function saveConfig() {
            if (!configWriteEnabled) {
                toast('当前面板已禁用配置写入，不会修改线上配置', 'warning');
                return;
            }
            const content = document.getElementById('config-editor').value;
            if (!content.trim()) { toast('配置内容为空', 'error'); return; }
            const r = await api('/api/config', { method: 'POST', body: JSON.stringify({ content }) });
            toast(r?.success ? '保存成功，请重载配置' : '保存失败', r?.success ? 'success' : 'error');
            if (r?.success) closeModal();
        }

        function closeModal(e) {
            if (e && e.target !== e.currentTarget) return;
            document.getElementById('config-modal').classList.remove('active');
        }

        // Toast
        function toast(msg, type = 'success') {
            const t = document.createElement('div');
            t.className = 'toast ' + type;
            t.textContent = msg;
            document.body.appendChild(t);
            requestAnimationFrame(() => t.classList.add('show'));
            setTimeout(() => {
                t.classList.remove('show');
                setTimeout(() => t.remove(), 200);
            }, 3000);
        }

	        // 初始化
	        initPanelKeyFromURL();
	        initTheme();
	        initLogFilter();
	        initQuote();
	        initPricingInputs();
	        initUpdateSettingsInputs();
        document.getElementById('cli-logs').addEventListener('scroll', () => {
            const el = document.getElementById('cli-logs');
            followLogs = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
            document.getElementById('log-follow').textContent = followLogs ? '跟随最新' : '已暂停 · 跟随最新';
        }, {passive:true});
        refreshAll();
        // 页面隐藏时暂停轮询，避免后台标签持续消耗服务器和浏览器资源。
        setInterval(() => { if (!document.hidden) refreshStatus(); }, 5000);
        setInterval(() => { if (!document.hidden) refreshResources(); }, 13000);
        setInterval(() => { if (!document.hidden) refreshCliLogs(); }, 11000);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) refreshAll();
        });
