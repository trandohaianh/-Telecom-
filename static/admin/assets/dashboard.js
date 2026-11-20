// Lưu ngôn ngữ đang chọn, trạng thái mới nhất và các metric benchmark trong bộ nhớ trang.
let L = 'vi', last = null, metrics = null;
// Viết tắt truy vấn một phần tử DOM theo CSS selector.
const $ = s => document.querySelector(s);
// Định dạng số theo locale đang được chọn; dùng 0 nếu giá trị chưa có.
const fmt = n => (n ?? 0).toLocaleString(L === 'vi' ? 'vi-VN' : 'en-US');
// Cập nhật văn bản song ngữ của các phần tử và vẽ lại dữ liệu theo ngôn ngữ mới.
function applyLang(){ document.querySelectorAll('[data-vi]').forEach(e => { e.innerHTML = e.dataset[L]; }); $('#lang').textContent = L === 'vi' ? 'EN' : 'VI'; document.documentElement.lang = L; if (last) render(last); if (metrics) renderMetrics(); }
// Khi bấm nút ngôn ngữ, đảo VI/EN rồi cập nhật nội dung giao diện.
$('#lang').onclick = () => { L = L === 'vi' ? 'en' : 'vi'; applyLang(); };
// Một tên duy nhất cho mỗi kiểu lừa đảo, dùng chung cho nút mô phỏng, bảng thống kê và app thuê bao.
const FRAUD_NAMES = {
  SIM_BOX: ['SIM box', 'SIM box'],
  CLI_SPOOF: ['Giả mạo số tổng đài', 'Spoofed hotline'],
  SMS_PHISH: ['SMS lừa đảo', 'Phishing SMS'],
  DEEPFAKE_VOICE: ['Giọng nói deepfake', 'Deepfake voice'],
  ZERO_DAY_WANGIRI: ['Wangiri (gọi nháy 1 tiếng)', 'Wangiri (one-ring calls)'],
  VOICE_HARVEST: ['Cuộc gọi im lặng thu giọng', 'Silent voice-harvesting calls'],
};
// Tên hiển thị song ngữ cho từng loại sự kiện.
const T = { CALL: ['Cuộc gọi', 'Call'], SMS: ['SMS', 'SMS'], AUDIO: ['Âm thanh trong cuộc gọi', 'In-call audio'] };
// Nút gạt sáng / tối: đổi data-theme trên <html>, nhớ lựa chọn, vẽ lại biểu đồ theo màu mới.
function setTheme(t){
  document.documentElement.dataset.theme = t; $('#themeSw').checked = t === 'light';
  try { localStorage.setItem('tfs_theme', t); } catch(e){}
  if (metrics) renderMetrics();
  watermark();
}
$('#themeSw').onchange = e => setTheme(e.target.checked ? 'light' : 'dark');
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
// Phiên đăng nhập (không phải token cá nhân) lưu trong sessionStorage: đóng tab là mất, hết hạn sau 8 giờ / 30 phút rảnh.
const store = { get(){ try { return sessionStorage.getItem('tfs_session') || ''; } catch(e){ return ''; } },
                set(v){ try { v ? sessionStorage.setItem('tfs_session', v) : sessionStorage.removeItem('tfs_session'); } catch(e){} } };
try { localStorage.removeItem('tfs_token'); } catch(e){}      // bản cũ từng lưu token cá nhân lâu dài: xóa đi
let TOKEN = store.get(), ME = null;
// Áp theme đã lưu (sau khi ME đã khai báo, vì setTheme vẽ lại watermark).
(() => { let t = 'dark'; try { t = localStorage.getItem('tfs_theme') || 'dark'; } catch(e){} setTheme(t); })();
// api gọi endpoint kèm token; 401 mở màn đăng nhập, 403 báo không đủ quyền.
// Người dùng đang trỏ chuột hoặc focus trong vùng này: giữ nguyên DOM (feed 700 ms, đề xuất 3 s).
function holding(sel){ const el = $(sel); return !!el && (el.matches(':hover') || el.contains(document.activeElement)); }
async function api(url, opts = {}){
  const r = await fetch(url, { ...opts, headers: { ...(opts.headers || {}), Authorization: 'Bearer ' + TOKEN } });
  if (r.status === 401){ if (ME) { ME = null; TOKEN = ''; store.set(''); } showLogin(L==='vi' ? 'Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại.' : 'Session expired, please sign in again.'); throw new Error('401'); }
  const j = await r.json().catch(() => ({}));
  // Every non-2xx is an error (was: only 401/403), so 400/409/5xx bodies never reach render code.
  if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : String(r.status));
  return j;
}
function showLogin(msg){ $('#lerr').textContent = msg || ''; $('#login').classList.add('show'); setTimeout(() => $('#user').focus(), 50); }
let useToken = false;
$('#toktoggle').onclick = e => { e.preventDefault(); useToken = !useToken;
  $('#tok').style.display = useToken ? '' : 'none'; $('#user').style.display = $('#pass').style.display = useToken ? 'none' : ''; };
async function finishLogin(r){
  if (!r.ok){
    const msg = r.status === 403 ? (L==='vi' ? 'Tài khoản đang bị khoá hoặc chưa được cấp quyền.' : 'Account suspended or not authorised.')
              : r.status === 429 ? (L==='vi' ? 'Thử quá nhiều lần, chờ ít phút.' : 'Too many attempts, wait a few minutes.')
              : (L==='vi' ? 'Thông tin đăng nhập không đúng.' : 'Invalid credential.');
    $('#lerr').textContent = msg; return;
  }
  TOKEN = (await r.json()).session; store.set(TOKEN); $('#login').classList.remove('show'); await boot();
}
// Đăng nhập: gửi thông tin một lần để đổi lấy phiên; mật khẩu/token không được lưu lại trong trình duyệt.
function netFail(){ $('#lerr').textContent = L==='vi' ? 'Không kết nối được máy chủ, thử lại sau.' : 'Cannot reach the server, try again.'; }
$('#lform').onsubmit = async e => {
  e.preventDefault();
  let r;
  try {
    r = useToken
      ? await fetch('/api/login', { method: 'POST', headers: { Authorization: 'Bearer ' + $('#tok').value.trim() } })
      : await fetch('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' },
                                    body: JSON.stringify({ username: $('#user').value.trim(), password: $('#pass').value }) });
  } catch(err){ netFail(); return; }
  finally { $('#tok').value = ''; $('#pass').value = ''; }
  await finishLogin(r);
};
// Google cho nhân viên: chỉ vào được nếu email Google đã được admin gắn với một tài khoản vận hành.
(async () => {
  let c = {}; try { c = await (await fetch('/api/auth/config')).json(); } catch(e){}
  if (!c.google_client_id) return;
  const sc = document.createElement('script'); sc.src = 'https://accounts.google.com/gsi/client'; sc.async = true;
  sc.onload = () => {
    google.accounts.id.initialize({ client_id: c.google_client_id, ux_mode: 'popup', callback: async resp => {
      let r;
      try { r = await fetch('/api/login/google', { method: 'POST', headers: { 'content-type': 'application/json' },
                                                   body: JSON.stringify({ credential: resp.credential }) }); }
      catch(err){ netFail(); return; }
      finishLogin(r); } });
    // Width follows the form so the button never overflows a 360 px phone (Google allows 200-400 px).
    const w = Math.max(200, Math.min(400, Math.floor($('#lform').clientWidth || 300)));
    google.accounts.id.renderButton($('#ogbtn'), { theme: 'filled_black', size: 'large', text: 'signin_with', locale: 'vi', width: w });
  };
  document.head.appendChild(sc);
})();
$('#logout').onclick = async () => { try { await fetch('/api/logout', { method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN } }); } catch(e){} TOKEN = ''; store.set(''); ME = null; showLogin(); };
// Ẩn/hiện bảng theo vai trò: tiêm tấn công + gây lỗi chỉ admin ở chế độ demo; bằng chứng từ analyst trở lên.
function applyRole(){
  const lvl = { viewer: 1, analyst: 2, admin: 3 }[ME.role];
  document.querySelectorAll('.adminonly').forEach(e => e.classList.toggle('hide', !(lvl >= 3 && ME.demo_mode)));
  document.querySelectorAll('.adminonly-any').forEach(e => e.classList.toggle('hide', lvl < 3));
  document.querySelectorAll('.analystonly').forEach(e => e.classList.toggle('hide', lvl < 2));
  $('#who').textContent = `${ME.name} · ${(ME.role_label || ME.role).toUpperCase()}`;
  watermark();
}
// Watermark: tên tài khoản + thời điểm, lặp chéo toàn màn hình (cập nhật mỗi phút).
function watermark(){
  if (!ME) return;
  const txt = `${ME.name} · ${new Date().toLocaleString('vi-VN')}`.replace(/[<&>]/g, '');
  const col = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim() || '#888';
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='420' height='220'><text x='10' y='120' transform='rotate(-22 210 110)' font-family='monospace' font-size='15' fill='${col}'>${txt}</text></svg>`;
  $('#wm').style.backgroundImage = `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}
setInterval(watermark, 60000);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// AI agent: hiển thị đề xuất; admin duyệt / từ chối (kèm ghi chú), người khác chỉ xem.
const ST = { pending: ['Chờ quyết định', 'Awaiting decision'], approved: ['Đã duyệt', 'Approved'], rejected: ['Đã từ chối', 'Rejected'],
             expired: ['Hết hạn', 'Expired'], obsolete: ['Không còn cần', 'No longer needed'] };
async function pollAgent(){
  if (ME) { try {
    const a = await api('/api/agent'), vi = L === 'vi';
    $('#agent_note').textContent = a.can_decide ? (vi ? 'bạn có quyền quyết định' : 'you can decide') : (vi ? 'chỉ admin quyết định' : 'admin decides');
    if (holding('#agent')) return;   // không làm mới khi người dùng đang trỏ/thao tác: nút Duyệt không được dịch dưới con trỏ
    $('#agent').innerHTML = a.proposals.length ? a.proposals.map(p => `<div class="prop ${p.severity} ${p.status !== 'pending' ? 'done' : ''}">
      <div class="t"><span>${esc(p.title)}</span><span class="mono" style="color:var(--mut)">${esc(p.id)}</span></div>
      <ul>${p.why.map(w => `<li>${esc(w)}</li>`).join('')}</ul>
      ${p.subjects.length ? `<div class="subj">${p.subjects.map(esc).join(' · ')}</div>` : ''}
      ${p.status === 'pending' && a.can_decide ? `<div class="acts"><button class="pri" data-ok="${esc(p.id)}">${vi ? 'DUYỆT' : 'APPROVE'}: ${esc(p.action.label)}</button><button data-no="${esc(p.id)}">${vi ? 'TỪ CHỐI' : 'REJECT'}</button></div>` : ''}
      <div class="st">${ST[p.status] ? ST[p.status][vi ? 0 : 1] : esc(p.status)}${p.decided_by ? ' · ' + esc(p.decided_by) : ''}${p.note ? ' · “' + esc(p.note) + '”' : ''}${p.result ? ' · ' + esc(p.result) : ''}</div>
    </div>`).join('') : `<div class="note">${vi ? 'Chưa có đề xuất. Agent đang giám sát: sự cố mô hình, nhóm SIM box, nguy cơ chặn nhầm, báo cáo người dùng, sự kiện lọt lúc sự cố, rò rỉ dữ liệu nội bộ.' : 'No proposals yet.'}</div>`;
    document.querySelectorAll('[data-ok],[data-no]').forEach(b => b.onclick = async () => {
      const ok = !!b.dataset.ok, id = b.dataset.ok || b.dataset.no;
      const note = prompt(L === 'vi' ? (ok ? 'Ghi chú khi duyệt (lý do):' : 'Lý do từ chối:') : 'Note:', '');
      if (note === null) return;
      if (!note.trim()) { alert(L === 'vi' ? 'Cần ghi lý do trước khi quyết định.' : 'A note is required.'); return; }
      try { await api(`/api/agent/${encodeURIComponent(id)}/decide`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision: ok ? 'approve' : 'reject', note }) }); } catch(e){ alert(e.message); }
      pollAgent();
    });
  } catch(e){} }
}
setInterval(() => { if (!document.hidden) pollAgent(); }, 3000);   // no polling in a hidden tab
// Tạo HTML cho một thẻ cảnh báo, gồm quyết định, lý do, số liên quan và điểm rủi ro.
function card(a){
  // Chuyển trạng thái fail-open thành nhãn riêng; nếu không thì dùng quyết định detector.
  // Fail-open thật (cho qua) khác với quyết định của luật dự phòng (vẫn chặn / gắn cờ khi mô hình không trả lời).
  const fo = a.fail_open && a.decision === 'ALLOW', cls = fo ? 'FAIL' : esc(a.decision);
  const fb = a.fail_open && a.decision !== 'ALLOW';
  // Tạo nhãn trực quan cho fail-open, dự phòng, hồi tố, zero-day và sự kiện được tiêm thủ công.
  const tags = `<span class="tag ${cls}">${fo ? 'FAIL-OPEN' : esc(a.decision)}</span> ${fb ? `<span class="tag fb">${L==='vi'?'DỰ PHÒNG':'FALLBACK'}</span>` : ''} ${a.retro ? `<span class="tag retro">${L==='vi'?'HỒI TỐ':'RETRO'}</span>` : ''} ${a.zero_day ? '<span class="tag z">ZERO-DAY</span>' : ''} ${a.injected ? `<span class="tag inj">${L==='vi'?'MÔ PHỎNG':'INJECTED'}</span>` : ''}`;
  // Hiển thị tối đa bốn mã/lý do phát hiện theo ngôn ngữ đang chọn.
  const rs = (a.reasons || []).slice(0, 4).map(r => `<li><code>${esc(r.code)}</code> ${esc(L === 'vi' ? r.vi : r.en)}</li>`).join('');
  // Trả markup của thẻ, gồm loại event, hai số liên quan, risk/anomaly và thời gian.
  return `<div class="al ${cls}" data-id="${esc(a.event_id)}"><div class="top"><div>${tags} <b>${esc((T[a.event_type] || [a.event_type, a.event_type])[L==='vi'?0:1])}</b> <span class="mono" style="color:var(--mut)">${esc(a.party)} → ${esc(a.counterparty)}</span></div><span class="mono">${a.zero_day && a.anomaly_score != null ? 'anomaly +' + Number(a.anomaly_score).toFixed(3) : (a.risk != null ? 'risk ' + Number(a.risk).toFixed(3) : '')} · ${esc(a.time)}</span></div><div class="act">${esc(L === 'vi' ? a.action_vi : a.action_en)}</div><ul>${rs}</ul></div>`;
}
// Cập nhật các KPI, trạng thái breaker và danh sách cảnh báo từ phản hồi /api/state.
function render(s){
  // Lưu trạng thái cuối để có thể vẽ lại khi đổi ngôn ngữ.
  last = s;
  // Cập nhật version, tổng sự kiện, cơ cấu loại sự kiện và throughput.
  $('#ver').textContent = s.model_version;
  $('#k_proc').textContent = fmt(s.processed);
  $('#k_mix').textContent = `${fmt(s.by_type.CALL)} ${L==='vi'?'gọi':'calls'} · ${fmt(s.by_type.SMS)} SMS · ${fmt(s.by_type.AUDIO)} audio`;
  $('#k_eps').textContent = fmt(Math.round(s.eps));
  // Cập nhật phân vị latency và tô màu theo SLA Tier 1 30 ms.
  $('#k_p99').textContent = s.latency_ms.p99.toFixed(2);
  $('#k_p99').parentElement.className = 'k ' + (s.latency_ms.p99 < 30 ? 'green' : 'red');
  // Hiển thị số block, flag, fail-open, tỷ lệ bắt gian lận và số chứng cứ.
  $('#k_blk').textContent = fmt(s.decisions.BLOCK); $('#k_flg').textContent = fmt(s.decisions.FLAG); $('#k_fo').textContent = fmt(s.decisions.FAIL_OPEN);
  $('#k_tp').textContent = `${fmt(s.truth.fraud_caught)}/${fmt(s.truth.fraud_seen)}`;
  $('#k_ev').textContent = fmt(s.evidence_written);
  // Cập nhật nhãn circuit breaker và kiểu viền/màu khi trạng thái không còn CLOSED.
  const st = s.breaker.state; $('#cbpill').textContent = 'BREAKER: ' + st; $('#cbpill').style.borderColor = st === 'CLOSED' ? 'var(--line)' : 'var(--red)'; $('#cbpill').style.color = st === 'CLOSED' ? '' : 'var(--red2)';
  // Trạng thái lớp chịu lỗi: luật dự phòng đã chặn bao nhiêu, hàng đợi chấm lại, cảnh báo hồi tố.
  const R = s.resilience || {}, vi = L === 'vi';
  $('#resil').textContent = vi
    ? `Dự phòng: chặn ${fmt(R.fallback_block)} · cờ ${fmt(R.fallback_flag)} · danh sách chặn ${fmt(R.denylist_size)}\nChờ chấm lại ${fmt(R.rescore_pending)}/${fmt(R.rescore_capacity)} · đã chấm lại ${fmt(R.rescored)} · bỏ do đầy ${fmt(R.rescore_dropped)}\nCảnh báo hồi tố ${fmt(R.retro_alerts)} · kết luận đến muộn ${fmt(R.late_verdicts)}`
    : `Fallback: blocked ${fmt(R.fallback_block)} · flagged ${fmt(R.fallback_flag)} · deny-list ${fmt(R.denylist_size)}\nRe-score pending ${fmt(R.rescore_pending)}/${fmt(R.rescore_capacity)} · re-scored ${fmt(R.rescored)} · dropped (full) ${fmt(R.rescore_dropped)}\nRetro alerts ${fmt(R.retro_alerts)} · late verdicts ${fmt(R.late_verdicts)}`;
  // Tạo lại feed và gắn sự kiện click để mở JSON chứng cứ của thẻ tương ứng.
  if (!holding('#feed')) $('#feed').innerHTML = s.alerts.length ? s.alerts.map(card).join('')
    : `<div class="empty">${vi ? 'Chưa có cảnh báo' : 'No alerts yet'}</div>`;
  document.querySelectorAll('.al').forEach(el => {
    el.tabIndex = 0; el.setAttribute('role', 'button');
    const open = () => { const a = s.alerts.find(x => x.event_id === el.dataset.id); if (!a) return; OPEN_ID = a.event_id; $('#rvres').textContent = '—'; $('#dbody').textContent = JSON.stringify(a, null, 2); openDrawer(el); };
    el.onclick = open;
    el.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } };
  });
}
// Ngăn bằng chứng là hộp thoại: focus vào trong khi mở, Tab không thoát ra ngoài, Escape đóng, focus trả về thẻ đã mở.
let DRAWER_OPENER = null;
const drawerFocusables = () => [...$('#drawer').querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled && el.offsetParent !== null);
function openDrawer(opener){
  const d = $('#drawer');
  if (!d.classList.contains('open')) DRAWER_OPENER = opener || document.activeElement;
  d.classList.add('open'); d.setAttribute('aria-hidden', 'false');
  setTimeout(() => $('#dclose').focus(), 30);
}
function closeDrawer(){
  const d = $('#drawer'); if (!d.classList.contains('open')) return;
  d.classList.remove('open'); d.setAttribute('aria-hidden', 'true');
  const o = DRAWER_OPENER; DRAWER_OPENER = null;
  // Feed có thể đã vẽ lại: tìm lại thẻ cùng event_id để trả focus.
  const again = o && o.dataset && o.dataset.id ? [...document.querySelectorAll('#feed .al')].find(x => x.dataset.id === o.dataset.id) : null;
  const target = (o && document.contains(o)) ? o : again;
  if (target && target.focus) target.focus();
}
$('#dclose').onclick = closeDrawer;
document.addEventListener('keydown', ev => {
  if (!$('#drawer').classList.contains('open')) return;
  if (ev.key === 'Escape') { ev.preventDefault(); closeDrawer(); return; }
  if (ev.key === 'Tab') {
    const f = drawerFocusables(); if (!f.length) return;
    const first = f[0], lastEl = f[f.length - 1], inside = $('#drawer').contains(document.activeElement);
    if (ev.shiftKey && (document.activeElement === first || !inside)) { ev.preventDefault(); lastEl.focus(); }
    else if (!ev.shiftKey && (document.activeElement === lastEl || !inside)) { ev.preventDefault(); first.focus(); }
  }
});
// Mở số đầy đủ của cảnh báo đang xem: bắt buộc nhập lý do; server ghi nhật ký và AI agent đếm số lần mở.
let OPEN_ID = null;
$('#reveal').onclick = async () => {
  const reason = prompt(L === 'vi' ? 'Lý do cần xem số đầy đủ (bắt buộc, sẽ ghi vào nhật ký):' : 'Reason (required, logged):', '');
  if (!reason) return;
  try { const r = await api('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: OPEN_ID, reason }) });
        $('#rvres').textContent = `${r.party} → ${r.counterparty}${r.imei ? '\nIMEI ' + r.imei : ''}\n${r.note}`; }
  catch(e){ $('#rvres').textContent = '✘ ' + e.message; }
};
// Thăm dò API trạng thái mỗi 700 ms; nếu một request lỗi, lịch thăm dò tiếp tục chạy.
// Lỗi không còn bị nuốt im lặng: hiện thanh báo lỗi, tự ẩn khi lần thăm dò sau thành công.
let POLL_FAILS = 0;
function pollError(e){
  POLL_FAILS++;
  if (POLL_FAILS < 2 || String(e && e.message) === '401') return;   // bỏ qua một lần chập chờn; 401 đã mở màn đăng nhập
  const vi = L === 'vi', bar = $('#pollErr');
  bar.textContent = vi ? `Mất kết nối tới máy chủ (/api/state): ${e && e.message ? e.message : 'lỗi mạng'}. Số liệu trên màn hình có thể đã cũ, đang thử lại…`
                       : `Lost connection to the server (/api/state): ${e && e.message ? e.message : 'network error'}. Figures may be stale, retrying…`;
  bar.classList.remove('hide');
}
function pollOk(){ POLL_FAILS = 0; $('#pollErr').classList.add('hide'); }
// Hidden tab: no polling. After failures: back off 0.7 -> 1.4 -> 2.8 ... up to 10 s.
async function poll(){
  if (ME && !document.hidden) { try { render(await api('/api/state')); pollOk(); } catch(e){ pollError(e); } }
  setTimeout(poll, POLL_FAILS ? Math.min(700 * 2 ** POLL_FAILS, 10000) : 700);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && ME) pollAgent(); });
// Gửi yêu cầu tiêm kiểu tấn công đang chọn, rồi tổng hợp quyết định để hiển thị kết quả.
document.querySelectorAll('[data-a]').forEach(b => b.onclick = async () => {
  // Báo hiệu đang xử lý, gửi POST tới API và đọc kết quả JSON của loại tấn công đã chọn.
  $('#res').textContent = '…';
  let r; try { r = await api('/api/inject?type=' + encodeURIComponent(b.dataset.a), { method: 'POST' }); } catch(e){ $('#res').textContent = '✘ ' + e.message; return; }
  if (!r || !Array.isArray(r.results)) { $('#res').textContent = '✘ ' + (L==='vi' ? 'Phản hồi không hợp lệ' : 'Invalid response'); return; }
  // Gộp số lượng theo cặp loại sự kiện/quyết định để tránh liệt kê từng event.
  const c = {}; r.results.forEach(x => c[x.event_type + ':' + x.decision] = (c[x.event_type + ':' + x.decision] || 0) + 1);
  // Chọn kết quả không ALLOW đầu tiên để đưa lý do phát hiện lên màn hình.
  const first = r.results.find(x => x.decision !== 'ALLOW');
  $('#res').textContent = `${r.attack}: ${r.results.length} ${L==='vi'?'sự kiện':'events'}\n` + Object.entries(c).map(([k,v]) => `  ${k.padEnd(16)} ${v}`).join('\n') + (first ? `\n${L==='vi'?'Lý do':'Reasons'}: ` + first.reasons.map(x => x.code).join(', ') : '');
});
// Bật chế độ latency/error hoặc tắt chaos bằng endpoint chịu lỗi.
document.querySelectorAll('[data-c]').forEach(b => b.onclick = async () => { try { await api('/api/chaos?mode=' + encodeURIComponent(b.dataset.c), { method: 'POST' }); } catch(e){ $('#res').textContent = '✘ ' + e.message; return; } document.querySelectorAll('[data-c]').forEach(x => x.classList.toggle('on', x === b && b.dataset.c !== 'off')); });
// Nhật ký thao tác: kiểm tra chuỗi + chữ ký, rồi liệt kê ai đã làm gì.
$('#audit').onclick = async () => {
  $('#ares').textContent = '…';
  let a; try { a = await api('/api/audit?n=12'); } catch(e){ $('#ares').textContent = '✘ ' + e.message; return; }
  const v = a.verify, vi = L === 'vi';
  const head = v.valid ? `✔ ${vi?'Hợp lệ':'Valid'} · ${fmt(v.records)} ${vi?'thao tác, chữ ký khớp từng người':'actions, every signature matches its user'}`
                       : `✘ ${vi?'Dòng':'Line'} ${v.first_bad_line}: ${v.problem}`;
  $('#ares').textContent = head + '\n' + a.entries.slice().reverse().map(r => `${new Date(r.entry.ts * 1000).toLocaleTimeString()}  ${r.entry.actor.padEnd(14)} ${r.entry.action} ${JSON.stringify(r.entry.params)}`).join('\n');
};
// Yêu cầu server xác minh hash chain và hiển thị kết quả kiểm tra toàn vẹn.
$('#verify').onclick = async () => { $('#vres').textContent = '…'; let v; try { v = await api('/api/evidence/verify'); } catch(e){ $('#vres').textContent = '✘ ' + e.message; return; } $('#vres').textContent = `${v.chain_valid ? '✔ ' + (L==='vi'?'Chuỗi hợp lệ':'Chain valid') : '✘ ' + (L==='vi'?'Bị sửa ở dòng ':'Tampered at line ') + v.first_bad_line}\n${fmt(v.records)} ${L==='vi'?'bản ghi':'records'} · last ${v.last_hash.slice(0, 24)}…`; };

// Tạo một hàng HTML cho bảng benchmark; sub chứa ghi chú phụ như TP/FP.
function row(label, val, sub){ return `<tr><td>${label}</td><td class="n">${val}</td><td class="n" style="color:var(--mut)">${sub ?? ''}</td></tr>`; }
// Dựng bảng độ chính xác, phát hiện theo họ tấn công và biểu đồ load test từ metrics đã nạp.
function renderMetrics(){
  // Lấy các phần dữ liệu benchmark và kết quả load test cần để dựng báo cáo.
  const b = metrics.benchmark, a = b.accuracy_test_50k, r = b.replicates_10x50k, lt = metrics.loadtest;
  // Chọn nhãn ngôn ngữ hiện tại cho tiêu đề và ghi chú trong bảng.
  const vi = L === 'vi';
  $('#bench').innerHTML = `<table>
    ${row(vi?'Sự kiện / gian lận':'Events / fraud', `${fmt(b.dataset.events)} / ${b.dataset.fraud_events}`, b.dataset.fraud_rate_pct + '%')}
    ${row(vi?'Chặn cứng: recall':'Hard block: recall', a.block.recall_pct + '%', `TP ${a.block.TP}`)}
    ${row(vi?'Chặn cứng: báo động giả':'Hard block: FPR', a.block.fpr_pct + '%', `FP ${a.block.FP}`)}
    ${row(vi?'Chặn + cờ: recall':'Block + flag: recall', a.block_or_flag.recall_pct + '%', `TP ${a.block_or_flag.TP}`)}
    ${row(vi?'Chặn + cờ: báo động giả':'Block + flag: FPR', a.block_or_flag.fpr_pct + '%', `FP ${a.block_or_flag.FP}`)}
    ${row(vi?'500k sự kiện: FPR chặn':'500k events: block FPR', r.block_fpr_pct + '%', `95% CI ≤ ${r.block_fpr_95ci_pct[1]}%`)}
    ${row(vi?'500k sự kiện: recall TB':'500k events: mean recall', r.recall_any_mean_pct + '%', `min ${r.recall_any_min_pct}%`)}
  </table><div class="note">${vi?'Mục tiêu: FPR chặn < 0,01%. Dữ liệu mô phỏng, không phải thuê bao thật.':'Target: block FPR < 0.01%. Simulated data, not real subscribers.'}</div>`;
  // Nhóm recall theo họ tấn công từ mười cửa sổ test để hiển thị mức trung bình.
  const fams = {}; r.per_window.forEach(w => Object.entries(w.by_family).forEach(([k, v]) => (fams[k] = fams[k] || []).push(v)));
  const name = Object.fromEntries(Object.entries(FRAUD_NAMES).map(([k, v]) => [k, v[vi ? 0 : 1]]));
  $('#fam').innerHTML = '<table>' + Object.entries(fams).map(([k, v]) => { const m = v.reduce((x, y) => x + y, 0) / v.length; return `<tr><td>${esc(name[k] || k)}</td><td style="width:45%"><div class="bar"><i style="width:${m}%"></i></div></td><td class="n">${m.toFixed(1)}%</td></tr>`; }).join('') + `</table><div class="note">${vi?'Tỷ lệ sự kiện gian lận bị chặn hoặc gắn cờ. Wangiri không có trong dữ liệu huấn luyện: chỉ mô hình không giám sát bắt được.':'Share of fraud events blocked or flagged. Wangiri never appears in training: only the unsupervised model catches it.'}</div>`;
  // Vẽ biểu đồ tải sau khi đã chuẩn bị các chuỗi dữ liệu cho từng cấu hình.
  // Chuẩn bị context canvas và xóa hình cũ trước khi vẽ lại biểu đồ.
  const c = $('#lt'), g = c.getContext('2d'), W = c.width, H = c.height; g.clearRect(0, 0, W, H);
  // Trích throughput và p99 của hai cấu hình load test làm các điểm biểu đồ.
  const s1 = (lt.single_process?.runs || []).map(x => [x.offered_rate_eps, x.latency_ms.CALL.p99]);
  const s2 = (lt.partitioned_2?.runs || []).map(x => [x.offered_rate_eps, x.call_p99_ms]);
  const s4 = (lt.partitioned_4?.runs || []).map(x => [x.offered_rate_eps, x.call_p99_ms]);
  // Số nhân lấy từ chính file kết quả, không viết cứng trong giao diện.
  const cores = lt.single_process?.hardware?.cpu_count;
  if (cores) $('#lt_title').textContent = (vi ? 'Tải streaming · máy ' : 'Streaming load · ') + cores + (vi ? ' nhân' : '-core machine');
  // Xác định giới hạn trục từ các tốc độ kiểm thử; trục latency luôn hiển thị đến 120 ms.
  const xs = [...s1, ...s2, ...s4].map(p => p[0]), xmax = Math.max(...xs) * 1.05, ymax = 120;
  // Chuyển tọa độ dữ liệu thành pixel trong vùng canvas và giới hạn trục dọc ở 120 ms.
  const X = v => 40 + v / xmax * (W - 55), Y = v => H - 28 - Math.min(v, ymax) / ymax * (H - 45);
  // Thiết lập màu chữ/lưới rồi vẽ các đường lưới và nhãn mốc độ trễ.
  // Màu biểu đồ lấy từ theme hiện tại (sáng / tối).
  const cLine = cssVar('--line'), cMut = cssVar('--mut'), cInk = cssVar('--ink');
  g.strokeStyle = cLine; g.fillStyle = cMut; g.font = '18px JetBrains Mono';
  [0, 30, 60, 90, 120].forEach(v => { g.beginPath(); g.moveTo(40, Y(v)); g.lineTo(W - 10, Y(v)); g.stroke(); g.fillText(v, 2, Y(v) + 6); });
  // Vẽ đường đứt tại giới hạn SLA 30 ms để đối chiếu nhanh.
  g.strokeStyle = '#e11d48'; g.setLineDash([6, 6]); g.beginPath(); g.moveTo(40, Y(30)); g.lineTo(W - 10, Y(30)); g.stroke(); g.setLineDash([]);
  // Vẽ từng chuỗi điểm; mỗi cấu hình có màu riêng và điểm tròn để nhận biết số đo.
  [[s1, cMut], [s2, cInk], [s4, '#e11d48']].forEach(([s, col]) => { g.strokeStyle = col; g.lineWidth = 3; g.beginPath(); s.forEach(([x, y], i) => i ? g.lineTo(X(x), Y(y)) : g.moveTo(X(x), Y(y))); g.stroke(); s.forEach(([x, y]) => { g.fillStyle = col; g.beginPath(); g.arc(X(x), Y(y), 5, 0, 7); g.fill(); }); });
  // Thêm chú giải trục hoành với đơn vị sự kiện/giây theo ngôn ngữ hiện tại.
  g.fillStyle = cMut; g.fillText((vi?'sự kiện/giây →':'events/s →') + ' ' + Math.round(xmax / 1000) + 'k', W - 230, H - 4);
  // Giải thích màu hai cấu hình và đường ngưỡng trong phần ghi chú dưới biểu đồ.
  // Mốc tốt nhất còn nằm trong ngân sách, lấy thẳng từ dữ liệu thay vì viết tay.
  const best = (lt.partitioned_4?.runs || []).filter(x => x.call_p99_ms <= 30).pop();
  // Mô tả biểu đồ cho trình đọc màn hình (canvas không có nội dung văn bản).
  const worst = Math.max(0, ...[...s1, ...s2, ...s4].map(p => p[1]));
  c.setAttribute('aria-label', vi
    ? `Biểu đồ đường: độ trễ p99 của Tier 1 (ms) theo tải tới ${fmt(Math.round(xmax))} sự kiện/giây, ba cấu hình (1 tiến trình, 2 và 4 phân vùng), ngưỡng 30 ms. p99 cao nhất ${worst.toFixed(1)} ms.` + (best ? ` Tốt nhất trong ngân sách: ${fmt(best.offered_rate_eps)} sự kiện/giây ở 4 phân vùng.` : '')
    : `Line chart: Tier-1 p99 latency (ms) vs load up to ${fmt(Math.round(xmax))} events/s for 1 process, 2 and 4 partitions, 30 ms budget line. Highest p99 ${worst.toFixed(1)} ms.` + (best ? ` Best within budget: ${fmt(best.offered_rate_eps)} events/s on 4 partitions.` : ''));
  const bestTxt = best ? (vi ? `Tốt nhất trong ngân sách: ${fmt(best.offered_rate_eps)} sự kiện/giây ở 4 phân vùng (p99 ${best.call_p99_ms} ms).`
                             : `Best within budget: ${fmt(best.offered_rate_eps)} events/s on 4 partitions (p99 ${best.call_p99_ms} ms).`) : '';
  $('#ltnote').innerHTML = (vi ? 'Xám: 1 tiến trình · Trắng: 2 phân vùng · Đỏ: 4 phân vùng. Đường đỏ đứt nét: ngân sách 30 ms. '
                               : 'Grey: 1 process · White: 2 partitions · Red: 4 partitions. Red dashed: the 30 ms budget. ') + bestTxt +
                           (vi ? ' Mục tiêu 50k TPS chưa đạt: cần chia phân vùng trên nhiều máy và một kho đặc trưng dùng chung.'
                               : ' The 50k TPS target is not met: it needs partitions across several machines and a shared feature store.');
}
// Nạp metrics lưu sẵn, render báo cáo, thiết lập ngôn ngữ và bắt đầu polling live.
// boot: xác định người dùng từ token, áp quyền, rồi nạp metrics; chưa có token thì mở màn đăng nhập.
async function boot(){
  if (!TOKEN){ showLogin(); return; }
  try { ME = await api('/api/whoami'); } catch(e){ return; }
  applyRole(); pollAgent();
  // Keep the previous metrics if the new payload cannot be rendered (an error body used to break
  // renderMetrics() on every later language / theme switch).
  try { const m = await api('/api/metrics'), prev = metrics; metrics = m; try { renderMetrics(); } catch(err){ metrics = prev; } } catch(e){}
}
applyLang(); boot(); poll();

// Navigation buttons (was onclick="location.href=..."; inline handlers are blocked by the CSP).
document.querySelectorAll('[data-href]').forEach(b => b.addEventListener('click', () => { location.href = b.dataset.href; }));
