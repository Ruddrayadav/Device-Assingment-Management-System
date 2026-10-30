(() => {
  const boot = JSON.parse(document.getElementById('boot').textContent);
  const RO = boot.readonly, CSRF = boot.csrf;
  let board = boot.board, pending = null, offset = 0, filter = '', idleTimer, pendTimer, muted = false, flashSerial = null;
  try { muted = localStorage.getItem('hhd_mute') === '1'; } catch (e) {}
  const $ = s => document.querySelector(s);
  const scan = $('#scan'), desk = $('#desk'), dlg = $('#dlg');
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const parse = s => new Date(String(s).replace(' ', 'T'));
  const nowMs = () => Date.now() + offset;
  const pad = n => String(n).padStart(2, '0');
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const hm = d => pad(d.getHours()) + ':' + pad(d.getMinutes());
  function when(s) {
    if (!s) return 'never';
    const d = parse(s), t = new Date(nowMs());
    if (d.toDateString() === t.toDateString()) return hm(d);
    return d.getDate() + ' ' + MON[d.getMonth()] + ' ' + hm(d);
  }
  function ago(s) {
    if (!s) return '';
    const m = Math.max(0, Math.floor((nowMs() - parse(s)) / 60000));
    if (m < 1) return 'just now';
    if (m < 60) return m + ' min';
    if (m < 1440) return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    return Math.floor(m / 1440) + ' day' + (m >= 2880 ? 's' : '');
  }

  /* ---------- sound ---------- */
  let ac;
  function beep(f, d, type, at) {
    if (muted) return;
    try {
      ac = ac || new (window.AudioContext || window.webkitAudioContext)();
      const o = ac.createOscillator(), g = ac.createGain();
      o.type = type || 'sine'; o.frequency.value = f; g.gain.value = .09;
      o.connect(g); g.connect(ac.destination);
      o.start(ac.currentTime + (at || 0)); o.stop(ac.currentTime + (at || 0) + d);
    } catch (e) {}
  }
  const sound = {
    ok: () => { beep(880, .08); beep(1320, .12, 'sine', .09); },
    pend: () => beep(660, .07),
    err: () => { beep(200, .22, 'square'); beep(160, .3, 'square', .26); }
  };

  /* ---------- rendering ---------- */
  const hit = (i, q) => !q || (i.serial + ' ' + (i.ename || '') + ' ' + (i.eid || '') + ' ' + (i.dept || '')).toLowerCase().includes(q);
  function tile(i) {
    const cls = i.status === 'Issued' ? 'stop' : i.status === 'Available' ? '' : 'hold';
    const body = i.status === 'Issued'
      ? `<span class="w">${esc(i.ename || i.eid)}</span><span class="d">${esc(i.dept || '')} · ${esc(ago(i.last_ho))}</span>`
      : i.status === 'Available' ? '' : `<span class="w">${esc(i.status)}</span>`;
    return `<button class="tile ${cls}${i.serial === flashSerial ? ' hit' : ''}" data-s="${esc(i.serial)}" data-st="${esc(i.status)}">
      <span class="s">${esc(i.serial.slice(-6))}</span><span class="f">${esc(i.serial)}</span>${body}</button>`;
  }
  const tiles = (arr, emptyMsg) => {
    const q = filter.toLowerCase().trim(), a = arr.filter(i => hit(i, q));
    return a.length ? a.map(tile).join('') : `<div class="empty">${q && arr.length ? 'No match for this filter.' : emptyMsg}</div>`;
  };
  function render() {
    const b = board, c = b.counts, q = filter.toLowerCase().trim();
    $('#k-av').textContent = c.available; $('#k-al').textContent = c.allotted;
    $('#k-nt').textContent = c.not_today; $('#k-is').textContent = c.issues; $('#k-td').textContent = c.out_today + ' / ' + c.back_today;
    $('#n-av').textContent = c.available + ' of ' + c.total; $('#n-al').textContent = c.allotted + ' of ' + c.total;
    $('#av').innerHTML = tiles(b.available, 'No device is free right now.');
    $('#al').innerHTML = tiles(b.allotted, 'No device is allotted.');
    const un = b.unavailable.filter(i => hit(i, q));
    $('#unsec').hidden = !b.unavailable.length;
    $('#un').innerHTML = un.map(tile).join('');
    const nt = b.not_today.filter(i => hit(i, q));
    const d = parse(b.today);
    $('#nt-date').textContent = d.getDate() + ' ' + MON[d.getMonth()];
    $('#nt-sum').textContent = c.not_today ? `${c.not_today_held} still with staff from earlier · ${c.not_today - c.not_today_held} idle in stock` : '';
    $('#nt').innerHTML = nt.length ? nt.map(i => i.status === 'Issued'
      ? `<div class="r"><span class="dot stop"></span><span><b>${esc(i.serial.slice(-6))}</b> <small>${esc(i.serial)}</small><br>With ${esc(i.ename || i.eid)} · ${esc(i.dept || '')}</span><span class="late">since ${esc(when(i.last_ho))}</span></div>`
      : `<div class="r"><span class="dot go"></span><span><b>${esc(i.serial.slice(-6))}</b> <small>${esc(i.serial)}</small><br>In stock</span><small>last out ${esc(when(i.last_ho))}</small></div>`).join('')
      : `<div class="empty">${q ? 'No match for this filter.' : 'Every device has been handed over today.'}</div>`;
    $('#feed').innerHTML = b.recent.length ? b.recent.map(r =>
      `<div class="r"><span class="t">${esc(when(r.time))}</span><span><span class="${r.type === 'Handover' ? 'ho' : 'to'}">${r.type === 'Handover' ? 'Out' : 'Back'}</span> ${esc(r.serial.slice(-6))} · ${esc(r.name)}${r.cond !== 'Working' ? ' · <b>' + esc(r.cond) + '</b>' : ''}</span></div>`).join('')
      : '<div class="empty">No activity yet.</div>';
  }

  /* ---------- scan desk state ---------- */
  function setDesk(cls, big, sub, extra) {
    desk.className = 'desk ' + cls;
    $('#big').textContent = big; $('#sub').textContent = sub || ''; $('#acts').innerHTML = extra || '';
    chips();
  }
  function chips() {
    const dv = $('#c-dev'), em = $('#c-emp');
    dv.className = 'chip'; em.className = 'chip'; dv.textContent = 'Device'; em.textContent = 'Employee ID';
    if (pending && pending.type === 'device') { dv.className = 'chip fill'; dv.textContent = 'Device ' + pending.code.slice(-6); em.className = 'chip need'; em.textContent = 'Scan employee ID'; }
    if (pending && pending.type === 'employee') { em.className = 'chip fill'; em.textContent = pending.label || ('Employee ' + pending.code); dv.className = 'chip need'; dv.textContent = 'Scan device'; }
  }
  function idle() {
    pending = null; clearTimeout(pendTimer);
    setDesk('', 'Ready to scan', 'Device alone = take it back · Device + employee ID = hand it over');
  }
  function settle(ms) { clearTimeout(idleTimer); idleTimer = setTimeout(() => { if (!pending) idle(); }, ms); }

  async function post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF }, body: JSON.stringify(body) });
    if (r.status === 401) { location.href = '/login?next=/'; throw new Error('auth'); }
    return r.json();
  }
  function apply(j, code) {
    clearTimeout(idleTimer); clearTimeout(pendTimer);
    pending = j.pending || null;
    if (pending && j.state === 'pending') pending.label = j.title;
    if (j.board) { board = j.board; flashSerial = j.device || null; render(); flashSerial = null; }
    if (j.state === 'done') {
      sound.ok();
      const flag = j.can_flag ? `<button class="secondary" data-flag="${esc(j.device)}">Report a problem</button>` : '';
      setDesk('ok', j.title + ' · ' + (j.device || '').slice(-6), j.message, flag);
      settle(6000);
    } else if (j.state === 'pending') {
      sound.pend();
      setDesk('pend', j.title, j.message);
      pendTimer = setTimeout(idle, 30000);
    } else {
      sound.err();
      let extra = '';
      if (j.state === 'unknown') {
        extra = (j.can_add ? `<button class="secondary" data-adddev="${esc(j.code)}">Add as device</button>` : '') +
                (j.can_add ? `<a class="btn secondary" href="/employees?add=${encodeURIComponent(j.code)}">Add as employee</a>` : '');
      }
      setDesk('err', j.title || 'Not recorded', j.message, extra);
      if (!pending) settle(9000);
    }
  }
  async function submit(code) {
    code = (code || '').trim();
    if (!code || RO) return;
    try { apply(await post('/api/scan', { code, pending: pending && { type: pending.type, code: pending.code } }), code); }
    catch (e) { if (e.message !== 'auth') { sound.err(); setDesk('err', 'Server not reachable', 'Nothing was saved. Check the network and scan again.'); } }
  }

  /* ---------- modal (take back with condition / report a problem) ---------- */
  function openReturn(serial) {
    const i = board.allotted.find(x => x.serial === serial); if (!i) return;
    dlg.innerHTML = `<div class="in"><h3 class="mono">${esc(serial)}</h3>
      <div class="muted">With ${esc(i.ename || i.eid)} · ${esc(i.dept || '')} · since ${esc(when(i.last_ho))}</div>
      <button class="go big" data-cond="Working" autofocus>Take back · working</button>
      <div class="muted" style="margin-bottom:6px">Or take it back as</div>
      <div class="trio"><button data-cond="Damaged">Damaged</button><button data-cond="Missing">Missing</button><button data-cond="Under Repair">Under repair</button></div>
      <div class="field"><label>Remarks (optional)</label><input id="d-rem" maxlength="300" autocomplete="off"></div>
      <div class="foot"><button class="secondary" data-close>Cancel</button></div></div>`;
    dlg.dataset.mode = 'return'; dlg.dataset.serial = serial; dlg.showModal();
  }
  function openFlag(serial) {
    dlg.innerHTML = `<div class="in"><h3>Report a problem</h3><div class="muted mono">${esc(serial)}</div>
      <div class="trio" style="margin-top:14px"><button data-cond="Damaged">Damaged</button><button data-cond="Missing">Missing</button><button data-cond="Under Repair">Under repair</button></div>
      <button class="secondary" data-cond="Accessory Missing" style="width:100%">Accessory / cover missing</button>
      <div class="field" style="margin-top:12px"><label>Details</label><input id="d-rem" maxlength="300" autocomplete="off"></div>
      <div class="foot"><button class="secondary" data-close>Cancel</button></div></div>`;
    dlg.dataset.mode = 'flag'; dlg.dataset.serial = serial; dlg.showModal();
  }
  function closeDlg() { if (dlg.open) dlg.close(); focusScan(); }
  const focusScan = () => { if (scan && !dlg.open) scan.focus(); };

  if (!RO) {
    scan.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        const v = scan.value; scan.value = ''; filter = ''; render(); submit(v);
      } else if (e.key === 'Escape') { scan.value = ''; filter = ''; render(); idle(); }
    });
    scan.addEventListener('input', () => { filter = scan.value; requestAnimationFrame(render); });
    scan.addEventListener('blur', () => setTimeout(() => { if (!dlg.open && (!document.activeElement || document.activeElement === document.body)) scan.focus(); }, 60));
    // A scanner types into whatever has focus; if nothing useful is focused, catch the keystrokes.
    document.addEventListener('keydown', e => {
      if (dlg.open || e.ctrlKey || e.altKey || e.metaKey || e.key.length !== 1) return;
      const t = e.target;
      if (t !== scan && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (t !== scan) scan.focus();
    }, true);
    dlg.addEventListener('close', () => setTimeout(focusScan, 30));
    dlg.addEventListener('click', async e => {
      const b = e.target.closest('button'); if (!b) { if (e.target === dlg) closeDlg(); return; }
      if (b.hasAttribute('data-close')) return closeDlg();
      const cond = b.dataset.cond; if (!cond) return;
      const rem = $('#d-rem').value, serial = dlg.dataset.serial; b.disabled = true;
      try {
        const j = dlg.dataset.mode === 'return' ? await post('/api/return', { serial, condition: cond, remarks: rem })
                                                  : await post('/api/flag', { serial, issue_type: cond, details: rem });
        dlg.close(); apply(j);
      } catch (er) { b.disabled = false; }
    });
    document.addEventListener('click', async e => {
      const t = e.target.closest('.tile'), f = e.target.closest('[data-flag]'), a = e.target.closest('[data-adddev]');
      if (t) { t.dataset.st === 'Issued' ? openReturn(t.dataset.s) : submit(t.dataset.s); focusScan(); }
      else if (f) openFlag(f.dataset.flag);
      else if (a) { try { const j = await post('/api/device/add', { serial: a.dataset.adddev }); apply(j); if (j.ok) submit(a.dataset.adddev); } catch (er) {} }
    });
  }
  const mb = $('#mute');
  if (mb) { mb.textContent = muted ? 'Sound off' : 'Sound on'; mb.onclick = () => { muted = !muted; mb.textContent = muted ? 'Sound off' : 'Sound on'; try { localStorage.setItem('hhd_mute', muted ? '1' : '0'); } catch (e) {} focusScan(); }; }
  if (RO) document.addEventListener('click', e => { const t = e.target.closest('.tile'); if (t) e.preventDefault(); });

  /* ---------- live updates for everyone on the LAN ---------- */
  const live = $('#live');
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch('/api/board?v=' + encodeURIComponent(board.version), { cache: 'no-store' });
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json();
      if (j.changed) { board = j.board; offset = parse(board.server_now) - Date.now(); render(); }
      live.innerHTML = '<span class="dot go"></span>Live · ' + hm(new Date(nowMs()));
    } catch (e) { live.innerHTML = '<span class="dot stop"></span>Offline · retrying'; }
  }
  offset = parse(board.server_now) - Date.now();
  render(); if (!RO) { idle(); scan.focus(); }
  setInterval(poll, 3000); setInterval(render, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
