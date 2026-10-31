(() => {
  // Elements
  const scanInput = document.getElementById('scan-input');
  const banner = document.getElementById('status-banner');
  const headline = document.getElementById('status-headline');
  const subline = document.getElementById('status-sub');
  const pendingContainer = document.getElementById('pending-badges');
  const recentFeed = document.getElementById('recent-feed');
  const btnIssue = document.getElementById('btn-mode-issue');
  const btnReturn = document.getElementById('btn-mode-return');
  const soundToggle = document.getElementById('sound-toggle');
  const damageDialog = document.getElementById('damage-dialog');
  const damageSerial = document.getElementById('damage-serial');
  const damageNotes = document.getElementById('damage-notes');
  const damageSubmitBtn = document.getElementById('damage-submit-btn');
  const damageCancelBtn = document.getElementById('damage-cancel-btn');
  const damageCatBtns = document.querySelectorAll('.damage-cat-btn');

  // Stats elements
  const statOpIssued = document.getElementById('stat-op-issued');
  const statOpReturned = document.getElementById('stat-op-returned');
  const statOpDamaged = document.getElementById('stat-op-damaged');
  const statOpRejected = document.getElementById('stat-op-rejected');
  const statFleetAvail = document.getElementById('stat-fleet-avail');
  const statFleetIssued = document.getElementById('stat-fleet-issued');
  const statFleetDamaged = document.getElementById('stat-fleet-damaged');

  let currentMode = 'Issue'; // 'Issue' or 'Return'
  let pendingState = null;   // { type: 'employee'|'device', code: string, label: string }
  let selectedDamageType = 'Blank screen';
  let bannerTimer = null;
  let boardVersion = '';

  // FIFO Scan Queue for serializing rapid consecutive scans
  const scanQueue = [];
  let isQueueProcessing = false;

  function enqueueScan(code) {
    if (!code) return;
    scanQueue.push(code);
    processScanQueue();
  }

  async function processScanQueue() {
    if (isQueueProcessing || scanQueue.length === 0) return;
    isQueueProcessing = true;
    const code = scanQueue.shift();
    try {
      await sendScanRequest(code);
    } finally {
      isQueueProcessing = false;
      if (scanQueue.length > 0) {
        processScanQueue();
      }
    }
  }

  // Initialize Scanner Engine
  const scanner = new BarcodeScannerEngine({
    inputElement: scanInput,
    minBarcodeLength: 4,
    burstTimeoutMs: 65,
    onScanComplete: (code) => enqueueScan(code)
  });

  scanner.onEscape = () => resetPending();

  // Mode Switching
  function setMode(mode) {
    currentMode = mode;
    resetPending();

    if (mode === 'Issue') {
      btnIssue.className = 'mode-btn active-issue';
      btnReturn.className = 'mode-btn';
      scanInput.placeholder = 'Issue Mode: Scan Worker ID or Device barcode';
      setStatus('idle', 'Issue Mode Active', 'Scan Worker ID barcode or Device serial (links automatically)');
    } else {
      btnIssue.className = 'mode-btn';
      btnReturn.className = 'mode-btn active-return';
      scanInput.placeholder = 'Return Mode: Scan ONLY Device barcode';
      setStatus('idle', 'Return Mode Active', 'Scan device barcode alone. Assigned worker is detected automatically.');
    }
    scanner.ensureFocus();
  }

  btnIssue.addEventListener('click', () => setMode('Issue'));
  btnReturn.addEventListener('click', () => setMode('Return'));

  // Hotkeys
  window.addEventListener('keydown', (e) => {
    if (e.key === 'F2') {
      e.preventDefault();
      setMode('Issue');
    } else if (e.key === 'F4') {
      e.preventDefault();
      setMode('Return');
    }
  });

  // Sound Toggle
  if (soundToggle) {
    const updateSoundLabel = () => {
      soundToggle.textContent = scanner.audioEnabled ? '🔊 Sound On' : '🔇 Sound Off';
    };
    updateSoundLabel();
    soundToggle.addEventListener('click', () => {
      scanner.toggleSound();
      updateSoundLabel();
      scanner.ensureFocus();
    });
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Visual Status Rendering
  function setStatus(type, title, message, pending = null) {
    clearTimeout(bannerTimer);
    banner.className = `status-banner ${type}`;
    headline.textContent = title;
    subline.textContent = message || '';

    // Render pending badges
    if (pending) {
      pendingState = pending;
      pendingContainer.style.display = 'flex';
      pendingContainer.innerHTML = `
        <span class="pending-badge">⏳ Pending ${escapeHtml(pending.type ? pending.type.toUpperCase() : '')}: ${escapeHtml(pending.label || pending.code)}</span>
        <button class="btn btn-sm btn-secondary" id="btn-cancel-pending" type="button">Cancel (Esc)</button>
      `;
      document.getElementById('btn-cancel-pending')?.addEventListener('click', () => resetPending());
    } else {
      pendingState = null;
      pendingContainer.style.display = 'none';
      pendingContainer.innerHTML = '';
    }

    // Auto-revert success and error messages to ready prompt after a delay
    if (type === 'success') {
      bannerTimer = setTimeout(() => {
        if (!pendingState) {
          setStatus('idle', `${currentMode} Mode Ready`, currentMode === 'Issue' ? 'Scan Worker ID or Device barcode' : 'Scan Device barcode to return');
        }
      }, 7000);
    } else if (type === 'error') {
      bannerTimer = setTimeout(() => {
        if (!pendingState) {
          setStatus('idle', `${currentMode} Mode Ready`, currentMode === 'Issue' ? 'Scan Worker ID or Device barcode' : 'Scan Device barcode to return');
        }
      }, 9000);
    }
  }

  function resetPending() {
    scanQueue.length = 0; // Clear the FIFO queue
    isQueueProcessing = false;
    pendingState = null;
    pendingContainer.style.display = 'none';
    pendingContainer.innerHTML = '';
    setStatus('idle', `${currentMode} Mode Ready`, currentMode === 'Issue' ? 'Scan Worker ID or Device barcode' : 'Scan Device barcode to return');
    scanner.clear();
    scanner.ensureFocus();
  }

  // Send Barcode Scan Request to API
  async function sendScanRequest(code) {
    if (!code) return;

    try {
      const payload = {
        code: code,
        mode: currentMode,
        pending: pendingState ? { type: pendingState.type, code: pendingState.code } : null,
        idempotency_key: 'scan_' + Date.now() + '_' + Math.random().toString(36).substring(2, 7)
      };

      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (res.status === 401) {
        window.location.href = '/login?next=/';
        return;
      }

      const data = await res.json();

      if (data.ok) {
        if (data.state === 'done') {
          // Success!
          scanner.playSuccess();
          setStatus('success', `✓ ${data.title}`, data.message);
          
          if (data.can_flag && data.device?.serial) {
            subline.innerHTML = `${escapeHtml(data.message)} &nbsp; <button class="btn btn-sm btn-danger" id="quick-damage-btn" type="button">Report Damage</button>`;
            document.getElementById('quick-damage-btn')?.addEventListener('click', () => openDamageModal(data.device.serial));
          }

        } else if (data.state === 'pending') {
          // Pending complementary scan
          scanner.playPending();
          setStatus('pending', `⏳ ${data.title}`, data.message, data.pending);
        }
      } else {
        // Error or Unknown
        scanner.playError();
        let unknownAction = '';
        if (data.state === 'unknown' && data.can_add) {
          unknownAction = ` &nbsp; <button class="btn btn-sm btn-primary" id="btn-quick-add-dev" type="button">Add as Device</button>`;
        }
        setStatus('error', `✗ ${data.title}`, data.message, pendingState);
        if (unknownAction) {
          subline.innerHTML = `${escapeHtml(data.message)} ${unknownAction}`;
          document.getElementById('btn-quick-add-dev')?.addEventListener('click', () => quickAddDevice(data.code));
        }
      }

      // Update Live UI from response
      if (data.board) {
        updateBoard(data.board);
      }
      if (data.operator_stats) {
        updateOperatorStats(data.operator_stats);
      }

    } catch (err) {
      console.error("Scan API Error:", err);
      scanner.playError();
      setStatus('error', 'Network or Server Error', 'Could not reach server. Verify network connection and scan again.', pendingState);
    } finally {
      scanner.ensureFocus();
    }
  }

  // Quick Add Device (for Admins)
  async function quickAddDevice(serial) {
    try {
      const res = await fetch('/api/device/quick-add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ serial: serial })
      });
      const data = await res.json();
      if (data.ok) {
        scanner.playSuccess();
        setStatus('success', 'Device Added to Fleet', data.message);
        if (data.board) updateBoard(data.board);
      } else {
        scanner.playError();
        setStatus('error', 'Add Failed', data.message);
      }
    } catch (e) {
      scanner.playError();
      setStatus('error', 'Error', 'Failed to add device.');
    }
  }

  // Damage Modal Handling
  function openDamageModal(serial) {
    if (!damageDialog) return;
    damageSerial.textContent = serial;
    damageDialog.dataset.serial = serial;
    damageNotes.value = '';
    selectedDamageType = 'Blank screen';
    damageCatBtns.forEach(btn => {
      btn.classList.toggle('selected', btn.dataset.cat === selectedDamageType);
    });
    damageDialog.showModal();
  }

  damageCatBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      damageCatBtns.forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      selectedDamageType = btn.dataset.cat;
    });
  });

  damageCancelBtn?.addEventListener('click', () => {
    damageDialog.close();
    scanner.ensureFocus();
  });

  damageSubmitBtn?.addEventListener('click', async () => {
    const serial = damageDialog.dataset.serial;
    const notes = damageNotes.value.trim();
    damageSubmitBtn.disabled = true;

    try {
      const res = await fetch('/api/damage/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          serial: serial,
          fault_type: selectedDamageType,
          details: notes
        })
      });
      const data = await res.json();
      damageDialog.close();
      if (data.ok) {
        scanner.playSuccess();
        setStatus('success', 'Damage Report Filed', data.message);
        if (data.board) updateBoard(data.board);
      } else {
        scanner.playError();
        setStatus('error', 'Report Failed', data.detail || 'Could not record damage.');
      }
    } catch (e) {
      damageDialog.close();
      scanner.playError();
      setStatus('error', 'Error', 'Network error filing damage report.');
    } finally {
      damageSubmitBtn.disabled = false;
      scanner.ensureFocus();
    }
  });

  // Live Stats & Feed Rendering
  function updateOperatorStats(stats) {
    if (statOpIssued && stats.issued_count !== undefined) statOpIssued.textContent = stats.issued_count;
    if (statOpReturned && stats.returned_count !== undefined) statOpReturned.textContent = stats.returned_count;
    if (statOpDamaged && stats.damaged_count !== undefined) statOpDamaged.textContent = stats.damaged_count;
    if (statOpRejected && stats.rejected_count !== undefined) {
      statOpRejected.textContent = stats.rejected_count;
      statOpRejected.classList.toggle('alert', stats.rejected_count > 0);
    }
  }

  function updateBoard(board) {
    boardVersion = board.version;
    if (statFleetAvail) statFleetAvail.textContent = board.counts.available;
    if (statFleetIssued) statFleetIssued.textContent = board.counts.allotted;
    if (statFleetDamaged) statFleetDamaged.textContent = board.counts.unavailable;

    // Render Recent Activity Feed
    if (recentFeed && board.recent) {
      if (board.recent.length === 0) {
        recentFeed.innerHTML = '<div class="text-muted" style="padding: 1rem 0;">No transactions recorded today.</div>';
      } else {
        recentFeed.innerHTML = board.recent.map(t => {
          const isIssue = t.type === 'Issue';
          const badgeClass = isIssue ? 'badge-issued' : 'badge-available';
          const condAlert = (t.cond && t.cond !== 'Working') ? ` <span class="badge badge-damaged">${escapeHtml(t.cond)}</span>` : '';
          return `
            <tr>
              <td class="mono">${escapeHtml(t.time)}</td>
              <td><span class="badge ${badgeClass}">${escapeHtml(t.type)}</span>${condAlert}</td>
              <td class="mono"><b>${escapeHtml(t.serial)}</b></td>
              <td>${escapeHtml(t.name)} <span class="text-muted">(${escapeHtml(t.dept)})</span></td>
              <td>${escapeHtml(t.by)}</td>
            </tr>
          `;
        }).join('');
      }
    }
  }

  // Periodic Polling for Multi-Operator Sync (every 3 seconds)
  async function pollUpdates() {
    if (document.hidden) return;
    try {
      const res = await fetch(`/api/board/live?v=${encodeURIComponent(boardVersion)}`);
      if (res.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await res.json();
      if (data.changed && data.board) {
        updateBoard(data.board);
      }
    } catch (e) {
      // Ignore background network hiccup
    }
  }

  setInterval(pollUpdates, 3000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollUpdates();
  });

  // Initial focus
  scanner.ensureFocus();
})();

