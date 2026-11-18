class SgDeskViewer {
  constructor(onDisconnect) {
    this.onDisconnect = onDisconnect;
    this.ws = null;
    this.canvas = document.getElementById("remote-canvas");
    this.ctx = this.canvas.getContext("2d", { alpha: false });
    this.stage = document.getElementById("canvas-stage");

    this.deskLabel = document.getElementById("viewer-desk-label");
    this.statFps = document.getElementById("stat-fps");
    this.statRtt = document.getElementById("stat-rtt");
    this.statBw = document.getElementById("stat-bw");

    this.selMonitor = document.getElementById("sel-monitor");
    this.selQuality = document.getElementById("sel-quality");
    this.selFps = document.getElementById("sel-fps");
    this.selScale = document.getElementById("sel-scale");

    this.clipboardModal = document.getElementById("clipboard-modal");
    this.clipboardText = document.getElementById("clipboard-textarea");

    this.allowInput = true;
    this.framesInSec = 0;
    this.bytesInSec = 0;
    this.lastMoveSent = 0;
    this.pendingBitmap = false;
    this.statTimer = null;
    this.pingTimer = null;

    this._bindEvents();
  }

  _bindEvents() {
    this.selMonitor.addEventListener("change", () => {
      this._send({ type: "config", monitor: parseInt(this.selMonitor.value, 10) });
    });
    this.selQuality.addEventListener("change", () => {
      this._send({ type: "config", quality: parseInt(this.selQuality.value, 10) });
    });
    this.selFps.addEventListener("change", () => {
      this._send({ type: "config", fps: parseInt(this.selFps.value, 10) });
    });
    this.selScale.addEventListener("change", () => {
      this._send({ type: "config", scale: parseFloat(this.selScale.value) });
    });

    document.getElementById("btn-disconnect").addEventListener("click", () => {
      this.disconnect();
    });

    document.getElementById("btn-fullscreen").addEventListener("click", async () => {
      const shell = document.getElementById("viewer-view");
      if (!document.fullscreenElement) {
        await shell.requestFullscreen?.();
        try {
          await navigator.keyboard?.lock?.(["AltLeft", "AltRight", "MetaLeft", "MetaRight", "Tab"]);
        } catch {
          // Keyboard Lock not supported in this browser
        }
      } else {
        try {
          navigator.keyboard?.unlock?.();
        } catch {
          // Ignore
        }
        document.exitFullscreen?.();
      }
    });

    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement) {
        try {
          navigator.keyboard?.unlock?.();
        } catch {
          // Ignore
        }
      }
    });

    const btnElevate = document.getElementById("btn-elevate");
    if (btnElevate) {
      btnElevate.addEventListener("click", () => {
        if (!this.ws) return;
        btnElevate.disabled = true;
        btnElevate.textContent = "🛡 Elevating...";
        this._send({ type: "request_elevation" });
      });
    }

    // Hotkeys dropdown
    const btnHotkeys = document.getElementById("btn-hotkeys");
    const dropdown = btnHotkeys.closest(".dropdown");
    btnHotkeys.addEventListener("click", (e) => {
      e.stopPropagation();
      dropdown.classList.toggle("open");
    });
    document.addEventListener("click", () => dropdown.classList.remove("open"));
    document.querySelectorAll("#hotkeys-menu button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const keys = btn.dataset.keys.split(",");
        this._send({ type: "hotkey", keys });
        dropdown.classList.remove("open");
        this.canvas.focus();
      });
    });

    // Clipboard modal
    document.getElementById("btn-clipboard").addEventListener("click", () => {
      this.clipboardModal.style.display = "flex";
    });
    document.getElementById("btn-close-clipboard").addEventListener("click", () => {
      this.clipboardModal.style.display = "none";
      this.canvas.focus();
    });
    document.getElementById("btn-pull-clipboard").addEventListener("click", () => {
      this._send({ type: "clipboard_get" });
    });
    document.getElementById("btn-push-clipboard").addEventListener("click", () => {
      this._send({ type: "clipboard_set", text: this.clipboardText.value });
      this.clipboardModal.style.display = "none";
      this.canvas.focus();
    });

    // Canvas Mouse Events
    this.canvas.addEventListener("contextmenu", (e) => e.preventDefault());

    this.canvas.addEventListener("mousemove", (e) => {
      if (!this.allowInput || !this.ws) return;
      const now = performance.now();
      if (now - this.lastMoveSent < 16) return;
      this.lastMoveSent = now;
      const pos = this._normPos(e);
      if (pos) this._send({ type: "mouse_move", x: pos.x, y: pos.y });
    });

    this.canvas.addEventListener("mousedown", (e) => {
      this.canvas.focus();
      if (!this.allowInput || !this.ws) return;
      e.preventDefault();
      const pos = this._normPos(e);
      if (pos) {
        this._send({
          type: "mouse_down",
          button: this._mouseBtnName(e.button),
          x: pos.x,
          y: pos.y,
        });
      }
    });

    this.canvas.addEventListener("mouseup", (e) => {
      if (!this.allowInput || !this.ws) return;
      e.preventDefault();
      const pos = this._normPos(e);
      if (pos) {
        this._send({
          type: "mouse_up",
          button: this._mouseBtnName(e.button),
          x: pos.x,
          y: pos.y,
        });
      }
    });

    this.canvas.addEventListener(
      "wheel",
      (e) => {
        if (!this.allowInput || !this.ws) return;
        e.preventDefault();
        this._send({ type: "wheel", dx: e.deltaX, dy: e.deltaY });
      },
      { passive: false }
    );

    // Keyboard Events
    this.canvas.addEventListener("keydown", (e) => {
      if (!this.allowInput || !this.ws) return;
      e.preventDefault();
      this._send({ type: "key", code: e.code, action: "down" });
    });

    this.canvas.addEventListener("keyup", (e) => {
      if (!this.allowInput || !this.ws) return;
      e.preventDefault();
      this._send({ type: "key", code: e.code, action: "up" });
    });
  }

  _mouseBtnName(btn) {
    if (btn === 2) return "right";
    if (btn === 1) return "middle";
    return "left";
  }

  _normPos(e) {
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    return { x, y };
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  connect(baseUrl, token, wsPath = "/ws/desk") {
    const u = new URL(baseUrl);
    const wsProto = u.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${u.host}${wsPath}?token=${encodeURIComponent(token)}`;

    this.ws = new WebSocket(wsUrl);
    this.ws.binaryType = "arraybuffer";

    this.ws.onopen = () => {
      this.canvas.focus();
      this.statTimer = setInterval(() => {
        this.statFps.textContent = `${this.framesInSec} FPS`;
        this.statBw.textContent = `${Math.round(this.bytesInSec / 1024)} KB/s`;
        this.framesInSec = 0;
        this.bytesInSec = 0;
      }, 1000);

      this.pingTimer = setInterval(() => {
        this._send({ type: "ping", t: performance.now() });
      }, 2000);
    };

    this.ws.onmessage = async (event) => {
      if (typeof event.data === "string") {
        const msg = JSON.parse(event.data);
        this._handleControl(msg);
        return;
      }

      // Binary JPEG frame
      const buf = event.data;
      this.bytesInSec += buf.byteLength;
      if (this.pendingBitmap) return;
      this.pendingBitmap = true;
      try {
        const blob = new Blob([buf], { type: "image/jpeg" });
        const bmp = await createImageBitmap(blob);
        if (this.canvas.width !== bmp.width || this.canvas.height !== bmp.height) {
          this.canvas.width = bmp.width;
          this.canvas.height = bmp.height;
        }
        this.ctx.drawImage(bmp, 0, 0);
        bmp.close();
        this.framesInSec += 1;
      } catch {
        // Ignore decode errors
      } finally {
        this.pendingBitmap = false;
      }
    };

    this.ws.onclose = () => {
      this._cleanup();
      this.onDisconnect?.();
    };

    this.ws.onerror = () => {
      this._cleanup();
      this.onDisconnect?.("WebSocket connection error.");
    };
  }

  _handleControl(msg) {
    if (msg.type === "hello") {
      this.allowInput = Boolean(msg.allow_input);
      this.deskLabel.textContent = `${msg.hostname} (${msg.desk_id})`;
      this.selMonitor.innerHTML = "";
      (msg.monitors || []).forEach((m) => {
        const opt = document.createElement("option");
        opt.value = String(m.index);
        opt.textContent = m.name;
        if (m.index === msg.monitor) opt.selected = true;
        this.selMonitor.appendChild(opt);
      });
      this.selQuality.value = String(msg.quality || 75);
      this.selFps.value = String(msg.fps || 24);
      this.selScale.value = String(msg.scale || 1.0);
    } else if (msg.type === "frame_meta") {
      if (msg.width && msg.height) {
        this.canvas.width = msg.width;
        this.canvas.height = msg.height;
      }
    } else if (msg.type === "pong") {
      const rtt = Math.round(performance.now() - msg.t);
      this.statRtt.textContent = `${rtt} ms`;
    } else if (msg.type === "input_permission") {
      this.allowInput = Boolean(msg.allow_input);
    } else if (msg.type === "elevation_status") {
      const btnElevate = document.getElementById("btn-elevate");
      if (btnElevate) {
        btnElevate.disabled = Boolean(msg.elevated);
        btnElevate.textContent = msg.elevated ? "🛡 Admin" : "🛡 Elevate";
      }
      if (msg.message) {
        alert(msg.message);
      }
    } else if (msg.type === "clipboard") {
      this.clipboardText.value = msg.text || "";
    }
  }

  _cleanup() {
    clearInterval(this.statTimer);
    clearInterval(this.pingTimer);
    this.statTimer = null;
    this.pingTimer = null;
    this.ws = null;
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
    } else {
      this._cleanup();
      this.onDisconnect?.();
    }
  }
}
