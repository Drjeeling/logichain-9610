/**
 * 9610 单证视图：把 /api/declaration/build 返回的 payload.form
 * 按海关总署 2016 年第 26 号公告附件 2 的版式渲染成 A4 单证。
 * 取值/状态由后端 form_layout.py 解析，本文件只负责排版与打印。
 */
function dfEsc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function dfNum(v, digits) {
    const n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toLocaleString('en-US', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

const DF_DOT = { ok: 'ok', derive: 'derive', default: 'default', sim: 'sim', need: 'need', empty: 'empty' };
const DF_VCLS = { sim: 'sim', derive: 'derive', need: 'need', empty: 'empty' };

function dfValue(c) {
    const v = (c.value === null || c.value === undefined || c.value === '') ? '—' : c.value;
    const cls = DF_VCLS[c.status] || '';
    return `<span class="df-v ${cls}"${c.note ? ` title="${dfEsc(c.note)}"` : ''}>${dfEsc(v)}</span>`;
}

function dfDot(c) {
    const cls = DF_DOT[c.status] || 'empty';
    return `<span class="df-dot ${cls}"${c.note ? ` title="${dfEsc(c.note)}"` : ''}></span>`;
}

function dfHeaderCell(c) {
    const span = c.span > 1 ? ` df-span${c.span}` : '';
    return `<div class="df-cell${span}">${dfDot(c)}
        <span class="df-k"><em>${dfEsc(c.no)}</em>${dfEsc(c.label)}</span>
        ${dfValue(c)}
    </div>`;
}

function dfStripCell(c) {
    return `<div class="df-s">${dfDot(c)}
        <div class="df-k"><em>${dfEsc(c.no)}</em>${dfEsc(c.label)}</div>
        ${dfValue(c)}
    </div>`;
}

function dfItemCell(c) {
    const align = c.align === 'left' ? 'left' : (c.align === 'num' ? 'num' : (c.align === 'code' ? 'code' : (c.align === 'unit' ? 'unit' : '')));
    const extra = c.status === 'sim' ? ' sim' : (c.status === 'need' ? ' need' : '');
    return `<td class="${align}${extra}">${dfEsc(c.value === '' || c.value === null || c.value === undefined ? '—' : c.value)}</td>`;
}

function renderDeclFormSheet(payload) {
    const f = payload && payload.form;
    if (!f || !f.header) {
        return '<div style="color:#dfe7f3;padding:40px;text-align:center;font-family:-apple-system,sans-serif">本次没有可用的单证数据</div>';
    }
    const totals = payload.totals || {};
    const rowsHtml = (f.header || []).reduce((acc, row) => acc.concat(row), []).map(dfHeaderCell).join('');

    const infoCells = `
        <div class="df-s"><div class="df-k">条目合计</div><div class="df-v">${dfEsc(f.entry_total || '42 / 42')}</div></div>
        <div class="df-s"><div class="df-k">本页状态</div>
            <div class="df-v" style="color:${f.pending_count + f.sim_count ? '#d97706' : '#16a34a'}">${dfEsc(f.status_text || '')}</div>
        </div>`;

    const cols = (f.columns || []).map(c => `<col style="width:${dfEsc(c.width || 'auto')}">`).join('');
    const th = (f.columns || []).map(c => {
        const label = String(c.label || '').replace('最终目的国（地区）', '最终目的国<br>（地区）');
        return `<th title="${dfEsc(c.note || '')}">${label}</th>`;
    }).join('');

    const bodyRows = (f.items || []).map(item =>
        `<tr>${(item.cells || []).map(dfItemCell).join('')}</tr>`).join('');

    const foot = f.footer || {};
    const qty = totals.quantity === null || totals.quantity === undefined ? '—' : dfNum(totals.quantity, 0);
    const legal = totals.net_weight === null || totals.net_weight === undefined ? '—' : dfNum(totals.net_weight, 0);
    const totalPrice = totals.total_price === null || totals.total_price === undefined ? '—' : dfNum(totals.total_price, 2);

    return `
    <div class="df-sheet">
      <div class="df-kicker">中华人民共和国海关</div>
      <h2 class="df-h1">${dfEsc(f.doc_title || '跨境电子商务零售出口商品申报清单')}</h2>
      <div class="df-sub">
        数据口径：${dfEsc(f.basis || '')}　｜　表头 42 项 / 表体 16 项<br>
        代码集：${dfEsc(f.code_sets || '')}
      </div>

      <div class="df-no">
        <span>清单编号（第 10 项）：</span><b class="derive">${dfEsc(f.list_no || '—')}</b>
        <span class="df-dot derive" title="派生：4 位关区 + 4 位年 + 进出口标记 E + 9 位流水"></span>
      </div>

      <div class="df-grid">${rowsHtml}</div>

      <div class="df-sect">清单数据表头（续）：第 3、5–9、11、20–22、32–37 项</div>
      <div class="df-strip">${(f.strip || []).map(dfStripCell).join('')}${infoCells}</div>

      <div class="df-body-label">
        <span>清单数据表体（第 43–58 项，16 项）</span>
        <span>${dfEsc(f.totals_text || '')}</span>
      </div>
      <table class="df-items">
        <colgroup>${cols}</colgroup>
        <thead><tr>${th}</tr></thead>
        <tbody>${bodyRows}</tbody>
        <tfoot>
          <tr>
            <td colspan="8">合计</td>
            <td class="num">${qty}</td>
            <td class="num">${legal}</td>
            <td>—</td>
            <td colspan="3"></td>
            <td></td>
            <td class="num">${totalPrice}</td>
          </tr>
        </tfoot>
      </table>
      <div class="df-code-note">
        计量单位按 JGS-20 代码集：007 = 个，035 = 千克。法定数量按净重口径派生；总价 = 申报数量 × 单价。
        橙色点线标注的为演示模拟编码，接入企业备案后应替换为真实值。
      </div>

      <div class="df-footer">
        <div class="df-fbox">
          <div class="df-row"><span>录入员</span><span>${dfEsc(foot.operator || '—')}</span></div>
          <div class="df-row"><span>录入单位</span><span>${dfEsc(foot.unit || '智关9610')}</span></div>
          <div class="df-row"><span>报关员</span><span>${dfEsc(foot.declarant || '—')}</span></div>
          <div class="df-row"><span>单位地址</span><span>${dfEsc(foot.address || '—')}</span></div>
          <div class="df-row"><span>电话</span><span>${dfEsc(foot.phone || '—')}</span></div>
          <div class="df-row"><span>邮编</span><span>${dfEsc(foot.zip || '—')}</span></div>
        </div>
        <div class="df-fbox df-declare">
          <div class="df-txt">兹声明以上申报无讹并承担法律责任。</div>
          <div class="df-sign">
            <div class="df-dash"></div>
            <div class="df-cap">申报单位（签章）　填制日期：${dfEsc(foot.fill_date || '')}</div>
          </div>
        </div>
        <div class="df-fbox df-customs">
          <div class="df-h">海关审批批注及放行日期（签章）</div>
          <div class="df-line"><span>审单</span><span></span></div>
          <div class="df-line"><span>查验</span><span></span></div>
          <div class="df-line"><span>放行</span><span></span></div>
        </div>
      </div>

      <div class="df-footnote">
        <span><b>说明：</b>版式按 2016 年第 26 号公告附件 2（零售出口）组装；企业代码类橙色编号为演示模拟值，正式申报须与企业海关备案一致。</span>
        <span style="white-space:nowrap">智关 Agent 9610</span>
      </div>
    </div>`;
}

function openDeclFormView() {
    const payload = declarationCtx && declarationCtx.payload;
    if (!payload || !payload.form || !payload.form.header) return;

    let ov = document.getElementById('declFormOverlay');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'declFormOverlay';
        document.body.appendChild(ov);
    }
    ov.innerHTML = `
        <div class="df-bar">
            <span class="df-title">9610 零售出口商品申报清单</span>
            <span class="df-hint">数据取自本次识别与制单结果</span>
            <span class="df-legend">
                <span><i style="background:#16a34a"></i>已识别</span>
                <span><i style="background:#2563eb"></i>派生</span>
                <span><i style="background:#7c3aed"></i>默认值</span>
                <span><i style="background:#d97706"></i>模拟编号 / 待核验</span>
                <span><i style="background:#dc2626"></i>必填缺失</span>
            </span>
            <button type="button" class="primary" onclick="printDeclForm()">打印 / 导出 PDF</button>
            <button type="button" onclick="closeDeclFormView()">关闭</button>
        </div>
        <div class="df-scroll">${renderDeclFormSheet(payload)}</div>`;
    ov.classList.add('open');
    document.body.classList.add('df-open');

    ov.onclick = e => {
        if (e.target === ov || (e.target.classList && e.target.classList.contains('df-scroll'))) {
            closeDeclFormView();
        }
    };
    if (!window.__dfEscBound) {
        window.__dfEscBound = true;
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') closeDeclFormView();
        });
    }
}

function closeDeclFormView() {
    const ov = document.getElementById('declFormOverlay');
    if (ov) ov.classList.remove('open');
    document.body.classList.remove('df-open', 'df-printing');
}

function printDeclForm() {
    document.body.classList.add('df-printing');
    setTimeout(() => {
        window.print();
        document.body.classList.remove('df-printing');
    }, 60);
}
