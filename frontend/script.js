/* ==========================================================================
   Bụi Records — Hanoi
   Discogs-backed application script

   Backend contract:
   - Discogs is the product/catalogue source
   - SQLite stores orders, preorders, shop prices and customer accounts
   - The signed session cookie stores the cart and the logged-in user id
   - Reviews/newsletter are informational, non-persistent endpoints
   ========================================================================== */

(function () {
  'use strict';

  class AppBase {
    constructor(props) { this.props = props || {}; }
    setState(update) {
      const patch = typeof update === 'function' ? update(this.state) : update;
      this.state = Object.assign({}, this.state, patch);
      render();
    }
  }

  const API = '/api';
  const VND = n => Number.isFinite(Number(n))
    ? new Intl.NumberFormat('vi-VN').format(Number(n)) + '₫'
    : 'Liên hệ';

  const THEMES = {
    dark: {
      bg: '#121214', surface: '#1E1E22', surfaceAlt: '#18181B', text: '#F4F4F5', muted: '#A1A1AA',
      border: '#27272A', borderStrong: '#3F3F46', headerBg: 'rgba(18,18,20,.86)',
      stripeA: 'rgba(244,244,245,.07)', stripeB: 'rgba(244,244,245,.02)',
      gradient: 'radial-gradient(75% 55% at 8% 2%, rgba(255,85,0,.5) 0%, rgba(255,85,0,.26) 26%, rgba(255,85,0,.08) 52%, rgba(255,85,0,0) 76%), radial-gradient(60% 42% at 88% 6%, rgba(255,85,0,.24) 0%, rgba(255,85,0,.07) 40%, rgba(255,85,0,0) 72%), linear-gradient(180deg, rgba(58,28,10,.55) 0%, rgba(24,18,18,.2) 34%, rgba(18,18,20,0) 56%), radial-gradient(130% 85% at 14% 0%, #2A2626 0%, #19191D 48%, #121214 100%)'
    },
    light: {
      bg: '#F7F4EF', surface: '#FFFFFF', surfaceAlt: '#F0ECE4', text: '#1C1A18', muted: '#6B6560',
      border: '#E4DFD6', borderStrong: '#D2CBBE', headerBg: 'rgba(255,255,255,.86)',
      stripeA: 'rgba(28,26,24,.05)', stripeB: 'rgba(28,26,24,.015)',
      gradient: 'radial-gradient(70% 50% at 8% 0%, rgba(255,85,0,.14) 0%, rgba(255,85,0,.05) 38%, rgba(255,85,0,0) 68%), radial-gradient(55% 38% at 90% 8%, rgba(255,85,0,.08) 0%, rgba(255,85,0,0) 65%), linear-gradient(180deg, #FBF3EC 0%, #F7F4EF 45%, #F7F4EF 100%)'
    }
  };

  // Discogs genres are not a fixed local table. These labels map to the
  // search parameters supported by /api/records in the refactored backend.
  const GENRE_FILTERS = [
    { label: 'Tất cả', params: {} },
    { label: 'Việt Nam', params: { country: 'Vietnam' } },
    { label: 'Jazz', params: { genre: 'Jazz' } },
    { label: 'Dân gian', params: { genre: 'Folk' } },
    { label: 'Soul / Funk', params: { genre: 'Funk / Soul' } },
    { label: 'Điện tử', params: { genre: 'Electronic' } },
    { label: 'Rock', params: { genre: 'Rock' } },
    { label: 'Cổ điển', params: { genre: 'Classical' } }
  ];

  const GENRE_COLORS = {
    Jazz: '#FF5500', 'Funk / Soul': '#F59E0B', Electronic: '#8B5CF6',
    Rock: '#EF4444', Classical: '#10B981', Folk: '#D97706'
  };

  let RECORDS = [];
  let MAX_QTY = 20;     // per cart line; replaced by max_qty from /api/cart at start-up
  const LOW_STOCK = 2;  // "Còn 2 bản" and below is highlighted
  let CATALOG_RECORDS = [];
  let PREORDERS = [];
  let REVIEWS = [];

  async function apiFetch(path, options) {
    const res = await fetch(API + path, Object.assign({ credentials: 'same-origin' }, options));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || ('Yêu cầu thất bại: ' + path));
      err.data = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function qs(params) {
    const out = new URLSearchParams();
    Object.keys(params || {}).forEach(k => {
      const v = params[k];
      if (v !== undefined && v !== null && String(v).trim() !== '') out.set(k, v);
    });
    return out.toString();
  }

  function genreAccent(r) {
    return GENRE_COLORS[r.genre] || '#FF5500';
  }

  function trackRows(r) {
    const list = r.tracks || r.tracklist || [];
    return list.map((t, i) => ({
      no: t.position || t.no || String(i + 1),
      name: t.title || t.name || 'Bài chưa đặt tên'
    }));
  }

  function normalizeRecord(r) {
    const tracks = trackRows(r);
    const hasPrice = r.price !== undefined && r.price !== null;
    const priceValue = hasPrice ? Number(r.price) : null;
    const priceCurrency = r.price_currency || 'VND';
    // Sold out = flagged by the shop, or a tracked stock that reached 0.
    const soldOut = r.stock_status === 'out_of_stock' || (r.stock != null && Number(r.stock) <= 0);
    const stockNum = r.stock != null ? Number(r.stock) : null;
    return Object.assign({}, r, {
      title: r.title || 'Chưa có tên',
      artist: r.artist || 'Chưa rõ nghệ sĩ',
      genre: r.genre || 'Other',
      format: r.format || 'Vinyl',
      tracks,
      hasPrice,
      soldOut,
      priceValue,
      priceCurrency,
      // Shop price in VND; a release without one shows "Liên hệ".
      priceLabel: hasPrice ? VND(priceValue) : 'Liên hệ',
      cover: r.cover_image || r.thumb || '',
      badge: soldOut ? 'Hết hàng' : (hasPrice ? 'Còn hàng' : 'Liên hệ'),
      gradeLabel: r.grade || 'Có tại Bụi',
      // In stock: say how many copies are left; untracked stock just says "Còn hàng".
      stockLabel: soldOut ? 'Hết hàng' : stockNum != null ? 'Còn ' + stockNum + ' bản' : (hasPrice ? 'Còn hàng' : 'Liên hệ cửa hàng'),
      lowStock: !soldOut && stockNum != null && stockNum <= LOW_STOCK
    });
  }

  // Most copies one cart line may hold: the stock when it is tracked, never above MAX_QTY.
  function qtyCap(stock) {
    return Math.min(MAX_QTY, stock != null ? Math.max(0, Number(stock)) : MAX_QTY);
  }

  function limitMessage(stock, inCart) {
    const why = (stock != null && Number(stock) < MAX_QTY) ? 'Chỉ còn ' + stock + ' bản' : 'Mỗi đĩa chỉ đặt tối đa ' + MAX_QTY + ' bản';
    return inCart > 0 ? why + ' — bạn đã có ' + inCart + ' bản trong giỏ.' : why + '.';
  }

  function normalizeCartItems(items) {
    return (items || []).map(i => ({
      id: i.item_id,
      title: i.title,
      artist: i.artist || '',
      price: Number(i.unit_price),
      qty: i.qty,
      tint: genreAccent({ genre: '' }),
      problem: i.problem || '',
      problemCode: i.problem_code || '',
      available: i.available != null ? Number(i.available) : null
    }));
  }

  async function fetchRecords(params) {
    const query = qs(Object.assign({ per_page: 24, page: 1 }, params || {}));
    const data = await apiFetch('/records?' + query);
    return {
      records: (data.records || []).map(normalizeRecord),
      total: data.total || 0,
      pagination: data.pagination || {}
    };
  }

  async function fetchRelease(id) {
    return normalizeRecord(await apiFetch('/records/' + encodeURIComponent(id)));
  }

  async function refreshPreorders() {
    PREORDERS = await apiFetch('/preorders');
  }

  // ---- Ghé thăm (store visit) ----
  // index = JS getDay(): 0 = Chủ nhật ... 6 = Thứ Bảy. open/close in hours (24h), null = closed.
  const STORE_HOURS = [
    { day: 'Chủ nhật', open: 11, close: 23 }, { day: 'Thứ Hai', open: null, close: null, note: 'Nghỉ — lục kho đĩa' },
    { day: 'Thứ Ba', open: 11, close: 21 }, { day: 'Thứ Tư', open: 11, close: 21 }, { day: 'Thứ Năm', open: 11, close: 21 },
    { day: 'Thứ Sáu', open: 11, close: 23 }, { day: 'Thứ Bảy', open: 11, close: 23 }
  ];
  const STORE_ORDER = [1, 2, 3, 4, 5, 6, 0];
  const STORE_MAP_URL = 'https://share.google/BEkSs8a5RwhulLgSA';
  const STORE_MAP_EMBED = 'https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3723.739985286313!2d105.84272297484932!3d21.04308738060981!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x3135abb0a227bdb5%3A0x9487b54407af71de!2zMTQgUC4gSMOgbmcgQsO6biwgQmEgxJDDrG5oLCBIw6AgTuG7mWksIFZp4buHdCBOYW0!5e0!3m2!1svi!2s!4v1790769737485!5m2!1svi!2s';
  const VISIT_ICONS = {
    listen: '<path d="M8 36v-6a24 24 0 0 1 48 0v6" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><rect x="6" y="34" width="10" height="16" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><rect x="48" y="34" width="10" height="16" rx="4" fill="none" stroke="currentColor" stroke-width="3"/>',
    coffee: '<path d="M12 24h30v14a12 12 0 0 1-12 12h-6a12 12 0 0 1-12-12z" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/><path d="M42 28h5a6 6 0 0 1 0 12h-5" fill="none" stroke="currentColor" stroke-width="3"/><path d="M20 10v8M28 10v8M36 10v8" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>',
    crate: '<rect x="8" y="22" width="48" height="30" rx="3" fill="none" stroke="currentColor" stroke-width="3"/><path d="M14 22 20 10h8l-4 12M30 22l4-12h8l-4 12M44 22l4-12h8l-2 12" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>',
    pin: '<path d="M32 58S14 40 14 26a18 18 0 0 1 36 0c0 14-18 32-18 32z" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/><circle cx="32" cy="26" r="6" fill="none" stroke="currentColor" stroke-width="3"/>'
  };

  function storeStatus() {
    // Vietnam time regardless of the visitor's timezone
    const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Ho_Chi_Minh', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(new Date());
    const get = t => (parts.find(p => p.type === t) || {}).value;
    const dayMap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    const today = dayMap[get('weekday')];
    const now = Number(get('hour')) + Number(get('minute')) / 60;
    const h = STORE_HOURS[today];
    let open = false, label = '';
    if (h.open == null) {
      label = 'Hôm nay Bụi nghỉ — mở lại ' + STORE_HOURS[(today + 1) % 7].day + ' lúc 11:00';
    } else if (now < h.open) {
      label = 'Chưa mở cửa · mở lúc ' + h.open + ':00';
    } else if (now < h.close) {
      open = true; label = 'Đang mở cửa · đóng lúc ' + h.close + ':00';
    } else {
      const next = STORE_HOURS[(today + 1) % 7];
      label = 'Đã đóng cửa · ' + (next.open == null ? 'mai nghỉ' : 'mai mở lúc ' + next.open + ':00');
    }
    return { today, open, label };
  }

  // ---- Phụ kiện (mock-up data: not connected to the backend yet) ----
  const GEAR_CATS = ['Tất cả', 'Kim đĩa', 'Vệ sinh', 'Bảo quản', 'Thiết bị'];
  const GEAR_TINTS = { 'Kim đĩa': '#FF5500', 'Vệ sinh': '#38BDF8', 'Bảo quản': '#F59E0B', 'Thiết bị': '#8B5CF6' };
  const GEAR_ICONS = {
    'Kim đĩa': '<circle cx="32" cy="32" r="22" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="32" cy="32" r="4" fill="currentColor"/><path d="M32 32 L52 12" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><path d="M52 12 l-7 1 M52 12 l-1 7" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>',
    'Vệ sinh': '<rect x="10" y="30" width="44" height="12" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><path d="M16 42v10M24 42v10M32 42v10M40 42v10M48 42v10" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><path d="M22 30V14h20v16" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>',
    'Bảo quản': '<rect x="8" y="16" width="48" height="36" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><path d="M8 26h48" stroke="currentColor" stroke-width="3"/><path d="M26 36h12" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>',
    'Thiết bị': '<rect x="8" y="12" width="48" height="40" rx="5" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="26" cy="32" r="11" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="26" cy="32" r="2.5" fill="currentColor"/><path d="M48 18l-8 14" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>'
  };
  const GEAR = [
    { id: 'g1', cat: 'Kim đĩa', name: 'Audio-Technica AT-VM95E', desc: 'Kim từ trường di động, âm thanh cân bằng cho người mới chơi.', price: 1450000, tag: 'Bán chạy' },
    { id: 'g2', cat: 'Kim đĩa', name: 'Ortofon 2M Red', desc: 'Đầu kim MM kinh điển, chi tiết và ấm.', price: 2900000 },
    { id: 'g3', cat: 'Kim đĩa', name: 'Kim thay thế Rega Carbon', desc: 'Kim thay thế chính hãng cho dòng Rega Carbon.', price: 890000 },
    { id: 'g4', cat: 'Vệ sinh', name: 'Chổi sợi carbon chống tĩnh điện', desc: 'Quét bụi khô trước và sau mỗi lần nghe.', price: 190000, tag: 'Bán chạy' },
    { id: 'g5', cat: 'Vệ sinh', name: 'Nước vệ sinh đĩa than 250ml', desc: 'Không cồn, an toàn cho PVC, dùng với khăn microfiber.', price: 280000 },
    { id: 'g6', cat: 'Vệ sinh', name: 'Khăn microfiber (bộ 3)', desc: 'Lau đĩa và bìa đĩa không để lại xơ.', price: 90000 },
    { id: 'g7', cat: 'Bảo quản', name: 'Bao trong chống tĩnh điện (50 cái)', desc: 'Bao lót polyethylene mềm, bảo vệ rãnh đĩa.', price: 150000 },
    { id: 'g8', cat: 'Bảo quản', name: 'Bao ngoài PP trong suốt (50 cái)', desc: 'Giữ bìa đĩa sạch, phẳng, không ố vàng.', price: 120000, tag: 'Mới' },
    { id: 'g9', cat: 'Bảo quản', name: 'Hộp gỗ đựng 30 đĩa 12"', desc: 'Gỗ thông tự nhiên, xếp đĩa thẳng đứng.', price: 890000 },
    { id: 'g10', cat: 'Thiết bị', name: 'Audio-Technica AT-LP60X', desc: 'Đầu đĩa tự động, có sẵn phono, chỉ cần cắm loa.', price: 4990000, tag: 'Bán chạy' },
    { id: 'g11', cat: 'Thiết bị', name: 'Loa Bluetooth có phono', desc: 'Nghe đĩa than không cần ampli, kết nối không dây.', price: 3600000, tag: 'Mới' },
    { id: 'g12', cat: 'Thiết bị', name: 'Cân lực kim kỹ thuật số', desc: 'Chỉnh lực kim chính xác đến 0,01g.', price: 450000 }
  ];

  // Marquee: the strip animates by -50%, so it holds two identical halves; each half
  // repeats the items enough times to be wider than any screen (no gap, seamless loop).
  const TICKER_ITEMS = ['Thông tin từ Discogs', 'Giá bán bằng VND', 'Giao hàng toàn quốc', 'Nhận đặt trước'];
  const TICKER_HTML = Array(4).fill(TICKER_ITEMS.map(t => '<span class="ticker-item">' + t + '</span><span class="ticker-star">✳</span>').join('')).join('');

  // ---- Giữ chỗ (preorder reservations) ----
  const RSV_STEPS = ['Đã giữ chỗ', 'Đã xác nhận', 'Đĩa đã về'];
  const RSV_INDEX = { pending: 0, confirmed: 1, arrived: 2 };
  const RSV_LABELS = { pending: 'Chờ xác nhận', confirmed: 'Đã xác nhận', arrived: 'Đĩa đã về', cancelled: 'Đã hủy' };
  function safeUrl(u) {
    const t = String(u || '');
    if (!/^https?:\/\//i.test(t)) return '';
    return encodeURI(t).replace(/['"()\\]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  }
  const GENRE_VI = { 'Electronic': 'Điện tử', 'Classical': 'Cổ điển', 'Folk, World, & Country': 'Dân gian & thế giới', 'Stage & Screen': 'Sân khấu & điện ảnh', 'Non-Music': 'Khác', 'Hip Hop': 'Hip Hop', 'Funk / Soul': 'Funk / Soul' };
  const viGenre = g => (g ? (GENRE_VI[g] || g) : '');
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function rsvTrack(status) {
    if (status === 'cancelled') return '<div class="rsv-cancelled">Giữ chỗ này đã được hủy. Suất đã được trả lại.</div>';
    const at = RSV_INDEX[status] != null ? RSV_INDEX[status] : 0;
    return '<ol class="rsv-track">' + RSV_STEPS.map((t, i) =>
      '<li class="' + (i < at ? 'is-done' : i === at ? 'is-now' : '') + '"><i>' + (i < at ? '✓' : i + 1) + '</i><span>' + t + '</span></li>').join('') + '</ol>';
  }

  let MYRES = [];
  try { MYRES = JSON.parse(localStorage.getItem('bui_reservations') || '[]'); } catch (e) { MYRES = []; }
  function saveMyRes() { try { localStorage.setItem('bui_reservations', JSON.stringify(MYRES.map(r => ({ code: r.code, phone: r.phone, title: r.title, artist: r.artist, status: r.status })))); } catch (e) { /* storage unavailable */ } }
  function rememberRes(res, phone) {
    MYRES = MYRES.filter(r => r.code !== res.code);
    MYRES.unshift({ code: res.code, phone, title: res.title, artist: res.artist, status: res.status });
    saveMyRes();
  }
  async function refreshMyRes() {
    await Promise.all(MYRES.map(async r => {
      try {
        const d = await apiFetch('/reservations/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: r.code, phone: r.phone }) });
        r.status = d.status; r.title = d.title; r.artist = d.artist;
      } catch (e) { /* keep last known status */ }
    }));
    saveMyRes();
  }

  // ---- Tìm kiếm & tài khoản (search + customer accounts) ----
  const SEARCH_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l5 5"/></svg>';
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/;
  const MIN_PASSWORD = 8;
  const ORDER_BADGE = { pending: 's-pending', confirmed: 's-confirmed', shipping: 's-confirmed', completed: 's-arrived', failed: 's-cancelled', lost: 's-cancelled', returned: 's-cancelled', cancelled: 's-cancelled' };
  const DATE_FMT = new Intl.DateTimeFormat('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
  const blankAuth = () => ({ mode: 'login', name: '', email: '', phone: '', password: '', notice: '', error: '', busy: false });
  const blankAccount = () => ({ orders: [], reasons: null, loading: false, ordersError: '', name: '', phone: '', error: '', note: '', busy: false });
  const blankCheckout = () => ({ name: '', phone: '', address: '', city: '', note: '', shipping: 'standard', payment: 'cod' });
  // fetch() rejects with an English "Failed to fetch" when the server is unreachable; errors that came
  // from the API carry a status and a Vietnamese message.
  const errText = (err, fallback) => (err && err.status) ? (err.message || fallback) : 'Không kết nối được máy chủ, vui lòng thử lại.';
  // Vietnamese names put the given name last: "Nguyễn Văn An" -> "An"
  const givenName = name => String(name || '').trim().split(/\s+/).pop() || '';
  // SQLite CURRENT_TIMESTAMP is UTC ("YYYY-MM-DD HH:MM:SS")
  function fmtDate(s) {
    const d = new Date(String(s || '').replace(' ', 'T') + 'Z');
    return isNaN(d) ? '' : DATE_FMT.format(d);
  }
  // Fill only the blank checkout fields from the account, never overwrite what was typed.
  function fillCheckout(c, user) {
    if (!user) return c;
    const ship = user.last_shipping || {};
    return Object.assign({}, c, {
      name: c.name || user.name || '', phone: c.phone || user.phone || '',
      address: c.address || ship.address || '', city: c.city || ship.city || ''
    });
  }
  function authInput(D, o) {
    return '<input data-field="auth' + o.prop + '" value="' + esc(D.auth[o.prop]) + '" oninput="__h(\'' + reg(D.authSet[o.prop]) + '\',event)" onkeydown="__h(\'' + reg(D.authKey) + '\',event)" type="' + (o.type || 'text') + '" placeholder="' + o.label + '" aria-label="' + o.label + '" autocomplete="' + o.autocomplete + '"' + (o.mode ? ' inputmode="' + o.mode + '"' : '') + ' maxlength="' + (o.max || 254) + '" class="field">';
  }

  class Component extends AppBase {
    state = {
      hov: null, flip: null,
      cartItems: [], cartOpen: false,
      view: 'home', homeGenre: 'Tất cả', catalogGenre: 'Tất cả', themeMode: (function () { try { const t = localStorage.getItem('bui_theme'); if (t === 'light' || t === 'dark') return t; } catch (e) { /* no storage */ } return (window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark'; })(),
      catalogPage: 1, gearCat: 'Tất cả', rsv: null, lookup: { code: '', phone: '', result: null, error: '', busy: false }, catalogTotal: 0,
      loadingCatalog: false, catalogQuery: '', catalogSearched: '', catalogBusy: false,
      user: null, authOpen: false, auth: blankAuth(), account: blankAccount(),
      checkout: { name: '', phone: '', address: '', city: '', note: '', shipping: 'standard', payment: 'cod' },
      checkoutError: '', placingOrder: false, orderReq: null, lastOrder: null, detail: null, detailQty: 1, cardQty: {}, toast: null, email: '', subscribed: false
    };

    say(msg) {
      clearTimeout(this._t);
      this.setState({ toast: msg });
      this._t = setTimeout(() => this.setState({ toast: null }), 2600);
    }

    async addToBag(item, qty = 1) {
      if (item.soldOut) {
        this.say('Đĩa này đã hết hàng.');
        return;
      }
      if (!item.hasPrice) {
        this.say('Đĩa này chưa có giá, vui lòng liên hệ cửa hàng.');
        return;
      }
      try {
        const cart = await apiFetch('/cart/items', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // No client-supplied price: the backend looks up the real,
          // shop price itself when adding.
          body: JSON.stringify({ item_type: 'record', item_id: String(item.id), qty })
        });
        this.setState({ cartItems: normalizeCartItems(cart.items) });
        this.say((qty > 1 ? 'Đã thêm ' + qty + ' bản vào giỏ — ' : 'Đã thêm vào giỏ — ') + item.title);
      } catch (err) {
        // The disc sold out after this page loaded: reflect it on screen too.
        if (err.data && err.data.code === 'out_of_stock') this.markSoldOut(item.id);
        // The server's message is already specific (sold out / only N left / no price / max qty).
        this.say(err.message || 'Không thêm được vào giỏ hàng.');
      }
    }

    // The last copy went while this page was open: update every place the disc is shown
    // (home shelf, catalogue, an open detail dialog) instead of waiting for a reload.
    markSoldOut(id) {
      const apply = r => {
        if (String(r.id) !== String(id)) return;
        Object.assign(r, { soldOut: true, lowStock: false, stock: 0, stock_status: 'out_of_stock', badge: 'Hết hàng', stockLabel: 'Hết hàng' });
      };
      RECORDS.forEach(apply);
      CATALOG_RECORDS.forEach(apply);
      const d = this.state.detail;
      this.setState(d && String(d.id) === String(id)
        ? { detail: Object.assign({}, d, { stock: 0, stock_status: 'out_of_stock' }) }
        : {});
    }

    async openDetail(record) {
      const seq = this._detailSeq = (this._detailSeq || 0) + 1;
      this._detailQtyRaw = null;
      this.setState({ detail: Object.assign({}, record, { loading: true }), detailQty: 1 });
      try {
        const detail = await fetchRelease(record.id);
        if (seq !== this._detailSeq || !this.state.detail) return;
        this.setState({ detail });
      } catch (err) {
        if (seq !== this._detailSeq || !this.state.detail) return;
        this.setState({ detail: record });
        this.say(err.message || 'Không tải được thông tin đĩa.');
      }
    }

    buildCard(r, hov, flip, preview) {
      const h = hov === r.id;
      const f = flip === r.id;
      const tint = genreAccent(r);
      // Quantity picker on the card: what is already in the cart counts against the stock.
      const inCart = (this.state.cartItems || []).filter(c => String(c.id) === String(r.id)).reduce((a, c) => a + c.qty, 0);
      const qtyMax = r.soldOut ? 0 : Math.max(0, qtyCap(r.stock) - inCart);
      const qty = Math.min(Math.max(1, (this.state.cardQty || {})[r.id] || 1), Math.max(1, qtyMax));
      const limit = limitMessage(r.stock, inCart);
      const raws = this._cardQtyRaw || (this._cardQtyRaw = {});
      const setCardQty = n => this.setState(s => ({ cardQty: Object.assign({}, s.cardQty, { [r.id]: n }) }));
      const clampQty = (raw, el) => {
        let n = parseInt(raw, 10);
        if (!Number.isFinite(n) || n < 1) n = 1;
        if (n > qtyMax) { this.say(limit); n = Math.max(1, qtyMax); }
        if (el) el.value = String(n); // the box keeps focus, so a re-render would not update it
        return n;
      };
      return Object.assign({}, r, {
        qty, qtyMax, inCart,
        qtyDec: e => { e.stopPropagation(); delete raws[r.id]; setCardQty(Math.max(1, qty - 1)); },
        qtyInc: e => { e.stopPropagation(); delete raws[r.id]; if (qty >= qtyMax) { this.say(limit); return; } setCardQty(qty + 1); },
        qtyInput: e => { raws[r.id] = e.target.value; },
        qtyCommit: e => { const n = clampQty(e.target.value, e.target); delete raws[r.id]; setCardQty(n); },
        qtyKey: e => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } },
        stopClick: e => e.stopPropagation(),
        tint,
        price: r.priceLabel,
        lift: h ? 'translateY(-14px) scale(1.02)' : 'translateY(0) scale(1)',
        discT: h && !f ? 'translate3d(72%,0,0) scale(1.06)' : 'translate3d(38%,0,0) scale(1)',
        spinDur: h ? '2.2s' : '7s',
        flipT: f ? 'rotateY(180deg)' : 'rotateY(0deg)',
        shadow: h ? '0 0 0 1px #FF5500, 0 0 38px -4px rgba(255,85,0,.5), 0 34px 60px -22px rgba(0,0,0,.95)' : '0 0 0 1px #27272A, 0 10px 24px -20px rgba(0,0,0,.7)',
        discGlow: h ? '0 0 0 2px rgba(255,85,0,.9), 0 0 30px 3px rgba(255,85,0,.75), 0 0 88px 8px rgba(255,85,0,.5), 0 24px 46px -18px rgba(0,0,0,.95)' : '0 0 0 1.5px rgba(255,85,0,.55), 0 0 30px -2px rgba(255,85,0,.4), 0 18px 34px -20px rgba(0,0,0,.9)',
        uiOp: h ? 1 : 0, prevOp: h && preview && !f ? 1 : 0, uiY: h ? '0px' : '10px', addH: h ? '60px' : '0px',
        previewLabel: (viGenre(r.genre) || 'Đĩa than') + ' · ' + String(r.format || 'Vinyl').split(',').slice(0, 2).join(',').trim(),
        enter: () => this.setState({ hov: r.id }),
        // Keep the buy row open while the customer is typing a quantity in it.
        leave: e => { if (e && e.type === 'mouseleave' && e.currentTarget && e.currentTarget.contains(document.activeElement)) return; this.setState({ hov: null }); },
        tap: e => { e.stopPropagation(); this.setState({ hov: r.id }); },
        keyOpen: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.openDetail(r); } },
        toggleFlip: e => { e.stopPropagation(); this.setState(s => ({ flip: s.flip === r.id ? null : r.id })); },
        open: e => { e.preventDefault(); this.openDetail(r); },
        add: e => {
          e.stopPropagation();
          if (r.soldOut || !r.hasPrice) { this.addToBag(r); return; } // addToBag explains why not
          if (qtyMax < 1) { this.say(limit); return; }
          // A number typed but not yet committed (box never left) still counts.
          const n = raws[r.id] != null ? clampQty(raws[r.id]) : qty;
          delete raws[r.id];
          this.addToBag(r, n).then(() => setCardQty(1));
        }
      });
    }

    // One loader for the nav link, the genre chips and the search box.
    // soft = keep the current cards on screen (dimmed) while new ones load, so live search doesn't flash.
    async loadCatalog(opts) {
      const o = opts || {};
      const seq = this._catSeq = (this._catSeq || 0) + 1;
      const q = this.state.catalogQuery.trim();
      this._catQ = q;
      this.setState({ loadingCatalog: !o.soft, catalogBusy: !!o.soft, catalogPage: 1, flip: null });
      try {
        const filter = GENRE_FILTERS.find(x => x.label === this.state.catalogGenre) || GENRE_FILTERS[0];
        const data = await fetchRecords(Object.assign({}, filter.params, { format: 'Vinyl', per_page: 100, page: 1, search: q }));
        if (seq !== this._catSeq) return;
        CATALOG_RECORDS = data.records;
        this.setState({ catalogTotal: data.total, catalogSearched: q, loadingCatalog: false, catalogBusy: false });
      } catch (err) {
        if (seq !== this._catSeq) return;
        this.setState({ loadingCatalog: false, catalogBusy: false });
        this.say(err.message || o.error || 'Không tải được danh sách sản phẩm.');
      }
    }

    async goCatalog(e) {
      if (e && e.preventDefault) e.preventDefault();
      this.setState({ view: 'catalog', cartOpen: false, loadingCatalog: true });
      await this.loadCatalog({ error: 'Không tải được danh sách sản phẩm.' });
      window.scrollTo({ top: 0 });
    }

    // The search box is an uncontrolled input (like the other text fields): typing never re-renders it,
    // so the caret and the Vietnamese IME composition are left alone.
    onSearchInput(value, composing) {
      this.state.catalogQuery = value;
      clearTimeout(this._searchT);
      if (composing) return; // the IME is still building a syllable; compositionend will call us again
      this._searchT = setTimeout(() => this.runSearch(), 300);
    }

    runSearch(force) {
      clearTimeout(this._searchT);
      // Same text as the request already sent/shown: just refresh the clear button (Enter retries anyway).
      if (!force && this.state.catalogQuery.trim() === this._catQ) { this.setState({}); return; }
      return this.loadCatalog({ soft: true, error: 'Không tìm được sản phẩm.' });
    }

    onSearchKey(e) {
      if (e.isComposing) return;
      if (e.key === 'Enter') { e.preventDefault(); this.runSearch(true); }
      else if (e.key === 'Escape' && this.state.catalogQuery) { e.preventDefault(); this.clearSearch(); }
    }

    clearSearch() {
      clearTimeout(this._searchT);
      this.state.catalogQuery = '';
      const el = document.querySelector('[data-field="catalogSearch"]');
      if (el) el.value = ''; // the renderer never overwrites a focused input (Escape is pressed inside it)
      this.runSearch();
      this.focusSearch();
    }

    focusSearch(select) {
      const el = document.querySelector('[data-field="catalogSearch"]');
      if (el) { el.focus(); if (select) el.select(); }
    }

    // Header magnifier: open the catalogue (if needed) and put the cursor in the search box.
    goSearch(e) {
      if (e && e.preventDefault) e.preventDefault();
      if (this.state.view === 'catalog') {
        this.setState({ cartOpen: false });
        window.scrollTo({ top: 0, behavior: 'smooth' });
      } else {
        this.goCatalog(); // renders the catalogue and its search box synchronously, then loads
      }
      this.focusSearch(true);
    }

    navHome(e) {
      if (e && e.preventDefault) e.preventDefault();
      this.setState({ view: 'home', cartOpen: false, detail: null });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    async pickCatalogGenre(label) {
      this.setState({ catalogGenre: label });
      await this.loadCatalog({ error: 'Không lọc được sản phẩm.' });
    }

    openReserve(p) {
      this.setState({ rsv: { pid: p.id, title: p.title, artist: p.artist, price: p.price, name: '', phone: '', error: '', busy: false, result: null } });
    }

    async submitReserve() {
      const r = this.state.rsv;
      if (!r || r.busy) return;
      if (!r.name.trim()) { this.setState({ rsv: { ...r, error: 'Vui lòng nhập họ tên.' } }); return; }
      if (r.phone.replace(/\D/g, '').length < 9) { this.setState({ rsv: { ...r, error: 'Số điện thoại chưa đúng.' } }); return; }
      this.setState({ rsv: { ...r, busy: true, error: '' } });
      try {
        const res = await apiFetch('/preorders/' + r.pid + '/reserve', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: r.name.trim(), phone: r.phone })
        });
        rememberRes(res, r.phone);
        this.setState({ rsv: { ...r, busy: false, result: res } });
        refreshPreorders().then(render);
      } catch (err) {
        this.setState({ rsv: { ...r, busy: false, error: err.message || 'Không giữ được suất đặt trước.' } });
      }
    }

    async doLookup() {
      const l = this.state.lookup;
      if (l.busy) return;
      this.setState({ lookup: { ...l, busy: true, error: '', result: null } });
      try {
        const res = await apiFetch('/reservations/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: l.code.trim().toUpperCase(), phone: l.phone }) });
        rememberRes(res, l.phone);
        this.setState({ lookup: { ...l, busy: false, result: res } });
      } catch (err) {
        this.setState({ lookup: { ...l, busy: false, error: err.message || 'Không tra cứu được.' } });
      }
    }

    async placeOrder() {
      // One request at a time: a double click must not send the same cart twice.
      if (this.state.placingOrder) return;
      const { name, phone, address, city, note, shipping, payment } = this.state.checkout;
      this.setState({ placingOrder: true, checkoutError: '' });
      try {
        const data = await apiFetch('/checkout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, phone, address, city, note, shipping, payment })
        });
        this.setState({
          lastOrder: {
            orderNo: data.order_no,
            items: data.items.map(i => ({ title: i.title, qty: i.qty })),
            subtotal: data.subtotal, shippingFee: data.shipping_fee,
            paymentLabel: data.payment_label, phone: data.phone
          },
          cartItems: [], checkoutError: '', view: 'confirmed'
        });
      } catch (err) {
        if (err.data && err.data.code === 'cart_unavailable') {
          // A disc sold out while the customer was filling in the form: reload the cart and
          // reopen it, where every line says what is wrong (the form is kept as typed).
          try {
            const cart = await apiFetch('/cart');
            this.setState({ cartItems: normalizeCartItems(cart.items), checkoutError: '', view: 'home', cartOpen: true });
            this.say('Một số đĩa trong giỏ đã hết hoặc không đủ hàng — vui lòng kiểm tra lại.');
            return;
          } catch (e) { /* could not reload the cart: fall through to the plain error */ }
        }
        this.setState({ checkoutError: errText(err, 'Đặt hàng chưa thành công, vui lòng thử lại.') });
      } finally {
        this.setState({ placingOrder: false });
      }
    }

    // ---- Accounts ----
    setUser(user) {
      this.setState(s => ({ user, checkout: fillCheckout(s.checkout, user) }));
    }

    openAuth(mode, notice) {
      this.setState({ authOpen: true, cartOpen: false, auth: Object.assign(blankAuth(), { mode: mode || 'login', notice: notice || '' }) });
      setTimeout(() => this.focusAuth(), 30);
    }

    closeAuth() {
      this.setState({ authOpen: false, auth: blankAuth() }); // also drops the typed password from memory
    }

    setAuthMode(mode) {
      this.setState(s => ({ auth: Object.assign({}, s.auth, { mode, error: '', notice: '' }) }));
      setTimeout(() => this.focusAuth(), 0);
    }

    focusAuth() {
      const modal = document.querySelector('.auth-modal');
      if (!modal) return;
      const inputs = Array.prototype.slice.call(modal.querySelectorAll('input'));
      const el = inputs.find(i => !i.value) || inputs[0];
      if (el) el.focus();
    }

    async submitAuth() {
      const a = this.state.auth;
      if (a.busy) return;
      const isLogin = a.mode === 'login';
      const email = a.email.trim().toLowerCase();
      const fail = msg => this.setState(s => ({ auth: Object.assign({}, s.auth, { error: msg }) }));
      if (!isLogin && !a.name.trim()) return fail('Vui lòng nhập họ tên.');
      if (!EMAIL_RE.test(email)) return fail('Vui lòng nhập email hợp lệ.');
      if (!a.password) return fail('Vui lòng nhập mật khẩu.');
      if (!isLogin && a.password.length < MIN_PASSWORD) return fail('Mật khẩu cần ít nhất ' + MIN_PASSWORD + ' ký tự.');
      this.setState(s => ({ auth: Object.assign({}, s.auth, { busy: true, error: '', notice: '' }) }));
      try {
        const body = isLogin
          ? { email, password: a.password }
          : { name: a.name.trim(), email, phone: a.phone.trim(), password: a.password };
        const res = await apiFetch('/auth/' + (isLogin ? 'login' : 'register'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        this.setState({ authOpen: false, auth: blankAuth() });
        this.setUser(res.user);
        this.say((isLogin ? 'Đã đăng nhập — xin chào ' : 'Đã tạo tài khoản — chào mừng ') + givenName(res.user.name));
      } catch (err) {
        this.setState(s => ({ auth: Object.assign({}, s.auth, { busy: false, error: errText(err, 'Không đăng nhập được, vui lòng thử lại.') }) }));
      }
    }

    async logout() {
      try {
        await apiFetch('/auth/logout', { method: 'POST' });
      } catch (err) {
        this.say('Không đăng xuất được, vui lòng thử lại.');
        return;
      }
      // Wipe what was pre-filled from the account (the next person on this device must not see it).
      this.setState(s => ({ user: null, account: blankAccount(), checkout: blankCheckout(), view: s.view === 'account' ? 'home' : s.view }));
      window.scrollTo({ top: 0 });
      this.say('Đã đăng xuất.');
    }

    sessionExpired() {
      this.setState({ user: null, account: blankAccount(), view: 'home' });
      this.openAuth('login', 'Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.');
    }

    async goAccount(e) {
      if (e && e.preventDefault) e.preventDefault();
      const u = this.state.user;
      if (!u) { this.openAuth('login'); return; }
      this.setState({ view: 'account', cartOpen: false, detail: null, account: Object.assign(blankAccount(), { loading: true, name: u.name, phone: u.phone || '' }) });
      window.scrollTo({ top: 0 });
      try {
        const data = await apiFetch('/orders');
        this.setState(s => ({ account: Object.assign({}, s.account, { orders: data.orders || [], reasons: data.reasons || null, loading: false }) }));
      } catch (err) {
        if (err.status === 401) { this.sessionExpired(); return; }
        this.setState(s => ({ account: Object.assign({}, s.account, { loading: false, ordersError: errText(err, 'Không tải được đơn hàng.') }) }));
      }
    }

    // ---- cancel / report an order (Tài khoản) ----
    openOrderRequest(order, kind) {
      const R = this.state.account.reasons;
      if (!R) return;
      const reasons = kind === 'cancel' ? R.cancel : R.report.filter(r => r.statuses.indexOf(order.status) >= 0);
      this.setState({ orderReq: { order_no: order.order_no, status: order.status, kind, mode: order.cancel_mode, reasons, reason: '', detail: '', busy: false, error: '' } });
    }

    async submitOrderRequest() {
      const q = this.state.orderReq;
      if (!q || q.busy) return;
      const fail = msg => this.setState(s => ({ orderReq: Object.assign({}, s.orderReq, { error: msg, busy: false }) }));
      if (!q.reason) return fail(q.kind === 'cancel' ? 'Vui lòng chọn lý do hủy đơn.' : 'Vui lòng chọn vấn đề bạn gặp.');
      if ((q.reason === 'other' || q.reason === 'missing' || q.reason === 'wrong_item') && !q.detail.trim()) return fail('Vui lòng mô tả thêm để Bụi hỗ trợ nhanh hơn.');
      this.setState({ orderReq: Object.assign({}, q, { busy: true, error: '' }) });
      try {
        const res = await apiFetch('/orders/' + encodeURIComponent(q.order_no) + '/' + (q.kind === 'cancel' ? 'cancel' : 'report'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: q.reason, detail: q.detail.trim() })
        });
        this.setState(s => ({
          orderReq: null,
          account: Object.assign({}, s.account, { orders: s.account.orders.map(o => (o.order_no === q.order_no ? res.order : o)) })
        }));
        this.say(q.kind === 'cancel' && res.order.status === 'cancelled' ? 'Đã hủy đơn ' + q.order_no + '.' : 'Đã gửi yêu cầu ' + res.code + '. Bụi sẽ liên hệ bạn sớm.');
      } catch (err) {
        if (err.status === 401) { this.setState({ orderReq: null }); this.sessionExpired(); return; }
        fail(errText(err, 'Chưa gửi được yêu cầu, vui lòng thử lại.'));
      }
    }

    async saveProfile() {
      const a = this.state.account;
      if (a.busy) return;
      const patch = p => this.setState(s => ({ account: Object.assign({}, s.account, p) }));
      if (!a.name.trim()) { patch({ error: 'Vui lòng nhập họ tên.', note: '' }); return; }
      patch({ busy: true, error: '', note: '' });
      try {
        const res = await apiFetch('/auth/me', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: a.name.trim(), phone: a.phone.trim() })
        });
        this.setState(s => ({ user: res.user, account: Object.assign({}, s.account, { busy: false, name: res.user.name, phone: res.user.phone, note: 'Đã lưu thông tin.' }) }));
      } catch (err) {
        if (err.status === 401) { this.sessionExpired(); return; }
        patch({ busy: false, error: errText(err, 'Không lưu được thông tin.') });
      }
    }

    renderVals() {
      const { hov, flip, detail, view, cartItems, cartOpen, checkout, checkoutError, lastOrder, themeMode, user, account } = this.state;
      const preview = this.props.hoverPreview !== false;
      const accent = this.props.accent || '#FF5500';
      const T = THEMES[themeMode];
      const cards = RECORDS.map(r => this.buildCard(r, hov, flip, preview));
      const catalogCards = CATALOG_RECORDS.map(r => this.buildCard(r, hov, flip, preview));
      const d = detail && this.buildCard(normalizeRecord(detail), hov, flip, preview);

      const cartCount = cartItems.reduce((a, c) => a + c.qty, 0);
      // Lines that can't be bought (sold out / not enough stock) never count towards what is paid.
      const cartSubtotalNum = cartItems.reduce((a, c) => c.problem ? a : a + c.price * c.qty, 0);
      const cartBlocked = cartItems.some(c => c.problem);
      // Quantity picker in the detail dialog: what is already in the cart counts against the stock.
      const detailInCart = d ? cartItems.filter(c => String(c.id) === String(d.id)).reduce((a, c) => a + c.qty, 0) : 0;
      const detailMax = d && !d.soldOut ? Math.max(0, qtyCap(d.stock) - detailInCart) : 0;
      const detailQty = Math.min(Math.max(1, this.state.detailQty || 1), Math.max(1, detailMax));
      const detailLimit = d ? limitMessage(d.stock, detailInCart) : '';
      const clampDetailQty = (raw, el) => {
        let n = parseInt(raw, 10);
        if (!Number.isFinite(n) || n < 1) n = 1;
        if (n > detailMax) { this.say(detailLimit); n = Math.max(1, detailMax); }
        if (el) el.value = String(n);
        return n;
      };
      const setQty = async (id, qty) => {
        try {
          const cart = await apiFetch('/cart/items/' + encodeURIComponent(id), {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ qty })
          });
          this.setState({ cartItems: normalizeCartItems(cart.items) });
        } catch (err) { this.say(err.message || 'Không cập nhật được giỏ hàng.'); }
      };
      const removeItem = async id => {
        try {
          const cart = await apiFetch('/cart/items/' + encodeURIComponent(id), { method: 'DELETE' });
          this.setState({ cartItems: normalizeCartItems(cart.items) });
        } catch (err) { this.say(err.message || 'Không cập nhật được giỏ hàng.'); }
      };
      const cartLines = cartItems.map(c => ({
        ...c, lineTotal: VND(c.price * c.qty), priceEach: VND(c.price),
        maxQty: Math.max(1, qtyCap(c.available)),
        stockNote: !c.problem && c.available != null ? 'còn ' + c.available + ' bản' : '',
        inc: () => {
          // No need to ask the server when we already know the answer.
          if (c.problem || c.qty >= qtyCap(c.available)) { this.say(c.problem || limitMessage(c.available, 0)); return; }
          setQty(c.id, c.qty + 1);
        },
        // Typed quantity: committed on Enter / leaving the box, clamped to what can be bought.
        commitQty: e => {
          let n = parseInt(e.target.value, 10);
          if (!Number.isFinite(n) || n < 1) { e.target.value = String(c.qty); this.say('Số lượng phải là số từ 1 trở lên.'); return; }
          const cap = qtyCap(c.available);
          if (cap >= 1 && n > cap) { this.say(limitMessage(c.available, 0)); n = cap; }
          e.target.value = String(n); // the box keeps focus, so a re-render would not update it
          if (n !== c.qty) setQty(c.id, n);
        },
        qtyKey: e => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } },
        dec: () => setQty(c.id, Math.max(1, c.qty - 1)), remove: () => removeItem(c.id),
        showFix: c.problemCode === 'insufficient_stock' && c.available > 0,
        fixQty: () => setQty(c.id, c.available)
      }));
      const shippingFee = checkout.shipping === 'express' ? 45000 : (cartSubtotalNum >= 1500000 ? 0 : 35000);
      const checkoutTotalNum = cartSubtotalNum + shippingFee;
      const paymentOptions = [
        { id: 'cod', label: 'Thanh toán khi nhận hàng' }, { id: 'bank', label: 'Chuyển khoản' },
        { id: 'momo', label: 'Momo' }, { id: 'zalopay', label: 'ZaloPay' }, { id: 'card', label: 'Thẻ tín dụng / ghi nợ' }
      ].map(p => ({
        ...p, bg: checkout.payment === p.id ? accent : T.surface, fg: checkout.payment === p.id ? '#121214' : T.text,
        bd: checkout.payment === p.id ? accent : T.border,
        pick: () => this.setState(s => ({ checkout: { ...s.checkout, payment: p.id } }))
      }));

      return {
        theme: T, toggleTheme: () => this.setState(s => { const m = s.themeMode === 'dark' ? 'light' : 'dark'; try { localStorage.setItem('bui_theme', m); } catch (e) { /* no storage */ } return { themeMode: m }; }),
        themeName: themeMode,
        themeToggleLabel: themeMode === 'dark' ? 'Chế độ sáng' : 'Chế độ tối',
        themeIcon: themeMode === 'dark'
          ? '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>'
          : '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z"/></svg>',
        cartCount, cartSubtotal: VND(cartSubtotalNum), toast: this.state.toast,
        // header + account
        user, userGiven: user ? givenName(user.name) : '', userInitial: user ? givenName(user.name).charAt(0).toUpperCase() : '',
        goSearch: e => this.goSearch(e), openLogin: () => this.openAuth('login'), goAccount: e => this.goAccount(e),
        logout: () => this.logout(), saveProfile: () => this.saveProfile(),
        account, accSetName: e => { this.state.account.name = e.target.value; }, accSetPhone: e => { this.state.account.phone = e.target.value; },
        accountOrders: (account.orders || []).map(o => ({
          ...o, dateLabel: fmtDate(o.created_at), badge: ORDER_BADGE[o.status] || 's-pending', totalLabel: VND(o.total),
          lines: (o.items || []).map(i => ({ title: i.title, qty: i.qty, total: VND(i.line_total) })),
          refundedLabel: o.refunded ? VND(o.refunded) : '',
          trackingLabel: o.tracking_no ? (o.carrier ? o.carrier + ', ' : '') + 'mã vận đơn ' + o.tracking_no : '',
          reportUntilLabel: o.report_until ? fmtDate(o.report_until) : '',
          caseList: (o.cases || []).map(c => ({ code: c.code, title: c.title, status: c.status, label: c.status_label, replies: c.replies || [] })),
          cancelLabel: o.cancel_mode === 'instant' ? 'Hủy đơn' : o.cancel_mode === 'request' ? 'Yêu cầu hủy đơn' : '',
          reportLabel: o.can_report ? (o.status === 'shipping' ? 'Báo sự cố giao hàng' : 'Báo sự cố, trả hàng') : '',
          cancel: () => this.openOrderRequest(o, 'cancel'),
          report: () => this.openOrderRequest(o, 'report')
        })),
        orderReq: this.state.orderReq ? (() => {
          const q = this.state.orderReq;
          const info = q.kind === 'cancel'
            ? (q.mode === 'instant' ? 'Đơn chưa được xác nhận nên sẽ được hủy ngay.'
              : q.status === 'shipping' ? 'Đơn đã giao cho đơn vị vận chuyển. Bụi sẽ yêu cầu dừng giao và chuyển hoàn; đơn được hủy khi hàng về kho.'
              : 'Đơn đã được xác nhận và có thể đang được đóng gói. Bụi sẽ kiểm tra và phản hồi bạn sớm.')
            : 'Nếu có, hãy giữ lại ảnh hoặc video mở hộp và vỏ gói hàng. Bụi sẽ liên hệ qua số điện thoại của đơn trong 24 giờ.';
          return Object.assign({}, q, {
            title: q.kind === 'cancel' ? (q.mode === 'instant' ? 'Hủy đơn' : 'Yêu cầu hủy đơn') : (q.status === 'shipping' ? 'Báo sự cố giao hàng' : 'Báo sự cố, trả hàng'),
            info,
            options: q.reasons.map(r => ({ key: r.key, label: r.label, on: q.reason === r.key,
              pick: () => this.setState(s => ({ orderReq: Object.assign({}, s.orderReq, { reason: r.key, error: '' }) })) }))
          });
        })() : null,
        reqClose: () => this.setState({ orderReq: null }),
        reqDetail: e => { if (this.state.orderReq) this.state.orderReq.detail = e.target.value; },
        reqSubmit: () => this.submitOrderRequest(),
        // login / register dialog
        authOpen: this.state.authOpen, auth: this.state.auth, authIsLogin: this.state.auth.mode === 'login',
        authClose: () => this.closeAuth(), authSubmit: () => this.submitAuth(),
        authSwitch: () => this.setAuthMode(this.state.auth.mode === 'login' ? 'register' : 'login'),
        authKey: e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); this.submitAuth(); } },
        authSet: {
          name: e => { this.state.auth.name = e.target.value; }, email: e => { this.state.auth.email = e.target.value; },
          phone: e => { this.state.auth.phone = e.target.value; }, password: e => { this.state.auth.password = e.target.value; }
        },
        // catalogue search
        catalogQuery: this.state.catalogQuery, catalogSearched: this.state.catalogSearched, catalogBusy: this.state.catalogBusy, catalogGenre: this.state.catalogGenre,
        searchInput: e => this.onSearchInput(e.target.value, e.isComposing),
        searchKey: e => this.onSearchKey(e), clearSearch: () => this.clearSearch(),
        cards, detail: d, isHome: view === 'home', isCatalog: view === 'catalog',
        isPreordersPage: view === 'preorders', isGearPage: view === 'gear', isVisitPage: view === 'visit',
        isCheckout: view === 'checkout', isConfirmed: view === 'confirmed', isAccountPage: view === 'account',
        goCatalog: e => this.goCatalog(e),
        goPreordersPage: e => { if (e) e.preventDefault(); this.setState({ view: 'preorders', cartOpen: false }); window.scrollTo({ top: 0 }); refreshPreorders().then(refreshMyRes).then(render); },
        goGearPage: e => { if (e) e.preventDefault(); this.setState({ view: 'gear', cartOpen: false }); window.scrollTo({ top: 0 }); },
        goVisitPage: e => { if (e) e.preventDefault(); this.setState({ view: 'visit', cartOpen: false }); window.scrollTo({ top: 0 }); },
        goHome: () => this.navHome(), goHomeNav: e => this.navHome(e),
        goCheckout: () => {
          if (!cartLines.length) { this.say('Giỏ hàng đang trống.'); return; }
          if (cartBlocked) { this.say('Có đĩa đã hết hoặc không đủ hàng — hãy xóa hoặc chỉnh số lượng trước khi thanh toán.'); return; }
          this.setState({ cartOpen: false, view: 'checkout' }); window.scrollTo({ top: 0 });
        },
        backToCart: () => this.setState({ view: 'home', cartOpen: true }),
        heroSlide: hov === 'hero' ? 'translateX(46%) scale(1.05)' : 'translateX(22%) scale(1)',
        heroShadow: hov === 'hero' ? '0 0 0 1px #FF5500, 0 0 70px -4px rgba(255,85,0,.6), 0 34px 64px -26px rgba(0,0,0,.95)' : '0 0 0 1px rgba(255,85,0,.3), 0 26px 50px -28px rgba(0,0,0,.8)',
        heroSpinDur: hov === 'hero' ? '3s' : '9s', heroIn: () => this.setState({ hov: 'hero' }), heroOut: () => this.setState({ hov: null }),
        heroTap: () => this.setState(s => ({ hov: s.hov === 'hero' ? null : 'hero' })),
        shelfBlurb: 'Những chiếc đĩa đang có tại Bụi. Thông tin chi tiết lấy từ Discogs, giá bán tính bằng VND.',
        genres: GENRE_FILTERS.map(g => ({
          name: g.label, bg: this.state.homeGenre === g.label ? accent : T.surface, fg: this.state.homeGenre === g.label ? '#121214' : T.muted, bd: T.border, tf: 'translateY(0)',
          pick: async () => {
            this.setState({ homeGenre: g.label });
            const list = GENRE_FILTERS.find(x => x.label === g.label);
            // Home stays lightweight: fetch a fresh Discogs page for the selected filter.
            try {
              const data = await fetchRecords(Object.assign({}, list.params, { per_page: 8, page: 1 }));
              RECORDS = data.records; this.setState({ flip: null });
            } catch (err) { this.say(err.message || 'Không tải được thể loại này.'); }
          }
        })),
        catalogCount: this.state.catalogTotal, catalogCards, loadingCatalog: this.state.loadingCatalog,
        catalogGenres: GENRE_FILTERS.map(g => ({ name: g.label, bg: this.state.catalogGenre === g.label ? accent : 'transparent', fg: this.state.catalogGenre === g.label ? '#121214' : T.text, pick: () => this.pickCatalogGenre(g.label) })),
        visit: (() => {
          const st = storeStatus();
          return {
            open: st.open, statusLabel: st.label,
            hours: STORE_ORDER.map(i => ({ day: STORE_HOURS[i].day, closed: STORE_HOURS[i].open == null, text: STORE_HOURS[i].open == null ? STORE_HOURS[i].note : STORE_HOURS[i].open + ':00 — ' + STORE_HOURS[i].close + ':00', today: i === st.today })),
            features: [
              { icon: VISIT_ICONS.listen, title: 'Nghe thử trước khi mua', text: 'Bàn nghe với tai nghe và loa, thoải mái quay thử từng mặt đĩa.' },
              { icon: VISIT_ICONS.coffee, title: 'Cà phê pha sẵn', text: 'Một ly cà phê, một chiếc đĩa, một buổi chiều chậm rãi.' },
              { icon: VISIT_ICONS.crate, title: 'Lục kệ đĩa', text: 'Kệ đĩa luôn có thứ mới để lục — hỏi Bụi nếu bạn đang tìm bản đặc biệt.' },
              { icon: VISIT_ICONS.pin, title: 'Nhận đĩa đặt trước', text: 'Đã giữ chỗ? Mang mã giữ chỗ tới quầy để nhận đĩa.' }
            ]
          };
        })(),
        mapUrl: STORE_MAP_URL, mapEmbed: STORE_MAP_EMBED,
        gearCats: GEAR_CATS.map(c => ({ name: c, bg: this.state.gearCat === c ? accent : 'transparent', fg: this.state.gearCat === c ? '#121214' : T.text, pick: () => this.setState({ gearCat: c }) })),
        gearItems: GEAR.filter(g => this.state.gearCat === 'Tất cả' || g.cat === this.state.gearCat).map(g => ({
          ...g, tint: GEAR_TINTS[g.cat], icon: GEAR_ICONS[g.cat], priceLabel: VND(g.price),
          ask: () => this.say('Phụ kiện sắp mở bán online — ghé Bụi ở 14 Hàng Bún để mua trực tiếp nhé.')
        })),
        gearCount: GEAR.filter(g => this.state.gearCat === 'Tất cả' || g.cat === this.state.gearCat).length,
        rsv: this.state.rsv ? (() => { const r = this.state.rsv; return {
          ...r, priceLabel: r.price != null ? VND(r.price) : 'Liên hệ',
          statusLabel: r.result ? RSV_LABELS[r.result.status] : '', track: r.result ? rsvTrack(r.result.status) : ''
        }; })() : null,
        rsvClose: () => this.setState({ rsv: null }),
        rsvSetName: e => { const r = this.state.rsv; if (r) r.name = e.target.value; },
        rsvSetPhone: e => { const r = this.state.rsv; if (r) r.phone = e.target.value; },
        rsvKey: e => { if (e.key === 'Enter') { e.preventDefault(); this.submitReserve(); } },
        rsvSubmit: () => this.submitReserve(),
        myRes: MYRES.map(r => ({ code: r.code, title: r.title, artist: r.artist, status: r.status, label: RSV_LABELS[r.status] || r.status, track: rsvTrack(r.status) })),
        lookup: { ...this.state.lookup, statusLabel: this.state.lookup.result ? RSV_LABELS[this.state.lookup.result.status] : '', track: this.state.lookup.result ? rsvTrack(this.state.lookup.result.status) : '' },
        lkSetCode: e => { this.state.lookup.code = e.target.value; },
        lkSetPhone: e => { this.state.lookup.phone = e.target.value; },
        lkKey: e => { if (e.key === 'Enter') { e.preventDefault(); this.doLookup(); } },
        lkSubmit: () => this.doLookup(),
        preorders: PREORDERS.map(p => ({
          title: p.title, artist: p.artist, ships: p.ships_label, held: p.held, total: p.total_slots, remaining: Math.max(0, p.total_slots - p.held), soldOut: !!p.sold_out, pct: p.pct + '%', id: p.id, rawPrice: p.price, price: p.price != null ? VND(p.price) : 'Liên hệ', tint: genreAccent({ genre: '' }), cover: p.cover_image || '', initial: (p.title || '?').trim().charAt(0).toUpperCase(),
          reserve: () => this.openReserve(p)
        })),
        reviews: REVIEWS.map(rv => ({ stars: '★'.repeat(rv.stars), quote: rv.quote, who: rv.who })),
        email: this.state.email, nlLabel: this.state.subscribed ? 'Hẹn thứ Năm nhé' : 'Đăng ký',
        setEmail: e => this.setState({ email: e.target.value }),
        subscribe: async () => {
          try {
            const res = await apiFetch('/newsletter', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: this.state.email }) });
            this.setState({ subscribed: true }); this.say(res.message);
          } catch (err) { this.say(err.message || 'Email này có vẻ chưa đúng.'); }
        },
        closeDetail: () => { this._detailSeq = (this._detailSeq || 0) + 1; this.setState({ detail: null }); },
        addDetail: () => {
          if (!(detail && !detail.loading && d && d.hasPrice && !d.soldOut)) return;
          if (detailMax < 1) { this.say(detailLimit); return; }
          // A number typed but not yet committed (no blur) still counts.
          const n = this._detailQtyRaw != null ? clampDetailQty(this._detailQtyRaw) : detailQty;
          this._detailQtyRaw = null;
          this.addToBag(detail, n); this.setState({ detail: null, detailQty: 1 });
        },
        detailQty, detailMax, detailInCart,
        detailQtyDec: () => { this._detailQtyRaw = null; this.setState({ detailQty: Math.max(1, detailQty - 1) }); },
        detailQtyInc: () => { this._detailQtyRaw = null; if (detailQty >= detailMax) { this.say(detailLimit); return; } this.setState({ detailQty: detailQty + 1 }); },
        detailQtyInput: e => { this._detailQtyRaw = e.target.value; },
        detailQtyCommit: e => { const n = clampDetailQty(e.target.value, e.target); this._detailQtyRaw = null; this.setState({ detailQty: n }); },
        detailQtyKey: e => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } },
        stop: e => e.stopPropagation(), openCart: () => this.setState({ cartOpen: true }), closeCart: () => this.setState({ cartOpen: false }),
        cartOpen, cartLines, cartBlocked, cartIsEmpty: cartLines.length === 0,
        checkoutName: checkout.name, checkoutPhone: checkout.phone, checkoutAddress: checkout.address, checkoutCity: checkout.city, checkoutNote: checkout.note,
        setCheckoutName: e => this.setState(s => ({ checkout: { ...s.checkout, name: e.target.value } })),
        setCheckoutPhone: e => this.setState(s => ({ checkout: { ...s.checkout, phone: e.target.value } })),
        setCheckoutAddress: e => this.setState(s => ({ checkout: { ...s.checkout, address: e.target.value } })),
        setCheckoutCity: e => this.setState(s => ({ checkout: { ...s.checkout, city: e.target.value } })),
        setCheckoutNote: e => this.setState(s => ({ checkout: { ...s.checkout, note: e.target.value } })),
        shippingStdBg: checkout.shipping === 'standard' ? accent : T.surface, shippingStdFg: checkout.shipping === 'standard' ? '#121214' : T.text,
        shippingExpBg: checkout.shipping === 'express' ? accent : T.surface, shippingExpFg: checkout.shipping === 'express' ? '#121214' : T.text,
        pickShippingStd: () => this.setState(s => ({ checkout: { ...s.checkout, shipping: 'standard' } })),
        pickShippingExp: () => this.setState(s => ({ checkout: { ...s.checkout, shipping: 'express' } })),
        paymentOptions, checkoutError, shippingFeeLabel: shippingFee === 0 ? 'Miễn phí' : VND(shippingFee), checkoutTotal: VND(checkoutTotalNum), placeOrder: () => this.placeOrder(), placingOrder: this.state.placingOrder,
        lastOrder: lastOrder && Object.assign({}, lastOrder, { subtotalLabel: VND(lastOrder.subtotal), shippingLabel: lastOrder.shippingFee === 0 ? 'Miễn phí' : VND(lastOrder.shippingFee), totalLabel: VND(lastOrder.subtotal + lastOrder.shippingFee) })
      };
    }
  }

  const appInstance = new Component({ accent: '#FF5500', hoverPreview: true });

  let H = {}, hidc = 0;
  function reg(fn) { const id = 'h' + (hidc++); H[id] = fn; return id; }
  window.__h = function (id, event) { const fn = H[id]; if (fn) fn(event); };

  function coverStyle(r, fallbackA, fallbackB) {
    const safe = safeUrl(r.cover);
    return safe
      ? 'background-image: linear-gradient(135deg, rgba(0,0,0,.12), rgba(0,0,0,.22)), url(\'' + safe + '\'); background-size: cover; background-position: center;'
      : 'background-image: repeating-linear-gradient(135deg, ' + fallbackA + ' 0 9px, ' + fallbackB + ' 9px 18px);';
  }

  function recordCardHTML(r, kind) {
    return `
      <article onmouseenter="__h('${reg(r.enter)}',event)" onmouseleave="__h('${reg(r.leave)}',event)" onfocusin="__h('${reg(r.enter)}',event)" onfocusout="__h('${reg(r.leave)}',event)" class="shelf-8${r.soldOut ? ' is-soldout' : ''}" style="transform: ${r.lift};">
        <div onclick="__h('${reg(r.tap)}',event)" onkeydown="__h('${reg(r.keyOpen)}',event)" role="button" tabindex="0" aria-label="Xem trước ${esc(r.title)}" class="sleeve-stage">
          <div class="shelf-9" style="transform: ${r.discT};">
            <div class="shelf-10" style="animation: spin ${r.spinDur} linear infinite; box-shadow: ${r.discGlow};">
              <div class="shelf-11" style="background: ${r.tint};"></div>
              <div class="disc-spindle"></div>
            </div>
          </div>
          <div class="shelf-12" style="transform: ${r.flipT};">
            <div class="shelf-13" style="box-shadow: ${r.shadow};">
              <div class="shelf-14" style="${coverStyle(r, 'rgba(255,85,0,.22)', 'rgba(255,85,0,.06)')}"><span class="shelf-15">${r.cover ? '' : 'Bìa đĩa'}</span></div>
              <span class="shelf-16">${esc(r.badge)}</span>
            </div>
            <div class="shelf-17">
              <span class="shelf-18">Danh sách bài hát</span>
              <div class="shelf-19">
                ${(r.tracks || []).slice(0, 8).map(t => `<div class="track-row"><span class="track-no">${esc(t.no)}</span><span>${esc(t.name)}</span></div>`).join('')}
                ${(!r.tracks || !r.tracks.length) ? '<div class="track-row"><span>Chưa có danh sách bài hát</span></div>' : ''}
              </div>
            </div>
          </div>
          <button onclick="__h('${reg(r.toggleFlip)}',event)" title="Lật bìa đĩa" aria-label="Lật bìa để xem danh sách bài hát" class="shelf-20" style="opacity: ${r.uiOp}; transform: translateY(${r.uiY});">⟳</button>
          <div class="shelf-21" style="opacity: ${r.prevOp}; transform: translateY(${r.uiY});">
            <span class="vu-meter"><i class="vu-bar-1"></i><i class="vu-bar-2"></i><i class="vu-bar-3"></i></span>
            <span class="shelf-22">${esc(r.previewLabel)}</span>
          </div>
        </div>
        <div class="shelf-23">
          <div class="shelf-24">
            <a href="#${kind}" onclick="__h('${reg(r.open)}',event)" class="shelf-25">${esc(r.title)}</a>
            <div class="shelf-26">${esc(r.artist)}${r.year ? ' · ' + esc(r.year) : ''}</div>
          </div>
          <div class="shelf-27">
            <div class="price">${r.price}</div>
            <div class="shelf-28 stock-note${r.soldOut ? ' is-out' : r.lowStock ? ' is-low' : ''}">${esc(r.stockLabel)}</div>
          </div>
        </div>
        <div class="shelf-29" style="max-height: ${r.addH}; opacity: ${r.uiOp};">
          ${r.soldOut
            ? `<span class="shelf-30 is-soldout" aria-disabled="true">Hết hàng</span>`
            : r.hasPrice
              ? (r.qtyMax > 0
                ? `<div class="card-buy" onclick="__h('${reg(r.stopClick)}',event)"><div class="qty-pick is-compact" role="group" aria-label="Chọn số lượng"><button onclick="__h('${reg(r.qtyDec)}',event)" class="qty-btn" aria-label="Giảm số lượng">−</button><input type="number" inputmode="numeric" min="1" max="${r.qtyMax}" value="${r.qty}" data-field="cardQty" oninput="__h('${reg(r.qtyInput)}',event)" onchange="__h('${reg(r.qtyCommit)}',event)" onkeydown="__h('${reg(r.qtyKey)}',event)" class="qty-input" aria-label="Số lượng ${esc(r.title)}"><button onclick="__h('${reg(r.qtyInc)}',event)" class="qty-btn" aria-label="Tăng số lượng">+</button></div><button onclick="__h('${reg(r.add)}',event)" class="shelf-30">Thêm vào giỏ</button></div>`
                : `<span class="shelf-30 is-soldout" aria-disabled="true">Đã đủ số lượng trong giỏ</span>`)
              : `<span class="shelf-30" style="opacity:.55;cursor:default;">Liên hệ</span>`}
        </div>
      </article>`;
  }

  function renderRecordGrid(records, kind) { return (records || []).map(r => recordCardHTML(r, kind)).join(''); }

  function renderApp(D) {
    const d = D.detail;
    return `<div class="page-1" data-theme="${D.themeName}" style="--bg:${D.theme.bg};--surface:${D.theme.surface};--surfaceAlt:${D.theme.surfaceAlt};--text:${D.theme.text};--muted:${D.theme.muted};--border:${D.theme.border};--borderStrong:${D.theme.borderStrong};--headerBg:${D.theme.headerBg};--gradient:${D.theme.gradient};--stripeA:${D.theme.stripeA};--stripeB:${D.theme.stripeB};">
      <header class="header-1"><div class="header-2"><div class="header-3"><span class="header-4">Bụi Records</span><span class="header-5">Hà Nội · thành lập 2009</span></div>
        <nav class="header-6">
          <a href="#home" onclick="__h('${reg(D.goHomeNav)}',event)" class="text-default">Trang chủ</a>
          <a href="#vinyls" onclick="__h('${reg(D.goCatalog)}',event)" class="text-default">Sản phẩm</a>
          <a href="#preorders" onclick="__h('${reg(D.goPreordersPage)}',event)" class="text-default">Đặt trước</a>
          <a href="#gear" onclick="__h('${reg(D.goGearPage)}',event)" class="text-default">Phụ kiện</a>
          <a href="#visit" onclick="__h('${reg(D.goVisitPage)}',event)" class="text-default">Ghé thăm</a>
        </nav>
        <button onclick="__h('${reg(D.goSearch)}',event)" class="header-7 header-theme" title="Tìm đĩa" aria-label="Tìm đĩa">${SEARCH_ICON}</button>
        <button onclick="__h('${reg(D.toggleTheme)}',event)" class="header-7 header-theme" title="${D.themeToggleLabel}" aria-label="${D.themeToggleLabel}">${D.themeIcon}</button>
        ${D.user
          ? `<button onclick="__h('${reg(D.goAccount)}',event)" class="header-7 header-account" title="Tài khoản của ${esc(D.user.name)}" aria-label="Tài khoản của ${esc(D.user.name)}"><span class="acct-dot">${esc(D.userInitial)}</span><span class="acct-name">${esc(D.userGiven)}</span></button>`
          : `<button onclick="__h('${reg(D.openLogin)}',event)" class="header-7">Đăng nhập</button>`}
        <button onclick="__h('${reg(D.openCart)}',event)" class="header-8">Giỏ hàng <span class="header-9">${D.cartCount}</span></button>
      </div></header>

      ${D.isHome ? `
      <section class="home-1"><div class="home-2"><div onmouseenter="__h('${reg(D.heroIn)}',event)" onmouseleave="__h('${reg(D.heroOut)}',event)" onclick="__h('${reg(D.heroTap)}',event)" role="button" tabindex="0" class="home-3">
        <div class="home-4" style="transform:${D.heroSlide};"><div class="home-5" style="animation:spin ${D.heroSpinDur} linear infinite;"><div class="home-6"><span class="home-7">MẶT A</span></div><div class="home-8"></div></div></div>
        <div class="home-9" style="box-shadow:${D.heroShadow};"><div class="home-10"><span class="home-11">Bản phát hành</span></div><div class="home-12"><span class="home-13">Kho đĩa</span><span class="home-14">Bụi Records</span></div></div>
      </div></div>
      <div class="hero-1"><span class="hero-2">Cửa hàng đĩa than Hà Nội</span><h1 class="hero-3">Bụi Records chọn từng chiếc đĩa cho bạn.<em class="hero-4">Nghe kỹ, lục kỹ, mang về nhà.</em></h1>
        <p class="hero-5">Những bản đĩa than được tuyển chọn, thông tin chi tiết lấy từ Discogs. Đặt hàng và đặt trước ngay trên website, giao tận nơi trên toàn quốc.</p>
        <div class="hero-6"><a href="#vinyls" onclick="__h('${reg(D.goCatalog)}',event)" class="hero-7">Xem sản phẩm</a><a href="#preorders" onclick="__h('${reg(D.goPreordersPage)}',event)" class="hero-8">Đặt trước</a></div>
        <div class="hero-9"><div><div class="hero-10">Discogs</div><div class="hero-11">nguồn thông tin đĩa</div></div><div><div class="hero-10">VND</div><div class="hero-11">giá bán tại cửa hàng</div></div><div><div class="hero-10">24h</div><div class="hero-11">xác nhận đơn hàng</div></div></div>
      </div></section>
      <div class="hero-12" aria-label="Thông tin cửa hàng"><div class="hero-13"><div class="ticker-half">${TICKER_HTML}</div><div class="ticker-half" aria-hidden="true">${TICKER_HTML}</div></div></div>
      <section id="new" class="ticker-1"><div class="shelf-1"><div><span class="eyebrow">Kệ đĩa</span><h2 class="shelf-2">Mới lên kệ</h2></div></div><p class="shelf-5">${D.shelfBlurb}</p>
        <div class="shelf-6">${D.genres.map(g => `<button onclick="__h('${reg(g.pick)}',event)" class="shelf-7" style="border:1px solid ${g.bd};background:${g.bg};color:${g.fg};transform:${g.tf};">${g.name}</button>`).join('')}</div>
        <div class="record-grid">${renderRecordGrid(D.cards, 'new')}</div>${!D.cards.length ? '<p class="shelf-31">Chưa có đĩa nào ở bộ lọc này.</p>' : ''}
      </section>
      <section class="shelf-32"><div class="shelf-33">${D.reviews.map(v => `<figure class="reviews-1"><div class="reviews-2">${v.stars}</div><blockquote class="reviews-3">${esc(v.quote)}</blockquote><figcaption class="reviews-4">${esc(v.who)}</figcaption></figure>`).join('')}</div></section>
      <section class="reviews-5"><div><span class="eyebrow">Ghé thăm</span><h2 class="visit-1">Ghé qua cửa hàng của chúng tôi</h2><p class="visit-2">Nghe thử trước khi mua, cà phê pha sẵn, và kệ đĩa luôn có gì mới để lục.</p><a href="#visit" onclick="__h('${reg(D.goVisitPage)}',event)" class="visit-3">Xem giờ mở cửa &amp; bản đồ →</a></div><div class="visit-4"><div class="header-5">Ghé qua</div><div class="visit-5">14 Hàng Bún, Ba Đình<br>Hà Nội</div><div class="visit-6"><div class="hours-row"><span>Thứ Ba — Thứ Năm</span><span class="font-semibold">11:00 — 21:00</span></div><div class="hours-row"><span>Thứ Sáu — CN</span><span class="font-semibold">11:00 — 23:00</span></div><div class="visit-7"><span>Thứ Hai</span><span class="font-semibold">lục kho đĩa</span></div></div></div></section>
      <footer class="footer-1"><div class="footer-2"><span class="footer-3">Bụi Records</span><div class="footer-4"><a href="#preorders" onclick="__h('${reg(D.goPreordersPage)}',event)" class="text-muted">Đặt trước</a><a href="#visit" onclick="__h('${reg(D.goVisitPage)}',event)" class="text-muted">Liên hệ</a></div><span class="footer-5">© 2026 · HÀ NỘI</span></div></footer>` : ''}

      ${D.isCatalog ? `<section id="vinyls" class="catalog-1"><aside class="catalog-2"><button onclick="__h('${reg(D.goHome)}',event)" class="catalog-3">← Về cửa hàng</button><span class="eyebrow">Cửa hàng</span><h1 class="catalog-4">Sản phẩm</h1>${D.catalogGenres.map(cg => `<button onclick="__h('${reg(cg.pick)}',event)" class="catalog-5" style="background:${cg.bg};color:${cg.fg};">${cg.name}</button>`).join('')}</aside><div><div class="catalog-search" role="search">${SEARCH_ICON}<input data-field="catalogSearch" value="${esc(D.catalogQuery)}" oninput="__h('${reg(D.searchInput)}',event)" onkeydown="__h('${reg(D.searchKey)}',event)" type="text" inputmode="search" enterkeyhint="search" autocomplete="off" spellcheck="false" maxlength="100" placeholder="Tìm theo tên đĩa, nghệ sĩ, hãng đĩa…" aria-label="Tìm sản phẩm" class="field">${D.catalogQuery ? `<button onclick="__h('${reg(D.clearSearch)}',event)" class="catalog-search-x" aria-label="Xóa tìm kiếm">×</button>` : ''}</div><p class="catalog-6" aria-live="polite">${D.catalogSearched ? D.catalogCount + ' kết quả cho “' + esc(D.catalogSearched) + '”.' : D.catalogCount + ' sản phẩm.'}</p>${D.loadingCatalog ? '<p class="shelf-31">Đang tải…</p>' : '<div class="record-grid' + (D.catalogBusy ? ' is-busy' : '') + '">' + renderRecordGrid(D.catalogCards, 'vinyls') + '</div>'}${!D.loadingCatalog && !D.catalogCards.length ? (D.catalogSearched ? '<p class="shelf-31">Không có sản phẩm nào khớp từ khóa này. Thử tên nghệ sĩ, tên đĩa hoặc hãng đĩa khác' + (D.catalogGenre !== 'Tất cả' ? ', hoặc chọn thể loại “Tất cả”' : '') + '.</p>' : '<p class="shelf-31">Không có sản phẩm nào ở bộ lọc này.</p>') : ''}</div></section>` : ''}


      ${D.isPreordersPage ? `<section class="preorder-1"><button onclick="__h('${reg(D.goHome)}',event)" class="preorder-2">← Về trang chủ</button>
        <div class="po-hero"><div><span class="preorder-5">Đặt trước</span><h1 class="preorder-6">Giữ chỗ, đề tên bạn</h1><p class="preorder-7">Những bản ép giới hạn sắp về Bụi. Giữ một suất bằng một cú bấm — chúng mình liên hệ để xác nhận khi đĩa cập bến.</p></div></div>
        ${D.preorders.length ? `<div class="po-grid">${D.preorders.map(p => `<article class="po-card${p.soldOut ? ' is-out' : ''}">
          <div class="po-art" style="--po-tint:${p.tint};"><div class="po-disc"><i></i></div><div class="po-sleeve"${p.cover ? ` style="background-image:url('${safeUrl(p.cover)}');background-size:cover;background-position:center;"` : ''}>${p.cover ? '' : `<span>${p.initial}</span>`}</div><span class="po-ships">${esc(p.ships)}</span></div>
          <div class="po-body"><div><h3 class="po-title">${esc(p.title)}</h3><div class="po-artist">${esc(p.artist)}</div></div>
            <div class="po-meter"><div class="po-bar"><i style="width:${p.pct};"></i></div><div class="po-count"><span>${p.soldOut ? 'Đã hết suất' : 'Còn <b>' + p.remaining + '</b> suất'}</span><span>${p.held}/${p.total} đã giữ</span></div></div>
            <div class="po-foot"><div class="po-price">${p.price}</div>${p.soldOut ? '<span class="po-btn is-disabled">Hết suất</span>' : `<button onclick="__h('${reg(p.reserve)}',event)" class="po-btn">Giữ đĩa</button>`}</div></div></article>`).join('')}</div>` : '<p class="shelf-31">Chưa có đợt đặt trước nào — quay lại sau nhé.</p>'}
        <div class="rsv-panel">
          <div class="rsv-box"><h2 class="rsv-h">Giữ chỗ của bạn</h2>
            ${D.myRes.length ? D.myRes.map(r => `<div class="rsv-item"><div class="rsv-item-top"><div><b>${esc(r.title)}</b><small>${esc(r.artist)} · mã ${esc(r.code)}</small></div><span class="rsv-badge s-${r.status}">${r.label}</span></div>${r.track}</div>`).join('') : '<p class="rsv-empty">Bạn chưa giữ chỗ đĩa nào. Bấm “Giữ đĩa” ở trên để bắt đầu.</p>'}
          </div>
          <div class="rsv-box"><h2 class="rsv-h">Tra cứu giữ chỗ</h2><p class="rsv-empty">Nhập mã giữ chỗ (dạng GC-XXXXXX) và số điện thoại đã đăng ký.</p>
            <div class="rsv-form"><input data-field="lkCode" value="${esc(D.lookup.code)}" oninput="__h('${reg(D.lkSetCode)}',event)" onkeydown="__h('${reg(D.lkKey)}',event)" placeholder="Mã giữ chỗ" class="field"><input data-field="lkPhone" value="${esc(D.lookup.phone)}" oninput="__h('${reg(D.lkSetPhone)}',event)" onkeydown="__h('${reg(D.lkKey)}',event)" placeholder="Số điện thoại" inputmode="tel" class="field"><button onclick="__h('${reg(D.lkSubmit)}',event)" class="po-btn"${D.lookup.busy ? ' disabled' : ''}>${D.lookup.busy ? 'Đang tìm…' : 'Tra cứu'}</button></div>
            ${D.lookup.error ? `<p class="rsv-error">${esc(D.lookup.error)}</p>` : ''}
            ${D.lookup.result ? `<div class="rsv-item"><div class="rsv-item-top"><div><b>${esc(D.lookup.result.title)}</b><small>${esc(D.lookup.result.artist)} · mã ${esc(D.lookup.result.code)}</small></div><span class="rsv-badge s-${D.lookup.result.status}">${D.lookup.statusLabel}</span></div>${D.lookup.track}</div>` : ''}
          </div>
        </div>
      </section>` : ''}

      ${D.isGearPage ? `<section class="preorder-1"><button onclick="__h('${reg(D.goHome)}',event)" class="preorder-2">← Về trang chủ</button>
        <div class="gr-hero"><span class="preorder-5">Phụ kiện</span><h1 class="preorder-6">Chăm đĩa, nghe trọn vẹn</h1><p class="preorder-7">Kim đĩa, đồ vệ sinh, bao bảo quản và thiết bị nghe — chọn lọc để chiếc đĩa của bạn bền và hay như ngày đầu.</p></div>
        <div class="gr-filters">${D.gearCats.map(c => `<button onclick="__h('${reg(c.pick)}',event)" class="catalog-5" style="background:${c.bg};color:${c.fg};">${c.name}</button>`).join('')}<span class="gr-count">${D.gearCount} sản phẩm</span></div>
        <div class="gr-grid">${D.gearItems.map(g => `<article class="gr-card">
          <div class="gr-art" style="--gr-tint:${g.tint};"><svg viewBox="0 0 64 64" class="gr-icon" aria-hidden="true">${g.icon}</svg>${g.tag ? `<span class="gr-tag">${g.tag}</span>` : ''}<span class="gr-cat">${g.cat}</span></div>
          <div class="gr-body"><h3 class="gr-name">${g.name}</h3><p class="gr-desc">${g.desc}</p>
            <div class="gr-foot"><div class="gr-price">${g.priceLabel}</div><button onclick="__h('${reg(g.ask)}',event)" class="po-btn">Hỏi mua</button></div></div></article>`).join('')}</div>
        <p class="gr-note">Bản mẫu giao diện — phụ kiện chưa mở bán online. Giá và sản phẩm chỉ mang tính minh họa.</p>
      </section>` : ''}

      ${D.isVisitPage ? `<section class="preorder-1"><button onclick="__h('${reg(D.goHome)}',event)" class="preorder-2">← Về trang chủ</button>
        <div class="vs-hero"><div><span class="preorder-5">Ghé thăm</span><h1 class="preorder-6">Ghé qua Bụi,<br>nghe thử trước khi mua</h1><p class="preorder-7">Một góc nhỏ ở Ba Đình dành cho đĩa than: nghe thử, uống cà phê, lục kệ và nói chuyện âm nhạc.</p></div>
          <div class="vs-status ${D.visit.open ? 'is-open' : 'is-closed'}"><i></i><span>${D.visit.statusLabel}</span></div></div>
        <div class="vs-grid">
          <div class="vs-map"><iframe title="Bản đồ Bụi Records" src="${D.mapEmbed}" loading="lazy" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
          <div class="vs-info">
            <div class="vs-block"><span class="vs-label">Địa chỉ</span><div class="vs-addr">14 Hàng Bún, Ba Đình<br>Hà Nội</div><a href="${D.mapUrl}" target="_blank" rel="noopener" class="po-btn vs-dir">Chỉ đường trên Google →</a></div>
            <div class="vs-block"><span class="vs-label">Giờ mở cửa</span>
              <ul class="vs-hours">${D.visit.hours.map(h => `<li class="${h.today ? 'is-today' : ''}${h.closed ? ' is-closed' : ''}"><span>${h.day}${h.today ? '<em>Hôm nay</em>' : ''}</span><b>${h.text}</b></li>`).join('')}</ul></div>
          </div>
        </div>
        <div class="vs-features">${D.visit.features.map(f => `<div class="vs-feat"><svg viewBox="0 0 64 64" aria-hidden="true">${f.icon}</svg><h3>${f.title}</h3><p>${f.text}</p></div>`).join('')}</div>
        <div class="vs-news"><div><span class="preorder-5">Thư thứ Năm</span><h2 class="vs-news-h">Một email, sáng ngày kệ đĩa mở ra</h2><p class="vs-news-p">Mỗi thứ Năm, Bụi gửi danh sách đĩa mới về kệ. Không spam, hủy bất cứ lúc nào.</p></div>
          <div class="vs-news-form"><input data-field="email" value="${esc(D.email)}" oninput="__h('${reg(D.setEmail)}',event)" placeholder="Email của bạn" class="field"><button onclick="__h('${reg(D.subscribe)}',event)" class="po-btn">${D.nlLabel}</button></div></div>
      </section>` : ''}

      ${D.isAccountPage && D.user ? `<section class="preorder-1"><button onclick="__h('${reg(D.goHome)}',event)" class="preorder-2">← Về trang chủ</button><div class="po-hero"><div><span class="preorder-5">Tài khoản</span><h1 class="preorder-6">Xin chào, ${esc(D.userGiven)}</h1><p class="preorder-7">${esc(D.user.email)}</p></div></div><div class="acct-panel"><div class="rsv-box"><h2 class="rsv-h">Thông tin của bạn</h2><div class="rsv-form"><label class="acct-label">Họ và tên<input data-field="accName" value="${esc(D.account.name)}" oninput="__h('${reg(D.accSetName)}',event)" maxlength="120" autocomplete="name" class="field"></label><div class="acct-label">Email<div class="acct-static">${esc(D.user.email)}</div></div><label class="acct-label">Số điện thoại<input data-field="accPhone" value="${esc(D.account.phone)}" oninput="__h('${reg(D.accSetPhone)}',event)" inputmode="tel" autocomplete="tel" maxlength="20" placeholder="Không bắt buộc" class="field"></label>${D.account.error ? `<p class="rsv-error" role="alert">${esc(D.account.error)}</p>` : ''}${D.account.note ? `<p class="acct-ok" role="status">${esc(D.account.note)}</p>` : ''}<button onclick="__h('${reg(D.saveProfile)}',event)" class="po-btn"${D.account.busy ? ' disabled' : ''}>${D.account.busy ? 'Đang lưu…' : 'Lưu thay đổi'}</button></div><button onclick="__h('${reg(D.logout)}',event)" class="acct-logout">Đăng xuất</button></div><div class="rsv-box"><h2 class="rsv-h">Đơn hàng của bạn</h2>${D.account.loading ? '<p class="rsv-empty">Đang tải đơn hàng…</p>' : D.account.ordersError ? `<p class="rsv-error">${esc(D.account.ordersError)}</p>` : D.accountOrders.length ? D.accountOrders.map(o => `<div class="rsv-item"><div class="rsv-item-top"><div><b>${esc(o.order_no)}</b><small>${esc(o.dateLabel)} · ${esc(o.payment_label)}</small></div><span class="rsv-badge ${o.badge}">${esc(o.status_label)}</span></div><ul class="acct-lines">${o.lines.map(l => `<li><span>${esc(l.title)} × ${l.qty}</span><span>${l.total}</span></li>`).join('')}</ul><div class="acct-total"><span>Tổng cộng</span><b>${o.totalLabel}</b></div>${o.refundedLabel ? `<div class="acct-total acct-refund"><span>Đã hoàn lại</span><b>${o.refundedLabel}</b></div>` : ''}${o.trackingLabel ? `<p class="acct-meta">${esc(o.trackingLabel)}</p>` : ''}${o.caseList.map(c => `<div class="acct-case"><div class="acct-case-top"><b>${esc(c.title)}</b><span class="acct-case-status s-${esc(c.status)}">${esc(c.label)}</span></div><small>Mã yêu cầu ${esc(c.code)}</small>${c.replies.map(r => `<p class="acct-reply"><span>Bụi, ${esc(fmtDate(r.at))}</span>${esc(r.text)}</p>`).join('')}</div>`).join('')}${o.cancelLabel || o.reportLabel ? `<div class="acct-actions">${o.cancelLabel ? `<button onclick="__h('${reg(o.cancel)}',event)" class="acct-act">${o.cancelLabel}</button>` : ''}${o.reportLabel ? `<button onclick="__h('${reg(o.report)}',event)" class="acct-act">${o.reportLabel}</button>` : ''}${o.reportUntilLabel ? `<small>Hạn báo sự cố, trả hàng: ${esc(o.reportUntilLabel)}</small>` : ''}</div>` : ''}</div>`).join('') : '<p class="rsv-empty">Bạn chưa có đơn hàng nào. Những đơn đặt khi đã đăng nhập sẽ hiện ở đây.</p>'}</div></div></section>` : ''}

      ${D.isCheckout ? `<section class="checkout-1"><div><button onclick="__h('${reg(D.backToCart)}',event)" class="checkout-2">← Quay lại giỏ hàng</button><h1 class="checkout-3">Thanh toán</h1><p class="checkout-auth">${D.user ? `Đơn hàng sẽ được lưu vào tài khoản <b>${esc(D.user.email)}</b>.` : `Đã có tài khoản? <button onclick="__h('${reg(D.openLogin)}',event)" class="checkout-auth-btn">Đăng nhập</button> để điền sẵn thông tin giao hàng và xem lại đơn sau này. Bạn vẫn có thể đặt hàng không cần tài khoản.`}</p><div class="checkout-4"><input data-field="checkoutName" value="${esc(D.checkoutName)}" oninput="__h('${reg(D.setCheckoutName)}',event)" placeholder="Họ và tên" class="field"><input data-field="checkoutPhone" value="${esc(D.checkoutPhone)}" oninput="__h('${reg(D.setCheckoutPhone)}',event)" placeholder="Số điện thoại" class="field"><input data-field="checkoutAddress" value="${esc(D.checkoutAddress)}" oninput="__h('${reg(D.setCheckoutAddress)}',event)" placeholder="Địa chỉ nhận hàng" class="field"><input data-field="checkoutCity" value="${esc(D.checkoutCity)}" oninput="__h('${reg(D.setCheckoutCity)}',event)" placeholder="Tỉnh / thành phố" class="field"><input data-field="checkoutNote" value="${esc(D.checkoutNote)}" oninput="__h('${reg(D.setCheckoutNote)}',event)" placeholder="Ghi chú cho cửa hàng (không bắt buộc)" class="field"></div><div class="checkout-5"><div class="checkout-6">Vận chuyển</div><div class="checkout-7"><button onclick="__h('${reg(D.pickShippingStd)}',event)" class="checkout-8" style="background:${D.shippingStdBg};color:${D.shippingStdFg};"><div class="checkout-9">Tiêu chuẩn</div><div class="checkout-10">Miễn phí từ 1.500.000₫</div></button><button onclick="__h('${reg(D.pickShippingExp)}',event)" class="checkout-8" style="background:${D.shippingExpBg};color:${D.shippingExpFg};"><div class="checkout-9">Hỏa tốc</div><div class="checkout-10">+45.000₫ · giao trong ngày hôm sau</div></button></div></div><div class="checkout-11"><div class="checkout-6">Thanh toán</div><div class="checkout-12">${D.paymentOptions.map(pm => `<button onclick="__h('${reg(pm.pick)}',event)" class="checkout-13" style="border:1px solid ${pm.bd};background:${pm.bg};color:${pm.fg};">${pm.label}</button>`).join('')}</div></div>${D.checkoutError ? `<p class="checkout-14">${esc(D.checkoutError)}</p>` : ''}<button onclick="__h('${reg(D.placeOrder)}',event)" class="checkout-15"${D.placingOrder ? ' disabled aria-busy="true"' : ''}>${D.placingOrder ? 'Đang đặt hàng…' : 'Đặt hàng'}</button></div><aside class="checkout-16"><div class="checkout-17">Tóm tắt đơn hàng</div>${D.cartLines.map(c => `<div class="checkout-18"><span>${esc(c.title)} × ${c.qty}</span><span class="checkout-19">${c.lineTotal}</span></div>`).join('')}<div class="checkout-20"><span class="checkout-21">Tạm tính</span><span>${D.cartSubtotal}</span></div><div class="checkout-22"><span class="checkout-21">Vận chuyển</span><span>${D.shippingFeeLabel}</span></div><div class="checkout-23"><span>Tổng cộng</span><span>${D.checkoutTotal}</span></div></aside></section>` : ''}

      ${D.isConfirmed ? `<section class="confirmed-1"><div class="confirmed-2">✓</div><h1 class="confirmed-3">Đặt hàng thành công</h1><p class="confirmed-4">Đơn hàng <strong class="checkout-19">${esc(D.lastOrder.orderNo)}</strong> — Bụi sẽ nhắn tới ${esc(D.lastOrder.phone)} khi đóng gói xong.</p><div class="confirmed-5">${D.lastOrder.items.map(c => `<div class="confirmed-6"><span>${esc(c.title)} × ${c.qty}</span></div>`).join('')}<div class="confirmed-7"><span class="checkout-21">Tạm tính</span><span>${D.lastOrder.subtotalLabel}</span></div><div class="checkout-22"><span class="checkout-21">Vận chuyển</span><span>${D.lastOrder.shippingLabel}</span></div><div class="checkout-22"><span class="checkout-21">Thanh toán</span><span>${esc(D.lastOrder.paymentLabel)}</span></div><div class="confirmed-8"><span>Tổng cộng</span><span>${D.lastOrder.totalLabel}</span></div></div><button onclick="__h('${reg(D.goHome)}',event)" class="confirmed-9">Tiếp tục xem đĩa</button></section>` : ''}

      ${D.cartOpen ? `<div onclick="__h('${reg(D.closeCart)}',event)" class="cart-1"></div><aside onclick="__h('${reg(D.stop)}',event)" class="cart-2" role="dialog" aria-modal="true" aria-label="Giỏ hàng"><div class="cart-3"><span class="cart-4">Giỏ hàng của bạn</span><button onclick="__h('${reg(D.closeCart)}',event)" aria-label="Đóng giỏ hàng" class="cart-5">×</button></div><div class="cart-6">${D.cartIsEmpty ? '<p class="cart-7">Giỏ hàng đang trống.</p>' : ''}${D.cartLines.map(c => `<div class="cart-8${c.problem ? ' is-unavailable' : ''}"><div class="cart-9"><div class="cart-10" style="background:${c.tint};"></div></div><div class="cart-11"><div class="cart-12">${esc(c.title)}</div>${c.problem ? `<div class="rsv-error">${esc(c.problem)}</div>` : ''}<div class="cart-13">${c.priceEach} / đĩa${c.stockNote ? ' · ' + c.stockNote : ''}</div><div class="cart-14"><button onclick="__h('${reg(c.dec)}',event)" class="cart-15">−</button><input type="number" inputmode="numeric" min="1" max="${c.maxQty}" value="${c.qty}" onchange="__h('${reg(c.commitQty)}',event)" onkeydown="__h('${reg(c.qtyKey)}',event)" class="cart-16 qty-input" aria-label="Số lượng ${esc(c.title)}"><button onclick="__h('${reg(c.inc)}',event)" class="cart-15">+</button>${c.showFix ? `<button onclick="__h('${reg(c.fixQty)}',event)" class="cart-17">Lấy ${c.available} bản</button>` : ''}<button onclick="__h('${reg(c.remove)}',event)" class="cart-17">Xóa</button></div></div><div class="cart-18${c.problem ? ' is-void' : ''}">${c.lineTotal}</div></div>`).join('')}</div><div class="cart-19"><div class="cart-20"><span class="checkout-21">Tạm tính</span><span class="cart-21">${D.cartSubtotal}</span></div>${D.cartBlocked ? '<p class="rsv-error">Có đĩa không thể đặt. Hãy xóa hoặc chỉnh số lượng trước khi thanh toán.</p>' : ''}<button onclick="__h('${reg(D.goCheckout)}',event)" class="cart-22${D.cartBlocked ? ' is-disabled' : ''}"${D.cartBlocked ? ' aria-disabled="true"' : ''}>Thanh toán</button></div></aside>` : ''}

      ${d ? `<div onclick="__h('${reg(D.closeDetail)}',event)" class="detail-1"><div onclick="__h('${reg(D.stop)}',event)" class="detail-2"><div class="detail-3"><div class="detail-4"><div class="detail-5"><div class="detail-6"><div class="detail-7" style="background:${d.tint};"></div><div class="disc-spindle"></div></div></div><div class="detail-8"><div class="shelf-14" style="${coverStyle(d, 'rgba(255,85,0,.22)', 'rgba(255,85,0,.06)')}"><span class="shelf-15">${d.cover ? '' : 'Bìa đĩa'}</span></div></div></div></div><div class="detail-9"><div class="detail-10"><span class="detail-11">${esc(d.badge)}</span><button onclick="__h('${reg(D.closeDetail)}',event)" aria-label="Đóng" class="detail-12">×</button></div><h3 class="detail-13">${esc(d.title)}</h3><div class="detail-14">${esc(d.artist)} · ${esc([d.year, viGenre(d.genre), d.country].filter(Boolean).join(' · '))}</div><p class="detail-15">${d.loading ? 'Đang tải thông tin đĩa…' : (d.notes ? 'Ghi chú gốc từ Discogs (tiếng Anh): ' + esc(d.notes) : 'Thông tin chi tiết do Discogs cung cấp.')}</p><div class="detail-16"><div><div class="detail-17">${esc(d.gradeLabel)}</div><div class="detail-18">TÌNH TRẠNG</div></div><div><div class="detail-17">${esc(d.format)}</div><div class="detail-18">ĐỊNH DẠNG</div></div><div><div class="detail-17">${esc(d.stockLabel)}</div><div class="detail-18">TỒN KHO</div></div></div><div class="detail-19">${(d.tracks || []).map(t => `<div class="detail-20"><span class="detail-21">${esc(t.no)}</span><span>${esc(t.name)}</span></div>`).join('')}</div><div class="detail-22"><span class="detail-23">${d.priceLabel}</span>${d.soldOut ? `<span class="detail-24 is-soldout" aria-disabled="true">Hết hàng</span>` : d.hasPrice ? `<div class="detail-buy">${D.detailMax > 0 ? `<div class="qty-pick" role="group" aria-label="Chọn số lượng"><button onclick="__h('${reg(D.detailQtyDec)}',event)" class="qty-btn" aria-label="Giảm số lượng">−</button><input type="number" inputmode="numeric" min="1" max="${D.detailMax}" value="${D.detailQty}" data-field="detailQty" oninput="__h('${reg(D.detailQtyInput)}',event)" onchange="__h('${reg(D.detailQtyCommit)}',event)" onkeydown="__h('${reg(D.detailQtyKey)}',event)" class="qty-input" aria-label="Số lượng"><button onclick="__h('${reg(D.detailQtyInc)}',event)" class="qty-btn" aria-label="Tăng số lượng">+</button></div><button onclick="__h('${reg(D.addDetail)}',event)" class="detail-24">Thêm vào giỏ</button>` : `<span class="detail-24 is-soldout" aria-disabled="true">Đã đủ số lượng trong giỏ</span>`}</div>${D.detailInCart ? `<p class="detail-qty-hint">Bạn đã có ${D.detailInCart} bản trong giỏ.</p>` : ''}` : `<span class="detail-24" style="opacity:.55;cursor:default;">Liên hệ</span>`}</div></div></div></div>` : ''}
      ${D.authOpen ? `<div onclick="__h('${reg(D.authClose)}',event)" class="rsv-back"><div onclick="__h('${reg(D.stop)}',event)" class="rsv-modal auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-title"><button onclick="__h('${reg(D.authClose)}',event)" aria-label="Đóng" class="rsv-x">×</button><h3 class="rsv-title" id="auth-title">${D.authIsLogin ? 'Đăng nhập' : 'Tạo tài khoản'}</h3><p class="rsv-sub">${D.authIsLogin ? 'Điền sẵn thông tin giao hàng và xem lại đơn hàng của bạn.' : 'Lưu thông tin giao hàng và xem lại các đơn đã đặt. Không bắt buộc để mua hàng.'}</p>${D.auth.notice ? `<p class="rsv-note" role="status">${esc(D.auth.notice)}</p>` : ''}${D.authIsLogin ? '' : authInput(D, { prop: 'name', label: 'Họ và tên', autocomplete: 'name', max: 120 })}${authInput(D, { prop: 'email', label: 'Email', type: 'email', autocomplete: D.authIsLogin ? 'username' : 'email', mode: 'email' })}${D.authIsLogin ? '' : authInput(D, { prop: 'phone', label: 'Số điện thoại (không bắt buộc)', type: 'tel', autocomplete: 'tel', mode: 'tel', max: 20 })}${authInput(D, { prop: 'password', label: D.authIsLogin ? 'Mật khẩu' : 'Mật khẩu (ít nhất 8 ký tự)', type: 'password', autocomplete: D.authIsLogin ? 'current-password' : 'new-password', max: 128 })}${D.auth.error ? `<p class="rsv-error" role="alert">${esc(D.auth.error)}</p>` : ''}<button onclick="__h('${reg(D.authSubmit)}',event)" class="po-btn rsv-full"${D.auth.busy ? ' disabled' : ''}>${D.auth.busy ? 'Đang xử lý…' : (D.authIsLogin ? 'Đăng nhập' : 'Tạo tài khoản')}</button><p class="auth-switch-row">${D.authIsLogin ? 'Chưa có tài khoản?' : 'Đã có tài khoản?'}<button onclick="__h('${reg(D.authSwitch)}',event)" class="auth-switch">${D.authIsLogin ? 'Tạo tài khoản' : 'Đăng nhập'}</button></p></div></div>` : ''}
      ${D.rsv ? `<div onclick="__h('${reg(D.rsvClose)}',event)" class="rsv-back"><div onclick="__h('${reg(D.stop)}',event)" class="rsv-modal" role="dialog" aria-modal="true">
        <button onclick="__h('${reg(D.rsvClose)}',event)" aria-label="Đóng" class="rsv-x">×</button>
        ${D.rsv.result ? `<div class="rsv-ok">✓</div><h3 class="rsv-title">Đã ghi nhận giữ chỗ</h3>
          <p class="rsv-sub">${esc(D.rsv.title)} · ${esc(D.rsv.artist)}</p>
          <div class="rsv-code"><span>Mã giữ chỗ</span><b>${esc(D.rsv.result.code)}</b></div>
          <div class="rsv-statusrow"><span>Trạng thái</span><span class="rsv-badge s-${D.rsv.result.status}">${D.rsv.statusLabel}</span></div>
          ${D.rsv.track}
          <p class="rsv-note">Bụi sẽ gọi/nhắn tới ${esc(D.rsv.phone)} để xác nhận. Hãy lưu mã trên — bạn có thể tra cứu trạng thái bất cứ lúc nào ở cuối trang Đặt trước.</p>
          <button onclick="__h('${reg(D.rsvClose)}',event)" class="po-btn rsv-full">Xong</button>`
        : `<h3 class="rsv-title">Giữ chỗ đĩa</h3>
          <p class="rsv-sub">${esc(D.rsv.title)} · ${esc(D.rsv.artist)} · ${D.rsv.priceLabel}</p>
          <input data-field="rsvName" value="${esc(D.rsv.name)}" oninput="__h('${reg(D.rsvSetName)}',event)" onkeydown="__h('${reg(D.rsvKey)}',event)" placeholder="Họ và tên" class="field">
          <input data-field="rsvPhone" value="${esc(D.rsv.phone)}" oninput="__h('${reg(D.rsvSetPhone)}',event)" onkeydown="__h('${reg(D.rsvKey)}',event)" placeholder="Số điện thoại" inputmode="tel" class="field">
          ${D.rsv.error ? `<p class="rsv-error">${esc(D.rsv.error)}</p>` : ''}
          <p class="rsv-note">Giữ chỗ chưa cần thanh toán. Bụi sẽ liên hệ xác nhận, và báo khi đĩa về kho.</p>
          <button onclick="__h('${reg(D.rsvSubmit)}',event)" class="po-btn rsv-full"${D.rsv.busy ? ' disabled' : ''}>${D.rsv.busy ? 'Đang gửi…' : 'Xác nhận giữ chỗ'}</button>`}
      </div></div>` : ''}
      ${D.orderReq ? `<div onclick="__h('${reg(D.reqClose)}',event)" class="rsv-back"><div onclick="__h('${reg(D.stop)}',event)" class="rsv-modal req-modal" role="dialog" aria-modal="true" aria-labelledby="req-title">
        <button onclick="__h('${reg(D.reqClose)}',event)" aria-label="Đóng" class="rsv-x">×</button>
        <h3 class="rsv-title" id="req-title">${D.orderReq.title}</h3>
        <p class="rsv-sub">Đơn ${esc(D.orderReq.order_no)}</p>
        <div class="req-reasons" role="radiogroup" aria-label="${D.orderReq.kind === 'cancel' ? 'Lý do hủy' : 'Vấn đề gặp phải'}">${D.orderReq.options.map(r => `<button type="button" role="radio" aria-checked="${r.on}" onclick="__h('${reg(r.pick)}',event)" class="req-reason${r.on ? ' is-on' : ''}">${esc(r.label)}</button>`).join('')}</div>
        <textarea data-field="reqDetail" oninput="__h('${reg(D.reqDetail)}',event)" maxlength="1000" rows="3" placeholder="Mô tả thêm (bắt buộc với Lý do khác, thiếu hàng, sai đĩa)" class="field req-detail">${esc(D.orderReq.detail)}</textarea>
        <p class="rsv-note">${esc(D.orderReq.info)}</p>
        ${D.orderReq.error ? `<p class="rsv-error" role="alert">${esc(D.orderReq.error)}</p>` : ''}
        <button onclick="__h('${reg(D.reqSubmit)}',event)" class="po-btn rsv-full"${D.orderReq.busy ? ' disabled' : ''}>${D.orderReq.busy ? 'Đang gửi…' : D.orderReq.kind === 'cancel' && D.orderReq.mode === 'instant' ? 'Hủy đơn' : 'Gửi yêu cầu'}</button>
      </div></div>` : ''}
      ${D.toast ? `<div class="toast-1" role="status" aria-live="polite">${esc(D.toast)}</div>` : ''}
    </div>`;
  }

  const root = document.getElementById('app');

  function morphChildren(parent, newParent) {
    const oldNodes = Array.prototype.slice.call(parent.childNodes);
    const newNodes = Array.prototype.slice.call(newParent.childNodes);
    const max = Math.max(oldNodes.length, newNodes.length);
    for (let i = 0; i < max; i++) {
      const o = oldNodes[i], n = newNodes[i];
      if (!n) { parent.removeChild(o); continue; }
      if (!o) { parent.appendChild(n); continue; }
      morphNode(parent, o, n);
    }
  }

  function morphNode(parent, o, n) {
    if (o.nodeType !== n.nodeType || o.nodeName !== n.nodeName) { parent.replaceChild(n, o); return; }
    if (o.nodeType === 3 || o.nodeType === 8) { if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue; return; }
    if (o.nodeType === 1) {
      Array.prototype.slice.call(o.attributes).forEach(a => { if (!n.hasAttribute(a.name)) o.removeAttribute(a.name); });
      Array.prototype.slice.call(n.attributes).forEach(a => { if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value); });
      if (o.tagName === 'INPUT' && document.activeElement !== o) { const v = n.getAttribute('value') || ''; if (o.value !== v) o.value = v; }
      morphChildren(o, n);
    }
  }

  // Removing a focused element (e.g. the "Thêm vào giỏ" button the customer just clicked, when the
  // card turns into "Đã đủ số lượng trong giỏ" or "Hết hàng") fires blur/focusout synchronously, and
  // those handlers call setState -> render(). A render started inside a render would rebuild the
  // handler table and walk DOM nodes that are already gone ("replaceChild ... no longer a child"),
  // so a render requested while one is running is queued and run right after it.
  let rendering = false, renderAgain = false;
  function render() {
    if (rendering) { renderAgain = true; return; }
    rendering = true;
    try {
      do {
        renderAgain = false;
        const D = appInstance.renderVals();
        H = {}; hidc = 0;
        const temp = document.createElement('div');
        temp.innerHTML = renderApp(D);
        morphChildren(root, temp);
      } while (renderAgain);
    } finally {
      rendering = false;
    }
  }

  function renderLoading() {
    root.innerHTML = '<div style="min-height:100vh;display:grid;place-items:center;background:#121214;color:#F4F4F5;font-family:\'Be Vietnam Pro\',Helvetica,sans-serif;">Đang mở cửa hàng…</div>';
  }

  function renderLoadError(err) {
    console.error(err);
    root.innerHTML = '<div style="min-height:100vh;display:grid;place-items:center;text-align:center;padding:24px;background:#121214;color:#F4F4F5;font-family:\'Be Vietnam Pro\',Helvetica,sans-serif;"><div>Không kết nối được máy chủ cửa hàng.<br>Hãy chắc chắn máy chủ đang chạy và <code>DISCOGS_TOKEN</code> đã được cấu hình.</div></div>';
  }

  async function boot() {
    renderLoading();
    try {
      const [homeData, preorders, reviews, cart, me] = await Promise.all([
        fetchRecords({ per_page: 8, page: 1 }),
        apiFetch('/preorders'),
        apiFetch('/reviews'),
        apiFetch('/cart'),
        // A failing account check must never stop the shop from opening.
        apiFetch('/auth/me').catch(() => ({ user: null }))
      ]);
      RECORDS = homeData.records;
      PREORDERS = preorders;
      REVIEWS = reviews;
      appInstance.state.cartItems = normalizeCartItems(cart.items);
      if (cart.max_qty) MAX_QTY = cart.max_qty;
      appInstance.state.user = me.user || null;
      appInstance.state.checkout = fillCheckout(appInstance.state.checkout, appInstance.state.user);
      render();
    } catch (err) {
      renderLoadError(err);
    }
  }

  // compositionend has no inline on* attribute, so listen on the document. Vietnamese IMEs (Telex/VNI)
  // mark every input event of a syllable as "composing"; this is what tells us the syllable is done.
  document.addEventListener('compositionend', e => {
    const t = e.target;
    if (t && t.dataset && t.dataset.field === 'catalogSearch') appInstance.onSearchInput(t.value, false);
  });

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !appInstance) return;
    const st = appInstance.state;
    if (st.rsv || st.detail || st.cartOpen || st.authOpen || st.orderReq) { appInstance._detailSeq = (appInstance._detailSeq || 0) + 1; appInstance.setState({ rsv: null, detail: null, cartOpen: false, authOpen: false, auth: blankAuth(), orderReq: null }); }
  });

  boot();
})();
