/* הספרייה שלי - single page app (hash routing, no build step) */
'use strict';

const $app = document.getElementById('app');
const $tabbar = document.getElementById('tabbar');
let cleanup = null;          // called when leaving the current page
let libraryCache = null;

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
  const r = await fetch(path, init);
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
  if (prev && pageState[prev]) pageState[prev].scrollY = window.scrollY;
  if (cleanup) { try { cleanup(); } catch (_) {} cleanup = null; }
  const hash = location.hash.replace(/^#/, '') || '/';
  route.current = hash;
  const [path] = hash.split('?');
  const parts = path.split('/').filter(Boolean);
  const first = parts[0] || '';
  // tab bar: hidden in the player; inner pages (title) keep the tab you came from
  const tab = TAB_OF[first] ?? lastTab;
  lastTab = tab;
  $tabbar.hidden = first === 'watch';
  $tabbar.querySelectorAll('a').forEach((a) => a.classList.toggle('on', a.dataset.tab === tab));
  document.body.classList.toggle('in-player', first === 'watch');
  const st = pageState[hash] || (pageState[hash] = {});
  window.scrollTo(0, 0);
  try {
    if (first === 'title') await pageTitle(+parts[1]);
    else if (first === 'watch') await pageWatch(+parts[1]);
    else if (first === 'more' || first === 'settings') await pageSettings();
    else if (first === 'series') await pageList('series', st);
    else if (first === 'movies') await pageList('movie', st);
    else if (first === 'search') await pageSearch(st);
    else if (first === 'mylist') await pageMyList();
    else await pageHome();
    if (st.scrollY) requestAnimationFrame(() => window.scrollTo(0, st.scrollY));
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

function pageHeader(title, sub = '', {logo = false} = {}) {
  return `<header class="phead">
    <div class="phead-title">${logo ? '<span class="logo"><i></i>הספרייה שלי</span>' : `<h1>${esc(title)}</h1>${sub ? `<div class="phead-sub">${sub}</div>` : ''}`}</div>
    <a class="iconbtn" href="#/search" aria-label="חיפוש">🔍</a>
  </header>`;
}

function emptyLibrary() {
  return `<div class="empty"><h2>הספרייה עדיין ריקה</h2>
    <p>בחר את קבוצות הטלגרם שמהן ייאספו הסדרות והסרטים, והפעל סריקה.</p>
    <a class="btn primary" href="#/more">להגדרות</a></div>`;
}

const PAGE = 60;
const n = (x) => Number(x || 0).toLocaleString('he-IL');

// ---------------------------------------------------------------------- home
async function pageHome() {
  if (!libraryCache) $app.innerHTML = pageHeader('', '', {logo: true}) + skeletonRows();
  const [all, series, movies] = await Promise.all([
    api('/api/library?limit=20'),
    api('/api/library?kind=series&limit=20'),
    api('/api/library?kind=movie&limit=20'),
  ]);
  libraryCache = all;
  if (!all.counts.all) { $app.innerHTML = pageHeader('', '', {logo: true}) + emptyLibrary(); return; }
  const cont = all.continue || [];
  $app.innerHTML = pageHeader('', '', {logo: true}) +
    (cont.length ? rowSection('המשך צפייה', '', `<div class="hrow">${cont.map(contCard).join('')}</div>`) : '') +
    (series.titles.length ? rowSection('סדרות שנוספו לאחרונה', '#/series', `<div class="hrow">${series.titles.map(posterCard).join('')}</div>`) : '') +
    (movies.titles.length ? rowSection('סרטים שנוספו לאחרונה', '#/movies', `<div class="hrow">${movies.titles.map(posterCard).join('')}</div>`) : '');
}

function rowSection(title, more, body) {
  return `<section class="rsec"><div class="rsec-head"><h2>${esc(title)}</h2>${more ? `<a href="${more}">הכל ›</a>` : ''}</div>${body}</section>`;
}

function skeletonRows() {
  const cards = Array.from({length: 4}, () => '<div class="poster-card"><div class="poster sk"></div></div>').join('');
  return [1, 2].map(() => `<section class="rsec"><div class="rsec-head"><h2 class="sk sk-text"></h2></div><div class="hrow">${cards}</div></section>`).join('');
}
function skeletonGrid() {
  return `<div class="pgrid">${Array.from({length: 9}, () => '<div class="poster-card"><div class="poster sk"></div></div>').join('')}</div>`;
}

// ------------------------------------------------------------ series / movies
async function pageList(kind, st) {
  const isSeries = kind === 'series';
  const title = isSeries ? 'סדרות' : 'סרטים';
  $app.innerHTML = pageHeader(title) + skeletonGrid();
  const want = Math.max(PAGE, st.loaded || 0);  // coming back: load as much as before
  const data = await api(`/api/library?kind=${kind}&offset=0&limit=${Math.min(200, want)}`);
  let titles = data.titles;
  while (titles.length < want && titles.length < data.total) {
    const more = await api(`/api/library?kind=${kind}&offset=${titles.length}&limit=200`);
    if (!more.titles.length) break;
    titles = titles.concat(more.titles);
  }
  if (!data.counts.all) { $app.innerHTML = pageHeader(title) + emptyLibrary(); return; }
  $app.innerHTML = pageHeader(title, `${n(data.total)} ${title} בספרייה`) +
    `<div class="pgrid" id="grid">${titles.map(posterCard).join('')}</div>
     <div id="more" ${titles.length < data.total ? '' : 'hidden'}><div class="spinner"></div></div>`;
  st.loaded = titles.length;
  cleanup = infiniteScroll(async () => {
    if (st.loaded >= data.total) return false;
    const more = await api(`/api/library?kind=${kind}&offset=${st.loaded}&limit=${PAGE}`);
    $app.querySelector('#grid').insertAdjacentHTML('beforeend', more.titles.map(posterCard).join(''));
    st.loaded += more.titles.length;
    return more.titles.length > 0 && st.loaded < data.total;
  });
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
      <span>🔍</span><input id="q" type="search" placeholder="חיפוש סדרה או סרט..." value="${esc(st.q || '')}" autocomplete="off">
    </div><a class="iconbtn" href="javascript:history.back()" aria-label="חזרה">✕</a></header>
    <div id="results"></div>`;
  const input = $app.querySelector('#q'), out = $app.querySelector('#results');
  let timer = null, token = 0;
  const run = async () => {
    const q = input.value.trim();
    st.q = q;
    const my = ++token;
    if (!q) { out.innerHTML = '<p class="muted center">הקלד שם של סדרה או סרט</p>'; return; }
    out.innerHTML = skeletonGrid();
    const [s, m] = await Promise.all([
      api(`/api/library?kind=series&limit=30&q=${encodeURIComponent(q)}`),
      api(`/api/library?kind=movie&limit=30&q=${encodeURIComponent(q)}`),
    ]);
    if (my !== token) return;
    if (!s.total && !m.total) { out.innerHTML = `<p class="muted center">לא נמצא "${esc(q)}" בספרייה</p>`; return; }
    out.innerHTML =
      (s.total ? rowSection(`סדרות (${n(s.total)})`, '', `<div class="pgrid">${s.titles.map(posterCard).join('')}</div>`) : '') +
      (m.total ? rowSection(`סרטים (${n(m.total)})`, '', `<div class="pgrid">${m.titles.map(posterCard).join('')}</div>`) : '');
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 300); });
  run();
  if (!st.q) setTimeout(() => input.focus(), 50);
  cleanup = () => clearTimeout(timer);
}

// ------------------------------------------------------------------- my list
async function pageMyList() {
  $app.innerHTML = pageHeader('הרשימה שלי') + skeletonRows();
  const data = await api('/api/library?limit=1');
  const cont = data.continue || [];
  $app.innerHTML = pageHeader('הרשימה שלי') +
    `<div class="note">בקרוב כאן: <b>לצפות בהמשך</b>, <b>היסטוריה</b> ו<b>סטטיסטיקה</b>.</div>` +
    (cont.length ? rowSection('באמצע לצפות', '', `<div class="vlist">${cont.map(contCard).join('')}</div>`) : '');
}

function contCard(c) {
  const m = c.media;
  const p = c.state === 'next' ? 0 : pct({...m, position: c.position});
  const label = c.state === 'next' ? '<span class="badge accent">הפרק הבא</span>'
    : c.state === 'start' ? '<span class="badge">חדש</span>'
    : `<span>נשארו ${fmtTime((m.duration || 0) - c.position)}</span>`;
  const pic = c.title.backdrop || m.thumb || c.title.poster;
  return `<a class="cont-card" href="#/watch/${m.id}">
    <div class="img"><div class="ph">${esc(c.title.name)}</div>${img(pic)}<div class="play">▶</div></div>
    <div class="bar"><i style="width:${p}%"></i></div>
    <div class="body">
      <div class="name">${esc(c.title.name)}</div>
      <div class="meta"><span>${esc(epLong(m) || 'סרט')}</span>${label}</div>
    </div></a>`;
}

function posterCard(t) {
  const watchedAll = t.media_count && t.watched_count >= t.media_count;
  const p = t.media_count ? (t.watched_count / t.media_count) * 100 : 0;
  const sub = t.kind === 'series'
    ? `${t.season_count > 1 ? t.season_count + ' עונות · ' : ''}${t.media_count} פרקים`
    : (t.year || 'סרט');
  const corner = watchedAll ? '<span class="badge ok corner">✓ נצפה</span>'
    : t.watched_count ? `<span class="badge accent corner">${t.kind === 'series' ? `${t.watched_count}/${t.media_count}` : 'בצפייה'}</span>` : '';
  return `<a class="poster-card" href="#/title/${t.id}">
    <div class="poster">
      <div class="ph">${esc(t.name)}</div>${img(t.poster)}
      ${corner}
      ${p && !watchedAll ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}
    </div>
    <div class="name">${esc(t.name)}</div>
    <div class="sub">${esc(sub)}</div></a>`;
}

// -------------------------------------------------------------------- title
let editMode = false;

async function pageTitle(id) {
  $app.innerHTML = '<div class="spinner"></div>';
  const data = await api(`/api/titles/${id}${editMode ? '?hidden=true' : ''}`);
  const {title: t, media, resume} = data;
  const labels = data.season_labels || {};
  const isSeries = t.kind === 'series';

  const seasons = [...new Set(media.map((m) => m.season ?? 1))].sort((a, b) => a - b);
  const key = `season:${id}`;
  let season = +sessionStorage.getItem(key) || (resume ? (resume.media.season ?? 1) : seasons[0]);
  if (!seasons.includes(season)) season = seasons[0];

  let resumeBtn = '';
  if (resume) {
    const m = resume.media;
    const label = resume.state === 'resume' ? `המשך ${isSeries ? epLabel(m) + ' ' : ''}מ-${fmtTime(resume.position)}`
      : resume.state === 'next' ? `המשך ל${epLong(m)}`
      : resume.state === 'done' ? 'צפה שוב'
      : isSeries ? `התחל לצפות · ${epLabel(m)}` : 'צפה עכשיו';
    resumeBtn = `<a class="btn primary" href="#/watch/${m.id}">▶ ${esc(label)}</a>`;
  }
  const total = media.filter((m) => !m.hidden).length;
  const watched = media.filter((m) => !m.hidden && m.completed).length;

  $app.innerHTML = `
    <section class="hero" ${bg(t.backdrop)}>
      <a class="iconbtn hero-back" href="javascript:history.back()" aria-label="חזרה">→</a>
      <div class="hero-inner">
        <div class="poster-img"><div class="ph">${esc(t.name)}</div>${img(t.poster)}</div>
        <div>
          <h1>${esc(t.name)}</h1>
          <div class="facts">
            <span>${isSeries ? 'סדרה' : 'סרט'}</span>
            ${t.year ? `<span>· ${t.year}</span>` : ''}
            ${isSeries ? `<span>· ${seasons.length} עונות · ${total} פרקים</span>` : ''}
            ${watched ? `<span>· צפית ב-${watched}/${total}</span>` : ''}
          </div>
          ${t.overview ? `<p class="overview">${esc(t.overview)}</p>` : ''}
          <div class="actions">
            ${resumeBtn}
            <button class="btn" id="edit-toggle">${editMode ? '✓ סיום עריכה' : '✏️ עריכה'}</button>
          </div>
        </div>
      </div>
    </section>
    ${editMode ? editTitlePanel(t) : ''}
    ${isSeries && seasons.length > 1 ? `<div class="tabs" style="margin-bottom:16px">
        ${seasons.map((s) => `<button class="tab ${s === season ? 'active' : ''}" data-season="${s}">עונה ${s}${labels[s] ? ' · ' + esc(labels[s]) : ''}</button>`).join('')}
      </div>` : ''}
    <div class="episodes" id="eps"></div>`;

  const renderEps = () => {
    const list = isSeries ? media.filter((m) => (m.season ?? 1) === season) : media;
    const seen = {};
    $app.querySelector('#eps').innerHTML = list.map((m) => {
      const k = `${m.season}:${m.episode}`;
      seen[k] = (seen[k] || 0) + 1;
      return episodeRow(m, isSeries, seen[k] > 1 ? seen[k] : 0, resume && resume.media.id === m.id);
    }).join('');
  };
  renderEps();

  $app.querySelectorAll('[data-season]').forEach((b) => b.onclick = () => {
    season = +b.dataset.season;
    sessionStorage.setItem(key, season);
    $app.querySelectorAll('[data-season]').forEach((x) => x.classList.toggle('active', x === b));
    renderEps();
  });
  $app.querySelector('#edit-toggle').onclick = () => { editMode = !editMode; pageTitle(id); };

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
      pageTitle(id);
    } catch (err) { alert(err.message); }
  };

  if (editMode) bindEditTitle(t);
}

function episodeRow(m, isSeries, version, current) {
  const p = pct(m);
  const title = isSeries && m.episode != null ? `פרק ${m.episode}` : (firstLine(m.caption) || m.file_name || 'וידאו');
  const desc = [firstLine(m.caption) || m.file_name, fmtDur(m.duration), m.quality ? m.quality + 'p' : '', fmtSize(m.size),
                !editMode && m.versions > 1 ? `${m.versions} גרסאות` : '']
    .filter(Boolean).map(esc).join(' · ');
  const state = m.completed ? '<span class="badge ok">נצפה</span>'
    : m.position >= 10 ? `<span class="badge accent">עצרת ב-${fmtTime(m.position)}</span>` : '';
  return `<div class="ep ${current ? 'current' : ''} ${m.hidden ? 'is-hidden' : ''}">
    <a class="num" href="#/watch/${m.id}">${isSeries && m.episode != null ? m.episode : '▶'}</a>
    <a class="thumb" href="#/watch/${m.id}">${img(m.thumb)}${p ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}</a>
    <a class="info" href="#/watch/${m.id}">
      <div class="t">${esc(title)} ${version ? `<span class="badge">גרסה ${version}</span>` : ''} ${state}</div>
      <div class="c">${desc}</div>
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
  const {media: m, title: t, prev, next, versions} = await api(`/api/media/${id}`);
  const isSeries = t.kind === 'series' && m.episode != null;
  document.title = `${t.name}${isSeries ? ' · ' + epLabel(m) : ''}`;

  $app.innerHTML = `<div class="watch">
    <div class="player-wrap" id="pw">
      <video id="v" controls playsinline preload="metadata" src="/api/stream/${m.id}"></video>
    </div>
    <div class="watch-head">
      <div>
        <h1><a href="#/title/${t.id}">${esc(t.name)}</a></h1>
        <div class="muted">${esc(isSeries ? epLong(m) : '')} ${esc(firstLine(m.caption) && isSeries ? '· ' + firstLine(m.caption) : '')}</div>
      </div>
      <div class="watch-controls">
        ${prev ? `<a class="btn" href="#/watch/${prev.id}" title="פרק קודם">→ ${esc(epLabel(prev) || 'הקודם')}</a>` : ''}
        <button class="btn" id="back10" title="10 שניות אחורה">⟲ 10 שנ׳</button>
        <button class="btn" id="fwd10" title="10 שניות קדימה">10 שנ׳ ⟳</button>
        ${next ? `<a class="btn primary" href="#/watch/${next.id}" title="הפרק הבא (N)">${esc(epLabel(next) || 'הבא')} ←</a>` : ''}
      </div>
    </div>
    ${versions && versions.length > 1 ? `<div class="versions"><span class="muted">איכות:</span>
      ${versions.map((x) => `<a class="tab ${x.id === m.id ? 'active' : ''}" href="#/watch/${x.id}">${x.quality ? x.quality + 'p' : '?'} · ${fmtSize(x.size)}</a>`).join('')}
    </div>` : ''}
    <p class="muted" style="font-size:13px">קיצורי מקלדת: רווח = עצור/נגן · חיצים = 10 שניות · F = מסך מלא · N = הפרק הבא</p>
  </div>`;

  const v = $app.querySelector('#v');
  const wrap = $app.querySelector('#pw');
  let lastSaved = -1, timer = null, nextTimer = null, finished = false;

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

  // resume where we stopped
  v.addEventListener('loadedmetadata', () => {
    const pos = m.position || 0;
    if (!m.completed && pos > 10 && pos < v.duration - 20) {
      v.currentTime = pos;
      const toast = document.createElement('div');
      toast.className = 'toast';
      toast.innerHTML = `ממשיך מ-${fmtTime(pos)} <button class="btn small" id="restart">מההתחלה</button>`;
      wrap.appendChild(toast);
      toast.querySelector('#restart').onclick = () => { v.currentTime = 0; toast.remove(); v.play(); };
      setTimeout(() => toast.remove(), 8000);
    }
    v.play().catch(() => {});
  }, {once: true});

  v.addEventListener('play', () => { timer = timer || setInterval(() => save(false), 10000); });
  v.addEventListener('pause', () => save(true));
  v.addEventListener('seeked', () => save(true));
  v.addEventListener('ended', () => {
    finished = true;
    api(`/api/progress/${m.id}`, {method: 'POST', body: {position: v.duration, duration: v.duration}}).catch(() => {});
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
    o.className = 'overlay';
    o.innerHTML = `<div class="box"><div class="muted">הפרק הבא</div><h2>${esc(epLong(next))}</h2>
      <div class="count" id="cd">${n}</div>
      <div style="display:flex;gap:8px;justify-content:center">
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

  $app.querySelector('#back10').onclick = () => { v.currentTime = Math.max(0, v.currentTime - 10); };
  $app.querySelector('#fwd10').onclick = () => { v.currentTime += 10; };

  const onKey = (e) => {
    if (e.target.matches('input, textarea, select, video')) return;
    const k = e.key.toLowerCase();
    if (k === ' ' || k === 'k') { e.preventDefault(); v.paused ? v.play() : v.pause(); }
    else if (k === 'arrowright') { e.preventDefault(); v.currentTime += 10; }
    else if (k === 'arrowleft') { e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 10); }
    else if (k === 'f' || k === 'כ') { document.fullscreenElement ? document.exitFullscreen() : wrap.requestFullscreen?.(); }
    else if ((k === 'n' || k === 'מ') && next) { location.hash = `#/watch/${next.id}`; }
  };
  const onHide = () => { if (document.visibilityState === 'hidden') beacon(); };
  document.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', onHide);
  window.addEventListener('pagehide', beacon);

  cleanup = () => {
    if (!finished) beacon();
    clearInterval(timer);
    clearInterval(nextTimer);
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('visibilitychange', onHide);
    window.removeEventListener('pagehide', beacon);
    v.pause();
    v.removeAttribute('src');
    v.load(); // stop downloading
    document.title = 'הספרייה שלי';
    libraryCache = null;
  };
}

// ----------------------------------------------------------------- settings
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

async function pageSettings() {
  $app.innerHTML = '<div class="spinner"></div>';
  const st = await api('/api/status');
  const auth = st.telegram_authorized;

  $app.innerHTML = `
    ${pageHeader("עוד", "הגדרות, חיבורים וקבוצות")}
    <div class="panel"><h3>חיבור לטלגרם</h3>${telegramPanel(st)}</div>
    <div class="panel" ${auth ? '' : 'hidden'}><h3>קבוצות וערוצים</h3>
      <p class="muted" style="margin-top:0">סמן את הקבוצות שמהן ייאספו הסדרות והסרטים.</p>
      <div id="chats">${auth ? '<div class="spinner"></div>' : ''}</div>
    </div>
    <div class="panel" ${auth ? '' : 'hidden'}><h3>סריקה</h3>
      <div id="scan-status" class="muted"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
        <button class="btn primary" id="scan">סרוק עכשיו</button>
        <button class="btn" id="scan-full">סריקה מלאה מההתחלה</button>
      </div>
      <p class="muted" style="font-size:13px;margin-bottom:0">כל עוד האתר פועל, הוא סורק לבד ${st.scan_interval_minutes ? `כל ${st.scan_interval_minutes} דקות` : ''} ובכל הפעלה. סריקה מלאה לא מוחקת עריכות שעשית.</p>
    </div>
    <div class="panel"><h3>ניקוי שמות</h3>
      <p class="muted" style="margin-top:0">האתר מזהה לבד חתימות של קבוצות (כמו "קינג סרט") ומוחק אותן משמות הסרטים, כדי שכל סרט יופיע פעם אחת. אם נשארה חתימה, כתוב אותה כאן, <b>שורה לכל ביטוי</b>, והיא תימחק מכל השמות.</p>
      <textarea class="input" id="strip-phrases" rows="5" style="width:100%;resize:vertical" placeholder="קינג סרט&#10;מדיה VOD&#10;פופקורן טיים">${esc((st.strip_phrases || []).join('\n'))}</textarea>
      <button class="btn" id="save-phrases" style="margin-top:8px">שמור וסדר מחדש</button>
    </div>
    <div class="panel"><h3>פוסטרים ותקצירים (רשות)</h3>
      <p class="muted" style="margin-top:0">${st.tmdb ? '✓ פעיל.' + (st.tmdb_status?.running ? ` ⏳ מוריד פוסטרים ותקצירים: ${st.tmdb_status.done.toLocaleString('he-IL')} מתוך ${st.tmdb_status.total.toLocaleString('he-IL')} (ממשיך ברקע)` : '') : 'כדי לקבל פוסטרים ותקצירים בעברית, הירשם בחינם ל-<a class="link" href="https://www.themoviedb.org/signup" target="_blank" rel="noopener">TMDB</a>, ואז ב-<a class="link" href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener">הגדרות ← API</a> העתק את ה-<b>API Key</b>.'}</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="input" id="tmdb-key" dir="ltr" autocomplete="off" placeholder="${st.tmdb ? 'להחלפה: הדבק מפתח חדש' : 'API Key'}" style="flex:1;min-width:180px">
        <button class="btn" id="save-tmdb">שמור</button>
      </div>
    </div>
    <div class="panel"><h3>סיסמת האתר</h3>
      <div class="form-grid">
        <input class="input" id="pw-cur" type="password" placeholder="סיסמה נוכחית">
        <input class="input" id="pw-new" type="password" placeholder="סיסמה חדשה">
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" id="change-pw">שנה סיסמה</button>
        <button class="btn danger" id="logout">יציאה מהאתר</button>
      </div>
    </div>`;

  const $ = (sel) => $app.querySelector(sel);
  const run = async (fn, errEl) => {
    if (errEl) errEl.textContent = '';
    try { await fn(); } catch (e) { if (errEl) errEl.textContent = e.message; else alert(e.message); }
  };

  // --- telegram connect flow
  $('#save-api')?.addEventListener('click', () => run(async () => {
    await api('/api/settings', {method: 'POST', body: {api_id: $('#api-id').value, api_hash: $('#api-hash').value.trim()}});
    pageSettings();
  }));
  $('#reset-api')?.addEventListener('click', () => {
    $('.panel').innerHTML = '<h3>חיבור לטלגרם</h3>' + telegramPanel({...st, telegram_configured: false});
    $('#save-api').onclick = () => run(async () => {
      await api('/api/settings', {method: 'POST', body: {api_id: $('#api-id').value, api_hash: $('#api-hash').value.trim()}});
      pageSettings();
    });
  });
  const tgErr = $('#tg-err');
  const show = (step) => ['phone', 'code', 'pass'].forEach((s) => { const el = $(`#tg-step-${s}`); if (el) el.hidden = s !== step; });
  const afterLogin = (r) => {
    if (r.need_password) { show('pass'); $('#tg-pass').focus(); } else pageSettings();
  };
  $('#tg-send')?.addEventListener('click', () => run(async () => {
    $('#tg-send').disabled = true;
    try { await api('/api/tg/send-code', {method: 'POST', body: {phone: $('#tg-phone').value}}); } finally { $('#tg-send').disabled = false; }
    show('code'); $('#tg-code').focus();
  }, tgErr));
  $('#tg-code-go')?.addEventListener('click', () => run(async () => {
    afterLogin(await api('/api/tg/sign-in', {method: 'POST', body: {code: $('#tg-code').value}}));
  }, tgErr));
  $('#tg-pass-go')?.addEventListener('click', () => run(async () => {
    afterLogin(await api('/api/tg/sign-in', {method: 'POST', body: {password: $('#tg-pass').value}}));
  }, tgErr));
  $('#tg-logout')?.addEventListener('click', () => confirm('להתנתק מחשבון הטלגרם? הספרייה ומעקב הצפייה יישמרו.') &&
    run(async () => { await api('/api/tg/logout', {method: 'POST'}); pageSettings(); }));

  // --- other settings
  $('#save-tmdb').onclick = () => run(async () => {
    await api('/api/settings', {method: 'POST', body: {tmdb_api_key: $('#tmdb-key').value.trim()}});
    if (auth) await api('/api/scan', {method: 'POST', body: {}});
    pageSettings();
  });
  $('#save-phrases').onclick = () => run(async () => {
    await api('/api/settings', {method: 'POST', body: {strip_phrases: $('#strip-phrases').value}});
    libraryCache = null;
    alert('נשמר! השמות מסודרים מחדש ברקע. בעוד דקה-שתיים זה יופיע בספרייה.');
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
    el.innerHTML = s.running
      ? `⏳ סורק${s.chat ? ': ' + esc(s.chat) : '...'} · נוספו ${s.added}`
      : `סריקה אחרונה: ${when}${s.added ? ` · נוספו ${s.added}` : ''}${s.error ? `<div class="error-box" style="margin-top:8px">${esc(s.error)}</div>` : ''}`;
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
