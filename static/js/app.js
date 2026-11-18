(() => {
  const dashboardView = document.getElementById("dashboard-view");
  const topbar = document.getElementById("topbar");
  const viewerView = document.getElementById("viewer-view");
  const hostPanel = document.getElementById("host-panel");
  const modeBadge = document.getElementById("mode-badge");
  const sysStats = document.getElementById("sys-stats");

  const connDeskIdInput = document.getElementById("conn-desk-id");
  const connUrlInput = document.getElementById("conn-url");
  const connSecretInput = document.getElementById("conn-secret");
  const connNameInput = document.getElementById("conn-name");
  const connectStatus = document.getElementById("connect-status");
  const btnConnect = document.getElementById("btn-connect");

  let adminToken = "";
  let hostState = null;
  let pinVisible = false;
  let hostWs = null;

  const viewer = new SgDeskViewer((errMsg) => {
    viewerView.style.display = "none";
    topbar.style.display = "flex";
    dashboardView.style.display = "grid";
    if (errMsg) {
      showConnectStatus(errMsg, "error");
    } else {
      connectStatus.style.display = "none";
    }
  });

  function showConnectStatus(text, kind = "") {
    connectStatus.textContent = text;
    connectStatus.className = `status-banner ${kind}`;
    connectStatus.style.display = "block";
  }

  async function hostFetch(url, options = {}) {
    const headers = Object.assign({}, options.headers || {}, {
      "Content-Type": "application/json",
    });
    if (adminToken) headers["x-sgdesk-admin"] = adminToken;
    const res = await fetch(url, { ...options, headers });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Request failed (${res.status})`);
    }
    return res.json();
  }

  function renderHostState(state) {
    hostState = state;
    if (state.admin_token) adminToken = state.admin_token;

    document.getElementById("host-desk-id").textContent = state.desk_id;
    document.getElementById("host-hostname").textContent = state.hostname;
    document.getElementById("host-pin").textContent = pinVisible
      ? state.session_pin
      : "••••••";

    sysStats.style.display = "inline-block";
    sysStats.textContent = `CPU ${Math.round(state.system_stats.cpu_percent)}% · RAM ${Math.round(
      state.system_stats.ram_percent
    )}%`;

    const btnHostElevate = document.getElementById("btn-host-elevate");
    const adminStatusText = document.getElementById("admin-status-text");
    if (btnHostElevate && adminStatusText) {
      if (state.is_admin) {
        btnHostElevate.disabled = true;
        btnHostElevate.textContent = "🛡 Running as Admin";
        adminStatusText.textContent = "Administrator (UAC elevated)";
        modeBadge.textContent = "Ready · Admin";
      } else {
        btnHostElevate.disabled = false;
        btnHostElevate.textContent = "🛡 Run as Administrator";
        adminStatusText.textContent = "Standard User";
        modeBadge.textContent = "Ready";
      }
    }

    // Relay status
    const relayBadge = document.getElementById("relay-status-badge");
    const relayText = document.getElementById("relay-status-text");
    const relayErr = document.getElementById("relay-error-text");
    const relayInput = document.getElementById("input-relay-url");

    if (document.activeElement !== relayInput && state.relay_url !== undefined) {
      relayInput.value = state.relay_url || "";
    }
    if (document.activeElement !== connUrlInput) {
      connUrlInput.value = state.relay_url || window.location.origin;
    }
    if (state.relay_url) {
      if (state.relay_status === "connected") {
        relayBadge.textContent = "Connected to Relay";
        relayBadge.className = "badge badge-green";
        relayText.textContent = `Registered as ${state.desk_id} on ${state.relay_url}`;
      } else if (state.relay_status === "connecting") {
        relayBadge.textContent = "Connecting...";
        relayBadge.className = "badge badge-amber";
        relayText.textContent = `Connecting to ${state.relay_url}...`;
      } else {
        relayBadge.textContent = "Relay Error";
        relayBadge.className = "badge badge-red";
        relayText.textContent = `Failed to reach ${state.relay_url}`;
      }
      relayErr.textContent = state.relay_error || "";
    } else {
      relayBadge.textContent = "Relay Server Mode";
      relayBadge.className = "badge badge-green";
      relayText.textContent = `Acting as Central Relay (${state.relay_online_desks || 0} remote desks online)`;
      relayErr.textContent = "";
    }

    // Share link
    const ts = state.tunnel_status || {};
    const shareUrl =
      ts.quick_tunnel_url || state.tunnel_url || state.relay_url || `http://localhost:${state.port}`;
    document.getElementById("share-link-input").value = shareUrl;

    // Tunnel status
    const cfBadge = document.getElementById("cf-worker-badge");
    const cfVer = document.getElementById("cf-version-text");
    const cfErr = document.getElementById("cf-error-text");

    if (ts.system_worker_running) {
      cfBadge.textContent = "Worker Active";
      cfBadge.className = "badge badge-green";
    } else if (ts.downloading) {
      cfBadge.textContent = "Downloading cloudflared...";
      cfBadge.className = "badge badge-amber";
    } else if (ts.quick_tunnel_running) {
      cfBadge.textContent = "Quick Tunnel Active";
      cfBadge.className = "badge badge-green";
    } else if (ts.cloudflared_installed) {
      cfBadge.textContent = "Ready";
      cfBadge.className = "badge";
    } else {
      cfBadge.textContent = "Auto-Download Ready";
      cfBadge.className = "badge";
    }

    cfVer.textContent = ts.cloudflared_installed
      ? `${ts.cloudflared_version} · Portable Quick Tunnel ready`
      : "cloudflared will be downloaded automatically when Quick Tunnel starts.";

    const chkAutoQuick = document.getElementById("chk-auto-quick-tunnel");
    if (chkAutoQuick) {
      chkAutoQuick.checked = Boolean(state.auto_quick_tunnel);
    }

    const tunnelInput = document.getElementById("input-tunnel-url");
    if (document.activeElement !== tunnelInput && state.tunnel_url !== undefined) {
      tunnelInput.value = state.tunnel_url || "";
    }

    const btnQuick = document.getElementById("btn-quick-tunnel");
    const quickBox = document.getElementById("quick-url-box");
    const quickLink = document.getElementById("quick-url-link");
    if (ts.quick_tunnel_running) {
      btnQuick.textContent = "Stop Quick Tunnel";
      if (ts.quick_tunnel_url) {
        quickBox.style.display = "flex";
        quickLink.href = ts.quick_tunnel_url;
        quickLink.textContent = ts.quick_tunnel_url;
      } else {
        quickBox.style.display = "flex";
        quickLink.removeAttribute("href");
        quickLink.textContent = "Starting tunnel...";
      }
    } else {
      btnQuick.textContent = "Start Quick Tunnel";
      quickBox.style.display = "none";
    }
    cfErr.textContent = ts.quick_tunnel_error || "";

    // Security controls
    document.getElementById("chk-require-approval").checked = Boolean(state.require_approval);
    document.getElementById("chk-allow-input").checked = Boolean(state.allow_input);
    document.getElementById("pw-status-text").textContent = state.has_unattended_password
      ? "Unattended password is set"
      : "Not set";

    // Pending approval requests
    const pendingCard = document.getElementById("pending-card");
    const pendingList = document.getElementById("pending-list");
    const pending = state.pending_requests || [];
    if (pending.length > 0) {
      pendingCard.style.display = "block";
      pendingList.innerHTML = "";
      pending.forEach((req) => {
        const row = document.createElement("div");
        row.className = "session-item";
        row.innerHTML = `
          <div>
            <div><strong>${escapeHtml(req.client_name)}</strong></div>
            <div class="small muted">${escapeHtml(req.remote_ip)}</div>
          </div>
          <div class="session-actions">
            <button class="btn btn-primary btn-sm" data-act="approve">Accept</button>
            <button class="btn btn-secondary btn-sm" data-act="reject">Reject</button>
          </div>
        `;
        row.querySelector('[data-act="approve"]').onclick = () =>
          hostFetch(`/api/host/requests/${req.request_id}/decide`, {
            method: "POST",
            body: JSON.stringify({ approve: true }),
          }).then(renderHostState);
        row.querySelector('[data-act="reject"]').onclick = () =>
          hostFetch(`/api/host/requests/${req.request_id}/decide`, {
            method: "POST",
            body: JSON.stringify({ approve: false }),
          }).then(renderHostState);
        pendingList.appendChild(row);
      });
    } else {
      pendingCard.style.display = "none";
    }

    // Active sessions
    const sessList = document.getElementById("active-sessions-list");
    const sessions = state.active_sessions || [];
    if (sessions.length === 0) {
      sessList.innerHTML = '<div class="muted small">No active remote sessions</div>';
    } else {
      sessList.innerHTML = "";
      sessions.forEach((s) => {
        const row = document.createElement("div");
        row.className = "session-item";
        row.innerHTML = `
          <div>
            <div><strong>${escapeHtml(s.client_name)}</strong></div>
            <div class="small muted">${escapeHtml(s.remote_ip)} · ${
          s.allow_input ? "Control" : "View-Only"
        }</div>
          </div>
          <div class="session-actions">
            <button class="btn btn-secondary btn-sm" data-act="toggle">${
              s.allow_input ? "Lock Input" : "Allow Input"
            }</button>
            <button class="btn btn-primary btn-sm" data-act="kick">Disconnect</button>
          </div>
        `;
        row.querySelector('[data-act="toggle"]').onclick = () =>
          hostFetch(`/api/host/sessions/${s.session_id}/toggle_input`, {
            method: "POST",
          }).then(renderHostState);
        row.querySelector('[data-act="kick"]').onclick = () =>
          hostFetch(`/api/host/sessions/${s.session_id}/disconnect`, {
            method: "POST",
          }).then(renderHostState);
        sessList.appendChild(row);
      });
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (m) => {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m];
    });
  }

  function connectHostWs() {
    const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
    const qp = adminToken ? `?admin_token=${encodeURIComponent(adminToken)}` : "";
    hostWs = new WebSocket(`${wsProto}//${location.host}/ws/host${qp}`);
    hostWs.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state" && msg.data) {
        renderHostState(msg.data);
      }
    };
    hostWs.onclose = () => {
      setTimeout(connectHostWs, 3000);
    };
  }

  function bindHostEvents() {
    const btnCopyDeskId = document.getElementById("btn-copy-desk-id");
    if (btnCopyDeskId) {
      btnCopyDeskId.addEventListener("click", () => {
        if (hostState?.desk_id) {
          navigator.clipboard?.writeText(hostState.desk_id);
          btnCopyDeskId.textContent = "Copied!";
          setTimeout(() => {
            btnCopyDeskId.textContent = "Copy";
          }, 1500);
        }
      });
    }

    const btnToggleSettings = document.getElementById("btn-toggle-settings");
    const settingsDrawer = document.getElementById("settings-drawer");
    if (btnToggleSettings && settingsDrawer) {
      btnToggleSettings.addEventListener("click", () => {
        const isHidden = settingsDrawer.style.display === "none";
        settingsDrawer.style.display = isHidden ? "grid" : "none";
      });
    }

    const btnHostElevate = document.getElementById("btn-host-elevate");
    if (btnHostElevate) {
      btnHostElevate.addEventListener("click", async () => {
        btnHostElevate.disabled = true;
        btnHostElevate.textContent = "🛡 Waiting for UAC...";
        try {
          const res = await hostFetch("/api/host/elevate", { method: "POST" });
          if (res.restarting) {
            btnHostElevate.textContent = "🛡 Restarting as Admin...";
          }
        } catch (err) {
          alert(err.message || "Elevation cancelled.");
          if (hostState) renderHostState(hostState);
        }
      });
    }

    document.getElementById("btn-toggle-pin").addEventListener("click", () => {
      pinVisible = !pinVisible;
      if (hostState) renderHostState(hostState);
    });

    document.getElementById("btn-copy-pin").addEventListener("click", () => {
      if (hostState?.session_pin) navigator.clipboard?.writeText(hostState.session_pin);
    });

    document.getElementById("btn-regen-pin").addEventListener("click", async () => {
      await hostFetch("/api/host/pin/regenerate", { method: "POST" });
      pinVisible = true;
      const st = await hostFetch("/api/host/state");
      renderHostState(st);
    });

    document.getElementById("btn-copy-link").addEventListener("click", () => {
      const val = document.getElementById("share-link-input").value;
      navigator.clipboard?.writeText(val);
    });

    document.getElementById("btn-copy-quick-url").addEventListener("click", () => {
      const val = document.getElementById("quick-url-link").textContent;
      navigator.clipboard?.writeText(val);
    });

    document.getElementById("btn-save-relay-url").addEventListener("click", async () => {
      const val = document.getElementById("input-relay-url").value.trim();
      const st = await hostFetch("/api/host/settings", {
        method: "POST",
        body: JSON.stringify({ relay_url: val }),
      });
      renderHostState(st);
    });

    document.getElementById("btn-save-tunnel-url").addEventListener("click", async () => {
      const val = document.getElementById("input-tunnel-url").value.trim();
      const st = await hostFetch("/api/host/settings", {
        method: "POST",
        body: JSON.stringify({ tunnel_url: val }),
      });
      renderHostState(st);
    });

    document.getElementById("btn-quick-tunnel").addEventListener("click", async () => {
      const running = hostState?.tunnel_status?.quick_tunnel_running;
      const endpoint = running ? "/api/host/tunnel/quick/stop" : "/api/host/tunnel/quick/start";
      const st = await hostFetch(endpoint, { method: "POST" });
      renderHostState(st);
    });

    const chkAutoQuick = document.getElementById("chk-auto-quick-tunnel");
    if (chkAutoQuick) {
      chkAutoQuick.addEventListener("change", async (e) => {
        const st = await hostFetch("/api/host/settings", {
          method: "POST",
          body: JSON.stringify({ auto_quick_tunnel: e.target.checked }),
        });
        renderHostState(st);
      });
    }

    document.getElementById("chk-require-approval").addEventListener("change", async (e) => {
      const st = await hostFetch("/api/host/settings", {
        method: "POST",
        body: JSON.stringify({ require_approval: e.target.checked }),
      });
      renderHostState(st);
    });

    document.getElementById("chk-allow-input").addEventListener("change", async (e) => {
      const st = await hostFetch("/api/host/settings", {
        method: "POST",
        body: JSON.stringify({ allow_input: e.target.checked }),
      });
      renderHostState(st);
    });

    document.getElementById("btn-save-pw").addEventListener("click", async () => {
      const pw = document.getElementById("input-unattended-pw").value;
      if (!pw) return;
      const st = await hostFetch("/api/host/settings", {
        method: "POST",
        body: JSON.stringify({ unattended_password: pw }),
      });
      document.getElementById("input-unattended-pw").value = "";
      renderHostState(st);
    });

    document.getElementById("btn-clear-pw").addEventListener("click", async () => {
      const st = await hostFetch("/api/host/settings", {
        method: "POST",
        body: JSON.stringify({ clear_unattended_password: true }),
      });
      renderHostState(st);
    });
  }

  const btnTogglePwInput = document.getElementById("btn-toggle-pw-input");
  const connOptionalFields = document.getElementById("conn-optional-fields");
  if (btnTogglePwInput && connOptionalFields) {
    btnTogglePwInput.addEventListener("click", () => {
      const hidden = connOptionalFields.style.display === "none";
      connOptionalFields.style.display = hidden ? "flex" : "none";
      btnTogglePwInput.textContent = hidden
        ? "− Sembunyikan Opsi Password"
        : "+ Pakai Unattended Password (Opsional)";
    });
  }

  // Client Connect Form
  document.getElementById("connect-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const deskId = connDeskIdInput.value.trim().toUpperCase();
    let targetUrl = (connUrlInput.value.trim() || hostState?.relay_url || window.location.origin).replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(targetUrl)) {
      targetUrl = `https://${targetUrl}`;
    }
    const secret = connSecretInput.value.trim();
    const clientName = connNameInput.value.trim() || "sgDesk Client";

    btnConnect.disabled = true;
    showConnectStatus(
      secret
        ? `Connecting to ${deskId}...`
        : `Sending connection request to ${deskId} (waiting for Accept)...`,
      "pending"
    );

    try {
      const res = await fetch(`${targetUrl}/api/relay/connect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ desk_id: deskId, secret, client_name: clientName }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Authentication failed (${res.status})`);
      }

      let data = await res.json();
      if (data.status === "pending") {
        showConnectStatus("Waiting for host to accept connection...", "pending");
        data = await pollApproval(targetUrl, data.request_id);
      }

      if (data.status === "approved" && data.token) {
        topbar.style.display = "none";
        dashboardView.style.display = "none";
        viewerView.style.display = "flex";
        viewer.connect(targetUrl, data.token, data.ws_path || "/ws/desk");
      } else {
        throw new Error("Connection was rejected by host.");
      }
    } catch (err) {
      showConnectStatus(err.message || "Failed to connect.", "error");
    } finally {
      btnConnect.disabled = false;
    }
  });

  async function pollApproval(baseUrl, requestId) {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const res = await fetch(`${baseUrl}/api/auth/status/${requestId}`);
      if (!res.ok) throw new Error("Approval request expired.");
      const data = await res.json();
      if (data.status === "approved" || data.status === "rejected") {
        return data;
      }
    }
    throw new Error("Timed out waiting for host approval.");
  }

  // Initialize
  async function init() {
    connUrlInput.value = window.location.origin;
    try {
      const info = await fetch("/api/public/info").then((r) => r.json());
      document.getElementById("brand-sub").textContent = `${info.hostname} · ${info.desk_id}`;

      if (info.is_local_host) {
        modeBadge.textContent = "Ready";
        modeBadge.className = "pill badge-green";
        hostPanel.style.display = "flex";
        const btnToggleSettings = document.getElementById("btn-toggle-settings");
        if (btnToggleSettings) btnToggleSettings.style.display = "inline-flex";
        dashboardView.classList.remove("remote-only");
        bindHostEvents();
        const st = await hostFetch("/api/host/state");
        renderHostState(st);
        connectHostWs();
      } else {
        modeBadge.textContent = "Remote Client";
        modeBadge.className = "pill badge-red";
        hostPanel.style.display = "none";
        dashboardView.classList.add("remote-only");
      }
    } catch (err) {
      modeBadge.textContent = "Offline";
      showConnectStatus("Unable to reach sgDesk server.", "error");
    }
  }

  init();
})();
