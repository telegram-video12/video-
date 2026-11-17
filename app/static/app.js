/* Streamora - single page app (hash routing, no build step) */
'use strict';

const $app = document.getElementById('app');
const $tabbar = document.getElementById('tabbar');
let cleanup = null;          // called when leaving the current page
let libraryCache = null;
let titleNewSince = null;    // episodes that arrived after this time (and are unwatched) get a "new" badge

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = h ? String(m).padStart(2, '0') : m;
  return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
}
const fmtDur = (sec) => {
  if (!sec) return '';
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} ש׳ ${m % 60} ד׳` : `${m} דק׳`;
};
const fmtLen = (sec) => `${Math.floor(sec / 3600)}:${String(Math.round(sec / 60) % 60).padStart(2, '0')}`;
const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(1) + 'GB' : Math.round(b / 1e6) + 'MB';
const epLabel = (m) => m.episode != null ? `ע${m.season ?? 1} · פ${m.episode}` : '';
const epLong = (m) => m.episode != null ? `עונה ${m.season ?? 1}, פרק ${m.episode}` : '';
const pct = (m) => {
  if (m.completed) return 100;
  const d = m.duration;
  return d && m.position ? Math.min(100, (m.position / d) * 100) : 0;
};
const firstLine = (s) => (s || '').split('\n').find((l) => l.trim()) || '';
const bg = (url) => url && !url.startsWith('/api/thumb') ? `style="background-image:url('${esc(url)}')"` : '';
const img = (url) => url ? `<img class="cover" src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">` : '';

async function api(path, opts = {}) {
  const init = {...opts, headers: {...(opts.headers || {})}};
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    init.body = JSON.stringify(opts.body);
    init.headers['Content-Type'] = 'application/json';
  }
  let r;
  try { r = await fetch(path, init); } catch (_) {
    throw new Error('אין חיבור לאתר כרגע. בדוק שהאתר פועל (ב-Termux או במחשב) ושיש רשת.');
  }
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `שגיאה ${r.status}`);
  return data;
}

function errorBox(e) { return `<div class="error-box">${esc(e.message || e)}</div>`; }

// ------------------------------------------------------------------- router
// Every page remembers how far you scrolled and how much was loaded, so "back" lands where you were.
const pageState = {};
const TAB_OF = {'': 'home', series: 'series', movies: 'movies', search: 'home', mylist: 'mylist', more: 'more', settings: 'more'};
let lastTab = 'home';

async function route() {
  const prev = route.current;
  route.cameFrom = prev || null;
  if (prev && pageState[prev]) pageState[prev].scrollY = window.scrollY;
  if (cleanup) { try { cleanup(); } catch (_) {} cleanup = null; }
  closeAllSheets();
  const hash = location.hash.replace(/^#/, '') || '/';
  route.current = hash;
  const [path] = hash.split('?');
  const parts = path.split('/').filter(Boolean);
  const first = parts[0] || '';
  // tab bar: hidden in the player; inner pages (title) keep the tab you came from
  const tab = TAB_OF[first] ?? lastTab;
  lastTab = tab;
  $tabbar.hidden = first === 'watch';
  document.querySelectorAll('#tabbar a, #topnav [data-tab]').forEach((a) => a.classList.toggle('on', a.dataset.tab === tab));
  document.body.classList.toggle('in-player', first === 'watch');
  const st = pageState[hash] || (pageState[hash] = {});
  window.scrollTo(0, 0);
  try {
    if (first === 'title') await pageTitle(+parts[1]);
    else if (first === 'watch') await pageWatch(+parts[1]);
    else if (first === 'more' || first === 'settings') await pageSettings(parts[1]);
    else if (first === 'series') await pageList('series', st);
    else if (first === 'movies') await pageList('movie', st);
    else if (first === 'search') await pageSearch(st);
    else if (first === 'mylist') await pageMyList();
    else await pageHome();
    if (st.scrollY) requestAnimationFrame(() => window.scrollTo(0, st.scrollY));
    // a soft fade-and-rise for the new page (not when only the episode changes inside the player)
    if (!(first === 'watch' && prev && prev.startsWith('/watch/'))) {
      $app.classList.remove('page-in'); void $app.offsetWidth; $app.classList.add('page-in');
    }
  } catch (e) {
    if (e.message !== 'login') $app.innerHTML = errorBox(e);
  }
}
window.addEventListener('hashchange', route);
// tapping the tab you are already on scrolls back to the top
$tabbar.addEventListener('click', (e) => {
  const a = e.target.closest('a');
  if (a && a.getAttribute('href') === (location.hash || '#/')) { e.preventDefault(); window.scrollTo({top: 0, behavior: 'smooth'}); }
});

function pageHeader(title, sub = '', {logo = false, dice = null} = {}) {
  return `<header class="phead${logo ? ' home-head' : ''}">
    <div class="phead-title">${logo ? '<span class="logo" dir="ltr"><svg class="logo-mark" viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 4.6v14.8a1.2 1.2 0 0 0 1.8 1l12.2-7.4a1.2 1.2 0 0 0 0-2L8.3 3.6a1.2 1.2 0 0 0-1.8 1z" fill="#16d9c5"/></svg><span>Streamora</span></span>' : `<h1>${esc(title)}</h1>${sub ? `<div class="phead-sub">${sub}</div>` : ''}`}</div>
    ${dice !== null ? `<button class="iconbtn dice" data-dice="${dice}" aria-label="תפתיע אותי" title="תפתיע אותי">🎲</button>` : ''}
    <a class="iconbtn" href="#/search" aria-label="חיפוש"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg></a>
  </header>`;
}

function emptyLibrary() {
  return `<div class="empty"><h2>הספרייה עדיין ריקה</h2>
    <p>בחר את קבוצות הטלגרם שמהן ייאספו הסדרות והסרטים, והפעל סריקה.</p>
    <a class="btn primary" href="#/more">להגדרות</a></div>`;
}

const PAGE = 60;
const n = (x) => Number(x || 0).toLocaleString('he-IL');
// "פרק אחד" instead of "1 פרקים"
const count = (x, one, many) => (Number(x) === 1 ? one : `${n(x)} ${many}`);

// ---------------------------------------------------------------------- home
async function pageHome() {
  if (cleanup) { cleanup(); cleanup = null; }  // drawn again (e.g. after arranging the rows): stop the old slideshow
  const head = pageHeader('', '', {logo: true, dice: ''});
  if (!libraryCache) $app.innerHTML = head + skeletonRows();
  const [all, series, movies, extra, newMovies, newSeries] = await Promise.all([
    api('/api/library?limit=20'),
    api('/api/library?kind=series&limit=20&nocont=1'),
    api('/api/library?kind=movie&limit=20&nocont=1'),
    api('/api/home').catch(() => ({new: [], genres: [], watchlist: []})),
    api('/api/library?kind=movie&sort=release&limit=20&nocont=1').catch(() => ({titles: []})),
    api('/api/library?kind=series&sort=release&limit=20&nocont=1').catch(() => ({titles: []})),
  ]);
  libraryCache = all;
  if (!all.counts.all) { $app.innerHTML = head + emptyLibrary(); return; }
  const cont = all.continue || [];
  const fresh = [...series.titles, ...movies.titles].find((t) => t.backdrop);
  const top = cont[0] || (fresh ? {title: fresh} : null);
  const feat = extra.featured || [];
  const posterRow = (list) => `<div class="hrow">${list.map(posterCard).join('')}</div>`;
  // the rows, in Netflix-like order; each one can be hidden with "סידור דף הבית" (remembered on this device)
  const rows = [
    ['cont', 'המשך צפייה', () => cont.length && rowSection('המשך צפייה', '', `<div class="hrow">${cont.map(contCard).join('')}</div>`)],
    ['new', '✨ פרקים חדשים', () => extra.new.length && rowSection('✨ פרקים חדשים', '', posterRow(extra.new))],
    ['watch', '⭐ לצפות בהמשך', () => (extra.watchlist || []).length && rowSection('⭐ לצפות בהמשך', '#/mylist', posterRow(extra.watchlist))],
    ['top', '🏆 טופ 10 שלי', () => (extra.top || []).length && rowSection('🏆 טופ 10 שלי', '', `<div class="hrow top10">${extra.top.map((t, i) => `<div class="top-item"><span class="top-num">${i + 1}</span>${posterCard(t)}</div>`).join('')}</div>`)],
    ['newSeries', '📺 סדרות חדשות', () => newSeries.titles.length && rowSection('📺 סדרות חדשות', '', posterRow(newSeries.titles))],
    ['newMovies', '🎬 סרטים חדשים', () => newMovies.titles.length && rowSection('🎬 סרטים חדשים', '', posterRow(newMovies.titles))],
    ['genres', 'שורות לפי ז\'אנרים', () => extra.genres.slice(0, 3).map((r) => rowSection(extra.personal ? `כי אתה אוהב ${r.genre}` : r.genre, '', posterRow(r.titles))).join('')],
    ['recentSeries', 'סדרות שנוספו לאחרונה', () => series.titles.length && rowSection('סדרות שנוספו לאחרונה', '#/series', posterRow(series.titles))],
    ['recentMovies', 'סרטים שנוספו לאחרונה', () => movies.titles.length && rowSection('סרטים שנוספו לאחרונה', '#/movies', posterRow(movies.titles))],
  ];
  const hidden = new Set(store.get('home:hidden', []));
  $app.innerHTML = head + (feat.length ? carousel(feat) : top ? banner(top) : '') +
    rows.filter(([k]) => !hidden.has(k)).map(([, , html]) => html() || '').join('') +
    '<div class="home-foot"><button class="linkbtn" id="home-rows">⚙ סידור דף הבית</button></div>' + copyright();
  $app.querySelector('#home-rows').onclick = async () => {
    const shown = rows.filter(([k]) => !hidden.has(k)).map(([k]) => k);
    const pick = await multiSheet('אילו שורות להציג בדף הבית?', rows.map(([k, label]) => [k, label]), shown,
      'מה שתסיר לא נמחק: הוא פשוט לא מוצג בדף הבית של המכשיר הזה.');
    if (!pick) return;
    store.set('home:hidden', rows.map(([k]) => k).filter((k) => !pick.includes(k)));
    pageHome();
  };
  if (feat.length) cleanup = startCarousel($app.querySelector('.cf'));
}

// site credit, at the bottom of the home and settings pages (and on the login page)
const copyright = () => '<footer class="site-credit">© 2026 בנימין אמבאו · כל הזכויות שמורות</footer>';

// the top of the home page: a 3D wheel of square cards (coverflow). Swipe, tap a side card, or let it turn.
function carousel(list) {
  const items = list.slice().sort(() => Math.random() - 0.5);  // a different first card every visit
  carousel.items = items;
  return `<section class="cf">
    <div class="cf-glow"><img alt=""></div>
    <div class="cf-stage">${items.map((t, i) => `<a class="cf-card" data-i="${i}" href="#/title/${t.id}">
      <div class="cf-img">${img(t.backdrop || t.poster)}</div>
      <span class="cf-tag">${t.recent ? '🆕 חדש' : '⭐ מומלץ'}</span></a>`).join('')}</div>
    <div class="cf-info"></div>
    <div class="cf-dots">${items.map((_, i) => `<i data-i="${i}"></i>`).join('')}</div>
    <button class="cf-arrow next" aria-label="הבא">‹</button><button class="cf-arrow prev" aria-label="הקודם">›</button>
  </section>`;
}

function startCarousel(root) {
  const items = carousel.items || [];
  const cards = [...root.querySelectorAll('.cf-card')];
  const dots = [...root.querySelectorAll('.cf-dots i')];
  const info = root.querySelector('.cf-info');
  const glow = root.querySelector('.cf-glow img');
  const n = cards.length;
  let cur = 0, auto = null;
  const rtl = getComputedStyle(root).direction === 'rtl';
  const draw = () => {
    cards.forEach((c, i) => {
      let d = i - cur;  // shortest way around the wheel
      if (d > n / 2) d -= n;
      if (d < -n / 2) d += n;
      const a = Math.abs(d), x = (rtl ? -1 : 1) * d;
      c.style.transform = `translateX(${x * 56}%) translateZ(${-a * 90}px) rotateY(${-x * 32}deg) scale(${1 - Math.min(a, 3) * 0.12})`;
      c.style.zIndex = String(20 - a);
      c.style.opacity = a > 2 ? '0' : String(1 - a * 0.28);
      c.style.pointerEvents = a > 2 ? 'none' : '';
      c.classList.toggle('on', d === 0);
    });
    dots.forEach((d, i) => d.classList.toggle('on', i === cur));
    const t = items[cur];
    if (!t) return;
    glow.src = t.backdrop || t.poster || '';
    const facts = [t.year, (t.genres || []).slice(0, 2).join(', '), t.rating ? `⭐ ${t.rating}` : ''].filter(Boolean)
      .map((x) => `<bdi>${esc(x)}</bdi>`).join(' · ');
    info.innerHTML = `<div class="cf-text"><h2>${esc(t.name)}</h2><div class="facts">${facts}</div>
      <div class="cf-actions"><a class="btn primary" href="#/watch/${t.play_id}">▶ צפה עכשיו</a>
      <a class="btn ghost" href="#/title/${t.id}">ⓘ פרטים</a></div></div>`;
  };
  const go = (i) => { cur = ((i % n) + n) % n; draw(); };
  const stop = () => { clearInterval(auto); auto = null; };  // once you touch it, it stays
  // swipe (and drag with the mouse on a computer)
  const stage = root.querySelector('.cf-stage');
  let x0 = null, moved = false;
  stage.addEventListener('dragstart', (e) => e.preventDefault());  // a dragged picture would cancel the swipe
  stage.addEventListener('pointerdown', (e) => { x0 = e.clientX; moved = false; stop(); });
  stage.addEventListener('pointermove', (e) => { if (x0 !== null && Math.abs(e.clientX - x0) > 12) moved = true; });
  stage.addEventListener('pointerup', (e) => {
    if (x0 === null) return;
    const dx = e.clientX - x0;
    x0 = null;
    // right-to-left: the next card waits on the left, so dragging the finger to the right brings it in
    if (Math.abs(dx) > 40) go(cur + ((rtl ? dx > 0 : dx < 0) ? 1 : -1));
  });
  stage.addEventListener('click', (e) => {
    const c = e.target.closest('.cf-card');
    if (!c) return;
    if (moved || !c.classList.contains('on')) { e.preventDefault(); if (!moved) { stop(); go(+c.dataset.i); } }
  });
  dots.forEach((d, i) => d.onclick = () => { stop(); go(i); });
  root.querySelector('.cf-arrow.next').onclick = () => { stop(); go(cur + 1); };
  root.querySelector('.cf-arrow.prev').onclick = () => { stop(); go(cur - 1); };
  draw();
  auto = setInterval(() => { if (!document.hidden) go(cur + 1); }, 6000);
  return () => clearInterval(auto);
}

function rowSection(title, more, body) {
  const arrows = body.includes('class="hrow"')
    ? '<button class="rowarrow next" data-scroll="1" aria-label="הבא">‹</button><button class="rowarrow prev" data-scroll="-1" aria-label="הקודם">›</button>' : '';
  return `<section class="rsec"><div class="rsec-head"><h2>${esc(title)}</h2>${more ? `<a href="${more}">הכל ›</a>` : ''}</div><div class="rwrap">${arrows}${body}</div></section>`;
}
// row arrows (computer): scroll a row by one screen; in right-to-left rows "next" moves left
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-scroll]');
  if (!b) return;
  const row = b.parentElement.querySelector('.hrow');
  row.scrollBy({left: -Number(b.dataset.scroll) * row.clientWidth * 0.9, behavior: 'smooth'});
});

function banner(item, kicker = '') {
  // the big picture at the top of the home page: what you are watching, or something new
  const t = item.title, m = item.media;
  const pic = t.backdrop || t.poster || (m && m.thumb);
  const facts = [t.year, (t.genres || []).slice(0, 2).join(', '), t.rating ? `⭐ ${t.rating}` : ''].filter(Boolean).map((x) => `<bdi>${esc(x)}</bdi>`).join(' · ');
  const resume = m && item.state;
  const left = m && m.duration ? Math.max(0, m.duration - (item.position || 0)) : 0;
  const href = m ? `#/watch/${m.id}` : `#/title/${t.id}`;
  return `<section class="banner${pic ? '' : ' banner-noimg'}">
    <div class="banner-img">${pic ? img(pic) : ''}</div>
    <div class="banner-text">
      <div class="kicker">${kicker || (resume ? '▶ המשך מאיפה שעצרת' : '✨ חדש בספרייה')}</div>
      <h1>${esc(t.name)}</h1>
      ${facts ? `<div class="facts">${facts}</div>` : ''}
      ${t.overview ? `<p class="ov">${esc(t.overview)}</p>` : ''}
      ${resume ? `<div class="where">${esc(epLong(m) || '')}${left ? `${epLong(m) ? ' · ' : ''}נשארו ${fmtDur(left)}` : ''}</div>
        <div class="bar banner-bar"><i style="width:${item.state === 'next' ? 0 : pct({...m, position: item.position})}%"></i></div>` : ''}
      <div class="banner-actions">
        <a class="btn primary" href="${href}">▶ ${resume ? 'המשך צפייה' : 'צפה עכשיו'}</a>
        <a class="btn ghost" href="#/title/${t.id}">ⓘ פרטים</a>
      </div>
    </div>
  </section>`;
}

function skeletonRows() {
  const cards = Array.from({length: 4}, () => '<div class="poster-card"><div class="poster sk"></div></div>').join('');
  return [1, 2].map(() => `<section class="rsec"><div class="rsec-head"><h2 class="sk sk-text"></h2></div><div class="hrow">${cards}</div></section>`).join('');
}
function skeletonGrid() {
  return `<div class="pgrid">${Array.from({length: 9}, () => '<div class="poster-card"><div class="poster sk"></div></div>').join('')}</div>`;
}

function undoToast(text, onUndo, ms = 5000) {
  document.querySelector('.undo-toast')?.remove();
  const el = document.createElement('div');
  el.className = 'undo-toast';
  el.innerHTML = `<span>${esc(text)}</span><button>בטל</button>`;
  const timer = setTimeout(() => el.remove(), ms);
  el.querySelector('button').onclick = () => { clearTimeout(timer); el.remove(); onUndo(); };
  document.body.appendChild(el);
}

// ------------------------------------------------------------ series / movies
const store = {  // localStorage can be missing (private mode): never let it break the page
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} },
};
const DECADES = [['', 'כל השנים'], ['2020', '2020 ומעלה'], ['2010', '2010–2019'], ['2000', '2000–2009'], ['1990', '1990–1999'], ['old', 'לפני 1990']];
const SORTS = [['added', 'נוסף לאחרונה'], ['watched', 'נצפה לאחרונה'], ['name', 'א–ת'], ['year', 'שנה (חדש קודם)'], ['rating', 'דירוג (גבוה קודם)']];

// ---- the phone's "back" closes an open sheet instead of leaving the page
const openSheets = [];
let popsToIgnore = 0, afterPop = null;
function sheetOpened(closeByBack) {
  const entry = {closeByBack};
  openSheets.push(entry);
  history.pushState({sheet: true}, '');
  // call when the page closes the sheet itself; resolves once the extra history step is gone
  return () => new Promise((done) => {
    const i = openSheets.indexOf(entry);
    if (i < 0) { done(); return; }
    openSheets.splice(i, 1);
    popsToIgnore++;
    afterPop = done;
    setTimeout(() => { if (afterPop === done) { afterPop = null; done(); } }, 400);  // in case no popstate comes
    history.back();
  });
}
window.addEventListener('popstate', () => {
  if (popsToIgnore) {
    popsToIgnore--;
    const f = afterPop; afterPop = null;
    if (f) f();
    return;
  }
  const top = openSheets.pop();
  if (top) top.closeByBack();
});
function closeAllSheets() {  // leaving the page: no sheet may stay on top of the next one
  openSheets.splice(0).forEach((s) => s.closeByBack());
  document.querySelectorAll('.sheet-wrap').forEach((el) => el.remove());
}

function multiSheet(title, options, chosen, note = '') {
  // options: [[value, label]]; returns the new list, or null when closed without "החל"
  return new Promise((resolve) => {
    const picked = new Set(chosen);
    const el = document.createElement('div');
    el.className = 'sheet-wrap';
    el.innerHTML = `<div class="sheet"><div class="sheet-grip"></div>
      <div class="sheet-head"><h3>${esc(title)}</h3><button class="linkbtn" data-clear>נקה</button></div>
      ${note ? `<p class="sheet-note">${note}</p>` : ''}
      <div class="sheet-list">${options.map(([v, l]) => `<button class="sheet-opt ${picked.has(v) ? 'on' : ''}" data-v="${esc(v)}">${esc(l)}<span class="tick">${picked.has(v) ? '✓' : ''}</span></button>`).join('')}</div>
      <button class="btn primary wide" data-apply>החל</button></div>`;
    const hide = () => { el.classList.remove('open'); setTimeout(() => el.remove(), 200); };
    const release = sheetOpened(() => { hide(); resolve(null); });
    const close = (v) => { hide(); release().then(() => resolve(v)); };
    el.addEventListener('click', (e) => {
      const b = e.target.closest('.sheet-opt');
      if (b) {
        const v = b.dataset.v;
        picked.has(v) ? picked.delete(v) : picked.add(v);
        b.classList.toggle('on', picked.has(v));
        b.querySelector('.tick').textContent = picked.has(v) ? '✓' : '';
      } else if (e.target.closest('[data-clear]')) {
        picked.clear();
        el.querySelectorAll('.sheet-opt').forEach((x) => { x.classList.remove('on'); x.querySelector('.tick').textContent = ''; });
      } else if (e.target.closest('[data-apply]')) close([...picked]);
      else if (e.target === el) close(null);
    });
    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add('open'));
  });
}

// ------------------------------------------------------------- surprise me
// 🎲 picks something you have not started yet; "another one" keeps rolling
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-dice]');
  if (b) { e.preventDefault(); surprise(b.dataset.dice || ''); }
});

async function surprise(kind) {
  const el = document.createElement('div');
  el.className = 'sheet-wrap';
  el.innerHTML = '<div class="sheet dice-sheet"><div class="sheet-grip"></div><div class="spinner"></div></div>';
  const hide = () => { el.classList.remove('open'); setTimeout(() => el.remove(), 200); };
  const release = sheetOpened(hide);
  const close = (href) => { hide(); release().then(() => { if (href) location.hash = href; }); };
  el.addEventListener('click', (e) => {
    const a = e.target.closest('a[href^="#"]');
    if (a) { e.preventDefault(); close(a.getAttribute('href')); return; }
    if (e.target === el || e.target.closest('[data-close]')) close();
  });
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('open'));
  const kinds = [['', 'הכל'], ['series', 'סדרה'], ['movie', 'סרט']];
  let last = '';
  const roll = async () => {
    const box = el.querySelector('.sheet');
    let t;
    try { t = await api(`/api/random?kind=${kind}&not=${last}`); } catch (err) {
      box.innerHTML = `<div class="sheet-grip"></div><h3>🎲 תפתיע אותי</h3><p class="muted">${esc(err.message)}</p>
        <div class="dice-actions"><button class="btn ghost" data-close>סגור</button></div>`;
      return;
    }
    last = t.id;
    const facts = [t.kind === 'series' ? 'סדרה' : 'סרט', t.year, (t.genres || []).slice(0, 2).join(', '), t.rating ? `⭐ ${t.rating}` : '']
      .filter(Boolean).map((x) => `<bdi>${esc(x)}</bdi>`).join(' · ');
    box.innerHTML = `<div class="sheet-grip"></div>
      <div class="dice-head"><h3>🎲 תפתיע אותי</h3>
        <div class="chips">${kinds.map(([k, l]) => `<button class="tab ${k === kind ? 'active' : ''}" data-k="${k}">${l}</button>`).join('')}</div></div>
      <a class="dice-pick" href="#/title/${t.id}">
        <div class="poster"><div class="ph">${esc(t.name)}</div>${img(t.poster)}</div>
        <div class="dice-info"><h2>${esc(t.name)}</h2><div class="facts">${facts}</div>
          ${t.overview ? `<p class="ov">${esc(t.overview)}</p>` : ''}</div>
      </a>
      <div class="dice-actions">
        <a class="btn primary" href="#/title/${t.id}">בוא נראה ›</a>
        <button class="btn ghost" data-again>🎲 עוד אחד</button>
      </div>`;
    box.querySelector('[data-again]').onclick = roll;
    box.querySelectorAll('[data-k]').forEach((c) => { c.onclick = () => { kind = c.dataset.k; last = ''; roll(); }; });
  };
  roll();
}

function bottomSheet(title, options, current) {
  return new Promise((resolve) => {
    const el = document.createElement('div');
    el.className = 'sheet-wrap';
    el.innerHTML = `<div class="sheet"><div class="sheet-grip"></div><h3>${esc(title)}</h3>
      ${options.map(([v, l]) => `<button class="sheet-opt ${v === current ? 'on' : ''}" data-v="${esc(v)}">${esc(l)}${v === current ? '<span>✓</span>' : ''}</button>`).join('')}
    </div>`;
    const hide = () => { el.classList.remove('open'); setTimeout(() => el.remove(), 200); };
    const release = sheetOpened(() => { hide(); resolve(null); });
    const close = (v) => { hide(); release().then(() => resolve(v)); };
    el.addEventListener('click', (e) => {
      const b = e.target.closest('.sheet-opt');
      if (b) close(b.dataset.v); else if (e.target === el) close(null);
    });
    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add('open'));
  });
}

async function pageList(kind, st) {
  const isSeries = kind === 'series';
  const title = isSeries ? 'סדרות' : 'סרטים';
  const key = `filters:${kind}`;
  const f = {state: 'all', decade: '', sort: '', genres: [], ...store.get(key, {})};
  const states = isSeries
    ? [['all', 'הכל'], ['watching', 'באמצע'], ['new', 'לא התחלתי'], ['done', 'סיימתי']]
    : [['all', 'הכל'], ['watching', 'באמצע'], ['new', 'לא ראיתי'], ['done', 'ראיתי']];
  // "באמצע" is most useful sorted by what you watched last, unless you picked another order
  const sortOf = () => f.sort || (f.state === 'watching' ? 'watched' : 'added');
  const query = (offset, limit) => `/api/library?kind=${kind}&state=${f.state}&decade=${f.decade}&genres=${encodeURIComponent((f.genres || []).join(','))}&sort=${sortOf()}&offset=${offset}&limit=${limit}&nocont=1`;
  const label = (list, v) => (list.find(([k]) => k === v) || list[0])[1];

  const shell = (sub, body) => `<div class="listhead">` + pageHeader(title, sub, {dice: kind}) + `
    <div class="filters">
      <div class="chips">${states.map(([v, l]) => `<button class="tab ${f.state === v ? 'active' : ''}" data-state="${v}">${l}</button>`).join('')}</div>
      <div class="chips">
        <button class="tab ${f.genres.length ? 'set' : ''}" data-pick="genre">${f.genres.length ? esc(f.genres.length === 1 ? f.genres[0] : `${f.genres.length} ז'אנרים`) : "ז'אנר"} ▾</button>
        <button class="tab ${f.decade ? 'set' : ''}" data-pick="decade">${f.decade ? esc(label(DECADES, f.decade)) : 'שנה'} ▾</button>
        <button class="tab" data-pick="sort">מיון: ${esc(label(SORTS, sortOf()))} ▾</button>
      </div>
    </div></div>` + body;

  const render = async (fresh) => {
    if (fresh) { st.loaded = 0; st.scrollY = 0; }
    $app.innerHTML = shell('', skeletonGrid());
    bind();
    const want = Math.max(PAGE, st.loaded || 0);  // coming back: load as much as before
    const data = await api(query(0, Math.min(200, want)));
    let titles = data.titles;
    while (titles.length < want && titles.length < data.total) {
      const more = await api(query(titles.length, 200));
      if (!more.titles.length) break;
      titles = titles.concat(more.titles);
    }
    const filtered = f.state !== 'all' || f.decade || f.genres.length;
    let body;
    if (data.total) {
      body = `<div class="pgrid" id="grid">${titles.map(posterCard).join('')}</div>
        <div id="more" ${titles.length < data.total ? '' : 'hidden'}><div class="spinner"></div></div>`;
    } else if (filtered) {
      body = `<div class="empty"><h2>אין ${title} במצב הזה</h2><button class="btn" id="clear">נקה סינון</button></div>`;
    } else {
      body = emptyLibrary();
    }
    $app.innerHTML = shell(data.total || filtered ? `${n(data.total)} ${title}${filtered ? ' בסינון הזה' : ' בספרייה'}` : '', body);
    bind();
    st.loaded = titles.length;
    if (cleanup) cleanup();
    cleanup = infiniteScroll(async () => {
      if (st.loaded >= data.total) return false;
      const more = await api(query(st.loaded, PAGE));
      $app.querySelector('#grid').insertAdjacentHTML('beforeend', more.titles.map(posterCard).join(''));
      st.loaded += more.titles.length;
      return more.titles.length > 0 && st.loaded < data.total;
    });
  };

  const changed = () => { store.set(key, f); render(true); };
  function bind() {
    $app.querySelectorAll('[data-state]').forEach((b) => b.onclick = () => { f.state = b.dataset.state; changed(); });
    $app.querySelector('[data-pick=genre]').onclick = async () => {
      const {genres: list, loading} = await api(`/api/genres?kind=${kind}`);
      const note = loading ? `⏳ הז'אנרים עדיין נטענים מ-TMDB (${n(loading.done)} מתוך ${n(loading.total)}). המספרים יגדלו בהדרגה.` : '';
      if (!list.length) { alert("עדיין אין ז'אנרים. הם מגיעים מ-TMDB, תוך כדי הורדת הפוסטרים."); return; }
      const v = await multiSheet("ז'אנר", list.map((g) => [g.name, `${g.name} (${n(g.count)})`]), f.genres, note);
      if (v !== null) { f.genres = v; changed(); }
    };
    $app.querySelector('[data-pick=decade]').onclick = async () => {
      const v = await bottomSheet('שנה', DECADES, f.decade);
      if (v !== null && v !== f.decade) { f.decade = v; changed(); }
    };
    $app.querySelector('[data-pick=sort]').onclick = async () => {
      const v = await bottomSheet('מיון', SORTS, sortOf());
      if (v !== null && v !== sortOf()) { f.sort = v; changed(); }
    };
    const clear = $app.querySelector('#clear');
    if (clear) clear.onclick = () => { f.state = 'all'; f.decade = ''; f.genres = []; changed(); };
  }
  await render(false);
}

function infiniteScroll(loadMore) {
  const sentinel = $app.querySelector('#more');
  if (!sentinel || sentinel.hidden) return null;
  let busy = false;
  const obs = new IntersectionObserver(async (entries) => {
    if (busy || !entries.some((e) => e.isIntersecting)) return;
    busy = true;
    try {
      if (!(await loadMore())) { sentinel.hidden = true; obs.disconnect(); }
    } finally { busy = false; }
  }, {rootMargin: '900px'});
  obs.observe(sentinel);
  return () => obs.disconnect();
}

// -------------------------------------------------------------------- search
async function pageSearch(st) {
  $app.innerHTML = `<header class="phead"><div class="searchbox">
      <span>🔍</span><input id="q" type="search" enterkeyhint="search" placeholder="חיפוש סדרה או סרט..." value="${esc(st.q || '')}" autocomplete="off">
    </div><button class="iconbtn" id="search-close" aria-label="חזרה">✕</button></header>
    <div id="results"></div>`;
  const input = $app.querySelector('#q'), out = $app.querySelector('#results');
  // ✕: back to where you came from, or home when search was the first page opened
  $app.querySelector('#search-close').onclick = () => { if (route.cameFrom) history.back(); else location.hash = '#/'; };
  let timer = null, saveTimer = null, token = 0;
  const recent = () => store.get('recent-searches', []);
  const remember = (q) => store.set('recent-searches', [q, ...recent().filter((x) => x !== q)].slice(0, 8));
  let genreList = null;
  const showRecent = async () => {
    // nothing typed yet: recent searches + genres to browse
    const r = recent();
    const draw = () => {
      out.innerHTML = (r.length
        ? `<div class="rsec-head"><h2>חיפושים אחרונים</h2><a href="javascript:void 0" id="clear-recent">נקה</a></div>
           <div class="chips wrap">${r.map((q) => `<button class="tab" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>`
        : '<p class="muted center">הקלד שם של סדרה או סרט</p>') +
        (genreList && genreList.length ? `<div class="rsec-head"><h2>לפי ז'אנר</h2></div>
           <div class="genre-grid">${genreList.map((g) => `<button class="genre-tile" data-g="${esc(g.name)}"><b>${esc(g.name)}</b><span>${n(g.count)}</span></button>`).join('')}</div>` : '');
      out.querySelectorAll('[data-q]').forEach((b) => b.onclick = () => { input.value = b.dataset.q; run(); });
      out.querySelectorAll('[data-g]').forEach((b) => b.onclick = () => showGenre(b.dataset.g));
      const c = out.querySelector('#clear-recent');
      if (c) c.onclick = () => { store.set('recent-searches', []); showRecent(); };
    };
    draw();
    if (!genreList) {
      try { genreList = (await api('/api/genres')).genres.slice(0, 18); } catch (_) { genreList = []; }
      if (!input.value.trim() && !st.genre) draw();
    }
  };
  const showGenre = async (g) => {
    st.genre = g;
    const my = ++token;
    out.innerHTML = skeletonGrid();
    let s, m;
    try {
      [s, m] = await Promise.all([
        api(`/api/library?kind=series&sort=rating&limit=60&nocont=1&genres=${encodeURIComponent(g)}`),
        api(`/api/library?kind=movie&sort=rating&limit=60&nocont=1&genres=${encodeURIComponent(g)}`),
      ]);
    } catch (e) { out.innerHTML = errorBox(e); return; }
    if (my !== token) return;
    out.innerHTML = `<div class="rsec-head"><h2>ז'אנר: ${esc(g)}</h2><a href="javascript:void 0" id="genre-back">‹ חזרה</a></div>` +
      (s.total ? rowSection(`סדרות (${n(s.total)})`, '', `<div class="pgrid">${s.titles.map(posterCard).join('')}</div>`) : '') +
      (m.total ? rowSection(`סרטים (${n(m.total)})`, '', `<div class="pgrid">${m.titles.map(posterCard).join('')}</div>`) : '');
    out.querySelector('#genre-back').onclick = () => { st.genre = null; showRecent(); };
  };
  const run = async () => {
    const q = input.value.trim();
    st.q = q;
    const my = ++token;
    clearTimeout(saveTimer);
    if (!q) { if (st.genre) showGenre(st.genre); else showRecent(); return; }
    st.genre = null;
    out.innerHTML = skeletonGrid();
    let s, m;
    try {
      [s, m] = await Promise.all([
        api(`/api/library?kind=series&sort=year&limit=30&nocont=1&files=1&q=${encodeURIComponent(q)}`),
        api(`/api/library?kind=movie&sort=year&limit=60&files=1&q=${encodeURIComponent(q)}`),
      ]);
    } catch (e) {
      if (my !== token) return;
      out.innerHTML = `<div class="empty"><h2>החיפוש לא הצליח</h2><p>${esc(e.message)}</p><button class="btn" id="retry">נסה שוב</button></div>`;
      out.querySelector('#retry').onclick = run;
      return;
    }
    if (my !== token) return;
    const byFile = [...(s.by_file || []), ...(m.by_file || [])];
    const fileSection = byFile.length ? rowSection('📂 נמצא בקבצים בטלגרם, תחת שם אחר באתר', '',
      `<p class="muted" style="margin:-4px 0 10px;font-size:13px">בכיתוב של הסרטונים בטלגרם מופיע "${esc(q)}", אבל באתר הם נמצאים תחת השמות האלה. אפשר לתקן שם בדף שלהם ← ⋯ ← עריכה, או לאחד.</p>
       <div class="pgrid">${byFile.map(posterCard).join('')}</div>`) : '';
    if (!s.total && !m.total && byFile.length) { out.innerHTML = fileSection; return; }
    if (!s.total && !m.total) {
      out.innerHTML = `<div class="empty"><h2>לא נמצא "${esc(q)}" בספרייה</h2><p>אולי הקבוצה שלו לא מסומנת ב<a class="link" href="#/more">הגדרות</a>.</p></div>`;
      return;
    }
    if (q.length >= 2) saveTimer = setTimeout(() => remember(q), 1500);
    if (s.season) {  // "הישרדות עונה 8": open the series right at that season
      out.dataset.season = s.season;
    } else delete out.dataset.season;
    out.innerHTML =
      (s.total ? rowSection(`סדרות (${n(s.total)})`, '', `<div class="pgrid">${s.titles.map(posterCard).join('')}</div>`) : '') +
      (m.total ? rowSection(`סרטים (${n(m.total)})`, '', `<div class="pgrid">${m.titles.map(posterCard).join('')}</div>`) : '') +
      fileSection;
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 300); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { input.blur(); if (input.value.trim().length >= 2) remember(input.value.trim()); } });
  out.addEventListener('click', (e) => {
    const card = e.target.closest('.poster-card');
    if (!card) return;
    if (input.value.trim().length >= 2) remember(input.value.trim());
    const id = (card.getAttribute('href') || '').split('/').pop();
    if (out.dataset.season && id) sessionStorage.setItem(`season:${id}`, out.dataset.season);
  });
  run();
  if (!st.q) setTimeout(() => input.focus(), 50);
  cleanup = () => { clearTimeout(timer); clearTimeout(saveTimer); };
}

// ------------------------------------------------------------------- my list
async function pageMyList() {
  const tabs = [['list', '⭐ לצפות בהמשך'], ['history', '🕘 היסטוריה'], ['stats', '📊 סטטיסטיקה']];
  let tab = store.get('mylist:tab', 'list');
  if (!tabs.some(([k]) => k === tab)) tab = 'list';
  $app.innerHTML = pageHeader('הרשימה שלי') + skeletonRows();
  const data = await api('/api/mylist');
  const draw = () => {
    $app.innerHTML = pageHeader('הרשימה שלי') +
      `<div class="chips mylist-tabs">${tabs.map(([k, l]) => `<button class="tab ${k === tab ? 'active' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>
       <div id="mlbody">${tab === 'list' ? myWatchlist(data) : tab === 'history' ? myHistory(data) : myStats(data.stats)}</div>`;
    $app.querySelectorAll('.mylist-tabs [data-tab]').forEach((b) => b.onclick = () => { tab = b.dataset.tab; store.set('mylist:tab', tab); draw(); });
    if (tab === 'history') bindHistory();
  };
  const bindHistory = () => {
    const hide = async (ids, msg) => {
      try { await api('/api/history/hide', {method: 'POST', body: {ids}}); } catch (e) { alert(e.message); return; }
      data.history = data.history.filter((h) => !ids.includes(h.media_id));
      draw();
      if (msg) undoToast(msg, null);
    };
    $app.querySelectorAll('[data-forget]').forEach((b) => b.onclick = (e) => { e.preventDefault(); hide([+b.dataset.forget]); });
    $app.querySelector('#clear-history')?.addEventListener('click', () => {
      if (confirm('למחוק את כל ההיסטוריה? ההתקדמות בצפייה (איפה עצרת, מה נצפה) נשארת.')) hide(data.history.map((h) => h.media_id));
    });
  };
  draw();
}

function myWatchlist(data) {
  if (!data.watchlist.length) {
    return `<div class="empty small"><h2>הרשימה ריקה</h2><p>בדף של כל סדרה או סרט יש כפתור <b>＋ לצפות בהמשך</b>. מה ששומרים מופיע כאן ובדף הבית.</p></div>`;
  }
  return `<div class="muted ml-count">${count(data.watchlist.length, 'שמור אחד', 'שמורים')}</div><div class="pgrid">${data.watchlist.map(posterCard).join('')}</div>`;
}

function dayName(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return 'היום';
  if (days === 1) return 'אתמול';
  if (days < 7) return d.toLocaleDateString('he-IL', {weekday: 'long'});
  return d.toLocaleDateString('he-IL', {weekday: 'long', day: 'numeric', month: 'long'});
}

function myHistory(data) {
  const h = data.history;
  if (!h.length) return `<div class="empty small"><h2>עוד לא צפית בכלום</h2><p>מה שתצפה בו יופיע כאן לפי ימים.</p></div>`;
  let html = '', last = '';
  for (const x of h) {
    const day = dayName(x.at);
    if (day !== last) { html += `${last ? '</div>' : ''}<h3 class="hist-day">${esc(day)}</h3><div class="hist-list">`; last = day; }
    const p = x.completed ? 100 : x.duration ? Math.min(100, (x.position / x.duration) * 100) : 0;
    const ep = x.kind === 'series' && x.episode != null ? epLong(x) : (x.kind === 'movie' ? 'סרט' : '');
    const time = new Date(x.at * 1000).toLocaleTimeString('he-IL', {hour: '2-digit', minute: '2-digit'});
    html += `<a class="hist-item" href="#/watch/${x.media_id}">
      <div class="thumb">${img(x.pic)}<div class="bar"><i style="width:${p}%"></i></div></div>
      <div class="hist-info"><b>${esc(x.name)}</b><span>${esc(ep)}${ep ? ' · ' : ''}${x.completed ? '✓ נצפה' : `עצרת ב-${fmtTime(x.position)}`}</span></div>
      <span class="hist-time">${time}</span>
      <button class="iconbtn small" data-forget="${x.media_id}" title="הסר מההיסטוריה" aria-label="הסר מההיסטוריה">✕</button>
    </a>`;
  }
  return html + `</div><button class="btn danger" id="clear-history" style="margin-top:20px">מחק את כל ההיסטוריה</button>
    <p class="muted" style="font-size:12.5px">מחיקה מההיסטוריה לא משנה את ההתקדמות בצפייה (איפה עצרת ומה סומן כנצפה).</p>`;
}

function hoursText(sec) {
  const h = sec / 3600;
  if (h < 1) return `${Math.round(sec / 60)} דק׳`;
  return `${h < 10 ? h.toFixed(1).replace(/\.0$/, '') : Math.round(h)} שע׳`;
}

function myStats(s) {
  const box = (label, x) => `<div class="stat">
      <div class="stat-label">${label}</div><div class="stat-big">${hoursText(x.seconds)}</div>
      <div class="stat-sub">${count(x.episodes, 'פרק אחד', 'פרקים')} · ${count(x.movies, 'סרט אחד', 'סרטים')}</div></div>`;
  const max = Math.max(1, ...s.days.map((d) => d.seconds));
  const bars = s.days.map((d, i) => {
    const date = new Date(d.day * 1000);
    const label = i === s.days.length - 1 ? 'היום' : date.toLocaleDateString('he-IL', {weekday: 'narrow'});
    return `<div class="dbar" title="${esc(date.toLocaleDateString('he-IL', {weekday: 'long', day: 'numeric', month: 'numeric'}))}: ${hoursText(d.seconds)}">
      <span class="dval">${d.seconds >= 60 ? hoursText(d.seconds) : ''}</span>
      <i style="height:${(d.seconds / max) * 100}%"></i><span class="dlab">${label}</span></div>`;
  }).join('');
  const gmax = Math.max(1, ...s.genres.map((g) => g.seconds));
  return `<div class="stats-grid">${box('7 הימים האחרונים', s.week)}${box('30 הימים האחרונים', s.month)}${box('מאז ומעולם', s.all)}</div>
    <div class="panel"><h3>צפייה ב-14 הימים האחרונים</h3><div class="dbars">${bars}</div></div>
    ${s.genres.length ? `<div class="panel"><h3>הז'אנרים שאתה הכי צופה בהם</h3>${s.genres.map((g) => `<div class="gbar">
      <span>${esc(g.name)}</span><div><i style="width:${(g.seconds / gmax) * 100}%"></i></div><b>${hoursText(g.seconds)}</b></div>`).join('')}</div>` : ''}
    <p class="muted" style="font-size:12.5px">הזמנים בערך: לכל פרק וסרט נספר הזמן עד המקום שהגעת אליו. פרקים שרק סימנת כנצפו לא נספרים.</p>`;
}

function contCard(c) {
  const m = c.media;
  const p = c.state === 'next' ? 0 : pct({...m, position: c.position});
  const label = c.state === 'next' ? '<span class="badge accent">הפרק הבא</span>'
    : c.state === 'start' ? '<span class="badge">חדש</span>'
    : `<span>נשארו ${fmtTime((m.duration || 0) - c.position)}</span>`;
  const pic = c.title.backdrop || m.thumb || c.title.poster;
  return `<a class="cont-card" href="#/watch/${m.id}" data-tid="${c.title.id}" data-kind="${c.title.kind}" data-name="${esc(c.title.name)}">
    <div class="img"><div class="ph">${esc(c.title.name)}</div>${img(pic)}<div class="play">▶</div></div>
    <div class="bar"><i style="width:${p}%"></i></div>
    <div class="body">
      <div class="name-row"><div class="name">${esc(c.title.name)}</div><button class="cont-more" data-cont-menu aria-label="אפשרויות">⋮</button></div>
      <div class="meta"><span>${esc(epLong(m) || 'סרט')}</span>${label}</div>
    </div></a>`;
}

// "continue watching" card: ⋮ or a long press -> remove it from the row / go to its page
async function contMenu(card) {
  const series = card.dataset.kind === 'series';
  const v = await bottomSheet(card.dataset.name, [['hide', '✕ הסר מ"המשך צפייה"'], ['title', `ⓘ לדף ה${series ? 'סדרה' : 'סרט'}`]], '');
  if (v === 'title') { location.hash = `#/title/${card.dataset.tid}`; return; }
  if (v !== 'hide') return;
  try { await api('/api/continue/hide', {method: 'POST', body: {title_id: +card.dataset.tid}}); } catch (e) { alert(e.message); return; }
  card.remove();
  undoToast(`"${card.dataset.name}" הוסר מהמשך צפייה`, async () => {
    await api('/api/continue/hide', {method: 'POST', body: {title_id: +card.dataset.tid, hidden: false}}).catch(() => {});
    route();
  });
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-cont-menu]');
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  contMenu(b.closest('.cont-card'));
});
document.addEventListener('contextmenu', (e) => {
  const card = e.target.closest('.cont-card');
  if (!card) return;
  e.preventDefault();
  contMenu(card);
});

function posterCard(t) {
  const watchedAll = t.media_count && t.watched_count >= t.media_count;
  const p = t.media_count ? (t.watched_count / t.media_count) * 100 : 0;
  const sub = t.kind === 'series'
    ? `${t.season_count > 1 ? t.season_count + ' עונות · ' : ''}${count(t.media_count, 'פרק אחד', 'פרקים')}`
    : null;
  // numbers in a right-to-left line get reordered by the browser, so each one is isolated
  const movieSub = [t.year, t.duration ? fmtLen(t.duration) : ''].filter(Boolean).map((x) => `<bdi>${esc(x)}</bdi>`).join(' · ') || 'סרט';
  const corner = t.new_count ? `<span class="badge new corner">${t.new_count === 1 ? 'פרק חדש' : `${t.new_count} פרקים חדשים`}</span>`
    : watchedAll ? '<span class="badge ok corner">✓ נצפה</span>'
    : t.watched_count ? `<span class="badge accent corner">${t.kind === 'series' ? `${t.watched_count}/${t.media_count}` : 'בצפייה'}</span>` : '';
  return `<a class="poster-card" href="#/title/${t.id}">
    <div class="poster">
      <div class="ph">${esc(t.name)}</div>${img(t.poster)}
      ${t.rating ? `<span class="rate">⭐ ${t.rating}</span>` : ''}
      ${corner}
      ${p && !watchedAll ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}
    </div>
    <div class="name">${esc(t.name)}</div>
    <div class="sub">${sub === null ? movieSub : esc(sub)}</div></a>`;
}

// -------------------------------------------------------------------- title
let editMode = false;

async function pageTitle(id) {
  // re-draw after a change without jumping back to the top
  const reload = async () => { const y = window.scrollY; await pageTitle(id); window.scrollTo(0, y); };
  $app.innerHTML = '<div class="spinner"></div>';
  const data = await api(`/api/titles/${id}${editMode ? '?hidden=true' : ''}`);
  const {title: t, media, resume} = data;
  titleNewSince = data.new_since || null;
  const labels = data.season_labels || {};
  const isSeries = t.kind === 'series';

  const seasons = [...new Set(media.map((m) => m.season ?? 1))].sort((a, b) => a - b);
  const key = `season:${id}`;
  let season = +sessionStorage.getItem(key) || (resume ? (resume.media.season ?? 1) : seasons[0]);
  if (!seasons.includes(season)) season = seasons[0];

  let resumeLabel = '';
  if (resume) {
    const m = resume.media;
    resumeLabel = resume.state === 'resume' ? `המשך${isSeries ? ' · ' + epLabel(m) : ''} · ${fmtTime(resume.position)}`
      : resume.state === 'next' ? `המשך · ${epLabel(m)}`
      : resume.state === 'done' ? 'צפה שוב'
      : isSeries ? `התחל לצפות · ${epLabel(m)}` : 'צפה עכשיו';
  }
  const visible = media.filter((m) => !m.hidden);
  const total = visible.length;
  const genres = (t.genres || []).slice(0, 3).join(', ');
  const length = !isSeries && (t.runtime || visible[0]?.duration) ? fmtDur(t.runtime ? t.runtime * 60 : visible[0].duration) : '';
  const facts = [t.year, genres, isSeries ? `${seasons.length} עונות` : length].filter(Boolean).map((x) => `<bdi>${esc(x)}</bdi>`).join(' · ');
  const cast = (t.cast || []).slice(0, 10);

  $app.innerHTML = `
    <section class="hero ${t.backdrop ? 'has-bg' : ''}" ${t.backdrop ? '' : bg(t.backdrop)}>
      ${t.backdrop ? `<div class="hero-bg">${img(t.backdrop)}</div>` : ''}
      <a class="iconbtn hero-back" href="javascript:history.back()" aria-label="חזרה">→</a>
      <button class="iconbtn hero-menu" id="title-menu" aria-label="עוד">⋯</button>
      <div class="hero-inner">
        <div class="poster-img"><div class="ph">${esc(t.name)}</div>${img(t.poster)}</div>
        <div class="hero-text">
          <h1>${esc(t.name)}</h1>
          ${t.original_name && t.original_name !== t.name ? `<div class="orig">${esc(t.original_name)}</div>` : ''}
          <div class="facts">${facts}</div>
          <div class="chips-inline">
            ${t.rating ? `<span class="chip-s gold">⭐ ${t.rating} TMDB</span>` : ''}
            ${isSeries ? `<span class="chip-s">${count(total, 'פרק אחד', 'פרקים')}</span>` : ''}
          </div>
        </div>
      </div>
    </section>
    <div class="title-grid"><aside class="title-side">
    ${resume ? `<a class="btn primary wide" href="#/watch/${resume.media.id}">▶ ${esc(resumeLabel)}</a>` : ''}
    <button class="btn wide listbtn ${data.in_list ? 'on' : ''}" id="listbtn">${data.in_list ? '✓ ברשימה שלי' : '＋ לצפות בהמשך'}</button>
    ${t.overview ? `<p class="overview-text clamp" id="overview">${esc(t.overview)}</p>
      ${t.overview.length > 160 ? '<button class="linkbtn" id="more-overview">קרא עוד ▾</button>' : ''}` : ''}
    ${cast.length ? `<div class="cast">${cast.map((c) => `<div class="castm"><div class="avatar">${c.photo ? img(c.photo) : `<span>${esc((c.name || '?')[0])}</span>`}</div><div class="cn">${esc(c.name)}</div></div>`).join('')}</div>` : ''}
    </aside><section class="title-main">
    ${editMode ? editTitlePanel(t) : ''}
    ${isSeries && seasons.length > 1 ? `<nav class="season-tabs">
        ${seasons.map((s) => `<button class="stab ${s === season ? 'active' : ''}" data-season="${s}">עונה ${s}${labels[s] ? `<small>${esc(labels[s])}</small>` : ''}</button>`).join('')}
      </nav>` : ''}
    ${isSeries ? '<div class="season-bar" id="season-bar"></div>' : ''}
    <div class="episodes" id="eps"></div>
    </section></div>`;

  $app.querySelector('#listbtn').onclick = async (e) => {
    const b = e.currentTarget, on = !b.classList.contains('on');
    try {
      await api(`/api/watchlist/${t.id}`, {method: 'POST', body: {on}});
      b.classList.toggle('on', on);
      b.textContent = on ? '✓ ברשימה שלי' : '＋ לצפות בהמשך';
      libraryCache = null;
    } catch (err) { alert(err.message); }
  };

  const seasonList = () => (isSeries ? media.filter((m) => (m.season ?? 1) === season && !m.hidden) : visible);
  const renderSeasonBar = () => {
    const bar = $app.querySelector('#season-bar');
    if (!bar) return;
    const list = seasonList(), done = list.filter((m) => m.completed).length, all = list.length && done === list.length;
    bar.innerHTML = `<span>עונה ${season} · ${done}/${list.length} נצפו</span>
      <button class="linkbtn" id="mark-season">${all ? '↺ בטל סימון עונה' : '✓ סמן עונה כנצפתה'}</button>`;
    bar.querySelector('#mark-season').onclick = () => markMany(list.map((m) => m.id), !all, `עונה ${season}`);
  };
  async function markMany(ids, watched, what) {
    try {
      const r = await api('/api/watched-many', {method: 'POST', body: {ids, watched}});
      libraryCache = null;
      await reload();
      undoToast(`${what} ${watched ? 'סומנה כנצפתה' : 'סומנה כלא נצפתה'}`, async () => {
        const back = Object.entries(r.before);
        await api('/api/watched-many', {method: 'POST', body: {ids: back.filter(([, w]) => w).map(([i]) => +i), watched: true}});
        await api('/api/watched-many', {method: 'POST', body: {ids: back.filter(([, w]) => !w).map(([i]) => +i), watched: false}});
        reload();
      });
    } catch (e) { alert(e.message); }
  }

  const renderEps = () => {
    const list = isSeries ? media.filter((m) => (m.season ?? 1) === season) : media;
    const seen = {};
    $app.querySelector('#eps').innerHTML = list.map((m) => {
      const k = `${m.season}:${m.episode}`;
      seen[k] = (seen[k] || 0) + 1;
      return episodeRow(m, isSeries, seen[k] > 1 ? seen[k] : 0, resume && resume.media.id === m.id);
    }).join('');
    renderSeasonBar();
  };
  renderEps();

  $app.querySelectorAll('[data-season]').forEach((b) => b.onclick = () => {
    season = +b.dataset.season;
    sessionStorage.setItem(key, season);
    $app.querySelectorAll('[data-season]').forEach((x) => x.classList.toggle('active', x === b));
    b.scrollIntoView({behavior: 'smooth', inline: 'center', block: 'nearest'});
    renderEps();
    const eps = $app.querySelector('#eps');
    eps.classList.remove('fade-in'); void eps.offsetWidth; eps.classList.add('fade-in');
  });
  $app.querySelector('#more-overview')?.addEventListener('click', (e) => {
    $app.querySelector('#overview').classList.toggle('clamp');
    e.target.textContent = $app.querySelector('#overview').classList.contains('clamp') ? 'קרא עוד ▾' : 'הצג פחות ▴';
  });
  $app.querySelector('#title-menu').onclick = async () => {
    const opts = [['edit', editMode ? '✓ סיום עריכה' : '✏️ עריכת פרטים ופרקים'],
                  ['all', '✓ סמן הכל כנצפה'], ['reset', '↺ אפס התקדמות']];
    opts.push(['hide', data.hidden_title ? '👁 החזר לספרייה' : `🙈 הסתר את ה${isSeries ? 'סדרה' : 'סרט'} מהספרייה`]);
    if (data.ep_names) opts.push(['names', data.ep_names_off ? '🏷 הצג שמות ותקצירי פרקים מ-TMDB' : '🏷 הסתר שמות פרקים מ-TMDB (אם הם לא מתאימים)']);
    const v = await bottomSheet(t.name, opts, '');
    if (v === 'hide') {
      const hide = !data.hidden_title;
      try { await api(`/api/titles/${t.id}`, {method: 'PATCH', body: {hidden_title: hide}}); } catch (e) { alert(e.message); return; }
      libraryCache = null;
      if (!hide) { reload(); return; }
      undoToast(`"${t.name}" הוסתר. אפשר להחזיר בהגדרות ← מוסתרים`, async () => {
        await api(`/api/titles/${t.id}`, {method: 'PATCH', body: {hidden_title: false}}).catch(() => {});
        libraryCache = null;
      }, 7000);
      if (route.cameFrom) history.back(); else location.hash = '#/';
      return;
    }
    if (v === 'names') {
      try { await api(`/api/titles/${t.id}`, {method: 'PATCH', body: {ep_names_off: !data.ep_names_off}}); } catch (e) { alert(e.message); return; }
      reload();
      return;
    }
    if (v === 'edit') { editMode = !editMode; pageTitle(id); }
    else if (v === 'all') markMany(visible.map((m) => m.id), true, isSeries ? 'הסדרה' : 'הסרט');
    else if (v === 'reset') markMany(visible.map((m) => m.id), false, isSeries ? 'הסדרה' : 'הסרט');
  };
  // long press on an episode: mark everything up to it as watched
  $app.querySelector('#eps').addEventListener('contextmenu', async (e) => {
    const row = e.target.closest('.ep');
    if (!row || !isSeries || editMode) return;
    e.preventDefault();
    const list = seasonList();
    const idx = list.findIndex((m) => String(m.id) === row.dataset.id);
    if (idx < 0) return;
    const v = await bottomSheet(`פרק ${list[idx].episode}`, [['upto', `✓ סמן עד כאן כנצפה (${count(idx + 1, 'פרק אחד', 'פרקים')})`]], '');
    if (v === 'upto') markMany(list.slice(0, idx + 1).map((m) => m.id), true, `עד פרק ${list[idx].episode}`);
  });

  // episode actions (event delegation)
  $app.querySelector('#eps').onclick = async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    e.preventDefault();
    const mid = +btn.dataset.id;
    const row = btn.closest('.ep');
    try {
      if (btn.dataset.act === 'watched') {
        await api(`/api/watched/${mid}`, {method: 'POST', body: {watched: !btn.classList.contains('on')}});
      } else if (btn.dataset.act === 'save') {
        const val = (n) => row.querySelector(`[name=${n}]`).value;
        await api(`/api/media/${mid}`, {method: 'PATCH', body: {
          season: val('season'), episode: val('episode'),
          move_to_name: val('move').trim() || undefined,
          kind: row.querySelector('[name=kind]')?.value,
        }});
      } else if (btn.dataset.act === 'hide') {
        await api(`/api/media/${mid}`, {method: 'PATCH', body: {hidden: btn.dataset.hidden !== '1'}});
      }
      libraryCache = null;
      reload();
    } catch (err) { alert(err.message); }
  };

  if (editMode) bindEditTitle(t);
}

function episodeRow(m, isSeries, version, current) {
  const p = pct(m);
  const isEp = isSeries && m.episode != null;
  const title = isEp ? `פרק ${m.episode}${m.ep_name ? ' · ' + m.ep_name : ''}` : (firstLine(m.caption) || m.file_name || 'וידאו');
  // like a streaming app: length and quality; the file's own caption/size only while editing
  const desc = [editMode || !isEp ? firstLine(m.caption) || m.file_name : '', fmtDur(m.duration), m.quality ? m.quality + 'p' : '',
                editMode ? fmtSize(m.size) : '', !editMode && m.versions > 1 ? `${m.versions} גרסאות` : '']
    .filter((x, i) => x && !(i === 0 && x === title)).map(esc).join(' · ');
  const state = m.completed ? '<span class="badge ok">נצפה</span>'
    : m.position >= 10 ? `<span class="badge accent">עצרת ב-${fmtTime(m.position)}</span>`
    : titleNewSince && m.added_at > titleNewSince ? '<span class="badge new">חדש</span>' : '';
  return `<div class="ep ${current ? 'current' : ''} ${m.hidden ? 'is-hidden' : ''}" data-id="${m.id}">
    <a class="num" href="#/watch/${m.id}">${isSeries && m.episode != null ? m.episode : '▶'}</a>
    <a class="thumb" href="#/watch/${m.id}">${img(m.ep_still || m.thumb)}${p ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}</a>
    <a class="info" href="#/watch/${m.id}">
      <div class="t">${esc(title)} ${version ? `<span class="badge">גרסה ${version}</span>` : ''} ${state}</div>
      <div class="c">${desc}</div>
      ${m.ep_overview && !editMode ? `<div class="ep-ov">${esc(m.ep_overview)}</div>` : ''}
    </a>
    <div class="side">
      <button class="check ${m.completed ? 'on' : ''}" title="${m.completed ? 'סמן כלא נצפה' : 'סמן כנצפה'}" data-act="watched" data-id="${m.id}">✓</button>
    </div>
    ${editMode ? `<div class="edit-row">
      <label class="field">עונה<input class="input num" name="season" value="${m.season ?? ''}"></label>
      <label class="field">פרק<input class="input num" name="episode" value="${m.episode ?? ''}"></label>
      <label class="field" style="flex:1;min-width:160px">העבר לסדרה/סרט אחר (שם)<input class="input" name="move" placeholder="השאר ריק כדי לא להעביר"></label>
      <label class="field">סוג<select class="input" name="kind"><option value="series" ${isSeries ? 'selected' : ''}>סדרה</option><option value="movie" ${isSeries ? '' : 'selected'}>סרט</option></select></label>
      <button class="btn small primary" data-act="save" data-id="${m.id}">שמור</button>
      <button class="btn small" data-act="hide" data-id="${m.id}" data-hidden="${m.hidden ? 1 : 0}">${m.hidden ? 'הצג' : 'הסתר'}</button>
      <span class="muted" style="font-size:12px;direction:ltr">${esc(m.file_name || '')}</span>
    </div>` : ''}
  </div>`;
}

function editTitlePanel(t) {
  return `<div class="panel" id="edit-title">
    <h3>עריכת פרטים</h3>
    <div class="form-grid">
      <label class="field">שם<input class="input" name="name" value="${esc(t.name)}"></label>
      <label class="field">סוג<select class="input" name="kind">
        <option value="series" ${t.kind === 'series' ? 'selected' : ''}>סדרה</option>
        <option value="movie" ${t.kind === 'movie' ? 'selected' : ''}>סרט</option></select></label>
      <label class="field">שנה<input class="input" name="year" value="${t.year ?? ''}"></label>
      <label class="field">מזהה TMDB (מספר או קישור)<input class="input" name="tmdb_id" placeholder="${t.tmdb_id ?? ''}"></label>
      <label class="field">קישור לפוסטר<input class="input" name="poster" value="${esc(t.poster && !t.poster.startsWith('/api') ? t.poster : '')}"></label>
    </div>
    <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn primary small" id="save-title">שמור</button>
      <button class="btn small" id="refresh-tmdb">חפש שוב ב-TMDB</button>
      <input class="input" id="merge-q" placeholder="איחוד עם... (הקלד שם)" style="flex:1;min-width:160px">
      <select class="input" id="merge-into" hidden></select>
      <button class="btn small" id="merge">אחד</button>
    </div></div>`;
}

function bindEditTitle(t) {
  const panel = $app.querySelector('#edit-title');
  const val = (n) => panel.querySelector(`[name=${n}]`).value;
  const done = (id) => { libraryCache = null; location.hash = `#/title/${id}`; pageTitle(id); };
  panel.querySelector('#save-title').onclick = async () => {
    try {
      await api(`/api/titles/${t.id}`, {method: 'PATCH', body: {
        name: val('name'), kind: val('kind'), year: val('year'), tmdb_id: val('tmdb_id') || undefined, poster: val('poster'),
      }});
      done(t.id);
    } catch (e) { alert(e.message); }
  };
  panel.querySelector('#refresh-tmdb').onclick = async () => {
    try { await api(`/api/titles/${t.id}`, {method: 'PATCH', body: {refresh_tmdb: true}}); done(t.id); } catch (e) { alert(e.message); }
  };
  const mq = panel.querySelector('#merge-q'), sel = panel.querySelector('#merge-into');
  let mt = null;
  mq.oninput = () => {
    clearTimeout(mt);
    mt = setTimeout(async () => {
      const q = mq.value.trim();
      if (!q) { sel.hidden = true; return; }
      const res = (await api(`/api/library?q=${encodeURIComponent(q)}&limit=30`)).titles.filter((o) => o.id !== t.id);
      sel.innerHTML = res.length
        ? res.map((o) => `<option value="${o.id}">${esc(o.name)} (${o.kind === 'series' ? 'סדרה' : 'סרט'}${o.year ? ', ' + o.year : ''})</option>`).join('')
        : '<option value="">לא נמצא</option>';
      sel.hidden = false;
    }, 300);
  };
  panel.querySelector('#merge').onclick = async () => {
    const into = +panel.querySelector('#merge-into').value;
    if (!into || !confirm('לאחד את כל הפרקים לתוך הכותר שנבחר?')) return;
    try { await api(`/api/titles/${t.id}/merge`, {method: 'POST', body: {into}}); done(into); } catch (e) { alert(e.message); }
  };
}

// -------------------------------------------------------------------- watch
async function pageWatch(id) {
  $app.innerHTML = '<div class="spinner"></div>';
  const {media: m, title: t, prev, next, versions, cast_url: castUrl} = await api(`/api/media/${id}`);
  const isSeries = t.kind === 'series' && m.episode != null;
  document.title = `${t.name}${isSeries ? ' · ' + epLabel(m) : ''}`;
  const sub = isSeries ? epLong(m) + (m.ep_name ? ' · ' + m.ep_name : '') : '';

  $app.innerHTML = `<div class="watch-layout"><div class="watch">
    <div class="player-wrap show-ui paused" id="pw">
      <video id="v" playsinline autopictureinpicture preload="metadata" src="/api/stream/${m.id}"></video>
      <div class="pl-spin"><div class="spinner"></div></div>
      <div class="pl-castnote">📺 משודר לטלוויזיה</div>
      <div class="pl-flash" id="flash"></div>
      <button class="pl-big" data-a="play" aria-label="נגן">▶</button>
      <div class="pl-top">
        <button class="pl-btn" data-a="back" aria-label="חזרה">→</button>
        <div class="pl-name"><b>${esc(t.name)}</b>${sub ? `<span>${esc(sub)}</span>` : ''}</div>
      </div>
      <button class="pl-pill pl-skip" id="skip" hidden>⏭ דלג על הפתיחה</button>
      ${next ? `<a class="pl-pill pl-next" id="nextpill" href="#/watch/${next.id}" hidden>הפרק הבא · ${esc(next.ep_name || epLabel(next))} ⏭</a>` : ''}
      <div class="pl-bar">
        <div class="pl-seek" id="seek"><div class="pl-track"><i class="pl-buf"></i><i class="pl-played"></i></div><i class="pl-knob"></i><span class="pl-tip"></span></div>
        <div class="pl-row">
          <button class="pl-btn" data-a="play" id="playbtn" aria-label="נגן/עצור">▶</button>
          <button class="pl-btn" data-a="back10" aria-label="10 שניות אחורה">⟲</button>
          <button class="pl-btn" data-a="fwd10" aria-label="10 שניות קדימה">⟳</button>
          ${next ? '<button class="pl-btn" data-a="next" aria-label="הפרק הבא">⏭</button>' : ''}
          <div class="pl-vol"><button class="pl-btn" data-a="mute" id="mutebtn" aria-label="השתק">🔊</button><input type="range" id="vol" min="0" max="1" step="0.05"></div>
          <span class="pl-time" id="time">0:00 / 0:00</span>
          <span class="sp"></span>
          <button class="pl-btn pl-txt" data-a="sleep" id="sleepbtn" title="שעון שינה">🌙</button>
          <button class="pl-btn pl-txt" data-a="speed" id="speedbtn" title="מהירות">1x</button>
          ${isSeries ? '<button class="pl-btn" data-a="intro" title="הגדרת הפתיחה של הסדרה">⚙</button>' : ''}
          ${'remote' in HTMLMediaElement.prototype ? `<button class="pl-btn pl-cast" data-a="cast" aria-label="שידור לטלוויזיה" title="שידור לטלוויזיה"><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M1 18v3h3c0-1.66-1.34-3-3-3zm0-4v2c2.76 0 5 2.24 5 5h2c0-3.87-3.13-7-7-7zm0-4v2c4.97 0 9 4.03 9 9h2c0-6.08-4.93-11-11-11zm20-7H3c-1.1 0-2 .9-2 2v3h2V5h18v14h-7v2h7c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2z"/></svg></button>` : ''}
          ${document.pictureInPictureEnabled ? '<button class="pl-btn" data-a="pip" aria-label="חלון צף" title="חלון צף">⧉</button>' : ''}
          <button class="pl-btn" data-a="fs" aria-label="מסך מלא">⛶</button>
        </div>
      </div>
    </div>
    <div class="watch-head">
      <div>
        <h1><a href="#/title/${t.id}">${esc(t.name)}</a></h1>
        <div class="muted">${esc(sub)}</div>
      </div>
      <div class="watch-controls">
        ${prev ? `<a class="btn" href="#/watch/${prev.id}" title="פרק קודם">→ ${esc(epLabel(prev) || 'הקודם')}</a>` : ''}
        ${next ? `<a class="btn primary" href="#/watch/${next.id}" title="הפרק הבא (N)">${esc(epLabel(next) || 'הבא')} ←</a>` : ''}
      </div>
    </div>
    ${versions && versions.length > 1 ? `<div class="versions"><span class="muted">איכות:</span>
      ${versions.map((x) => `<a class="tab ${x.id === m.id ? 'active' : ''}" href="#/watch/${x.id}">${x.quality ? x.quality + 'p' : '?'} · ${fmtSize(x.size)}</a>`).join('')}
    </div>` : ''}
    <p class="muted kbd-help">קיצורי מקלדת: רווח = עצור/נגן · חיצים = 10 שניות · ↑↓ = עוצמה · M = השתק · F = מסך מלא · S = דלג על הפתיחה · N = הפרק הבא</p>
  </div>${isSeries ? '<aside class="watch-side" id="wside"></aside>' : ''}</div>`;
  if (isSeries) {  // computer: the season's episodes next to the player
    api(`/api/titles/${t.id}`).then(({media}) => {
      const side = $app.querySelector('#wside');
      if (!side) return;
      const list = media.filter((x) => (x.season ?? 1) === (m.season ?? 1));
      side.innerHTML = `<h3>${esc(t.name)}</h3><div class="muted" style="margin-bottom:12px">עונה ${m.season ?? 1} · ${count(list.length, 'פרק אחד', 'פרקים')}</div>` +
        list.map((x) => `<a class="side-ep ${x.episode === m.episode ? 'on' : ''}" href="#/watch/${x.id}">
          <div class="thumb">${img(x.ep_still || x.thumb)}${pct(x) ? `<div class="bar"><i style="width:${pct(x)}%"></i></div>` : ''}</div>
          <div><b>פרק ${x.episode}</b>${x.ep_name ? `<div class="side-name">${esc(x.ep_name)}</div>` : ''}<div class="muted">${x.episode === m.episode ? 'מתנגן עכשיו' : x.completed ? '✓ נצפה' : fmtDur(x.duration)}</div></div></a>`).join('');
      side.querySelector('.side-ep.on')?.scrollIntoView({block: 'center'});
    }).catch(() => {});
  }

  const v = $app.querySelector('#v');
  const wrap = $app.querySelector('#pw');
  const q = (s) => wrap.querySelector(s);
  let lastSaved = -1, timer = null, nextTimer = null, finished = false, hideTimer = null, tick = null;
  let introDone = false, genericSkipUsed = false;

  // ---- saving progress
  const payload = () => ({position: v.currentTime || 0, duration: isFinite(v.duration) ? v.duration : null});
  const save = (force) => {
    if (!v.currentTime || (!force && Math.abs(v.currentTime - lastSaved) < 3)) return;
    lastSaved = v.currentTime;
    api(`/api/progress/${m.id}`, {method: 'POST', body: payload()}).catch(() => {});
  };
  const beacon = () => {
    if (!v.currentTime) return;
    navigator.sendBeacon(`/api/progress/${m.id}`, new Blob([JSON.stringify(payload())], {type: 'application/json'}));
  };

  // ---- small helpers
  const toast = (html, ms = 5000) => {
    wrap.querySelectorAll('.toast').forEach((x) => x.remove());
    const el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = html;
    wrap.appendChild(el);
    if (ms) setTimeout(() => el.remove(), ms);
    return el;
  };
  const flash = (text) => {  // the big "+10" / "⏸" bubble in the middle
    const f = q('#flash');
    f.textContent = text;
    f.classList.remove('on'); void f.offsetWidth; f.classList.add('on');
  };
  const seek = (sec) => {
    if (!isFinite(v.duration)) return;
    v.currentTime = Math.max(0, Math.min(v.duration - 0.5, sec));
  };
  const togglePlay = () => { if (v.paused) v.play().catch(() => {}); else v.pause(); };

  // ---- show / hide the controls
  // phone/touch: the controls stay until you tap the picture again; computer: they hide after 3 s without the mouse
  let touchMode = matchMedia('(hover: none)').matches;
  wrap.addEventListener('pointerdown', (e) => { touchMode = e.pointerType !== 'mouse'; }, true);
  const showUI = () => {
    wrap.classList.add('show-ui');
    clearTimeout(hideTimer);
    if (!v.paused && !touchMode) hideTimer = setTimeout(() => wrap.classList.remove('show-ui'), 3000);
  };
  wrap.addEventListener('mousemove', () => { if (!touchMode) showUI(); });  // phones send a fake mouse move with every tap
  wrap.addEventListener('mouseleave', () => { if (!v.paused && !touchMode) wrap.classList.remove('show-ui'); });

  // tap/click on the picture: computer = play/pause, phone = show controls; double = ±10 s (sides) or full screen
  let lastTap = 0, tapTimer = null;
  v.addEventListener('pointerup', (e) => {
    const touch = e.pointerType !== 'mouse';
    const now = Date.now();
    const r = v.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    if (now - lastTap < 300) {  // double tap / double click
      clearTimeout(tapTimer);
      lastTap = 0;
      if (touch && x < 0.35) { seek(v.currentTime - 10); flash('⟲ 10'); }
      else if (touch && x > 0.65) { seek(v.currentTime + 10); flash('10 ⟳'); }
      else toggleFs();
      return;
    }
    lastTap = now;
    tapTimer = setTimeout(() => {
      if (touch) { if (wrap.classList.contains('show-ui') && !v.paused) wrap.classList.remove('show-ui'); else showUI(); }
      else { togglePlay(); flash(v.paused ? '⏸' : '▶'); }
    }, touch ? 250 : 200);
  });

  // ---- full screen: the whole page goes full screen, so moving to the next episode stays full screen
  const toggleFs = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else {
      const el = document.documentElement;
      (el.requestFullscreen ? el.requestFullscreen() : Promise.reject()).then(() => {
        screen.orientation?.lock?.('landscape').catch(() => {});
      }).catch(() => v.webkitEnterFullscreen?.());
    }
  };

  // ---- volume & speed (remembered)
  const volEl = q('#vol');
  v.volume = +store.get('player:vol', 1);
  v.muted = !!store.get('player:muted', false);
  v.playbackRate = +(sessionStorage.getItem('player:rate') || 1);
  const drawVol = () => {
    volEl.value = v.muted ? 0 : v.volume;
    q('#mutebtn').textContent = v.muted || v.volume === 0 ? '🔇' : v.volume < 0.5 ? '🔉' : '🔊';
    volEl.style.setProperty('--p', `${(v.muted ? 0 : v.volume) * 100}%`);
  };
  v.addEventListener('volumechange', () => { drawVol(); store.set('player:vol', v.volume); store.set('player:muted', v.muted); });
  volEl.addEventListener('input', () => { v.volume = +volEl.value; v.muted = v.volume === 0; });
  drawVol();
  const drawRate = () => { q('#speedbtn').textContent = `${v.playbackRate}x`; };
  v.addEventListener('ratechange', () => { sessionStorage.setItem('player:rate', v.playbackRate); drawRate(); });
  drawRate();

  // ---- the time line
  const seekEl = q('#seek');
  let dragging = false;
  const ratioAt = (e) => { const r = seekEl.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
  const drawTime = (cur = v.currentTime) => {
    const d = isFinite(v.duration) ? v.duration : (m.duration || 0);
    const p = d ? (cur / d) * 100 : 0;
    seekEl.style.setProperty('--p', `${p}%`);
    q('#time').textContent = `${fmtTime(cur)} / ${fmtTime(d)}`;
    if (v.buffered.length && d) {
      let end = 0;
      for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= cur + 1) end = Math.max(end, v.buffered.end(i));
      seekEl.style.setProperty('--b', `${(end / d) * 100}%`);
    }
  };
  seekEl.addEventListener('pointerdown', (e) => {
    dragging = true; seekEl.setPointerCapture(e.pointerId); seekEl.classList.add('drag');
    drawTime(ratioAt(e) * (v.duration || 0));
  });
  seekEl.addEventListener('pointermove', (e) => {
    const d = v.duration || 0;
    const tip = q('.pl-tip');
    tip.textContent = fmtTime(ratioAt(e) * d);
    tip.style.left = `${ratioAt(e) * 100}%`;
    if (dragging) { drawTime(ratioAt(e) * d); showUI(); }
  });
  seekEl.addEventListener('pointerup', (e) => {
    if (!dragging) return;
    dragging = false; seekEl.classList.remove('drag');
    seek(ratioAt(e) * (v.duration || 0));
  });

  // ---- skip the opening (per series: set once with ⚙, or a quick 85-second jump)
  const hasIntro = () => t.intro_end != null;
  const inIntro = (ct) => hasIntro() && ct >= (t.intro_start || 0) && ct < t.intro_end - 1;
  const skipIntro = () => {
    const from = v.currentTime;
    if (hasIntro()) { seek(t.intro_end); introDone = true; }
    else { seek(from + 85); genericSkipUsed = true; }
    const el = toast(`דילגתי על הפתיחה <button class="btn small">↶ בטל</button>`);
    el.querySelector('button').onclick = () => { seek(from); el.remove(); };
  };
  q('#skip').onclick = (e) => { e.stopPropagation(); skipIntro(); };
  const autoSkip = () => !!store.get(`intro-auto:${t.id}`, false);

  // ---- sleep timer (keeps counting across episodes)
  const sleep = () => { try { return JSON.parse(sessionStorage.getItem('player:sleep') || 'null'); } catch (_) { return null; } };
  const setSleep = (s) => { if (s) sessionStorage.setItem('player:sleep', JSON.stringify(s)); else sessionStorage.removeItem('player:sleep'); drawSleep(); };
  const drawSleep = () => {
    const s = sleep(), b = q('#sleepbtn');
    b.classList.toggle('on', !!s);
    b.textContent = !s ? '🌙' : s.ep ? '🌙 סוף פרק' : `🌙 ${Math.max(1, Math.ceil((s.until - Date.now()) / 60000))} ד׳`;
  };
  const sleepNow = () => {
    setSleep(null);
    const vol = v.volume;
    let i = 0;
    const fade = setInterval(() => {  // fade out gently instead of a sudden stop
      i += 1;
      v.volume = Math.max(0, vol * (1 - i / 16));
      if (i >= 16) {
        clearInterval(fade);
        v.pause();
        v.volume = vol;
        const o = document.createElement('div');
        o.className = 'overlay';
        o.innerHTML = `<div class="box"><div style="font-size:40px">💤</div><h2>לילה טוב</h2>
          <p class="muted">שעון השינה עצר את הצפייה. המקום נשמר.</p><button class="btn primary">המשך לצפות</button></div>`;
        o.querySelector('button').onclick = () => { o.remove(); v.play(); };
        wrap.appendChild(o);
      }
    }, 500);
  };
  drawSleep();

  // ---- lock screen / notification: name, picture and buttons (play, pause, ±10 s, previous/next episode)
  const ms = navigator.mediaSession;
  const msSet = (action, fn) => { try { ms.setActionHandler(action, fn); } catch (_) {} };
  if (ms) {
    const pic = t.poster || m.ep_still || m.thumb;
    try {
      ms.metadata = new MediaMetadata({
        title: isSeries ? (m.ep_name ? `${epLabel(m)} · ${m.ep_name}` : epLong(m)) : t.name,
        artist: isSeries ? t.name : (t.year ? String(t.year) : ''),
        album: 'Streamora',
        artwork: pic ? [{src: new URL(pic, location.href).href, sizes: '342x513', type: 'image/jpeg'}] : [],
      });
    } catch (_) {}
    msSet('play', () => v.play().catch(() => {}));
    msSet('pause', () => v.pause());
    msSet('seekbackward', (d) => seek(v.currentTime - (d.seekOffset || 10)));
    msSet('seekforward', (d) => seek(v.currentTime + (d.seekOffset || 10)));
    msSet('seekto', (d) => seek(d.seekTime));
    msSet('previoustrack', prev ? () => { location.hash = `#/watch/${prev.id}`; } : null);
    msSet('nexttrack', next ? () => { location.hash = `#/watch/${next.id}`; } : null);
  }
  // casting: show it on the player
  if (v.remote) {
    const castState = () => {
      const on = v.remote.state !== 'disconnected';
      wrap.classList.toggle('casting', on);
      wrap.querySelector('.pl-cast')?.classList.toggle('on', on);
    };
    v.remote.onconnecting = castState;
    v.remote.onconnect = () => { castState(); toast('📺 משודר לטלוויזיה. הפלאפון הוא השלט', 4000); };
    v.remote.ondisconnect = castState;
  }
  const msPosition = () => {
    if (!ms || !ms.setPositionState || !isFinite(v.duration) || !v.duration) return;
    try { ms.setPositionState({duration: v.duration, playbackRate: v.playbackRate, position: Math.min(v.currentTime, v.duration)}); } catch (_) {}
  };

  // ---- every second: the time line, skip button, next-episode button, sleep timer
  tick = setInterval(() => {
    if (!dragging) drawTime();
    msPosition();
    const ct = v.currentTime, d = v.duration;
    const showSkip = isSeries && !v.paused && (hasIntro() ? inIntro(ct) && !introDone : !genericSkipUsed && ct > 3 && ct < 240);
    if (isSeries && hasIntro() && inIntro(ct) && !introDone && autoSkip() && !v.paused) skipIntro();
    q('#skip').hidden = !showSkip;
    const np = q('#nextpill');
    if (np) np.hidden = !(isFinite(d) && d - ct < 30 && d > 120 && !(sleep() || {}).ep);
    const s = sleep();
    if (s && s.until) { drawSleep(); if (Date.now() >= s.until && !v.paused) sleepNow(); }
  }, 500);

  // ---- resume where we stopped
  v.addEventListener('loadedmetadata', () => {
    const pos = m.position || 0;
    if (!m.completed && pos > 10 && pos < v.duration - 20) {
      v.currentTime = pos;
      const el = toast(`ממשיך מ-${fmtTime(pos)} <button class="btn small">מההתחלה</button>`, 8000);
      el.querySelector('button').onclick = () => { v.currentTime = 0; el.remove(); v.play(); };
    }
    drawTime();
    v.play().catch(() => {});
  }, {once: true});

  const drawPlay = () => {
    wrap.classList.toggle('paused', v.paused);
    q('#playbtn').textContent = v.paused ? '▶' : '⏸';
    if (v.paused) { wrap.classList.add('show-ui'); clearTimeout(hideTimer); } else showUI();
  };
  v.addEventListener('play', () => { drawPlay(); timer = timer || setInterval(() => save(false), 10000); });
  v.addEventListener('pause', () => { drawPlay(); save(true); });
  v.addEventListener('waiting', () => wrap.classList.add('loading'));
  ['playing', 'canplay', 'seeked'].forEach((ev) => v.addEventListener(ev, () => wrap.classList.remove('loading')));
  v.addEventListener('seeked', () => { save(true); if (!inIntro(v.currentTime)) introDone = false; });
  v.addEventListener('progress', () => drawTime());
  v.addEventListener('ended', () => {
    finished = true;
    api(`/api/progress/${m.id}`, {method: 'POST', body: {position: v.duration, duration: v.duration}}).catch(() => {});
    if ((sleep() || {}).ep) { sleepNow(); return; }
    if (next) showNextOverlay();
  });
  v.addEventListener('error', () => {
    const code = v.error && v.error.code;
    const msg = code === 4
      ? 'הדפדפן לא מצליח לנגן את הקובץ הזה (כנראה פורמט MKV/HEVC). נסה בכרום או אדג׳, או הורד את הקובץ ופתח ב-VLC.'
      : 'שגיאה בטעינת הסרטון. בדוק שהשרת מחובר לטלגרם (בהגדרות).';
    const o = document.createElement('div');
    o.className = 'overlay';
    o.innerHTML = `<div class="box"><p>${msg}</p><a class="btn" href="/api/stream/${m.id}" download="${esc(m.file_name || 'video')}">⬇ הורדה</a></div>`;
    wrap.appendChild(o);
  });

  function showNextOverlay() {
    let n = 10;
    const o = document.createElement('div');
    o.className = 'overlay next-overlay';
    const pic = next.ep_still || next.thumb;
    o.innerHTML = `<div class="box"><div class="muted">הפרק הבא מתחיל בעוד</div>
      <div class="count" id="cd">${n}</div>
      ${pic ? `<div class="next-pic">${img(pic)}</div>` : ''}
      <h2>${esc(epLong(next))}${next.ep_name ? ` · ${esc(next.ep_name)}` : ''}</h2>
      ${next.ep_overview ? `<p class="next-ov">${esc(next.ep_overview)}</p>` : ''}
      <div style="display:flex;gap:8px;justify-content:center;margin-top:12px">
        <a class="btn primary" href="#/watch/${next.id}">▶ נגן עכשיו</a>
        <button class="btn" id="cancel-next">ביטול</button></div></div>`;
    wrap.appendChild(o);
    o.querySelector('#cancel-next').onclick = () => { clearInterval(nextTimer); o.remove(); };
    nextTimer = setInterval(() => {
      n -= 1;
      const cd = o.querySelector('#cd');
      if (cd) cd.textContent = n;
      if (n <= 0) { clearInterval(nextTimer); location.hash = `#/watch/${next.id}`; }
    }, 1000);
  }

  // ---- buttons
  const actions = {
    play: () => togglePlay(),
    back: () => { if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); location.hash = `#/title/${t.id}`; },
    back10: () => { seek(v.currentTime - 10); flash('⟲ 10'); },
    fwd10: () => { seek(v.currentTime + 10); flash('10 ⟳'); },
    next: () => { location.hash = `#/watch/${next.id}`; },
    mute: () => { v.muted = !v.muted; if (!v.muted && v.volume === 0) v.volume = 0.5; },
    fs: () => toggleFs(),
    cast: () => {  // send the video to the TV (Chromecast); the TV fetches it straight from this site over the Wi-Fi
      if (!castUrl) { toast('כדי לשדר לטלוויזיה, הפלאפון צריך להיות מחובר ל-Wi-Fi של הבית', 4000); return; }
      if (v.remote && v.remote.state !== 'disconnected') { v.remote.prompt().catch(() => {}); return; }
      if (!v.src.includes('?t=')) {  // an address the TV can open (with a one-time key instead of your login)
        const at = v.currentTime, playing = !v.paused;
        v.src = castUrl;
        v.addEventListener('loadedmetadata', () => { v.currentTime = at; if (playing) v.play().catch(() => {}); }, {once: true});
      }
      v.remote.prompt().catch((e) => {
        if (e && e.name === 'NotFoundError') toast('לא נמצאה טלוויזיה לשידור. בדוק שהטלוויזיה דלוקה ובאותה רשת Wi-Fi', 5000);
        else if (e && e.name !== 'AbortError') toast('השידור לא זמין כרגע בדפדפן הזה', 4000);
      });
    },
    pip: () => {  // small floating window that stays on top of other apps
      if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
      else v.requestPictureInPicture().catch(() => toast('החלון הצף לא זמין כרגע בדפדפן הזה', 3000));
    },
    speed: async () => {
      const r = await bottomSheet('מהירות ניגון', [0.5, 0.75, 1, 1.25, 1.5, 2].map((x) => [String(x), x === 1 ? 'רגיל' : `${x}x`]), String(v.playbackRate));
      if (r) v.playbackRate = +r;
    },
    sleep: async () => {
      const s = sleep();
      const r = await bottomSheet('🌙 שעון שינה', [['off', 'כבוי'], ['15', '15 דקות'], ['30', '30 דקות'], ['45', '45 דקות'], ['60', 'שעה'], ['90', 'שעה וחצי'], ['ep', 'בסוף הפרק הזה']],
        !s ? 'off' : s.ep ? 'ep' : '');
      if (!r) return;
      setSleep(r === 'off' ? null : r === 'ep' ? {ep: true} : {until: Date.now() + (+r) * 60000});
      if (r !== 'off') toast(r === 'ep' ? '🌙 הצפייה תיעצר בסוף הפרק' : `🌙 הצפייה תיעצר בעוד ${r} דקות`, 3000);
    },
    intro: async () => {
      const ct = v.currentTime;
      const cur = hasIntro() ? `${fmtTime(t.intro_start || 0)}–${fmtTime(t.intro_end)}` : 'עוד לא הוגדרה';
      const r = await bottomSheet(`הפתיחה של ${t.name} · ${cur}`, [
        ['start', `▶ הפתיחה מתחילה כאן (${fmtTime(ct)})`],
        ['end', `⏹ הפתיחה נגמרת כאן (${fmtTime(ct)})`],
        ['auto', autoSkip() ? 'דילוג אוטומטי: פועל ✓ (לחץ לכיבוי)' : 'דילוג אוטומטי: כבוי (לחץ להפעלה)'],
        ...(hasIntro() ? [['clear', '🗑 מחק את הגדרת הפתיחה']] : []),
      ]);
      if (!r) return;
      if (r === 'auto') { store.set(`intro-auto:${t.id}`, !autoSkip()); toast(autoSkip() ? 'הפתיחה תדולג אוטומטית בכל הפרקים' : 'דילוג אוטומטי כבוי', 3000); return; }
      let body;
      if (r === 'clear') body = {intro_start: null, intro_end: null};
      else if (r === 'start') body = {intro_start: ct, intro_end: t.intro_end != null && t.intro_end > ct ? t.intro_end : Math.min(ct + 90, (v.duration || ct + 90))};
      else body = {intro_start: t.intro_start != null && t.intro_start < ct ? t.intro_start : Math.max(0, Math.min(t.intro_start ?? 0, ct - 1)), intro_end: ct};
      try {
        await api(`/api/titles/${t.id}`, {method: 'PATCH', body});
        Object.assign(t, body);
        introDone = false;
        toast(r === 'clear' ? 'הגדרת הפתיחה נמחקה' : `נשמר לכל הפרקים: הפתיחה ${fmtTime(t.intro_start || 0)}–${fmtTime(t.intro_end)}`, 4000);
      } catch (e) { alert(e.message); }
    },
  };
  wrap.addEventListener('click', (e) => {
    const b = e.target.closest('[data-a]');
    if (!b) return;
    e.stopPropagation();
    actions[b.dataset.a]?.();
    b.blur();
    showUI();
  });
  wrap.addEventListener('dblclick', (e) => { if (e.target.closest('.pl-bar, .pl-top, button')) e.stopPropagation(); });

  const onKey = (e) => {
    if (e.target.matches('input:not([type=range]), textarea, select')) return;
    const k = e.key.toLowerCase();
    if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
    else if (k === 'arrowright') { e.preventDefault(); actions.fwd10(); }
    else if (k === 'arrowleft') { e.preventDefault(); actions.back10(); }
    else if (k === 'arrowup') { e.preventDefault(); v.muted = false; v.volume = Math.min(1, v.volume + 0.1); }
    else if (k === 'arrowdown') { e.preventDefault(); v.volume = Math.max(0, v.volume - 0.1); }
    else if (k === 'm' || k === 'צ') actions.mute();
    else if (k === 'f' || k === 'כ') toggleFs();
    else if ((k === 's' || k === 'ד') && isSeries) skipIntro();
    else if ((k === 'n' || k === 'מ') && next) actions.next();
    else return;
    showUI();
  };
  const onHide = () => { if (document.visibilityState === 'hidden') beacon(); };
  const onFs = () => document.body.classList.toggle('pl-fs', !!document.fullscreenElement);
  document.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', onHide);
  document.addEventListener('fullscreenchange', onFs);
  window.addEventListener('pagehide', beacon);
  onFs();

  cleanup = () => {
    if (!finished) beacon();
    clearInterval(timer);
    clearInterval(nextTimer);
    clearInterval(tick);
    clearTimeout(hideTimer);
    if (ms) {
      ['play', 'pause', 'seekbackward', 'seekforward', 'seekto', 'previoustrack', 'nexttrack'].forEach((a) => msSet(a, null));
      try { ms.metadata = null; } catch (_) {}
    }
    if (document.pictureInPictureElement === v) document.exitPictureInPicture().catch(() => {});
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('visibilitychange', onHide);
    document.removeEventListener('fullscreenchange', onFs);
    window.removeEventListener('pagehide', beacon);
    v.pause();
    v.removeAttribute('src');
    v.load(); // stop downloading
    document.title = 'Streamora';
    libraryCache = null;
    // full screen stays on only when going straight to another episode
    setTimeout(() => {
      if (!location.hash.startsWith('#/watch/') && document.fullscreenElement) document.exitFullscreen().catch(() => {});
      if (!location.hash.startsWith('#/watch/')) document.body.classList.remove('pl-fs');
    }, 0);
  };
}

// ----------------------------------------------------------------- settings
function syncLine(sync) {
  const when = (t) => new Date(t * 1000).toLocaleString('he-IL');
  const parts = [];
  if (sync.last) {
    parts.push(sync.last.ok
      ? `✓ סונכרן ב-${when(sync.last.t)}: התקבלו ${sync.last.got ?? 0} עדכונים, נשלחו ${sync.last.sent ?? 0}. הסנכרון חוזר לבד כל 5 דקות.`
      : `<span style="color:var(--red)">✗ ${esc(sync.last.error || 'הסנכרון נכשל')}</span>`);
  }
  if (sync.last_from) parts.push(`מכשיר אחר סנכרן איתי ב-${when(sync.last_from.t)}.`);
  return parts.join('<br>') || (sync.peer ? 'עוד לא סונכרן.' : '');
}
function telegramPanel(st) {
  if (st.telegram_authorized) {
    return `<div class="status-line"><span class="status-dot on"></span>
        מחובר לטלגרם בתור <b>${esc(st.me?.name || '')}</b> ${st.me?.username ? '@' + esc(st.me.username) : ''}</div>
      <button class="btn small danger" id="tg-logout" style="margin-top:12px">התנתק מטלגרם</button>`;
  }
  if (!st.telegram_configured) {
    return `<div class="status-line"><span class="status-dot"></span> שלב 1 מתוך 2: מפתחות API</div>
      <ol class="steps">
        <li>פתח את <a class="link" href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a> והתחבר עם מספר הטלפון שלך. הקוד יגיע אליך באפליקציית טלגרם.</li>
        <li>לחץ על <b>API development tools</b>.</li>
        <li>מלא את <b>App title</b> ואת <b>Short name</b> (כל שם באנגלית, למשל <code>myvideos</code>), ולחץ <b>Create application</b>.</li>
        <li>העתק לכאן את <b>App api_id</b> ואת <b>App api_hash</b>.</li>
      </ol>
      <div class="form-grid">
        <label class="field">API ID<input class="input" id="api-id" inputmode="numeric" dir="ltr" placeholder="1234567"></label>
        <label class="field">API Hash<input class="input" id="api-hash" dir="ltr" autocapitalize="off" autocomplete="off" spellcheck="false" placeholder="32 תווים"></label>
      </div>
      <button class="btn primary" id="save-api">שמור והמשך</button>`;
  }
  return `<div class="status-line"><span class="status-dot"></span> שלב 2 מתוך 2: התחברות לחשבון</div>
    <div id="tg-step-phone">
      <p class="muted">הכנס את מספר הטלפון של חשבון הטלגרם. קוד יישלח אליך <b>באפליקציית טלגרם</b>.</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="input" id="tg-phone" type="tel" dir="ltr" placeholder="050-1234567" style="flex:1;min-width:180px">
        <button class="btn primary" id="tg-send">שלח קוד</button>
      </div>
    </div>
    <div id="tg-step-code" hidden>
      <p class="muted">הקלד את הקוד שקיבלת בטלגרם (5 ספרות):</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="input" id="tg-code" inputmode="numeric" dir="ltr" autocomplete="one-time-code" style="flex:1;min-width:140px">
        <button class="btn primary" id="tg-code-go">התחבר</button>
      </div>
    </div>
    <div id="tg-step-pass" hidden>
      <p class="muted">לחשבון שלך יש אימות דו-שלבי. הכנס את הסיסמה שלו:</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="input" id="tg-pass" type="password" style="flex:1;min-width:160px">
        <button class="btn primary" id="tg-pass-go">התחבר</button>
      </div>
    </div>
    <div class="err-line" id="tg-err"></div>
    <button class="btn small" id="reset-api" style="margin-top:14px">החלף API ID / Hash</button>`;
}

const SETTINGS_SECTIONS = [['sec-tg', '📡', 'חיבור לטלגרם'], ['sec-chats', '📋', 'קבוצות וערוצים'], ['sec-scan', '🔄', 'סריקה'],
  ['sec-sync', '🔗', 'סנכרון מכשירים'], ['sec-dups', '👯', 'כפילויות אפשריות'], ['sec-hidden', '🙈', 'מוסתרים'], ['sec-clean', '🧹', 'ניקוי שמות'], ['sec-tmdb', '🖼', 'פוסטרים ותקצירים'], ['sec-pw', '🔒', 'סיסמה ויציאה']];

async function pageSettings(section) {
  // phone: a menu of topics, each opens on its own screen (#/more/sec-…); computer: all topics with a side menu
  const reload = () => pageSettings(section);
  $app.innerHTML = '<div class="spinner"></div>';
  const st = await api('/api/status');
  const auth = st.telegram_authorized;
  const sec = SETTINGS_SECTIONS.find(([id]) => id === section);
  const statusOf = {
    'sec-tg': auth ? `✓ מחובר${st.me?.name ? ' · ' + st.me.name : ''}` : 'לא מחובר',
    'sec-scan': st.scan?.running ? '⏳ סורק עכשיו' : '',
    'sec-sync': st.sync?.peer ? st.sync.peer : 'כבוי',
    'sec-dups': !st.duplicates ? 'אין' : st.duplicates === 1 ? 'אחת לבדיקה' : `${n(st.duplicates)} לבדיקה`,
    'sec-tmdb': !st.tmdb ? 'לא מוגדר' : st.tmdb_status?.running ? `⏳ ${st.tmdb_status.done.toLocaleString('he-IL')} מתוך ${st.tmdb_status.total.toLocaleString('he-IL')}` : '✓ פעיל',
  };

  $app.innerHTML = `
    ${sec ? `<a class="backlink" href="#/more">→ הגדרות</a>${pageHeader(sec[2])}` : pageHeader("עוד", "הגדרות, חיבורים וקבוצות")}
    <div class="settings-layout ${sec ? 'one' : 'menu'}" ${sec ? `data-sec="${sec[0]}"` : ''}><nav class="settings-nav">
      ${SETTINGS_SECTIONS.filter(([id]) => auth || (id !== 'sec-chats' && id !== 'sec-scan'))
        .map(([id, e, l]) => `<button data-go="${id}" class="${id === section ? 'on' : ''}"><span>${e}</span><b>${l}</b>${statusOf[id] ? `<small>${esc(statusOf[id])}</small>` : ''}<i>‹</i></button>`).join('')}
    </nav><div class="settings-body">
    <div class="panel" id="sec-tg"><h3>חיבור לטלגרם</h3>${telegramPanel(st)}</div>
    <div class="panel" id="sec-chats" ${auth ? '' : 'hidden'}><h3>קבוצות וערוצים</h3>
      <p class="muted" style="margin-top:0">סמן את הקבוצות שמהן ייאספו הסדרות והסרטים.</p>
      <div id="chats">${auth ? '<div class="spinner"></div>' : ''}</div>
    </div>
    <div class="panel" id="sec-scan" ${auth ? '' : 'hidden'}><h3>סריקה</h3>
      <div id="scan-status" class="muted"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
        <button class="btn primary" id="scan">סרוק עכשיו</button>
        <button class="btn" id="scan-full">סריקה מלאה מההתחלה</button>
      </div>
      <p class="muted" style="font-size:13px;margin-bottom:0">כל עוד האתר פועל, הוא סורק לבד ${st.scan_interval_minutes ? `כל ${st.scan_interval_minutes} דקות` : ''} ובכל הפעלה. סריקה מלאה לא מוחקת עריכות שעשית.</p>
    </div>
    <div class="panel" id="sec-sync"><h3>סנכרון בין מכשירים</h3>
      <p class="muted" style="margin-top:0">מעביר את ההתקדמות בצפייה בין הפלאפון למחשב, כשהם באותו Wi-Fi. בכל סרט ופרק נשמרת הצפייה האחרונה, לא משנה איפה צפית.</p>
      ${st.sync.this_device ? `<div class="note" style="margin-bottom:12px">הכתובת של המכשיר הזה: <b dir="ltr">${esc(st.sync.this_device)}</b><br>אותה כותבים במכשיר השני.</div>` : ''}
      <label class="field">הכתובת של המכשיר השני (המחשב)
        <input class="input" id="sync-peer" dir="ltr" inputmode="url" placeholder="192.168.1.20:8000" value="${esc(st.sync.peer || '')}">
      </label>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
        <button class="btn primary" id="sync-now">שמור וסנכרן עכשיו</button>
      </div>
      <div id="sync-status" class="muted" style="margin-top:10px;font-size:13px">${syncLine(st.sync)}</div>
    </div>
    <div class="panel" id="sec-dups"><h3>כפילויות אפשריות</h3>
      <p class="muted" style="margin-top:0">כאן מופיעים סרטים וסדרות שנראים אותו דבר עם מילה נוספת בשם, והאתר לא איחד אותם לבד כי לא היה בטוח. לכל זוג: <b>אחד</b> מאחד רק אותם, <b>מחק בכל האתר</b> מוחק את המילה המיותרת מכל השמות, <b>לא אותו דבר</b> מסתיר את ההצעה.</p>
      <div id="dups"><div class="spinner"></div></div>
    </div>
    <div class="panel" id="sec-hidden"><h3>מוסתרים</h3>
      <p class="muted" style="margin-top:0">סדרות וסרטים שהסתרת (בדף שלהם ← ⋯ ← הסתר). הם לא מופיעים בשום מקום באתר עד שתחזיר אותם.</p>
      <div id="hidden-list"><div class="spinner"></div></div>
    </div>
    <div class="panel" id="sec-clean"><h3>ניקוי שמות</h3>
      <p class="muted" style="margin-top:0">האתר מזהה לבד חתימות של קבוצות (כמו "קינג סרט") ומוחק אותן משמות הסרטים, כדי שכל סרט יופיע פעם אחת. אם נשארה חתימה, כתוב אותה כאן, <b>שורה לכל ביטוי</b>, והיא תימחק מכל השמות.</p>
      <textarea class="input" id="strip-phrases" rows="5" style="width:100%;resize:vertical" placeholder="(ריק) כתוב כאן, למשל:&#10;קינג סרט&#10;פופקורן טיים">${esc((st.strip_phrases || []).join('\n'))}</textarea>
      <button class="btn" id="save-phrases" style="margin-top:8px">שמור וסדר מחדש</button>
    </div>
    <div class="panel" id="sec-tmdb"><h3>פוסטרים ותקצירים (רשות)</h3>
      <p class="muted" style="margin-top:0">${st.tmdb ? '✓ פעיל.' + (st.tmdb_status?.running ? ` ⏳ מוריד פוסטרים ותקצירים: ${st.tmdb_status.done.toLocaleString('he-IL')} מתוך ${st.tmdb_status.total.toLocaleString('he-IL')} (ממשיך ברקע)` : '') : 'כדי לקבל פוסטרים ותקצירים בעברית, הירשם בחינם ל-<a class="link" href="https://www.themoviedb.org/signup" target="_blank" rel="noopener">TMDB</a>, ואז ב-<a class="link" href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener">הגדרות ← API</a> העתק את ה-<b>API Key</b>.'}</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="input" id="tmdb-key" dir="ltr" autocomplete="off" placeholder="${st.tmdb ? 'להחלפה: הדבק מפתח חדש' : 'API Key'}" style="flex:1;min-width:180px">
        <button class="btn" id="save-tmdb">שמור</button>
      </div>
    </div>
    <div class="panel" id="sec-pw"><h3>סיסמת האתר</h3>
      <div class="form-grid">
        <input class="input" id="pw-cur" type="password" placeholder="סיסמה נוכחית">
        <input class="input" id="pw-new" type="password" placeholder="סיסמה חדשה">
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" id="change-pw">שנה סיסמה</button>
        <button class="btn danger" id="logout">יציאה מהאתר</button>
      </div>
    </div>
    </div></div>
    ${copyright()}`;

  const $ = (sel) => $app.querySelector(sel);
  $app.querySelectorAll('[data-go]').forEach((b) => b.onclick = () => {
    if (!matchMedia('(min-width: 1000px)').matches) { location.hash = `#/more/${b.dataset.go}`; return; }
    $app.querySelectorAll('[data-go]').forEach((x) => x.classList.toggle('on', x === b));
    document.getElementById(b.dataset.go)?.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  if (sec && matchMedia('(min-width: 1000px)').matches) document.getElementById(sec[0])?.scrollIntoView({block: 'start'});
  const run = async (fn, errEl) => {
    if (errEl) errEl.textContent = '';
    try { await fn(); } catch (e) { if (errEl) errEl.textContent = e.message; else alert(e.message); }
  };

  // --- telegram connect flow
  $('#save-api')?.addEventListener('click', () => run(async () => {
    await api('/api/settings', {method: 'POST', body: {api_id: $('#api-id').value, api_hash: $('#api-hash').value.trim()}});
    reload();
  }));
  $('#reset-api')?.addEventListener('click', () => {
    $('.panel').innerHTML = '<h3>חיבור לטלגרם</h3>' + telegramPanel({...st, telegram_configured: false});
    $('#save-api').onclick = () => run(async () => {
      await api('/api/settings', {method: 'POST', body: {api_id: $('#api-id').value, api_hash: $('#api-hash').value.trim()}});
      reload();
    });
  });
  const tgErr = $('#tg-err');
  const show = (step) => ['phone', 'code', 'pass'].forEach((s) => { const el = $(`#tg-step-${s}`); if (el) el.hidden = s !== step; });
  const afterLogin = (r) => {
    if (r.need_password) { show('pass'); $('#tg-pass').focus(); } else reload();
  };
  $('#tg-send')?.addEventListener('click', () => run(async () => {
    $('#tg-send').disabled = true;
    let r;
    try { r = await api('/api/tg/send-code', {method: 'POST', body: {phone: $('#tg-phone').value}}); } finally { $('#tg-send').disabled = false; }
    show('code'); $('#tg-code').focus();
    if (r.already_sent) tgErr.textContent = 'הקוד כבר נשלח קודם. הזן את הקוד האחרון שקיבלת בטלגרם.';
  }, tgErr));
  $('#tg-code-go')?.addEventListener('click', () => run(async () => {
    afterLogin(await api('/api/tg/sign-in', {method: 'POST', body: {code: $('#tg-code').value}}));
  }, tgErr));
  $('#tg-pass-go')?.addEventListener('click', () => run(async () => {
    afterLogin(await api('/api/tg/sign-in', {method: 'POST', body: {password: $('#tg-pass').value}}));
  }, tgErr));
  $('#tg-logout')?.addEventListener('click', () => confirm('להתנתק מחשבון הטלגרם? הספרייה ומעקב הצפייה יישמרו.') &&
    run(async () => { await api('/api/tg/logout', {method: 'POST'}); reload(); }));

  // --- other settings
  $('#save-tmdb').onclick = () => run(async () => {
    await api('/api/settings', {method: 'POST', body: {tmdb_api_key: $('#tmdb-key').value.trim()}});
    if (auth) await api('/api/scan', {method: 'POST', body: {}});
    reload();
  });
  $('#sync-now').onclick = () => run(async () => {
    const out = $('#sync-status');
    await api('/api/settings', {method: 'POST', body: {sync_peer: $('#sync-peer').value}});
    out.textContent = '⏳ מסנכרן...';
    const r = await api('/api/sync/now', {method: 'POST'});
    out.innerHTML = syncLine({peer: r.peer, last: r});
    libraryCache = null;
  }, $('#sync-status'));
  // --- possible duplicates
  const drawDups = async () => {
    const box = $('#dups');
    if (!box) return;
    let pairs, total;
    try { ({pairs, total} = await api('/api/duplicates')); } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
    if (!pairs.length) { box.innerHTML = '<div class="note">✓ אין כרגע כפילויות לבדיקה.</div>'; return; }
    const one = (x, kind) => `<a class="dup-t" href="#/title/${x.id}"><div class="poster">${img(x.poster)}</div>
      <div><b>${esc(x.name)}</b><span>${[x.year, kind === 'movie' && x.duration ? fmtDur(x.duration) : '', count(x.files, 'קובץ אחד', 'קבצים')].filter(Boolean).map((y) => `<bdi>${esc(y)}</bdi>`).join(' · ')}</span></div></a>`;
    box.innerHTML = `<div class="muted" style="font-size:13px;margin-bottom:8px">${count(total, 'זוג אחד', 'זוגות')}${total > pairs.length ? ` (מוצגים ${pairs.length} הראשונים)` : ''}</div>` + pairs.map((p) => `
      <div class="dup" data-key="${p.key}">
        <div class="dup-pair">${one(p.a, p.kind)}<span class="dup-eq">≈</span>${one(p.b, p.kind)}</div>
        <div class="dup-acts">
          <button class="btn small primary" data-join="${p.b.id}" data-into="${p.a.id}">אחד</button>
          ${(p.extras || [p.extra]).map((x) => `<button class="btn small" data-strip="${esc(x)}">מחק "${esc(x)}" בכל האתר</button>`).join('')}
          <button class="btn small ghost" data-dismiss="${p.key}">לא אותו דבר</button>
        </div></div>`).join('');
  };
  $('#dups')?.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const row = b.closest('.dup');
    run(async () => {
      if (b.dataset.join) {
        await api(`/api/titles/${b.dataset.join}/merge`, {method: 'POST', body: {into: +b.dataset.into}});
        row.remove();
      } else if (b.dataset.dismiss) {
        await api('/api/duplicates/dismiss', {method: 'POST', body: {key: b.dataset.dismiss}});
        row.remove();
      } else if (b.dataset.strip) {
        const word = b.dataset.strip;
        if (!confirm(`למחוק את "${word}" מכל השמות באתר? (אפשר לבטל אחר כך ב"ניקוי שמות")`)) return;
        const list = (st.strip_phrases || []).concat(word);
        st.strip_phrases = list;
        await api('/api/settings', {method: 'POST', body: {strip_phrases: list.join('\n')}});
        const ta = $('#strip-phrases');
        if (ta) ta.value = list.join('\n');
        $app.querySelectorAll(`[data-strip="${CSS.escape(word)}"]`).forEach((x) => x.closest('.dup')?.remove());
      }
      libraryCache = null;
    });
  });
  if (!sec || sec[0] === 'sec-dups' || matchMedia('(min-width: 1000px)').matches) drawDups();

  // --- hidden titles
  const drawHidden = async () => {
    const box = $('#hidden-list');
    if (!box) return;
    let list;
    try { list = (await api('/api/hidden')).titles; } catch (e) { box.innerHTML = errorBox(e); return; }
    box.innerHTML = list.length ? list.map((x) => `<div class="hid-row" data-id="${x.id}">
        <a href="#/title/${x.id}"><b>${esc(x.name)}</b> <span class="muted">${x.kind === 'series' ? 'סדרה' : 'סרט'}${x.year ? ' · ' + x.year : ''}</span></a>
        <button class="btn small">👁 החזר</button></div>`).join('')
      : '<div class="note">אין כרגע סדרות או סרטים מוסתרים.</div>';
    box.querySelectorAll('.hid-row button').forEach((b) => b.onclick = () => run(async () => {
      const row = b.closest('.hid-row');
      await api(`/api/titles/${row.dataset.id}`, {method: 'PATCH', body: {hidden_title: false}});
      row.remove();
      libraryCache = null;
      if (!box.querySelector('.hid-row')) drawHidden();
    }));
  };
  if (!sec || sec[0] === 'sec-hidden' || matchMedia('(min-width: 1000px)').matches) drawHidden();

  $('#save-phrases').onclick = () => run(async () => {
    await api('/api/settings', {method: 'POST', body: {strip_phrases: $('#strip-phrases').value}});
    libraryCache = null;
    renderScan({running: true, chat: 'מסדר שמות לפי הרשימה שלך', added: 0});
    startPoll();
    $('#scan-status').scrollIntoView({behavior: 'smooth', block: 'center'});
  });
  $('#change-pw').onclick = () => run(async () => {
    await api('/api/password', {method: 'POST', body: {current: $('#pw-cur').value, new: $('#pw-new').value}});
    alert('הסיסמה שונתה');
    $('#pw-cur').value = $('#pw-new').value = '';
  });
  $('#logout').onclick = async () => { await api('/api/logout', {method: 'POST'}); location.href = '/login'; };

  if (!auth) return;

  // --- scanning
  let poll = null;
  const renderScan = (s) => {
    const el = $('#scan-status');
    if (!el) return;
    const when = s.last_finished ? new Date(s.last_finished * 1000).toLocaleString('he-IL') : 'עדיין לא';
    // per group: what the last scan found, and why nothing was added when that happens
    const why = (r) => {
      const parts = [`נבדקו ${count(r.checked, 'הודעה אחת', 'הודעות')}`, count(r.videos, 'סרטון אחד', 'סרטונים'), `נוספו ${n(r.added)}`];
      let hint = '';
      if (!r.checked) hint = r.first ? 'לא נמצאו הודעות בקבוצה.' : 'אין הודעות חדשות מאז הסריקה הקודמת.';
      else if (!r.videos && r.files) hint = `יש ${count(r.files, 'קובץ אחד', 'קבצים')}, אבל אף אחד מהם אינו קובץ וידאו (למשל RAR/ZIP או מסמכים).`;
      else if (!r.videos) hint = 'אין בקבוצה סרטונים, רק הודעות טקסט, תמונות או קישורים.';
      else if (!r.added && r.short === r.videos) hint = 'כל הסרטונים קצרים מ-3 דקות (טריילרים/קטעים), ולכן דולגו.';
      else if (!r.added) hint = 'כל הסרטונים כבר נמצאים בספרייה.';
      else if (r.short) hint = r.short === 1 ? 'סרטון אחד קצר מ-3 דקות דולג.' : `${n(r.short)} סרטונים קצרים מ-3 דקות דולגו.`;
      return `<div class="scan-rep"><b>${esc(r.chat)}</b>: ${parts.join(' · ')}${hint ? `<div class="muted">${esc(hint)}</div>` : ''}</div>`;
    };
    el.innerHTML = s.running
      ? `⏳ סורק${s.chat ? ': ' + esc(s.chat) : '...'} · נוספו ${s.added}${s.queued ? '<div class="muted">הסריקה של הקבוצות שבחרת תתחיל מיד כשזה יסתיים.</div>' : ''}`
      : `סריקה אחרונה: ${when}${s.added ? ` · נוספו ${s.added}` : ''}${(s.report || []).map(why).join('')}${s.note ? `<div class="note" style="margin:8px 0 0">✓ ${esc(s.note)}</div>` : ''}${s.error ? `<div class="error-box" style="margin-top:8px">${esc(s.error)}</div>` : ''}`;
  };
  const startPoll = () => {
    clearInterval(poll);
    poll = setInterval(async () => {
      const s = (await api('/api/status').catch(() => null))?.scan;
      if (!s) return;
      renderScan(s);
      if (!s.running) { clearInterval(poll); libraryCache = null; }
    }, 2000);
  };
  renderScan(st.scan);
  if (st.scan.running) startPoll();
  cleanup = () => clearInterval(poll);
  const doScan = async (full) => {
    try { renderScan(await api('/api/scan', {method: 'POST', body: {full}})); startPoll(); } catch (e) { alert(e.message); }
  };
  $('#scan').onclick = () => doScan(false);
  $('#scan-full').onclick = () => confirm('לסרוק מחדש את כל ההודעות בכל הקבוצות? זה יכול לקחת זמן.') && doScan(true);

  // --- chats
  const box = $('#chats');
  try {
    const dialogs = await api('/api/dialogs');
    dialogs.sort((a, b) => (b.enabled - a.enabled) || a.title.localeCompare(b.title, 'he'));
    box.innerHTML = `
      <input class="input" id="chat-filter" placeholder="חיפוש קבוצה..." style="width:100%">
      <p class="muted" style="font-size:13px">💡 אם כל ערוץ הוא עונה אחרת של אותה סדרה (למשל ערוץ לכל סאגה), כתוב ליד כל ערוץ מסומן את מספר העונה שלו. אפשר גם לתת שם לעונה.</p>
      <div class="chat-list">${dialogs.map((d) => `
        <div class="chat-item ${d.enabled ? 'on' : ''}" data-name="${esc(d.title.toLowerCase())}" data-id="${d.id}" data-title="${esc(d.title)}">
          <label class="chat-main">
            <input type="checkbox" ${d.enabled ? 'checked' : ''}>
            <span style="flex:1">${esc(d.title)}</span>
            <span class="badge">${d.type === 'channel' ? 'ערוץ' : 'קבוצה'}</span>
          </label>
          <div class="chat-season">
            <label>עונה <input class="input num" name="season" inputmode="numeric" placeholder="אוטו׳" value="${d.season ?? ''}"></label>
            <input class="input" name="season_label" placeholder="שם העונה (רשות), למשל: איסט בלו" value="${esc(d.season_label || '')}">
          </div>
        </div>`).join('')}</div>
      <button class="btn primary" id="save-chats">שמור וסרוק</button>`;
    box.querySelectorAll('.chat-item input[type=checkbox]').forEach((cb) => cb.onchange = () => {
      cb.closest('.chat-item').classList.toggle('on', cb.checked);
    });
    box.querySelector('#chat-filter').oninput = (e) => {
      const q = e.target.value.trim().toLowerCase();
      box.querySelectorAll('.chat-item').forEach((el) => { el.style.display = el.dataset.name.includes(q) ? '' : 'none'; });
    };
    box.querySelector('#save-chats').onclick = async () => {
      const chats = [...box.querySelectorAll('.chat-item')]
        .filter((el) => el.querySelector('input[type=checkbox]').checked || dialogs.find((d) => String(d.id) === el.dataset.id)?.enabled)
        .map((el) => ({
          id: el.dataset.id, title: el.dataset.title,
          enabled: el.querySelector('input[type=checkbox]').checked,
          season: el.querySelector('[name=season]').value.trim(),
          season_label: el.querySelector('[name=season_label]').value.trim(),
        }));
      try {
        await api('/api/chats', {method: 'POST', body: {chats}});
        await doScan(false);
        $('#scan-status').scrollIntoView({behavior: 'smooth', block: 'center'});
      } catch (e) { alert(e.message); }
    };
  } catch (e) {
    box.innerHTML = errorBox(e);
  }
}

route();
