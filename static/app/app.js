const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Hiển thị số theo kiểu Việt Nam: 84xxxxxxxxx -> 0xxx xxx xxx; số nước ngoài giữ dạng +.
// Số trong nước (0..., 1900..., 113) và tên thương hiệu giữ nguyên.
const pretty = n => { n = String(n || ''); if (/^84\d{9}$/.test(n)) { const d = '0' + n.slice(2); return d.slice(0, 4) + ' ' + d.slice(4, 7) + ' ' + d.slice(7); }
  return /^\d{11,}$/.test(n) && !n.startsWith('0') ? '+' + n : n; };
const store = { get(k){ try { return localStorage.getItem(k) || ''; } catch(e){ return ''; } }, set(k, v){ try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch(e){} } };
let TOKEN = store.get('tfs_app'), MSISDN = store.get('tfs_app_num'), items = [];

function toast(msg){ const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2600); }
async function api(url, opts = {}){
  const r = await fetch(url, { ...opts, headers: { ...(opts.headers || {}), Authorization: 'Bearer ' + TOKEN } });
  if (r.status === 401){ signOut(); throw new Error('Phiên đăng nhập đã hết hạn'); }
  if (r.status === 409 && url !== '/api/app/auth/me'){ show('phone'); }
  if (!r.ok){ let m = r.status; try { m = (await r.json()).detail; } catch(e){} throw new Error(m); }
  return r.json();
}
function show(tab){
  document.querySelectorAll('.tab').forEach(s => s.classList.toggle('on', s.id === tab));
  document.querySelectorAll('nav button').forEach(b => {
    const on = b.dataset.t === tab; b.classList.toggle('on', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  window.scrollTo(0, 0);
}
// Lỗi mạng hiển thị rõ (không nuốt lỗi im lặng); tự ẩn khi lần gọi sau thành công.
function netErr(msg){ const e = $('#netErr'); e.textContent = msg || ''; e.hidden = !msg; }
document.querySelectorAll('nav button').forEach(b => b.onclick = () => show(b.dataset.t));

// ------------------------------------------------------------------ đăng nhập (tài khoản thật)
let CFG = null;
async function cfg(){ if (!CFG) { try { CFG = await (await fetch('/api/auth/config')).json(); } catch(e){ CFG = {}; } } return CFG; }
function lerr(m){ $('#lerr').textContent = m || ''; }
async function authed(r){
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.status);
  TOKEN = j.token; store.set('tfs_app', TOKEN); await afterLogin(j.me);
}
async function afterLogin(me){
  if (!me) {
    try { me = await api('/api/app/auth/me'); netErr(''); }
    catch(e){
      // Offline PWA start or server error: never leave a blank screen; retry while still signed in.
      if (!document.querySelector('.tab.on')) show('login');
      if (TOKEN) { netErr('Không kết nối được máy chủ, đang thử lại…'); setTimeout(() => { if (TOKEN) afterLogin(); }, 5000); }
      return;
    }
  }
  if (me.msisdn) { MSISDN = me.msisdn; store.set('tfs_app_num', MSISDN); start(); return; }
  const c = await cfg();
  $('#ph_demo').style.display = c.phone_verification === 'demo_pool' ? '' : 'none';
  $('#ph_otp').style.display = c.phone_verification === 'otp' ? 'grid' : 'none';
  $('#ph_none').style.display = c.phone_verification === 'unavailable' ? '' : 'none';
  show('phone');
}
async function emailAuth(path){
  lerr('');
  try { await authed(await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' },
                                         body: JSON.stringify({ email: $('#email').value.trim(), password: $('#pw').value }) })); }
  catch(e){ lerr(String(e.message)); }
}
$('#signin').onclick = () => emailAuth('/api/app/auth/login');
$('#signup').onclick = () => emailAuth('/api/app/auth/register');
$('#claim').onclick = async () => {
  try { const r = await api('/api/app/phone/claim-demo', { method: 'POST' }); MSISDN = r.msisdn; store.set('tfs_app_num', MSISDN); start(); }
  catch(e){ toast('Không cấp được số: ' + e.message); }
};
$('#otpsend').onclick = async () => {
  try { await api('/api/app/phone/otp/start', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ phone: $('#phnum').value }) });
        toast('Đã gửi mã OTP tới số của bạn'); }
  catch(e){ toast('Không gửi được OTP: ' + e.message); }
};
$('#otpok').onclick = async () => {
  try { const r = await api('/api/app/phone/otp/verify', { method: 'POST', headers: { 'content-type': 'application/json' },
                                                           body: JSON.stringify({ phone: $('#phnum').value, code: $('#otpcode').value }) });
        MSISDN = r.msisdn; store.set('tfs_app_num', MSISDN); start(); }
  catch(e){ toast(e.message); }
};
// Google Sign-In (hiện khi máy chủ có GOOGLE_CLIENT_ID); lỗi tải script thì ẩn, vẫn dùng email được.
async function initGoogle(){
  const c = await cfg();
  if (!c.google_client_id) return;
  // Inside the Android shell (WebView), Google refuses OAuth in embedded browsers ("disallowed_useragent"):
  // keep the button hidden there and let the user sign in with email.
  if (/\bLaChanAndroid\//.test(navigator.userAgent)) return;
  const sc = document.createElement('script'); sc.src = 'https://accounts.google.com/gsi/client'; sc.async = true;
  sc.onload = () => {
    google.accounts.id.initialize({ client_id: c.google_client_id, ux_mode: 'popup', callback: async (resp) => {
      try { await authed(await fetch('/api/app/auth/google', { method: 'POST', headers: { 'content-type': 'application/json' },
                                                               body: JSON.stringify({ credential: resp.credential }) })); }
      catch(e){ lerr(String(e.message)); } } });
    google.accounts.id.renderButton($('#gbtn'), { theme: 'outline', size: 'large', shape: 'pill', text: 'continue_with', locale: 'vi', width: 300 });
    $('#gdiv').style.display = '';
  };
  document.head.appendChild(sc);
}
function signOut(){
  if (TOKEN) pushUnsubscribe(TOKEN);   // thiết bị dùng chung: không nhận cảnh báo của tài khoản cũ
  if (TOKEN) fetch('/api/app/auth/logout', { method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN } }).catch(() => {});
  TOKEN = ''; MSISDN = ''; store.set('tfs_app', ''); store.set('tfs_app_num', ''); $('#nav').style.display = 'none'; $('#me').textContent = ''; show('login');
}
$('#out2').onclick = signOut;
$('#out').onclick = signOut;

// ------------------------------------------------------------------ hộp thư
const ICON = { CALL: '📞', SMS: '✉️', AUDIO: '🎙️', FAMILY: '👪', WARN: '📣' };
const LABEL = { BLOCK: 'Đã chặn', FLAG: 'Nghi lừa đảo', ALLOW: 'An toàn' };
function row(i){
  const what = i.type === 'SMS' ? (i.text || 'Tin nhắn') : (i.action || (i.type === 'AUDIO' ? 'Trong cuộc gọi' : 'Cuộc gọi'));
  const bg = i.decision === 'BLOCK' ? 'var(--red-bg)' : i.decision === 'FLAG' ? 'var(--amber-bg)' : 'var(--green-bg)';
  const extra = (i.retro ? ' · phát hiện sau sự cố' : '') + (i.degraded ? ' · chế độ dự phòng' : '');
  const open = OPEN.has(String(i.id));
  return `<div class="item${open ? ' open' : ''}" data-id="${esc(i.id)}" role="button" tabindex="0" aria-expanded="${open}"><div class="ic" style="background:${bg}" aria-hidden="true">${ICON[i.type] || '•'}</div><div class="body">
    <div class="t"><span class="num">${esc(pretty(i.from))}</span><span class="chip c-${esc(i.decision)}">${LABEL[i.decision] || esc(i.decision)}</span></div>
    <div class="sub">${esc(what)}</div><div class="time">${esc(i.time)}${extra}</div>
    <ul>${(i.reasons || []).map(r => `<li>${esc(r)}</li>`).join('') || '<li>Không có dấu hiệu bất thường.</li>'}</ul></div></div>`;
}
// Hàng đang mở và hàng đang được focus được giữ nguyên qua mỗi lần làm mới (2 giây/lần).
const OPEN = new Set();
function renderInbox(){
  const act = document.activeElement, focusId = act && act.classList && act.classList.contains('item') ? act.dataset.id : null;
  const focusBox = focusId ? act.parentElement.id : null;
  const html = items.map(row).join('');
  $('#list').innerHTML = html || '<div class="empty">Chưa có cuộc gọi hay tin nhắn nào.</div>';
  $('#recent').innerHTML = items.slice(0, 4).map(row).join('') || '<div class="empty">Chưa có cuộc gọi hay tin nhắn nào.</div>';
  if (focusId && focusBox) { const again = [...document.querySelectorAll('#' + focusBox + ' .item')].find(x => x.dataset.id === focusId); if (again) again.focus(); }
  document.querySelectorAll('.item').forEach(el => {
    const toggle = () => { const on = el.classList.toggle('open'); el.setAttribute('aria-expanded', String(on)); if (on) OPEN.add(el.dataset.id); else OPEN.delete(el.dataset.id); };
    el.onclick = toggle;
    el.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); } };
  });
}
let LOADED = false, PAUSE = 0;
const SHOWN = new Set();                   // sự kiện đã hiện màn hình (bấm nút giả lập) thì không báo lại
async function refresh(){
  if (!TOKEN || !MSISDN) return;
  try {
    const r = await api('/api/app/inbox');
    // Thông báo nổi khi có cảnh báo mới về người thân hoặc từ nhà mạng.
    const seen = new Set(items.map(i => i.id));
    const fresh = LOADED ? r.items.filter(i => !seen.has(i.id)) : [];
    const fam = fresh.filter(i => i.type === 'FAMILY' || i.type === 'WARN');
    if (fam.length) toast((fam[0].type === 'FAMILY' ? '👪 ' : '📣 ') + fam[0].action);
    const calls = fresh.filter(i => ['CALL', 'SMS', 'AUDIO'].includes(i.type) && !SHOWN.has(i.id));
    if (PAUSE) calls.forEach(i => SHOWN.add(i.id));   // đang bấm nút giả lập: màn hình của nút sẽ hiện, không báo trùng
    else alertIncoming(calls);
    LOADED = true;
    items = r.items;
    $('#st_b').textContent = r.stats.blocked; $('#st_w').textContent = r.stats.warned; $('#st_a').textContent = r.stats.allowed;
    renderInbox();
    netErr('');
  } catch(e){ netErr('Không tải được lịch sử cuộc gọi: ' + e.message + '. Đang thử lại…'); }
}

// ------------------------------------------------------------------ cuộc gọi / tin nhắn tới thật (qua hộp thư)
// Điện thoại thật: cuộc bị nhà mạng chặn KHÔNG đổ chuông, chỉ để lại một thông báo; cuộc nghi ngờ vẫn đổ chuông
// kèm màn hình cảnh báo; cuộc bình thường đổ chuông như mọi khi.
const RANK = { ALLOW: 0, FLAG: 1, BLOCK: 2 };
function alertIncoming(fresh){
  if (!fresh.length) return;
  // Cùng một cuộc gọi có thể có sự kiện CALL rồi AUDIO (giọng nói trong cuộc gọi): lấy quyết định nặng nhất.
  const top = fresh.slice().sort((a, b) => RANK[b.decision] - RANK[a.decision])[0];
  fresh.forEach(i => SHOWN.add(i.id));
  const what = top.type === 'SMS' ? 'tin nhắn' : 'cuộc gọi';
  if (top.decision === 'FLAG' || (top.decision === 'BLOCK' && top.type === 'AUDIO')) { incoming(top); notify(top); return; }
  const title = top.decision === 'BLOCK' ? `🚫 Đã chặn ${what} lừa đảo` : top.type === 'SMS' ? '✉️ Tin nhắn mới' : '📞 Cuộc gọi đến';
  const body = top.decision === 'BLOCK' ? (top.reasons[0] || 'Dấu hiệu lừa đảo') : 'Không có dấu hiệu lừa đảo';
  heads(top.decision, title, pretty(top.from), body, () => incoming(top));
  notify(top, title, body);
}
function heads(decision, title, who, body, onTap){
  const h = $('#heads');
  h.className = 'heads ' + decision;
  h.innerHTML = `<div class="ic" aria-hidden="true">${decision === 'BLOCK' ? '🛡️' : decision === 'FLAG' ? '⚠️' : '📞'}</div>
    <div><b>${esc(title)}</b><span>${esc(who)} · ${esc(body)}</span><small>Lá chắn · bây giờ · chạm để xem lý do</small></div>`;
  h.onclick = () => { h.classList.remove('show'); onTap && onTap(); };
  requestAnimationFrame(() => h.classList.add('show'));
  clearTimeout(h._t); h._t = setTimeout(() => h.classList.remove('show'), 5200);
}
// Màn hình cuộc gọi/tin nhắn đến là hộp thoại thật: focus vào trong, Tab chỉ chạy trong hộp thoại,
// Escape để đóng, đóng xong trả focus về nút đã mở nó.
let OV_OPEN = false, OV_OPENER = null, OV_ONCLOSE = null;
const ovFocusables = () => [...$('#ov').querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled);
function openOv(onClose){
  if (!OV_OPEN) OV_OPENER = document.activeElement;
  OV_OPEN = true; OV_ONCLOSE = onClose || null;
  const ov = $('#ov'), first = ovFocusables()[0];
  ov.setAttribute('aria-label', (ov.querySelector('.lab')?.textContent || 'Cuộc gọi đến') + ' ' + (ov.querySelector('.who')?.textContent || ''));
  (first || ov).focus();
}
function closeOv(){
  const ov = $('#ov'); ov.className = 'overlay';
  if (!OV_OPEN) return;
  OV_OPEN = false;
  const cb = OV_ONCLOSE, opener = OV_OPENER; OV_ONCLOSE = null; OV_OPENER = null;
  if (opener && opener !== document.body && document.contains(opener) && opener.focus) opener.focus();
  if (cb) cb();
}
document.addEventListener('keydown', ev => {
  if (!OV_OPEN) return;
  if (ev.key === 'Escape') { ev.preventDefault(); closeOv(); return; }
  if (ev.key === 'Tab') {
    const f = ovFocusables(); if (!f.length) { ev.preventDefault(); $('#ov').focus(); return; }
    const first = f[0], lastEl = f[f.length - 1];
    if (ev.shiftKey && (document.activeElement === first || !$('#ov').contains(document.activeElement))) { ev.preventDefault(); lastEl.focus(); }
    else if (!ev.shiftKey && (document.activeElement === lastEl || !$('#ov').contains(document.activeElement))) { ev.preventDefault(); first.focus(); }
  }
});
function incoming(i){
  const ov = $('#ov'); const reasons = (i.reasons || []).slice(0, 3).map(r => `<li>${esc(r)}</li>`).join('');
  if (i.type === 'SMS'){
    const head = { BLOCK: '🚫 Tin nhắn lừa đảo đã bị chặn', FLAG: '⚠️ Cảnh báo: tin nhắn nghi lừa đảo', ALLOW: '✉️ Tin nhắn mới' }[i.decision];
    ov.innerHTML = `<div><div class="lab">Tin nhắn từ</div><div class="who">${esc(pretty(i.from))}</div>
      ${i.text ? `<div class="sms">${esc(i.text)}</div>` : ''}
      <div class="banner"><b>${head}</b>${i.decision === 'BLOCK' ? '<div style="font-size:14px;margin-bottom:6px">Tin nhắn đã được nhà mạng cách ly, bạn sẽ không nhận được nó.</div>' : ''}<ul>${reasons || '<li>Không có dấu hiệu bất thường.</li>'}</ul></div></div>
      <div class="acts"><button class="b-rep" data-rep="${esc(i.from)}">Báo cáo</button><button class="b-ok" data-close>Đóng</button></div>`;
  } else {
    const inCall = i.type === 'AUDIO';
    const head = inCall ? (i.decision === 'BLOCK' ? '🚫 Đã ngắt cuộc gọi: phát hiện dấu hiệu lừa đảo trong cuộc gọi' : '⚠️ Cảnh báo trong cuộc gọi: hãy cẩn thận, đừng đọc OTP')
               : { BLOCK: '🚫 Nhà mạng đã chặn cuộc gọi lừa đảo', FLAG: '⚠️ Cuộc gọi nghi lừa đảo', ALLOW: '✅ Không có dấu hiệu lừa đảo' }[i.decision];
    const acts = i.decision === 'BLOCK'
      ? `<button class="b-rep" data-rep="${esc(i.from)}">Báo cáo</button><button class="b-ok" data-close>Đã hiểu</button>`
      : `<button class="b-dec" data-close>Từ chối</button><button class="b-acc" data-close>${i.decision === 'FLAG' ? 'Vẫn nghe' : 'Nghe'}</button>`;
    ov.innerHTML = `<div><div class="lab">${i.decision === 'BLOCK' && !inCall ? 'Cuộc gọi bị chặn' : 'Cuộc gọi đến'}</div><div class="who">${esc(pretty(i.from))}</div>
      <div class="banner"><b>${head}</b>${i.decision === 'BLOCK' && !inCall ? '<div style="font-size:14px;margin-bottom:6px">Điện thoại của bạn không đổ chuông với cuộc gọi này.</div>' : ''}<ul>${reasons || '<li>Số gọi có lịch sử hoạt động bình thường.</li>'}</ul></div></div>
      <div class="acts">${acts}</div>`;
  }
  ov.className = 'overlay show ' + i.decision;
  ov.querySelectorAll('[data-close]').forEach(b => b.onclick = () => closeOv());
  ov.querySelectorAll('[data-rep]').forEach(b => b.onclick = async () => { await report(b.dataset.rep, 'Báo cáo từ màn hình cảnh báo'); closeOv(); });
  openOv();
}
// Thông báo hệ thống (khi người dùng cho phép và app đang chạy nền).
function notify(i, title, body){
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;
  title = title || (i.decision === 'BLOCK' ? '🚫 Đã chặn lừa đảo' : '⚠️ Nghi lừa đảo');
  body = `${pretty(i.from)} · ${body || i.reasons[0] || ''}`;
  navigator.serviceWorker?.ready.then(r => r.showNotification(title, { body, tag: i.id, icon: 'icon.svg' })).catch(() => {});
}
// Web Push (VAPID): nhận cảnh báo cả khi app đã đóng. Payload không chứa số điện thoại / nội dung tin.
function b64uToBytes(s){
  const pad = '='.repeat((4 - s.length % 4) % 4), raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
async function pushSubscribe(){
  if (!('PushManager' in window) || !navigator.serviceWorker) return false;
  try {
    const { public_key } = await api('/api/app/push/key');
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(public_key) });
    await api('/api/app/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub.toJSON()) });
    return true;
  } catch(e){ return false; }
}
async function pushUnsubscribe(token){
  try {
    const reg = await navigator.serviceWorker?.ready, sub = reg && await reg.pushManager?.getSubscription();
    if (!sub) return;
    await fetch('/api/app/push/unsubscribe', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
                                               body: JSON.stringify({ endpoint: sub.endpoint }) }).catch(() => {});
    await sub.unsubscribe();
  } catch(e){}
}
$('#notifBtn').onclick = async () => {
  if (!('Notification' in window)) { toast('Trình duyệt này không hỗ trợ thông báo.'); return; }
  const p = await Notification.requestPermission();
  if (p !== 'granted') { toast('Chưa được phép gửi thông báo.'); return; }
  toast(await pushSubscribe() ? 'Đã bật thông báo, kể cả khi app đã đóng.' : 'Đã bật thông báo khi app chạy nền.');
};
$('#runBtn').onclick = async () => {
  const b = $('#runBtn'); b.disabled = true;
  try {
    const r = await api('/api/app/simulate-stream', { method: 'POST' });
    let left = r.seconds + 4;
    $('#runInfo').textContent = `Đang chạy thử: ${r.events} cuộc gọi / tin nhắn sẽ tới trong khoảng ${r.seconds} giây.`;
    clearInterval(b._t);
    b._t = setInterval(() => {
      left -= 1;
      if (left <= 0) { clearInterval(b._t); b.disabled = false; $('#runInfo').textContent = 'Xong. Xem lại toàn bộ trong mục Lịch sử.'; return; }
      $('#runInfo').textContent = `Đang chạy thử… còn khoảng ${left} giây.`;
    }, 1000);
  } catch(e){ toast('Không chạy thử được: ' + e.message); b.disabled = false; }
};

// ------------------------------------------------------------------ màn hình cuộc gọi / tin nhắn đến (demo)
function overlay(kind, res){
  // Lấy sự kiện "mạnh nhất" gửi tới người dùng: âm thanh trong cuộc gọi có thể ngắt cuộc gọi đã qua bước báo hiệu.
  const rank = { ALLOW: 0, FLAG: 1, BLOCK: 2 };
  const call = res.find(r => r.event_type === 'CALL'), sms = res.find(r => r.event_type === 'SMS');
  const audio = res.filter(r => r.event_type === 'AUDIO').sort((a, b) => rank[b.decision] - rank[a.decision])[0];
  const main = sms || call || audio;
  if (!main){ toast('Không có sự kiện nào tới số của bạn.'); return; }
  const reasons = (main.reasons || []).slice(0, 3).map(r => `<li>${esc(r.vi)}</li>`).join('');
  const ov = $('#ov'); let html;
  if (sms){
    const head = { BLOCK: '🚫 Tin nhắn lừa đảo đã bị chặn', FLAG: '⚠️ Cảnh báo: tin nhắn nghi lừa đảo', ALLOW: '✉️ Tin nhắn mới' }[sms.decision];
    html = `<div><div class="lab">Tin nhắn từ</div><div class="who">${esc(pretty(sms.party))}</div>
      <div class="sms">${esc(sms.text)}</div>
      <div class="banner"><b>${head}</b>${sms.decision === 'BLOCK' ? '<div style="font-size:14px;margin-bottom:6px">Tin nhắn đã được nhà mạng cách ly, bạn sẽ không nhận được nó.</div>' : ''}<ul>${reasons || '<li>Không có dấu hiệu bất thường.</li>'}</ul></div></div>
      <div class="acts"><button class="b-rep" data-rep="${esc(sms.party)}">Báo cáo</button><button class="b-ok" data-close>Đóng</button></div>`;
    ov.className = 'overlay show ' + sms.decision;
  } else {
    let d = call ? call.decision : 'ALLOW';
    const cut = audio && rank[audio.decision] > rank[d];
    if (cut) d = audio.decision;
    // Cuộc gọi im lặng thu giọng (A106/A107) có thông điệp riêng: đừng nói gì thêm.
    const harvest = cut && (audio.reasons || []).some(r => r.code === 'A106' || r.code === 'A107');
    const head = harvest ? (audio.decision === 'BLOCK' ? '🤐 Đã ngắt cuộc gọi: người gọi im lặng để ghi âm giọng bạn' : '🤐 Cảnh báo: người gọi im lặng, có thể đang ghi âm giọng bạn. Đừng nói gì thêm, hãy cúp máy')
               : cut ? (audio.decision === 'BLOCK' ? '🚫 Đã ngắt cuộc gọi: phát hiện giọng nói giả / kịch bản lừa đảo' : '⚠️ Cảnh báo trong cuộc gọi: giọng nói đáng ngờ')
               : { BLOCK: '🚫 Nhà mạng đã chặn cuộc gọi lừa đảo', FLAG: '⚠️ Cuộc gọi nghi lừa đảo', ALLOW: '✅ Không có dấu hiệu lừa đảo' }[d];
    const rs = cut ? (audio.reasons || []).slice(0, 3).map(r => `<li>${esc(r.vi)}</li>`).join('') : reasons;
    // Giả mạo số tổng đài: số hiển thị là số thật của ngân hàng/cơ quan, chỉ cuộc gọi này là giả.
    const spoofed = (main.reasons || []).some(r => r.code === 'R502' || r.code === 'F003');
    const sub = (d === 'BLOCK' && !cut ? '<div style="font-size:14px;margin-bottom:6px">Điện thoại của bạn sẽ không đổ chuông với cuộc gọi này.</div>' : '')
      + (spoofed ? '<div style="font-size:14px;margin-bottom:6px">Số hiển thị là tổng đài thật của ngân hàng/cơ quan, nhưng cuộc gọi này không do họ thực hiện.</div>' : '');
    const acts = d === 'BLOCK'
      ? `<button class="b-rep" data-rep="${esc(main.party)}">Báo cáo</button><button class="b-ok" data-close>Đã hiểu</button>`
      : `<button class="b-dec" data-close>Từ chối</button><button class="b-acc" data-close>${d === 'FLAG' ? 'Vẫn nghe' : 'Nghe'}</button>`;
    html = `<div><div class="lab">${d === 'BLOCK' && !cut ? 'Cuộc gọi bị chặn' : 'Cuộc gọi đến'}</div><div class="who">${esc(pretty(main.party))}</div>
      <div class="banner"><b>${head}</b>${sub}<ul>${rs || '<li>Số gọi có lịch sử hoạt động bình thường.</li>'}</ul></div></div>
      <div class="acts">${acts}</div>`;
    ov.className = 'overlay show ' + d;
  }
  ov.innerHTML = html;
  ov.querySelectorAll('[data-close]').forEach(b => b.onclick = () => closeOv());
  ov.querySelectorAll('[data-rep]').forEach(b => b.onclick = async () => { await report(b.dataset.rep, 'Báo cáo từ màn hình cảnh báo'); closeOv(); });
  openOv(refresh);
}
document.querySelectorAll('[data-k]').forEach(b => b.onclick = async () => {
  b.disabled = true; const old = b.innerHTML; b.textContent = '…'; PAUSE++;
  try {
    const r = await api('/api/app/simulate?kind=' + b.dataset.k, { method: 'POST' });
    (r.results || []).forEach(x => x.event_id && SHOWN.add(x.event_id));   // đã hiện màn hình rồi, hộp thư không báo lại
    overlay(b.dataset.k, r.results);
  }
  catch(e){ toast('Không giả lập được: ' + e.message); }
  PAUSE--; b.disabled = false; b.innerHTML = old;
});

// ------------------------------------------------------------------ tra cứu & báo cáo
$('#go').onclick = async () => {
  const n = $('#q').value.trim();
  if (!n) { $('#vres').innerHTML = '<div class="err-msg" role="alert">Hãy nhập số điện thoại cần tra cứu.</div>'; $('#q').focus(); return; }
  const go = $('#go'); go.disabled = true; go.setAttribute('aria-busy', 'true');
  $('#vres').innerHTML = '<div class="loading" role="status">Đang tra cứu…</div>';
  try {
    const r = await api('/api/app/lookup?number=' + encodeURIComponent(n));
    $('#vres').innerHTML = `<div class="verdict v-${esc(r.level)}"><b>${esc(r.verdict)}</b><span>${esc(pretty(r.number))} · nhà mạng chặn ${esc(r.blocked)} lần · cảnh báo ${esc(r.flagged)} lần · ${esc(r.user_reports)} người dùng báo cáo</span></div>`;
  } catch(e){ $('#vres').innerHTML = `<div class="err-msg" role="alert">Không tra cứu được: ${esc(e.message)}</div>`; }
  finally { go.disabled = false; go.removeAttribute('aria-busy'); }
};
async function report(number, why){
  try { const r = await api('/api/app/report?number=' + encodeURIComponent(number) + '&reason=' + encodeURIComponent(why), { method: 'POST' }); toast('Đã gửi báo cáo. ' + r.note); }
  catch(e){ toast('Không gửi được: ' + e.message); }
}
$('#rep').onclick = () => { const n = $('#rnum').value.trim(); if (n) report(n, $('#rwhy').value); };

// ------------------------------------------------------------------ bảo vệ người thân
async function loadFamily(){
  if (!TOKEN || !MSISDN) return;
  try {
    const f = await api('/api/app/family');
    $('#fam_recv').innerHTML = f.invites_received.map(g => `<div class="verdict v-warn" style="margin:0 0 10px"><b>${esc(pretty(g))} muốn nhận cảnh báo khi bạn gặp lừa đảo</b>
      <div style="display:flex;gap:8px;margin-top:8px"><button class="pri" data-acc="${esc(g)}">Đồng ý</button><button data-dec="${esc(g)}">Từ chối</button></div></div>`).join('');
    const parts = [];
    if (f.protecting.length) parts.push('Bạn đang bảo vệ: ' + f.protecting.map(x => esc(pretty(x))).join(', '));
    if (f.guardians.length) parts.push('Người bảo vệ bạn: ' + f.guardians.map(g => `${esc(pretty(g))} <a href="#" class="rm" data-rm="${esc(g)}" aria-label="Gỡ người bảo vệ ${esc(pretty(g))}">gỡ</a>`).join(', '));
    if (f.invites_sent.length) parts.push('Đã mời, chờ đồng ý: ' + f.invites_sent.map(x => esc(pretty(x))).join(', '));
    $('#fam_list').innerHTML = parts.join('<br>') || 'Chưa liên kết người thân nào.';
    document.querySelectorAll('[data-acc],[data-dec],[data-rm]').forEach(b => b.onclick = async ev => {
      ev.preventDefault();
      const g = b.dataset.acc || b.dataset.dec || b.dataset.rm;
      try { await api(`/api/app/family/respond?guardian=${encodeURIComponent(g)}&accept=${b.dataset.acc ? 'true' : 'false'}`, { method: 'POST' }); }
      catch(e){ toast('Không cập nhật được: ' + e.message); return; }
      toast(b.dataset.acc ? 'Đã đồng ý. Người thân sẽ nhận cảnh báo khi bạn gặp lừa đảo.' : 'Đã cập nhật.'); loadFamily();
    });
  } catch(e){ $('#fam_list').innerHTML = `<span class="err-msg" role="alert" style="display:block">Không tải được danh sách người thân: ${esc(e.message)}</span>`; }
}
$('#fam_inv').onclick = async () => {
  const n = $('#fam_num').value.trim(); if (!n) return;
  try { const r = await api('/api/app/family/invite?number=' + encodeURIComponent(n), { method: 'POST' }); toast(r.note || 'Đã liên kết.'); $('#fam_num').value = ''; loadFamily(); }
  catch(e){ toast('Không gửi được lời mời: ' + e.message); }
};
setInterval(() => { if (document.visibilityState !== 'hidden') loadFamily(); }, 4000);

// ------------------------------------------------------------------ khởi động
function start(){ $('#me').innerHTML = `Số của bạn<br><b>${esc(pretty(MSISDN))}</b>`; $('#nav').style.display = ''; show('home'); refresh(); loadFamily();
  // Nút mô phỏng chỉ có nghĩa ở bản demo; ở chế độ thật (OTP) server trả 403 nên ẩn hẳn.
  cfg().then(c => document.querySelectorAll('.demo-only').forEach(el => { el.style.display = c.phone_verification === 'demo_pool' ? '' : 'none'; })); }
// Không hỏi server khi tab bị ẩn (đỡ pin); quay lại tab thì cập nhật ngay.
const visible = () => document.visibilityState !== 'hidden';
setInterval(() => { if (visible()) refresh(); }, 2000);
document.addEventListener('visibilitychange', () => { if (visible() && MSISDN) { refresh(); loadFamily(); } });
initGoogle();
if (TOKEN) afterLogin(); else show('login');
if (TOKEN && 'Notification' in window && Notification.permission === 'granted') pushSubscribe();   // gia hạn đăng ký
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
