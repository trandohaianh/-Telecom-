const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const when = t => t ? new Date(t * 1000).toLocaleString('vi-VN') : '—';
const usd = v => v == null ? '—' : '$' + Number(v).toFixed(v && v < 0.01 ? 6 : 2);
let TOKEN = ''; try { TOKEN = sessionStorage.getItem('tfs_session') || ''; } catch(e){}
let ME = null;
async function api(url, opts = {}){
  const r = await fetch(url, { ...opts, headers: { 'content-type': 'application/json', ...(opts.headers || {}), Authorization: 'Bearer ' + TOKEN } });
  if (r.status === 401){ location.href = '/admin/'; throw new Error('401'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : r.status);
  return j;
}
function fail(e){ $('#err').textContent = e.message || e; }
// Dòng "Đang tải…" trong bảng trong lúc chờ API.
function loading(sel, cols){ $(sel).innerHTML = `<tr><td colspan="${cols}" class="loading" role="status">Đang tải…</td></tr>`; }
function show(t){
  document.querySelectorAll('.tab').forEach(s => s.classList.toggle('hide', s.id !== t));
  document.querySelectorAll('#tabs button').forEach(b => {
    const on = b.dataset.t === t; b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1;
  });
  $('#err').textContent = ''; ({ reports: loadReports, pipeline: loadPipeline, costs: loadCosts, operators: loadOperators })[t]();
}
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => show(b.dataset.t));
// Mũi tên trái/phải, Home/End chuyển giữa các tab đang hiện (mẫu WAI-ARIA tablist).
$('#tabs').addEventListener('keydown', ev => {
  const tabs = [...document.querySelectorAll('#tabs [role="tab"]')].filter(b => !b.classList.contains('hide'));
  const i = tabs.indexOf(document.activeElement); if (i < 0) return;
  let j = null;
  if (ev.key === 'ArrowRight') j = (i + 1) % tabs.length;
  else if (ev.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
  else if (ev.key === 'Home') j = 0;
  else if (ev.key === 'End') j = tabs.length - 1;
  if (j === null) return;
  ev.preventDefault(); tabs[j].focus(); show(tabs[j].dataset.t);
});

// ---------------------------------------------------------------- báo cáo số
$('#rstatus').onchange = loadReports;
async function loadReports(){
  loading('#rbody', 6);
  try {
    const rows = await api('/api/admin/reports?status=' + $('#rstatus').value);
    const pending = $('#rstatus').value === 'pending';
    $('#rbody').innerHTML = rows.length ? rows.map(r => `<tr><td class="mono">${esc(r.number_masked)}</td><td class="n">${r.reporters}</td>
      <td class="n">${r.model_blocks ?? 0} / ${r.model_flags ?? 0}</td><td>${esc(r.reasons || '—')}</td><td class="mut">${when(r.last_at)}${r.reviewer ? '<br>duyệt: ' + esc(r.reviewer) : ''}</td>
      <td>${pending ? `<button class="red" data-k="${esc(r.key)}" data-d="confirm">XÁC NHẬN CHẶN</button> <button data-k="${esc(r.key)}" data-d="dismiss">BÁC BỎ</button>` : ''}</td></tr>`).join('')
      : '<tr><td colspan="6" class="mut">Không có báo cáo.</td></tr>';
    document.querySelectorAll('#rbody button').forEach(b => b.onclick = async () => {
      const note = prompt(b.dataset.d === 'confirm' ? 'Ghi chú xác nhận (bắt buộc ghi căn cứ):' : 'Lý do bác bỏ:');
      if (note === null) return;
      if (!note.trim()) { alert('Cần ghi căn cứ / lý do trước khi duyệt.'); return; }
      try { await api(`/api/admin/reports/${encodeURIComponent(b.dataset.k)}/review`, { method: 'POST', body: JSON.stringify({ decision: b.dataset.d, note }) }); loadReports(); }
      catch(e){ fail(e); }
    });
  } catch(e){ fail(e); }
}

// ---------------------------------------------------------------- pipeline & KPI
async function loadPipeline(){
  loading('#pbody', 5);
  try {
    const p = await api('/api/admin/pipeline');
    $('#pbody').innerHTML = Object.entries(p.last_runs).map(([job, r]) => `<tr><td class="mono">${esc(job)}</td>
      <td class="st-${esc(r?.status || 'warn')}">${r ? esc(r.status) : 'chưa chạy'}</td><td class="mut">${when(r?.finished_at || r?.started_at)}</td>
      <td class="mut mono" style="font-size:11.5px">${esc(r?.detail ? JSON.stringify(r.detail).slice(0, 180) : '')}</td>
      <td class="adm"><button data-j="${esc(job)}">CHẠY NGAY</button></td></tr>`).join('');
    document.querySelectorAll('#pbody button').forEach(b => b.onclick = async () => { b.disabled = true; try { await api('/api/admin/pipeline/run?job=' + encodeURIComponent(b.dataset.j), { method: 'POST' }); loadPipeline(); } catch(e){ fail(e); b.disabled = false; } });
    applyRole();
    const k = p.kpi, last = k[k.length - 1];
    $('#kpis').innerHTML = last ? [
      ['Đã chấm', last.processed], ['Chặn', last.blocked], ['Gắn cờ', last.flagged], ['Fail-open', last.fail_open],
      ['p99 (ms)', last.latency_p99_ms], ['Thuê bao', last.subscribers], ['Báo cáo chờ', last.reports_pending],
      ['Drift PSI', last.drift_max_psi == null ? '—' : last.drift_max_psi + ' · ' + last.drift_status]]
      .map(([l, v]) => `<div class="kpi"><b>${esc(v ?? '—')}</b><span>${l}</span></div>`).join('') : '<p class="mut">Chưa có ảnh chụp KPI (job chạy mỗi giờ).</p>';
    $('#kchart').innerHTML = chart(k.map(x => ({ t: x.taken_at, v: x.latency_p99_ms ?? 0 })), 'p99 độ trễ quyết định (ms)');
  } catch(e){ fail(e); }
}
function chart(pts, label){
  if (pts.length < 2) return '';
  const W = 1100, H = 140, pl = 44, pb = 20, max = Math.max(...pts.map(p => p.v), 1);
  const x = i => pl + i * (W - pl - 6) / (pts.length - 1), y = v => 8 + (H - pb - 8) * (1 - v / max);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${label}"><text x="0" y="12">${max.toFixed(1)}</text><text x="0" y="${H - pb}">0</text>
    <line x1="${pl}" x2="${W}" y1="${H - pb}" y2="${H - pb}" stroke="currentColor" opacity=".2"/><path d="${d}" fill="none" stroke="#e11d48" stroke-width="2"/>
    <text x="${pl}" y="${H - 4}">${label}</text></svg>`;
}

// ---------------------------------------------------------------- chi phí
async function loadCosts(){
  loading('#ubody', 4); loading('#ibody', 4);
  try {
    const c = await api('/api/admin/costs');
    const cm = c.compute;
    $('#ckpis').innerHTML = [
      ['Hạ tầng tháng ' + c.infra.month, usd(c.infra.total_usd)], ['Dịch vụ theo lượt (30 ngày)', usd(c.external_usage.total_usd)],
      ['Ước tính tháng', usd(c.total_month_estimate_usd)], ['Chi phí / 1.000 quyết định', usd(cm.infra_cost_per_1k_decisions_usd)],
      ['CPU ms / 1.000 quyết định', cm.cpu_ms_per_1k_decisions ?? '—'], ['Quyết định / giây', cm.decisions_per_s], ['RAM đỉnh (MB)', cm.rss_mb_peak]]
      .map(([l, v]) => `<div class="kpi"><b>${esc(v)}</b><span>${l}</span></div>`).join('');
    $('#cnote').textContent = c.note;
    $('#ubody').innerHTML = c.external_usage.by_kind.length ? c.external_usage.by_kind.map(r => `<tr><td>${esc(r.kind)}</td><td>${esc(r.provider || '')}</td><td class="n">${esc(r.units)}</td><td class="n">${usd(r.cost_usd)}</td></tr>`).join('')
      : '<tr><td colspan="4" class="mut">Chưa có (chế độ demo không gửi SMS).</td></tr>';
    $('#ibody').innerHTML = c.infra.items.map(r => `<tr><td>${esc(r.provider)}</td><td>${esc(r.item)}</td><td class="n">${usd(r.amount_usd)}</td><td><button data-id="${esc(r.id)}">XOÁ</button></td></tr>`).join('')
      || '<tr><td colspan="4" class="mut">Chưa nhập.</td></tr>';
    document.querySelectorAll('#ibody button').forEach(b => b.onclick = async () => { if (!confirm('Xoá khoản chi phí này?')) return; try { await api('/api/admin/costs/infra/' + encodeURIComponent(b.dataset.id), { method: 'DELETE' }); loadCosts(); } catch(e){ fail(e); } });
    $('#iadd').onclick = async () => {
      try { await api('/api/admin/costs/infra', { method: 'POST', body: JSON.stringify({ month: c.infra.month, provider: $('#iprov').value, item: $('#iitem').value, amount_usd: Number($('#iamt').value) || 0 }) });
            $('#iprov').value = $('#iitem').value = ''; loadCosts(); } catch(e){ fail(e); }
    };
  } catch(e){ fail(e); }
}

// ---------------------------------------------------------------- tài khoản nhân viên
async function loadOperators(){
  loading('#obody', 6);
  try {
    const rows = await api('/api/admin/operators');
    $('#obody').innerHTML = rows.map(o => `<tr><td class="mono">${esc(o.name)}</td><td>${esc(o.role_label)}</td><td>${esc(o.email || '—')}</td>
      <td>${o.has_password ? 'đã đặt' : '<span class="st-warn">chưa có</span>'}</td><td class="${o.disabled || o.suspended ? 'st-fail' : 'st-ok'}">${o.disabled ? 'đã khoá' : o.suspended ? 'tạm khoá (agent)' : 'hoạt động'}</td>
      <td>${o.name === ME.name ? '' : `<button data-n="${esc(o.name)}" data-r="${esc(o.role)}" data-d="${o.disabled ? '0' : '1'}">${o.disabled ? 'MỞ KHOÁ' : 'KHOÁ'}</button>`}</td></tr>`).join('');
    document.querySelectorAll('#obody button').forEach(b => b.onclick = async () => {
      if (b.dataset.d === '1' && !confirm(`Khoá tài khoản "${b.dataset.n}"? Người này sẽ không đăng nhập được cho tới khi được mở khoá.`)) return;
      try { await api('/api/admin/operators', { method: 'POST', body: JSON.stringify({ name: b.dataset.n, role: b.dataset.r, disabled: b.dataset.d === '1' }) }); loadOperators(); } catch(e){ fail(e); }
    });
  } catch(e){ fail(e); }
}
$('#osave').onclick = async () => {
  const body = { name: $('#oname').value.trim(), role: $('#orole').value, email: $('#oemail').value.trim() || null };
  if ($('#opass').value) body.password = $('#opass').value;
  try { await api('/api/admin/operators', { method: 'POST', body: JSON.stringify(body) }); $('#opass').value = ''; loadOperators(); } catch(e){ fail(e); }
};

// Watermark: tên tài khoản + thời điểm, lặp chéo toàn màn hình (cập nhật mỗi phút), giống /admin/.
function watermark(){
  if (!ME) return;
  const txt = `${ME.name} · ${new Date().toLocaleString('vi-VN')}`.replace(/[<&>'"]/g, '');
  const col = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim() || '#888';
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='420' height='220'><text x='10' y='120' transform='rotate(-22 210 110)' font-family='monospace' font-size='15' fill='${col}'>${txt}</text></svg>`;
  $('#wm').style.backgroundImage = `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}
function applyRole(){ document.querySelectorAll('.adm').forEach(e => e.classList.toggle('hide', ME?.role !== 'admin')); }
(async () => {
  if (!TOKEN){ location.href = '/admin/'; return; }
  try { ME = await api('/api/whoami'); } catch(e){ fail(e); return; }   // never an empty page without a message
  if (ME.role === 'viewer'){ document.body.innerHTML = '<div class="wrap"><p>Vai trò Nhân viên (chỉ xem) không có quyền quản trị. <a href="/admin/">Quay lại</a></p></div>'; return; }
  $('#who').textContent = `${ME.name} · ${(ME.role_label || ME.role).toUpperCase()}`;
  applyRole(); watermark(); setInterval(watermark, 60000); show('reports');
})();

// Navigation buttons (was onclick="location.href=..."; inline handlers are blocked by the CSP).
document.querySelectorAll('[data-href]').forEach(b => b.addEventListener('click', () => { location.href = b.dataset.href; }));
