/**
 * Zero-Enter Barcode Scanner Engine
 * Automatically detects barcode bursts from USB HID scanners without requiring Enter or Submit buttons.
 */
class BarcodeScannerEngine {
  constructor(options = {}) {
    this.inputElement = options.inputElement || document.getElementById('scan-input');
    this.onScanComplete = options.onScanComplete || (() => {});
    this.minBarcodeLength = options.minBarcodeLength || 4;
    this.burstTimeoutMs = options.burstTimeoutMs || 65; // Idle interval after fast typing to trigger scan
    this.manualDebounceMs = options.manualDebounceMs || 350; // Delay for manual typing
    
    this.lastCharTime = 0;
    this.charIntervals = [];
    this.timer = null;
    this.isProcessing = false;
    this.audioEnabled = localStorage.getItem('hhd_sound_enabled') !== '0';
    this.audioCtx = null;

    this.initAudio();
    this.bindEvents();
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

    // Detect barcode input bursts
    this.inputElement.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === 'Tab') {
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

    this.inputElement.addEventListener('input', (e) => {
      const now = performance.now();
      const interval = this.lastCharTime ? now - this.lastCharTime : 100;
      this.lastCharTime = now;
      this.charIntervals.push(interval);
      if (this.charIntervals.length > 20) this.charIntervals.shift();

      clearTimeout(this.timer);

      const val = this.inputElement.value.trim();
      if (val.length < this.minBarcodeLength) {
        return;
      }

      // Check average interval of recent characters (USB HID scanners type with sub-45ms intervals)
      const recentIntervals = this.charIntervals.slice(-4);
      const avgInterval = recentIntervals.reduce((a, b) => a + b, 0) / recentIntervals.length;
      const isScannerBurst = avgInterval < 45;

      if (isScannerBurst) {
        // Automatic completion for hardware scanner bursts without requiring Enter/Tab suffix
        this.timer = setTimeout(() => {
          this.triggerScanImmediate();
        }, this.burstTimeoutMs);
      } else {
        // Manual keyboard typing: do NOT prematurely auto-submit on a timer that splits long serials.
        // Manual entry is submitted when the operator presses Enter or Tab.
      }
    });
  }

  ensureFocus() {
    if (this.inputElement && document.activeElement !== this.inputElement) {
      this.inputElement.focus();
    }
  }

  triggerScanImmediate() {
    clearTimeout(this.timer);
    const code = this.inputElement.value.trim();
    if (!code || this.isProcessing) return;

    this.inputElement.value = '';
    this.charIntervals = [];
    this.lastCharTime = 0;

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
  }
}

window.BarcodeScannerEngine = BarcodeScannerEngine;

