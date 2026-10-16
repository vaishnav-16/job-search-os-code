
// --- theme -----------------------------------------------------------------
// A CLOSED IIFE of its own, ahead of everything else. app.js and decisions.js
// are two halves of ONE open IIFE -- decisions.js closes it and calls functions
// defined here -- so this block cannot go at the end of the file, and keeping it
// separate is what lets the theme control work even if the board never renders.
//
// Three states, and "auto" is a state rather than the absence of one. The
// pressed button therefore reflects the STORED preference, never the resolved
// theme -- under a dark OS with no override it is "auto" that reads pressed and
// "dark" that does not, which is the whole point of a three-state control.
//
// localStorage is allowed here and guarded everywhere. The decisions client
// keeps it at arm's length because of the GitHub token's blast radius; a colour
// preference has none. The guard is for browsers with site data blocked, where
// getItem itself throws -- a dead toggle is better than a dead page.
(function () {
  var bar = document.querySelector('.themebar');
  if (!bar) return;
  var KEY = 'jsos.theme', root = document.documentElement;
  var btns = bar.querySelectorAll('button[data-theme-set]');

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return (v === 'dark' || v === 'light') ? v : 'auto';
    } catch (e) { return 'auto'; }
  }

  function paint(mode) {
    // 'auto' REMOVES the attribute rather than setting it to "auto": the
    // prefers-color-scheme block is guarded on :not([data-theme="light"]), so
    // ANY present value would read as a live override of the OS setting.
    if (mode === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);
    btns.forEach(function (b) {
      b.setAttribute('aria-pressed',
        b.getAttribute('data-theme-set') === mode ? 'true' : 'false');
    });
  }

  btns.forEach(function (b) {
    b.onclick = function () {
      var mode = b.getAttribute('data-theme-set');
      try {
        if (mode === 'auto') localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, mode);
      } catch (e) { /* not persisted; the page still switches for this visit */ }
      paint(mode);
    };
  });

  // The inline bootstrap in <head> has already set the attribute before first
  // paint; this only brings the pressed states into agreement with it.
  paint(stored());
})();

(function () {
  // Rooted on the BOARD, and deliberately not on a guard that can return.
  // Everything below -- search, filters, sort and the view toggle -- used to
  // sit behind `if (!tbody) return`, so the day the table stopped existing the
  // whole page would have gone inert while still looking perfect. An absent
  // board now costs an empty card list and nothing else.
  //
  // This IIFE CLOSES here since 2026-08-23. It used to close at the foot of
  // decisions.js -- a syntax error the moment the local build stopped shipping
  // that half. The two now share exactly one global, window.jsosTray.
  var board = document.getElementById('board');
  var trayHost = document.getElementById('traycards');
  // Tray cards are .card elements OUTSIDE the board. Collected here so a card
  // that arrives already-trayed behaves exactly like a clicked one when it is
  // cancelled back.
  var cards = Array.prototype.slice.call(
    document.querySelectorAll('#board .card, #traycards .card'));
  var cols = board ? Array.prototype.slice.call(board.querySelectorAll('.col')) : [];
  // How many cards a column shows before the "Show the other N" button. Read
  // off the board rather than duplicated here: dashboard.COLUMN_CAP is the one
  // statement of it, and a second copy in JS would drift the first time it moves.
  var CAP = board ? (+board.getAttribute('data-cap') || 0) : 0;
  // The archive's got-away entries are NOT .card elements and live outside the
  // board, so they are never counted, filtered or sorted -- but decisions.js
  // still finds them by data-id, which is the behaviour the old two-shapes
  // rendering had by accident and this has on purpose.

  cards.forEach(function (c, i) {
    c._i = i;
    // Where this card belongs. Sorting re-appends every card to its own host,
    // which is what keeps the Submitted column's three sub-groups intact, and
    // the tray needs somewhere to put a card back. A card ALREADY in the tray
    // has no such parent, so its home comes from its own data-col/data-status
    // -- else cancelling it would file it back into the tray it just left.
    c._host = (trayHost && c.parentNode === trayHost) ? columnHome(c) : c.parentNode;
    // The leading text node, not the whole element: the source badge is a child
    // div, so element text runs the two together ("Stripegreenhouse") and a
    // search for "stripe payments" would miss a row whose company is Stripe.
    // Fall back to the element itself -- `company` is NOT NULL but not
    // non-empty, and one blank-company row would otherwise leave firstChild null
    // and take search, filters and sort down with it.
    var co = c.querySelector('.co');
    c._c = co ? ((co.firstChild || co).textContent || '').toLowerCase() : '';
    // Already lowercased by Python. The card shows company, role and location in
    // three separate elements, so reading them back out on every keystroke would
    // be work the build has already done.
    c._s = c.getAttribute('data-search') || '';
  });

  var box = document.getElementById('q');
  var counter = document.getElementById('count');
  var nohits = document.getElementById('nohits');
  var groups = ['tier', 'status', 'band', 'lane', 'art'];
  var sel = { tier: [], status: [], band: [], lane: [], art: [] };
  var loc = '', term = '', sortKey = '', sortDir = 'asc', view = 'board';
  // Which columns the reader has opened past the cap, by column key. Sticky on
  // purpose: expanding Decide and then typing a search should not silently
  // re-collapse it when the search clears.
  var opened = {};

  function activeNames() {
    var n = [];
    if (term) n.push('search "' + term + '"');
    groups.forEach(function (g) { if (sel[g].length) n.push(g === 'art' ? 'artifacts' : g); });
    if (loc) n.push('location');
    return n;
  }

  function filtering() {
    if (term || loc) return true;
    for (var i = 0; i < groups.length; i++) { if (sel[groups[i]].length) return true; }
    return false;
  }

  function matches(r) {
    if (term && r._s.indexOf(term) < 0) return false;
    if (loc && r.getAttribute('data-bucket') !== loc) return false;
    for (var i = 0; i < groups.length; i++) {
      var g = groups[i], picked = sel[g];
      if (!picked.length) continue;
      var v = r.getAttribute('data-' + g) || '';
      // A chip may stand for several values ("Closed" covers five statuses), so
      // membership is tested against each chip's own list, not against the
      // selection flat. Within a group chips OR; across groups they AND.
      var hit = picked.some(function (p) { return p.split(',').indexOf(v) >= 0; });
      if (!hit) return false;
    }
    return true;
  }

  // The column cap, recomputed from scratch every time. It has to be positional
  // rather than a fixed set of elements: sorting reorders the DOM, so "the first
  // 25" is a different 25 afterwards, and freezing the original 25 as hidden
  // would leave an arbitrary subset on screen after every sort.
  //
  // Any active filter lifts the cap entirely. A card the reader has just
  // searched for must not stay behind "Show the other 167" -- the filter is a
  // more specific request than the cap, and the cap exists only to keep a
  // 192-card column from burying the other four on first load.
  function paintColumns() {
    var lift = filtering();
    cols.forEach(function (col) {
      var key = col.getAttribute('data-col');
      var vis = 0, over = 0;
      Array.prototype.forEach.call(col.querySelectorAll('.card'), function (c) {
        if (c.classList.contains('hide')) { c.hidden = false; return; }
        // Decide's "Scorer skipped" group: collapsed, counted in its own .n2,
        // never in the header or the cap.
        if (c.closest('.aside')) { c.hidden = false; return; }
        vis++;
        var capped = !(lift || opened[key]) && vis > CAP;
        c.hidden = capped;
        if (capped) over++;
      });
      // The header count is the whole matching column, never the drawn subset:
      // 25 on screen plus "Show the other 167" adds up to it in plain sight.
      var n = col.querySelector('.colhead .n');
      if (n) n.textContent = vis;
      var btn = col.querySelector('.showmore');
      if (btn) {
        btn.hidden = over === 0;
        btn.textContent = 'Show the other ' + over;
      }
      Array.prototype.forEach.call(col.querySelectorAll('.grp'), function (g) {
        var sub = 0;
        Array.prototype.forEach.call(g.querySelectorAll('.card'), function (c) {
          if (!c.classList.contains('hide')) sub++;
        });
        var e = g.querySelector('.n2');
        if (e) e.textContent = sub;
      });
    });
  }

  function apply() {
    var shown = 0, total = 0;
    cards.forEach(function (r) {
      // A trayed card is a decision already made: not on the board, not in
      // the count, and never filtered -- that would hide its own cancel link.
      if (!board || !board.contains(r)) {
        r.classList.remove('hide'); r.hidden = false; return;
      }
      total++;
      var ok = matches(r);
      r.classList.toggle('hide', !ok);
      if (ok) shown++;
    });
    paintColumns();
    paintTallies();
    if (counter) counter.innerHTML = '<b>' + shown + '</b> of ' + total + ' roles';
    var names = activeNames();
    if (nohits && shown === 0 && names.length) {
      nohits.innerHTML = 'No roles match <b>' + names.join('</b> + <b>') +
        '</b>. <a href="#" id="clr">Clear all filters</a>';
      nohits.classList.add('on');
      document.getElementById('clr').onclick = function (e) { e.preventDefault(); reset(); };
    } else if (nohits) {
      nohits.classList.remove('on');
    }
    writeHash();
  }

  function num(r, a) { var v = r.getAttribute(a); return v === '' || v === null ? null : +v; }

  // Sorting happens WITHIN each column, never across them: a column is the stage
  // a job is at, and an ordering that could move a card between stages would be
  // reporting something that is not true. Each card goes back to the container
  // it came from, which is also how the Submitted column keeps its sub-groups.
  function sortBy(key) {
    if (sortKey === key) { sortDir = sortDir === 'asc' ? 'desc' : 'asc'; }
    else { sortKey = key; sortDir = (key === 'score' || key === 'ats') ? 'desc' : 'asc'; }
    var dir = sortDir === 'asc' ? 1 : -1;
    var sorted = cards.slice().sort(function (a, b) {
      var r;
      if (key === 'default') { r = a._i - b._i; }
      else if (key === 'score' || key === 'age' || key === 'ats') {
        // Nulls last in BOTH directions: 16 cards have no score and 8 have no
        // parseable date, and floating them to the top on one pick is noise.
        // Most cards have no ATS number at all -- only tailored ones do -- so
        // that key needs the same treatment, and data-ats carries RAW.
        var at = key === 'score' ? 'data-score'
               : key === 'age' ? 'data-best' : 'data-ats';
        var x = num(a, at);
        var y = num(b, at);
        if (x === null && y === null) r = 0;
        else if (x === null) return 1;
        else if (y === null) return -1;
        else r = x - y;
      } else if (key === 'company') {
        r = a._c.localeCompare(b._c);
      } else {
        r = (a.getAttribute('data-' + key) || '').localeCompare(
             b.getAttribute('data-' + key) || '');
      }
      return r ? r * dir : a._i - b._i;
    });
    var hosts = [], frags = [];
    sorted.forEach(function (c) {
      // Never drag a trayed card back: `_host` is where it will RETURN to,
      // and re-appending now would silently un-tray an actioned job.
      if (!board || !board.contains(c)) return;
      var i = hosts.indexOf(c._host);
      if (i < 0) { i = hosts.push(c._host) - 1; frags.push(document.createDocumentFragment()); }
      frags[i].appendChild(c);
    });
    hosts.forEach(function (h, i) { h.appendChild(frags[i]); });
    var s = document.getElementById('sortby');
    // Assigning a value no <option> carries leaves a select BLANK -- it reads as
    // "no sort chosen" while the board is in fact sorted, which is the one thing
    // a control must never do. Read it back and fall to `default` if it did not
    // take. readHash normalizes ahead of this; the re-check is the belt to that
    // brace, because sortBy is also reachable from the select itself.
    if (s && s.value !== key) {
      s.value = key;
      if (s.value !== key) s.value = 'default';
    }
    paintColumns();
    writeHash();
  }

  // Sort keys that used to exist. The table's <th data-sort> set was
  // score/company/age/status/ats/default, and those URLs are bookmarked. Every
  // one still maps except `status`: a stage is a COLUMN now, so an ordering
  // ACROSS stages is something the board cannot express and must not pretend to
  // -- it falls to `default`, and writeHash() then drops the stale `sort=` key
  // rather than leaving the URL claiming a sort the select denies.
  // hasOwnProperty, not a bare lookup: the hash is reader-supplied, and
  // `#sort=constructor` would otherwise hand sortKey a function.
  var SORT_ALIAS = { status: 'default', tier: 'default', best: 'age',
                     posted: 'age', co: 'company' };
  function sortOption(k) {
    var s = document.getElementById('sortby'), ok = false;
    if (s) {
      Array.prototype.forEach.call(s.options, function (o) { if (o.value === k) ok = true; });
    }
    if (ok) return k;
    return Object.prototype.hasOwnProperty.call(SORT_ALIAS, k) ? SORT_ALIAS[k] : 'default';
  }

  function setView(v) {
    view = v === 'list' ? 'list' : 'board';
    if (board) board.setAttribute('data-view', view);
    document.querySelectorAll('[data-view-set]').forEach(function (b) {
      b.setAttribute('aria-pressed',
        b.getAttribute('data-view-set') === view ? 'true' : 'false');
    });
  }

  // The ONE place chip state is written, and both readings move together: the
  // class is what a sighted reader sees, aria-pressed what a screen reader
  // hears, and a toggle repainting only one lies to the other. `sel[g]` is
  // guarded so a chip in an unknown group does nothing instead of throwing and
  // taking the whole filter down with it.
  function paintChips() {
    document.querySelectorAll('.f').forEach(function (el) {
      var g = el.getAttribute('data-g');
      var on = !!sel[g] && sel[g].indexOf(el.getAttribute('data-v')) >= 0;
      el.classList.toggle('on', on);
      el.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  // Chip tallies, recomputed for the same reason the column headers are:
  // chips_for() counts the cards the BOARD holds, so a card moving into the
  // tray leaves every chip that matched it one too high. Deliberately
  // UNFILTERED -- see chips_for() in dashboard.py.
  var chips = Array.prototype.slice.call(document.querySelectorAll('.f'));
  function paintTallies() {
    var tally = {};
    cards.forEach(function (r) {
      if (!board || !board.contains(r)) return;
      groups.forEach(function (g) {
        var k = g + '=' + (r.getAttribute('data-' + g) || '');
        tally[k] = (tally[k] || 0) + 1;
      });
    });
    chips.forEach(function (el) {
      var b = el.querySelector('b');
      if (!b) return;
      var g = el.getAttribute('data-g'), n = 0;
      // A chip may stand for several values, exactly as matches() reads it.
      (el.getAttribute('data-v') || '').split(',').forEach(function (v) {
        n += tally[g + '=' + v] || 0;
      });
      b.textContent = n;
    });
  }

  function reset() {
    groups.forEach(function (g) { sel[g] = []; });
    loc = ''; term = '';
    if (box) box.value = '';
    var ls = document.getElementById('loc'); if (ls) ls.value = '';
    paintChips(); apply();
  }

  // tabindex and the initial aria-pressed are server-rendered now (chips_for in
  // dashboard.py, which carries the why). Nothing to attach here but behaviour.
  document.querySelectorAll('.f').forEach(function (el) {
    function toggle() {
      var g = el.getAttribute('data-g'), v = el.getAttribute('data-v');
      var i = sel[g].indexOf(v);
      if (i < 0) { sel[g].push(v); } else { sel[g].splice(i, 1); }
      paintChips(); apply();
    }
    el.onclick = toggle;
    el.onkeydown = function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    };
  });

  var timer;
  if (box) {
    box.oninput = function () {
      clearTimeout(timer);
      timer = setTimeout(function () { term = box.value.trim().toLowerCase(); apply(); }, 110);
    };
  }
  document.addEventListener('keydown', function (e) {
    if (!box) return;
    if (e.key === '/' && e.target !== box) { e.preventDefault(); box.focus(); box.select(); }
    if (e.key === 'Escape' && e.target === box) { box.value = ''; term = ''; apply(); }
  });
  var ls = document.getElementById('loc');
  if (ls) ls.onchange = function () { loc = ls.value; apply(); };
  var sorter = document.getElementById('sortby');
  if (sorter) sorter.onchange = function () { sortBy(sorter.value); };
  document.querySelectorAll('[data-view-set]').forEach(function (b) {
    b.onclick = function () { setView(b.getAttribute('data-view-set')); writeHash(); };
  });
  document.querySelectorAll('.showmore').forEach(function (b) {
    b.onclick = function () { opened[b.getAttribute('data-more')] = true; paintColumns(); };
  });
  var rst = document.getElementById('reset');
  if (rst) rst.onclick = function (e) { e.preventDefault(); reset(); };

  // The info link on an unmeasured coverage badge. Delegated (cards move), and
  // it OPENS the explainer rather than letting the browser jump to a still-shut
  // <details>. preventDefault keeps #ats-help out of the filter-state hash.
  var help = document.getElementById('ats-help');
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('.atsq') : null;
    if (!a || !help) return;
    e.preventDefault();
    help.open = true;
    help.scrollIntoView({ block: 'nearest' });
    var s = help.querySelector('summary');
    if (s) s.focus();
  });

  // Hash state, so a filtered view survives the page being regenerated under it
  // and can be bookmarked. The URL is the only home for VIEW state; localStorage
  // is touched by the decisions block below, and only in the published build.
  var quiet = false;
  function writeHash() {
    if (quiet) return;
    var p = [];
    if (term) p.push('q=' + encodeURIComponent(term));
    groups.forEach(function (g) { if (sel[g].length) p.push(g + '=' + sel[g].join(',')); });
    if (loc) p.push('loc=' + encodeURIComponent(loc));
    if (sortKey && sortKey !== 'default') p.push('sort=' + sortKey + ':' + sortDir);
    if (view === 'list') p.push('view=list');
    var h = p.join('&');
    if (h !== location.hash.slice(1)) {
      history.replaceState(null, '', h ? '#' + h : location.pathname + location.search);
    }
  }
  function readHash() {
    var h = location.hash.slice(1);
    if (!h) return;
    quiet = true;
    h.split('&').forEach(function (kv) {
      var i = kv.indexOf('='); if (i < 0) return;
      var k = kv.slice(0, i), v = decodeURIComponent(kv.slice(i + 1));
      if (k === 'q') { term = v.toLowerCase(); if (box) box.value = v; }
      else if (k === 'loc') { loc = v; var e = document.getElementById('loc'); if (e) e.value = v; }
      else if (k === 'sort') { var s = v.split(':'); sortKey = sortOption(s[0]); sortDir = s[1] === 'desc' ? 'asc' : 'desc'; }
      else if (k === 'view') { setView(v); }
      else if (groups.indexOf(k) >= 0) { sel[k] = v.split(','); }
    });
    quiet = false;
    paintChips();
    if (sortKey) sortBy(sortKey);
  }

  // --- the queued-to-sync tray ---------------------------------------------
  // Why a clicked card MOVES: dashboard.tray(). All the DOM work lives here,
  // in the file that knows where each card came from and owns the counts;
  // decisions.js reaches it through window.jsosTray in three lines.
  var tray = document.getElementById('tray');
  var TRAY_LABEL = { applied: 'Applied', rejected: 'Rejected', skipped: 'Skipped',
                     interview: 'Interview', offer: 'Offer' };

  // Mirrors dashboard.decision_note(), which carries the why: `promote` must
  // never name a destination, and the concept is "Tailor" wherever it is read.
  function trayNote(action) {
    return action === 'promote'
      ? '\u2192 queued for tailoring (moves to Queued)'
      : '\u2192 will be marked ' + (TRAY_LABEL[action] || action);
  }

  // The Submitted column holds THREE .cards keyed data-sub, so `[data-col=
  // "out"] .cards` alone would file every cancelled interview under Applied.
  function columnHome(el) {
    var col = el.getAttribute('data-col') || '', st = el.getAttribute('data-status') || '';
    var lane = el.getAttribute('data-lane') || '';
    return document.querySelector('[data-col="' + col + '"] .cards[data-sub="' + st + '"]')
        || (col === 'decide' && document.querySelector(
             '[data-col="decide"] .cards[data-sub="' + (lane === 'auto-tailor' || lane === 'skip' ? lane : 'digest') + '"]'))
        || document.querySelector('[data-col="' + col + '"] .cards');
  }

  function traySync() {
    if (!tray) return;
    var n = trayHost ? trayHost.querySelectorAll('.card').length : 0;
    tray.hidden = !n;
    var c = tray.querySelector('.trayn'); if (c) c.textContent = n;
    var p = tray.querySelector('.trays'); if (p) p.textContent = n === 1 ? '' : 's';
    // apply(), not paintColumns(): "N of M roles" is a count too, and one
    // still saying 230 with 228 on the board is the same defect as a header
    // that does.
    apply();
  }

  // Captured BEFORE decisions.js's bubble handler, because paint() hides and
  // disables the pressed button -- that, not the reparent, is what drops focus
  // to <body>. Only set when focus was really inside the card, so a
  // programmatic put (reconcile at load, "Cancel all") never steals it.
  var press = null;
  document.addEventListener('click', function (e) {
    var c = e.target && e.target.closest ? e.target.closest('.card') : null;
    press = (c && c.contains(document.activeElement)) ? c : null;
  }, true);

  function trayPut(el) {
    if (!trayHost || !el.classList || !el.classList.contains('card')) return;
    if (el.parentNode === trayHost) return;          // already here (reconcile)
    var refocus = el.contains(document.activeElement) || press === el;
    press = null;
    // The exact sibling it stood in front of: `_host` alone appends to the
    // bottom of the column, which is not where the card was.
    el._back = el.nextSibling;
    // Cap and filter are COLUMN state; in here they would hide the card the
    // reader just clicked, cancel link and all.
    el.hidden = false;
    el.classList.remove('hide');
    trayHost.appendChild(el);
    if (refocus) {
      // The tray heading is the fallback for a build that renders no cancel
      // link (the local file:// copy) -- it still names where the card went.
      // preventScroll (Vaish 2026-10-12): the tray sits above the board, so a
      // plain focus() jumped the page to the top on every Applied / Skip click
      // and he lost his place in the column. Focus still moves for keyboard
      // and screen-reader users; only the scroll is suppressed.
      var to = el.querySelector('.cancel')
            || (tray && tray.querySelector('.trayhead h2'));
      if (to) {
        try { to.focus({ preventScroll: true }); } catch (e) { /* not focusable; leave it */ }
      }
    }
    traySync();
  }

  function trayBack(el) {
    if (!trayHost || !el.classList || !el.classList.contains('card')) return;
    if (el.parentNode !== trayHost) return;
    var home = el._host || columnHome(el);
    // Move first, THEN recount -- and recount either way. A card with no home
    // (a status the board draws no column for) stays here rather than being
    // dropped, but the tray's own count and hidden state must still be true.
    if (home) {
      home.insertBefore(el, el._back && el._back.parentNode === home ? el._back : null);
      el._back = null;
    }
    traySync();                    // recounts, and re-filters the returned card
  }

  window.jsosTray = { put: trayPut, back: trayBack, note: trayNote };

  // "Cancel all" clicks each trayed card's own cancel anchor -- the one
  // decisions.js already delegates on. ONE retraction path: cancel() owns the
  // tombstone protocol and a second caller is how the two drift apart.
  var cancelAll = document.getElementById('traycancel');
  if (cancelAll) cancelAll.onclick = function () {
    var seen = {};
    Array.prototype.forEach.call(
      document.querySelectorAll('#tray [data-cancel]'), function (a) {
        var id = a.getAttribute('data-cancel');
        if (id && !seen[id]) { seen[id] = 1; a.click(); }
      });
  };

  // Mirrored off the panel, never toggled here: the client opens the panel
  // itself on a tokenless click, and a handler counting gear presses would
  // then read "collapsed" over an open panel forever. See act_ui in main().
  var gear = document.getElementById('gear'), patbox = document.getElementById('pat');
  if (gear && patbox && window.MutationObserver) {
    new MutationObserver(function () {
      gear.setAttribute('aria-expanded',
        patbox.classList.contains('on') ? 'true' : 'false');
    }).observe(patbox, { attributes: true, attributeFilter: ['class'] });
  }

  readHash();
  apply();
  traySync();
})();
