/* ============================================================
   THE CASE-LAW REVIEW — application
   Renders data/cases.json into a calm master list + routed
   detail view. Hash routing, sticky filters, incremental
   rendering, and a strict inline-HTML sanitizer.

   The daily pipeline edits ONLY data/cases.json. This file
   never hard-codes a case.
   ============================================================ */
(function () {
  'use strict';

  var DATA_URL = 'data/cases.json';
  var BATCH = 24;                 // rows rendered per scroll batch
  var LS_KEY = 'clr.filters.v1';  // persisted filter state

  /* ---------- tiny DOM builder ---------- */
  function h(tag, props) {
    var node = document.createElement(tag);
    var p = props || {};
    for (var k in p) {
      if (!Object.prototype.hasOwnProperty.call(p, k)) continue;
      var v = p[k];
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;            // caller guarantees sanitized
      else if (k === 'dataset') { for (var d in v) node.dataset[d] = v[d]; }
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v);
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  function append(node, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) { for (var i = 0; i < child.length; i++) append(node, child[i]); return; }
    node.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
  }
  function frag() { return document.createDocumentFragment(); }

  /* ---------- security helpers ---------- */
  var ALLOWED = { B: 1, STRONG: 1, I: 1, EM: 1, BR: 1 };

  // Allow a tiny inline tag set; strip everything else (incl. attributes,
  // comments, scripts). Unknown elements are unwrapped to their text.
  function sanitizeInline(input) {
    var tpl = document.createElement('template');
    tpl.innerHTML = input == null ? '' : String(input);
    (function walk(parent) {
      var kids = Array.prototype.slice.call(parent.childNodes);
      for (var i = 0; i < kids.length; i++) {
        var n = kids[i];
        if (n.nodeType === 1) {                 // element
          if (ALLOWED[n.tagName]) {
            while (n.attributes.length) n.removeAttribute(n.attributes[0].name);
            walk(n);
          } else {
            walk(n);                            // sanitize children first
            while (n.firstChild) parent.insertBefore(n.firstChild, n);
            parent.removeChild(n);              // unwrap
          }
        } else if (n.nodeType === 8) {          // comment
          parent.removeChild(n);
        }
      }
    })(tpl.content);
    return tpl.innerHTML;
  }

  // Only http(s) link targets. Reject embedded credentials, control-char and
  // protocol-relative tricks (e.g. "/\evil.com", "https://ok\n@evil.com").
  // Absolute http(s) URLs may be off-site (AustLII/Jade); anything else must be same-origin.
  function safeUrl(u) {
    if (typeof u !== 'string') return '';
    var s = u.trim();
    if (!s) return '';   // empty/blank: no link (don't resolve "" to the current page)
    var isAbsolute = /^https?:\/\//i.test(s);
    var url;
    try { url = new URL(s, document.baseURI); } catch (e) { return ''; }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
    if (url.username || url.password) return '';
    if (!isAbsolute && url.origin !== window.location.origin) return '';
    return url.href;
  }

  function stripTags(s) { return String(s == null ? '' : s).replace(/<[^>]*>/g, ' '); }

  /* ---------- date helpers ---------- */
  // Render whatever precision the data carries:
  //   "2026-06-17" -> "17 JUN 2026"  (daily entries)
  //   "2026-06"    -> "JUN 2026"
  //   "2017"       -> "2017"         (year-only authorities — exact date pending)
  var MON = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  function fmtDate(raw) {
    var s = String(raw == null ? '' : raw).trim();
    var f = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (f) return f[3] + ' ' + (MON[parseInt(f[2], 10) - 1] || f[2]) + ' ' + f[1];
    var ym = /^(\d{4})-(\d{2})$/.exec(s);
    if (ym) return (MON[parseInt(ym[2], 10) - 1] || ym[2]) + ' ' + ym[1];
    var y = /^(\d{4})$/.exec(s);
    if (y) return y[1];
    return s; // unknown format: show verbatim rather than blank
  }
  function yearOf(c) {
    var m = /(\d{4})/.exec(String(c.date || ''));
    if (m) return m[1];
    m = /(\d{4})/.exec(c.citation || c.decided || '');
    return m ? m[1] : '';
  }

  /* ---------- state ---------- */
  var ALLCASES = [];
  var COURTS = [];   // [{tag, name}]
  var YEARS = [];    // ['2026', ...]
  var state = { q: '', court: 'ALL', rel: 'ALL', year: 'ALL' };

  // live list-view references
  var listEl = null, sentinelEl = null, countEl = null, io = null;
  var filtered = [], rendered = 0;
  var listMemo = null; // { key, count, scroll } for back-navigation restore
  var filtersOpen = false;      // mobile: filter panel starts collapsed (toggle to reveal)
  var filterToggleEl = null;    // the "Filters" toggle button (shown on mobile only)

  var app = document.getElementById('app');

  /* ---------- normalize + load ---------- */
  function normalize(arr) {
    var seen = {};
    var out = (arr || []).map(function (c, i) {
      c = c || {};
      // stable, unique, string id — matches the always-string id from the hash route.
      // Fallbacks use a "~i" suffix so they can't shadow an author's real slug.
      var id = (c.id == null || c.id === '') ? '' : String(c.id);
      if (!id || seen[id]) {
        if (id && seen[id]) { try { console.warn('cases.json: duplicate id "' + id + '" at index ' + i + ' — using a generated fallback'); } catch (e) {} }
        id = (id || 'case') + '~' + i;
      }
      c.id = id;
      seen[id] = 1;
      c.tags = Array.isArray(c.tags) ? c.tags : [];   // never let a bad field crash the load
      c._year = yearOf(c);
      c._search = [
        c.caseName, c.citation, c.court, c.courtTag, c.oneLine,
        c.tags.join(' '),
        stripTags(c.whatHappened), stripTags(c.whatHeld), stripTags(c.whatItMeans), stripTags(c.verdict)
      ].join(' ').toLowerCase();
      return c;
    });
    out.sort(function (a, b) { return String(b.date || '').localeCompare(String(a.date || '')); });
    return out;
  }

  function deriveFacets() {
    var cSeen = {}, ySeen = {};
    COURTS = []; YEARS = [];
    ALLCASES.forEach(function (c) {
      if (c.courtTag && !cSeen[c.courtTag]) { cSeen[c.courtTag] = 1; COURTS.push({ tag: c.courtTag, name: c.court || c.courtTag }); }
      if (c._year && !ySeen[c._year]) { ySeen[c._year] = 1; YEARS.push(c._year); }
    });
    COURTS.sort(function (a, b) { return a.tag.localeCompare(b.tag); });
    YEARS.sort(function (a, b) { return b.localeCompare(a); });
  }

  function load() {
    app.innerHTML = '';
    app.appendChild(skeleton());
    fetch(DATA_URL, { cache: 'no-cache' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (json) {
        if (!Array.isArray(json)) throw new Error('cases.json is not an array');
        ALLCASES = normalize(json);
        deriveFacets();
        restoreState();   // so a deep-linked detail page has correct Back-to-index filters
        window.addEventListener('hashchange', route);
        route();
      })
      .catch(function (err) { renderError(err); });
  }

  /* ---------- routing ---------- */
  function parseHash() {
    var hash = location.hash || '';
    if (hash.charAt(0) === '#') hash = hash.slice(1);
    var qi = hash.indexOf('?');
    var path = qi === -1 ? hash : hash.slice(0, qi);
    var query = qi === -1 ? '' : hash.slice(qi + 1);
    var params = {};
    try { new URLSearchParams(query).forEach(function (v, k) { params[k] = v; }); } catch (e) {}
    var m = /^\/case\/(.+)$/.exec(path);
    if (m) {
      var id;
      try { id = decodeURIComponent(m[1]); } catch (e) { id = m[1]; } // tolerate a malformed %-escape
      return { view: 'detail', id: id, params: params };
    }
    return { view: 'list', params: params, hasQuery: query.length > 0 };
  }

  function route() {
    if (io) { io.disconnect(); io = null; }
    try {
      var r = parseHash();
      if (r.view === 'detail') renderDetail(r.id);
      else renderList(r);
    } catch (e) {
      try { console.error('route error', e); } catch (_) {}
      renderList({ view: 'list', params: {}, hasQuery: false }); // never wedge the router
    }
  }

  function filtersToQuery() {
    var parts = [];
    if (state.q) parts.push('q=' + encodeURIComponent(state.q));
    if (state.court !== 'ALL') parts.push('court=' + encodeURIComponent(state.court));
    if (state.rel !== 'ALL') parts.push('rel=' + encodeURIComponent(state.rel));
    if (state.year !== 'ALL') parts.push('year=' + encodeURIComponent(state.year));
    return parts.length ? '?' + parts.join('&') : '';
  }

  function persist() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (e) {}
  }
  function restoreState() {
    try {
      var s = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
      if (s && typeof s === 'object') {
        state.q = typeof s.q === 'string' ? s.q : '';
        state.court = s.court || 'ALL';
        state.rel = s.rel || 'ALL';
        state.year = s.year || 'ALL';
      }
    } catch (e) {}
  }

  /* ---------- shared bits ---------- */
  // Three states, not two. A case added as full text only (pipeline/add_text.py)
  // has no relevance yet — showing it as "Awareness" would assert a call nobody
  // made, so it gets its own quiet badge until someone writes it up.
  // A fourth state (pipeline/add_case.py --audit): the write-up exists but its
  // fact-check found problems, so it is HELD — same quiet styling, and never a call.
  function badge(rel, held) {
    if (held) return h('span', { class: 'badge badge--fulltext' }, 'Held for review');
    var r = String(rel || '').toUpperCase();
    if (r === 'ACTION') return h('span', { class: 'badge badge--action' }, 'Action');
    if (r === 'AWARENESS') return h('span', { class: 'badge badge--awareness' }, 'Awareness');
    return h('span', { class: 'badge badge--fulltext' }, 'Full text');
  }
  function tagPills(tags) {
    return (Array.isArray(tags) ? tags : []).map(function (t) { return h('span', { class: 'tag', text: t }); });
  }
  function courtName(tag) {
    for (var i = 0; i < COURTS.length; i++) if (COURTS[i].tag === tag) return COURTS[i].name;
    return tag;
  }
  var ICON = {
    search: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
    chevR: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
    chevD: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
    arrowL: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>',
    ext: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3h7v7"/><path d="M21 3l-9 9"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>'
  };

  function skeleton() {
    var f = frag();
    var wrap = h('div', { class: 'wrap' });
    for (var i = 0; i < 4; i++) wrap.appendChild(h('div', { class: 'skeleton-row' }));
    f.appendChild(wrap);
    return f;
  }

  /* ---------- LIST VIEW ---------- */
  function renderList(r) {
    // resolve filters: explicit hash query wins, else persisted, else defaults
    if (r && r.hasQuery) {
      state.q = r.params.q || '';
      state.court = r.params.court || 'ALL';
      state.rel = (r.params.rel ? String(r.params.rel).toUpperCase() : 'ALL');
      state.year = r.params.year || 'ALL';
    } else {
      restoreState();
    }
    // validate facet values still exist
    if (state.court !== 'ALL' && !COURTS.some(function (c) { return c.tag === state.court; })) state.court = 'ALL';
    if (state.year !== 'ALL' && YEARS.indexOf(state.year) === -1) state.year = 'ALL';
    if (['ALL', 'ACTION', 'AWARENESS'].indexOf(state.rel) === -1) state.rel = 'ALL';
    persist();
    syncHash();   // reconcile the URL with persisted filters so it stays shareable & Back-href is correct

    document.title = 'The Case-Law Review — WA Criminal';

    var view = h('div', { class: 'view' });

    // masthead
    var updated = ALLCASES.length ? fmtDate(ALLCASES[0].date) : '—';
    view.appendChild(h('div', { class: 'wrap' },
      h('header', { class: 'masthead' },
        h('div', { class: 'label eyebrow' }, 'WA Criminal Case-Law'),
        h('h1', {}, 'The Case-Law Review'),
        h('p', { class: 'deck' }, "A growing archive of the decisions that touch a detective’s work — the law, what it changed, and what it means for your jobs."),
        h('div', { class: 'label', style: 'margin-top:18px;letter-spacing:.1em;' },
          ALLCASES.length + (ALLCASES.length === 1 ? ' case' : ' cases') + ' · ' +
          COURTS.length + (COURTS.length === 1 ? ' court' : ' courts') + ' · updated ' + updated)
      )
    ));

    // sticky controls
    view.appendChild(buildControls());

    // list region
    listEl = h('div', { class: 'list', id: 'case-list' });
    sentinelEl = h('div', { class: 'load-more-sentinel', 'aria-hidden': 'true' });
    view.appendChild(h('div', { class: 'wrap' }, listEl, sentinelEl));

    app.innerHTML = '';
    app.appendChild(view);

    applyFilters();

    // restore prior scroll position when returning from a detail page
    var key = filtersToQuery();
    if (listMemo && listMemo.key === key) {
      while (rendered < Math.min(listMemo.count, filtered.length)) renderBatch();
      var y = listMemo.scroll;
      listMemo = null;
      requestAnimationFrame(function () { window.scrollTo(0, y); });
    } else {
      listMemo = null;
      app.focus({ preventScroll: true });
    }

    observe();
  }

  function buildControls() {
    var countSpan = h('span', { class: 'result-count label', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
    countEl = countSpan;

    var input = h('input', {
      type: 'search', value: state.q, 'aria-label': 'Search cases, citations, and tags',
      placeholder: 'Search cases, citations, tags…', autocomplete: 'off', spellcheck: 'false',
      oninput: function (e) { state.q = e.target.value; persist(); syncHash(); applyFilters(); }
    });

    var searchRow = h('div', { class: 'search-row' },
      h('div', { class: 'search' }, h('span', { html: ICON.search }), input),
      countSpan
    );

    var courtGroup = chipGroup('Court', 'court',
      [{ v: 'ALL', label: 'All' }].concat(COURTS.map(function (c) { return { v: c.tag, label: c.tag }; })));
    var relGroup = chipGroup('Relevance', 'rel',
      [{ v: 'ALL', label: 'All' }, { v: 'ACTION', label: 'Action' }, { v: 'AWARENESS', label: 'Awareness' }]);
    var yearGroup = YEARS.length > 1 ? chipGroup('Year', 'year',
      [{ v: 'ALL', label: 'All' }].concat(YEARS.map(function (y) { return { v: y, label: y }; })), true) : null;

    var filtersPanel = h('div',
      { class: 'filters' + (filtersOpen ? '' : ' is-collapsed'), id: 'filter-panel' },
      courtGroup, relGroup, yearGroup);

    // Mobile-only toggle: the filter panel is tall (esp. the year row), so it
    // starts collapsed and the first case sits near the top. Hidden on desktop.
    filterToggleEl = h('button', {
      type: 'button', class: 'filter-toggle', 'aria-controls': 'filter-panel',
      'aria-expanded': filtersOpen ? 'true' : 'false',
      onclick: function () {
        filtersOpen = !filtersOpen;
        filtersPanel.classList.toggle('is-collapsed', !filtersOpen);
        filterToggleEl.setAttribute('aria-expanded', filtersOpen ? 'true' : 'false');
      }
    },
      h('span', { class: 'ft-label' }, 'Filters'),
      h('span', { class: 'ft-right' })
    );
    refreshFilterToggle();

    return h('div', { class: 'controls' },
      h('div', { class: 'wrap controls-inner' },
        searchRow,
        filterToggleEl,
        filtersPanel
      )
    );
  }

  // Count of non-default filters, surfaced as a badge on the collapsed toggle so
  // an active filter is never hidden.
  function activeFilterCount() {
    var n = 0;
    if (state.court && state.court !== 'ALL') n++;
    if (state.rel && state.rel !== 'ALL') n++;
    if (state.year && state.year !== 'ALL') n++;
    return n;
  }

  function refreshFilterToggle() {
    if (!filterToggleEl) return;
    var right = filterToggleEl.querySelector('.ft-right');
    if (!right) return;
    right.innerHTML = '';
    var n = activeFilterCount();
    if (n) right.appendChild(h('span', { class: 'ft-count' }, String(n)));
    right.appendChild(h('span', { class: 'chev', html: ICON.chevD }));
  }

  function chipGroup(label, dim, opts, scroll) {
    var group = h('div', { class: 'filter-group' + (scroll ? ' filter-group--scroll' : ''), role: 'group', 'aria-label': label },
      h('span', { class: 'label' }, label));
    opts.forEach(function (o) {
      var active = state[dim] === o.v;
      var btn = h('button', {
        type: 'button', class: 'chip', 'aria-pressed': active ? 'true' : 'false',
        onclick: function () {
          state[dim] = o.v; persist(); syncHash();
          // update pressed states within this group
          Array.prototype.forEach.call(group.querySelectorAll('.chip'), function (c) { c.setAttribute('aria-pressed', 'false'); });
          btn.setAttribute('aria-pressed', 'true');
          applyFilters();
        }
      }, o.label);
      group.appendChild(btn);
    });
    return group;
  }

  function syncHash() {
    var target = '#/' + filtersToQuery();
    if (location.hash !== target) {
      // replace (don't push) so typing doesn't flood history or refire the router
      history.replaceState(null, '', target);
    }
  }

  function matches(c) {
    if (state.court !== 'ALL' && c.courtTag !== state.court) return false;
    if (state.rel !== 'ALL' && String(c.relevance || '').toUpperCase() !== state.rel) return false;
    if (state.year !== 'ALL' && c._year !== state.year) return false;
    if (state.q) { if (c._search.indexOf(state.q.toLowerCase()) === -1) return false; }
    return true;
  }

  function applyFilters() {
    filtered = ALLCASES.filter(matches);
    rendered = 0;
    listEl.innerHTML = '';

    if (countEl) {
      countEl.textContent = filtered.length === ALLCASES.length
        ? filtered.length + (filtered.length === 1 ? ' case' : ' cases')
        : filtered.length + ' of ' + ALLCASES.length;
    }
    refreshFilterToggle();

    if (!filtered.length) {
      var anyFilter = state.q || state.court !== 'ALL' || state.rel !== 'ALL' || state.year !== 'ALL';
      listEl.appendChild(h('div', { class: 'state' },
        ALLCASES.length === 0 ? 'No cases in the library yet.' : 'No cases match that filter.',
        anyFilter ? h('span', { class: 'sub' },
          h('button', { class: 'chip', type: 'button', onclick: clearFilters }, 'Clear filters')) : null
      ));
      return;
    }
    renderBatch();
  }

  function clearFilters() {
    state.q = ''; state.court = 'ALL'; state.rel = 'ALL'; state.year = 'ALL';
    persist(); syncHash();
    // refresh control widgets
    var ctl = app.querySelector('.controls');
    if (ctl) { var s = ctl.querySelector('input'); if (s) s.value = ''; }
    Array.prototype.forEach.call(app.querySelectorAll('.filter-group'), function (g) {
      Array.prototype.forEach.call(g.querySelectorAll('.chip'), function (c, i) { c.setAttribute('aria-pressed', i === 0 ? 'true' : 'false'); });
    });
    applyFilters();
  }

  function renderBatch() {
    var f = frag();
    var end = Math.min(rendered + BATCH, filtered.length);
    for (var i = rendered; i < end; i++) f.appendChild(caseRow(filtered[i]));
    listEl.appendChild(f);
    rendered = end;
  }

  function observe() {
    if (!('IntersectionObserver' in window)) { while (rendered < filtered.length) renderBatch(); return; }
    io = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting && rendered < filtered.length) renderBatch();
    }, { rootMargin: '600px 0px' });
    io.observe(sentinelEl);
  }

  function caseRow(c) {
    var a = h('a', {
      class: 'case-row', href: '#/case/' + encodeURIComponent(c.id),
      onclick: function () { listMemo = { key: filtersToQuery(), count: rendered, scroll: window.scrollY }; }
    },
      h('div', { class: 'case-rail' },
        h('span', { class: 'court-tag', text: c.courtTag || '' }),
        h('span', { class: 'case-date', text: fmtDate(c.date) })
      ),
      h('div', { class: 'case-main' },
        c.court ? h('div', { class: 'label case-court', text: c.court }) : null,
        h('h2', { class: 'case-name' }, c.caseName || 'Untitled',
          c.citation ? [' ', h('span', { class: 'cite', text: c.citation })] : null),
        c.oneLine ? h('p', { class: 'case-oneline', html: sanitizeInline(c.oneLine) }) : null,
        h('div', { class: 'case-meta' }, badge(c.relevance, c.needsReview), tagPills(c.tags))
      ),
      h('span', { class: 'case-go', html: ICON.chevR, 'aria-hidden': 'true' })
    );
    return a;
  }

  /* ---------- DETAIL VIEW ---------- */
  function findCase(id) {
    for (var i = 0; i < ALLCASES.length; i++) if (ALLCASES[i].id === id) return ALLCASES[i];
    return null;
  }

  function fact(label, val) {
    // metadata fields are plain text — strip any inline markup the pipeline may emit
    var v = (val == null || val === '') ? '—' : stripTags(val).replace(/\s+/g, ' ').trim();
    return h('div', { class: 'fact' },
      h('div', { class: 'label' }, label),
      h('div', { class: 'val', text: v || '—' }));
  }

  function section(title, richHtml) {
    if (!richHtml) return null;
    return h('section', { class: 'section' },
      h('h2', {}, title),
      h('div', { class: 'prose', html: sanitizeInline(richHtml) }));
  }

  function docLink(href, label, icon) {
    var u = safeUrl(href);
    if (!u) return null;
    return h('a', { class: 'doclink', href: u, target: '_blank', rel: 'noopener noreferrer' },
      h('span', { text: label }), h('span', { html: icon, 'aria-hidden': 'true' }));
  }

  // A download button (saves rather than navigates). Only http(s)/same-origin
  // targets pass safeUrl; absent paths render nothing (graceful fallback).
  function dlButton(href, label, downloadName) {
    var u = safeUrl(href);
    if (!u) return null;
    return h('a', { class: 'doclink doclink--dl', href: u, download: downloadName || '' },
      h('span', { html: ICON.download, 'aria-hidden': 'true' }), h('span', { text: label }));
  }

  /* ---------- full judgment reader (lazy-loaded from the case .md) ---------- */
  var _judgmentCache = {};   // id -> { source, text } | 'NA'

  function judgmentSection(c, mdPath) {
    var bodyEl = h('div', { class: 'judgment-body is-hidden' });
    var label = h('span', { class: 'jt-label', text: 'Read the full judgment' });
    var btn = h('button', {
      type: 'button', class: 'judgment-toggle', 'aria-expanded': 'false',
      onclick: function () {
        var nowHidden = bodyEl.classList.toggle('is-hidden');
        btn.setAttribute('aria-expanded', nowHidden ? 'false' : 'true');
        label.textContent = nowHidden ? 'Read the full judgment' : 'Hide the full judgment';
        if (!nowHidden && !bodyEl.getAttribute('data-loaded')) {
          bodyEl.setAttribute('data-loaded', '1');
          loadJudgment(c, mdPath, bodyEl);
        }
      }
    }, h('span', { html: ICON.file, 'aria-hidden': 'true' }), label,
       h('span', { class: 'chev', html: ICON.chevD }));
    return h('div', { class: 'judgment' },
      h('div', { class: 'label jh-eyebrow' }, 'The judgment, in full'),
      btn, bodyEl);
  }

  function loadJudgment(c, mdPath, container) {
    container.appendChild(h('div', { class: 'judgment-loading', text: 'Loading the judgment…' }));
    var render = function (j) {
      container.innerHTML = '';
      if (!j || j === 'NA' || !j.text) {
        container.appendChild(h('p', { class: 'judgment-na' },
          'The verbatim text isn’t in the app for this case yet — read it at the source link above.'));
        return;
      }
      container.appendChild(h('div', { class: 'judgment-source' },
        h('span', { text: 'Verbatim judgment text' }),
        h('a', {
          class: 'jsrc', target: '_blank', rel: 'noopener noreferrer',
          href: safeUrl(j.source) || safeUrl(c.austliiUrl) || safeUrl(c.jadeUrl) || '#'
        }, 'view source ↗')));
      var article = h('div', { class: 'judgment-text' });
      judgmentNodes(j.text).forEach(function (n) { article.appendChild(n); });
      container.appendChild(article);
    };
    if (_judgmentCache[c.id]) { render(_judgmentCache[c.id]); return; }
    fetch(mdPath)
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (md) { var j = extractJudgment(md) || 'NA'; _judgmentCache[c.id] = j; render(j); })
      .catch(function (e) {
        container.innerHTML = '';
        container.appendChild(h('p', { class: 'judgment-na',
          text: 'Could not load the judgment (' + (e && e.message ? e.message : 'error') + ').' }));
      });
  }

  // Pull the verbatim "## Full judgment" section (+ its source url) out of the .md.
  function extractJudgment(md) {
    var i = md.indexOf('## Full judgment');
    if (i === -1) return null;
    var rest = md.slice(i);
    var nl = rest.indexOf('\n');
    var header = nl === -1 ? rest : rest.slice(0, nl);
    var text = (nl === -1 ? '' : rest.slice(nl + 1)).replace(/^\s+/, '');
    var m = header.match(/https?:\/\/[^\s)]+/);
    return { source: m ? m[0] : '', text: text };
  }

  // Front-matter recognizers. The top of a judgment (court name, the bench, the
  // parties and the "AND"/"v" connectors) was being mis-rendered as a run of teal
  // section headings — these patterns peel those shapes into a quiet masthead
  // instead. Kept tight so body headings (CATCHWORDS / ORDER / RESULT …) are
  // untouched: the party rule needs a tab / multi-space column separator (which a
  // heading never has), and the court/coram rules anchor on court-type words and
  // judicial suffixes.
  // whole-line court name (anchored, so prose like "Supreme Court rejected this" can't match)
  var COURT_LINE = /^(?:the\s+|in\s+the\s+)?(?:(?:high|supreme|district|federal(?:\s+circuit)?|family|magistrates'?|children'?s|local|coroners'?|county)\s+court|(?:full\s+)?court\s+of\s+(?:criminal\s+)?appeal)(?:\s+of\s+[\w' .-]+?)?(?:\s+\([^)]*\))?(?:\s+at\s+[\w' .-]+?)?$/i;
  var CORAM_PREFIX = /^(?:coram|before)\b\s*[:\-]\s*/i;
  var CORAM_SUFFIX = /^(?:CJ|ACJ|JJA|JJ|JA|AJA)\.?$/;        // multi-letter (multi-judge) suffix
  var JUDGE_SUFFIX = /^(?:CJ|ACJ|JJA|JJ|JA|AJA|AJ|J|P)\.?$/; // any judicial-suffix token
  var ROLE = '(?:(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\\s+)?(?:named\\s+)?(?:appellants?|respondents?|applicants?|plaintiffs?|defendants?|prosecutors?|petitioners?|claimants?|interve(?:nor|ner)s?|amici?(?:\\s+curiae)?|cross-(?:appellants?|respondents?)|accused)';
  var PARTY_RE = new RegExp('^(.+?)(?:\\t+|\\u00a0+| {2,})(' + ROLE + ')\\s*$', 'i');
  // a line that is WHOLLY a bench list (every token a name / "and" / judicial suffix,
  // ending in a multi-judge suffix) — or a lone all-caps single judge ("WHITBY J").
  // The all-tokens rule is what stops prose that merely ends "…and Gummow JJ".
  function looksLikeCoram(s) {
    if (/^[A-Z][A-Z'’.-]+\s+(?:J|AJ|CJ|ACJ|JA)\.?$/.test(s)) return true;
    var toks = s.replace(/,/g, ' ').split(/\s+/).filter(Boolean);
    if (toks.length < 2 || toks.length > 16) return false;
    if (!CORAM_SUFFIX.test(toks[toks.length - 1])) return false;
    return toks.every(function (t) {
      return /^[A-Z][A-Za-z'’.-]*$/.test(t) || /^(?:and|&)$/i.test(t) || JUDGE_SUFFIX.test(t);
    });
  }

  // A sentence ends in terminal punctuation; a heading or a table cell does not.
  // That single difference is what separates a section heading from the rows of a
  // sentencing table, so it is used in both places below.
  var SENTENCE_END = /[.?!]["'’”)\]]?$/;
  // Shapes that are never a section heading, however short: a statutory sub-clause
  // ("(a) make a continuing detention order"), a wholly parenthetical aside
  // ("(citation omitted)"), or a clause that runs on into the next ("… ; and").
  var NOT_HEADING = /^\([^)]*\)$|[,;]\s*(?:and|or)$|^[A-Z][A-Z'’ .-]+(?:,\s*M[RS]S?)?:\s\S/;
  //   … the last shape: a transcript speaker line ("SWEENEY DCJ: But I don't …")
  // The WA judgment template's front-matter labels, which are headings even though
  // they are sentence case and only a word or two long.
  var MASTHEAD_LABEL = new RegExp('^(?:jurisdiction|title of court|citation|coram|heard' +
    '|delivered|published|file no(?:/s)?|catchwords?|legislation|result|category' +
    '|representation|counsel|solicitors?|on appeal from|cases?(?:\\(s\\))? referred to[^:]*' +
    '|case\\(s\\) referred to[^:]*)\\s*:$', 'i');

  // Split the document into blocks, one per rendered element.
  //
  // Blank lines are the natural separator, but a Word export is not written that
  // way: macOS `textutil` turns each Word paragraph into a single "\n", so an
  // entire set of reasons arrives as ONE block and renders as a wall of text.
  // (Every .doc-sourced case in the library is like this; AustLII-sourced ones
  // carry real blank lines.) So after the blank-line split, any block whose lines
  // read as PROSE — a long line, or most lines closing a sentence — is exploded
  // into one block per line.
  //
  // The test is deliberately narrow. A masthead row ("JURISDICTION : SUPREME
  // COURT" + "IN CRIMINAL"), a party block, a bench list and a stacked header are
  // all short, unpunctuated lines that belong together, and they stay grouped.
  // A margin paragraph number standing on its own line. `pdftotext` (the eCourts PDF
  // route) keeps a WA judgment's paragraph numbers that way — "3", blank line, then
  // the hard-wrapped text — and sometimes glues the number to the END of the previous
  // block ("… for the evidence to have any value.86" / "80" / blank / "Hung's …").
  var BARE_NUM = /^\[?\d{1,4}\]?$/;
  // A list marker a PDF export leaves on its own line: "1.", "(a)", "(iv)".
  var LIST_MARKER = /^(?:\d{1,2}\.|\([a-z0-9]{1,4}\)|\([ivxlc]+\)|[a-z]\.)$/;
  // A front-matter label that stands alone even inside a wrapped block
  // ("Catchwords:" / "Legislation:" / "Result:" run together in a PDF export).
  var LABEL_LINE = /^[A-Z][A-Za-z()\/ .]{0,30}:$/;
  var CITATION = /\[\d{4}\]\s+[A-Z][A-Za-z]*\s+\d+/;
  // A reported citation — "(1988) 164 CLR 365", "(2009) 40 WAR 489", "(1987) 44 SASR 591" —
  // the shape an older High Court judgment's footnotes are made of.
  var REPORTED = /\(\d{4}\)\s+\d{1,3}\s+[A-Z][A-Za-z]{0,7}(?:\s[A-Z][A-Za-z]{0,7}){0,3}\s+\d+/;
  var OPENS_SENTENCE = /^(?:\[\d+\]\s|\d+\.\s|["'“(]?[A-Z])/;
  // A paragraph's last line closes a sentence — allowing a footnote marker after the
  // full stop ("… in about November 2017.36") and a colon that introduces a quotation.
  var CLOSES = /[.?!:;]["'’”)\]]?\d{0,3}$/;
  // A transcript's speaker marker on its own line, as an older High Court PDF sets
  // it: "Q" / "Well now how many vehicles …" / "A" / "There were eight vehicles."
  var QA_MARKER = /^[QA]\.?$/;
  // A judge's reasons opening in the WA form — "BUSS JA: On 23 April 2009, …" — which
  // otherwise has the shape of a transcript speaker line.
  var JUDGE_OPENER = /^[A-Z][A-Z'’-]+(?: [A-Z][A-Z'’-]+)* (?:CJ|ACJ|P|JA|JJA|J|JJ|AJA|DCJ):\s\S/;

  // Is this block one or more paragraphs hard-wrapped at a column (a PDF export,
  // the pre-1998 corpus), as opposed to a stack of whole rows (masthead, bench list,
  // sentencing table, list of cases)? Inside wrapped prose the lines run to the
  // margin and break mid-sentence — so a line without a full stop is followed by one
  // that starts lower-case, or ends on a word at the margin. Rows never do that.
  function isWrappedProse(lines) {
    var longest = 0, cites = 0, running = 0, i, s, next;
    for (i = 0; i < lines.length; i++) {
      s = lines[i];
      if (s.length > longest) longest = s.length;
      if (/\t/.test(s) || /\.{4,}/.test(s)) return false;   // masthead columns / contents leaders
      if (CITATION.test(s)) cites++;
    }
    if (longest > 125 || cites >= 2) return false;
    // A body set at a wrap width breaks every line at ~75 columns; a Word export's
    // short paragraphs and headings can run to 125 without a break. Past 82 only the
    // lower-case continuation is proof (a footnote is set at a wider measure).
    var narrow = longest <= 82;
    for (i = 0; i < lines.length - 1; i++) {
      s = lines[i]; next = lines[i + 1];
      if (SENTENCE_END.test(s) || /:$/.test(s) || BARE_NUM.test(s) || LIST_MARKER.test(s)) continue;
      if (s.length >= 30 && /^[a-z]/.test(next)) return true;        // continuation starts lower-case
      if (!narrow) continue;
      if (s.length >= 55 && /[a-z]{2,}$/.test(s)) return true;       // broken at the margin, mid-sentence
      // a narrow wrap (an indented quotation, a transcript): a line broken before a
      // word, then the paragraph's short last line closing the sentence
      if (s.length >= 45 && /[a-z]{2,}$/.test(s) && SENTENCE_END.test(next) &&
          next.length < 0.6 * s.length) return true;
      if (s.length >= 55) running++;
    }
    return running >= 2;
  }

  // Re-flow hard-wrapped lines into paragraphs. Whitespace only — no word moves.
  // Inside a paragraph every line runs to the margin; only its LAST line stops
  // short. So a line that closes a sentence AND is well short of the wrap width,
  // followed by a line that opens one, is where a paragraph ends. A label line and
  // a bare paragraph number always stand alone.
  function reflow(lines) {
    var blocks = [], cur = [], longest = 0;
    lines.forEach(function (s) { if (s.length > longest) longest = s.length; });
    lines.forEach(function (s, i) {
      var next = i + 1 < lines.length ? lines[i + 1] : '';
      // a list marker stands alone only when an item follows it — "… the basis of
      // count" / "20." is a paragraph's last word, not a marker
      var prevLine = i > 0 ? lines[i - 1] : '';
      var markerAlone = LIST_MARKER.test(s) && (next || SENTENCE_END.test(prevLine) || /:$/.test(prevLine));
      var qaAlone = QA_MARKER.test(s) && !!next;
      if (BARE_NUM.test(s) || markerAlone || qaAlone || LABEL_LINE.test(s) || MASTHEAD_LABEL.test(s)) {
        if (cur.length) blocks.push(cur.join('\n'));
        blocks.push(s);
        cur = [];
        return;
      }
      cur.push(s);
      if (next && ((SENTENCE_END.test(s) && s.length < 0.7 * longest && OPENS_SENTENCE.test(next)) ||
                   (/:\d{0,3}$/.test(s) && /^["'“(\[A-Z]/.test(next)))) {   // "… as follows:" / "… case:3"
        blocks.push(cur.join('\n'));
        cur = [];
      }
    });
    if (cur.length) blocks.push(cur.join('\n'));
    return blocks;
  }

  function splitProseRuns(norm) {
    var blocks = [];
    norm.split(/\n{2,}/).forEach(function (block) {
      var lines = block.split('\n').map(function (l) { return l.trim(); }).filter(Boolean);
      if (!lines.length) return;
      // a paragraph number glued to the end of the previous block stands alone; so does
      // a bracketed list marker after a lead-in ("… as follows:" / "(1)") — but not a
      // "12." that is the paragraph's last word ("… the basis for count" / "12.")
      var tailLine = lines[lines.length - 1];
      if (lines.length >= 2 && (BARE_NUM.test(tailLine) ||
          (LIST_MARKER.test(tailLine) && tailLine.charAt(0) === '(' && /:$/.test(lines[lines.length - 2])))) {
        var tail = lines.pop();
        splitProseRuns(lines.join('\n')).forEach(function (b) { blocks.push(b); });
        blocks.push(tail);
        return;
      }
      if (lines.length < 2) { blocks.push(lines[0]); return; }
      var longest = 0, closed = 0;
      lines.forEach(function (s) {
        if (s.length > longest) longest = s.length;
        if (SENTENCE_END.test(s)) closed++;
      });
      if (longest > 140) {
        // Word export: one paragraph per line. A run of SHORT lines inside such a
        // block (a transcript excerpt, an indented quotation set at a narrow measure)
        // may itself be hard-wrapped, so each run gets the wrapped-prose test.
        var run = [];
        var flushRun = function () {
          if (run.length >= 2 && isWrappedProse(run)) reflow(run).forEach(function (b) { blocks.push(b); });
          else run.forEach(function (l) { blocks.push(l); });
          run = [];
        };
        lines.forEach(function (l) {
          if (l.length > 100) { flushRun(); blocks.push(l); } else run.push(l);
        });
        flushRun();
        return;
      }
      if (isWrappedProse(lines)) {
        reflow(lines).forEach(function (b) { blocks.push(b); });
        return;
      }
      if (closed >= Math.ceil(lines.length / 2)) {
        lines.forEach(function (l) { blocks.push(l); });   // prose run -> one block per paragraph
        return;
      }
      // Hard-wrapped text the tests above did not catch — the pre-1998 High Court
      // corpus is set at ~80 columns with several paragraphs per blank-line block.
      if (lines.length >= 6) {
        reflow(lines).forEach(function (b) { blocks.push(b); });
        return;
      }
      blocks.push(lines.join('\n'));                       // masthead / stacked rows -> keep together
    });
    return blocks;
  }

  // Format plain judgment text into readable nodes: stacked header blocks,
  // section headings, and hanging paragraph numbers. Heuristic but robust.
  function judgmentNodes(text) {
    var out = [];
    var norm = String(text).replace(/\r/g, '').replace(/\n{3,}/g, '\n\n');
    // Each numbered paragraph starts its own block — "12. Text" (Word) and the High
    // Court's "[42] Text", which hard-wrapped HCA text otherwise runs together. Only
    // a marker that CONTINUES THE COUNT: a wrapped line starting "[61] - [62] above …"
    // is a cross-reference, not paragraph 61.
    // The count is followed through the whole text, mid-line markers included (a
    // Lexis export runs "… sentence. [2] Next …" on one line), so a marker at a line
    // start is split off when it continues the count OR follows a line that closed
    // (a heading, "… as follows:"). "[61] - [62] above" after "referred to at" is
    // neither, and stays a cross-reference.
    var last = 0;
    norm = norm.replace(/([^\n]{0,2}|^)(\n?)(?:\[(\d{1,4})\]|(\d{1,4})\.)(?=\s)/g, function (m, prev, nl, a, b) {
      var n = parseInt(a || b, 10);
      var counts = n > last && n <= last + 3;
      if (!nl) {                       // mid-line: "… sentence. [2] Next" advances the
        if (counts && a && /[.?!:"'’”)\]]\s$/.test(prev)) last = n;   // count; "at [45]" does not
        return m;
      }
      if (counts || /[.?!:;"'’”)\]]$/.test(prev) || !prev) {
        if (counts) last = n;
        return prev + '\n\n' + m.slice(prev.length + 1);
      }
      return m;
    });
    var blocks = splitProseRuns(norm);
    // The masthead recognizers below apply only while we're still in the front
    // matter (top of the document). Once the reasons begin (a numbered paragraph or
    // a long prose block) this flips off, so a body per-judge reasons heading
    // ("MAZZA JA", "Brennan and Toohey JJ.") stays a prominent section divider
    // rather than being demoted to a quiet coram subtitle.
    var inFrontMatter = true;
    var lastNum = 0;                    // the last paragraph number accepted, for the sequence test
    // Bare margin numbers (PDF export) are held until the text shows which paragraph
    // each belongs to. The PDF text layer emits them in reading order but not always
    // beside their paragraph: "9" / "10" / text of 9 / … / text of 10, or, for a short
    // paragraph, text of 60 / "60" / "61" / text of 61. So: the first number goes to
    // the text that follows (or, when a pair follows an unnumbered paragraph, to that
    // paragraph); the next number waits for the next paragraph — unless that
    // paragraph is a quotation or list the previous one introduced with a colon.
    // A number no paragraph claims is a footnote marker and is kept, quietly.
    var pending = [];
    var sincePending = 0;               // nodes emitted since the last number was held
    var lastNode = null;                // the node emitted last, for the retro-assignment
    // A footnote's first words, as the WA template sets them — never a paragraph's.
    var FOOTNOTE_START = /^(?:trial ts|ts,? \d|[a-z]{2,6} ts,? \d|appeal ts|see,? |cf,? |ibid|exhibits?\b|annexures?\b|pars?\b|paragraphs?\s+\[?\d|appellant['’]s|respondent['’]s|state['’]s|applicant['’]s)/i;
    var INTRODUCES = /:\s*\d{0,3}$/;    // "… as follows:" or "… but:32" (a footnote number)
    var FOOTNOTE_REF = /\b(?:trial ts|appeal ts|ts \d{2,}|ts, \d|exhibit \d|WAB \d|\(ts \d)|\[\d+\]\s*[-–]\s*\[\d+\]|\[\d+\]\.$/i;
    var FOOTNOTE_REF_TS = /\b(?:trial ts|appeal ts|ts \d{2,}|ts, \d|exhibit \d|WAB \d)/i;    // the transcript, wherever it sits
    var FOOTNOTE_REF_XREF = /\[\d+\]\s*[-–]\s*\[\d+\]|\[\d+\]\.$/;                    // "[12] - [14]": a note, unless a paragraph ends "See [92] - [93] above." 
    var SPEAKER_LABEL = /^[A-Z][A-Za-z'’]+(?: [A-Z][A-Za-z'’]+)?(?:,\s*M[RS]S?)?:$/;     // "Nguyet:" / "BEVILACQUA, MR:"
    var SPEAKER_LINE = /^[A-Z][A-Za-z'’]+(?: [A-Z][A-Za-z'’]+)?(?:,\s*M[RS]S?)?:\s\S/;  // "Complainant: I'm just …"
    function numOf(tok) { return parseInt(tok.replace(/[\[\]]/g, ''), 10); }
    function afterParagraphEnd(node) {     // the node before reads as a finished paragraph or item
      if (!node) return true;
      var t = node.textContent || '';
      if (node.tagName !== 'P') return true;                       // a heading
      if (/\bjp-item\b|\bjp-qa\b/.test(node.className)) return true;   // a list item, a transcript line
      return t.length >= 80 && SENTENCE_END.test(t);
    }
    function introducesQuote(node) {       // a paragraph (not a heading) ending "… as follows:"
      if (!node) return false;
      var t = node.textContent || '';
      if (node.tagName === 'P' && /\bjp\b/.test(node.className)) return INTRODUCES.test(t);
      return SPEAKER_LABEL.test(t);        // "Nguyet:" — a transcript's speaker, however rendered
    }
    function emit(node) { out.push(node); lastNode = node; sincePending++; return node; }
    function footnoteNum(tok) {           // a stray number is not "text gone by"
      var n = sincePending;
      emit(h('p', { class: 'jp jp-fn', text: tok }));
      sincePending = n;
    }
    function flushPending() {
      pending.forEach(footnoteNum);
      pending = [];
    }
    function numberedNode(tok, text) {
      return h('p', { class: 'jp jp-num' },
        h('span', { class: 'jn', text: tok.replace(/[\[\]]/g, '') }), h('span', { text: text }));
    }
    // Text that can carry a paragraph number: opens a sentence, is not a footnote or
    // a page header, and is either a full paragraph or a complete short sentence.
    function claimable(text, nLines) {
      // "(ts 20)" at the end of a paragraph is an inline transcript cite; a footnote's
      // "see trial ts 3659" is not bracketed
      var unbracketed = text.replace(/\(ts[^)]{0,40}\)/gi, '');
      return text.length >= 20 && OPENS_SENTENCE.test(text) && (!SPEAKER_LINE.test(text) || JUDGE_OPENER.test(text)) &&
        (nLines >= 2 || text.length >= 80 || SENTENCE_END.test(text) || INTRODUCES.test(text)) &&
        // a short block that does not close a sentence is a table cell ("Indecently
        // dealt with T by … (s 320(4))"), not a paragraph
        (text.length >= 120 || CLOSES.test(text)) &&
        !FOOTNOTE_START.test(text) &&
        // a note is a citation; a paragraph that OPENS with one runs on well past it —
        // but until the count is established a long block opening with a citation is
        // the "cases referred to" list, not paragraph 1
        !((text.length < 300 || lastNum === 0) && (CITATION.test(text.slice(0, 60)) || REPORTED.test(text.slice(0, 90)))) &&
        // a short note citing the transcript — as against a paragraph that cites it in
        // passing at the end ("… 'did it happen?' (ts 20).")
        !(text.length < 250 && FOOTNOTE_REF_TS.test(unbracketed)) &&
        // a short note that is a cross-reference — as against a paragraph that closes
        // with one ("… in Schaper. See [92] - [93] above.")
        !(FOOTNOTE_REF_XREF.test(text) && (text.length < 100 || FOOTNOTE_REF_XREF.test(text.slice(0, 40))));
    }
    // The PDF text layer sets a SHORT paragraph's number after its text. Give the
    // number to the unnumbered paragraph emitted just before it, when that reads as
    // one (complete, in sequence, not a quotation the paragraph before introduced).
    function retroAssign(tok) {
      var n = numOf(tok), node = lastNode, t = node ? (node.textContent || '') : '';
      if (!node || out[out.length - 1] !== node) return false;
      // "In particular:" / "75" / "(a) …" — a paragraph that is only a label introducing
      // the list below it, rendered as a heading a moment ago: the number is its own
      if (node.tagName === 'H4' && LABEL_LINE.test(t) && !MASTHEAD_LABEL.test(t) && labelCount[t] === 1 &&
          continuesCount(n, t) && !(out.length > 1 && introducesQuote(out[out.length - 2]))) {
        out[out.length - 1] = lastNode = numberedNode(tok, t);
        lastNum = n;
        return true;
      }
      if (node.tagName !== 'P' || node.className !== 'jp') return false;
      // a short, complete sentence — not the tail of a paragraph a page break cut
      if (t.length < 30 || t.length > 160 || !OPENS_SENTENCE.test(t)) return false;
      if (!(SENTENCE_END.test(t) || INTRODUCES.test(t)) || !continuesCount(n, t)) return false;
      if (FOOTNOTE_START.test(t) || CITATION.test(t.slice(0, 60)) || REPORTED.test(t.slice(0, 90)) ||
          FOOTNOTE_REF.test(t) || SPEAKER_LINE.test(t)) return false;
      if (out.length > 1 && introducesQuote(out[out.length - 2])) return false;
      if (out.length > 1 && /\bjp-fn\b/.test(out[out.length - 2].className || '')) return false;   // footnote text
      out[out.length - 1] = lastNode = numberedNode(tok, t);
      lastNum = n;
      return true;
    }
    function continuesCount(n, text) {
      // the first number needs a real paragraph behind it (not a table cell) — or a
      // judge's opening line, however short ("PULLIN JA: I agree with Buss JA.")
      return n <= 2000 && (lastNum === 0 ? (text.length > 60 || (n === 1 && JUDGE_OPENER.test(text)))
                                         : (n > lastNum && n <= lastNum + 3));
    }
    function fitsCount(n) { return lastNum > 0 && n > lastNum && n <= lastNum + 3; }
    var marker = null, markerIsQA = false, qaSeen = false;
    var labelCount = {};                // how often each label line ("Hung:", "First:") has appeared
    function flushMarker() {
      if (!marker) return;
      emit(h('p', { class: 'jp', text: marker }));
      marker = null; markerIsQA = false;
    }
    blocks.forEach(function (raw, bi) {
      var lines = raw.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      if (!lines.length) return;
      if (lines.length === 1 && LABEL_LINE.test(lines[0])) labelCount[lines[0]] = (labelCount[lines[0]] || 0) + 1;
      var next = String(blocks[bi + 1] || '').replace(/\s+/g, ' ').trim();
      // for the heading tests, look past a bare margin number (or two) to the paragraph
      for (var skip = 1; skip <= 2 && BARE_NUM.test(next); skip++) {
        next = String(blocks[bi + 1 + skip] || '').replace(/\s+/g, ' ').trim();
      }
      // ---- a bare margin number: hold it for the text that claims it
      if (lines.length === 1 && BARE_NUM.test(lines[0])) {
        var tok = lines[0], tn = numOf(tok);
        // a number still waiting after text has gone by is a footnote marker. So is a
        // newcomer that does not continue the run — unless IT fits the paragraph count
        // and the one waiting does not ("85" / "62" / "63" / text of 85: the footnote
        // markers 62 and 63 are the strays)
        if (pending.length && sincePending > 0) flushPending();
        if (pending.length && tn !== numOf(pending[pending.length - 1]) + 1) {
          var fitsNew = fitsCount(tn), fitsOld = fitsCount(numOf(pending[0]));
          if (fitsOld && !fitsNew) {
            // "… well established:64" / "92" / "64": before the stray is set down, the
            // number waiting may be the short paragraph's just before it
            if (pending.length === 1 && retroAssign(pending[0])) pending = [];
            footnoteNum(tok);
            return;
          }
          flushPending();
        }
        pending.push(tok);
        sincePending = 0;
        // "text of 60" / "60" / "61": a pair right after a short unnumbered paragraph —
        // the first number is that paragraph's, the second waits for the next
        if (pending.length === 2 && retroAssign(pending[0])) pending.shift();
        if (pending.length > 3) footnoteNum(pending.shift());
        return;
      }
      // ---- a list marker on its own line ("1." / "(a)") joins the item that follows
      if (lines.length === 1 && LIST_MARKER.test(lines[0])) {
        flushMarker();
        marker = lines[0];
        // "Gillan DCJ was satisfied that:" / "16" / "(a)" — the number is the line's
        if (pending.length === 1 && retroAssign(pending[0])) pending = [];
        return;
      }
      // ---- a transcript's "Q" / "A" on its own line (an older High Court PDF) joins the
      // question or answer that follows. A lone "A" counts only once a "Q" has been seen,
      // so a lettered section heading is never taken for an answer.
      if (lines.length === 1 && QA_MARKER.test(lines[0]) && (lines[0].charAt(0) === 'Q' || qaSeen)) {
        flushMarker();
        marker = lines[0]; markerIsQA = true; qaSeen = true;
        if (pending.length === 1 && retroAssign(pending[0])) pending = [];
        return;
      }
      if (marker) {
        var mtext = lines.join(' ').replace(/\s+/g, ' ').trim();
        if (markerIsQA) {                    // never claims a paragraph number
          emit(h('p', { class: 'jp jp-qa', text: marker + ' ' + mtext }));
          marker = null; markerIsQA = false;
          return;
        }
        if (mtext.length >= 12 && !CITATION.test(mtext.slice(0, 40))) {
          emit(h('p', { class: 'jp jp-item', text: marker + ' ' + mtext }));
          marker = null;
          return;
        }
        flushMarker();
      }
      if (pending.length) {
        // PDF export: "3" / blank / "The relevant background …" is paragraph 3 when
        // the number continues the sequence and the text reads as a paragraph — not
        // a footnote ("Trial ts 1547.") that happens to carry the next number.
        var pn = numOf(pending[0]);
        var ptext = lines.join(' ').replace(/\s+/g, ' ').trim();
        // a quotation or list introduced by the paragraph before it takes no number
        var introduced = introducesQuote(lastNode);
        // a number carried past a quotation or list ("9" / "10" / text of 9 / (a) / (b) /
        // text of 10) is claimed only once that material has plainly ended
        var carried = sincePending > 0;
        if (claimable(ptext, lines.length) && continuesCount(pn, ptext) && !introduced &&
            (!carried || afterParagraphEnd(lastNode))) {
          lastNum = pn;
          inFrontMatter = false;
          emit(numberedNode(pending.shift(), ptext));
          return;
        }
        // "109" / "First:" / the quotation — a paragraph that is only a label
        // introducing the quotation below it takes its number on the label. Not a
        // transcript's speaker ("Hung:"): that label recurs, and another follows the
        // text after it.
        var after2 = String(blocks[bi + 2] || '').trim();
        if (lines.length === 1 && LABEL_LINE.test(ptext) && !MASTHEAD_LABEL.test(ptext) &&
            labelCount[ptext] === 1 && !LABEL_LINE.test(after2) &&
            continuesCount(pn, ptext) && !introduced && (!carried || afterParagraphEnd(lastNode))) {
          lastNum = pn;
          inFrontMatter = false;
          emit(numberedNode(pending.shift(), ptext));
          return;
        }
        // not this block's: a lone number may be the short paragraph's before it;
        // otherwise it is a footnote marker — unless it is waiting out a quotation
        if (pending.length === 1 && retroAssign(pending[0])) pending = [];
        else if (pending.length === 1 && !introduced) flushPending();
      }
      // ---- "Jurisdiction" / ": DISTRICT COURT OF WESTERN AUSTRALIA" — a PDF export
      // splits the ON APPEAL FROM rows into a label block and a colon-led value block.
      var colonValue = lines.length === 1 && lines[0].match(/^:\s*(\S.*)$/);
      if (colonValue && out.length) {
        var prev = out[out.length - 1];
        var prevText = (prev.textContent || '').trim();
        var LABEL_SHAPE = /^[A-Z][A-Za-z ()\/]{1,27}$/;
        if (prev.tagName === 'P' && /\bjp\b/.test(prev.className) && !/\bjp-/.test(prev.className) &&
            LABEL_SHAPE.test(prevText)) {
          out[out.length - 1] = lastNode = h('p', { class: 'jmeta' },
            h('span', { class: 'jmeta-k', text: prevText }),
            h('span', { class: 'jmeta-v', text: colonValue[1] }));
          return;
        }
        // … or the label is the last line of a stacked header ("ON APPEAL FROM:" / "Jurisdiction")
        var lastLine = prev.tagName === 'DIV' && /\bjhead\b/.test(prev.className) ? prev.lastElementChild : null;
        if (lastLine && prev.children.length >= 2 && LABEL_SHAPE.test((lastLine.textContent || '').trim())) {
          prev.removeChild(lastLine);
          emit(h('p', { class: 'jmeta' },
            h('span', { class: 'jmeta-k', text: (lastLine.textContent || '').trim() }),
            h('span', { class: 'jmeta-v', text: colonValue[1] })));
          return;
        }
      }
      // peel a leading standalone label (ORDER / HELD / INTRODUCTION ...) into a heading
      var lead = lines[0];
      if (lines.length > 1 && lead.length <= 40 &&
          /^(orders?|held|introduction|background|conclusion|disposition|result|reasons|catchwords)\b[:.]?$/i.test(lead)) {
        emit(h('h4', { class: 'jh', text: lead.length <= 14 ? lead : titleish(lead) }));
        lines = lines.slice(1);
        if (!lines.length) return;
      }
      var oneLine = lines.join(' ').replace(/\s+/g, ' ').trim();

      // ---- party rows & connectors -> masthead. Identified by a tab / multi-space
      // column separator or a lone "AND"/"v" — shapes body prose never produces — so
      // they're recognised anywhere (a joined appeal repeats a party block mid-document).
      if (/^(?:and|v|-v-|&)$/i.test(oneLine)) {
        emit(h('div', { class: 'jconn', text: /^(?:v|-v-)$/i.test(oneLine) ? 'v' : 'and' }));
        return;
      }
      var partyRows = lines.map(function (l) { return l.match(PARTY_RE); });
      if (partyRows.length && partyRows.every(Boolean)) {
        var pbox = h('div', { class: 'jparties' });
        partyRows.forEach(function (pr) {
          pbox.appendChild(h('div', { class: 'jparty' },
            h('span', { class: 'jparty-name', text: pr[1].replace(/\s+/g, ' ').trim() }),
            h('span', { class: 'jparty-role', text: pr[2].replace(/\s+/g, ' ').trim() })));
        });
        out.push(pbox);
        return;
      }
      // ---- court & coram -> masthead. A court name or bench list is ambiguous with a
      // body per-judge reasons heading ("MAZZA JA"), so only treat as masthead while
      // still in the front matter; afterwards it falls through to the heading path.
      if (inFrontMatter) {
        // a single-line court identifier -> masthead title (verbatim, not a heading)
        if (lines.length === 1 && oneLine.length <= 64 && COURT_LINE.test(oneLine)) {
          emit(h('div', { class: 'jcourt', text: oneLine }));
          return;
        }
        // the bench. Skip when the block LEADS with the court name: that whole block
        // is a clean stacked header, better left to the jhead path below.
        if (!COURT_LINE.test(lines[0]) && (CORAM_PREFIX.test(oneLine) || looksLikeCoram(oneLine))) {
          emit(h('div', { class: 'jcoram', text: oneLine.replace(CORAM_PREFIX, '') }));
          return;
        }
      }

      // a PDF page's running header ("[2026] WASCA 117" / "JUDGMENT OF THE COURT"),
      // repeated every page — kept, but set small so the reasons read through it
      var isRunningHeader = !inFrontMatter && lines.length >= 2 && lines.length <= 3 &&
        /^\[\d{4}\] [A-Z]+ \d+$/.test(lines[0]) &&
        lines.slice(1).every(function (l) { return l.length <= 40 && l === l.toUpperCase(); });
      // short, multi-line, non-numbered block -> a stacked header (court/parties/coram)
      var isHeaderBlock = lines.length >= 2 && lines.length <= 8 &&
        lines.every(function (l) { return l.length < 52 && !/^\d/.test(l); });
      if (isHeaderBlock) {
        var box = h('div', { class: isRunningHeader ? 'jhead jrun' : 'jhead' });
        lines.forEach(function (l) { box.appendChild(h('div', { class: 'jhead-line', text: l })); });
        out.push(box);
        return;
      }
      // "LABEL : value" header metadata (jurisdiction / coram / citation / ...) -> compact row
      var meta = oneLine.match(/^([A-Z][A-Za-z()\/ .]{1,28}?)\s:\s+(\S.*)$/);
      if (meta && meta[1].trim() === meta[1].trim().toUpperCase()) {
        emit(h('p', { class: 'jmeta' },
          h('span', { class: 'jmeta-k', text: titleish(meta[1].trim()) }),
          h('span', { class: 'jmeta-v', text: meta[2] })));
        return;
      }
      // section heading: short, ALL-CAPS or a known label, no trailing sentence punctuation
      var isCaps = /[A-Z]/.test(oneLine) && oneLine === oneLine.toUpperCase();
      var isHeadingWord = /^(orders?|introduction|background|conclusion|disposition|catchwords|result|the appeal|grounds? of appeal|reasons)\b/i.test(oneLine);
      // A WA judgment sets its section headings in sentence case ("Resentence",
      // "Appeal ground 1: material error of fact"), so the ALL-CAPS test above
      // misses them and they render as body prose. A heading is a short line that
      // does NOT close a sentence and is FOLLOWED BY one that does — which is what
      // separates it from a run of sentencing-table cells, where neither does.
      // a masthead label ("Catchwords:", "Result:") — not a lead-in sentence that
      // happens to end in a colon ("Section 84 relevantly provides:")
      var isLabel = /:$/.test(oneLine) &&
        (MASTHEAD_LABEL.test(oneLine) || oneLine.split(/\s+/).length <= 2);
      var isSentenceCaseHeading = lines.length === 1 && oneLine.length <= 80 &&
        /^[A-Z]/.test(oneLine) && !SENTENCE_END.test(oneLine) && !/:\d{0,3}$/.test(oneLine) &&   // "… as follows:64" is a lead-in, not a heading
        !/[a-z]{2}[.?!]["'’”)\]]?\s+[A-Z]/.test(oneLine) &&   // a sentence ends inside it: wrapped prose
        !NOT_HEADING.test(oneLine) && SENTENCE_END.test(next);
      if (oneLine.length <= 80 && (isCaps || isHeadingWord || isLabel || isSentenceCaseHeading) &&
          !/[.,;]$/.test(oneLine)) {
        // A short label sits in the teal eyebrow the masthead uses; a section
        // heading written as a sentence keeps its own case and sits heavier,
        // because 60 characters of letterspaced capitals is not readable.
        if (isSentenceCaseHeading && !isCaps) {
          emit(h('h4', { class: 'jh jh-section', text: oneLine }));
        } else {
          emit(h('h4', { class: 'jh', text: oneLine.length <= 14 ? oneLine : titleish(oneLine) }));
        }
        return;
      }
      // numbered paragraph -> hanging number in the gutter (reasons have begun).
      // A paragraph number CONTINUES THE DOCUMENT'S SEQUENCE and introduces a
      // sentence. Both tests earn their keep once a Word export's rows stand as
      // their own blocks: a sentencing table's "21 September 2025" would otherwise
      // render as paragraph 21, and "4 months' imprisonment" as paragraph 4.
      // "12. Text", "12 Text" (Word) or "[12] Text" (High Court)
      var nm = oneLine.match(/^(?:\[(\d{1,4})\]|(\d{1,4})\.?)\s+(\S[\s\S]*)$/);
      if (nm) {
        var num = nm[1] || nm[2];
        var n = parseInt(num, 10);
        var rest = nm[3];
        var opensSentence = /^["'“‘(\[]?[A-Z]/.test(rest);
        // Never counts backwards. That is what keeps a quoted numbered list
        // ("1. As a suspect he should have been cautioned …") set inside the
        // reasons from resetting the sequence and stripping the numbers off every
        // real paragraph after it.
        var inSequence = lastNum === 0
          ? rest.length > 60                     // the first one: a real paragraph, not a table cell
          : (n > lastNum && n <= lastNum + 3);   // sequential, tolerating a number lost in conversion
        if (n <= 2000 && !/^0\d/.test(num) && opensSentence && inSequence) {
          lastNum = n;
          inFrontMatter = false;
          emit(h('p', { class: 'jp jp-num' },
            h('span', { class: 'jn', text: num }), h('span', { text: rest })));
          return;
        }
      }
      if (oneLine.length > 140) inFrontMatter = false;   // a prose block: past the masthead
      // the text of a footnote, following its number — set small like the number
      var afterNote = lastNode && /\bjp-fn\b|\bjp-fntext\b/.test(lastNode.className || '');
      var noteLike = oneLine.length < 300 && (FOOTNOTE_START.test(oneLine) || FOOTNOTE_REF.test(oneLine) ||
        CITATION.test(oneLine) || REPORTED.test(oneLine) || oneLine.length < 120);
      emit(h('p', { class: afterNote && noteLike ? 'jp jp-fntext' : 'jp', text: oneLine }));
    });
    flushMarker();
    flushPending();
    return out;
  }

  function titleish(s) {
    return s.replace(/\w\S*/g, function (w) { return w.charAt(0) + w.slice(1).toLowerCase(); });
  }

  function renderDetail(id) {
    var c = findCase(id);
    var backHref = '#/' + filtersToQuery();

    if (!c) {
      document.title = 'Case not found — The Case-Law Review';
      app.innerHTML = '';
      app.appendChild(h('div', { class: 'view wrap' },
        h('a', { class: 'back', href: backHref }, h('span', { html: ICON.arrowL, 'aria-hidden': 'true' }), 'Back to index'),
        h('div', { class: 'state' }, 'That case is not in the library.',
          h('span', { class: 'sub' }, 'It may have been removed, or the link is out of date.'))
      ));
      app.focus({ preventScroll: true });
      window.scrollTo(0, 0);
      return;
    }

    document.title = (c.caseName || 'Case') + ' — The Case-Law Review';

    var files = c.files || {};
    // source links: AustLII / JADE, with sourceUrl as a fallback when both are absent
    var sourceLinks = [
      docLink(c.austliiUrl, 'Full judgment · AustLII', ICON.ext),
      docLink(c.jadeUrl, 'BarNet Jade', ICON.ext),
      docLink(c.sourceUrl, c.sourceLabel || 'Source', ICON.ext)
    ].filter(Boolean);
    // download buttons (optional files committed by the pipeline)
    var downloads = [
      dlButton(files.judgment, 'Download judgment (PDF)', (c.id || 'judgment') + '-judgment.pdf'),
      dlButton(files.llm, 'Download LLM file (.md)', (c.id || 'case') + '.md')
    ].filter(Boolean);
    var links = downloads.concat(sourceLinks);

    var held = !!c.needsReview;
    var tier = held ? '' : (String(c.relevance || '').toUpperCase() === 'ACTION' ? 'Action.' : 'Awareness.');

    var view = h('div', { class: 'view wrap detail' },
      h('a', { class: 'back', href: backHref }, h('span', { html: ICON.arrowL, 'aria-hidden': 'true' }), 'Back to index'),

      h('div', { class: 'detail-head' },
        h('div', { class: 'label case-court', text: c.court || '' }),
        h('h1', { class: 'detail-name', text: c.caseName || 'Untitled' }),
        c.citation ? h('div', { class: 'detail-cite', text: c.citation }) : null,
        c.oneLine ? h('p', { class: 'detail-oneline', html: sanitizeInline(c.oneLine) }) : null,
        h('div', { class: 'detail-meta' }, badge(c.relevance, c.needsReview), tagPills(c.tags))
      ),

      held ? h('div', { class: 'hold-notice', role: 'note' },
        h('b', {}, 'Held for review. '),
        'This write-up did not pass its fact-check against the judgment, so it carries no Action/Awareness call. Treat every statement below as unverified until the hold is cleared.') : null,

      h('div', { class: 'facts' },
        fact('Citation', c.citation),
        fact('Court', c.court),
        fact('Decided', c.decided || fmtDate(c.date)),
        fact('On appeal from', c.appealFrom),
        fact('Outcome', c.outcome),
        fact('Weight in WA', c.weight)
      ),

      section('What happened', c.whatHappened),
      section('What the Court held', c.whatHeld),
      section('What it means for your casework', c.whatItMeans),

      c.verdict ? h('div', { class: 'verdict' },
        h('div', { class: 'vlabel' }, held ? 'Draft call — held for review' : 'Does this apply to you?'),
        h('div', { class: 'vbody' }, h('span', { class: 'tier serif' }, tier), ' ',
          h('span', { html: sanitizeInline(c.verdict) }))
      ) : null,

      links.length ? h('div', { class: 'links' }, links) : null,

      files.llm ? judgmentSection(c, files.llm) : null
    );

    app.innerHTML = '';
    app.appendChild(view);
    app.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  /* ---------- error ---------- */
  function renderError(err) {
    var local = location.protocol === 'file:';
    app.innerHTML = '';
    app.appendChild(h('div', { class: 'view wrap' },
      h('div', { class: 'state' },
        local ? 'This archive needs to be served over http.' : 'The case library could not be loaded.',
        h('span', { class: 'sub' },
          local
            ? 'Open it through the published site, or run a local server (e.g. "python3 -m http.server") instead of opening the file directly.'
            : 'Could not read data/cases.json (' + (err && err.message ? err.message : 'unknown error') + ').')
      )
    ));
  }

  /* ---------- go ---------- */
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();
