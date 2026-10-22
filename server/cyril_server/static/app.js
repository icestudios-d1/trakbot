/* Cyril dashboard shared code: auth guard, nav, API, date-range bar, small SVG/HTML chart helpers. */
(function () {
  const TB = (window.TB = {});
  const KEY = 'tb_tok';
  const isLogin = location.pathname === '/login';

  /* ---------- auth ---------- */
  TB.token = () => { try { return localStorage.getItem(KEY) || sessionStorage.getItem(KEY) || ''; } catch (e) { return ''; } };
  TB.setToken = (t, remember) => {
    try { localStorage.removeItem(KEY); sessionStorage.removeItem(KEY); (remember ? localStorage : sessionStorage).setItem(KEY, t); } catch (e) { /* storage blocked */ }
  };
  TB.signOut = () => { try { localStorage.removeItem(KEY); sessionStorage.removeItem(KEY); } catch (e) {} location.href = '/login'; };
  const toLogin = () => { location.replace('/login?next=' + encodeURIComponent(location.pathname + location.search)); };
  if (!isLogin && !TB.token()) { toLogin(); }

  TB.api = async (path, opts = {}) => {
    const r = await fetch(path, { ...opts, headers: { Authorization: 'Bearer ' + TB.token(), 'Content-Type': 'application/json', ...(opts.headers || {}) } });
    if (r.status === 401) { if (!isLogin) toLogin(); throw new Error('Not signed in'); }
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
    return r.json();
  };
  TB.blobUrl = async (path) => {
    const r = await fetch(path, { headers: { Authorization: 'Bearer ' + TB.token() } });
    if (!r.ok) throw new Error(r.statusText);
    return URL.createObjectURL(await r.blob());
  };
  TB.tz = -new Date().getTimezoneOffset(); // minutes east of UTC, so the server buckets days/hours in *your* time
  TB.qs = (o) => new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v !== '' && v != null))).toString();

  /* ---------- formatting ---------- */
  TB.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  TB.fmt = (s) => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h ${String(m).padStart(2, '0')}m` : s && !m ? '<1m' : `${m}m`; };
  TB.clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '–';
  TB.when = (ts) => new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  TB.ymd = (d) => d.toLocaleDateString('en-CA'); // local YYYY-MM-DD
  TB.pct = (v) => (v == null ? '–' : Math.round(v) + '%');
  TB.LABELS = [['productive', 'Productive', 'var(--c-p)'], ['unproductive', 'Unproductive', 'var(--c-u)'], ['neutral', 'Neutral', 'var(--c-n)'], ['idle', 'Idle', 'var(--c-i)']];
  TB.labelColor = (l) => (TB.LABELS.find((x) => x[0] === l) || [0, 0, 'var(--c-n)'])[2];
  TB.sum = (o) => Object.values(o).reduce((a, b) => a + b, 0);
  const $ = (s, r = document) => r.querySelector(s);
  TB.$ = $;
  TB.delta = (cur, prev, kind) => {
    if (cur == null || prev == null) return '<span class="dl flat">no prior data</span>';
    const d = cur - prev;
    if (kind === 'pp') { const r = Math.round(d * 10) / 10; return r === 0 ? '<span class="dl flat">same as before</span>' : `<span class="dl ${r > 0 ? 'up' : 'down'}">${r > 0 ? '▲' : '▼'} ${Math.abs(r)} pts vs previous period</span>`; }
    if (!prev) return '<span class="dl flat">no prior data</span>';
    const r = Math.round((100 * d) / prev);
    return r === 0 ? '<span class="dl flat">same as before</span>' : `<span class="dl ${r > 0 ? 'up' : 'down'}">${r > 0 ? '▲' : '▼'} ${Math.abs(r)}% vs previous period</span>`;
  };
  TB.kpi = (label, val, sub) => `<div class="card kpi"><div class="lab">${label}</div><div class="val num">${val}</div><div>${sub || ''}</div></div>`;
  TB.error = (e) => { const el = $('#err'); if (el) { el.textContent = e ? (e.message || e) : ''; el.style.display = e ? 'block' : 'none'; } };

  /* ---------- URL state ---------- */
  TB.params = () => Object.fromEntries(new URLSearchParams(location.search));
  TB.setParams = (o) => { const p = new URLSearchParams(location.search); for (const [k, v] of Object.entries(o)) v ? p.set(k, v) : p.delete(k); history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : '')); };

  /* ---------- nav ---------- */
  const NAV = [['/', 'Overview'], ['/employees', 'Employees'], ['/day', 'Day viewer'], ['/web', 'Web activity'], ['/alerts', 'Alerts'], ['/screenshots', 'Screenshots'], ['/rules', 'Productivity rules'], ['/install', 'Install agents']];
  if (!isLogin) {
    const here = location.pathname === '/employee' ? '/employees' : location.pathname;
    const top = document.createElement('header');
    top.className = 'top';
    top.innerHTML = `<a class="brand" href="/"><i></i>Cyril</a><button class="burger" id="burger" aria-label="Menu" aria-expanded="false" aria-controls="mainnav"><span></span><span></span><span></span></button><nav id="mainnav">${NAV.map(([h, t]) => `<a href="${h}" class="${h === here ? 'on' : ''}">${t}${h === '/alerts' ? '<span id="alertBadge"></span>' : ''}</a>`).join('')}</nav><span class="sm mut" id="build" title="Version of the server code that is running"></span><button class="out" id="signout">Sign out</button>`;
    document.body.prepend(top);
    $('#signout').onclick = TB.signOut;
    $('#burger').onclick = () => { const o = top.classList.toggle('open'); $('#burger').setAttribute('aria-expanded', o); };
    fetch('/api/health').then((r) => r.json()).then((h) => { if (h.build) $('#build').textContent = 'build ' + h.build; }).catch(() => {});
    TB.api('/api/alerts?' + TB.qs({ unacked: 'true', limit: 1000 })).then((a) => { if (a.length) $('#alertBadge').outerHTML = `<span class="badge">${a.length}</span>`; }).catch(() => {});
    const tip = document.createElement('div'); tip.id = 'tip'; document.body.append(tip);
    document.addEventListener('mousemove', (e) => {
      const t = e.target.closest && e.target.closest('[data-tip]');
      if (!t) { tip.style.display = 'none'; return; }
      tip.innerHTML = t.dataset.tip; tip.style.display = 'block';
      const w = tip.offsetWidth, h = tip.offsetHeight;
      tip.style.left = Math.max(4, Math.min(innerWidth - w - 4, e.clientX + 12)) + 'px';
      tip.style.top = Math.max(4, e.clientY - h - 12 < 4 ? e.clientY + 16 : e.clientY - h - 12) + 'px';
    });
  }

  /* ---------- users + range bar ---------- */
  // Display names: an admin can tag a device login with the real person's name; ids stay the login everywhere in the API.
  let _users; const _names = {};
  TB.users = async () => {
    if (!_users) { _users = await TB.api('/api/users'); _users.forEach((u) => { _names[u.user] = u.name || ''; }); }
    return _users;
  };
  TB.dn = (u) => _names[u] || u;                                   // name to show
  TB.who = (u) => { const n = _names[u]; return n ? `${TB.esc(n)} <span class="mut sm">(${TB.esc(u)})</span>` : TB.esc(u); };
  TB.rename = async (user) => {
    const cur = _names[user] || '';
    const v = prompt(`Who is using the "${user}" login?\nThis name is shown instead of the login everywhere. Leave empty to clear it.`, cur);
    if (v === null) return false;
    await TB.api('/api/users/rename', { method: 'POST', body: JSON.stringify({ user, name: v }) });
    _users = null; await TB.users(); return true;
  };

  // Permanently delete an employee and everything recorded about them (typed confirmation, shows the counts first).
  TB.deleteEmployee = async (user) => {
    const pv = await TB.api('/api/users/delete-preview?' + TB.qs({ user }));
    const name = TB.dn(user);
    const warn = pv.still_reporting ? `\n\nWARNING: this PC reported in the last 10 minutes. Uninstall the agent first, or ${name} will reappear as soon as it reports again.` : '';
    const typed = prompt(`Permanently delete ${name} (login "${user}")?\n\nThis removes ${pv.events.toLocaleString()} activity records, ${pv.screenshots.toLocaleString()} screenshots and ${pv.alerts} alerts from the server. It cannot be undone.${warn}\n\nType the login to confirm:`);
    if (typed === null) return false;
    if (typed.trim() !== user) { alert('That did not match the login, so nothing was deleted.'); return false; }
    const r = await TB.api('/api/users/delete', { method: 'POST', body: JSON.stringify({ user }) });
    _users = null;
    alert(`Deleted ${name}: ${r.deleted.events.toLocaleString()} activity records, ${r.deleted.screenshots.toLocaleString()} screenshots, ${r.deleted.alerts} alerts.` +
      (r.failed_screenshots ? `\n\n${r.failed_screenshots} screenshot file(s) could not be removed from storage and were kept. Run Delete again to retry.` : ''));
    return true;
  };

  // "Screenshot now": ask the employee's PC for a live capture and show it. Tells "the feature works" apart from
  // "they just switched apps": if the PC answers you get the picture within seconds.
  TB.screenshotNow = async (user, { onDone } = {}) => {
    const devs = (await TB.api('/api/devices')).filter((d) => d.user === user);
    if (!devs.length) { alert('No PC has reported for this employee yet.'); return; }
    const now = Date.now() / 1000, live = devs.filter((d) => now - d.last_seen < 600);
    const targets = live.length ? live : [devs[0]];
    // Agents ask the server for work every ~10 s. One that never has is too old for this feature (or off).
    const silent = targets.filter((d) => !d.last_poll || now - d.last_poll > 60);
    const ov = document.createElement('div');
    ov.className = 'modal';
    ov.innerHTML = `<div class="box"><div class="mtop"><b>Screenshot now · ${TB.esc(TB.dn(user))}</b><button class="x" aria-label="Close">×</button></div><div class="mbody"></div></div>`;
    document.body.append(ov);
    let open = true;
    const close = () => { open = false; ov.remove(); document.removeEventListener('keydown', esc); };
    const esc = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', esc);
    ov.querySelector('.x').onclick = close;
    ov.onclick = (e) => { if (e.target === ov) close(); };
    const body = ov.querySelector('.mbody');
    const askAll = () => Promise.all(targets.map(async (d) => {
      const row = document.createElement('div');
      row.className = 'mrow';
      row.innerHTML = `<div class="mut sm">${TB.esc(d.device.length > 24 ? d.device.slice(0, 24) + '…' : d.device)}${live.length ? '' : ' (not seen recently; may be off)'}</div><div class="st">Asking the PC…</div><div class="img"></div>`;
      body.append(row);
      const st = row.querySelector('.st'), img = row.querySelector('.img');
      try {
        const { id } = await TB.api('/api/screenshot-now', { method: 'POST', body: JSON.stringify({ device: d.device }) });
        for (let i = 0; open && i < 50; i++) {
          await new Promise((r) => setTimeout(r, i ? 2000 : 1000));
          if (!open) return;
          const r = await TB.api('/api/screenshot-now/' + id);
          if (r.status === 'pending') st.textContent = `Waiting for the PC to pick it up… ${Math.round(r.waited)}s (PCs check every ~10 s)`;
          else if (r.status === 'delivered') st.textContent = 'The PC is capturing…';
          else if (r.status === 'done') {
            st.innerHTML = `<b style="color:var(--c-p)">✓ Captured ${new Date().toLocaleTimeString()}</b> · the PC is alive and the screenshot feature works.`;
            const url = await TB.blobUrl(`/api/screenshots/${r.screenshot_id}/image`);
            img.innerHTML = `<a href="${url}" target="_blank" rel="noopener"><img alt="Live screenshot" src="${url}"></a>`;
            if (onDone) onDone(r.screenshot_id);
            return;
          } else if (r.status === 'failed') { st.innerHTML = `<b style="color:var(--c-u)">✕ The PC could not take it:</b> ${TB.esc(r.error || 'unknown error')}`; return; }
          else { st.innerHTML = '<b style="color:var(--c-u)">✕ No answer in 90 seconds.</b> The PC may be off, asleep or offline, or still running an older agent that cannot take on-demand screenshots (see Agent build on Install agents).'; return; }
        }
      } catch (e) { st.innerHTML = `<b style="color:var(--c-u)">✕ ${TB.esc(e.message)}</b>`; }
    }));
    if (silent.length === targets.length) {
      body.innerHTML = `<div class="mrow"><div class="st"><b style="color:var(--c-u)">✕ This PC is not checking in for requests.</b></div>
        <p class="sm">Either its agent is older than the Screenshot now feature (see <b>Agent build</b> on Install agents: "unknown" or "update pending"), or the PC is off or asleep.</p>
        <p class="sm">To update a PC: publish a new build (<code>windows\\build.ps1</code> on the server), then on that PC in an elevated PowerShell run
        <code>Start-ScheduledTask 'Cyril Updater'</code>. It picks the new agent up within a minute or two.</p>
        <button id="tryAnyway">Try anyway</button></div>`;
      body.querySelector('#tryAnyway').onclick = () => { body.innerHTML = ''; askAll(); };
    } else askAll();
  };

  // "Group by": the reporting interval. One server-side setting shared by every page and admin; each block of
  // time is reported as the app used most in it. 0 = exact.
  TB.SLOTS = [[0, 'Exact'], [1, '1 min'], [5, '5 min'], [10, '10 min'], [15, '15 min'], [30, '30 min']];
  TB.slotNote = (n) => (n ? ` · grouped in ${n}-minute blocks (each block shows the app used most in it)` : '');
  TB.slotSelect = async (parent, onChange) => {
    const cur = (await TB.api('/api/settings')).report_interval;
    const l = document.createElement('label');
    l.className = 'mut'; l.title = 'Group activity into blocks of this length. Each block is reported as the app or site used most within it.';
    l.innerHTML = `Group by <select aria-label="Reporting interval">${TB.SLOTS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>`;
    const sel = l.querySelector('select');
    sel.value = String(cur);
    sel.onchange = async () => {
      sel.disabled = true;
      try { await TB.api('/api/settings', { method: 'PUT', body: JSON.stringify({ report_interval: +sel.value }) }); onChange(+sel.value); }
      catch (e) { TB.error(e); } finally { sel.disabled = false; }
    };
    parent.append(l);
    return cur;
  };

  // Presets + start/end date inputs (+ optional employee picker). State lives in the URL so views are shareable.
  TB.rangeBar = (el, { user = false, userAll = 'All employees', defDays = 7, slot = false, onChange }) => {
    const p = TB.params();
    const today = new Date();
    const back = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return TB.ymd(d); };
    const PRE = [['Today', () => [back(0), back(0)]], ['Yesterday', () => [back(1), back(1)]], ['Last 7 days', () => [back(6), back(0)]],
      ['Last 30 days', () => [back(29), back(0)]], ['This month', () => [TB.ymd(new Date(today.getFullYear(), today.getMonth(), 1)), back(0)]]];
    const st = { start: p.start || back(defDays - 1), end: p.end || back(0), user: p.user || '' };
    el.classList.add('bar-controls');
    el.innerHTML = `${user ? '<select id="rbUser" aria-label="Employee"></select>' : ''}
      ${PRE.map(([t], i) => `<button class="chip" data-i="${i}">${t}</button>`).join('')}
      <label>From <input type="date" id="rbS"></label><label>To <input type="date" id="rbE"></label>`;
    const sync = () => {
      $('#rbS', el).value = st.start; $('#rbE', el).value = st.end;
      PRE.forEach(([, f], i) => { const [s, e] = f(); el.querySelector(`[data-i="${i}"]`).classList.toggle('on', s === st.start && e === st.end); });
      TB.setParams(st);
    };
    const fire = () => { if (st.start > st.end) [st.start, st.end] = [st.end, st.start]; sync(); onChange({ ...st }); };
    el.querySelectorAll('.chip').forEach((b) => (b.onclick = () => { [st.start, st.end] = PRE[+b.dataset.i][1](); fire(); }));
    $('#rbS', el).onchange = (e) => { if (e.target.value) { st.start = e.target.value; fire(); } };
    $('#rbE', el).onchange = (e) => { if (e.target.value) { st.end = e.target.value; fire(); } };
    const start = () => {
      if (user) {
        TB.users().then((u) => {
          const s = $('#rbUser', el);
          s.innerHTML = (userAll === null ? '' : `<option value="">${userAll}</option>`) + u.map((x) => `<option value="${TB.esc(x.user)}">${TB.esc(TB.dn(x.user))}</option>`).join('');
          if (userAll === null && !st.user && u.length) st.user = u[0].user;
          s.value = st.user; s.onchange = () => { st.user = s.value; fire(); };
          fire();
        }).catch(TB.error);
      } else fire();
    };
    // the interval picker is part of the bar; the first load waits for the saved setting so numbers match it
    if (slot) TB.slotSelect(el, () => fire()).then(start).catch((e) => { TB.error(e); start(); }); else start();
    return { get: () => ({ ...st }), set: (o) => { Object.assign(st, o); fire(); } };
  };
  TB.rangeLabel = (s, e) => (s === e ? new Date(s + 'T00:00').toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
    : new Date(s + 'T00:00').toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' – ' + new Date(e + 'T00:00').toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }));

  /* ---------- charts ---------- */
  // Re-render a chart at its real pixel width (and again on resize) so text stays a readable size.
  const draw = (el, fn) => {
    const go = () => { el.innerHTML = fn(Math.max(280, el.clientWidth || 600)); };
    go();
    if (!el._ro && window.ResizeObserver) { let last = el.clientWidth, t; el._ro = new ResizeObserver(() => { if (el.clientWidth === last) return; last = el.clientWidth; clearTimeout(t); t = setTimeout(go, 80); }); el._ro.observe(el); }
    el._redraw = go;
  };
  const axisFmt = (s) => (s >= 3600 ? (s / 3600) + 'h' : Math.round(s / 60) + 'm');
  const niceTicks = (max) => {
    const steps = [300, 600, 900, 1800, 3600, 7200, 14400, 18000, 28800, 36000, 72000, 144000, 288000, 360000];
    const step = steps.find((s) => max / s <= 4) || steps[steps.length - 1];
    const out = []; for (let v = 0; ; v += step) { out.push(v); if (v >= max) break; } return out;
  };
  TB.empty = (el, msg) => { el.innerHTML = `<div class="empty">${msg}</div>`; };

  // Vertical stacked columns. rows: [{label, title, vals:{key:seconds}}]; keys: [[key,cssColor,name],...]
  TB.stacked = (el, rows, keys, { height = 220, every = 1 } = {}) => {
    const max = Math.max(1, ...rows.map((r) => keys.reduce((a, [k]) => a + (r.vals[k] || 0), 0)));
    if (max <= 1) return TB.empty(el, 'No activity recorded in this range.');
    draw(el, (W) => {
      const L = 38, B = 22, T = 8, pw = W - L - 4, ph = height - B - T, ticks = niceTicks(max), top = ticks[ticks.length - 1];
      const bw = pw / rows.length, w = Math.max(2, Math.min(bw - 3, 44));
      let g = ticks.map((t) => { const y = T + ph - (t / top) * ph; return `<g class="grid"><line x1="${L}" x2="${W}" y1="${y}" y2="${y}"/></g><text x="${L - 6}" y="${y + 4}" text-anchor="end">${axisFmt(t)}</text>`; }).join('');
      rows.forEach((r, i) => {
        const x = L + i * bw + (bw - w) / 2; let y = T + ph;
        for (const [k, c] of keys) { const v = r.vals[k] || 0; if (!v) continue; const h = Math.max(1, (v / top) * ph); y -= h; g += `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${c}" stroke="var(--card)" stroke-width="1" rx="1.5"/>`; }
        if (i % every === 0) g += `<text x="${x + w / 2}" y="${height - 6}" text-anchor="middle">${TB.esc(r.label)}</text>`;
        const tip = `<b>${TB.esc(r.title || r.label)}</b>` + keys.map(([k, c, n]) => (r.vals[k] ? `<br>${n}: ${TB.fmt(r.vals[k])}` : '')).join('');
        g += `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${ph}" fill="transparent" data-tip="${TB.esc(tip)}"/>`;
      });
      return `<svg viewBox="0 0 ${W} ${height}" height="${height}" role="img">${g}</svg>`;
    });
  };

  // Line of 0-100% values with gaps for null. pts: [{label,title,v}]
  TB.line = (el, pts, { height = 180, every = 1, color = 'var(--accent)' } = {}) => {
    if (!pts.some((p) => p.v != null)) return TB.empty(el, 'No data in this range.');
    draw(el, (W) => {
      const L = 38, B = 22, T = 8, pw = W - L - 12, ph = height - B - T, n = pts.length;
      const X = (i) => L + (n === 1 ? pw / 2 : (i / (n - 1)) * pw), Y = (v) => T + ph - (v / 100) * ph;
      let g = [0, 25, 50, 75, 100].map((t) => `<g class="grid"><line x1="${L}" x2="${W}" y1="${Y(t)}" y2="${Y(t)}"/></g><text x="${L - 6}" y="${Y(t) + 4}" text-anchor="end">${t}%</text>`).join('');
      let d = '', pen = false;
      pts.forEach((p, i) => { if (p.v == null) { pen = false; return; } d += `${pen ? 'L' : 'M'}${X(i)},${Y(p.v)} `; pen = true; });
      g += `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      pts.forEach((p, i) => {
        if (p.v != null && n <= 45) g += `<circle cx="${X(i)}" cy="${Y(p.v)}" r="3.5" fill="${color}" stroke="var(--card)" stroke-width="2"/>`;
        if (i % every === 0) g += `<text x="${X(i)}" y="${height - 6}" text-anchor="middle">${TB.esc(p.label)}</text>`;
        g += `<rect x="${X(i) - pw / n / 2}" y="${T}" width="${pw / n}" height="${ph}" fill="transparent" data-tip="${TB.esc(`<b>${p.title || p.label}</b><br>${p.v == null ? 'no activity' : p.v + '% productive'}`)}"/>`;
      });
      return `<svg viewBox="0 0 ${W} ${height}" height="${height}" role="img">${g}</svg>`;
    });
  };

  TB.donut = (el, parts, centerTop, centerSub) => {
    const tot = parts.reduce((a, p) => a + p.v, 0);
    if (!tot) return TB.empty(el, 'No activity recorded in this range.');
    const R = 54, C = 2 * Math.PI * R; let off = 0;
    const arcs = parts.filter((p) => p.v > 0).map((p) => { const len = (p.v / tot) * C, a = `<circle r="${R}" cx="70" cy="70" fill="none" stroke="${p.color}" stroke-width="18" stroke-dasharray="${Math.max(0, len - 2)} ${C}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)" data-tip="${TB.esc(`<b>${p.name}</b><br>${TB.fmt(p.v)} · ${Math.round((100 * p.v) / tot)}%`)}"/>`; off += len; return a; }).join('');
    el.innerHTML = `<div class="donutwrap"><svg width="140" height="140" viewBox="0 0 140 140" role="img">${arcs}<text x="70" y="68" text-anchor="middle" style="font:600 22px system-ui;fill:var(--fg)">${centerTop}</text><text x="70" y="86" text-anchor="middle" style="font:11px system-ui;fill:var(--mut)">${centerSub}</text></svg>
      <div style="flex:1;min-width:150px">${parts.map((p) => `<div class="hb" style="grid-template-columns:1fr auto auto"><span class="nm"><span class="dot" style="background:${p.color}"></span>${p.name}</span><span class="num">${TB.fmt(p.v)}</span><span class="mut num right" style="min-width:36px">${Math.round((100 * p.v) / tot)}%</span></div>`).join('')}</div></div>`;
  };

  // Ranked horizontal bars. items: [{name, seconds, label?, category?}]
  TB.hbars = (el, items, { color, sub } = {}) => {
    if (!items || !items.length) return TB.empty(el, 'No data in this range.');
    const mx = Math.max(1, ...items.map((i) => i.seconds));
    el.innerHTML = items.map((i) => { const c = color ? color(i) : i.label ? TB.labelColor(i.label) : 'var(--accent)';
      return `<div class="hb" data-tip="${TB.esc(`<b>${i.name}</b><br>${TB.fmt(i.seconds)}${i.category ? '<br>' + i.category : ''}${i.label ? ' · ' + i.label : ''}`)}"><span class="nm"><span class="dot" style="background:${c}"></span>${TB.esc(i.name)}</span><span class="tr"><i style="width:${(100 * i.seconds) / mx}%;background:${c}"></i></span><span class="mut num right">${TB.fmt(i.seconds)}</span></div>`; }).join('')
      + (sub ? `<div class="legend">${sub}</div>` : '');
  };
  TB.labelLegend = '<span><span class="dot" style="background:var(--c-p)"></span>productive</span><span><span class="dot" style="background:var(--c-u)"></span>unproductive</span><span><span class="dot" style="background:var(--c-n)"></span>neutral</span>';

  // Weekday x hour heat map of active seconds. matrix[0] = Monday.
  TB.heatmap = (el, m) => {
    const mx = Math.max(...m.flat());
    if (!mx) return TB.empty(el, 'No activity recorded in this range.');
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    let h = '<div></div>' + Array.from({ length: 24 }, (_, i) => `<div class="h">${i % 3 === 0 ? i : ''}</div>`).join('');
    m.forEach((row, d) => { h += `<div class="r">${days[d]}</div>` + row.map((v, i) => `<div class="c" style="${v ? `background:color-mix(in srgb,var(--c-a) ${Math.max(12, Math.round((100 * v) / mx))}%,transparent)` : ''}" data-tip="${TB.esc(`<b>${days[d]} ${i}:00</b><br>${TB.fmt(v)} active`)}"></div>`).join(''); });
    el.innerHTML = `<div class="heat">${h}</div>`;
  };

  TB.csv = (name, rows) => {
    const q = (v) => { v = String(v ?? ''); if (/^[=+\-@\t\r]/.test(v)) v = "'" + v; return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([rows.map((r) => r.map(q).join(',')).join('\n')], { type: 'text/csv' }));
    a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  TB.SERIES = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)', 'var(--s6)', 'var(--s7)', 'var(--s8)'];
})();
