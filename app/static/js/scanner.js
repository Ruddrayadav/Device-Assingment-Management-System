/**
 * Zero-Enter Barcode Scanner Engine
 * Automatically detects barcode bursts from USB HID scanners, wireless/Bluetooth scanners,
 * clipboard copy-pastes, wedge scanners, and debounced typing without requiring Enter or Submit buttons.
 */
class BarcodeScannerEngine {
  constructor(options = {}) {
    this.inputElement = options.inputElement || document.getElementById('scan-input');
    this.onScanComplete = options.onScanComplete || (() => {});
    this.minBarcodeLength = options.minBarcodeLength !== undefined ? options.minBarcodeLength : 2;
    this.burstTimeoutMs = options.burstTimeoutMs || 70; // Idle interval after fast typing to trigger scan
    this.manualDebounceMs = options.manualDebounceMs || 320; // Delay for manual typing
    
    this.lastCharTime = 0;
    this.charIntervals = [];
    this.timer = null;
    this.isProcessing = false;
    this.previousValueLength = 0;
    this.audioEnabled = localStorage.getItem('hhd_sound_enabled') !== '0';
    this.audioCtx = null;

    this.initAudio();
    this.bindEvents();
  }

  cleanCode(raw) {
    if (!raw) return '';
    return String(raw)
      .replace(/[\u200B-\u200D\uFEFF]/g, '')  // zero-width spaces
      .replace(/[\u00A0\s]+/g, ' ')           // non-breaking and multiple spaces to single
      .replace(/[\r\n\t]+/g, '')              // carriage returns, newlines, tabs
      .replace(/^["']|["']$/g, '')            // accidental outer quotes from copy-paste
      .trim();
  }

  initAudio() {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        this.audioCtx = new AudioContext();
      }
    } catch (e) {
      console.warn("AudioContext not supported", e);
    }
  }

  beep(freq, duration, type = 'sine', delay = 0) {
    if (!this.audioEnabled || !this.audioCtx) return;
    try {
      if (this.audioCtx.state === 'suspended') {
        this.audioCtx.resume();
      }
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      osc.type = type;
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.12, this.audioCtx.currentTime + delay);
      gain.gain.exponentialRampToValueAtTime(0.001, this.audioCtx.currentTime + delay + duration);
      osc.connect(gain);
      gain.connect(this.audioCtx.destination);
      osc.start(this.audioCtx.currentTime + delay);
      osc.stop(this.audioCtx.currentTime + delay + duration);
    } catch (e) {}
  }

  playSuccess() {
    // 2-tone crisp chime (880Hz then 1320Hz)
    this.beep(880, 0.09, 'sine', 0);
    this.beep(1320, 0.14, 'sine', 0.08);
  }

  playPending() {
    // Soft high blip
    this.beep(660, 0.07, 'sine', 0);
  }

  playError() {
    // Low harsh buzzer (220Hz then 160Hz square wave)
    this.beep(220, 0.18, 'square', 0);
    this.beep(160, 0.25, 'square', 0.15);
  }

  toggleSound() {
    this.audioEnabled = !this.audioEnabled;
    localStorage.setItem('hhd_sound_enabled', this.audioEnabled ? '1' : '0');
    return this.audioEnabled;
  }

  bindEvents() {
    if (!this.inputElement) return;

    // Maintain focus continuously
    window.addEventListener('click', (e) => {
      // Don't steal focus if clicking inside a modal or dialog
      if (e.target.closest('dialog') || e.target.closest('input:not(#scan-input)') || e.target.closest('textarea')) {
        return;
      }
      this.ensureFocus();
    });

    document.addEventListener('keydown', (e) => {
      // If modal dialog is open, do not redirect focus
      const activeDialog = document.querySelector('dialog[open]');
      if (activeDialog) return;

      // Ignore special modifier keys
      if (e.ctrlKey || e.altKey || e.metaKey) return;

      // If key is typed outside input, immediately route to scan-input
      if (document.activeElement !== this.inputElement && e.key.length === 1) {
        this.inputElement.focus();
      }
    });

    this.inputElement.addEventListener('blur', () => {
      setTimeout(() => {
        const activeDialog = document.querySelector('dialog[open]');
        if (!activeDialog && document.activeElement !== this.inputElement) {
          this.ensureFocus();
        }
      }, 50);
    });

    // Detect Enter, Tab, Escape, CRLF
    this.inputElement.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === 'Tab' || e.key === '\r' || e.key === '\n') {
        e.preventDefault();
        this.triggerScanImmediate();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        this.clear();
        if (typeof this.onEscape === 'function') {
          this.onEscape();
        }
      }
    });

    // Instant Copy-Paste detection (Cmd+V, Ctrl+V, Right-Click -> Paste, Middle-click)
    this.inputElement.addEventListener('paste', (e) => {
      clearTimeout(this.timer);
      
      const pastedText = (e.clipboardData || window.clipboardData)?.getData('text');
      if (pastedText) {
        e.preventDefault();
        const clean = this.cleanCode(pastedText);
        if (clean.length >= this.minBarcodeLength) {
          this.inputElement.value = clean;
          this.triggerScanImmediate();
          return;
        }
      }

      // Fallback if clipboardData was restricted by browser policy
      setTimeout(() => {
        if (!this.inputElement) return;
        const clean = this.cleanCode(this.inputElement.value);
        if (clean.length >= this.minBarcodeLength) {
          this.inputElement.value = clean;
          this.triggerScanImmediate();
        }
      }, 10);
    });

    // Input event: zero-enter detection for hardware bursts, chunk input, and debounced typing
    this.inputElement.addEventListener('input', (e) => {
      clearTimeout(this.timer);
      if (!this.inputElement) return;

      let rawVal = this.inputElement.value;

      // If scanner appended or embedded carriage return / newline, trigger immediately
      if (/[\r\n]/.test(rawVal)) {
        this.inputElement.value = this.cleanCode(rawVal);
        this.triggerScanImmediate();
        return;
      }

      const val = this.cleanCode(rawVal);
      if (val.length < this.minBarcodeLength) {
        this.previousValueLength = val.length;
        return;
      }

      const now = performance.now();
      const currentLength = val.length;
      const charsAdded = currentLength - this.previousValueLength;
      this.previousValueLength = currentLength;

      // Immediate trigger for paste, drop, or multi-character wedge injection
      if (e.inputType === 'insertFromPaste' || e.inputType === 'insertFromDrop' || charsAdded > 1) {
        this.inputElement.value = val;
        this.triggerScanImmediate();
        return;
      }

      // Track interval between keystrokes (ignoring idle pause between separate scans)
      if (this.lastCharTime) {
        const delta = now - this.lastCharTime;
        if (delta < 800) {
          this.charIntervals.push(delta);
          if (this.charIntervals.length > 15) this.charIntervals.shift();
        } else {
          // New scan burst started after idle pause: reset interval history
          this.charIntervals = [];
        }
      }
      this.lastCharTime = now;

      // Hardware barcode scanners (USB/Bluetooth) typically type < 75ms per character
      let isScannerBurst = false;
      if (this.charIntervals.length >= 1) {
        const recent = this.charIntervals.slice(-4);
        const avgInterval = recent.reduce((a, b) => a + b, 0) / recent.length;
        isScannerBurst = avgInterval < 75;
      }

      // Fast burst: burstTimeoutMs (70ms) - immediate response once scanner finishes transmission
      // Slower scan / manual typing: manualDebounceMs (320ms) - auto-triggers without pressing Enter
      const delay = isScannerBurst ? this.burstTimeoutMs : this.manualDebounceMs;
      this.timer = setTimeout(() => {
        this.triggerScanImmediate();
      }, delay);
    });
  }

  ensureFocus() {
    if (this.inputElement && document.activeElement !== this.inputElement) {
      this.inputElement.focus();
    }
  }

  triggerScanImmediate() {
    clearTimeout(this.timer);
    if (!this.inputElement) return;

    const code = this.cleanCode(this.inputElement.value);
    if (!code || this.isProcessing) return;

    this.inputElement.value = '';
    this.charIntervals = [];
    this.lastCharTime = 0;
    this.previousValueLength = 0;

    this.isProcessing = true;
    try {
      this.onScanComplete(code);
    } finally {
      this.isProcessing = false;
      this.ensureFocus();
    }
  }

  clear() {
    clearTimeout(this.timer);
    if (this.inputElement) {
      this.inputElement.value = '';
    }
    this.charIntervals = [];
    this.lastCharTime = 0;
    this.previousValueLength = 0;
  }
}

window.BarcodeScannerEngine = BarcodeScannerEngine;
