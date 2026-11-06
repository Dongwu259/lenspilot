const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const { EventEmitter } = require('events');
const { randomUUID } = require('crypto');

class PythonService extends EventEmitter {
  constructor() {
    super();
    this.process = null;
    this.pending = new Map();       // id → { resolve, reject, timer }
    this.buffer = '';
    this._running = false;
    this._restartDelay = 3000;
    this._cmdTimeout = 60000;       // 60s default timeout (copy can be longer)
    this._restartAttempts = 0;      // P1-34: consecutive crash-restart counter
    this._maxRestarts = 5;          // P1-34: give up after this many consecutive crashes
    this._stderrTail = '';          // rolling stderr buffer for startup diagnostics
  }

  /**
   * Start the Python backend process.
   */
  async start() {
    let cmd, args, cwd;

    const pbDir = path.join(process.resourcesPath || '', 'python_backend');
    // PyInstaller --onedir outputs a `service/` folder containing service.exe
    // (win) / service (mac) plus all deps. --onefile outputs a single exe/bin.
    const onedirWin = path.join(pbDir, 'service', 'service.exe');
    const onedirMac = path.join(pbDir, 'service', 'service');
    const onefileWin = path.join(pbDir, 'service.exe');
    const onefileMac = path.join(pbDir, 'service');

    if (fs.existsSync(onedirWin)) {
      // Windows packaged (onedir)
      cmd = onedirWin;
      args = [];
      cwd = path.join(pbDir, 'service');
    } else if (fs.existsSync(onedirMac)) {
      // macOS packaged (onedir)
      cmd = onedirMac;
      args = [];
      cwd = path.join(pbDir, 'service');
    } else if (fs.existsSync(onefileWin)) {
      // Windows packaged (onefile, legacy)
      cmd = onefileWin;
      args = [];
      cwd = pbDir;
    } else if (fs.existsSync(onefileMac)) {
      // macOS packaged (onefile, legacy)
      cmd = onefileMac;
      args = [];
      cwd = pbDir;
    } else {
      // Development mode: on Windows prefer python.org CPython 3.13 (has torch).
      // gphoto2 runs in a separate mingw sidecar spawned by GPhoto2Backend, so the
      // main python must be CPython -- NOT mingw python (no torch) and NOT the
      // WindowsApps `python` stub (which just opens the Store).
      const backendDir = path.join(__dirname, 'python_backend');
      args = [path.join(backendDir, 'service.py')];
      cwd = backendDir;
      if (process.platform === 'win32') {
        const home = process.env.USERPROFILE || '';
        const candidates = [];
        if (process.env.LENSPILOT_DEV_PYTHON) candidates.push(process.env.LENSPILOT_DEV_PYTHON);
        candidates.push(path.join(home, 'AppData', 'Local', 'Programs', 'Python', 'Python313', 'python.exe'));
        candidates.push(path.join(home, 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'));
        candidates.push('C:\\Python313\\python.exe', 'C:\\Python312\\python.exe');
        cmd = candidates.find((p) => p && fs.existsSync(p)) || 'python';
      } else {
        cmd = 'python3';
      }
    }

    console.log(`Starting Python backend: ${cmd} ${args.join(' ')}`);

    // P0-1: pass the per-user data directory so Python writes app_config.json
    // to a writable location (not the read-only packaged resources dir).
    let userDataDir = '';
    try {
      const { app } = require('electron');
      userDataDir = app.getPath('userData');
    } catch (_) { /* dev mode without electron app context — Python falls back */ }

    // Whitelist-safe env: only pass essential vars to Python child process
    const safeEnv = {};
    const allowedVars = new Set([
      'PATH', 'HOME', 'USERPROFILE', 'TEMP', 'TMP', 'TMPDIR',
      'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'windir',
      'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
      'LANG', 'LC_ALL', 'LC_CTYPE', 'PYTHONPATH',
      'CUDA_PATH', 'CUDA_VISIBLE_DEVICES',
    ]);
    for (const key of allowedVars) {
      if (process.env[key] !== undefined) safeEnv[key] = process.env[key];
    }
    safeEnv.PYTHONUNBUFFERED = '1';
    safeEnv.PYTHONIOENCODING = 'utf-8';
    safeEnv.APS_USER_DATA = userDataDir || '';

    this.process = spawn(cmd, args, {
      cwd: cwd,
      env: safeEnv,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    this._running = true;

    this.process.stdout.on('data', (chunk) => {
      this._handleStdout(chunk.toString());
    });

    this.process.stderr.on('data', (chunk) => {
      const text = chunk.toString().trim();
      // Keep a rolling tail so a failed startup can report the real cause
      this._stderrTail = (this._stderrTail + chunk.toString()).slice(-4000);
      if (text) {
        console.error('[python stderr]', text);
        // Some log messages come through stderr
        this.emit('event', 'log', { message: text, level: 'stderr' });
      }
    });

    this.process.on('close', (code) => {
      console.log(`Python process exited with code ${code}`);
      this._running = false;
      this.emit('status', { running: false, exitCode: code });

      // Reject all pending commands
      for (const [id, { reject }] of this.pending) {
        reject(new Error(`Python process exited (code ${code})`));
      }
      this.pending.clear();

      // Auto-restart after crash with exponential backoff (P1-34)
      if (code !== 0 && this._autoRestart !== false) {
        this._restartAttempts++;
        if (this._restartAttempts > this._maxRestarts) {
          console.error(`[python-service] Giving up after ${this._maxRestarts} consecutive crashes`);
          this.emit('status', { running: false, failed: true, reason: 'too_many_crashes' });
          return;
        }
        const delay = Math.min(60000, 2000 * Math.pow(2, this._restartAttempts - 1));
        console.log(`[python-service] Auto-restarting in ${delay}ms (attempt ${this._restartAttempts}/${this._maxRestarts})...`);
        setTimeout(() => this.start(), delay);
      }
    });

    this.process.on('error', (err) => {
      console.error('Failed to start Python process:', err.message);
      this._running = false;
      this.emit('status', { running: false, error: err.message });
      // Fail the startup promise fast with the real cause (e.g. ENOENT) instead
      // of making the user wait for the 60s timeout.
      if (this._startupReject) {
        this._startupReject(new Error(`Failed to start Python process: ${err.message}`));
        this._startupReject = null;
      }
    });

    // Wait for ready signal
    return new Promise((resolve, reject) => {
      this._startupResolve = resolve;
      this._startupReject = reject;
      let settled = false;
      const settle = (fn, val) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        this.removeListener('status', onStatus);
        this._startupResolve = this._startupReject = null;
        fn(val);
      };
      const timeout = setTimeout(() => {
        // Cold start (first torch import, Windows Defender scanning freshly
        // staged runtime files) can exceed 30s — allow 60s. On failure report
        // exit code + stderr tail so the cause is diagnosable, and kill the
        // child so no orphan service.py is left behind.
        const exited = this.process && this.process.exitCode !== null
          ? ` (process exited code=${this.process.exitCode})` : '';
        const tail = this._stderrTail.trim()
          ? `\n[python stderr]\n${this._stderrTail.trim().slice(-1500)}` : '';
        this._killChild();
        settle(reject, new Error(`Python backend startup timeout${exited}${tail}`));
      }, 60000);

      const onStatus = (status) => {
        if (status.ready) {
          this._restartAttempts = 0;   // P1-34: reset on successful start
          settle(resolve);
        }
      };
      this.on('status', onStatus);
    });
  }

  /**
   * Stop the Python backend process.
   */
  stop() {
    if (this.process) {
      this._running = false;
      // Send shutdown command gracefully
      try {
        this._send({ command: 'shutdown', params: {} });
      } catch (_) { /* ignore */ }

      // Force kill after 5s
      setTimeout(() => {
        if (this.process && !this.process.killed) {
          this.process.kill();
        }
      }, 5000);
    }
  }

  /**
   * Send a command to Python and return a Promise with the result.
   */
  sendCommand(command, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this._generateId();
      // load_model may auto-download model files on first run (can take minutes);
      // switch_backend may too; upload_photos is long by nature.
      // Scoring / selection / export walk whole folders — far past the 60s
      // default on a real shoot; a timed-out UI must never start a second
      // concurrent loop while the backend is still working.
      // get_thumbnails decodes RAW in batches — a 30-photo batch can exceed 60s.
      const longCmds = ['upload_photos', 'load_model', 'download_model', 'switch_backend', 'agent_run', 'coach_advise',
        'agent_download_local_model',
        'score_photos', 'score_files', 'apply_selection', 'export_photos', 'apply_watermark', 'get_thumbnails',
        'scan_retouched'];
      const timeout = longCmds.includes(command) ? 1800000 : this._cmdTimeout;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Command '${command}' timed out`));
      }, timeout);

      this.pending.set(id, { resolve, reject, timer });
      this._send({ id, command, params });
    });
  }

  isRunning() {
    return this._running && this.process && !this.process.killed && this.process.exitCode === null;
  }

  /**
   * Hard-kill the spawned backend. Used on startup timeout so a wedged
   * service.py doesn't linger as an orphan holding ports/files.
   */
  _killChild() {
    if (this.process && this.process.exitCode === null) {
      try { this.process.kill(); } catch (_) { /* already gone */ }
    }
  }

  // ── Private ──

  _send(msg) {
    if (!this.process || !this.process.stdin.writable) {
      throw new Error('Python service is not ready — stdin is not writable');
    }
    this.process.stdin.write(JSON.stringify(msg) + '\n');
  }

  _handleStdout(text) {
    this.buffer += text;
    const lines = this.buffer.split('\n');
    // Keep the last incomplete line in buffer
    this.buffer = lines.pop() || '';

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      try {
        const msg = JSON.parse(trimmed);
        if (msg.id && this.pending.has(msg.id)) {
          // Response to a pending command
          const { resolve, reject, timer } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          clearTimeout(timer);

          if (msg.error) {
            reject(new Error(msg.error));
          } else {
            resolve(msg.result);
          }
        } else if (msg.event) {
          // Unsolicited event from Python
          this.emit('event', msg.event, msg.data || {});
        } else if (msg.ready) {
          // Initial ready signal
          this.emit('status', { ready: true, running: true });
        } else if (msg.status) {
          // Status update
          this.emit('status', msg.status);
        }
      } catch (_) {
        // Not JSON — log message
        console.log('[python]', trimmed);
      }
    }
  }

  _generateId() {
    // Simple unique ID without uuid dependency
    return Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
  }
}

module.exports = { PythonService };
