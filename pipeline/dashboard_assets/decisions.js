// --- decisions: a click becomes a line in decisions/pending.json ---------
  // This page is static and has no server, so a decision can only be recorded
  // the way everything else here travels: as a file in git. A click queues an
  // entry, a debounced push commits the queue to decisions/pending.json through
  // the GitHub contents API, and pipeline/decisions.py folds it into the DB on
  // the next fold: a routine push, or the one detect.yml dispatches when
  // decisions are waiting, so within about 2h. A click starts no workflow
  // (W7, 2026-10-13). The DB stays the source of truth; a badge is only a note.
  //
  // EVERY localStorage and network call below is gated on cfg.actions, which is
  // true in the published build only. Chrome shares localStorage across ALL
  // file:// documents, so a token typed into the local copy would be readable by
  // every other local page on this machine -- the local build gets badges and a
  // link to the live page instead.
  //
  // Its OWN IIFE since 2026-08-23: this file used to close the one app.js
  // opens, which became a syntax error the moment dashboard.py stopped
  // inlining it into the local build. The only thing it wants from app.js is
  // window.jsosTray -- three lines, in paint() and unpaint() below.
(function () {
  var CFG = (function () {
    var el = document.getElementById('cfg');
    try { return JSON.parse((el && el.textContent) || '{}'); } catch (e) { return {}; }
  })();
  // PUSH_MS: 10s until W7. A push is now free but still a commit on main that
  // detect's push loop has to replay over, so a triage session batches its
  // clicks; leaving the tab pushes at once (flushOnHide).
  var QKEY = 'jsos.decisions', PKEY = 'jsos.pat', PUSH_MS = 120000;
  var ID_OK = /^[0-9a-f]{40}$/;
  // decisions.ACTIONS, not the page's own DECISION_ACTIONS: that one drops
  // `cancel` because a cancel is never a BADGE, and mirroring it here would make
  // every tombstone junk. merge() excluding cancels is the only path that ever
  // removes an id from the file, so a retraction would then never land.
  var ACT_OK = /^(applied|rejected|skipped|interview|offer|promote|cancel)$/;
  // What new Date().toISOString() produces, which is the only thing that ever
  // writes this field. Lexical order over this shape IS chronological order,
  // which is what merge() and decisions._read both rely on. Deliberately
  // STRICTER than decisions.parse_at, which also takes a +02:00 offset and a
  // bare date: an entry this side cannot read is passed through as junk rather
  // than dropped, so being strict costs nothing and never lets one win an id.
  var AT_OK = /^\d{4}-\d{2}-\d{2}T[\d:.]+Z$/;
  // Mirrors decisions.WEDGED_HOURS. See fresh().
  var MAX_AGE_MS = 24 * 60 * 60 * 1000;
  var queue = [], pushTimer = null, pushing = false;

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function pat() { return lsGet(PKEY) || ''; }
  function saveQueue() { lsSet(QKEY, JSON.stringify(queue)); }
  function served(id) { return (CFG.pending || {})[id] || ''; }

  function note(msg) {
    var n = document.getElementById('err');
    if (!n) return;
    n.textContent = msg || '';
    n.classList.toggle('on', !!msg);
  }

  // One job can be on the page twice: a tailored role is a band card AND a table
  // row. Every paint loops, or clicking in the band leaves live buttons below.
  function elemsFor(id) { return document.querySelectorAll('[data-id="' + id + '"]'); }
  function cellOf(el) { return el.querySelector('.st, .stt'); }

  function paint(id, action) {
    Array.prototype.forEach.call(elemsFor(id), function (el) {
      var cell = cellOf(el);
      if (!cell) return;
      // Hidden, not removed: cancel has to be able to put them back without a
      // reload, and re-deriving the button set in JS would fork the rules that
      // ROW_ACTIONS owns in Python. Disabled as well as hidden, so a CSS that
      // never loaded still cannot produce a second decision for the same row.
      Array.prototype.forEach.call(cell.querySelectorAll('.acts, .pending:not(.pend-d)'),
        function (n) { n.classList.add('off'); });
      Array.prototype.forEach.call(cell.querySelectorAll('.act'),
        function (n) { n.disabled = true; });
      var b = cell.querySelector('.pend-d');
      if (!b) { b = document.createElement('div'); b.className = 'pending pend-d'; cell.appendChild(b); }
      b.textContent = (window.jsosTray ? window.jsosTray.note(action)
                                       : 'pending: ' + action + ' (awaiting sync)') + ' ';
      var c = document.createElement('a');
      c.href = '#'; c.className = 'cancel'; c.setAttribute('data-cancel', id);
      c.textContent = 'cancel';
      b.appendChild(c);
      // Into the tray: the column it stood in counts decisions still to make.
      if (window.jsosTray) window.jsosTray.put(el);
    });
  }

  function unpaint(id) {
    Array.prototype.forEach.call(elemsFor(id), function (el) {
      var cell = cellOf(el);
      if (!cell) return;
      Array.prototype.forEach.call(cell.querySelectorAll('.pend-d'),
        function (n) { n.parentNode.removeChild(n); });
      Array.prototype.forEach.call(cell.querySelectorAll('.acts, .pending'),
        function (n) { n.classList.remove('off'); });
      Array.prototype.forEach.call(cell.querySelectorAll('.act'),
        function (n) { n.disabled = false; });
      // Back to the exact place in the exact column it came from.
      if (window.jsosTray) window.jsosTray.back(el);
    });
  }

  function enqueue(id, action) {
    for (var i = 0; i < queue.length; i++) {
      // Replace by id, never append: the fold keeps one entry per id anyway, and
      // two entries would only differ in which one the merge happened to drop.
      if (queue[i].id === id) { queue.splice(i, 1); break; }
    }
    queue.push({ id: id, action: action, at: new Date().toISOString() });
    saveQueue();
    schedule();
  }

  function cancel(id) {
    for (var i = 0; i < queue.length; i++) {
      if (queue[i].id === id) { queue.splice(i, 1); break; }
    }
    // A tombstone, not just a local delete. The entry may already be in the file,
    // and then dropping it here would only let the next merge re-adopt the
    // server's copy. It is stripped before the PUT, so no `cancel` ever ships.
    queue.push({ id: id, action: 'cancel', at: new Date().toISOString() });
    saveQueue();
    unpaint(id);
    schedule();
  }

  function schedule() {
    if (pushTimer) clearTimeout(pushTimer);
    pushTimer = setTimeout(function () { push(false); }, PUSH_MS);
  }

  // Entries expire from THIS BROWSER at 24h. Without it a tab left open for
  // days re-merges and re-PUTs every decision it ever recorded on each push,
  // and once another writer (/mark on the laptop, a second device) has moved
  // the row past that action, the fold re-applies the stale mark -- a revert
  // loop that looks like the DB fighting itself. Dropping is safe for anything
  // that reached the file: decisions/pending.json is the durable record, and an
  // entry still sitting in it after 24h is already the fold's WEDGED report,
  // not this tab's job. The one thing it does forget is a decision that NEVER
  // pushed -- no token entered, or a full day of failures -- and that is the
  // accepted trade: a click that could not be saved for 24h is not a decision
  // the pipeline knows about, and re-clicking it is one keystroke.
  function fresh(list) {
    var cut = Date.now() - MAX_AGE_MS;
    return list.filter(function (e) {
      var t = Date.parse(e && e.at);
      return t === t && t >= cut;        // NaN !== NaN drops an unreadable stamp
    });
  }

  // The SAME contract decisions._read enforces, and it has to be the same one.
  // merge() keeps a single entry per id by lexical `at`, so a server entry this
  // side waves through -- action 'delete', at 'banana' -- wins that comparison
  // against every real ISO timestamp forever, and the fold preserves malformed
  // entries by design, so the junk re-arrives on the next read and shadows the
  // job's real decisions permanently. A lenient client validator over a
  // last-write-wins merge is a silent-loss shape, not a leniency.
  function valid(e) {
    return !!e && ID_OK.test(String(e.id)) && ACT_OK.test(String(e.action))
      && AT_OK.test(String(e.at));
  }
  function byAt(a, b) {
    if (a.at !== b.at) return a.at < b.at ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }
  // Order-insensitive over the entries this page understands, so a file that
  // already says what the merge would say is never rewritten. Unreadable
  // entries keep their file order, which is the only order they have.
  function sig(list) {
    var good = [], junk = [], i;
    for (i = 0; i < list.length; i++) {
      if (valid(list[i])) good.push(list[i]); else junk.push(JSON.stringify(list[i]));
    }
    return good.sort(byAt).map(function (e) {
      return e.id + '|' + e.action + '|' + e.at;
    }).concat(junk).join(';');
  }

  function merge(server) {
    // decisions._read's rule, client side: at most one entry per id, latest `at`
    // wins. A winning `cancel` deletes the id instead of shipping, which is how a
    // retraction reaches an entry that already reached the file.
    var by = {}, junk = [], i, e, prev;
    function take(e) {
      prev = by[e.id];
      if (!prev || String(e.at) >= String(prev.at)) {
        by[e.id] = { id: String(e.id), action: String(e.action), at: String(e.at) };
      }
    }
    for (i = 0; i < server.length; i++) {
      e = server[i];
      // An entry this page cannot read is passed through untouched, exactly as
      // decisions.py leaves it: it is a decision Vaish believes he recorded, and
      // deleting it here would lose it silently while looking like housekeeping.
      if (valid(e)) take(e); else junk.push(e);
    }
    for (i = 0; i < queue.length; i++) { if (valid(queue[i])) take(queue[i]); }
    var out = [];
    for (var k in by) { if (by[k].action !== 'cancel') out.push(by[k]); }
    return out.sort(byAt).concat(junk);
  }

  // UTF-8 safe both ways. The file is ASCII in practice, but btoa throws on any
  // code point above 255 and would take the whole push down with it.
  function b64decode(s) {
    var bin = atob(String(s).replace(/[^A-Za-z0-9+/=]/g, ''));
    var a = new Uint8Array(bin.length), i;
    for (i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(a);
  }
  function b64encode(s) {
    var b = new TextEncoder().encode(s), bin = '', i;
    for (i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
    return btoa(bin);
  }

  function api(method, body, then, fail) {
    var url = 'https://api.github.com/repos/' + CFG.owner + '/' + CFG.repo +
              '/contents/' + CFG.path + (method === 'GET' ? '?ref=' + CFG.branch : '');
    fetch(url, {
      method: method,
      // An authenticated contents GET comes back cacheable for 60s, and a cached
      // sha is exactly what the 409 retry must not re-read.
      cache: 'no-store',
      headers: {
        'Authorization': 'Bearer ' + pat(),
        'Accept': 'application/vnd.github+json'
      },
      body: body ? JSON.stringify(body) : null
    }).then(function (r) {
      return r.json().then(function (j) { then(r.status, j); },
                           function () { then(r.status, null); });
    }).catch(fail);
  }

  function push(isRetry) {
    var before = queue.length;
    queue = fresh(queue);
    if (queue.length !== before) saveQueue();
    if (!queue.length) return;
    // Re-arm rather than drop: a click landing while a slow push is in flight
    // would otherwise sit in localStorage until the next click or reload, which
    // is the silent-loss shape this whole file is written against.
    if (pushing) { schedule(); return; }
    if (!pat()) { openPanel(); return; }
    pushing = true;

    function netfail() {
      pushing = false;
      note('Network error saving decisions. They are kept in this browser and'
           + ' go out on your next click or reload.');
    }
    function done() {
      pushing = false;
      // Tombstones are spent the moment the file no longer names the id. Real
      // entries stay until the fold moves the row: that is what reconcile reads.
      queue = queue.filter(function (e) { return e.action !== 'cancel'; });
      saveQueue();
      note('');
    }

    api('GET', null, function (st, data) {
      if (st !== 200 || !data || typeof data.content !== 'string') {
        pushing = false;
        note('Could not read ' + CFG.path + ' (HTTP ' + st + '). Check the token in'
             + ' the settings panel. Your clicks are saved.');
        return;
      }
      var server = null;
      try { server = JSON.parse(b64decode(data.content)); } catch (e) { server = null; }
      if (!(server instanceof Array)) {
        // Same call decisions.py makes: there is nothing to merge into, and
        // overwriting would destroy whatever has to be fixed by hand.
        pushing = false;
        note(CFG.path + ' is not a JSON array right now. Nothing was written; fix'
             + ' the file in the repo.');
        return;
      }
      var out = merge(server);
      if (sig(out) === sig(server)) { done(); return; }
      api('PUT', {
        message: 'dashboard: ' + queue.length + ' decision(s)',
        content: b64encode(JSON.stringify(out, null, 2) + '\n'),
        sha: data.sha,
        branch: CFG.branch
      }, function (st2) {
        pushing = false;
        if (st2 === 200 || st2 === 201) { done(); return; }
        // Someone committed between the GET and the PUT (the hourly cron does,
        // and so does a second tab). Re-read, re-merge, and try exactly once.
        if ((st2 === 409 || st2 === 422) && !isRetry) { push(true); return; }
        note('Could not save decisions (HTTP ' + st2 + '). They are kept in this'
             + ' browser and retry on your next click or reload.');
      }, netfail);
    }, netfail);
  }

  function reconcile() {
    var keep = [];
    queue.forEach(function (e) {
      var els = elemsFor(e.id), landed = false;
      if (e.action === 'cancel') {
        // Nothing left to retract once the file stops naming the id.
        landed = !served(e.id);
      } else if (els.length) {
        var st = els[0].getAttribute('data-status') || '';
        var lane = els[0].getAttribute('data-lane') || '';
        // A promote never becomes a status: decisions.py sends it to db.set_lane,
        // so the row's LANE is what proves it landed. The brief's status rule
        // alone would leave every promote badged forever.
        landed = e.action === 'promote'
          ? (lane === 'auto-tailor' || (st !== 'scored' && st !== 'new'))
          : st === e.action;
      }
      if (landed) return;
      keep.push(e);
      if (e.action !== 'cancel') paint(e.id, e.action);
    });
    queue = keep;
    saveQueue();
    // Retry whatever the last push never landed, and nothing else: a browser
    // whose entries are all in the served file makes no network call on load.
    var unsent = queue.some(function (e) {
      return e.action === 'cancel' ? !!served(e.id) : served(e.id) !== e.action;
    });
    if (unsent) schedule();
  }

  function openPanel() {
    var box = document.getElementById('pat');
    if (!box) return;
    box.classList.add('on');
    var inp = document.getElementById('pati');
    if (inp) inp.focus();
  }
  function patState() {
    var s = document.getElementById('patstate');
    if (s) s.textContent = pat() ? 'A token is stored in this browser.'
                                 : 'No token stored yet.';
  }

  function startDecisions() {
    var stored = null;
    try { stored = JSON.parse(lsGet(QKEY) || '[]'); } catch (e) { stored = null; }
    // valid(), not a second looser copy of it: two validators over one merged
    // file is the whole defect this contract is written against.
    queue = fresh((stored instanceof Array ? stored : []).filter(valid));

    document.addEventListener('click', function (ev) {
      var t = ev.target;
      if (!t || !t.getAttribute) return;
      if (t.classList && t.classList.contains('act')) {
        var host = t.closest('[data-id]');
        var id = host && host.getAttribute('data-id');
        var action = t.getAttribute('data-act');
        if (!id || !action) return;
        // No token, no queue entry: the click would sit in localStorage looking
        // recorded while nothing could ever push it.
        if (!pat()) { openPanel(); return; }
        enqueue(id, action);
        paint(id, action);
        return;
      }
      var c = t.getAttribute('data-cancel');
      if (c) { ev.preventDefault(); cancel(c); }
    });

    var gear = document.getElementById('gear');
    if (gear) gear.onclick = function (ev) {
      ev.preventDefault();
      document.getElementById('pat').classList.toggle('on');
      patState();
    };
    var save = document.getElementById('patsave');
    if (save) save.onclick = function () {
      var inp = document.getElementById('pati');
      var v = (inp.value || '').trim();
      if (v) lsSet(PKEY, v);
      inp.value = '';                       // never leave the token in the DOM
      document.getElementById('pat').classList.remove('on');
      patState();
      if (queue.length) schedule();
    };
    var forget = document.getElementById('patclear');
    if (forget) forget.onclick = function () {
      try { localStorage.removeItem(PKEY); } catch (e) {}
      patState();
    };

    patState();
    reconcile();
    flushOnHide();
  }

  // Switching tabs or minimising pushes the batch now rather than in 2 min.
  // Closing the tab may cut the GET/PUT pair short; the clicks stay in
  // localStorage and go out on the next load, as they always have.
  function flushOnHide() {
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'hidden' || !queue.length) return;
      if (pushTimer) { clearTimeout(pushTimer); pushTimer = null; }
      push(false);
    });
  }

  // readHash()/apply() moved to app.js with the IIFE split -- they are the
  // board's bootstrap, and the local build has to run them without this file.
  if (CFG.actions) startDecisions();
})();
