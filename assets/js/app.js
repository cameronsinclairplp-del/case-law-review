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
  function badge(rel) {
    var action = String(rel || '').toUpperCase() === 'ACTION';
    return h('span', { class: 'badge ' + (action ? 'badge--action' : 'badge--awareness') }, action ? 'Action' : 'Awareness');
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
        h('div', { class: 'case-meta' }, badge(c.relevance), tagPills(c.tags))
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

  // Format plain judgment text into readable nodes: stacked header blocks,
  // section headings, and hanging paragraph numbers. Heuristic but robust.
  function judgmentNodes(text) {
    var out = [];
    var norm = String(text).replace(/\r/g, '').replace(/\n{3,}/g, '\n\n')
      .replace(/\n(?=\d{1,4}\.\s)/g, '\n\n');   // each numbered paragraph starts its own block
    var blocks = norm.split(/\n{2,}/);
    // The masthead recognizers below apply only while we're still in the front
    // matter (top of the document). Once the reasons begin (a numbered paragraph or
    // a long prose block) this flips off, so a body per-judge reasons heading
    // ("MAZZA JA", "Brennan and Toohey JJ.") stays a prominent section divider
    // rather than being demoted to a quiet coram subtitle.
    var inFrontMatter = true;
    blocks.forEach(function (raw) {
      var lines = raw.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      if (!lines.length) return;
      // peel a leading standalone label (ORDER / HELD / INTRODUCTION ...) into a heading
      var lead = lines[0];
      if (lines.length > 1 && lead.length <= 40 &&
          /^(orders?|held|introduction|background|conclusion|disposition|result|reasons|catchwords)\b[:.]?$/i.test(lead)) {
        out.push(h('h4', { class: 'jh', text: lead.length <= 14 ? lead : titleish(lead) }));
        lines = lines.slice(1);
        if (!lines.length) return;
      }
      var oneLine = lines.join(' ').replace(/\s+/g, ' ').trim();

      // ---- party rows & connectors -> masthead. Identified by a tab / multi-space
      // column separator or a lone "AND"/"v" — shapes body prose never produces — so
      // they're recognised anywhere (a joined appeal repeats a party block mid-document).
      if (/^(?:and|v|-v-|&)$/i.test(oneLine)) {
        out.push(h('div', { class: 'jconn', text: /^(?:v|-v-)$/i.test(oneLine) ? 'v' : 'and' }));
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
          out.push(h('div', { class: 'jcourt', text: oneLine }));
          return;
        }
        // the bench. Skip when the block LEADS with the court name: that whole block
        // is a clean stacked header, better left to the jhead path below.
        if (!COURT_LINE.test(lines[0]) && (CORAM_PREFIX.test(oneLine) || looksLikeCoram(oneLine))) {
          out.push(h('div', { class: 'jcoram', text: oneLine.replace(CORAM_PREFIX, '') }));
          return;
        }
      }

      // short, multi-line, non-numbered block -> a stacked header (court/parties/coram)
      var isHeaderBlock = lines.length >= 2 && lines.length <= 8 &&
        lines.every(function (l) { return l.length < 52 && !/^\d/.test(l); });
      if (isHeaderBlock) {
        var box = h('div', { class: 'jhead' });
        lines.forEach(function (l) { box.appendChild(h('div', { class: 'jhead-line', text: l })); });
        out.push(box);
        return;
      }
      // "LABEL : value" header metadata (jurisdiction / coram / citation / ...) -> compact row
      var meta = oneLine.match(/^([A-Z][A-Za-z()\/ .]{1,28}?)\s:\s+(\S.*)$/);
      if (meta && meta[1].trim() === meta[1].trim().toUpperCase()) {
        out.push(h('p', { class: 'jmeta' },
          h('span', { class: 'jmeta-k', text: titleish(meta[1].trim()) }),
          h('span', { class: 'jmeta-v', text: meta[2] })));
        return;
      }
      // section heading: short, ALL-CAPS or a known label, no trailing sentence punctuation
      var isCaps = /[A-Z]/.test(oneLine) && oneLine === oneLine.toUpperCase();
      var isHeadingWord = /^(orders?|introduction|background|conclusion|disposition|catchwords|result|the appeal|grounds? of appeal|reasons)\b/i.test(oneLine);
      if (oneLine.length <= 80 && (isCaps || isHeadingWord) && !/[.,;]$/.test(oneLine)) {
        out.push(h('h4', { class: 'jh', text: oneLine.length <= 14 ? oneLine : titleish(oneLine) }));
        return;
      }
      // numbered paragraph -> hanging number in the gutter (reasons have begun)
      var nm = oneLine.match(/^(\d{1,4})\.?\s+(\S[\s\S]*)$/);
      if (nm && parseInt(nm[1], 10) <= 2000) {
        inFrontMatter = false;
        out.push(h('p', { class: 'jp jp-num' },
          h('span', { class: 'jn', text: nm[1] }), h('span', { text: nm[2] })));
        return;
      }
      if (oneLine.length > 140) inFrontMatter = false;   // a prose block: past the masthead
      out.push(h('p', { class: 'jp', text: oneLine }));
    });
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

    var tier = String(c.relevance || '').toUpperCase() === 'ACTION' ? 'Action.' : 'Awareness.';

    var view = h('div', { class: 'view wrap detail' },
      h('a', { class: 'back', href: backHref }, h('span', { html: ICON.arrowL, 'aria-hidden': 'true' }), 'Back to index'),

      h('div', { class: 'detail-head' },
        h('div', { class: 'label case-court', text: c.court || '' }),
        h('h1', { class: 'detail-name', text: c.caseName || 'Untitled' }),
        c.citation ? h('div', { class: 'detail-cite', text: c.citation }) : null,
        c.oneLine ? h('p', { class: 'detail-oneline', html: sanitizeInline(c.oneLine) }) : null,
        h('div', { class: 'detail-meta' }, badge(c.relevance), tagPills(c.tags))
      ),

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
        h('div', { class: 'vlabel' }, 'Does this apply to you?'),
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
