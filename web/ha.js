/*
 * Home Assistant through tv-bridge (ha-bridge/ in this repository), on the home network. The TV holds no token: it
 * reads the bridge's view (the household strip, the player, the action tiles, the doorbell) and asks it to run an
 * action by id; the bridge decides what that does and refuses anything else. Without an address (Home Settings →
 * Home Assistant, kept on this TV) none of it runs. Live updates come as Server-Sent Events; where EventSource is missing (old TVs), the view is read every 10 s.
 */
var HA = (function () {
  'use strict';

  var view = null, status = 'off', stream = null, pollTimer = 0, wasOk = false, heardAt = 0, watchTimer = 0;
  var SILENT = 50000;   // the bridge sends at least a ping every 20 s: this long without a word, and it's gone
  var listeners = [], rings = [];

  /** The bridge's address: typed on the TV (Home Settings), else a build's own. Read each time: it can change. */
  function base() { return Prefs.haBridge().replace(/\/+$/, ''); }
  function available() { return !!base(); }
  function on() { return available(); }

  function changed() { for (var i = 0; i < listeners.length; i++) listeners[i](); }

  function take(v) {
    if (!v || typeof v !== 'object') return;
    view = v;
    status = v.connected ? 'ok' : 'down';   // 'down': the bridge answers, Home Assistant doesn't
    wasOk = wasOk || status === 'ok';
    changed();
  }

  function rang(d) {
    for (var i = 0; i < rings.length; i++) rings[i](d);
  }

  function fail() {
    if (status === 'error') return;
    status = 'error';
    changed();
  }

  function get(path, done) {
    var x = new XMLHttpRequest(), over = false;
    x.open('GET', base() + path, true);
    x.timeout = 8000;
    x.onreadystatechange = function () {
      if (x.readyState !== 4 || over) return;
      over = true;
      if (x.status !== 200) { done(new Error('HTTP ' + x.status)); return; }
      var json;
      try { json = JSON.parse(x.responseText); } catch (e) { done(e); return; }
      done(null, json);
    };
    x.ontimeout = x.onerror = function () { if (!over) { over = true; done(new Error('no connection')); } };
    x.send();
  }

  function poll() {
    clearTimeout(pollTimer);
    get('/view', function (err, v) {
      if (err) fail(); else take(v);
      if (stream === 'poll') pollTimer = setTimeout(poll, 10000);
    });
  }

  function start() {
    stop();
    if (!on()) { view = null; status = 'off'; changed(); return; }
    if (status === 'off') { status = 'connecting'; changed(); }
    if (!window.EventSource) { stream = 'poll'; poll(); return; }
    // EventSource reconnects by itself after an error (a restart of the bridge, the Wi-Fi coming back).
    var es = stream = new window.EventSource(base() + '/events');
    heardAt = Date.now();
    es.addEventListener('view', function (e) { heardAt = Date.now(); try { take(JSON.parse(e.data)); } catch (x) {} });
    es.addEventListener('ding', function (e) { heardAt = Date.now(); try { rang(JSON.parse(e.data)); } catch (x) {} });
    es.addEventListener('ping', function () { heardAt = Date.now(); });
    es.onerror = function () { if (stream === es) fail(); };
    // A bridge that vanishes (power cut, Wi-Fi drop) may never send an error: silence is the sign. Start over.
    watchTimer = setInterval(function () {
      if (stream === es && Date.now() - heardAt > SILENT) { fail(); start(); }
    }, 15000);
  }

  function stop() {
    clearTimeout(pollTimer);
    clearInterval(watchTimer);
    if (stream && stream !== 'poll') { try { stream.close(); } catch (e) {} }
    stream = null;
  }

  /** Whether a bridge answers at this address (before saving it); done(error). */
  function check(addr, done) {
    var x = new XMLHttpRequest(), over = false;
    x.open('GET', addr + '/health', true);
    x.timeout = 6000;
    x.onreadystatechange = function () {
      if (x.readyState !== 4 || over) return;
      over = true;
      var ok = false;
      try { ok = x.status === 200 && JSON.parse(x.responseText).ok === true; } catch (e) {}
      done(ok ? null : new Error(x.status ? 'HTTP ' + x.status : 'no connection'));
    };
    x.ontimeout = x.onerror = function () { if (!over) { over = true; done(new Error('no connection')); } };
    x.send();
  }

  /** Asks the bridge to run an action by id; done(error). text/plain, so the TV's browser sends it without a preflight. */
  function run(id, done) {
    var x = new XMLHttpRequest(), over = false;
    x.open('POST', base() + '/call', true);
    x.timeout = 10000;
    x.setRequestHeader('Content-Type', 'text/plain');
    x.onreadystatechange = function () {
      if (x.readyState !== 4 || over) return;
      over = true;
      if (done) done(x.status === 200 ? null : new Error(x.status ? x.responseText || 'HTTP ' + x.status : 'no connection'));
    };
    x.ontimeout = x.onerror = function () { if (!over) { over = true; if (done) done(new Error('no connection')); } };
    x.send(JSON.stringify({action: id}));
  }

  return {
    available: available,
    on: on,
    start: start,
    stop: stop,
    run: run,
    check: check,
    /** 'off', 'connecting', 'ok', 'down' (Home Assistant away) or 'error' (the bridge away). */
    status: function () { return status; },
    /** True once it has worked since the app started: a later error is worth a word, a first one is a setup issue. */
    worked: function () { return wasOk; },
    view: function () { return status === 'ok' || status === 'down' ? view : null; },
    snapshot: function () { return base() + '/snapshot?t=' + Date.now(); },
    art: function (key) { return base() + '/art?v=' + encodeURIComponent(key); },
    onChange: function (f) { listeners.push(f); },
    onRing: function (f) { rings.push(f); }
  };
})();
