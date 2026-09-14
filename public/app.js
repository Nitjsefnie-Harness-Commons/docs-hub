// docs-hub SPA — hash-routed screens, live backend data.
// Routes:
//   #/                  → index
//   #/d/<slug>          → doc viewer (chrome + iframe)
//   #/d/<slug>/v<n>     → doc viewer at specific version
//   #/d/<slug>/versions → version history

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// A slug reaches this file from the URL hash, so it is attacker-controllable
// even though the server only ever mints slugs matching [a-z0-9/_-]. Encode it
// per SEGMENT — '/' is a legitimate separator inside a slug — before putting it
// in a URL, so a crafted one cannot append a query string or a fragment to a
// request the reader's browser makes with the reader's cookie.
const apiSlug = (slug) => String(slug).split('/').map(encodeURIComponent).join('/');

// ─────────────────────────── data layer ───────────────────────────

let _docs = null;        // cached GET /api/list
const _versions = new Map();  // slug → GET /api/versions/<slug>
                              // a Map, not an object literal: the slug comes
                              // from the URL hash, and `obj[slug] = …` with a
                              // slug of `__proto__` writes the prototype.
let _whoami = null;      // cached GET /api/whoami
let _lastSig = null;     // signature of the currently-rendered screen (poll gate)

async function fetchJSON(url, retries = 2) {
  // Retry transient failures (server restart races, momentary network blips)
  // before bubbling. 5xx retries; 4xx falls through immediately.
  let lastErr;
  for (let i = 0; i <= retries; i++) {
    try {
      const r = await fetch(url, { headers: { accept: 'application/json' } });
      if (r.ok) return r.json();
      if (r.status >= 500 && i < retries) {
        lastErr = new Error(url + ' → ' + r.status);
      } else {
        throw new Error(url + ' → ' + r.status);
      }
    } catch (e) {
      lastErr = e;
      if (i === retries) throw e;
    }
    await new Promise((res) => setTimeout(res, 250 * (i + 1)));
  }
  throw lastErr;
}

async function loadDocs(force = false) {
  if (_docs === null || force) {
    const j = await fetchJSON('/api/list');
    _docs = j.docs || [];
  }
  return _docs;
}

async function loadVersions(slug, force = false) {
  if (!_versions.has(slug) || force) {
    try {
      const j = await fetchJSON('/api/versions/' + apiSlug(slug));
      _versions.set(slug, j.versions || []);
    } catch {
      // On a forced refresh failure, keep whatever we already had rather
      // than clobbering it to []. Only seed [] on a true first-load failure.
      if (!_versions.has(slug)) _versions.set(slug, []);
    }
  }
  return _versions.get(slug);
}

async function loadWhoami() {
  if (_whoami === null) {
    try { _whoami = await fetchJSON('/api/whoami'); }
    catch { _whoami = { user_id: null }; }
  }
  return _whoami;
}

// ─────────────────────────── utilities ───────────────────────────

function fmtBytes(n) {
  if (n < 1024) return n + ' b';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' kb';
  return (n / (1024 * 1024)).toFixed(2) + ' mb';
}

function relTime(iso) {
  const t = new Date(iso).getTime();
  const now = Date.now();
  const d = Math.max(0, now - t);
  const mins = d / 60_000;
  if (mins < 1) return 'just now';
  if (mins < 60) return Math.round(mins) + 'm ago';
  const hours = mins / 60;
  if (hours < 24) return Math.round(hours) + 'h ago';
  const days = hours / 24;
  if (days < 14) return Math.round(days) + 'd ago';
  if (days < 60) return Math.round(days / 7) + 'w ago';
  return Math.round(days / 30) + 'mo ago';
}

function relUntil(iso) {
  const d = new Date(iso).getTime() - Date.now();
  if (d <= 0) return 'expired';
  const mins = d / 60_000;
  if (mins < 1) return 'in <1m';
  if (mins < 60) return 'in ' + Math.round(mins) + 'm';
  const hours = mins / 60;
  if (hours < 24) return 'in ' + Math.round(hours) + 'h';
  const days = hours / 24;
  if (days < 14) return 'in ' + Math.round(days) + 'd';
  return 'in ' + Math.round(days / 7) + 'w';
}

function absTime(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return (
    d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) +
    ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes())
  );
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function uniq(arr) { return [...new Set(arr)]; }

// ─────────────────────────── filter state ───────────────────────────

const FILTER = {
  q: '', project: '', agent: '', tag: '',
  sort: 'updated', sortDir: 'desc',
};

const SORT_DEFAULT_DIR = {
  title: 'asc', slug: 'asc', project: 'asc', tags: 'desc',
  agent: 'asc', updated: 'desc', version: 'desc', size: 'desc',
};

function setSort(key) {
  if (FILTER.sort === key) {
    FILTER.sortDir = FILTER.sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    FILTER.sort = key;
    FILTER.sortDir = SORT_DEFAULT_DIR[key] || 'desc';
  }
}

function applyFilters(docs) {
  let out = docs.slice();
  if (FILTER.q) {
    const q = FILTER.q.toLowerCase();
    out = out.filter(
      (d) => d.title.toLowerCase().includes(q) || d.slug.toLowerCase().includes(q),
    );
  }
  if (FILTER.project) out = out.filter((d) => d.project === FILTER.project);
  if (FILTER.agent)   out = out.filter((d) => d.posted_by === FILTER.agent);
  if (FILTER.tag)     out = out.filter((d) => d.tags.includes(FILTER.tag));

  const dir = FILTER.sortDir === 'asc' ? 1 : -1;
  const sizeOf = (d) => d.byte_size ?? 0;
  const cmp = {
    title:   (a, b) => a.title.localeCompare(b.title),
    slug:    (a, b) => a.slug.localeCompare(b.slug),
    project: (a, b) => (a.project || '').localeCompare(b.project || ''),
    tags:    (a, b) => a.tags.length - b.tags.length,
    agent:   (a, b) => a.posted_by.localeCompare(b.posted_by),
    updated: (a, b) => new Date(a.updated_at) - new Date(b.updated_at),
    version: (a, b) => a.latest_version - b.latest_version,
    size:    (a, b) => sizeOf(a) - sizeOf(b),
  }[FILTER.sort] || (() => 0);
  out.sort((a, b) => dir * cmp(a, b));
  return out;
}

// ─────────────────────────── screens ───────────────────────────

function renderTopbar(opts = {}) {
  const crumbs = opts.crumbs || [];
  const crumbHtml = crumbs.length
    ? crumbs
        .map((c, i) => {
          const piece = c.href
            ? `<a href="${esc(c.href)}">${esc(c.label)}</a>`
            : `<span class="here">${esc(c.label)}</span>`;
          return (i ? '<span class="sep">/</span>' : '') + piece;
        })
        .join('')
    : '';
  const who = esc(_whoami && _whoami.user_id != null ? _whoami.user_id : '—');
  return `
    <header class="topbar">
      <a href="#/" class="brand" style="color:inherit">
        <span class="prompt">$</span>
        <span class="host">docs.nitjsefni.eu</span>
        <span class="cursor"></span>
      </a>
      <div class="crumbs">${crumbHtml}</div>
      <div class="meta">
        <span>user <span class="who">${who}</span></span>
        <span class="dot">·</span>
        <a href="/logout" title="Sign out">logout</a>
      </div>
    </header>
  `;
}

function renderIndex() {
  const docs = applyFilters(_docs);
  const total = _docs.length;
  const allProjects = uniq(_docs.map((d) => d.project).filter(Boolean)).sort();
  const allAgents   = uniq(_docs.map((d) => d.posted_by)).sort();
  const allTags     = uniq(_docs.flatMap((d) => d.tags)).sort();

  const opt = (val, label, sel) =>
    `<option value="${esc(val)}" ${val === sel ? 'selected' : ''}>${esc(label)}</option>`;

  const headers = [
    { key: 'title',   label: 'Title / Slug' },
    { key: 'project', label: 'Project' },
    { key: 'tags',    label: 'Tags' },
    { key: 'agent',   label: 'Agent' },
    { key: 'updated', label: 'Updated' },
    { key: 'version', label: 'Ver' },
    { key: 'size',    label: 'Size', align: 'right' },
  ];

  const headHtml = headers
    .map((h) => {
      const active = FILTER.sort === h.key;
      const glyph = active ? (FILTER.sortDir === 'asc' ? '▲' : '▼') : '▼';
      const cls = active ? '' : 'inactive';
      const style = h.align === 'right' ? 'text-align:right' : '';
      const titleAttr = active
        ? `title="sort by ${h.label.toLowerCase()} — reverse"`
        : `title="sort by ${h.label.toLowerCase()}"`;
      return `<th class="${cls}" style="${style}" ${titleAttr} data-sort="${h.key}">${esc(h.label)}<span class="arrow">${glyph}</span></th>`;
    })
    .join('');

  const rowHtml = docs.length
    ? docs
        .map((d) => {
          const size = d.byte_size ? fmtBytes(d.byte_size) : '';
          return `
            <tr>
              <td class="title-cell">
                <a class="t" href="#/d/${esc(d.slug)}">${esc(d.title)}</a>
                <div class="slug"><span class="prompt">›</span>${esc(d.slug)}${d.expires_at ? `<span class="ttl" title="expires ${esc(absTime(d.expires_at))} UTC">⏳ ${esc(relUntil(d.expires_at))}</span>` : ''}</div>
              </td>
              <td class="proj">${esc(d.project || '—')}</td>
              <td class="tags">${d.tags.map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('')}</td>
              <td class="agent">${esc(d.posted_by)}</td>
              <td class="upd">
                <span class="rel">${relTime(d.updated_at)}</span>
                <span class="abs">${absTime(d.updated_at)}</span>
              </td>
              <td class="ver">
                <a class="v" href="#/d/${esc(d.slug)}">v${d.latest_version}</a>
                <a class="hist" href="#/d/${esc(d.slug)}/versions" title="version history">log →</a>
              </td>
              <td class="size tnum">${size}</td>
            </tr>
          `;
        })
        .join('')
    : `<tr class="empty"><td colspan="${headers.length}">no docs match these filters · <a href="#" id="clear-filters">clear all</a></td></tr>`;

  return `
    ${renderTopbar({ crumbs: [{ label: 'index' }] })}
    <main class="main">
      <div class="filterbar">
        <div class="field">
          <span class="label">search</span>
          <input id="f-q" placeholder="title or slug…" value="${esc(FILTER.q)}" autocomplete="off">
        </div>
        <div class="field select">
          <span class="label">project</span>
          <select id="f-project">
            <option value="">all</option>
            ${allProjects.map((p) => opt(p, p, FILTER.project)).join('')}
          </select>
        </div>
        <div class="field select">
          <span class="label">agent</span>
          <select id="f-agent">
            <option value="">all</option>
            ${allAgents.map((a) => opt(a, a, FILTER.agent)).join('')}
          </select>
        </div>
        <div class="field select">
          <span class="label">tag</span>
          <select id="f-tag">
            <option value="">all</option>
            ${allTags.map((t) => opt(t, '#' + t, FILTER.tag)).join('')}
          </select>
        </div>
        <div class="count"><b class="accent">${docs.length}</b> / ${total} docs</div>
      </div>
      <table class="docs-table">
        <thead><tr>${headHtml}</tr></thead>
        <tbody>${rowHtml}</tbody>
      </table>
    </main>
  `;
}

function wireIndex(searchWasFocused) {
  const reroute = () => render();
  $('#f-q')?.addEventListener('input', (e) => { FILTER.q = e.target.value; reroute(); });
  $('#f-project')?.addEventListener('change', (e) => { FILTER.project = e.target.value; reroute(); });
  $('#f-agent')?.addEventListener('change', (e) => { FILTER.agent = e.target.value; reroute(); });
  $('#f-tag')?.addEventListener('change', (e) => { FILTER.tag = e.target.value; reroute(); });
  $$('[data-sort]').forEach((th) =>
    th.addEventListener('click', () => {
      setSort(th.dataset.sort);
      reroute();
    }),
  );
  $$('.docs-table .tag[data-tag]').forEach((el) =>
    el.addEventListener('click', () => {
      FILTER.tag = FILTER.tag === el.dataset.tag ? '' : el.dataset.tag;
      reroute();
    }),
  );
  $('#clear-filters')?.addEventListener('click', (e) => {
    e.preventDefault();
    Object.assign(FILTER, { q: '', project: '', agent: '', tag: '' });
    reroute();
  });
  if (searchWasFocused) {
    const inp = $('#f-q');
    if (inp) {
      inp.focus();
      inp.setSelectionRange(inp.value.length, inp.value.length);
    }
  }
}

function renderDocViewer(slug, requestedVersion) {
  const doc = (_docs || []).find((d) => d.slug === slug);
  if (!doc) return renderNotFound('/d/' + slug);

  const versions = _versions.get(slug) || [];
  const latest = doc.latest_version;
  const ver = requestedVersion ?? latest;
  const v = versions.find((x) => x.version === ver);
  if (!v) return renderNotFound('/d/' + slug + '/v' + ver);

  const isStale = ver !== latest;
  const idx = versions.findIndex((x) => x.version === ver);
  const newer = versions[idx - 1];
  const older = versions[idx + 1];

  const crumbs = [
    { label: 'index', href: '#/' },
    { label: doc.project || '—' },
    { label: slug.split('/').slice(1).join('/') || slug, href: '#/d/' + slug },
  ];

  return `
    ${renderTopbar({ crumbs })}
    <div class="docview">
      <div class="docchrome">
        <div class="left">
          <div class="title-row">
            <div class="title">${esc(doc.title)}</div>
            <div class="vtag ${isStale ? 'stale stale-warn' : ''}">v${v.version}</div>
          </div>
          <div class="meta">
            <span class="slug"><span class="prompt">›</span>${esc(slug)}</span>
            <span class="dot">·</span>
            <span>by <span class="agent">${esc(v.posted_by)}</span></span>
            <span class="dot">·</span>
            <span>${relTime(v.created_at)} · ${absTime(v.created_at)}</span>
            <span class="dot">·</span>
            <span>${fmtBytes(v.byte_size)}</span>
            <span class="dot">·</span>
            <span title="${esc(v.sha256)}">sha ${esc(v.sha256.slice(0, 10))}…</span>
          </div>
        </div>
        <div class="actions">
          <div class="nav-arrows">
            <button ${older ? `onclick="location.hash='#/d/${esc(slug)}/v${older.version}'"` : 'disabled'} title="older version">←</button>
            <span class="which">v${v.version} / v${latest}</span>
            <button ${newer ? `onclick="location.hash='#/d/${esc(slug)}/v${newer.version}'"` : 'disabled'} title="newer version">→</button>
          </div>
          ${isStale ? `<a class="btn accent" href="#/d/${esc(slug)}">latest →</a>` : ''}
          <a class="btn" href="#/d/${esc(slug)}/versions">log <span class="kbd">L</span></a>
          <button class="btn" id="copy-link" title="copy permalink">⎘ link</button>
          <button class="btn ${doc.public ? 'accent' : ''}" id="toggle-public"
                  title="${doc.public ? 'public — anyone can read /d/' + esc(slug) + ' without logging in; click to make private' : 'private — click to let anyone read /d/' + esc(slug)}">
            ${doc.public ? '◉ public' : '○ private'}
          </button>
          ${doc.public ? `<button class="btn" id="copy-public" title="copy public direct URL">⎘ public url</button>` : ''}
          ${doc.expires_at ? `<span class="ttl" title="expires ${esc(absTime(doc.expires_at))} UTC">⏳ expires ${esc(relUntil(doc.expires_at))}</span>` : ''}
        </div>
      </div>
      <div class="artifact-frame">
        <iframe id="artifact" sandbox="allow-scripts allow-same-origin" src="/d/${esc(apiSlug(slug))}/v${v.version}"></iframe>
      </div>
    </div>
  `;
}

function wireDocViewer(slug) {
  $('#copy-link')?.addEventListener('click', (e) => {
    const btn = e.currentTarget;
    const url = location.href;
    if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => {});
    const old = btn.innerHTML;
    btn.innerHTML = '✓ copied';
    setTimeout(() => (btn.innerHTML = old), 1200);
  });
  $('#copy-public')?.addEventListener('click', (e) => {
    const btn = e.currentTarget;
    const url = location.origin + '/d/' + apiSlug(slug);
    if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => {});
    const old = btn.innerHTML;
    btn.innerHTML = '✓ copied';
    setTimeout(() => (btn.innerHTML = old), 1200);
  });
  $('#toggle-public')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const doc = (_docs || []).find((d) => d.slug === slug);
    if (!doc) return;
    btn.disabled = true;
    try {
      const r = await fetch('/api/doc/' + apiSlug(slug) + '/public', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ public: !doc.public }),
      });
      if (r.ok) {
        await loadDocs(true);   // drop the stale flag from the cached list
        render();
      }
    } finally {
      btn.disabled = false;
    }
  });
  const iframe = $('#artifact');
  if (iframe) {
    iframe.addEventListener('load', () => {
      try {
        const t = iframe.contentDocument && iframe.contentDocument.title;
        if (t) document.title = t + ' · docs';
      } catch { /* cross-origin fallback: keep the slug title */ }
    });
  }
  document.addEventListener('keydown', _docKeydown);
}
const _docKeydown = (e) => {
  if (e.target.matches('input, textarea, select')) return;
  if (e.key === 'l' || e.key === 'L') {
    const m = location.hash.match(/^#\/d\/(.+?)(?:\/v\d+|\/versions)?$/);
    if (m) location.hash = '#/d/' + m[1] + '/versions';
  }
};

function renderVersions(slug) {
  const doc = (_docs || []).find((d) => d.slug === slug);
  if (!doc) return renderNotFound('/d/' + slug + '/versions');
  const versions = _versions.get(slug) || [];
  const latest = doc.latest_version;

  const crumbs = [
    { label: 'index', href: '#/' },
    { label: doc.project || '—' },
    { label: slug.split('/').slice(1).join('/') || slug, href: '#/d/' + slug },
    { label: 'versions' },
  ];

  const rows = versions
    .map(
      (v) => `
      <div class="version-row" data-href="#/d/${esc(slug)}/v${v.version}">
        <div class="vtag ${v.version === latest ? 'latest' : ''}">v${v.version}</div>
        <div>
          <div class="who">${esc(v.posted_by)}</div>
          <div class="sha" title="${esc(v.sha256)}">sha ${esc(v.sha256.slice(0, 16))}…</div>
        </div>
        <div class="when">
          ${relTime(v.created_at)}
          <span class="abs">${absTime(v.created_at)}</span>
        </div>
        <div class="size tnum">${fmtBytes(v.byte_size)}</div>
        <div class="sha tnum">${esc(v.sha256.slice(0, 8))}</div>
        <div class="actions">
          <button class="render-btn">render →</button>
        </div>
      </div>
    `,
    )
    .join('');

  return `
    ${renderTopbar({ crumbs })}
    <main class="main">
      <div class="version-head">
        <div>
          <h1>${esc(doc.title)}</h1>
          <div class="sub"><span class="prompt">›</span>${esc(slug)} · ${versions.length} version${versions.length === 1 ? '' : 's'}</div>
        </div>
        <div class="version-actions">
          <a class="btn" href="#/d/${esc(slug)}">view latest →</a>
          <a class="btn" href="#/">← back to index</a>
        </div>
      </div>
      <div class="version-list">${rows}</div>
    </main>
  `;
}

function wireVersions() {
  $$('.version-row').forEach((row) =>
    row.addEventListener('click', () => {
      location.hash = row.dataset.href;
    }),
  );
}

function renderNotFound(path) {
  return `
    ${renderTopbar({ crumbs: [{ label: '404' }] })}
    <div class="notfound">
      <div>
        <div class="code">4<span class="slash">/</span>4</div>
        <div class="path"><span class="prompt">›</span>${esc(path || location.hash.slice(1) || '/')}</div>
        <h2>No artifact under that slug.</h2>
        <p>The slug doesn't resolve to any published document, or that specific version doesn't exist yet. Re-publish from the agent CLI, or pick a different slug from the index.</p>
        <div class="actions">
          <a class="btn accent" href="#/">← back to index</a>
        </div>
      </div>
    </div>
  `;
}

function renderError(err) {
  return `
    ${renderTopbar({ crumbs: [{ label: 'error' }] })}
    <div class="notfound">
      <div>
        <div class="code">5<span class="slash">/</span>0<span class="slash">/</span>0</div>
        <h2>Couldn't load from the server.</h2>
        <p>${esc(String((err && err.message) || err))}</p>
        <div class="actions">
          <a class="btn accent" href="#/">← back to index</a>
        </div>
      </div>
    </div>
  `;
}

// ─────────────────────────── poll signatures ───────────────────────────
// A cheap deterministic string of the SERVER-DRIVEN state each screen
// depends on, so the poll loop re-renders only when that data changes.
// Local-only state (the index filter/sort in FILTER) is intentionally NOT
// included — the poll never changes it, so it must not trigger a re-render.

function sigIndex() {
  // '|' / '\n' are safe delimiters: slugs and ISO timestamps never contain them.
  return (_docs || [])
    .map((d) => d.slug + '|' + d.latest_version + '|' + d.updated_at +
                '|' + (d.expires_at || ''))
    .join('\n');
}

function sigVersions(slug) {
  return (_versions.get(slug) || [])
    .map((v) => v.version + '|' + v.sha256)
    .join('\n');
}

// The viewer keys off the ONE version on screen, not the whole list. A route
// pinned to /v<n> resolves to n (never changes on publish → no reload); a bare
// slug resolves to latest (bumps when a new version lands → iframe swaps).
function sigViewer(route) {
  const doc = (_docs || []).find((d) => d.slug === route.slug);
  // version is 1-based; ?? is safe here (0 never occurs as a pinned version).
  // The public flag is part of the signature so a poll picks up toggles.
  return String(route.version ?? (doc && doc.latest_version) ?? '?') +
    '|' + (doc && doc.public ? 'pub' : 'priv');
}

function currentSig(route) {
  if (route.name === 'index') return sigIndex();
  if (route.name === 'versions') return sigVersions(route.slug);
  if (route.name === 'viewer') return sigViewer(route);
  return null;
}

// ─────────────────────────── router ───────────────────────────

function parseRoute() {
  const h = location.hash.replace(/^#/, '') || '/';
  if (h === '/' || h === '') return { name: 'index' };
  if (h === '/404') return { name: '404' };
  let m = h.match(/^\/d\/(.+?)\/versions$/);
  if (m) return { name: 'versions', slug: m[1] };
  m = h.match(/^\/d\/(.+?)\/v(\d+)$/);
  if (m) return { name: 'viewer', slug: m[1], version: Number(m[2]) };
  m = h.match(/^\/d\/(.+)$/);
  if (m) return { name: 'viewer', slug: m[1], version: null };
  return { name: '404' };
}

async function render() {
  document.removeEventListener('keydown', _docKeydown);
  const route = parseRoute();
  const root = $('#app');
  // No initializer: every branch below, including the catch, assigns it.
  let html;
  try {
    await loadWhoami();
    if (route.name === 'index') {
      await loadDocs();
      html = renderIndex();
    } else if (route.name === 'versions') {
      await loadDocs();
      await loadVersions(route.slug);
      html = renderVersions(route.slug);
    } else if (route.name === 'viewer') {
      await loadDocs();
      await loadVersions(route.slug);
      html = renderDocViewer(route.slug, route.version);
    } else {
      html = renderNotFound();
    }
  } catch (e) {
    html = renderError(e);
  }
  const searchWasFocused =
    !!(document.activeElement && document.activeElement.id === 'f-q');
  root.innerHTML = html;
  switch (route.name) {
    case 'index':    wireIndex(searchWasFocused); break;
    case 'versions': wireVersions(); break;
    case 'viewer':   wireDocViewer(route.slug); break;
  }
  document.title =
    ({
      index: 'docs.nitjsefni.eu',
      versions: 'versions · docs',
      viewer: (route.slug || '') + ' · docs',
      '404': '404 · docs',
    })[route.name] || 'docs.nitjsefni.eu';
  _lastSig = currentSig(route);
}

// ─────────────────────────── poll loop ───────────────────────────
// One global, route-aware timer. Each tick force-refreshes the current
// screen's data and re-renders only when its signature changed. Pauses
// while the tab is hidden; fires once immediately when the tab is reshown.

const POLL_MS = 30_000;

function indexFilterFocused() {
  const el = document.activeElement;
  return !!(el && el.closest && el.closest('.filterbar'));
}

function maybeRerender(sig) {
  if (sig === _lastSig) return;                 // signature gate: nothing changed
  if (parseRoute().name === 'index' && indexFilterFocused()) return; // defer mid-filter
  render();                                     // render() re-stamps _lastSig
}

async function pollTick() {
  if (document.visibilityState === 'hidden') return;
  const route = parseRoute();
  try {
    if (route.name === 'index') {
      await loadDocs(true);
      maybeRerender(sigIndex());
    } else if (route.name === 'viewer') {
      await loadDocs(true);                      // refreshes latest_version
      await loadVersions(route.slug, true);      // refreshes the new version object
      maybeRerender(sigViewer(route));           // resolved DISPLAYED version only
    } else if (route.name === 'versions') {
      await loadDocs(true);
      await loadVersions(route.slug, true);
      maybeRerender(sigVersions(route.slug));
    }
  } catch {
    // Swallow — a failed tick is skipped, the next one recovers. Never tear
    // the screen into the error view over a transient blip.
  }
}

function startPolling() {
  setInterval(pollTick, POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') pollTick();
  });
}

window.addEventListener('hashchange', render);
window.addEventListener('DOMContentLoaded', render);
window.addEventListener('DOMContentLoaded', startPolling);
