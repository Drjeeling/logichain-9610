/**
 * 出海物流多 Agent 协同系统 - 前端逻辑
 */
const API_BASE = '';
let map = null;
let markers = [];
let polylines = [];
let nodeMap = {};  // 节点名 → {lng, lat, type}
let currentRoutes = [];
let selectedRouteIdx = 0;
let lastPayload = null;  // 上次提交参数，供"确认订舱"复用
let lastRun = null;      // 上次"单据识别即跑"的参数（query + 识别要素），供确认订舱二次调用
let samples = [];        // 示例单据清单
let lastBanner = '';     // 识别结果横幅，确认订舱后仍保留展示
let lastQuery = '';      // 最近一次业务需求，供"依据与解释"检索复用

// 口岸配色（散点/条形图共用）
const PORT_COLORS = {
    '宁波港': '#2dd4bf', '上海港': '#60a5fa', '深圳港': '#a78bfa',
    '广州港': '#4ade80', '义乌港': '#fbbf24', '青岛港': '#f472b6',
};
function portColor(p) { return PORT_COLORS[p] || '#8eaed0'; }

// ---------- 初始化 ----------
document.addEventListener('DOMContentLoaded', () => {
    initMap();
    loadNodes();
    loadSamples();
    bindEvents();
    setStatus('online', '系统就绪');
});

function initMap() {
    map = new AMap.Map('mapContainer', {
        zoom: 5,
        center: [114, 32],
        mapStyle: 'amap://styles/dark',  // 深色主题
        viewMode: '2D',
        features: ['bg', 'road', 'building'],  // 简化要素
    });
    map.addControl(new AMap.Scale());
    map.addControl(new AMap.ToolBar({ position: 'RB' }));
}

async function loadNodes() {
    try {
        const res = await fetch('/api/nodes');
        const data = await res.json();
        data.nodes.forEach(n => {
            nodeMap[n.id] = { lng: parseFloat(n.lng), lat: parseFloat(n.lat), type: n.type, name: n.name };
        });
        // 填充城市建议
        const origins = data.nodes.filter(n => n.type === 'origin').map(n => n.id);
        const dests = data.nodes.filter(n => n.type === 'sea_port' || n.type === 'river_port' || n.type === 'airport').map(n => n.id);
        fillDatalist('cityList', origins);
        fillDatalist('destList', dests);
    } catch (e) {
        console.error('加载节点失败:', e);
    }
}

function fillDatalist(id, items) {
    const dl = document.getElementById(id);
    dl.innerHTML = items.map(i => `<option value="${i}">`).join('');
}

// ---------- 事件绑定 ----------
function bindEvents() {
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('docFile');

    document.getElementById('dzBrowse').addEventListener('click', e => { e.stopPropagation(); fileInput.click(); });
    dropZone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', e => {
        const f = e.target.files[0];
        if (f) runDocument(f);
        fileInput.value = '';
    });
    // 文件拖拽：dragenter 与 dragover 都必须 preventDefault，
    // 否则 Safari / 部分 WebKit 内核会显示禁止放入图标（红叉）并拒绝 drop。
    dropZone.addEventListener('dragenter', e => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragover', e => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', e => {
        e.preventDefault();
        e.stopPropagation();
        // 仅当真正离开整个拖拽区时才取消高亮，避免经过子元素时闪烁
        if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('dragover');
    });
    dropZone.addEventListener('drop', e => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('dragover');
        const f = e.dataTransfer.files[0];
        if (f) runDocument(f);
    });

    // 兜底：阻止在页面其他区域拖放时浏览器直接打开文件（表现为"没反应"）
    window.addEventListener('dragover', e => {
        if (!dropZone.contains(e.target)) e.preventDefault();
    });
    window.addEventListener('drop', e => {
        if (!dropZone.contains(e.target)) e.preventDefault();
    });

    // 示例单据按钮（事件委托）
    document.getElementById('samplesList').addEventListener('click', e => {
        const btn = e.target.closest('button[data-act="run"]');
        if (btn) runSample(btn.dataset.id, btn.dataset.fmt);
    });

    // 手动提交
    document.getElementById('submitBtn').addEventListener('click', submitQuery);

    // 法规知识库抽屉
    const kbDrawer = document.getElementById('kbDrawer');
    if (kbDrawer) {
        document.getElementById('kbTrigger').addEventListener('click', openKb);
        document.getElementById('kbClose').addEventListener('click', closeKb);
        document.getElementById('kbBackdrop').addEventListener('click', closeKb);
        document.getElementById('kbForm').addEventListener('submit', e => {
            e.preventDefault();
            askKb();
        });
        document.getElementById('kbPresets').addEventListener('click', e => {
            const btn = e.target.closest('button[data-q]');
            if (!btn) return;
            document.getElementById('kbInput').value = btn.dataset.q;
            askKb();
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') closeKb();
        });
    }

    // 复制按钮（订舱指令等，事件委托，兼容动态重渲染）
    document.addEventListener('click', e => {
        const btn = e.target.closest('.copy-btn');
        if (!btn) return;
        const pre = btn.parentElement.querySelector('pre');
        if (!pre) return;
        const text = pre.textContent;
        const done = () => {
            const old = btn.textContent;
            btn.textContent = '已复制';
            setTimeout(() => { btn.textContent = old; }, 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(() => {});
        } else {
            const ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            done();
        }
    });
}

// ---------- 示例单据 ----------
async function loadSamples() {
    try {
        const res = await fetch('/api/scenarios');
        const data = await res.json();
        samples = data.scenarios || [];
        renderSamples();
    } catch (e) {
        console.error('加载示例单据失败:', e);
        document.getElementById('samplesList').innerHTML = '<div class="samples-loading">示例单据加载失败</div>';
    }
}

function renderSamples() {
    const box = document.getElementById('samplesList');
    if (!samples.length) { box.innerHTML = '<div class="samples-loading">暂无示例单据</div>'; return; }
    box.innerHTML = samples.map(s => `
        <div class="sample-card">
            <div class="sc-head">
                <span class="sc-badge">${s.name}</span>
                <span class="sc-risk">${s.risk_tag || ''}</span>
            </div>
            <a class="sc-thumb-wrap" href="${s.png_url}" target="_blank" rel="noopener" title="点击查看单据原图">
                <img class="sc-thumb" src="${s.png_url}" alt="${s.name} 单据原图" loading="lazy">
            </a>
            <div class="sc-sub">${s.subtitle}</div>
            <div class="sc-meta">${s.seller} <span class="sc-arrow">→</span> ${s.buyer}</div>
            <div class="sc-meta dim">${s.doc_type} ${s.invoice_no}｜毛重 ${s.gross_weight}｜USD ${Number(s.total_amount).toLocaleString()}</div>
            <div class="sc-actions">
                <a class="btn-mini ghosty" href="${s.png_url}" download>下载单据</a>
                <a class="btn-mini ghosty" href="${s.png_url}" target="_blank" rel="noopener">预览</a>
                <button class="btn-mini primary" data-act="run" data-id="${s.id}" data-fmt="png">识别并跑 ▶</button>
            </div>
        </div>`).join('') +
        `<div class="samples-note">结构化电子单据（JSON）直接解析：
            ${samples.map(s => `<button class="link-btn" data-act="run" data-id="${s.id}" data-fmt="json">${s.id}</button>`).join(' · ')}
        </div>`;
}

async function runSample(id, fmt) {
    const s = samples.find(x => x.id === id);
    if (!s) return;
    const url = fmt === 'json' ? s.json_url : s.png_url;
    try {
        setStatus('loading', '读取示例单据...');
        const res = await fetch(url);
        const blob = await res.blob();
        const name = url.split('/').pop();
        await runDocument(new File([blob], name, { type: blob.type || 'application/octet-stream' }));
    } catch (e) {
        console.error(e);
        showError('示例单据加载失败：' + e.message);
        setStatus('error', '执行失败');
    }
}

// ---------- 核心：甩入单据 → 识别 → 全链路自动开跑 ----------
async function runDocument(file) {
    if (!file) return;
    setLoading(true);
    setStatus('loading', `识别单据 ${file.name} 中...`);
    lastPayload = null;
    try {
        const fd = new FormData();
        fd.append('file', file);
        fd.append('interactive', 'true');
        const res = await fetch('/api/recognize_and_dispatch', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.detail) throw new Error(data.detail);

        // 记住识别结果，供"确认订舱"阶段二次调用（无需重传文件）
        lastRun = { query: data.query, doc_fields: data.recognized, interactive: true };
        lastPayload = { query: data.query, month: data.recognized.month || 11, doc_fields: data.recognized };
        lastQuery = data.query;

        const banner = buildRecognizedBanner(data.recognized, data.query, file.name);
        lastBanner = banner;
        renderResult(data.dispatch, banner);
        const mode = (data.dispatch.intent && data.dispatch.intent.mode) === 'pipeline' ? '4 Agent 协同' : '单 Agent';
        setStatus('online', `已识别单据并跑通全链路（${mode}）`);
    } catch (e) {
        console.error(e);
        showError(e.message);
        setStatus('error', '识别失败');
    } finally {
        setLoading(false);
    }
}

function buildRecognizedBanner(f, query, filename) {
    const prio = { cost: '省钱', time: '最快', balanced: '折中' }[f.priority] || '';
    const chips = [
        ['单据', f.doc_type], ['起运地', f.origin], ['目的港', f.dest], ['目的国', f.dest_country],
        ['货名', f.cargo_name], ['货量', f.tonnage ? f.tonnage + ' 吨' : ''],
        ['单价', f.unit_price_usd ? '$' + f.unit_price_usd : ''],
        ['贸易条款', f.trade_terms], ['发运月份', f.month ? f.month + '月' : ''],
        ['优先级', prio], ['特殊属性', f.special_notes],
        ['AI置信度', Math.round((f.confidence || 0) * 100) + '%'],
    ].filter(x => x[1] !== '' && x[1] != null);
    return `
      <div class="recognized">
        <div class="rec-head">
          <span class="rec-title">单据识别结果</span>
          <span class="rec-file">${filename}</span>
          <span class="rec-src">${(f.evidence || []).join(' · ')}</span>
        </div>
        <div class="rec-chips">${chips.map(c => `<span class="rec-chip"><i>${c[0]}</i>${c[1]}</span>`).join('')}</div>
        <div class="rec-query">自动生成需求 <code>${query}</code></div>
      </div>`;
}

// ---------- 提交查询（手动模式）----------
async function submitQuery() {
    const origin = document.getElementById('originInput').value.trim();
    const dest = document.getElementById('destInput').value.trim();
    if (!origin || !dest) { alert('请填写起运地和目的港'); return; }
    const weight = document.getElementById('weightInput').value || 20;
    const month = document.getElementById('monthSelect').value;
    const priority = document.querySelector('input[name=priority]:checked').value;
    const extra = document.getElementById('extraInput').value.trim();
    const priorityMap = { cost: '省钱', time: '最快', balanced: '折中' };
    const query = `${origin}发${weight}吨${extra || '货物'}到${dest}，${month}月，${priorityMap[priority]}优先，全流程`;

    setLoading(true);
    lastRun = null;
    lastPayload = { query, month };
    lastQuery = query;
    try {
        const res = await fetch('/api/dispatch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query, month: parseInt(month), interactive: true }),
        });
        const data = await res.json();
        if (data.detail) throw new Error(data.detail);
        renderResult(data);
        setStatus('online', '执行完成');
    } catch (e) {
        console.error(e);
        showError(e.message);
        setStatus('error', '执行失败');
    } finally {
        setLoading(false);
    }
}

function setLoading(loading) {
    const btn = document.getElementById('submitBtn');
    btn.disabled = loading;
    btn.querySelector('.btn-text').style.display = loading ? 'none' : 'inline';
    btn.querySelector('.btn-spinner').style.display = loading ? 'inline-block' : 'none';
    if (loading) setStatus('loading', 'Agent 执行中...');
}

function setStatus(state, text) {
    const dot = document.getElementById('statusDot');
    const txt = document.getElementById('statusText');
    dot.className = 'status-dot ' + state;
    txt.textContent = text;
}

// ---------- 渲染结果 ----------
function renderResult(data, bannerHtml = '') {
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('resultContent').style.display = 'block';
    const content = document.getElementById('resultContent');

    // 找 transport agent 的结果，提取路线
    const results = data.results || [];
    const transportResult = results.find(r => r.agent === 'transport' || (r.data && (r.data.all_routes || r.data.routes)));

    let routes = [];
    if (transportResult && transportResult.data) {
        // 优先用 all_routes（完整候选），其次 recommended（帕累托前沿）
        routes = transportResult.data.all_routes || transportResult.data.routes || transportResult.data.recommended || [];
    }
    // 把路径字符串解析成 segments 格式（便于画地图）
    routes = routes.map(r => normalizeRoute(r));
    currentRoutes = routes;

    // 渲染方案卡片
    let html = '';
    if (routes.length > 0) {
        html += '<div class="routes-list">';
        routes.forEach((route, idx) => {
            html += renderRouteCard(route, idx);
        });
        html += '</div>';
    }

    // 渲染各 agent 输出
    html += '<div class="agent-outputs">';
    results.forEach(r => {
        html += renderAgentOutput(r);
    });
    html += '</div>';

    // 报关域命中时，追加「依据与解释」（异步检索，不阻塞主流程）
    const hasCustoms = results.some(r => r.agent === 'customs');
    html += '<div class="kb-inline" id="kbInline" style="display:none;"></div>';

    content.innerHTML = bannerHtml + html;
    if (hasCustoms) loadKbEvidence(lastQuery);

    // 绑定卡片点击
    document.querySelectorAll('.route-card').forEach(card => {
        card.addEventListener('click', () => {
            const idx = parseInt(card.dataset.idx);
            selectRoute(idx);
        });
    });

    // 绑定订舱方案选择（交互闸门阶段1 → 阶段2）
    document.querySelectorAll('.booking-option').forEach(card => {
        card.addEventListener('click', () => {
            document.querySelectorAll('.booking-option').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            const btn = document.getElementById('confirmBooking');
            if (btn) {
                btn.disabled = false;
                btn.dataset.option = card.dataset.option;
                btn.textContent = `确认订舱 ${card.dataset.option}`;
            }
        });
    });
    const confirmBtn = document.getElementById('confirmBooking');
    if (confirmBtn) {
        confirmBtn.addEventListener('click', () => confirmBooking(confirmBtn.dataset.option));
    }

    // 默认选第一个
    if (routes.length > 0) {
        selectRoute(0);
    }

    // 渲染 Agent trace
    renderTrace(data.steps || [], data.total_duration || 0);
}

function renderRouteCard(route, idx) {
    const badgeMap = { cheapest: '最省钱', fastest: '最快', balanced: '折中' };
    const label = route.label || route.tag || (idx === 0 ? '推荐' : '');
    const badge = label ? `<span class="route-badge ${label}">${badgeMap[label] || label}</span>` : '';

    // 路径段
    let pathHtml = '';
    if (route.segments && route.segments.length) {
        pathHtml = route.segments.map((seg, i) => {
            const modeColor = getModeColor(seg.mode || seg.transport_mode);
            const modeIcon = getModeIcon(seg.mode || seg.transport_mode);
            const text = `${seg.from} → ${seg.to}`;
            const html = `<span class="route-segment" style="border-left:2px solid ${modeColor}">${modeIcon} ${text}</span>`;
            return html;
        }).join('<span class="route-sep">→</span>');
    } else if (route.path) {
        pathHtml = `<div class="route-path">${route.path}</div>`;
    }

    // 统计
    const cost = route.total_cost || route.cost || 0;
    const hours = route.total_hours || route.hours || 0;
    const carbon = route.total_carbon || route.carbon || 0;
    const transfers = route.transfer_count || (route.segments ? route.segments.length - 1 : 0);

    // 成本明细
    let breakdownHtml = '';
    if (route.breakdown) {
        const b = route.breakdown;
        breakdownHtml = `
        <div class="cost-breakdown">
            <div class="breakdown-row"><span>L1 干线运费</span><span>¥${b.l1_freight || 0}/吨</span></div>
            <div class="breakdown-row"><span>L2 中转衔接</span><span>¥${b.l2_transfer || 0}/吨</span></div>
            <div class="breakdown-row"><span>L3 时间滞期</span><span>¥${b.l3_delay || 0}/吨</span></div>
            <div class="breakdown-row"><span>L4 风险准备金</span><span>¥${b.l4_risk || 0}/吨</span></div>
            <div class="breakdown-row total"><span>总计</span><span>¥${cost}/吨</span></div>
        </div>`;
    }

    return `
    <div class="route-card ${idx === 0 ? 'selected' : ''}" data-idx="${idx}">
        <div class="route-card-head">
            ${badge}
            <span style="font-size:11px;color:var(--text-muted)">方案 ${idx + 1}</span>
        </div>
        <div class="route-path">${pathHtml}</div>
        <div class="route-stats">
            <div class="stat"><div class="stat-label">运费</div><div class="stat-value cost">¥${cost}</div></div>
            <div class="stat"><div class="stat-label">时效</div><div class="stat-value time">${formatHours(hours)}</div></div>
            <div class="stat"><div class="stat-label">中转</div><div class="stat-value">${transfers}</div></div>
            <div class="stat"><div class="stat-label">碳排放</div><div class="stat-value carbon">${Math.round(carbon)}g</div></div>
        </div>
        ${breakdownHtml}
    </div>`;
}

function renderAgentOutput(result) {
    const agent = result.agent || 'unknown';
    const summary = result.summary || '';
    const status = result.status || 'success';
    const data = result.data || {};

    let bodyHtml = '';
    let vizHtml = '';

    if (agent === 'booking' && Array.isArray(data.options) && data.options.length) {
        vizHtml = renderBookingViz(data);
    } else if (agent === 'customs' && (data.tier || data.risk_flags || data.agent_slots)) {
        vizHtml = renderCustomsViz(data);
    } else if (agent === 'tracking' && (data.tracking_no || data.state || data.tracking)) {
        vizHtml = renderTrackingViz(data);
    }

    // 可视化结果 → 分层 AI 建议 → 折叠的计算过程
    bodyHtml = vizHtml + renderAIAdvice(data, agent) + wrapProcess(summary);

    // 兜底分支
    if (!vizHtml && !summary) {
        if (data.declaration) {
            bodyHtml = wrapProcess(data.declaration);
        } else if (data.recommendation) {
            bodyHtml = `<div>${data.recommendation.replace(/\n/g, '<br>')}</div>`;
        } else if (data.risks && data.risks.length) {
            bodyHtml = data.risks.map(r => `<div>⚠ ${r.message || r}</div>`).join('');
        }
    }

    return `
    <div class="agent-output ${agent} ${status === 'error' ? 'error' : ''}">
        <div class="agent-output-head">
            <span class="agent-name">${agent}</span>
            ${status === 'error' ? '<span style="color:var(--danger);font-size:10px">ERROR</span>' : ''}
        </div>
        <div class="agent-output-body">${bodyHtml}</div>
    </div>`;
}

// 折叠的计算过程（小箭头控制展开/收起）
function wrapProcess(summary) {
    if (!summary) return '';
    return `<details class="ai-process">
        <summary>
            <span class="ai-arrow">▶</span>
            <span class="ai-process-title">计算过程</span>
            <span class="ai-process-hint">展开查看完整推理</span>
        </summary>
        <pre>${summary}</pre>
    </details>`;
}

// AI 建议分层展示（推荐理由 / 合规链 / 归类推理 / 合规问题 / 追踪建议）
function renderAIAdvice(data, agent) {
    if (agent === 'booking') return renderBookingAdvice(data);
    if (agent === 'customs') return renderCustomsAdvice(data);
    if (agent === 'tracking') return renderTrackingAdvice(data);
    return '';
}

function renderBookingAdvice(data) {
    let html = '<div class="ai-advice">';

    // ① 推荐理由
    if (data.recommended_reason) {
        html += `<div class="ai-rec ai-rec-booking">
            <div class="ai-rec-label">🎯 推荐理由</div>
            <div class="ai-rec-text">${data.recommended_reason}</div>
        </div>`;
    }

    // ② 合规决策链（风险 → 口岸 → 航线 → 单证 → 申报策略）
    if (Array.isArray(data.compliance_chain) && data.compliance_chain.length) {
        html += '<div class="ai-rec ai-rec-booking"><div class="ai-rec-label">🛡 合规决策链</div><ol class="ai-chain">';
        data.compliance_chain.forEach(s => {
            html += `<li><b>${s.step}</b>：${s.detail}<br><span class="ai-chain-decision">→ ${s.decision}</span></li>`;
        });
        html += '</ol></div>';
    }

    // ③ 反事实合规优化（补齐单证 → 解锁更优方案）：booking 的差异化亮点
    if (Array.isArray(data.counterfactual) && data.counterfactual.length) {
        html += '<div class="ai-rec ai-rec-booking"><div class="ai-rec-label">🔓 反事实合规优化（补齐单证可解锁更优方案）</div><div class="cf-list">';
        data.counterfactual.forEach(c => {
            html += `<div class="cf-card">
                <div class="cf-risk">⚠ ${c.risk}</div>
                <div class="cf-remedy">补救：${(c.remediation || '').replace(/^补救：/, '')}</div>
                <div class="cf-benefit">→ ${c.benefit}</div>
            </div>`;
        });
        html += '</div></div>';
    }

    // ④ 议价与守利润（确认方案后生成）
    if (data.negotiation) {
        const n = data.negotiation;
        html += '<div class="ai-rec ai-rec-booking"><div class="ai-rec-label">💬 AI 议价与守利润建议</div>';
        html += '<div class="neg-path">';
        html += `<div class="neg-node"><span class="neg-node-label">开局锚点</span><b>$${n.opening}</b></div>`;
        html += '<span class="neg-arrow">→</span>';
        html += `<div class="neg-node target"><span class="neg-node-label">目标价</span><b>$${n.target}</b></div>`;
        html += '<span class="neg-arrow">→</span>';
        html += `<div class="neg-node ceiling"><span class="neg-node-label">守利润底线</span><b>$${n.ceiling}</b></div>`;
        html += '</div>';
        html += `<div class="neg-meta">市场区间 <b>$${n.market_low}–$${n.market_high}</b>｜当前推荐 <b>$${n.current}</b>｜可比船司 ${n.peer_count} 家</div>`;
        if (n.saving_vs_current != null) {
            html += `<div class="neg-save">潜在节省：相对当前推荐 <b>$${n.saving_vs_current}</b>/40ft｜相对区间最高价 <b>$${n.saving_vs_high}</b>/40ft</div>`;
        }
        if (Array.isArray(n.levers) && n.levers.length) {
            html += '<div class="neg-levers"><span class="neg-sub">谈判筹码：</span>' + n.levers.map(l => `<span class="neg-lever">${l}</span>`).join('') + '</div>';
        }
        if (n.script) {
            html += `<details class="ai-rfq" open><summary><span class="ai-arrow">▶</span> 议价话术（可直接发送）</summary><pre>${n.script}</pre></details>`;
        }
        html += '</div>';
    }

    // ⑤ 订舱指令（最终交付物）
    if (data.booking_order) {
        html += `<div class="ai-rec ai-rec-booking"><div class="ai-rec-label">📄 订舱指令 ${data.stage === 'confirmed' ? '（已确认）' : '（拟订）'}</div>
            <div class="bo-order-wrap">
                <button class="copy-btn" data-copy="booking-order">复制</button>
                <pre class="bo-order">${data.booking_order}</pre>
            </div></div>`;
    }

    // ⑥ 询价单 RFQ（可折叠）
    if (data.rfq_text) {
        html += `<details class="ai-rfq">
            <summary><span class="ai-arrow">▶</span> 询价单 RFQ（点击展开）</summary>
            <pre>${data.rfq_text}</pre>
        </details>`;
    }

    // ⑦ AI 边界声明
    if (data.ai_boundary) {
        html += `<div class="ai-boundary">ℹ ${data.ai_boundary}</div>`;
    }

    html += '</div>';
    return html;
}

function renderCustomsAdvice(data) {
    let html = '<div class="ai-advice">';
    if (Array.isArray(data.compliance_chain) && data.compliance_chain.length) {
        html += '<div class="ai-rec ai-rec-customs"><div class="ai-rec-label">🛡 合规决策链</div><ol class="ai-chain">';
        data.compliance_chain.forEach(s => {
            html += `<li><b>${s.step}</b>：${s.detail}<br><span class="ai-chain-decision">→ ${s.decision}</span></li>`;
        });
        html += '</ol></div>';
    }
    if (data.hs_code && Array.isArray(data.hs_code.reasoning_chain)) {
        html += '<div class="ai-rec ai-rec-customs"><div class="ai-rec-label">🔍 归类推理链</div><ol class="ai-chain">';
        data.hs_code.reasoning_chain.forEach(step => {
            html += `<li>${step}</li>`;
        });
        html += '</ol></div>';
    }
    // 字段缺口自主分析（缺什么 / 为何缺 / 自解还是提问）
    const gaps = data.field_gaps;
    if (gaps && ((gaps.ask_user || []).length || (gaps.self_resolved || []).length)) {
        const ask = gaps.ask_user || [];
        const self = gaps.self_resolved || [];
        html += `<div class="ai-rec ai-rec-customs"><div class="ai-rec-label">🧩 字段缺口自主分析（必填 ${gaps.required_total || 0} / 已具备 ${gaps.filled || 0} / 自解 ${self.length} / 待提问 ${ask.length}）</div>`;
        self.slice(0, 3).forEach(i => {
            html += `<div class="ai-issue"><b>[自解] ${i.field}</b> = ${i.value}（${i.method}）</div>`;
        });
        ask.slice(0, 5).forEach(i => {
            html += `<div class="ai-issue critical"><b>[提问] ${i.field}</b> ${i.question} —— ${i.reason}</div>`;
        });
        html += '</div>';
    }
    if (data.decl_compliance && Array.isArray(data.decl_compliance.issues)) {
        const issues = data.decl_compliance.issues;
        if (issues.length) {
            html += `<div class="ai-rec ai-rec-customs"><div class="ai-rec-label">⚠ 清单合规校验（${data.decl_compliance.critical_count || 0} 严重 / ${data.decl_compliance.warning_count || 0} 警告）</div><div class="ai-issues">`;
            issues.forEach(i => {
                const sev = i.severity === 'critical' ? 'critical' : 'warning';
                const icon = sev === 'critical' ? '🔴' : '🟡';
                html += `<div class="ai-issue ${sev}"><b>[${i.field}]</b> ${i.message}</div>`;
            });
            html += '</div></div>';
        }
    }
    html += '</div>';
    return html;
}

// 报关域：分流等级 + 6 槽位状态 + 风险标记
function renderCustomsViz(data) {
    const tier = data.tier || '—';
    const tierCls = { A: 'tier-a', B: 'tier-b', C: 'tier-c' }[tier] || 'tier-x';
    const slots = data.agent_slots || {};
    const slotOrder = ['classify', 'three_doc', 'decl_compliance', 'risk_screen', 'merge', 'risk_monitor'];
    const slotCn = { classify: '① 归类', three_doc: '② 三单预检', decl_compliance: '③ 清单合规',
                     risk_screen: '④ 风险预判', merge: '⑤ 归并', risk_monitor: '⑥ 节点监控' };
    const slotHtml = slotOrder.map(k => {
        const st = slots[k] || 'absent';
        const cls = st === 'success' ? 'ok' : (st === 'error' ? 'err' : 'idle');
        const mark = st === 'success' ? '✓' : (st === 'error' ? '✗' : '·');
        return `<div class="cs-slot ${cls}"><span class="cs-mark">${mark}</span>${slotCn[k]}</div>`;
    }).join('');

    const flags = data.risk_flags || [];
    const flagHtml = flags.length
        ? flags.map(f => {
            const sev = f.severity === 'critical' ? 'critical' : 'warning';
            const icon = sev === 'critical' ? '🔴' : '🟡';
            return `<div class="cs-risk ${sev}">${icon} <b>[${f.type}]</b> ${f.detail || ''}</div>`;
        }).join('')
        : '<div class="cs-risk none">✓ 未检出风险</div>';

    return `
    <div class="customs-viz">
        <div class="cs-top">
            <div class="cs-tier ${tierCls}">分流等级 <b>${tier}</b></div>
            <div class="cs-status">链路状态：<b>${data.status || '—'}</b></div>
        </div>
        <div class="cs-slots">${slotHtml}</div>
        <div class="cs-risks">${flagHtml}</div>
    </div>`;
}

// 运单状态机（与后端 TrackingAgent.SHIPMENT_STATES 一致，用于进度条）
const SHIPMENT_STATES = ['已揽收', '国内运输中', '报关中', '已装船', '海上运输中',
    '抵达目的港', '清关中', '已放行', '末端派送', '已签收'];

// 追踪域：运单状态进度条 + 状态卡 + 在途异常告警
function renderTrackingViz(data) {
    const state = data.state || '';
    const trackingNo = data.tracking_no || '';
    const events = (data.tracking && (data.tracking.events || data.tracking.timeline)) || [];
    if (!state && !trackingNo && !events.length) return '';

    let html = '<div class="tracking-viz">';

    // 状态进度条
    if (state) {
        const idx = SHIPMENT_STATES.indexOf(state);
        html += '<div class="tk-progress-wrap"><div class="tk-progress">';
        SHIPMENT_STATES.forEach((s, i) => {
            const status = idx < 0 ? 'todo' : (i < idx ? 'done' : (i === idx ? 'current' : 'todo'));
            html += `<div class="tk-step ${status}">
                <span class="tk-dot"></span>
                <span class="tk-step-label">${s}</span>
            </div>`;
        });
        html += '</div></div>';
    }

    // 状态卡
    const eta = data.eta || new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10);
    html += `<div class="tk-status-card">
        <div class="tk-sc-item"><span class="tk-sc-label">运单号</span><span class="tk-sc-no">${trackingNo || '—'}</span></div>
        <div class="tk-sc-item"><span class="tk-sc-label">当前状态</span><span class="tk-sc-state">${state || '—'}</span></div>
        <div class="tk-sc-item"><span class="tk-sc-label">预计到达</span><span class="tk-sc-eta">${eta}</span></div>
    </div>`;

    // 在途异常告警
    if (data.anomaly) {
        html += `<div class="tk-anomaly">⚠️ ${data.anomaly}</div>`;
    }
    if (Array.isArray(data.alerts) && data.alerts.length) {
        html += '<div class="tk-alerts">';
        data.alerts.forEach(a => {
            html += `<div class="tk-alert"><span class="tk-alert-dot"></span>${a}</div>`;
        });
        html += '</div>';
    }

    // 兜底：仅当后端返回事件流时展示
    if (events.length) {
        html += '<div class="tk-events">' + events.slice(0, 6).map(ev => {
            const t = ev.time || ev.date || ev.node || '';
            const desc = ev.desc || ev.event || ev.message || '';
            return `<div class="tk-row"><span class="tk-time">${t}</span><span class="tk-desc">${desc}</span></div>`;
        }).join('') + '</div>';
    }

    html += '</div>';
    return html;
}

// 追踪域建议：税务节点提醒 + 极端天气报告 + 处理建议
function renderTrackingAdvice(data) {
    let html = '<div class="ai-advice">';

    // 税务节点提醒（按紧急度分级）
    if (Array.isArray(data.tax_reminders) && data.tax_reminders.length) {
        html += '<div class="ai-rec ai-rec-tracking"><div class="ai-rec-label">📋 税务节点提醒</div><div class="tax-list">';
        data.tax_reminders.forEach(t => {
            const lvl = t.level === 'urgent' ? 'urgent' : (t.level === 'warning' ? 'warning' : 'info');
            const icon = lvl === 'urgent' ? '🔴' : (lvl === 'warning' ? '🟡' : '🟢');
            const days = t.days_left < 0 ? `已逾期 ${-t.days_left} 天` : `剩余 ${t.days_left} 天`;
            html += `<div class="tax-card ${lvl}">
                <div class="tax-head"><span class="tax-name">${icon} ${t.name}</span><span class="tax-days">${days}</span></div>
                <div class="tax-desc">${t.desc}</div>
                <div class="tax-foot"><span>截止 ${t.deadline}</span><span class="tax-penalty">逾期：${t.penalty}</span></div>
            </div>`;
        });
        html += '</div></div>';
    }

    // 极端天气报告
    if (data.weather_report) {
        html += `<div class="ai-rec ai-rec-tracking"><div class="ai-rec-label">🌦️ 极端天气报告</div>
            <div class="weather-report">${data.weather_report.replace(/\n/g, '<br>')}</div></div>`;
    }

    // 处理建议
    if (Array.isArray(data.suggestions) && data.suggestions.length) {
        html += '<div class="ai-rec ai-rec-tracking"><div class="ai-rec-label">✅ 处理建议</div><div class="sug-list">';
        data.suggestions.forEach(s => {
            html += `<div class="sug-item">${s}</div>`;
        });
        html += '</div></div>';
    }

    html += '</div>';
    return html;
}

function renderTrace(steps, duration) {
    if (!steps.length) {
        document.getElementById('agentTrace').style.display = 'none';
        return;
    }
    document.getElementById('agentTrace').style.display = 'block';
    document.getElementById('traceDuration').textContent = `总耗时 ${duration}s`;

    const html = steps.map((s, i) => {
        const status = s.status || 'success';
        return `
            <div class="trace-step ${status}">
                <span class="trace-step-num">${i + 1}</span>
                <span>${s.agent}</span>
                <span style="color:var(--text-muted)">${s.duration}s</span>
            </div>
            ${i < steps.length - 1 ? '<span class="trace-arrow">→</span>' : ''}
        `;
    }).join('');
    document.getElementById('traceFlow').innerHTML = html;
}

function showError(msg) {
    document.getElementById('resultContent').style.display = 'block';
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('resultContent').innerHTML = `
        <div class="agent-output error">
            <div class="agent-output-head"><span class="agent-name">系统错误</span></div>
            <div class="agent-output-body">${msg}</div>
        </div>`;
}

// ---------- 选中方案 → 画地图 ----------
function selectRoute(idx) {
    selectedRouteIdx = idx;
    document.querySelectorAll('.route-card').forEach((c, i) => {
        c.classList.toggle('selected', i === idx);
    });
    if (currentRoutes[idx]) {
        drawRouteOnMap(currentRoutes[idx]);
    }
}

function drawRouteOnMap(route) {
    clearMapOverlay();

    if (!route.segments || !route.segments.length) {
        return;
    }

    const bounds = [];

    route.segments.forEach((seg, i) => {
        const fromNode = nodeMap[seg.from];
        const toNode = nodeMap[seg.to];
        if (!fromNode || !toNode) return;

        // 起点标记
        if (i === 0) {
            const m = addMarker(fromNode, '起', '#00d4ff');
            bounds.push([fromNode.lng, fromNode.lat]);
        }
        // 终点标记
        const color = i === route.segments.length - 1 ? '#ff5470' : getModeColor(seg.mode);
        const label = i === route.segments.length - 1 ? '终' : '';
        if (label) {
            const m = addMarker(toNode, label, color);
        } else {
            // 中转点
            const m = addMarker(toNode, '●', color, true);
        }
        bounds.push([toNode.lng, toNode.lat]);

        // 画线
        const lineColor = getModeColor(seg.mode);
        const line = new AMap.Polyline({
            path: [[fromNode.lng, fromNode.lat], [toNode.lng, toNode.lat]],
            strokeColor: lineColor,
            strokeWeight: 4,
            strokeOpacity: 0.9,
            strokeStyle: 'solid',
            lineJoin: 'round',
            showDir: true,
        });
        map.add(line);
        polylines.push(line);

        // 线上的信息窗（运费+时效）
        const midLng = (fromNode.lng + toNode.lng) / 2;
        const midLat = (fromNode.lat + toNode.lat) / 2;
        const metrics = [];
        if (seg.distance_km) metrics.push(`${seg.distance_km}km`);
        if (seg.cost_per_ton) metrics.push(`¥${seg.cost_per_ton}/吨`);
        if (seg.hours) metrics.push(formatHours(seg.hours));
        const content = `<div style="background:#0f1e36;color:#e6f0ff;padding:4px 8px;border-radius:4px;font-size:10px;border:1px solid #1f3a5f">
            <div>${getModeIcon(seg.mode)} ${seg.mode}${metrics.length ? ' · ' + metrics.join(' · ') : ''}</div>
        </div>`;
        const infoWindow = new AMap.Marker({
            position: [midLng, midLat],
            content: content,
            offset: new AMap.Pixel(-40, -25),
        });
        map.add(infoWindow);
        polylines.push(infoWindow);
    });

    // 调整视野
    if (bounds.length) {
        map.setFitView(polylines, false, [60, 60, 60, 60]);
    }
}

function addMarker(node, label, color, small = false) {
    const size = small ? 8 : 24;
    const content = small
        ? `<div style="width:10px;height:10px;border-radius:50%;background:${color};border:2px solid #0a1628;box-shadow:0 0 6px ${color}"></div>`
        : `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${color};color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;border:2px solid #fff;box-shadow:0 0 10px ${color}">${label}</div>`;

    const marker = new AMap.Marker({
        position: [node.lng, node.lat],
        content: content,
        offset: new AMap.Pixel(-size / 2, -size / 2),
    });
    map.add(marker);
    markers.push(marker);
    return marker;
}

function clearMapOverlay() {
    markers.forEach(m => map.remove(m));
    polylines.forEach(p => map.remove(p));
    markers = [];
    polylines = [];
}

// ---------- 工具函数 ----------
function normalizeRoute(route) {
    // 把 path/modes 字符串解析成 segments 数组格式
    const normalized = { ...route };
    // 字段映射：tag → label, cost_per_ton → total_cost, hours → total_hours, carbon_g_per_ton → total_carbon
    normalized.label = route.tag || route.label || '';
    normalized.total_cost = route.cost_per_ton || route.total_cost || route.cost || 0;
    normalized.total_hours = route.hours || route.total_hours || 0;
    normalized.total_carbon = route.carbon_g_per_ton || route.total_carbon || route.carbon || 0;
    normalized.breakdown = route.l1_freight ? {
        l1_freight: route.l1_freight,
        l2_transfer: route.l2_transfer || route.l2_transfer_cost || 0,
        l3_delay: route.l3_demurrage || 0,
        l4_risk: route.l4_risk_reserve || 0,
    } : route.breakdown;

    // 解析 path 和 modes
    const pathStr = route.path || '';
    const modesStr = route.modes || '';
    const nodes = pathStr.split('→').map(s => s.trim()).filter(Boolean);
    const modes = modesStr.split('→').map(s => s.trim()).filter(Boolean);

    // 构建 segments
    const segments = [];
    for (let i = 0; i < nodes.length - 1; i++) {
        const modeText = modes[i] || '';
        const modeKey = parseModeKey(modeText);
        segments.push({
            from: nodes[i],
            to: nodes[i + 1],
            mode: modeKey,
            transport_mode: modeKey,
            distance_km: 0,  // 后端没单独返回，留 0
            cost_per_ton: 0,
            hours: 0,
        });
    }
    normalized.segments = segments;
    normalized.transfer_count = Math.max(0, segments.length - 1);
    return normalized;
}

function parseModeKey(modeText) {
    const map = {
        '公路': 'truck', 'truck': 'truck',
        '铁路': 'rail', 'rail': 'rail',
        '河运': 'river', 'river': 'river', '水运': 'river',
        '海运': 'sea', 'sea': 'sea',
        '空运': 'air', 'air': 'air', '航空': 'air',
    };
    return map[modeText] || 'truck';
}

function getModeColor(mode) {
    const colors = {
        truck: '#4fc3f7',
        rail: '#ff9558',
        river: '#66bb6a',
        sea: '#ab47bc',
        air: '#ffd54f',
    };
    return colors[mode] || '#8eaed0';
}

function getModeIcon(mode) {
    const icons = {
        truck: '🚚',
        rail: '🚆',
        river: '⛴',
        sea: '🚢',
        air: '✈️',
    };
    return icons[mode] || '•';
}

function formatHours(hours) {
    if (!hours) return '0h';
    if (hours < 24) return `${hours.toFixed(1)}h`;
    return `${Math.floor(hours / 24)}d${Math.round(hours % 24)}h`;
}

// ---------- 订舱可视化：帕累托散点 + 口岸查验率 ----------
function renderBookingViz(data) {
    const options = data.options || [];
    const stage = data.stage || 'auto';
    const chosenId = data.selected_option_id;

    let html = '<div class="booking-viz">';

    // 合规风险标记行（一眼看清有无风险，不再埋在计算过程里）
    const flags = data.risk_flags || [];
    if (flags.length) {
        html += '<div class="bv-risks">' + flags.map(f => {
            const sev = f.severity === 'critical' ? 'critical' : 'warning';
            const icon = sev === 'critical' ? '🔴' : '🟡';
            return `<span class="bv-risk ${sev}">${icon} [${f.type}] ${f.detail || ''}</span>`;
        }).join('') + '</div>';
    } else if (data.options) {
        html += '<div class="bv-risks"><span class="bv-risk none">✓ 未检出合规风险</span></div>';
    }

    html += '<div class="bv-title">订舱候选 · 帕累托散点（运价 × 航程）</div>';
    html += buildBookingScatterSVG(options);
    html += '<div class="bv-title">口岸查验率对比（越低越可控）</div>';
    html += buildBookingBarsSVG(options);
    html += buildPortLegend(options);

    if (stage === 'proposed') {
        // 交互闸门阶段1：等用户选方案
        html += '<div class="bo-title">请选择要执行的方案（确认后自动议价 + 生成订舱指令）</div>';
        html += '<div class="booking-options">';
        options.forEach(o => {
            const pareto = o.pareto ? '<span class="bo-pareto" title="帕累托前沿方案">✦</span>' : '';
            const ir = o.inspection_rate != null ? `查验率 ${(o.inspection_rate * 100).toFixed(0)}%` : '';
            html += `<div class="booking-option ${o.is_recommended ? 'recommended' : ''}" data-option="${o.option_id}">
                <div class="bo-id">${o.option_id}${o.is_recommended ? ' ★' : ''}</div>
                <div class="bo-main">
                    <div class="bo-port">${o.origin_port} · ${o.carrier} ${pareto}</div>
                    <div class="bo-sub">${o.vessel}｜${o.eta_days}天｜$${o.price_per_40ft}/40ft${ir ? '｜' + ir : ''}</div>
                </div>
                <div class="bo-score">${(o.score || 0).toFixed(3)}</div>
            </div>`;
        });
        html += '</div>';
        html += '<button class="btn-primary" id="confirmBooking" disabled>请先选择方案</button>';
    } else if (stage === 'confirmed') {
        html += `<div class="bo-confirmed">✅ 已确认方案 ${chosenId}，议价话术与订舱指令见下方</div>`;
    }
    html += '</div>';
    return html;
}

function buildBookingScatterSVG(options) {
    const W = 480, H = 250, L = 54, R = 40, T = 18, B = 38;
    const pw = W - L - R, ph = H - T - B;
    const prices = options.map(o => o.price_per_40ft);
    const etas = options.map(o => o.eta_days);
    let xMin = Math.min(...prices), xMax = Math.max(...prices);
    let yMin = Math.min(...etas), yMax = Math.max(...etas);
    if (xMax === xMin) xMax = xMin + 100;
    if (yMax === yMin) yMax = yMin + 1;
    const xp = (xMax - xMin) * 0.08, yp = (yMax - yMin) * 0.15;
    xMin -= xp; xMax += xp; yMin -= yp; yMax += yp;
    const X = p => L + (p - xMin) / (xMax - xMin) * pw;
    const Y = e => T + (e - yMin) / (yMax - yMin) * ph;

    let s = `<svg viewBox="0 0 ${W} ${H}" class="bv-svg">`;
    for (let i = 0; i <= 3; i++) {
        const v = xMin + (xMax - xMin) * i / 3, x = X(v);
        s += `<line x1="${x}" y1="${T}" x2="${x}" y2="${T + ph}" stroke="#1f3a5f" stroke-width="1"/>`;
        s += `<text x="${x}" y="${T + ph + 15}" fill="#5a7898" font-size="10" text-anchor="middle">${Math.round(v)}</text>`;
    }
    for (let i = 0; i <= 4; i++) {
        const v = yMin + (yMax - yMin) * i / 4, y = Y(v);
        s += `<line x1="${L}" y1="${y}" x2="${L + pw}" y2="${y}" stroke="#1f3a5f" stroke-width="1"/>`;
        s += `<text x="${L - 6}" y="${y + 3}" fill="#5a7898" font-size="10" text-anchor="end">${v.toFixed(1)}</text>`;
    }
    s += `<text x="${L + pw / 2}" y="${H - 5}" fill="#8eaed0" font-size="10" text-anchor="middle">运价 USD/40ft</text>`;
    s += `<text x="12" y="${T + ph / 2}" fill="#8eaed0" font-size="10" text-anchor="middle" transform="rotate(-90 12 ${T + ph / 2})">航程(天)</text>`;

    // 帕累托前沿连线
    const front = options.filter(o => o.pareto).slice().sort((a, b) => a.price_per_40ft - b.price_per_40ft);
    if (front.length > 1) {
        const pts = front.map(o => `${X(o.price_per_40ft)},${Y(o.eta_days)}`).join(' ');
        s += `<polyline points="${pts}" fill="none" stroke="#00d4ff" stroke-width="1.6" stroke-dasharray="4 3" opacity="0.8"/>`;
    }
    // 数据点
    options.forEach(o => {
        const x = X(o.price_per_40ft), y = Y(o.eta_days), c = portColor(o.origin_port);
        const r = o.is_recommended ? 6 : (o.pareto ? 4.5 : 3.2);
        if (o.is_recommended) s += `<circle cx="${x}" cy="${y}" r="11" fill="none" stroke="${c}" stroke-width="1.5" opacity="0.5"/>`;
        s += `<circle cx="${x}" cy="${y}" r="${r}" fill="${c}" stroke="#0a1628" stroke-width="1" opacity="${o.pareto ? 1 : 0.5}"/>`;
        s += `<text x="${x + 8}" y="${y + 3}" fill="#8eaed0" font-size="9">${o.option_id}</text>`;
    });
    s += '</svg>';
    return s;
}

function buildBookingBarsSVG(options) {
    const map = {};
    options.forEach(o => {
        if (o.inspection_rate != null && map[o.origin_port] == null) map[o.origin_port] = o.inspection_rate;
    });
    const rows = Object.keys(map).map(p => ({ port: p, rate: map[p] })).sort((a, b) => a.rate - b.rate);
    if (!rows.length) return '';
    const W = 480, L = 76, R = 44, T = 8, rowH = 30;
    const H = T + rows.length * rowH + 12;
    const maxR = (Math.max(...rows.map(r => r.rate)) * 1.15) || 1;
    const bw = W - L - R;
    let s = `<svg viewBox="0 0 ${W} ${H}" class="bv-svg">`;
    rows.forEach((r, i) => {
        const y = T + i * rowH, bh = 16;
        const w = r.rate / maxR * bw, c = portColor(r.port);
        s += `<text x="${L - 8}" y="${y + bh * 0.8}" fill="#8eaed0" font-size="10" text-anchor="end">${r.port}</text>`;
        s += `<rect x="${L}" y="${y}" width="${bw}" height="${bh}" rx="3" fill="#142540"/>`;
        s += `<rect x="${L}" y="${y}" width="${w}" height="${bh}" rx="3" fill="${c}"/>`;
        s += `<text x="${L + w + 6}" y="${y + bh * 0.8}" fill="#8eaed0" font-size="10">${(r.rate * 100).toFixed(0)}%</text>`;
    });
    s += '</svg>';
    return s;
}

function buildPortLegend(options) {
    const seen = {}, items = [];
    options.forEach(o => {
        if (!seen[o.origin_port]) {
            seen[o.origin_port] = 1;
            items.push(`<span class="bv-leg"><i style="background:${portColor(o.origin_port)}"></i>${o.origin_port}</span>`);
        }
    });
    items.push('<span class="bv-leg"><i class="dash"></i>帕累托前沿</span>');
    items.push('<span class="bv-leg"><b style="color:#00d4ff">★</b> AI 推荐</span>');
    return `<div class="bv-legend">${items.join('')}</div>`;
}

// 交互闸门阶段2：确认方案 → 议价 + 订舱指令
async function confirmBooking(optionId) {
    if (!lastPayload || !optionId) return;
    const btn = document.getElementById('confirmBooking');
    if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
    setStatus('loading', `确认 ${optionId}，生成议价与订舱指令...`);
    try {
        let res;
        if (lastPayload.file) {
            const fd = new FormData();
            fd.append('file', lastPayload.file);
            fd.append('query', lastPayload.query);
            fd.append('month', lastPayload.month);
            fd.append('interactive', 'true');
            fd.append('selected_option', optionId);
            res = await fetch('/api/dispatch_image', { method: 'POST', body: fd });
        } else {
            res = await fetch('/api/dispatch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    query: lastPayload.query,
                    month: parseInt(lastPayload.month),
                    interactive: true,
                    selected_option: optionId,
                    doc_fields: lastPayload.doc_fields,
                }),
            });
        }
        const data = await res.json();
        if (data.detail) throw new Error(data.detail);
        renderResult(data, lastBanner);
        setStatus('online', `已确认 ${optionId}：议价 + 订舱指令已生成`);
    } catch (e) {
        console.error(e);
        showError(e.message);
        setStatus('error', '确认失败');
    }
}

// ============================================================
// 法规知识库（RAG）：抽屉问答 + 申报结果内嵌依据
// ============================================================
const KB_KIND_CN = { original: '原文', summary: '摘要', editorial: '编者注', rule: '规则说明', hs_note: '归类参考' };

function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

function openKb() {
    document.getElementById('kbDrawer').classList.add('open');
    document.getElementById('kbDrawer').setAttribute('aria-hidden', 'false');
    setTimeout(() => document.getElementById('kbInput').focus(), 140);
}

function closeKb() {
    document.getElementById('kbDrawer').classList.remove('open');
    document.getElementById('kbDrawer').setAttribute('aria-hidden', 'true');
}

async function askKb() {
    const input = document.getElementById('kbInput');
    const q = input.value.trim();
    if (!q) { input.focus(); return; }

    const btn = document.getElementById('kbAskBtn');
    const box = document.getElementById('kbResult');
    btn.disabled = true;
    box.innerHTML = '<div class="kb-empty">检索并生成中…</div>';

    try {
        const res = await fetch('/api/kb/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: q, top_k: 5 }),
        });
        const data = await res.json();
        if (data.detail) throw new Error(data.detail);
        renderKbAnswer(data);
    } catch (e) {
        console.error(e);
        box.innerHTML = `<div class="kb-answer refused">问答失败：${escHtml(e.message)}</div>`;
    } finally {
        btn.disabled = false;
    }
}

function renderKbAnswer(data) {
    const refused = data.insufficient || !data.grounded;
    const meta = [
        `<span class="kb-badge">${escHtml(data.mode || 'ADVISORY')}</span>`,
        `<span class="kb-badge">引用 ${(data.citations || []).length} 条</span>`,
    ];
    if (data.degraded) {
        meta.push(`<span class="kb-badge editorial">降级：${escHtml(data.degraded_reason || '')}</span>`);
    }
    if (data.expanded_terms && data.expanded_terms.length) {
        meta.push(`<span class="kb-badge via-ref">查询扩展：${escHtml(data.expanded_terms.join('、'))}</span>`);
    }

    let html = `<div class="kb-answer ${refused ? 'refused' : ''}">${escHtml(data.answer || '')}
        <div class="kb-answer-meta">${meta.join('')}</div>
    </div>`;

    const cites = data.citations || [];
    if (cites.length) {
        html += cites.map((c, i) => renderKbCite(c, i + 1)).join('');
    }
    document.getElementById('kbResult').innerHTML = html;
}

function renderKbCite(c, idx) {
    const kind = c.text_kind || 'summary';
    const badge = `<span class="kb-badge ${escHtml(kind)}">${escHtml(c.text_kind_cn || KB_KIND_CN[kind] || kind)}</span>`;
    const via = c.via === 'cross_ref' ? '<span class="kb-badge via-ref">交叉引用</span>' : '';
    const art = c.article ? ` ${escHtml(c.article)}` : '';
    const doc = c.doc_no ? `（${escHtml(c.doc_no)}）` : '';
    const link = c.official_url
        ? `<a class="kb-cite-link" href="${escHtml(c.official_url)}" target="_blank" rel="noopener">查看官方原文 ↗</a>`
        : '';
    const verified = c.verified_at
        ? `<span class="kb-badge">已核验 ${escHtml(c.verified_at)}</span>`
        : '';
    return `<div class="kb-cite">
        <div class="kb-cite-head">
            <span class="kb-cite-title">[${idx}] ${escHtml(c.source_title || '')}${art}</span>${doc}${badge}${verified}${via}
        </div>
        <div class="kb-cite-text">${escHtml(c.quote || '')}</div>
        ${link}
    </div>`;
}

// 报关结果内嵌：只做检索不做生成，避免每次申报都消耗 token
async function loadKbEvidence(query) {
    const box = document.getElementById('kbInline');
    if (!box || !query) return;
    try {
        const res = await fetch(`/api/kb/search?q=${encodeURIComponent(query)}&top_k=3`);
        const data = await res.json();
        const hits = (data.hits || []).filter(h => h.score >= 4 && h.text_kind !== 'editorial');
        if (!hits.length) return;

        box.style.display = 'block';
        box.innerHTML = `<div class="kb-evidence">
            <div class="kb-evidence-head">依据与解释
                <span>来源：法规知识库 · 仅供解释，不参与申报判断</span>
            </div>
            ${hits.slice(0, 2).map(h => `<div class="kb-evidence-item">
                <div class="kb-evidence-src">
                    ${escHtml(h.source_title || '')}${h.article ? ' ' + escHtml(h.article) : ''}<br>
                    <span class="kb-badge ${escHtml(h.text_kind)}">${escHtml(h.text_kind_cn || '')}</span>
                </div>
                <div class="kb-evidence-text">${escHtml(h.text)}</div>
            </div>`).join('')}
        </div>`;
    } catch (e) {
        // 静默失败：依据展示不影响主链路
    }
}
