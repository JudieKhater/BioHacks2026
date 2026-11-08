// Recap: shared helpers for every page. No build step, no external libraries.

// Optional shared token: open any page once as ?token=XYZ and it is remembered.
(function captureToken() {
  const t = new URLSearchParams(location.search).get("token");
  if (t) { try { localStorage.setItem("recapToken", t); } catch (e) {} }
})();
function token() { try { return localStorage.getItem("recapToken") || ""; } catch (e) { return ""; } }

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Recap-Token": token() },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

// WebSocket that reconnects on its own (venue Wi-Fi drops happen).
function liveSocket(path, onMessage, onStatus) {
  let ws, timer;
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
  function connect() {
    ws = new WebSocket(url);
    ws.onopen = () => { onStatus && onStatus(true); timer = setInterval(() => ws.readyState === 1 && ws.send("ping"), 20000); };
    ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = () => { onStatus && onStatus(false); clearInterval(timer); setTimeout(connect, 1500); };
  }
  connect();
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// Minimal line chart on a <canvas>. series: [{label, color, points:[[x,y],...]}]
function lineChart(canvas, series, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const pad = { l: 42, r: 12, t: 10, b: 26 };
  const pts = series.flatMap(s => s.points.filter(p => p[1] !== null && p[1] !== undefined));
  if (!pts.length) {
    ctx.fillStyle = cssVar("--muted"); ctx.font = "14px system-ui";
    ctx.fillText(opts.empty || "No data yet", pad.l, h / 2); return;
  }
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = opts.yMin ?? Math.min(...ys), y1 = opts.yMax ?? Math.max(...ys);
  if (x0 === x1) { x0 -= 1; x1 += 1; }
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const X = x => pad.l + (x - x0) / (x1 - x0) * (w - pad.l - pad.r);
  const Y = y => h - pad.b - (y - y0) / (y1 - y0) * (h - pad.t - pad.b);

  ctx.strokeStyle = cssVar("--line"); ctx.lineWidth = 1;
  ctx.fillStyle = cssVar("--muted"); ctx.font = "11px system-ui";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(v.toFixed(opts.decimals ?? 0), 4, y + 4);
  }
  (opts.refLines || []).forEach(r => {
    ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = cssVar("--warn");
    ctx.beginPath(); ctx.moveTo(pad.l, Y(r.y)); ctx.lineTo(w - pad.r, Y(r.y)); ctx.stroke();
    ctx.fillStyle = cssVar("--warn"); ctx.fillText(r.label, w - pad.r - ctx.measureText(r.label).width, Y(r.y) - 4);
    ctx.restore();
  });
  const xl = opts.xLabels || [];
  xl.forEach(([x, label]) => ctx.fillText(label, Math.min(X(x) - 10, w - 60), h - 8));

  series.forEach(s => {
    ctx.strokeStyle = s.color; ctx.lineWidth = 2.5; ctx.beginPath();
    let started = false;
    s.points.forEach(([x, y]) => {
      if (y === null || y === undefined) { started = false; return; }
      started ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)); started = true;
    });
    ctx.stroke();
    ctx.fillStyle = s.color;
    s.points.forEach(([x, y]) => { if (y !== null && y !== undefined && s.points.length < 40) { ctx.beginPath(); ctx.arc(X(x), Y(y), 3, 0, 7); ctx.fill(); } });
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Start / End session control. `el` is an empty element; call update(state) with each /ws/state message.
// Ending a session builds the report and opens it.
function sessionControl(el) {
  el.innerHTML = `<div class="session-bar">
      <div><span class="status-dot" data-dot></span><b data-title>Session</b>
        <div class="muted small" data-sub>connecting…</div></div>
      <button class="primary" data-btn disabled>Start session</button></div>`;
  const btn = el.querySelector("[data-btn]"), title = el.querySelector("[data-title]"),
        sub = el.querySelector("[data-sub]"), dot = el.querySelector("[data-dot]");
  let st = null, timer = null, busy = false;
  function tick() {
    if (!st || !st.session.active) return;
    const mins = Math.floor((Date.now() / 1000 - st.session.started) / 60);
    sub.textContent = `Listening for ${mins} min` + (st.mic_connected ? "" : " · mic client not running");
  }
  function update(s) {
    if (!s || !s.session || busy) return;
    st = s;
    const on = s.session.active;
    dot.classList.toggle("live", on);
    title.textContent = on ? "Session running" : "No session running";
    btn.textContent = on ? "End session" : "Start session";
    btn.classList.toggle("primary", !on);
    btn.disabled = false;
    clearInterval(timer);
    if (on) { tick(); timer = setInterval(tick, 15000); }
    else sub.textContent = s.mic_connected ? "The mic is off until you start."
      : "Mic client not running: open start_session.bat (or run client/audio_client.py).";
  }
  btn.onclick = async () => {
    if (!st) return;
    btn.disabled = true;
    if (!st.session.active) {
      try { await api("/api/session/start", {}); } catch (e) { sub.textContent = "Couldn't start: " + e.message; btn.disabled = false; }
      return;
    }
    busy = true;
    btn.textContent = "Building report…";
    sub.textContent = "Mic off. Working out your guidance and breathing plan.";
    try {
      const r = await api("/api/session/end", {});
      location.href = `/report.html?id=${encodeURIComponent(r.id)}`;
    } catch (e) { busy = false; sub.textContent = "Couldn't end the session: " + e.message; btn.disabled = false; }
  };
  return { update };
}

function nav(active) {
  const links = [["/", "Home"], ["/alex.html", "Alex"], ["/captions.html", "Captions"], ["/notes.html", "Notes"],
                 ["/report.html", "Report"], ["/trainer.html", "Breathing"], ["/recap.html", "Recap"], ["/clinic.html", "Clinic"]];
  return links.map(([h, t]) => `<a href="${h}"${t === active ? ' aria-current="page"' : ""}>${t}</a>`).join("");
}
