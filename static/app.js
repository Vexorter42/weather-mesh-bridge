// Weather → Mesh UI
const DAYS = [
  { k: "mon", t: "Пн" }, { k: "tue", t: "Вт" }, { k: "wed", t: "Ср" },
  { k: "thu", t: "Чт" }, { k: "fri", t: "Пт" }, { k: "sat", t: "Сб" },
  { k: "sun", t: "Вс" },
];
let ALL_FIELDS = [];
let CONFIG = {};

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));

// Escape text, then turn http(s) URLs into clickable links (opens new tab).
function linkify(text) {
  return escapeHtml(text || "").replace(/https?:\/\/[^\s<]+/g, (url) => {
    let clean = url, tail = "";
    const m = clean.match(/[.,!?)\]]+$/);   // keep trailing sentence punctuation out of the link
    if (m) { tail = clean.slice(-m[0].length); clean = clean.slice(0, -m[0].length); }
    return `<a href="${clean}" target="_blank" rel="noopener noreferrer" class="chat-link">${clean}</a>${tail}`;
  });
}

// Russian plural for "hop"
function pluralHops(n) {
  const a = Math.abs(Math.trunc(n));
  const last2 = a % 100;
  const last = a % 10;
  if (last2 >= 11 && last2 <= 14) return "прыжков";
  if (last === 1) return "прыжок";
  if (last >= 2 && last <= 4) return "прыжка";
  return "прыжков";
}

// classify RSSI strength for color coding (LoRa typical ranges)
function rssiClass(rssi) {
  if (rssi == null) return "";
  if (rssi >= -90) return "good";
  if (rssi >= -110) return "ok";
  return "weak";
}

function buildRfMeta(m) {
  const parts = [];
  if (m.hops_taken != null) {
    if (m.hops_taken === 0) {
      parts.push(`<span class="hops direct" title="Принято напрямую без ретрансляторов">↯ напрямую</span>`);
    } else {
      parts.push(`<span class="hops" title="Количество ретрансляций">↯ ${m.hops_taken} ${pluralHops(m.hops_taken)}</span>`);
    }
  }
  if (m.rx_rssi != null) {
    parts.push(`<span class="rssi ${rssiClass(m.rx_rssi)}" title="Сила сигнала">${Math.round(m.rx_rssi)} dBm</span>`);
  }
  if (m.rx_snr != null) {
    parts.push(`<span class="snr" title="Соотношение сигнал/шум">SNR ${m.rx_snr.toFixed(1)}</span>`);
  }
  if (m.via_mqtt) {
    parts.push(`<span class="mqtt" title="Пришло через MQTT-шлюз (интернет), не напрямую по радио">🌐 MQTT</span>`);
  }
  return parts.length ? `<div class="rf-meta">${parts.join(" · ")}</div>` : "";
}

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = (typeof t === "function") ? t(msg) : msg;
  el.className = "toast show " + kind;
  setTimeout(() => el.classList.remove("show"), 2400);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------- Tabs ----------
let CURRENT_TAB = "home";

// Format a unix timestamp as a relative-ago string in Russian.
function relTime(ts) {
  if (!ts) return "—";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (sec < 60) return `${sec} с назад`;
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} ч назад`;
  const days = Math.floor(sec / 86400);
  return `${days} ${days === 1 ? "день" : (days < 5 ? "дня" : "дней")} назад`;
}
$$(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    CURRENT_TAB = btn.dataset.tab;
    $$(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
    $$(".tab-panel").forEach(p => p.classList.toggle("active", p.dataset.tab === CURRENT_TAB));
    if (CURRENT_TAB === "chat") {
      const badge = $("#chatBadge");
      badge.hidden = true; badge.textContent = "0";
      const log = $("#chatLog");
      requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    } else if (CURRENT_TAB === "home") {
      refreshDashboard();
    } else if (CURRENT_TAB === "net") {
      refreshMap();
    } else if (CURRENT_TAB === "weather") {
      refreshNowcastStatus();
    } else if (CURRENT_TAB === "integr") {
      refreshTelegramStatus();
      refreshTgStatusBot();
      refreshLlmStatus();
      loadMqttConfig();
      refreshMqttStatus();
    } else if (CURRENT_TAB === "system") {
      refreshHealth();
      refreshUpdateInfo();
      loadProxyConfig();
    }
  });
});

// ---------- Theme (light / dark) ----------
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function applyTheme(theme) {
  if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  const btn = $("#themeToggle");
  if (btn) btn.textContent = theme === "light" ? "☀️" : "🌙";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#e9edf6" : "#0a0e1a");
}
(function initTheme() {
  applyTheme(currentTheme());
  $("#themeToggle")?.addEventListener("click", () => {
    const next = currentTheme() === "light" ? "dark" : "light";
    try { localStorage.setItem("theme", next); } catch (e) {}
    applyTheme(next);
  });
})();

// ---------- Collapsible cards (Settings / Прочее) ----------
// Wrap each card's body so its header toggles it; remember state per card.
// Keeps the form-heavy tabs short — open only what you need.
function enhanceCollapsibleCards() {
  ["weather", "integr", "system", "net"].forEach((tab) => {
    document.querySelectorAll(`.tab-panel[data-tab="${tab}"] > .card`).forEach((card, i) => {
      if (card.dataset.collapsible) return;
      const h2 = card.querySelector(":scope > h2");
      if (!h2) return;
      card.dataset.collapsible = "1";
      const body = document.createElement("div");
      body.className = "card-body";
      let n = h2.nextSibling;
      while (n) { const next = n.nextSibling; body.appendChild(n); n = next; }
      card.appendChild(body);
      h2.classList.add("card-toggle");
      const chev = document.createElement("span");
      chev.className = "card-chev";
      chev.textContent = "▸";
      chev.setAttribute("aria-hidden", "true");
      h2.appendChild(chev);
      const key = "card:" + (card.id || h2.textContent.trim().slice(0, 40));
      const saved = localStorage.getItem(key);
      const open = saved === "open" ? true : saved === "closed" ? false : i === 0;
      card.classList.toggle("collapsed", !open);
      h2.addEventListener("click", () => {
        const collapse = !card.classList.contains("collapsed");
        card.classList.toggle("collapsed", collapse);
        try { localStorage.setItem(key, collapse ? "closed" : "open"); } catch (e) {}
      });
    });
  });
}
enhanceCollapsibleCards();

// ---------- Dashboard ----------
let KNOWN_NODES = [];     // last fetched node list (for DM picker)

async function refreshDashboard() {
  try {
    const [stats, nodes, channels, wxCur, wxHourly, airtime] = await Promise.all([
      api("/api/stats"),
      api("/api/nodes").catch(() => []),
      api("/api/channels").catch(() => []),
      api("/api/weather/current").catch(() => null),
      api("/api/weather/hourly").catch(() => null),
      api("/api/airtime").catch(() => null),
    ]);
    KNOWN_NODES = Array.isArray(nodes) ? nodes : [];
    KNOWN_CHANNELS = Array.isArray(channels) ? channels : [];
    // Populate selectors FIRST — so a later render error (weather widget /
    // hourly chart) can't leave the channel/destination dropdowns empty.
    populateDestinationSelectors(KNOWN_NODES);
    populateTgChannelSelect();
    populateRailChannel();
    rebuildConversations();
    renderConvList();
    renderDashboard(stats, KNOWN_NODES);
    renderAirtime(airtime);
    renderWeatherWidget(wxCur);
    renderHourlyChart(wxHourly);
  } catch (e) { /* silent — dashboard isn't critical */ }
}

function renderAirtime(a) {
  if (!a) return;
  const gauge = (valEl, fillEl, pct) => {
    const v = $(valEl), f = $(fillEl);
    if (pct == null || isNaN(pct)) {
      if (v) v.textContent = "—";
      if (f) { f.style.width = "0%"; f.className = "ag-fill"; }
      return;
    }
    const p = Math.max(0, Math.min(100, Number(pct)));
    if (v) v.textContent = `${p.toFixed(p < 10 ? 1 : 0)}%`;
    if (f) {
      f.style.width = `${p}%`;
      f.className = "ag-fill" + (p >= 50 ? " danger" : p >= 25 ? " warn" : "");
    }
  };
  gauge("#airChanVal", "#airChanFill", a.channel_utilization);
  gauge("#airTxVal", "#airTxFill", a.air_util_tx);
  $("#airSent1h").textContent = a.sent_1h ?? "—";
  $("#airRecv1h").textContent = a.received_1h ?? "—";
  $("#airSent24h").textContent = `${a.sent_24h ?? 0} за сутки`;
  $("#airRecv24h").textContent = `${a.received_24h ?? 0} за сутки`;

  // Warn banner when the channel is congested.
  const hint = $("#airtimeHint");
  if (hint) {
    const cu = a.channel_utilization;
    if (cu != null && cu >= 50) {
      hint.innerHTML = "🔴 <strong>Эфир перегружен</strong> — пакеты теряются. Сократи рассылки/частоту или подними интервалы.";
    } else if (cu != null && cu >= 25) {
      hint.innerHTML = "🟡 <strong>Эфир нагружен</strong> — близко к порогу. Бот уже придерживает ответы; не лей лишнего.";
    } else {
      hint.innerHTML = "Сколько эфира занято в твоём канале. Выше ~25% — пакеты начинают теряться и растут задержки; бот сам притормаживает рассылки.";
    }
  }
}

function renderDashboard(s, nodes) {
  const conn = $("#statConn");
  if (s.mesh_connected) {
    conn.textContent = t("Подключено");
    conn.className = "stat-value good";
  } else {
    conn.textContent = t("Нет связи");
    conn.className = "stat-value bad";
  }
  $("#statConnSub").textContent = s.mesh_connected ? t("Heltec на связи") : t("Проверь настройки");
  $("#statNodes").textContent = s.mesh_nodes_known ?? "—";
  $("#statSenders").textContent = s.unique_senders_24h ?? 0;
  $("#statTotal").textContent = s.total_messages ?? 0;
  $("#statSent").textContent = s.sent_24h ?? 0;
  $("#statRecv").textContent = s.received_24h ?? 0;
  $("#statRssi").textContent = s.avg_rssi_24h != null ? `${s.avg_rssi_24h} dBm` : "—";
  $("#statHops").textContent = s.avg_hops_24h != null ? s.avg_hops_24h.toFixed(1) : "—";
  $("#statLastOut").textContent = relTime(s.last_outgoing_ts);
  $("#statLastIn").textContent = relTime(s.last_incoming_ts);

  const wrap = $("#nodesList");
  wrap.innerHTML = "";
  if (!nodes.length) {
    wrap.innerHTML = "<div class='muted'>Список пуст — нода ещё никого не слышала.</div>";
    return;
  }
  for (const n of nodes) {
    const row = document.createElement("div");
    row.className = "node-row";
    row.title = "Открыть профиль";
    const long = n.long_name || n.node_id || "?";
    const short = n.short_name ? `<span class="node-short">[${escapeHtml(n.short_name)}]</span>` : "";
    const age = n.last_heard ? relTime(n.last_heard) : "—";
    const snr = n.snr != null ? `<span class="node-snr">SNR ${Number(n.snr).toFixed(1)}</span>` : "";
    row.innerHTML =
      `<div><span class="node-name">${escapeHtml(long)}</span>${short}</div>` +
      `<div class="node-age">${age}</div>` +
      `<div>${snr}</div>`;
    row.addEventListener("click", () => openNodeProfile(n.node_id));
    wrap.appendChild(row);
  }
}

// ---------- Weather widget + hourly chart ----------

function renderWeatherWidget(w) {
  const body = $("#wwBody");
  const cityEl = $("#wwCity");
  if (!body) return;
  if (!w || w.error) {
    body.innerHTML = `<div class="muted">${escapeHtml((w && w.error) || "Нет данных. Выбери город в настройках.")}</div>`;
    if (cityEl) cityEl.textContent = "";
    return;
  }
  if (cityEl) cityEl.textContent = w.city || "";
  const temp = (w.temperature_c != null) ? `${w.temperature_c > 0 ? "+" : ""}${w.temperature_c.toFixed(1)}°C` : "—";
  const feels = (w.feels_like_c != null) ? `${w.feels_like_c > 0 ? "+" : ""}${w.feels_like_c.toFixed(0)}°` : "—";
  const minmax = (w.today_min != null && w.today_max != null)
    ? `${w.today_min > 0 ? "+" : ""}${Math.round(w.today_min)}…${w.today_max > 0 ? "+" : ""}${Math.round(w.today_max)}°`
    : "—";
  const wind = (w.wind_speed_ms != null)
    ? `${w.wind_speed_ms.toFixed(1)} м/с${w.wind_direction ? " " + w.wind_direction : ""}${w.wind_gusts_ms ? " (порывы " + Math.round(w.wind_gusts_ms) + ")" : ""}`
    : "—";
  const hum = (w.humidity != null) ? `${Math.round(w.humidity)}%` : "—";
  const press = (w.pressure_mmhg != null) ? `${w.pressure_mmhg} мм рт.ст.` : "—";
  const precip = (w.precipitation_mm != null && w.precipitation_mm > 0) ? `${w.precipitation_mm} мм` : "—";

  body.innerHTML = `
    <div class="ww-main">
      <div class="ww-temp">${escapeHtml(temp)}</div>
      <div class="ww-cond">
        <div class="ww-emoji">${w.condition_emoji || (w.is_day ? "☀️" : "🌙")}</div>
        <div class="ww-cond-text">${escapeHtml(w.condition_text || "—")}</div>
        <div class="ww-feels muted">ощущается ${escapeHtml(feels)} · ${escapeHtml(minmax)} сегодня</div>
      </div>
    </div>
    <div class="ww-grid">
      <div class="ww-item"><span class="muted">💨 ветер</span><strong>${escapeHtml(wind)}</strong></div>
      <div class="ww-item"><span class="muted">💧 влажность</span><strong>${escapeHtml(hum)}</strong></div>
      <div class="ww-item"><span class="muted">⏲ давление</span><strong>${escapeHtml(press)}</strong></div>
      <div class="ww-item"><span class="muted">🌧 осадки</span><strong>${escapeHtml(precip)}</strong></div>
    </div>
  `;
}

function renderHourlyChart(h) {
  const wrap = $("#hourlyChart");
  if (!wrap) return;
  if (!h || h.error || !h.time || !h.time.length) {
    wrap.innerHTML = `<div class="muted">${escapeHtml((h && h.error) || "Нет данных.")}</div>`;
    return;
  }
  const temps = h.temperature_2m.map(v => v == null ? null : Number(v));
  const probs = (h.precipitation_probability || []).map(v => v == null ? 0 : Number(v));
  const hums  = (h.relative_humidity_2m   || []).map(v => v == null ? null : Number(v));
  const winds = (h.wind_speed_10m         || []).map(v => v == null ? null : Number(v));
  const times = h.time.map(t => t.slice(11, 16));   // "HH:MM"

  const n = temps.length;
  if (!n) { wrap.innerHTML = `<div class="muted">Нет данных.</div>`; return; }

  // Layout
  const W = 760, H = 200;
  const P = { l: 36, r: 16, t: 18, b: 32 };
  const cw = W - P.l - P.r;
  const ch = H - P.t - P.b;

  const validTemps = temps.filter(v => v != null);
  const tMin = Math.min(...validTemps) - 1;
  const tMax = Math.max(...validTemps) + 1;
  const tRange = (tMax - tMin) || 1;

  const x = i => P.l + (i / (n - 1 || 1)) * cw;
  const yT = t => P.t + ch - ((t - tMin) / tRange) * ch;

  // Precip bars (probability 0..100)
  const barW = Math.max(2, cw / n * 0.55);
  const colW = cw / Math.max(n - 1, 1);
  const bars = probs.map((p, i) => {
    const h2 = (p / 100) * ch;
    return `<rect x="${x(i) - barW/2}" y="${P.t + ch - h2}" width="${barW}" height="${h2}" rx="2" fill="rgba(106,163,255,0.35)"/>`;
  }).join("");

  // Temperature polyline
  const points = temps.map((t, i) => t == null ? null : `${x(i)},${yT(t)}`).filter(Boolean).join(" ");

  // Y-axis ticks (temp)
  const tTicks = [tMin, (tMin+tMax)/2, tMax].map(v => {
    return `<g class="hc-tick">
      <text x="${P.l - 6}" y="${yT(v) + 4}" text-anchor="end">${(v > 0 ? "+" : "") + v.toFixed(0)}°</text>
      <line x1="${P.l}" x2="${P.l + cw}" y1="${yT(v)}" y2="${yT(v)}" stroke="rgba(255,255,255,0.07)"/>
    </g>`;
  }).join("");

  // X-axis labels every 3-4 hours
  const step = Math.max(1, Math.floor(n / 8));
  const xTicks = times.map((t, i) => i % step === 0
    ? `<text x="${x(i)}" y="${H - 8}" text-anchor="middle" class="hc-tick">${t}</text>` : "").join("");

  // Invisible hit areas covering each hour column (full chart height) — they
  // catch mouse events for the custom tooltip. Wider than the visible bar so
  // hover is forgiving.
  const hitW = Math.max(barW + 4, colW);
  const hits = temps.map((_, i) =>
    `<rect class="hc-hit" data-idx="${i}" x="${x(i) - hitW/2}" y="${P.t}" width="${hitW}" height="${ch}" fill="transparent" pointer-events="all"/>`
  ).join("");

  wrap.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" class="hourly-svg" preserveAspectRatio="xMidYMid meet">
      ${tTicks}
      ${bars}
      <polyline fill="none" stroke="url(#hcGrad)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" points="${points}"/>
      ${temps.map((t, i) => t == null ? "" :
        `<circle class="hc-dot" data-idx="${i}" cx="${x(i)}" cy="${yT(t)}" r="2.5" fill="#fff"/>`).join("")}
      <line id="hcCursor" x1="0" x2="0" y1="${P.t}" y2="${P.t + ch}" stroke="rgba(255,255,255,0.25)" stroke-dasharray="3,3" style="display:none"/>
      ${hits}
      ${xTicks}
      <defs>
        <linearGradient id="hcGrad" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stop-color="#8b9eff"/>
          <stop offset="50%" stop-color="#d77bff"/>
          <stop offset="100%" stop-color="#4dd0e1"/>
        </linearGradient>
      </defs>
    </svg>
    <div class="hc-tooltip" hidden></div>
  `;

  // ----- Wire up the custom tooltip -----
  const svg = wrap.querySelector("svg");
  const tip = wrap.querySelector(".hc-tooltip");
  const cursor = wrap.querySelector("#hcCursor");

  function showTipFor(idx, clientX) {
    const t  = temps[idx];
    const pp = probs[idx];
    const hm = hums[idx];
    const ws = winds[idx];
    const parts = [`<div class="hc-tt-time">${times[idx]}</div>`];
    if (t != null)  parts.push(`<div>🌡 <strong>${(t > 0 ? "+" : "")}${t.toFixed(1)}°C</strong></div>`);
    if (hm != null) parts.push(`<div>💧 ${Math.round(hm)}%</div>`);
    if (pp != null) parts.push(`<div>🌧 ${Math.round(pp)}%</div>`);
    if (ws != null) parts.push(`<div>💨 ${ws.toFixed(1)} м/с</div>`);
    tip.innerHTML = parts.join("");
    tip.hidden = false;

    // Position tooltip — translate SVG x-coord into wrap-relative px.
    const svgBox = svg.getBoundingClientRect();
    const wrapBox = wrap.getBoundingClientRect();
    const xRatio = svgBox.width / W;
    const cxPx = (x(idx)) * xRatio + (svgBox.left - wrapBox.left);
    // Place tooltip above the temp dot, clamp to wrap bounds.
    const ttW = tip.offsetWidth || 130;
    let left = cxPx - ttW / 2;
    left = Math.max(4, Math.min(left, wrapBox.width - ttW - 4));
    tip.style.left = `${left}px`;
    tip.style.top = `4px`;

    // Move vertical cursor line on SVG
    cursor.setAttribute("x1", x(idx));
    cursor.setAttribute("x2", x(idx));
    cursor.style.display = "";
  }

  function hideTip() {
    tip.hidden = true;
    cursor.style.display = "none";
  }

  wrap.querySelectorAll(".hc-hit").forEach(r => {
    r.addEventListener("mouseenter", e => showTipFor(+e.target.dataset.idx, e.clientX));
    r.addEventListener("mousemove",  e => showTipFor(+e.target.dataset.idx, e.clientX));
    r.addEventListener("mouseleave", hideTip);
  });
  // Touch — tap a column to pin tooltip; tap outside to hide.
  wrap.addEventListener("touchstart", e => {
    const t = e.target.closest(".hc-hit");
    if (t) showTipFor(+t.dataset.idx, e.touches[0].clientX);
  }, { passive: true });
}

function populateDestinationSelectors(nodes) {
  // Helper to repopulate any <select> while preserving its current value.
  function fill(selectEl) {
    if (!selectEl) return;
    const prev = selectEl.value;
    selectEl.innerHTML = '<option value="broadcast">📢 Broadcast</option>';
    for (const n of nodes) {
      const opt = document.createElement("option");
      opt.value = n.node_id || `!${(n.num >>> 0).toString(16)}`;
      const long = n.long_name || n.node_id;
      const short = n.short_name ? ` [${n.short_name}]` : "";
      opt.textContent = `👤 ${long}${short}`;
      selectEl.appendChild(opt);
    }
    // Restore previous selection (if still valid) or keep broadcast
    if (prev && Array.from(selectEl.options).some(o => o.value === prev)) {
      selectEl.value = prev;
    }
  }
  fill($("#chatDestination"));
  fill($("#tgDest"));
  // Update every per-slot dropdown too
  $$(".slot .dest").forEach(sel => {
    const prev = sel.value;
    fill(sel);
    if (prev) sel.value = prev;
  });
}

/** Fill the Telegram-bridge channel-index dropdown from KNOWN_CHANNELS. */
function populateTgChannelSelect() {
  const sel = $("#tgChannelIndex");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = "";
  // If we know the real channels, list them with names; otherwise fall back
  // to a plain 0–7 picker.
  const chans = (KNOWN_CHANNELS || []).filter(c => c && c.index != null);
  if (chans.length) {
    for (const ch of chans) {
      const opt = document.createElement("option");
      opt.value = String(ch.index);
      const role = ch.role === "primary" ? " ★" : "";
      opt.textContent = `Канал ${ch.index} — ${ch.name || ""}${role}`;
      sel.appendChild(opt);
    }
  } else {
    for (let i = 0; i < 8; i++) {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = `Канал ${i}`;
      sel.appendChild(opt);
    }
  }
  if (prev && Array.from(sel.options).some(o => o.value === prev)) {
    sel.value = prev;
  }
}

// ---------- Node profile modal ----------
function ensureDmConversation(peerId) {
  const key = `dm:${peerId}`;
  if (!CONVS.has(key)) {
    CONVS.set(key, {
      key,
      kind: "dm",
      channel: null,
      peerId,
      lastMsg: null,
      unread: 0,
    });
    renderConvList();
  }
  return key;
}

function _rowDef(label, value, opts = {}) {
  if (value == null || value === "") return "";
  const v = escapeHtml(String(value));
  if (opts.bar != null) {
    const pct = Math.max(0, Math.min(100, opts.bar));
    const cls = pct >= 60 ? "high" : pct >= 30 ? "mid" : "low";
    return `<div class="profile-row">
      <div class="profile-label">${escapeHtml(label)}</div>
      <div class="profile-value">${v}
        <div class="bar ${cls}" style="--pct: ${pct}%"></div>
      </div>
    </div>`;
  }
  return `<div class="profile-row">
    <div class="profile-label">${escapeHtml(label)}</div>
    <div class="profile-value">${v}</div>
  </div>`;
}

function openNodeProfile(nodeId) {
  const n = KNOWN_NODES.find(x => x.node_id === nodeId);
  if (!n) {
    toast("Узел больше не виден боту", "err");
    return;
  }

  const longName = n.long_name || n.short_name || nodeId;
  $("#profileName").innerHTML =
    `<span style="color: var(--accent)">👤</span> ${escapeHtml(longName)}` +
    (n.short_name ? ` <span class="muted" style="font-size:0.85em">[${escapeHtml(n.short_name)}]</span>` : "");

  const parts = [];

  // Identity
  parts.push(`<div class="profile-section">Идентификация</div>`);
  parts.push(_rowDef("ID", n.node_id));
  if (n.num != null) parts.push(_rowDef("Num", n.num));
  if (n.hw_model) parts.push(_rowDef("Модель", n.hw_model));
  if (n.role) parts.push(_rowDef("Роль", n.role));

  // RF
  parts.push(`<div class="profile-section">Радио</div>`);
  parts.push(_rowDef("Слышали", n.last_heard ? relTime(n.last_heard) : "—"));
  if (n.snr != null) parts.push(_rowDef("SNR", Number(n.snr).toFixed(1)));

  // Position
  if (n.latitude != null && n.longitude != null) {
    parts.push(`<div class="profile-section">Положение</div>`);
    parts.push(_rowDef("Координаты", `${n.latitude.toFixed(5)}, ${n.longitude.toFixed(5)}`));
    if (n.altitude != null) parts.push(_rowDef("Высота", `${Math.round(n.altitude)} м`));
  }

  // Telemetry
  const hasTelemetry = n.battery_level != null || n.voltage != null
                       || n.channel_utilization != null || n.air_util_tx != null
                       || n.uptime_seconds != null;
  if (hasTelemetry) {
    parts.push(`<div class="profile-section">Телеметрия</div>`);
    if (n.battery_level != null) {
      parts.push(_rowDef("Батарея", `${n.battery_level}%`, { bar: n.battery_level }));
    }
    if (n.voltage != null) parts.push(_rowDef("Напряжение", `${Number(n.voltage).toFixed(2)} В`));
    if (n.channel_utilization != null) {
      const pct = Number(n.channel_utilization);
      parts.push(_rowDef(
        "Загрузка канала",
        `${pct.toFixed(1)}%`,
        { bar: Math.min(100, 100 - pct * 2) },  // invert: высокая загрузка = красная
      ));
    }
    if (n.air_util_tx != null) {
      parts.push(_rowDef("Air util TX", `${Number(n.air_util_tx).toFixed(1)}%`));
    }
    if (n.uptime_seconds != null) {
      const u = Number(n.uptime_seconds);
      let s;
      if (u < 60) s = `${Math.round(u)} с`;
      else if (u < 3600) s = `${Math.round(u / 60)} мин`;
      else if (u < 86400) s = `${(u / 3600).toFixed(1)} ч`;
      else s = `${(u / 86400).toFixed(1)} дн`;
      parts.push(_rowDef("Аптайм", s));
    }
  }

  if (n.num != null) {
    parts.push(`<div class="profile-section">История телеметрии (24ч)</div>`);
    parts.push(`<div id="profileCharts" class="node-charts muted">Загружаю график…</div>`);
  }
  if (nodeId) {
    parts.push(`<div class="profile-section">История маршрутов</div>`);
    parts.push(`<div id="profileTraceHist" class="trace-hist muted">Загружаю…</div>`);
  }

  $("#profileBody").innerHTML = parts.join("");
  $("#nodeProfile").hidden = false;
  if (n.num != null) loadNodeCharts(n.num);
  if (nodeId) loadTraceHistory(nodeId);
  // Reset traceroute panel between opens
  const tr = $("#tracerouteResult");
  if (tr) { tr.hidden = true; tr.innerHTML = ""; }

  // Wire action buttons (re-bind each open — node changes between calls)
  $("#profileDm").onclick = () => {
    closeNodeProfile();
    ensureDmConversation(nodeId);
    const tabBtn = document.querySelector('.tab-btn[data-tab="chat"]');
    if (tabBtn) tabBtn.click();
    setTimeout(() => selectConversation(`dm:${nodeId}`), 80);
  };

  $("#profileTrace").onclick = async () => {
    const out = $("#tracerouteResult");
    out.hidden = false;
    out.innerHTML = `<div class="muted">📡 Шлю traceroute, жду ответа (до 1 мин)…</div>`;
    const btn = $("#profileTrace");
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = "🛰 Идёт…";
    try {
      const r = await api("/api/mesh/traceroute", {
        method: "POST",
        body: { destination: nodeId, hop_limit: 5, timeout: 60 },
      });
      renderTracerouteResult(out, r, nodeId, n);
    } catch (e) {
      out.innerHTML = `<div class="muted" style="color: var(--danger)">⚠️ ${escapeHtml(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  };

  $("#profileMap").onclick = () => {
    closeNodeProfile();
    const tabBtn = document.querySelector('.tab-btn[data-tab="net"]');
    if (tabBtn) tabBtn.click();
    if (n.latitude != null && n.longitude != null) {
      setTimeout(() => {
        if (typeof L !== "undefined" && MAP) {
          MAP.setView([n.latitude, n.longitude], 14);
          // Pop the corresponding marker, if we can find it
          MAP_MARKER_LAYER?.eachLayer(m => {
            const ll = m.getLatLng?.();
            if (ll && Math.abs(ll.lat - n.latitude) < 1e-5 && Math.abs(ll.lng - n.longitude) < 1e-5) {
              m.openPopup?.();
            }
          });
        }
      }, 400);
    } else {
      toast("У узла нет координат — он не появится на карте", "");
    }
  };
}

function closeNodeProfile() {
  $("#nodeProfile").hidden = true;
}

// ---- Node telemetry sparklines (history from history.db) ----
function _spark(values, color) {
  const W = 260, H = 38, pad = 3;
  const nums = values.filter(v => v != null).map(Number);
  if (nums.length < 2) return "";
  const min = Math.min(...nums), max = Math.max(...nums);
  const span = (max - min) || 1;
  const n = values.length;
  let d = "", started = false;
  values.forEach((v, i) => {
    if (v == null) { started = false; return; }
    const x = pad + (W - 2 * pad) * (n === 1 ? 0 : i / (n - 1));
    const y = pad + (H - 2 * pad) * (1 - (Number(v) - min) / span);
    d += (started ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1) + " ";
    started = true;
  });
  return `<svg class="nc-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`
       + `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
function _fmtNum(v) {
  if (v == null) return "—";
  return Number.isInteger(Number(v)) ? String(v) : Number(v).toFixed(1);
}
async function loadNodeCharts(num) {
  const box = $("#profileCharts");
  if (!box) return;
  let data;
  try { data = await api(`/api/nodes/${num}/telemetry?hours=24`); }
  catch { box.className = "node-charts"; box.innerHTML = `<span class="muted">Не удалось загрузить историю.</span>`; return; }
  if (!Array.isArray(data) || data.length < 2) {
    box.className = "node-charts";
    box.innerHTML = `<span class="muted">Пока мало данных — снимки делаются раз в 10 мин, график появится позже.</span>`;
    return;
  }
  const series = [
    { key: "battery",   label: "Батарея",        unit: "%",  color: "var(--good)" },
    { key: "voltage",   label: "Напряжение",     unit: " В", color: "var(--accent)" },
    { key: "chan_util", label: "Загрузка канала", unit: "%",  color: "var(--warn)" },
    { key: "snr",       label: "SNR",            unit: "",   color: "var(--teal)" },
  ];
  let html = "";
  for (const s of series) {
    const vals = data.map(d => d[s.key]);
    if (vals.filter(v => v != null).length < 2) continue;
    const last = [...vals].reverse().find(v => v != null);
    html += `<div class="nc-row"><div class="nc-head"><span>${s.label}</span>`
          + `<span class="nc-last">${_fmtNum(last)}${s.unit}</span></div>${_spark(vals, s.color)}</div>`;
  }
  box.className = "node-charts";
  box.innerHTML = html || `<span class="muted">Нет числовых рядов для графика.</span>`;
}

// ---- Traceroute history (route changes over time) ----
async function loadTraceHistory(nodeId) {
  const box = $("#profileTraceHist");
  if (!box) return;
  let data;
  try { data = await api(`/api/mesh/traceroute/history?dest=${encodeURIComponent(nodeId)}`); }
  catch { box.className = "trace-hist"; box.innerHTML = `<span class="muted">Не удалось загрузить.</span>`; return; }
  if (!Array.isArray(data) || !data.length) {
    box.className = "trace-hist";
    box.innerHTML = `<span class="muted">Пока нет записей — нажми «🛰 Traceroute», результат сохранится сюда.</span>`;
    return;
  }
  const nameOf = (id) => {
    const n = KNOWN_NODES.find(x => x.node_id === id);
    return n ? (n.short_name || n.long_name || id) : id;
  };
  const routeStr = (route) => (route && route.length)
    ? route.map(nameOf).map(escapeHtml).join(" → ")
    : "🎯 прямая видимость";
  let html = "";
  for (let i = 0; i < data.length; i++) {
    const e = data[i];
    const older = data[i + 1];   // chronologically previous (data is newest-first)
    const changed = older && JSON.stringify(e.route) !== JSON.stringify(older.route);
    const okIcon = e.ok ? "" : ` <span style="color:var(--danger)">⚠️ нет ответа</span>`;
    const tag = changed ? ` <span class="th-changed">маршрут изменился</span>` : "";
    html += `<div class="th-row"><div class="th-when">${relTime(e.time)}${okIcon}${tag}</div>`
          + `<div class="th-route">${routeStr(e.route)}</div></div>`;
  }
  box.className = "trace-hist";
  box.innerHTML = html;
}

// Click on backdrop or × closes the modal
document.getElementById("nodeProfile")?.addEventListener("click", (e) => {
  if (e.target.matches("[data-close]")) closeNodeProfile();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#nodeProfile").hidden) closeNodeProfile();
});

// ---------- Map (Leaflet) ----------
let MAP = null;
let MAP_MARKER_LAYER = null;
let MAP_COVERAGE_LAYER = null;   // translucent circles coloured by SNR (coverage)
let MAP_RETRIES = 0;

// Colour a node pin by how recently we heard it (freshness).
function _freshnessColor(lastHeard) {
  if (!lastHeard) return "#8a93a8";                 // unknown — grey
  const age = Date.now() / 1000 - Number(lastHeard);
  if (age < 7200)  return "#5eeb8e";                // < 2h — green
  if (age < 43200) return "#ffc24a";                // < 12h — amber
  return "#8a93a8";                                  // older — grey
}
// Colour a coverage circle by SNR (signal quality).
function _snrColor(snr) {
  if (snr == null) return "#8a93a8";
  const s = Number(snr);
  if (s >= 5)  return "#5eeb8e";   // strong
  if (s >= 0)  return "#ffc24a";   // ok
  return "#ff7a8a";                // weak / noisy
}
let TRACEROUTE_LAYER = null;          // Leaflet layer-group with the active traceroute drawing
let LAST_TRACEROUTE = null;           // Latest traceroute result (for "Show on map" button)

// ---------- Traceroute rendering ----------

/** Render a rich traceroute result into the given container. */
function renderTracerouteResult(out, r, nodeId, destNode) {
  if (!r || r.error) {
    const err = r?.error || "Неизвестная ошибка";
    const elapsed = r?.elapsed_seconds != null ? ` · ${r.elapsed_seconds.toFixed(1)} сек` : "";
    out.innerHTML = `<div class="muted" style="color: var(--danger)">⚠️ ${escapeHtml(err)}${elapsed}</div>`;
    LAST_TRACEROUTE = null;
    return;
  }

  LAST_TRACEROUTE = { ...r, destNodeId: nodeId, destNode };

  const fwd = r.hops_forward || r.hops || [];
  const back = r.hops_back || [];
  const elapsed = r.elapsed_seconds != null ? r.elapsed_seconds.toFixed(1) + ' сек' : '—';

  // Header with destination + elapsed time
  let html = `<div class="trace-head">
    <span>📡 Traceroute → <strong>${escapeHtml(r.from_name || nodeId)}</strong></span>
    <span class="trace-elapsed">⏱ ${elapsed}</span>
  </div>`;

  // Stats row (last-hop RSSI/SNR — what we measured on the response packet)
  const rssi = r.rx_rssi != null ? `${Math.round(r.rx_rssi)} dBm` : '—';
  const snr  = r.rx_snr  != null ? r.rx_snr.toFixed(1) : '—';
  html += `<div class="trace-stats">
    <span class="trace-stat">📶 RSSI <strong>${rssi}</strong></span>
    <span class="trace-stat">📊 SNR <strong>${snr}</strong></span>
    <span class="trace-stat">↯ Hops <strong>${fwd.length}</strong></span>
  </div>`;

  // Forward path
  html += `<div class="trace-section trace-section-fwd">
    <span class="trace-dir red">→</span> Туда (${fwd.length} ${fwd.length === 1 ? 'hop' : 'hops'})
  </div>`;
  if (fwd.length) {
    html += fwd.map((h, i) => {
      const s = r.snr_towards && r.snr_towards[i] != null
        ? `<span class="trace-snr">SNR ${r.snr_towards[i].toFixed(1)}</span>` : "";
      return `<div class="trace-hop">
        <span class="trace-num red">${i + 1}</span>
        <span class="trace-name">${escapeHtml(h.name || h.node_id)}</span>
        <span class="trace-id muted">${escapeHtml(h.node_id)}</span>
        ${s}
      </div>`;
    }).join("");
  } else {
    html += `<div class="muted trace-direct">🎯 Без ретрансляторов — узел в прямой видимости</div>`;
  }

  // Backward path
  if (back.length) {
    html += `<div class="trace-section trace-section-back">
      <span class="trace-dir blue">←</span> Обратно (${back.length} ${back.length === 1 ? 'hop' : 'hops'})
    </div>`;
    html += back.map((h, i) => {
      const s = r.snr_back && r.snr_back[i] != null
        ? `<span class="trace-snr">SNR ${r.snr_back[i].toFixed(1)}</span>` : "";
      return `<div class="trace-hop">
        <span class="trace-num blue">${i + 1}</span>
        <span class="trace-name">${escapeHtml(h.name || h.node_id)}</span>
        <span class="trace-id muted">${escapeHtml(h.node_id)}</span>
        ${s}
      </div>`;
    }).join("");
  } else if (fwd.length) {
    // We have forward hops but no back hops — possibly returned via different path
    // that the firmware didn't report. Don't show anything; not useful noise.
  }

  // Map button — only if we have at least one node with coordinates
  html += `<div class="trace-actions">
    <button class="ghost" id="traceMapBtn">🗺 Показать на карте</button>
  </div>`;

  out.innerHTML = html;

  // Wire the map button now that the HTML is in place
  const mapBtn = out.querySelector("#traceMapBtn");
  if (mapBtn) {
    mapBtn.onclick = () => showTraceOnMap(LAST_TRACEROUTE);
  }
}

// --- Geometry helpers for the map ---
function _bearing(lat1, lng1, lat2, lng2) {
  const φ1 = lat1 * Math.PI / 180, φ2 = lat2 * Math.PI / 180;
  const Δλ = (lng2 - lng1) * Math.PI / 180;
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}
function _mid(lat1, lng1, lat2, lng2) {
  return [(lat1 + lat2) / 2, (lng1 + lng2) / 2];
}
function _findCoords(nodeId, nodeNum) {
  for (const n of KNOWN_NODES) {
    if ((nodeId && n.node_id === nodeId) || (nodeNum != null && String(n.num) === String(nodeNum))) {
      if (n.latitude != null && n.longitude != null) {
        return {
          lat: n.latitude,
          lng: n.longitude,
          name: n.long_name || n.short_name || nodeId || `!${nodeNum}`,
        };
      }
      return null;
    }
  }
  return null;
}

/** Switch to the map tab and draw the forward (red) + return (blue) routes. */
async function showTraceOnMap(r) {
  if (!r) { toast("Нет данных traceroute для отрисовки", "err"); return; }
  closeNodeProfile();

  const tabBtn = document.querySelector('.tab-btn[data-tab="net"]');
  if (tabBtn) tabBtn.click();

  // Make sure we have fresh node positions — without this, _findCoords may
  // return null for nodes the user hasn't seen on the Home tab yet.
  try {
    const nodes = await api("/api/nodes");
    if (Array.isArray(nodes)) KNOWN_NODES = nodes;
  } catch (e) {
    log?.warn?.("Failed to refresh nodes before drawing trace:", e);
  }

  // Give Leaflet a moment if the map tab was previously hidden.
  setTimeout(() => {
    const map = ensureMap();
    if (!map) { toast("Карта ещё не загрузилась", "err"); return; }

    // Clean previous traceroute drawing (layer + legend)
    if (TRACEROUTE_LAYER) {
      if (TRACEROUTE_LAYER._legendControl) {
        try { map.removeControl(TRACEROUTE_LAYER._legendControl); } catch {}
      }
      TRACEROUTE_LAYER.remove();
      TRACEROUTE_LAYER = null;
    }
    TRACEROUTE_LAYER = L.layerGroup().addTo(map);

    // Resolve endpoint coordinates
    const meC   = r.me        ? _findCoords(r.me.node_id, r.me.num) : null;
    const destC = r.from_id   ? _findCoords(r.from_id, null) : null;

    if (!meC && !destC) {
      toast("Ни у тебя, ни у получателя нет координат — рисовать нечего 😕", "err");
      return;
    }
    if (!meC)   toast("У бота нет координат — стрелка пойдёт не от тебя", "");
    if (!destC) toast("У получателя нет координат — точка назначения пропадает", "");

    // Build forward node sequence: [me, ...hops_forward, dest]
    const fwdSeq = [];
    if (meC)   fwdSeq.push({ ...meC,   role: "me",    label: r.me?.name || "Я" });
    for (const h of (r.hops_forward || [])) {
      const c = _findCoords(h.node_id, h.num);
      if (c) fwdSeq.push({ ...c, role: "relay", label: h.name || h.node_id });
    }
    if (destC) fwdSeq.push({ ...destC, role: "dest",  label: r.from_name || r.from_id });

    // Backward: [dest, ...hops_back, me]
    const backSeq = [];
    if (destC) backSeq.push({ ...destC, role: "dest",  label: r.from_name || r.from_id });
    for (const h of (r.hops_back || [])) {
      const c = _findCoords(h.node_id, h.num);
      if (c) backSeq.push({ ...c, role: "relay", label: h.name || h.node_id });
    }
    if (meC)   backSeq.push({ ...meC,   role: "me",    label: r.me?.name || "Я" });

    const drawPath = (seq, color, offset, dashed) => {
      if (seq.length < 2) return;
      // Slight offset for the return path so the two lines don't overlap.
      const off = offset || 0;
      const pts = seq.map(s => [s.lat + off, s.lng + off]);
      L.polyline(pts, {
        color, weight: 3.5, opacity: 0.85,
        dashArray: dashed ? "8,8" : null,
      }).addTo(TRACEROUTE_LAYER);
      // Arrow at the midpoint of every segment
      for (let i = 0; i < seq.length - 1; i++) {
        const a = seq[i], b = seq[i + 1];
        const mid = _mid(a.lat + off, a.lng + off, b.lat + off, b.lng + off);
        const deg = _bearing(a.lat, a.lng, b.lat, b.lng);
        L.marker(mid, {
          interactive: false,
          icon: L.divIcon({
            className: "trace-arrow-marker",
            html: `<div class="trace-arrow-glyph" style="color:${color}; transform:rotate(${deg - 90}deg)">▶</div>`,
            iconSize: [18, 18],
            iconAnchor: [9, 9],
          }),
        }).addTo(TRACEROUTE_LAYER);
      }
    };

    drawPath(fwdSeq,  "#ff5a6a", 0,      false);  // forward — red, solid
    drawPath(backSeq, "#5a9eff", 0.0003, true);   // backward — blue, dashed, slightly offset

    // Numbered/role markers at every unique coordinate
    const placed = new Map();   // key -> { role, label }
    const rank = { me: 3, dest: 2, relay: 1 };
    const allNodes = [...fwdSeq, ...backSeq];
    for (const n of allNodes) {
      const k = `${n.lat.toFixed(5)},${n.lng.toFixed(5)}`;
      const prev = placed.get(k);
      if (!prev || rank[n.role] > rank[prev.role]) placed.set(k, n);
    }
    let relayCounter = 1;
    for (const [k, n] of placed) {
      const [lat, lng] = k.split(",").map(Number);
      let html, popup;
      if (n.role === "me") {
        html = `<div class="trace-node-pin me" title="Бот">🛰</div>`;
        popup = `<strong>🛰 ${escapeHtml(n.label)}</strong><br><span class="muted">это твоя нода</span>`;
      } else if (n.role === "dest") {
        html = `<div class="trace-node-pin dest" title="Цель">🎯</div>`;
        popup = `<strong>🎯 ${escapeHtml(n.label)}</strong><br><span class="muted">получатель</span>`;
      } else {
        html = `<div class="trace-node-pin relay">${relayCounter++}</div>`;
        popup = `<strong>${escapeHtml(n.label)}</strong><br><span class="muted">ретранслятор</span>`;
      }
      L.marker([lat, lng], {
        icon: L.divIcon({ className: "trace-node-marker", html, iconSize: [32, 32], iconAnchor: [16, 16] }),
      })
        .bindPopup(popup)
        .addTo(TRACEROUTE_LAYER);
    }

    // Legend
    const legend = L.control({ position: "topright" });
    legend.onAdd = () => {
      const div = L.DomUtil.create("div", "trace-legend");
      div.innerHTML = `
        <div><span class="trace-leg-swatch red"></span> Туда (запрос)</div>
        <div><span class="trace-leg-swatch blue"></span> Обратно (ответ)</div>
        <div><span class="trace-leg-pin">🛰</span> Бот · <span class="trace-leg-pin">🎯</span> Цель · <span class="trace-leg-pin relay">1</span> Ретранслятор</div>
      `;
      return div;
    };
    legend.addTo(map);
    // Remember it so we can remove on next traceroute
    TRACEROUTE_LAYER._legendControl = legend;

    // Fit to bounds
    const pts = allNodes.map(n => [n.lat, n.lng]);
    if (pts.length === 1) {
      map.setView(pts[0], 14);
    } else if (pts.length > 1) {
      map.fitBounds(pts, { padding: [50, 50] });
    }
  }, 350);
}



function ensureMap() {
  if (MAP) return MAP;
  if (typeof L === "undefined") return null;
  const el = document.getElementById("map");
  if (!el) return null;
  MAP = L.map(el).setView([55.75, 37.62], 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(MAP);
  MAP_MARKER_LAYER = L.layerGroup().addTo(MAP);
  MAP_COVERAGE_LAYER = L.layerGroup();   // added/removed via the «Покрытие» toggle
  return MAP;
}

document.getElementById("mapCoverage")?.addEventListener("change", (e) => {
  if (!MAP || !MAP_COVERAGE_LAYER) return;
  if (e.target.checked) MAP_COVERAGE_LAYER.addTo(MAP);
  else MAP.removeLayer(MAP_COVERAGE_LAYER);
});

async function refreshMap() {
  const map = ensureMap();
  const info = document.getElementById("mapInfo");
  if (!map) {
    if (MAP_RETRIES < 8) {
      // Leaflet is loaded with `defer` — on first paint it might not yet be ready.
      MAP_RETRIES += 1;
      if (info) info.textContent = `Загружаю карту… (попытка ${MAP_RETRIES})`;
      setTimeout(refreshMap, 600);
      return;
    }
    if (info) {
      info.textContent = "Leaflet не загрузился. Проверь, что у браузера есть интернет, "
        + "и попробуй открыть https://unpkg.com/leaflet@1.9.4/dist/leaflet.js в новой вкладке — должен открыться JS-файл.";
    }
    return;
  }
  MAP_RETRIES = 0;
  // Leaflet sometimes draws a blank canvas when initialized inside a hidden tab.
  // Force a redraw now that the panel is visible.
  setTimeout(() => map.invalidateSize(), 50);
  try {
    const nodes = await api("/api/nodes");
    KNOWN_NODES = Array.isArray(nodes) ? nodes : [];
    populateDestinationSelectors(KNOWN_NODES);
    MAP_MARKER_LAYER.clearLayers();
    const withPos = KNOWN_NODES.filter(n => Number.isFinite(n.latitude) && Number.isFinite(n.longitude));
    if (!withPos.length) {
      if (info) info.textContent = `Узлов с координатами: 0 (всего узлов: ${KNOWN_NODES.length})`;
      return;
    }
    if (MAP_COVERAGE_LAYER) MAP_COVERAGE_LAYER.clearLayers();
    const bounds = [];
    for (const n of withPos) {
      const lat = Number(n.latitude), lon = Number(n.longitude);
      bounds.push([lat, lon]);
      const long = n.long_name || n.node_id || "?";
      const short = n.short_name ? ` [${n.short_name}]` : "";
      const age = n.last_heard ? relTime(n.last_heard) : "—";
      const snr = n.snr != null ? `<br>SNR: ${Number(n.snr).toFixed(1)}` : "";
      const batt = n.battery_level != null ? `<br>🔋 ${n.battery_level}%` : "";
      const alt = Number.isFinite(n.altitude) ? `<br>Высота: ${Math.round(n.altitude)} м` : "";
      const popup = `<strong>${escapeHtml(long)}</strong>${escapeHtml(short)}<br>` +
                    `<span class="muted">${lat.toFixed(4)}, ${lon.toFixed(4)}</span><br>` +
                    `Слышали: ${age}${snr}${batt}${alt}` +
                    `<br><a href="#" class="map-profile-link" data-node-id="${escapeHtml(n.node_id)}">Открыть профиль →</a>`;
      // Colour-coded pin by freshness.
      const color = _freshnessColor(n.last_heard);
      const icon = L.divIcon({
        className: "node-map-marker",
        html: `<span class="nmm-dot" style="background:${color}"></span>`,
        iconSize: [18, 18], iconAnchor: [9, 9], popupAnchor: [0, -8],
      });
      L.marker([lat, lon], { icon }).addTo(MAP_MARKER_LAYER).bindPopup(popup);
      // Coverage circle coloured by SNR (lives in its own toggled layer).
      if (MAP_COVERAGE_LAYER) {
        L.circleMarker([lat, lon], {
          radius: 22, weight: 0, fillColor: _snrColor(n.snr), fillOpacity: 0.28,
        }).addTo(MAP_COVERAGE_LAYER);
      }
    }
    // Delegate clicks inside popups to the node-profile modal
    map.off("popupopen").on("popupopen", (e) => {
      const link = e.popup.getElement()?.querySelector(".map-profile-link");
      if (link) {
        link.addEventListener("click", (ev) => {
          ev.preventDefault();
          const id = link.dataset.nodeId;
          if (id) openNodeProfile(id);
        });
      }
    });
    if (bounds.length === 1) {
      map.setView(bounds[0], 12);
    } else {
      map.fitBounds(bounds, { padding: [30, 30], maxZoom: 13 });
    }
    if (info) info.textContent = `Узлов с координатами: ${withPos.length} из ${KNOWN_NODES.length}`;
  } catch (e) {
    if (info) info.textContent = "Не удалось загрузить узлы: " + e.message;
  }
}

document.getElementById("mapRefresh")?.addEventListener("click", refreshMap);

// ---------- RainViewer radar overlay ----------
// API: https://api.rainviewer.com/public/weather-maps.json — free, no API key.
// Returns past+nowcast radar tile URLs, served as XYZ tiles.

let RADAR_FRAMES = [];     // [{ path, time }] — past + nowcast frames
let RADAR_LAYER = null;    // currently shown Leaflet TileLayer
let RADAR_INDEX = 0;       // current frame index in RADAR_FRAMES
let RADAR_TIMER = null;    // animation interval
let RADAR_HOST = "https://tilecache.rainviewer.com";  // overwritten from API
let RADAR_PAST_COUNT = 0;  // how many of RADAR_FRAMES are past (rest = nowcast)

async function loadRadarFrames() {
  try {
    // Go through our own server (it proxies RainViewer) so the browser never
    // hits the CDN directly — works even when the ISP resets that connection.
    const data = await api("/api/radar/maps");
    if (data.error) throw new Error(data.error);
    if (data.host) RADAR_HOST = data.host;
    const past = data.radar?.past || [];
    const nowcast = data.radar?.nowcast || [];
    RADAR_FRAMES = past.concat(nowcast);
    RADAR_PAST_COUNT = past.length;
    RADAR_INDEX = Math.max(0, past.length - 1);   // start at "now" (latest past)
    return true;
  } catch (e) {
    toast("Не удалось загрузить радар: " + e.message, "err");
    return false;
  }
}

function showRadarFrame(idx) {
  if (!MAP || !RADAR_FRAMES[idx]) return;
  if (RADAR_LAYER) MAP.removeLayer(RADAR_LAYER);
  const f = RADAR_FRAMES[idx];
  // Tiles are proxied through our server: /api/radar/tile/{z}/{x}/{y}?path=…
  // maxNativeZoom caps requests at RainViewer's radar coverage (it returns a
  // "Zoom Level Not Supported" placeholder above that) — Leaflet upscales
  // lower-zoom tiles instead.
  const url = `/api/radar/tile/{z}/{x}/{y}?path=${encodeURIComponent(f.path)}&color=2`;
  RADAR_LAYER = L.tileLayer(url, {
    opacity: 0.7, tileSize: 256, zIndex: 400, maxNativeZoom: 8, maxZoom: 19,
  }).addTo(MAP);
  RADAR_INDEX = idx;
  const tEl = $("#radarTime");
  if (tEl) {
    tEl.hidden = false;
    const d = new Date(f.time * 1000);
    const isForecast = idx >= RADAR_PAST_COUNT;   // nowcast frames come after past
    tEl.textContent = (isForecast ? "🔮 " : "") + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
}

async function enableRadar() {
  if (!MAP) ensureMap();
  if (!MAP) { toast("Карта не готова — открой вкладку Карта и попробуй снова", "err"); return; }
  if (!RADAR_FRAMES.length) {
    if (!await loadRadarFrames()) return;
  }
  if (!RADAR_FRAMES.length) { toast("Радар: нет кадров", "err"); return; }
  $("#radarPlay").hidden = false;
  showRadarFrame(RADAR_INDEX);
  // Make sure Leaflet (re)requests tiles even if the map was just shown.
  setTimeout(() => MAP.invalidateSize(), 60);
  toast(`🌧 Радар включён · ${RADAR_FRAMES.length} кадров. Цветом — где идёт дождь (сейчас над твоим районом может быть чисто).`, "ok");
}

function disableRadar() {
  if (RADAR_LAYER) { MAP.removeLayer(RADAR_LAYER); RADAR_LAYER = null; }
  if (RADAR_TIMER) { clearInterval(RADAR_TIMER); RADAR_TIMER = null; $("#radarPlay").textContent = "▶"; }
  $("#radarPlay").hidden = true;
  $("#radarTime").hidden = true;
}

function toggleRadarPlay() {
  if (RADAR_TIMER) {
    clearInterval(RADAR_TIMER); RADAR_TIMER = null;
    $("#radarPlay").textContent = "▶";
    return;
  }
  RADAR_TIMER = setInterval(() => {
    const next = (RADAR_INDEX + 1) % RADAR_FRAMES.length;
    showRadarFrame(next);
  }, 600);
  $("#radarPlay").textContent = "⏸";
}

document.getElementById("radarEnabled")?.addEventListener("change", (e) => {
  if (e.target.checked) enableRadar(); else disableRadar();
});
document.getElementById("radarPlay")?.addEventListener("click", toggleRadarPlay);

// ---------- Weather alerts ----------
async function refreshAlertsUi() {
  try {
    const [cfg, status] = await Promise.all([
      api("/api/config"),
      api("/api/alerts/status").catch(() => ({})),
    ]);
    const a = cfg.alerts || {};
    $("#alertsEnabled").checked = !!a.enabled;
    $("#alertsThunderstorm").checked = a.thunderstorm_alerts !== false;
    $("#alertsWind").value = a.wind_threshold_ms ?? 15;
    $("#alertsRain").value = a.rain_prob_threshold ?? 80;
    $("#alertsFrost").value = a.frost_threshold_c ?? -5;
    $("#alertsHeat").value = a.heat_threshold_c ?? 30;
    $("#alertsFog").checked = a.fog_alerts !== false;
    $("#alertsIce").checked = a.ice_alerts !== false;
    $("#alertsFogVisibility").value = a.fog_visibility_m ?? 200;
    $("#alertsInterval").value = a.check_interval_minutes ?? 15;

    const last = status.last_check_ts;
    $("#alertsStatus").textContent = last
      ? `Последняя проверка: ${relTime(last)}.`
      : "Проверок ещё не было.";

    const hist = $("#alertsHistory");
    hist.innerHTML = "";
    const items = (status.history || []).slice().reverse().slice(0, 5);
    for (const h of items) {
      const div = document.createElement("div");
      div.className = "alert-item";
      div.innerHTML = `<div class="alert-time">${new Date(h.ts * 1000).toLocaleString()}</div>` +
                      escapeHtml(h.text || "");
      hist.appendChild(div);
    }
  } catch (e) { /* silent */ }
}

$("#alertsSave")?.addEventListener("click", async () => {
  const payload = {
    enabled: $("#alertsEnabled").checked,
    thunderstorm_alerts: $("#alertsThunderstorm").checked,
    wind_threshold_ms: parseFloat($("#alertsWind").value) || 15,
    rain_prob_threshold: parseInt($("#alertsRain").value, 10) || 80,
    frost_threshold_c: parseFloat($("#alertsFrost").value),
    heat_threshold_c: parseFloat($("#alertsHeat").value),
    fog_alerts: $("#alertsFog").checked,
    ice_alerts: $("#alertsIce").checked,
    fog_visibility_m: parseInt($("#alertsFogVisibility").value, 10) || 200,
    check_interval_minutes: parseInt($("#alertsInterval").value, 10) || 15,
  };
  try {
    await api("/api/config", { method: "POST", body: { alerts: payload } });
    toast("Настройки предупреждений сохранены", "ok");
    refreshAlertsUi();
  } catch (e) { toast(e.message, "err"); }
});

$("#alertsCheckNow")?.addEventListener("click", async () => {
  try {
    const r = await api("/api/alerts/check", { method: "POST" });
    if (r.count > 0) {
      toast(`Отправлено предупреждений: ${r.count}`, "ok");
    } else {
      toast("Условий для предупреждений сейчас нет", "ok");
    }
    refreshAlertsUi();
  } catch (e) { toast(e.message, "err"); }
});

// ---------- Mesh status ----------
async function refreshMeshStatus() {
  const el = $("#meshStatus");
  try {
    const s = await api("/api/mesh/status");
    if (s.connected) {
      el.className = "status ok";
      // Backend now returns nodes_online_2h (2-hour window); fall back to
      // legacy nodes_online_1h if the user hasn't restarted the service yet.
      const online = s.nodes_online_2h ?? s.nodes_online_1h ?? 0;
      el.textContent = `📡 узлов: ${s.nodes_known ?? 0} · онлайн: ${online}`;
    } else {
      el.className = "status";
      el.textContent = `📡 нет связи`;
    }
  } catch (e) {
    el.className = "status bad";
    el.textContent = "📡 ошибка";
  }
}

// ---------- Cities ----------
$("#cityBtn").addEventListener("click", searchCities);
$("#cityQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") searchCities(); });

async function searchCities() {
  const q = $("#cityQuery").value.trim();
  if (!q) return;
  const ul = $("#cityResults");
  ul.innerHTML = "<li class='muted'>Ищем…</li>";
  try {
    const items = await api(`/api/cities?q=${encodeURIComponent(q)}`);
    ul.innerHTML = "";
    if (!items.length) { ul.innerHTML = "<li class='muted'>Ничего не нашлось</li>"; return; }
    for (const c of items) {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${escapeHtml(c.name)}</strong>` +
        `<span class="meta">${escapeHtml([c.admin1, c.country].filter(Boolean).join(", "))} · ${c.latitude.toFixed(2)}, ${c.longitude.toFixed(2)}</span>`;
      li.addEventListener("click", () => pickCity(c));
      ul.appendChild(li);
    }
  } catch (e) { toast(e.message, "err"); ul.innerHTML = ""; }
}

async function pickCity(c) {
  CONFIG.location = {
    name: c.name, country: c.country,
    latitude: c.latitude, longitude: c.longitude,
    timezone: c.timezone || "auto",
  };
  await api("/api/config", { method: "POST", body: { location: CONFIG.location } });
  $("#cityResults").innerHTML = "";
  $("#cityQuery").value = "";
  renderCurrentCity();
  toast("Город сохранён", "ok");
}

function renderCurrentCity() {
  const loc = CONFIG.location || {};
  const el = $("#currentCity");
  if (loc.latitude == null) {
    el.textContent = "Город не выбран.";
  } else {
    el.innerHTML = `<strong>${escapeHtml(loc.name)}</strong>${loc.country ? ", " + escapeHtml(loc.country) : ""} ` +
      `<span class="muted">(${loc.latitude.toFixed(2)}, ${loc.longitude.toFixed(2)}, ${escapeHtml(loc.timezone || "auto")})</span>`;
  }
}

// ---------- Message style ----------
async function saveMessageStyle() {
  const message = {
    use_emojis: $("#useEmojis").checked,
    include_header: $("#includeHeader").checked,
  };
  let dmin = parseInt($("#cmdDelayMin").value, 10); if (!Number.isFinite(dmin)) dmin = 5;
  let dmax = parseInt($("#cmdDelayMax").value, 10); if (!Number.isFinite(dmax)) dmax = 10;
  if (dmax < dmin) dmax = dmin;
  const cmds = {
    enabled: $("#commandsEnabled").checked,
    reply_delay_min_s: Math.max(0, dmin),
    reply_delay_max_s: Math.max(0, dmax),
  };
  CONFIG = await api("/api/config", { method: "POST", body: { message, commands: cmds } });
  toast("Сохранено", "ok");
}
$("#useEmojis").addEventListener("change", saveMessageStyle);
$("#includeHeader").addEventListener("change", saveMessageStyle);
$("#commandsEnabled").addEventListener("change", saveMessageStyle);
$("#cmdDelayMin").addEventListener("change", saveMessageStyle);
$("#cmdDelayMax").addEventListener("change", saveMessageStyle);

// ---------- Mesh form ----------
function updateConnectionFields() {
  const type = $("#connectionType").value;
  $("#serialFields").classList.toggle("hidden", type !== "serial");
  $("#tcpFields").classList.toggle("hidden", type !== "tcp");
}
$("#connectionType").addEventListener("change", updateConnectionFields);

$("#saveMesh").addEventListener("click", async () => {
  const rawDelay = parseFloat($("#chunkDelay").value);
  const chunkDelay = Number.isFinite(rawDelay) ? Math.max(0, Math.min(120, rawDelay)) : 10;
  const mesh = {
    connection_type: $("#connectionType").value,
    device_path: $("#devicePath").value.trim() || "auto",
    tcp_host: $("#tcpHost").value.trim(),
    tcp_port: parseInt($("#tcpPort").value || "4403", 10) || 4403,
    channel_index: parseInt($("#channelIndex").value || "0", 10),
    chunk_delay: chunkDelay,
    destination: "broadcast",
  };
  CONFIG = await api("/api/config", { method: "POST", body: { mesh } });
  toast("Сохранено", "ok");
  refreshMeshStatus();
});

$("#testConnect").addEventListener("click", async () => {
  toast("Подключаюсь к Heltec…");
  try {
    const s = await api("/api/mesh/connect", { method: "POST" });
    if (s.connected) {
      const where = s.connection_type === "tcp"
        ? `${s.tcp_host}:${s.tcp_port}`
        : s.resolved_path;
      toast(`Связь есть · ${where} · узлов: ${s.nodes_known ?? 0}`, "ok");
    } else {
      toast(`Не подключено: ${s.error || "устройство не найдено"}`, "err");
    }
  } catch (e) { toast(e.message, "err"); }
  refreshMeshStatus();
});

$("#disconnectMesh").addEventListener("click", async () => {
  if (!confirm("Отключить связь с Heltec? Следующая отправка/healthcheck снова поднимет соединение автоматически.")) return;
  const btn = $("#disconnectMesh");
  btn.disabled = true;
  try {
    await api("/api/mesh/disconnect", { method: "POST" });
    toast("Связь с Heltec закрыта", "ok");
  } catch (e) {
    toast("Ошибка: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
  refreshMeshStatus();
});

// ---------- Heltec device settings modal ----------

let HELTEC_INFO = null;

async function openHeltecModal() {
  const modal = document.getElementById("heltecModal");
  modal.hidden = false;
  document.getElementById("heltecLoading").hidden = false;
  document.getElementById("heltecLoading").textContent = "Запрашиваю настройки у ноды…";
  document.getElementById("heltecForm").hidden = true;
  await loadHeltecInfo();
}

function closeHeltecModal() {
  document.getElementById("heltecModal").hidden = true;
}

async function loadHeltecInfo() {
  try {
    const info = await api("/api/heltec/info");
    HELTEC_INFO = info;
    if (!info.connected) {
      document.getElementById("heltecLoading").textContent =
        "Нет связи с Heltec. Открой «Проверить связь» и попробуй ещё раз.";
      return;
    }
    fillHeltecForm(info);
    document.getElementById("heltecLoading").hidden = true;
    document.getElementById("heltecForm").hidden = false;
  } catch (e) {
    document.getElementById("heltecLoading").textContent = "Ошибка: " + e.message;
  }
}

function fillHeltecForm(info) {
  const regionSel = document.getElementById("heltecRegion");
  regionSel.innerHTML = "";
  for (const r of info.regions || []) {
    const opt = document.createElement("option");
    opt.value = r.value;
    opt.textContent = r.label;
    if (r.value === info.region) opt.selected = true;
    regionSel.appendChild(opt);
  }

  const roleSel = document.getElementById("heltecRole");
  roleSel.innerHTML = "";
  for (const r of info.roles || []) {
    const opt = document.createElement("option");
    opt.value = r.value;
    opt.textContent = r.label;
    if (r.value === info.role) opt.selected = true;
    roleSel.appendChild(opt);
  }

  const modemSel = document.getElementById("heltecModemPreset");
  modemSel.innerHTML = "";
  for (const m of info.modem_presets || []) {
    const opt = document.createElement("option");
    opt.value = m.value;
    opt.textContent = m.label;
    if (m.value === info.modem_preset) opt.selected = true;
    modemSel.appendChild(opt);
  }

  document.getElementById("heltecLongName").value = info.long_name || "";
  document.getElementById("heltecShortName").value = info.short_name || "";
  document.getElementById("heltecHopLimit").value = info.hop_limit ?? 3;
  document.getElementById("heltecTxPower").value = info.tx_power ?? 0;
  document.getElementById("heltecTxEnabled").checked = info.tx_enabled !== false;

  document.getElementById("heltecFw").textContent = info.firmware_version || "—";
  document.getElementById("heltecHw").textContent = info.hw_model || "—";
  document.getElementById("heltecNodeId").textContent =
    info.my_node_num != null ? `!${info.my_node_num.toString(16).padStart(8, "0")}` : "—";
}

async function saveHeltecSettings() {
  if (!HELTEC_INFO) return;
  const payload = {};
  const longName  = document.getElementById("heltecLongName").value.trim();
  const shortName = document.getElementById("heltecShortName").value.trim();
  if (longName  !== (HELTEC_INFO.long_name  || "")) payload.long_name  = longName;
  if (shortName !== (HELTEC_INFO.short_name || "")) payload.short_name = shortName;

  const region = parseInt(document.getElementById("heltecRegion").value, 10);
  if (region !== HELTEC_INFO.region) payload.region = region;

  const role = parseInt(document.getElementById("heltecRole").value, 10);
  if (role !== HELTEC_INFO.role) payload.role = role;

  const modem = parseInt(document.getElementById("heltecModemPreset").value, 10);
  if (modem !== HELTEC_INFO.modem_preset) payload.modem_preset = modem;

  const hop = Math.max(1, Math.min(7, parseInt(document.getElementById("heltecHopLimit").value, 10) || 3));
  if (hop !== HELTEC_INFO.hop_limit) payload.hop_limit = hop;

  const txp = Math.max(0, Math.min(30, parseInt(document.getElementById("heltecTxPower").value, 10) || 0));
  if (txp !== HELTEC_INFO.tx_power) payload.tx_power = txp;

  const txEn = document.getElementById("heltecTxEnabled").checked;
  if (txEn !== (HELTEC_INFO.tx_enabled !== false)) payload.tx_enabled = txEn;

  if (Object.keys(payload).length === 0) {
    toast("Нечего применять — ничего не изменилось");
    return;
  }

  const btn = document.getElementById("heltecSave");
  btn.disabled = true;
  btn.textContent = "Применяю…";
  try {
    const res = await api("/api/heltec/settings", { method: "POST", body: payload });
    toast("Применено: " + (res.applied || []).join(", "), "ok");
    await loadHeltecInfo();
  } catch (e) {
    toast("Ошибка: " + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "💾 Применить";
  }
}

async function rebootHeltec() {
  if (!confirm("Перезагрузить ноду Heltec? Связь временно прервётся на ~30 секунд.")) return;
  try {
    await api("/api/heltec/reboot", { method: "POST", body: { delay: 5 } });
    toast("Команда reboot отправлена. Через 5 секунд устройство перезагрузится.", "ok");
    closeHeltecModal();
  } catch (e) {
    toast("Ошибка: " + e.message, "err");
  }
}

document.getElementById("heltecSettingsBtn")?.addEventListener("click", openHeltecModal);
document.getElementById("heltecRefresh")?.addEventListener("click", loadHeltecInfo);
document.getElementById("heltecSave")?.addEventListener("click", saveHeltecSettings);
document.getElementById("heltecReboot")?.addEventListener("click", rebootHeltec);
document.getElementById("heltecModal")?.addEventListener("click", (e) => {
  if (e.target.dataset.close !== undefined) closeHeltecModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("heltecModal").hidden) closeHeltecModal();
});

// ---------- Field/day chip helpers ----------
function buildFieldChips(container, selected) {
  container.innerHTML = "";
  for (const f of ALL_FIELDS) {
    const id = `f_${f.key}_${Math.random().toString(36).slice(2, 7)}`;
    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = f.key; cb.id = id;
    cb.checked = selected.includes(f.key);
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + f.label));
    if (cb.checked) lbl.classList.add("checked");
    cb.addEventListener("change", () => lbl.classList.toggle("checked", cb.checked));
    container.appendChild(lbl);
  }
}
function readFields(container) {
  return $$('input[type="checkbox"]', container).filter(c => c.checked).map(c => c.value);
}

function buildDayButtons(container, selected) {
  container.innerHTML = "";
  for (const d of DAYS) {
    const btn = document.createElement("button");
    btn.type = "button"; btn.textContent = d.t; btn.dataset.k = d.k;
    if (selected.includes(d.k)) btn.classList.add("on");
    btn.addEventListener("click", () => btn.classList.toggle("on"));
    container.appendChild(btn);
  }
}
function readDays(container) {
  return $$("button.on", container).map(b => b.dataset.k);
}

// ---------- Slots ----------
async function refreshSlots() {
  const slots = await api("/api/schedules");
  const jobs = await api("/api/scheduler/jobs");
  const jobMap = Object.fromEntries(jobs.map(j => [j.id, j]));
  const wrap = $("#slots");
  wrap.innerHTML = "";
  if (!slots.length) {
    wrap.innerHTML = "<div class='muted'>Слотов нет. Добавь хотя бы один ниже.</div>";
    return;
  }
  const tpl = $("#slotTemplate");
  for (const s of slots) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = s.id;
    $(".enabled", node).checked = !!s.enabled;
    $(".time", node).value = s.time || "12:00";
    $(".tz", node).value = s.timezone || "Europe/Moscow";
    // Destination select — fill with known nodes, then restore slot's value.
    populateDestinationSelectors(KNOWN_NODES);
    const destSel = $(".dest", node);
    // Make sure the saved value is selectable even if the node hasn't been
    // heard since boot (e.g. configured DM target that's currently offline).
    const desired = s.destination || "broadcast";
    if (desired !== "broadcast" && !Array.from(destSel.options).some(o => o.value === desired)) {
      const opt = document.createElement("option");
      opt.value = desired;
      opt.textContent = `👤 ${desired} (вне связи)`;
      destSel.appendChild(opt);
    }
    destSel.value = desired;
    buildDayButtons($(".dow", node), s.days || DAYS.map(d => d.k));
    buildFieldChips($(".fields", node), s.fields || []);
    const job = jobMap[`slot-${s.id}`];
    if (job?.next_run) {
      const dt = new Date(job.next_run);
      $(".next-run", node).textContent = `Следующий запуск: ${dt.toLocaleString()}`;
    } else if (s.enabled) {
      $(".next-run", node).textContent = "Следующий запуск: —";
    } else {
      $(".next-run", node).textContent = "Слот выключен";
    }
    const save = async () => {
      const payload = {
        enabled: $(".enabled", node).checked,
        time: $(".time", node).value,
        timezone: $(".tz", node).value,
        days: readDays($(".dow", node)),
        fields: readFields($(".fields", node)),
        destination: $(".dest", node).value || "broadcast",
      };
      try {
        await api(`/api/schedules/${s.id}`, { method: "PATCH", body: payload });
        toast("Слот обновлён", "ok");
        refreshSlots();
      } catch (e) { toast(e.message, "err"); }
    };
    $(".enabled", node).addEventListener("change", save);
    $(".time", node).addEventListener("change", save);
    $(".tz", node).addEventListener("change", save);
    $(".dow", node).addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") save(); });
    $(".fields", node).addEventListener("change", save);
    $(".dest", node).addEventListener("change", save);
    $(".run-now", node).addEventListener("click", async () => {
      const btn = $(".run-now", node);
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const res = await api(`/api/schedules/${s.id}/run`, { method: "POST" });
        const parts = res.chunks > 1 ? `, частей: ${res.chunks}` : "";
        toast(`Слот отправлен · ${res.chars} симв.${parts}`, "ok");
      } catch (e) {
        toast(e.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "▶";
      }
    });
    $(".delete", node).addEventListener("click", async () => {
      if (!confirm(`Удалить слот ${s.time}?`)) return;
      await api(`/api/schedules/${s.id}`, { method: "DELETE" });
      refreshSlots();
    });
    wrap.appendChild(node);
  }
}

$("#createSlot").addEventListener("click", async () => {
  const payload = {
    time: $("#newTime").value || "12:00",
    timezone: $("#newTz").value,
    days: readDays($("#newDays")),
    fields: readFields($("#newFields")),
    enabled: true,
  };
  if (!payload.fields.length) { toast("Выбери хотя бы одно поле", "err"); return; }
  if (!payload.days.length) { toast("Выбери хотя бы один день", "err"); return; }
  try {
    await api("/api/schedules", { method: "POST", body: payload });
    toast("Слот создан", "ok");
    refreshSlots();
  } catch (e) { toast(e.message, "err"); }
});

// ---------- Manual send ----------
$("#previewBtn").addEventListener("click", async () => {
  try {
    const res = await api("/api/preview", { method: "POST", body: { fields: readFields($("#manualFields")) } });
    $("#previewBox").textContent = res.text;
  } catch (e) { toast(e.message, "err"); }
});

$("#sendBtn").addEventListener("click", async () => {
  if (!confirm("Отправить погоду в mesh-сеть прямо сейчас?")) return;
  try {
    const res = await api("/api/send", { method: "POST", body: { fields: readFields($("#manualFields")) } });
    $("#previewBox").textContent = res.text;
    const parts = res.chunks > 1 ? `, частей: ${res.chunks}` : "";
    toast(`Отправлено · ${res.chars} симв.${parts}`, "ok");
  } catch (e) { toast(e.message, "err"); }
});

// ---------- Chat: state ----------
let LAST_MSG_ID = 0;
let UNREAD = 0;
let ALL_MESSAGES = [];      // full log, kept in memory for filtering
let SELECTED_CONV = null;   // currently displayed conversation key
let CONVS = new Map();      // convKey -> {key, title, kind, channel, peerId, lastMsg, unread, icon}
let KNOWN_CHANNELS = [];    // [{index, name, role}] — pre-populates the sidebar even with no messages

// Internal IDs of outgoing messages whose delivery status is still pending
// (i.e. "enroute" — no ACK yet). pollChat sends these to the backend so it
// can return up-to-date statuses without us having to re-fetch the whole log.
const PENDING_OUT_IDS = new Set();
// Cap how long we keep checking a single message — broadcasts never get ACK
// so we'd keep polling forever otherwise. After 2 min, give up on a message.
const PENDING_TTL_MS = 2 * 60 * 1000;
const PENDING_START_TS = new Map();

function trackPending(m) {
  if (m && !m.incoming && !m.is_reaction
      && m.delivery_status === "enroute" && m.id) {
    PENDING_OUT_IDS.add(m.id);
    PENDING_START_TS.set(m.id, Date.now());
  }
}
function untrackPending(id) {
  PENDING_OUT_IDS.delete(id);
  PENDING_START_TS.delete(id);
}
function pruneStalePending() {
  const now = Date.now();
  for (const [id, ts] of PENDING_START_TS) {
    if (now - ts > PENDING_TTL_MS) untrackPending(id);
  }
}

// Render delivery indicator HTML for an outgoing message. Returns "" for
// incoming, reactions, or messages without a known status.
function deliveryIconHtml(m) {
  if (!m || m.incoming || m.is_reaction) return "";
  const s = m.delivery_status;
  if (!s) return "";
  let icon, label, cls;
  if (s === "delivered") {
    const hops = m.delivery_hops;
    icon = "✓✓";
    label = hops != null
      ? `Доставлено · ACK через ${hops} hop${hops === 1 ? "" : "s"}`
      : "Доставлено · ACK получен";
    cls = "delivered";
  } else if (s === "error") {
    icon = "⚠";
    label = "Ошибка доставки";
    cls = "error";
  } else if (s === "enroute") {
    icon = "☁";
    label = "Отправлено в эфир, ждём ACK";
    cls = "enroute";
  } else if (s === "queued") {
    icon = "⏳";
    label = "В очереди";
    cls = "queued";
  } else {
    return "";
  }
  return `<span class="msg-status ${cls}" title="${label}">${icon}</span>`;
}

// Re-render the delivery indicator of a single visible message bubble.
function updateMessageStatusInDom(m) {
  if (!m || m.incoming || m.is_reaction) return;
  const log = $("#chatLog");
  if (!log) return;
  const bubble = m.msg_id
    ? log.querySelector(`[data-mesh-id="${m.msg_id}"]`)
    : log.querySelector(`[data-row-id="${m.id}"]`);
  if (!bubble) return;
  const meta = bubble.querySelector(".meta");
  if (!meta) return;
  // Remove existing status node if any
  meta.querySelector(".msg-status")?.remove();
  // Insert fresh one right before the action buttons (or at the end of meta)
  const html = deliveryIconHtml(m);
  if (!html) return;
  const tmp = document.createElement("template");
  tmp.innerHTML = html.trim();
  const node = tmp.content.firstChild;
  const insertBefore = meta.querySelector(".reply-btn") || null;
  meta.insertBefore(node, insertBefore);
}

const BROADCAST_TO = new Set(["^all", "all", "", null, undefined]);

function isBroadcast(m) { return BROADCAST_TO.has(m.to_id); }

function conversationKey(m) {
  // Broadcasts live in a per-channel bucket. DMs are keyed by the *other* party.
  if (isBroadcast(m)) return `ch:${m.channel ?? 0}`;
  const other = m.incoming ? m.from_id : m.to_id;
  return `dm:${other}`;
}

function lookupNodeName(nodeId) {
  if (!nodeId) return String(nodeId || "?");
  const n = KNOWN_NODES.find(x => x.node_id === nodeId || String(x.num) === String(nodeId));
  if (n) return n.long_name || n.short_name || nodeId;
  return String(nodeId);
}

function conversationLabel(key) {
  if (key.startsWith("ch:")) {
    const idx = parseInt(key.slice(3), 10);
    return `📢 ${channelDisplayName(idx)}`;
  }
  if (key.startsWith("dm:")) {
    const id = key.slice(3);
    return `👤 ${lookupNodeName(id)}`;
  }
  return key;
}

function rebuildConversations() {
  const map = new Map();

  // 1. Pre-populate with every channel configured on the Heltec — that way the
  //    sidebar shows all channels even if no message has arrived for them yet.
  for (const ch of KNOWN_CHANNELS) {
    const key = `ch:${ch.index}`;
    map.set(key, {
      key,
      kind: "channel",
      channel: ch.index,
      channelName: ch.name,
      peerId: null,
      lastMsg: null,
      unread: 0,
    });
  }

  // 2. Walk messages, creating DM convs and updating lastMsg of channels.
  for (const m of ALL_MESSAGES) {
    const key = conversationKey(m);
    let conv = map.get(key);
    if (!conv) {
      conv = {
        key,
        kind: key.startsWith("ch:") ? "channel" : "dm",
        channel: key.startsWith("ch:") ? parseInt(key.slice(3), 10) : null,
        peerId: key.startsWith("dm:") ? key.slice(3) : null,
        lastMsg: m,
        unread: 0,
      };
      map.set(key, conv);
    }
    if (m.id > (conv.lastMsg?.id || 0)) conv.lastMsg = m;
  }
  // 3. Carry over unread counters from the previous CONVS map.
  for (const [key, conv] of map) {
    const prev = CONVS.get(key);
    if (prev) conv.unread = prev.unread || 0;
  }
  CONVS = map;
}

function channelDisplayName(idx) {
  const ch = KNOWN_CHANNELS.find(c => c.index === idx);
  return ch?.name || `Канал ${idx}`;
}

function renderConvList() {
  const wrap = $("#convList");
  wrap.innerHTML = "";
  const convs = [...CONVS.values()].sort((a, b) => {
    // Channels with messages or DMs sort by recency. Empty channels go below.
    const aId = a.lastMsg?.id || 0;
    const bId = b.lastMsg?.id || 0;
    if (aId !== bId) return bId - aId;
    // Tie-breaker: channel index ascending
    return (a.channel ?? 99) - (b.channel ?? 99);
  });
  if (!convs.length) {
    wrap.innerHTML = "<div class='muted' style='padding: 12px;'>Чатов пока нет.</div>";
    return;
  }
  for (const conv of convs) {
    const div = document.createElement("div");
    div.className = "conv-item";
    if (conv.key === SELECTED_CONV) div.classList.add("active");
    const icon = conv.kind === "channel" ? "📢" : "👤";
    const title = conv.kind === "channel"
      ? channelDisplayName(conv.channel)
      : lookupNodeName(conv.peerId);
    const preview = conv.lastMsg
      ? (conv.lastMsg.from_name && !conv.lastMsg.incoming ? "Я: " : "")
        + (conv.lastMsg.text || "").slice(0, 60)
      : (conv.kind === "channel" ? "Сообщений ещё нет" : "");
    const time = conv.lastMsg
      ? new Date(conv.lastMsg.time * 1000).toLocaleTimeString().slice(0, 5)
      : "";
    const unreadHtml = conv.unread > 0
      ? `<div class="conv-unread">${conv.unread}</div>`
      : "";
    div.innerHTML =
      `<div class="conv-icon">${icon}</div>` +
      `<div class="conv-body">` +
        `<div class="conv-name">${escapeHtml(title)}</div>` +
        `<div class="conv-preview">${escapeHtml(preview)}</div>` +
      `</div>` +
      `<div class="conv-meta"><span>${time}</span>${unreadHtml}</div>`;
    div.addEventListener("click", () => selectConversation(conv.key));
    wrap.appendChild(div);
  }
}

function selectConversation(key) {
  SELECTED_CONV = key;
  const conv = CONVS.get(key);
  if (conv) {
    conv.unread = 0;
  }
  $("#chatLayout").classList.add("show-main");
  $("#chatTitle").textContent = conversationLabel(key);
  $("#chatInput").disabled = false;
  $("#chatSend").disabled = false;
  $("#chatInput").placeholder = conv?.kind === "dm"
    ? `Написать ${lookupNodeName(conv.peerId)}…`
    : "Написать в канал…";
  PENDING_REACTIONS.clear();
  renderConvList();
  renderChatLog();
}

// Maximum messages rendered into the DOM per conversation. Anything older is
// kept in ALL_MESSAGES (and SQLite) but not painted. Keeps the UI snappy on
// long-running chats — picking a chat used to lag for seconds with ~200 msgs.
const CHAT_RENDER_LIMIT = 200;

// Build the sender-name span; clickable (→ profile / DM) for incoming nodes.
function fromSpanHtml(m) {
  const name = escapeHtml(m.from_name || "?");
  const fid = m.from_id;
  if (m.incoming && fid && fid !== "me") {
    return `<span class="from from-clickable" data-from-id="${escapeHtml(String(fid))}" title="Профиль · написать DM">${name}</span>`;
  }
  return `<span class="from">${name}</span>`;
}

// Open a sender's profile, or offer a direct DM if they're not in the node table.
function openSenderInfo(fromId) {
  if (!fromId || fromId === "me") return;
  const n = KNOWN_NODES.find(x => x.node_id === fromId || String(x.num) === String(fromId));
  if (n) { openNodeProfile(n.node_id); return; }
  if (confirm("Этого узла нет в текущей таблице нод. Открыть личную переписку?")) {
    ensureDmConversation(fromId);
    const tabBtn = document.querySelector('.tab-btn[data-tab="chat"]');
    if (tabBtn) tabBtn.click();
    setTimeout(() => selectConversation(`dm:${fromId}`), 80);
  }
}

function _buildMessageElement(m, byMsgId, elements) {
  // Reaction packets aren't rendered as separate bubbles — they become chips
  // on their parent message. If the parent is already in our batch, attach
  // immediately; otherwise stash so a later parent in this same batch picks
  // it up. (Cross-batch reactions are handled by appendChatMessage's PENDING.)
  if (m.is_reaction && m.reply_to) {
    const parent = elements.get(String(m.reply_to));
    if (parent) {
      applyReactionChip(parent, m);
      return null;
    }
    if (!PENDING_REACTIONS.has(m.reply_to)) PENDING_REACTIONS.set(m.reply_to, []);
    PENDING_REACTIONS.get(m.reply_to).push(m);
    return null;
  }

  const div = document.createElement("div");
  div.className = "chat-msg" + (m.incoming ? "" : " outgoing");
  if (m.msg_id) {
    div.dataset.meshId = String(m.msg_id);
    elements.set(String(m.msg_id), div);
  }
  if (m.id) div.dataset.rowId = String(m.id);

  // Build reply quote from the in-memory parent (no DOM lookup at all).
  let replyHtml = "";
  if (m.reply_to && !m.is_reaction) {
    const parent = byMsgId.get(String(m.reply_to));
    const name = parent?.from_name || "?";
    const text = parent ? (parent.text || "") : "(сообщение недоступно)";
    const preview = text.length > 80 ? text.slice(0, 80) + "…" : text;
    replyHtml =
      `<div class="reply-quote" data-target="${m.reply_to}">` +
        `<span class="reply-quote-arrow">↪</span> ` +
        `<strong class="reply-quote-name">${escapeHtml(name)}</strong>: ` +
        `<span class="reply-quote-text">${escapeHtml(preview)}</span>` +
      `</div>`;
  }

  const t = new Date(m.time * 1000).toLocaleTimeString();
  const status = deliveryIconHtml(m);
  const actions = m.msg_id
    ? `<button class="reply-btn" title="Ответить" data-msg-id="${m.msg_id}">↩</button>` +
      `<button class="react-btn" title="Поставить реакцию" data-msg-id="${m.msg_id}">+</button>`
    : "";

  div.innerHTML =
    `<div class="meta">` +
      fromSpanHtml(m) +
      `<span class="ch">ch${m.channel ?? 0}</span>` +
      `<span>${t}</span>` +
      status +
      actions +
    `</div>` +
    replyHtml +
    `<div class="text">${linkify(m.text)}</div>` +
    buildRfMeta(m);

  // Apply any reactions that arrived earlier in this batch waiting for parent.
  if (m.msg_id && PENDING_REACTIONS.has(m.msg_id)) {
    for (const r of PENDING_REACTIONS.get(m.msg_id)) applyReactionChip(div, r);
    PENDING_REACTIONS.delete(m.msg_id);
  }

  return div;
}

function renderChatLog() {
  const log = $("#chatLog");
  log.innerHTML = "";
  if (!SELECTED_CONV) {
    log.innerHTML = "<div class='chat-empty muted'>Выберите канал или собеседника в списке слева.</div>";
    return;
  }
  let messages = ALL_MESSAGES.filter(m => conversationKey(m) === SELECTED_CONV);
  if (!messages.length) {
    log.innerHTML = "<div class='chat-empty muted'>В этом чате ещё нет сообщений.</div>";
    return;
  }
  const truncated = messages.length > CHAT_RENDER_LIMIT;
  if (truncated) messages = messages.slice(-CHAT_RENDER_LIMIT);

  // Index ALL conversation messages (not just rendered slice) so reply quotes
  // can still show preview text when parent is older than the visible window.
  const byMsgId = new Map();
  for (const m of ALL_MESSAGES) {
    if (m.msg_id && conversationKey(m) === SELECTED_CONV) {
      byMsgId.set(String(m.msg_id), m);
    }
  }

  // Build everything in a DocumentFragment — one reflow at the end.
  PENDING_REACTIONS.clear();
  const elements = new Map();
  const fragment = document.createDocumentFragment();
  if (truncated) {
    const hint = document.createElement("div");
    hint.className = "chat-empty muted";
    hint.style.padding = "6px 10px";
    hint.style.fontSize = "0.78rem";
    hint.textContent = `Показаны последние ${CHAT_RENDER_LIMIT} сообщений из ${ALL_MESSAGES.filter(m => conversationKey(m) === SELECTED_CONV).length}.`;
    fragment.appendChild(hint);
  }
  for (const m of messages) {
    const el = _buildMessageElement(m, byMsgId, elements);
    if (el) fragment.appendChild(el);
  }
  log.appendChild(fragment);
  // Use rAF so the browser paints first, then scrolls — feels snappier.
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}

document.getElementById("chatBack")?.addEventListener("click", () => {
  $("#chatLayout").classList.remove("show-main");
});

// Map of mesh msg_id -> reactions {emoji: {count, names: Set}}, used when a
// reaction arrives BEFORE its parent message has been rendered (rare but possible
// with out-of-order delivery or initial buffer fetch).
const PENDING_REACTIONS = new Map();

// Build the small "↪ name: preview" quote bubble shown above replies.
function buildReplyQuote(m, log) {
  if (!m.reply_to || m.is_reaction) return "";
  const parent = log.querySelector(`[data-mesh-id="${m.reply_to}"]`);
  let name = "?", text = "";
  if (parent) {
    name = parent.querySelector(".meta .from")?.textContent || "?";
    text = parent.querySelector(".text")?.textContent || "";
  } else {
    text = "(сообщение недоступно)";
  }
  const preview = text.length > 80 ? text.slice(0, 80) + "…" : text;
  return `<div class="reply-quote" data-target="${m.reply_to}">` +
           `<span class="reply-quote-arrow">↪</span> ` +
           `<strong class="reply-quote-name">${escapeHtml(name)}</strong>: ` +
           `<span class="reply-quote-text">${escapeHtml(preview)}</span>` +
         `</div>`;
}

function applyReactionChip(parentDiv, reaction) {
  let bar = parentDiv.querySelector(".reactions");
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "reactions";
    parentDiv.appendChild(bar);
  }
  const emoji = reaction.text;
  const fromName = reaction.from_name || "?";
  let chip = Array.from(bar.children).find(c => c.dataset.emoji === emoji);
  if (chip) {
    const cnt = (parseInt(chip.dataset.count, 10) || 0) + 1;
    chip.dataset.count = String(cnt);
    const names = new Set((chip.dataset.names || "").split("\n").filter(Boolean));
    names.add(fromName);
    chip.dataset.names = [...names].join("\n");
    chip.title = [...names].join(", ");
    chip.querySelector(".count").textContent = cnt;
    chip.classList.toggle("multi", cnt > 1);
  } else {
    chip = document.createElement("span");
    chip.className = "reaction-chip" + (reaction.incoming ? "" : " mine");
    chip.dataset.emoji = emoji;
    chip.dataset.count = "1";
    chip.dataset.names = fromName;
    chip.title = fromName;
    chip.innerHTML =
      `<span class="emoji">${escapeHtml(emoji)}</span>` +
      `<span class="count">1</span>`;
    bar.appendChild(chip);
  }
}

function appendChatMessage(m) {
  const log = $("#chatLog");
  // remove placeholder once we have any messages
  const empty = log.querySelector(".chat-empty");
  if (empty) empty.remove();

  // ----- reaction handling -----
  if (m.is_reaction && m.reply_to) {
    const parent = log.querySelector(`[data-mesh-id="${m.reply_to}"]`);
    if (parent) {
      applyReactionChip(parent, m);
      return;
    }
    // Parent not in DOM yet — stash and retry when parent appears.
    if (!PENDING_REACTIONS.has(m.reply_to)) PENDING_REACTIONS.set(m.reply_to, []);
    PENDING_REACTIONS.get(m.reply_to).push(m);
    // Don't render this reaction as a standalone message; it'll attach later.
    return;
  }

  // ----- regular message -----
  const div = document.createElement("div");
  div.className = "chat-msg" + (m.incoming ? "" : " outgoing");
  if (m.msg_id) div.dataset.meshId = m.msg_id;
  if (m.id) div.dataset.rowId = String(m.id);
  const t = new Date(m.time * 1000).toLocaleTimeString();
  const status = deliveryIconHtml(m);
  // Action buttons: reply + reaction. Both need a real mesh msg_id to target.
  const actions = m.msg_id
    ? `<button class="reply-btn" title="Ответить" data-msg-id="${m.msg_id}">↩</button>` +
      `<button class="react-btn" title="Поставить реакцию" data-msg-id="${m.msg_id}">+</button>`
    : "";
  div.innerHTML =
    `<div class="meta">` +
      fromSpanHtml(m) +
      `<span class="ch">ch${m.channel ?? 0}</span>` +
      `<span>${t}</span>` +
      status +
      actions +
    `</div>` +
    buildReplyQuote(m, log) +
    `<div class="text">${linkify(m.text)}</div>` +
    buildRfMeta(m);
  log.appendChild(div);

  // attach pending reactions, if any
  if (m.msg_id && PENDING_REACTIONS.has(m.msg_id)) {
    for (const r of PENDING_REACTIONS.get(m.msg_id)) applyReactionChip(div, r);
    PENDING_REACTIONS.delete(m.msg_id);
  }

  // keep buffer trimmed in DOM too (avoid leaks for long sessions)
  while (log.children.length > 250) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

async function pollChat() {
  try {
    pruneStalePending();
    const params = new URLSearchParams({ since: String(LAST_MSG_ID) });
    // On the very first poll, grab only the newest ~200 messages instead of
    // replaying the entire history forward from id 0 (which was slow AND made
    // a sound fire for every backlogged message).
    if (LAST_MSG_ID === 0) params.set("tail", "200");
    if (PENDING_OUT_IDS.size) {
      params.set("status_for", Array.from(PENDING_OUT_IDS).join(","));
    }
    const data = await api(`/api/chat/messages?${params.toString()}`);

    // Apply delivery status updates to existing messages (and DOM)
    if (Array.isArray(data.status_updates) && data.status_updates.length) {
      for (const u of data.status_updates) {
        const i = ALL_MESSAGES.findIndex(x => x.id === u.id);
        if (i < 0) continue;
        const m = ALL_MESSAGES[i];
        const wasStatus = m.delivery_status;
        m.delivery_status = u.delivery_status;
        m.delivery_hops  = u.delivery_hops;
        // Stop tracking if the new status is terminal
        if (m.delivery_status !== "enroute" && m.delivery_status !== "queued") {
          untrackPending(m.id);
        }
        if (wasStatus !== m.delivery_status) {
          updateMessageStatusInDom(m);
        }
      }
    }

    if (!data.messages?.length) return;
    const firstPoll = LAST_MSG_ID === 0;
    let shouldRerender = false;
    let freshIncoming = 0;     // count genuinely-new incoming msgs this batch
    for (const m of data.messages) {
      ALL_MESSAGES.push(m);
      trackPending(m);
      if (m.id > LAST_MSG_ID) LAST_MSG_ID = m.id;
      const convKey = conversationKey(m);
      // If the message belongs to the currently-open conversation, append to DOM
      if (convKey === SELECTED_CONV) {
        appendChatMessage(m);
      } else if (m.incoming && !m.is_reaction) {
        // For other conversations, bump per-conv unread counter
        const conv = CONVS.get(convKey);
        if (conv) conv.unread = (conv.unread || 0) + 1;
      }
      // Tab-level unread counter
      if (m.incoming && !m.is_reaction && CURRENT_TAB !== "chat") {
        UNREAD += 1;
      }
      // Browser notification only for genuinely-fresh incoming text messages.
      // The freshness guard (msg younger than 45s) stops the whole backlog
      // from notifying when history pages in across polls.
      if (!firstPoll && m.incoming && !m.is_reaction && isFreshIncoming(m)) {
        notifyIncoming(m);
        freshIncoming += 1;
      }
      if (!firstPoll || ALL_MESSAGES.length === data.messages.length) {
        shouldRerender = true;
      }
    }
    // One sound per poll batch (not per message), and only if something fresh.
    if (freshIncoming > 0 && soundPrefEnabled()) {
      playNotificationSound("ding");
    }
    if (shouldRerender) {
      rebuildConversations();
      renderConvList();
    }
    // Trim in-memory buffer to last 500 messages to keep things sprightly
    if (ALL_MESSAGES.length > 500) {
      ALL_MESSAGES = ALL_MESSAGES.slice(-500);
    }
    const badge = $("#chatBadge");
    if (UNREAD > 0 && CURRENT_TAB !== "chat") {
      badge.hidden = false;
      badge.textContent = UNREAD;
    } else {
      UNREAD = 0;
      badge.hidden = true;
    }
  } catch (e) { /* silent — keep polling */ }
}

// ---------- Browser notifications ----------
const NOTIF_LS_KEY = "wmb_notifications_enabled";
const NOTIF_SUPPORTED = typeof Notification !== "undefined";

function notifPrefEnabled() {
  return localStorage.getItem(NOTIF_LS_KEY) === "1";
}

function setNotifPref(on) {
  localStorage.setItem(NOTIF_LS_KEY, on ? "1" : "0");
}

function refreshNotifUi() {
  const status = $("#notifStatus");
  const permBtn = $("#notifPermBtn");
  const toggle = $("#notifEnabled");

  if (!NOTIF_SUPPORTED) {
    status.textContent = "Этот браузер не поддерживает уведомления.";
    toggle.disabled = true;
    permBtn.hidden = true;
    return;
  }

  toggle.checked = notifPrefEnabled();

  switch (Notification.permission) {
    case "granted":
      permBtn.hidden = true;
      status.textContent = toggle.checked
        ? "Уведомления включены."
        : "Разрешение получено, но уведомления выключены.";
      break;
    case "denied":
      permBtn.hidden = true;
      status.textContent = "Уведомления заблокированы в настройках браузера.";
      toggle.disabled = true;
      break;
    default:  // "default" — ещё не спрашивали
      permBtn.hidden = !toggle.checked;
      status.textContent = toggle.checked
        ? "Нажми кнопку справа, чтобы выдать разрешение."
        : "";
  }
}

$("#notifEnabled").addEventListener("change", async (e) => {
  setNotifPref(e.target.checked);
  if (e.target.checked && NOTIF_SUPPORTED && Notification.permission === "default") {
    const r = await Notification.requestPermission();
    if (r === "granted") {
      new Notification("Уведомления включены", { body: "Бот сообщит о новых сообщениях." });
    }
  }
  refreshNotifUi();
});

$("#notifPermBtn").addEventListener("click", async () => {
  if (!NOTIF_SUPPORTED) return;
  const r = await Notification.requestPermission();
  if (r === "granted") {
    new Notification("Уведомления включены", { body: "Бот сообщит о новых сообщениях." });
  }
  refreshNotifUi();
});

function shouldNotify() {
  if (!NOTIF_SUPPORTED) return false;
  if (!notifPrefEnabled()) return false;
  if (Notification.permission !== "granted") return false;
  // Не дёргать пользователя, если он уже смотрит в чат и окно активно.
  if (document.hasFocus() && CURRENT_TAB === "chat") return false;
  return true;
}

// A message is "fresh" only if it arrived within the last 45 seconds — used to
// suppress notifications/sound when a large backlog is paged in over polls.
function isFreshIncoming(m) {
  return (Date.now() / 1000 - (m.time || 0)) < 45;
}

function notifyIncoming(m) {
  // Sound is played once per poll batch in pollChat — not here (per message).
  if (!shouldNotify()) return;
  try {
    const n = new Notification(`💬 ${m.from_name || "Mesh"}`, {
      body: m.text,
      tag: "wmb-mesh",          // новые уведомления заменяют старое
      renotify: true,
      silent: false,
    });
    n.onclick = () => {
      window.focus();
      const tabBtn = document.querySelector('.tab-btn[data-tab="chat"]');
      if (tabBtn && CURRENT_TAB !== "chat") tabBtn.click();
      n.close();
    };
  } catch (e) { /* ignore */ }
}

// ---------- Chat search ----------

let CHAT_SEARCH_MODE = false;

async function runChatSearch(q) {
  const log = $("#chatLog");
  if (!log) return;
  if (!q) {
    CHAT_SEARCH_MODE = false;
    renderChatLog();
    return;
  }
  CHAT_SEARCH_MODE = true;
  log.innerHTML = `<div class="chat-empty muted">🔎 Ищу «${escapeHtml(q)}»…</div>`;
  try {
    const r = await api(`/api/chat/search?q=${encodeURIComponent(q)}&limit=200`);
    const msgs = r.messages || [];
    if (!msgs.length) {
      log.innerHTML = `<div class="chat-empty muted">Ничего не найдено по «${escapeHtml(q)}»</div>`;
      return;
    }
    log.innerHTML = "";
    const head = document.createElement("div");
    head.className = "chat-empty muted";
    head.style.padding = "8px 10px";
    head.style.fontSize = "0.82rem";
    head.innerHTML = `🔎 Найдено: <strong>${msgs.length}</strong> для «${escapeHtml(q)}»`;
    log.appendChild(head);
    const byMsgId = new Map();
    const elements = new Map();
    for (const m of msgs) {
      if (m.msg_id) byMsgId.set(String(m.msg_id), m);
    }
    PENDING_REACTIONS.clear();
    // Render newest first
    for (const m of msgs) {
      const el = _buildMessageElement(m, byMsgId, elements);
      if (el) log.appendChild(el);
    }
  } catch (e) {
    log.innerHTML = `<div class="chat-empty muted" style="color: var(--danger)">Ошибка поиска: ${escapeHtml(e.message)}</div>`;
  }
}

// ---------- Notification sounds (WebAudio, no .wav files needed) ----------

const SOUND_PREF_KEY = "wmb_sound";
let AUDIO_CTX = null;

function soundPrefEnabled() {
  return localStorage.getItem(SOUND_PREF_KEY) !== "off";
}
function setSoundPref(on) {
  localStorage.setItem(SOUND_PREF_KEY, on ? "on" : "off");
  refreshSoundUi();
}
function refreshSoundUi() {
  const cb = $("#soundEnabled");
  if (cb) cb.checked = soundPrefEnabled();
}

function _ensureAudioCtx() {
  if (!AUDIO_CTX) {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) AUDIO_CTX = new Ctx();
    } catch { /* unsupported */ }
  }
  // Some browsers suspend the ctx until a user gesture; resume on demand.
  if (AUDIO_CTX && AUDIO_CTX.state === "suspended") {
    AUDIO_CTX.resume().catch(() => {});
  }
  return AUDIO_CTX;
}

/** Play a short tone built from WebAudio oscillators. Variant tunes pitch+envelope. */
function playNotificationSound(variant = "ding") {
  const ctx = _ensureAudioCtx();
  if (!ctx) return;
  const profiles = {
    // (freq1, freq2, duration, type)
    ding:  { f1: 880,  f2: 1320, dur: 0.18, type: "sine"     },   // bright two-note
    ping:  { f1: 1040, f2: null, dur: 0.13, type: "triangle" },   // single short pip
    soft:  { f1: 660,  f2: null, dur: 0.10, type: "sine"     },   // for reactions
    alert: { f1: 600,  f2: 1000, dur: 0.40, type: "square"   },   // sharper "tревога"
  };
  const p = profiles[variant] || profiles.ding;
  const now = ctx.currentTime;
  const gain = ctx.createGain();
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.18, now + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + p.dur);
  gain.connect(ctx.destination);
  const osc1 = ctx.createOscillator();
  osc1.type = p.type;
  osc1.frequency.setValueAtTime(p.f1, now);
  if (p.f2) osc1.frequency.exponentialRampToValueAtTime(p.f2, now + p.dur * 0.8);
  osc1.connect(gain);
  osc1.start(now);
  osc1.stop(now + p.dur + 0.05);
}

// ---------- Reaction picker ----------
const QUICK_REACTIONS = [
  // Лица / эмоции
  "👍", "👎", "❤️", "🔥", "😂", "😮", "😢", "😡",
  "🙏", "👀", "🎉", "🤔", "😍", "🥳", "😎", "🤝",
  // Статус / реакция по делу
  "✅", "❌", "⚠️", "❓", "❗", "💯", "👌", "🫡",
  // Тематика mesh / погода / тревоги
  "📡", "🛰️", "🌧️", "⛈️", "❄️", "☀️", "🚨", "✈️",
];
let reactionPickerEl = null;
let reactionTargetMsgId = null;

function closeReactionPicker() {
  if (reactionPickerEl) {
    reactionPickerEl.remove();
    reactionPickerEl = null;
    reactionTargetMsgId = null;
  }
}

function openReactionPicker(btn) {
  closeReactionPicker();
  const msgId = btn.dataset.msgId;
  if (!msgId) return;
  reactionTargetMsgId = msgId;

  const picker = document.createElement("div");
  picker.className = "reaction-picker";
  picker.innerHTML =
    `<div class="reaction-grid">` +
      QUICK_REACTIONS.map(e => `<button data-emoji="${escapeHtml(e)}" title="${escapeHtml(e)}">${e}</button>`).join("") +
    `</div>` +
    `<div class="reaction-custom">` +
      `<input type="text" class="reaction-custom-input" maxlength="8" placeholder="свой эмодзи…" aria-label="Свой эмодзи">` +
      `<button class="reaction-custom-send" title="Отправить">➤</button>` +
    `</div>`;
  reactionPickerEl = picker;

  // Position picker near the button, clamped to the viewport (flips above the
  // button if there isn't enough room below — the grid makes it taller now).
  document.body.appendChild(picker);
  const rect = btn.getBoundingClientRect();
  const pw = picker.offsetWidth;
  const ph = picker.offsetHeight;
  let left = rect.left + window.scrollX + rect.width / 2 - pw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  let top = rect.bottom + window.scrollY + 6;
  // Not enough space below → place above the button
  if (rect.bottom + ph + 12 > window.innerHeight) {
    top = rect.top + window.scrollY - ph - 6;
    if (top < window.scrollY + 8) top = window.scrollY + 8;   // clamp to top
  }
  picker.style.left = left + "px";
  picker.style.top = top + "px";
  // Autofocus the custom-emoji input only on non-touch (avoid popping the
  // mobile keyboard over the grid).
  if (window.matchMedia("(hover: hover)").matches) {
    picker.querySelector(".reaction-custom-input")?.focus();
  }

  async function sendReaction(emoji) {
    const reply_to = reactionTargetMsgId;
    closeReactionPicker();
    emoji = (emoji || "").trim();
    if (!emoji || !reply_to) return;
    try {
      await api("/api/chat/react", {
        method: "POST",
        body: { emoji, reply_to: parseInt(reply_to, 10) },
      });
      // Don't toast — the chip will appear under the message via pollChat.
      pollChat();
    } catch (err) { toast(err.message, "err"); }
  }

  picker.addEventListener("click", (e) => {
    const tgt = e.target.closest("button[data-emoji]");
    if (tgt) { sendReaction(tgt.dataset.emoji); return; }
    if (e.target.closest(".reaction-custom-send")) {
      sendReaction(picker.querySelector(".reaction-custom-input")?.value);
    }
  });
  const customInput = picker.querySelector(".reaction-custom-input");
  customInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendReaction(customInput.value); }
    else if (e.key === "Escape") { e.preventDefault(); closeReactionPicker(); }
  });
}

document.addEventListener("click", (e) => {
  // Click on a sender name → open their profile / offer a DM
  const fromEl = e.target.closest(".from-clickable");
  if (fromEl) {
    e.stopPropagation();
    openSenderInfo(fromEl.dataset.fromId);
    return;
  }
  // Reaction button → open emoji picker
  const reactBtn = e.target.closest(".react-btn");
  if (reactBtn) {
    e.stopPropagation();
    openReactionPicker(reactBtn);
    return;
  }
  // Reply button → enter reply mode
  const replyBtn = e.target.closest(".reply-btn");
  if (replyBtn) {
    e.stopPropagation();
    const msgEl = replyBtn.closest(".chat-msg");
    if (msgEl) startReply(msgEl);
    return;
  }
  // Click on a reply-quote → scroll to and highlight the parent message
  const quote = e.target.closest(".reply-quote");
  if (quote) {
    const targetId = quote.dataset.target;
    if (targetId) {
      const parent = document.querySelector(`#chatLog [data-mesh-id="${targetId}"]`);
      if (parent) {
        parent.scrollIntoView({ behavior: "smooth", block: "center" });
        parent.classList.add("highlight");
        setTimeout(() => parent.classList.remove("highlight"), 1500);
      }
    }
    return;
  }
  if (reactionPickerEl && !reactionPickerEl.contains(e.target)) {
    closeReactionPicker();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeReactionPicker();
});

// ---------- Reply mode ----------
let REPLY_TO = null; // { msg_id, name, preview }

function startReply(msgEl) {
  const id = parseInt(msgEl.dataset.meshId, 10);
  if (!id) return;
  const name = msgEl.querySelector(".meta .from")?.textContent || "?";
  const text = msgEl.querySelector(".text")?.textContent || "";
  const preview = text.length > 80 ? text.slice(0, 80) + "…" : text;
  REPLY_TO = { msg_id: id, name, preview };

  $("#replyTo").hidden = false;
  $("#replyTo .reply-to-name").textContent = name;
  $("#replyTo .reply-to-preview").textContent = preview ? `: ${preview}` : "";
  $("#chatInput").focus();
}

function cancelReply() {
  REPLY_TO = null;
  $("#replyTo").hidden = true;
}

$("#replyTo .reply-to-cancel").addEventListener("click", cancelReply);

$("#chatSend").addEventListener("click", async () => {
  const text = $("#chatInput").value.trim();
  if (!text) return;
  if (!SELECTED_CONV) {
    toast("Выберите чат слева", "err");
    return;
  }
  const conv = CONVS.get(SELECTED_CONV);
  const destination = conv?.kind === "dm" ? conv.peerId : "broadcast";
  const channel = conv?.kind === "channel" ? conv.channel : null;
  const body = { text, destination };
  if (channel != null) body.channel = channel;
  try {
    if (REPLY_TO) {
      await api("/api/chat/reply", {
        method: "POST",
        body: { ...body, reply_to: REPLY_TO.msg_id },
      });
      cancelReply();
    } else {
      await api("/api/chat/send", { method: "POST", body });
    }
    $("#chatInput").value = "";
    $("#chatInput").style.height = "auto";  // collapse back to one row
    pollChat();
  } catch (e) { toast(e.message, "err"); }
});
$("#chatInput").addEventListener("keydown", (e) => {
  // Enter alone — send. Shift+Enter — let the textarea insert a newline.
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chatSend").click();
    return;
  }
  if (e.key === "Escape" && REPLY_TO) { e.preventDefault(); cancelReply(); }
});
// Auto-grow textarea as the user adds lines (capped by CSS max-height).
$("#chatInput").addEventListener("input", (e) => {
  const el = e.target;
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
});

// ---------- Init ----------
async function init() {
  ALL_FIELDS = await api("/api/fields");
  CONFIG = await api("/api/config");
  $("#connectionType").value = CONFIG.mesh?.connection_type || "serial";
  $("#devicePath").value = CONFIG.mesh?.device_path || "auto";
  $("#tcpHost").value = CONFIG.mesh?.tcp_host || "";
  $("#tcpPort").value = CONFIG.mesh?.tcp_port || 4403;
  $("#channelIndex").value = CONFIG.mesh?.channel_index ?? 0;
  $("#chunkDelay").value = CONFIG.mesh?.chunk_delay ?? 10;
  $("#useEmojis").checked = !!CONFIG.message?.use_emojis;
  $("#includeHeader").checked = CONFIG.message?.include_header !== false;
  $("#commandsEnabled").checked = CONFIG.commands?.enabled !== false;
  $("#cmdDelayMin").value = CONFIG.commands?.reply_delay_min_s ?? 5;
  $("#cmdDelayMax").value = CONFIG.commands?.reply_delay_max_s ?? 10;
  updateConnectionFields();
  renderCurrentCity();
  buildDayButtons($("#newDays"), DAYS.map(d => d.k));
  buildFieldChips($("#newFields"), ALL_FIELDS.map(f => f.key));
  buildFieldChips($("#manualFields"), ALL_FIELDS.map(f => f.key));
  await refreshSlots();
  refreshMeshStatus();
  refreshNotifUi();
  refreshSoundUi();
  refreshAlertsUi();
  $("#soundEnabled")?.addEventListener("change", (e) => setSoundPref(e.target.checked));
  $("#soundTestBtn")?.addEventListener("click", () => playNotificationSound("ding"));

  // Chat full-text search
  let chatSearchTimer = null;
  $("#chatSearchInput")?.addEventListener("input", (e) => {
    clearTimeout(chatSearchTimer);
    const q = e.target.value.trim();
    $("#chatSearchClear").hidden = q.length === 0;
    chatSearchTimer = setTimeout(() => runChatSearch(q), 250);
  });
  $("#chatSearchClear")?.addEventListener("click", () => {
    $("#chatSearchInput").value = "";
    $("#chatSearchClear").hidden = true;
    runChatSearch("");
  });
  pollChat();
  refreshDashboard();
  setInterval(refreshMeshStatus, 15000);
  setInterval(pollChat, 4000);
  setInterval(() => {
    if (CURRENT_TAB === "home") refreshDashboard();
  }, 30000);
  // Auto-refresh the Telegram bridge + status-bot panels while user looks at them
  setInterval(() => {
    if (CURRENT_TAB === "integr") {
      refreshTelegramStatus();
      refreshTgStatusBot();
    }
  }, 8000);
  // Wire telegram controls now that the DOM exists
  wireTelegramPanel();
  wireUpdatePanel();
  wireTgStatusBotPanel();
  wireLlmPanel();
  wireProxyPanel();
  wireNowcastPanel();
  wireMqttPanel();
}

// ---------- Радар-нокаст («дождь идёт к тебе») ----------

function wireNowcastPanel() {
  $("#ncEnabled")?.addEventListener("change", async () => {
    await saveNowcastConfig();
    refreshNowcastStatus();
  });
  $("#ncSave")?.addEventListener("click", async () => {
    await saveNowcastConfig(); toast("Сохранено", "ok"); refreshNowcastStatus();
  });
  $("#ncRefresh")?.addEventListener("click", refreshNowcastStatus);
  $("#ncTest")?.addEventListener("click", testNowcast);
}

async function saveNowcastConfig() {
  const nowcast = {
    enabled:                $("#ncEnabled").checked,
    check_interval_minutes: Math.max(2, parseInt($("#ncInterval").value, 10) || 10),
    lookahead_minutes:      Math.max(15, parseInt($("#ncLookahead").value, 10) || 60),
    min_intensity_mm:       Math.max(0.1, parseFloat($("#ncMinMm").value) || 0.3),
    quiet_minutes:          Math.max(0, parseInt($("#ncQuiet").value, 10) || 60),
    alert_ongoing:          $("#ncAlertOngoing").checked,
  };
  try {
    await api("/api/config", { method: "POST", body: { nowcast } });
  } catch (e) { toast(e.message, "err"); }
}

async function refreshNowcastStatus() {
  let s;
  try { s = await api("/api/nowcast/status"); }
  catch (e) { setNcStatus("err", tf("Ошибка: {0}", e.message)); return; }
  const c = s.config || {};
  const st = s.state || {};

  if (c.enabled) {
    const last = st.last_check_ts ? new Date(st.last_check_ts * 1000).toLocaleTimeString() : "—";
    setNcStatus("ok", tf("Включён · последняя проверка {0}", last));
  } else {
    setNcStatus("idle", t("Выключен"));
  }
  // Reflect config into fields only when untouched defaults.
  if ($("#ncEnabled")) $("#ncEnabled").checked = !!c.enabled;
  if (c.check_interval_minutes != null && $("#ncInterval").value === "10") $("#ncInterval").value = c.check_interval_minutes;
  if (c.lookahead_minutes != null && $("#ncLookahead").value === "60") $("#ncLookahead").value = c.lookahead_minutes;
  if (c.min_intensity_mm != null && $("#ncMinMm").value === "0.3") $("#ncMinMm").value = c.min_intensity_mm;
  if (c.quiet_minutes != null && $("#ncQuiet").value === "60") $("#ncQuiet").value = c.quiet_minutes;
  if (typeof c.alert_ongoing === "boolean") $("#ncAlertOngoing").checked = c.alert_ongoing;
}

async function testNowcast() {
  const out = $("#ncTestResult");
  out.hidden = false;
  out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ Запрашиваю поминутный прогноз…</span>`;
  try {
    await saveNowcastConfig();
    const r = await api("/api/nowcast/check", { method: "POST" });
    if (r.sent && r.sent.text) {
      out.className = "tg-test-result ok";
      out.innerHTML = `✅ <strong>Отправлено в mesh:</strong><div class="muted" style="margin-top:4px">${escapeHtml(r.sent.text)}</div>`;
    } else {
      out.className = "tg-test-result";
      out.innerHTML = `<span class="muted">☀️ Осадков в ближайший час не ожидается — ничего не отправлено.</span>`;
    }
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
  refreshNowcastStatus();
}

function setNcStatus(kind, text) {
  const dot = $("#ncStatus .tg-dot");
  const txt = $("#ncStatusText");
  if (dot) dot.className = `tg-dot tg-dot-${kind}`;
  if (txt) txt.textContent = text;
}

// ---------- MQTT → Home Assistant ----------
function wireMqttPanel() {
  $("#mqttEnabled")?.addEventListener("change", async () => { await saveMqttConfig(); refreshMqttStatus(); });
  $("#mqttSave")?.addEventListener("click", async () => { await saveMqttConfig(); toast(t("Сохранено"), "ok"); refreshMqttStatus(); });
  $("#mqttRefresh")?.addEventListener("click", refreshMqttStatus);
  $("#mqttTest")?.addEventListener("click", testMqtt);
}

function _mqttFromInputs() {
  return {
    enabled:         $("#mqttEnabled").checked,
    host:            $("#mqttHost").value.trim() || "127.0.0.1",
    port:            Math.max(1, parseInt($("#mqttPort").value, 10) || 1883),
    username:        $("#mqttUser").value.trim(),
    base_topic:      $("#mqttBaseTopic").value.trim() || "weather-mesh",
    interval_s:      Math.max(10, parseInt($("#mqttInterval").value, 10) || 60),
    publish_weather: $("#mqttPubWeather").checked,
    publish_nodes:   $("#mqttPubNodes").checked,
    publish_alerts:  $("#mqttPubAlerts").checked,
  };
}

async function saveMqttConfig() {
  const mqtt = _mqttFromInputs();
  const pass = $("#mqttPass").value;   // only send if typed (don't wipe saved)
  if (pass) mqtt.password = pass;
  try { await api("/api/config", { method: "POST", body: { mqtt } }); }
  catch (e) { toast(e.message, "err"); }
}

async function refreshMqttStatus() {
  let s;
  try { s = await api("/api/mqtt/status"); }
  catch (e) { setMqttStatus("err", t("Ошибка: ") + e.message); return; }
  if (!s.available) {
    setMqttStatus("err", "paho-mqtt не установлен на сервере");
  } else if (!s.enabled) {
    setMqttStatus("idle", t("Выключен"));
  } else if (s.connected) {
    const last = s.last_publish_ts ? relTime(s.last_publish_ts) : "—";
    setMqttStatus("ok", tf("Подключён к {0}:{1} · публикация {2}", s.host, s.port, last));
  } else {
    setMqttStatus("warn", (s.last_error ? t("Ошибка: ") + s.last_error : t("Подключаюсь…")));
  }
  if ($("#mqttEnabled")) $("#mqttEnabled").checked = !!s.enabled;
}

async function testMqtt() {
  const out = $("#mqttTestResult");
  out.hidden = false; out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ ${t("Проверяю брокер…")}</span>`;
  try {
    const mqtt = _mqttFromInputs();
    const pass = $("#mqttPass").value;
    if (pass) mqtt.password = pass;
    const r = await api("/api/mqtt/test", { method: "POST", body: { mqtt } });
    if (r.ok) {
      out.className = "tg-test-result ok";
      out.innerHTML = `✅ <strong>${t("Брокер отвечает")}</strong>`;
    } else {
      out.className = "tg-test-result err";
      out.innerHTML = `❌ ${escapeHtml(r.error || "?")}`;
    }
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
}

async function loadMqttConfig() {
  let cfg;
  try { cfg = await api("/api/config"); }
  catch { return; }
  const m = cfg.mqtt || {};
  const setv = (id, v) => { const el = $(id); if (el && v != null && el.value === el.defaultValue) el.value = v; };
  if ($("#mqttHost") && !$("#mqttHost").value) $("#mqttHost").value = m.host || "127.0.0.1";
  if ($("#mqttPort")) $("#mqttPort").value = m.port || 1883;
  if ($("#mqttUser") && !$("#mqttUser").value) $("#mqttUser").value = m.username || "";
  if ($("#mqttBaseTopic") && !$("#mqttBaseTopic").value) $("#mqttBaseTopic").value = m.base_topic || "weather-mesh";
  if ($("#mqttInterval")) $("#mqttInterval").value = m.interval_s || 60;
  if ($("#mqttPass") && m.password) $("#mqttPass").placeholder = "(сохранён — впиши, чтобы заменить)";
  if (typeof m.publish_weather === "boolean") $("#mqttPubWeather").checked = m.publish_weather;
  if (typeof m.publish_nodes === "boolean") $("#mqttPubNodes").checked = m.publish_nodes;
  if (typeof m.publish_alerts === "boolean") $("#mqttPubAlerts").checked = m.publish_alerts;
  if (typeof m.enabled === "boolean") $("#mqttEnabled").checked = m.enabled;
}

function setMqttStatus(kind, text) {
  const dot = $("#mqttStatus .tg-dot");
  const txt = $("#mqttStatusText");
  if (dot) dot.className = `tg-dot tg-dot-${kind}`;
  if (txt) txt.textContent = text;
}

// ---------- Backup / restore ----------
$("#backupDownload")?.addEventListener("click", () => {
  const dbs = $("#backupDbs")?.checked ? "?dbs=1" : "";
  const a = document.createElement("a");
  a.href = `/api/backup/download${dbs}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast("Бэкап скачивается…", "ok");
});
$("#backupRestoreBtn")?.addEventListener("click", () => $("#backupFile")?.click());
$("#backupFile")?.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  if (!confirm(`Восстановить настройки из «${file.name}»? Текущие будут перезаписаны, сервис перезапустится.`)) {
    e.target.value = "";
    return;
  }
  const out = $("#backupResult");
  out.hidden = false; out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ Загружаю и восстанавливаю…</span>`;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/backup/restore", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    out.className = "tg-test-result ok";
    out.innerHTML = `✅ <strong>Восстановлено:</strong> ${escapeHtml((data.restored || []).join(", "))}`
      + `<div class="muted" style="margin-top:4px">Сервис перезапускается, страница переподключится через ~10 сек…</div>`;
    setTimeout(() => location.reload(), 10000);
  } catch (err) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(err.message)}`;
  }
  e.target.value = "";
});

// ---------- Health / self-diagnostics ----------
$("#healthRefresh")?.addEventListener("click", refreshHealth);

$("#botRestart")?.addEventListener("click", async () => {
  if (!confirm(t("Перезагрузить сервис бота? Веб-интерфейс переподключится через ~10 сек."))) return;
  const out = $("#restartResult");
  out.hidden = false; out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ ${t("Перезагружаю бота…")}</span>`;
  try {
    await api("/api/system/restart", { method: "POST" });
    out.className = "tg-test-result ok";
    out.innerHTML = `✅ ${t("Сервис перезапускается, страница переподключится…")}`;
    setTimeout(() => location.reload(), 10000);
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
});

async function refreshHealth() {
  const box = $("#healthGrid");
  if (!box) return;
  let h;
  try { h = await api("/api/health/full"); }
  catch (e) { box.innerHTML = `<div class="muted">Ошибка: ${escapeHtml(e.message)}</div>`; return; }

  const dot = (ok) => ok == null ? `<span class="hd-dot idle"></span>`
                                 : `<span class="hd-dot ${ok ? "ok" : "bad"}"></span>`;
  const row = (label, val, ok) =>
    `<div class="hd-row">${dot(ok)}<span class="hd-label">${escapeHtml(t(label))}</span>` +
    `<span class="hd-val">${escapeHtml(String(val))}</span></div>`;

  const up = h.uptime_seconds || 0;
  const upStr = up < 3600 ? `${Math.round(up / 60)} мин`
              : up < 86400 ? `${(up / 3600).toFixed(1)} ч`
              : `${(up / 86400).toFixed(1)} дн`;
  const wxOk = h.weather_last_ok_ts && (Date.now() / 1000 - h.weather_last_ok_ts) < 3600;
  const disk = h.disk || {};
  const diskOk = disk.used_pct == null ? null : disk.used_pct < 90;
  const xrayOk = h.xray_active === "active";

  let html = "";
  html += row("Нода Heltec", h.mesh_connected ? `${t("на связи")} · ${h.nodes_online_2h ?? 0} ${t("онлайн")}` : t("нет связи"), !!h.mesh_connected);
  html += row("Погода (Open-Meteo)",
    wxOk ? `${t("ок")} · ${relTime(h.weather_last_ok_ts)}`
         : (h.location_set ? t("нет свежих данных") : t("город не задан")),
    wxOk ? true : (h.location_set ? false : null));
  html += row("Прокси",
    h.proxy_via ? `${t("через прокси · выход")} ${h.proxy_exit_ip || "?"}` : t("напрямую (без прокси)"),
    h.proxy_via ? !!h.proxy_exit_ip : null);
  html += row("Xray", h.xray_active || "—", xrayOk);
  html += row("Диск",
    disk.free_gb != null ? `${disk.free_gb} ГБ свободно · занято ${disk.used_pct}%` : "—", diskOk);
  html += row("Аптайм сервиса", upStr, null);
  html += row("Версия", h.version || "—", null);
  box.innerHTML = html;
}

// ---------- Прокси (общий, с тумблерами по сервисам) ----------

function wireProxyPanel() {
  $("#proxySave")?.addEventListener("click", saveProxyConfig);
  $("#proxyRefresh")?.addEventListener("click", loadProxyConfig);
  $("#proxyTest")?.addEventListener("click", testProxy);
  $("#proxySubLoad")?.addEventListener("click", loadProxySubscription);
  $("#proxyExitApply")?.addEventListener("click", applyProxyExit);
  $("#proxyAutoSwitch")?.addEventListener("change", async (e) => {
    try { await api("/api/config", { method: "POST", body: { proxy: { auto_switch: e.target.checked } } }); }
    catch (err) { toast(err.message, "err"); }
  });
}

async function loadProxyConfig() {
  let cfg;
  try { cfg = await api("/api/config"); }
  catch (e) { toast(e.message, "err"); return; }
  const p = cfg.proxy || {};
  if ($("#proxyUrl")) $("#proxyUrl").value = p.url || "";
  const set = (id, v) => { const el = $(id); if (el) el.checked = v !== false; };
  set("#proxyUseWeather",  p.use_weather);
  set("#proxyUseRadar",    p.use_radar);
  set("#proxyUseTelegram", p.use_telegram);
  set("#proxyUseLlm",      p.use_llm);
  set("#proxyUseTgstatus", p.use_tgstatus);
  if ($("#proxyAutoSwitch")) $("#proxyAutoSwitch").checked = !!p.auto_switch;
  if ($("#proxySubUrl") && p.subscription_url) $("#proxySubUrl").placeholder = "(подписка сохранена — впиши, чтобы заменить)";
  loadProxyExits();
}

let PROXY_EXITS = [];
let PROXY_SELECTED = null;
let PROXY_PINGS = {};        // index -> ms (or null)

function _renderExitOptions(exits, selected, pings) {
  if (exits !== undefined) PROXY_EXITS = exits || [];
  if (selected !== undefined) PROXY_SELECTED = selected;
  if (pings !== undefined) PROXY_PINGS = pings || {};
  const sel = $("#proxyExitSelect");
  if (!sel) return;
  if (!PROXY_EXITS.length) {
    sel.innerHTML = `<option value="">${t("— сначала загрузи подписку —")}</option>`;
    return;
  }
  const cur = sel.value;
  sel.innerHTML = PROXY_EXITS.map(e => {
    let suffix = "";
    if (e.index in PROXY_PINGS) {
      const ms = PROXY_PINGS[e.index];
      suffix = ms == null ? " · —" : ` · ${ms} ${t("мс")}`;
    }
    return `<option value="${e.index}"${e.index === PROXY_SELECTED ? " selected" : ""}>`
         + `${escapeHtml(e.name || e.host)}${suffix}</option>`;
  }).join("");
  if (cur) sel.value = cur;
}

async function measureProxyPings() {
  if (!PROXY_EXITS.length) return;
  try {
    const r = await api("/api/proxy/ping");
    const map = {};
    (r.pings || []).forEach(p => { map[p.index] = p.ms; });
    _renderExitOptions(undefined, undefined, map);
  } catch { /* pings are best-effort */ }
}

async function loadProxyExits() {
  let d;
  try { d = await api("/api/proxy/exits"); }
  catch { return; }
  _renderExitOptions(d.exits, d.selected, {});
  if ($("#proxyAutoSwitch")) $("#proxyAutoSwitch").checked = !!d.auto_switch;
  measureProxyPings();
}

async function loadProxySubscription() {
  const out = $("#proxyMgrResult");
  const url = $("#proxySubUrl").value.trim();
  out.hidden = false; out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ Получаю подписку…</span>`;
  try {
    const body = url ? { url } : {};
    const r = await api("/api/proxy/subscription", { method: "POST", body });
    _renderExitOptions(r.exits, null, {});
    measureProxyPings();
    out.className = "tg-test-result ok";
    out.innerHTML = `✅ Загружено выходов: <strong>${r.count}</strong>. Выбери страну и нажми «Переключить».`;
    $("#proxySubUrl").value = "";
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
}

async function applyProxyExit() {
  const sel = $("#proxyExitSelect");
  const index = sel?.value;
  if (index === "" || index == null) { toast("Сначала загрузи подписку и выбери страну", "err"); return; }
  const out = $("#proxyMgrResult");
  out.hidden = false; out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ Переключаю выход и перезапускаю Xray…</span>`;
  try {
    const r = await api("/api/proxy/select", { method: "POST", body: { index: parseInt(index, 10) } });
    out.className = "tg-test-result ok";
    out.innerHTML = `✅ <strong>${escapeHtml(r.exit_name || "выход")}</strong>`
      + `<div class="muted" style="margin-top:4px">Внешний IP: ${escapeHtml(r.exit_ip || "проверь кнопкой «Проверить»")}</div>`;
    loadProxyConfig();
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
}

async function saveProxyConfig() {
  const proxy = {
    url:          $("#proxyUrl").value.trim(),
    use_weather:  $("#proxyUseWeather").checked,
    use_radar:    $("#proxyUseRadar").checked,
    use_telegram: $("#proxyUseTelegram").checked,
    use_llm:      $("#proxyUseLlm").checked,
    use_tgstatus: $("#proxyUseTgstatus").checked,
  };
  try {
    await api("/api/config", { method: "POST", body: { proxy } });
    toast("Прокси сохранён", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function testProxy() {
  const url = $("#proxyUrl").value.trim();
  const out = $("#proxyTestResult");
  out.hidden = false;
  out.className = "tg-test-result";
  out.innerHTML = url
    ? `<span class="muted">${t("⏳ Проверяю выход через прокси…")}</span>`
    : `<span class="muted">${t("⏳ Проверяю прямое соединение…")}</span>`;
  try {
    const r = await api("/api/proxy/test", { method: "POST", body: { url } });
    const via = r.via_proxy ? "через прокси" : "напрямую";
    if (r.ok) {
      out.className = "tg-test-result ok";
      out.innerHTML =
        `✅ <strong>Связь есть</strong> — ${via}` +
        `<div class="muted" style="margin-top:4px">Внешний IP: ${escapeHtml(r.ip || "?")}</div>`;
    } else {
      out.className = "tg-test-result err";
      out.innerHTML =
        `❌ <strong>Не удалось</strong> — ${via}` +
        `<div class="muted" style="margin-top:4px">${escapeHtml(r.error || "?")}</div>`;
    }
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
}

// ---------- LLM / AI assistant ----------

function wireLlmPanel() {
  $("#llmEnabled")?.addEventListener("change", async (e) => {
    // Toggle persists immediately (save current fields too)
    await saveLlmConfig();
    refreshLlmStatus();
  });
  $("#llmSave")?.addEventListener("click", async () => { await saveLlmConfig(); toast("Сохранено", "ok"); refreshLlmStatus(); });
  $("#llmTest")?.addEventListener("click", testLlm);
  $("#llmAskBtn")?.addEventListener("click", askLlm);
  $("#llmAskInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") askLlm(); });
}

async function saveLlmConfig() {
  const llm = {
    enabled:         $("#llmEnabled").checked,
    base_url:        $("#llmBaseUrl").value.trim() || "https://integrate.api.nvidia.com/v1",
    model:           $("#llmModel").value.trim() || "moonshotai/kimi-k2-instruct",
    fallback_models: $("#llmFallback").value.split(",").map(s => s.trim()).filter(Boolean),
    context_memory:  $("#llmContextMemory").checked,
    system_prompt:   $("#llmSystemPrompt").value.trim(),
    max_tokens:      Math.max(16, parseInt($("#llmMaxTokens").value, 10) || 200),
    max_reply_chars: Math.max(50, parseInt($("#llmMaxChars").value, 10) || 600),
    temperature:     Math.max(0, Math.min(2, parseFloat($("#llmTemp").value) || 0.6)),
  };
  // Only send api_key if the user typed something (don't wipe a saved key)
  const key = $("#llmApiKey").value.trim();
  if (key) llm.api_key = key;
  try {
    await api("/api/config", { method: "POST", body: { llm } });
  } catch (e) { toast(e.message, "err"); }
}

async function refreshLlmStatus() {
  let s;
  try { s = await api("/api/llm/status"); }
  catch (e) { setLlmStatus("err", tf("Ошибка: {0}", e.message)); return; }

  if (s.enabled && s.api_key_set) {
    setLlmStatus("ok", tf("Включён · модель {0}", s.model));
  } else if (s.enabled && !s.api_key_set) {
    setLlmStatus("warn", t("Включён, но не задан API-ключ"));
  } else {
    setLlmStatus("idle", s.api_key_set ? t("Выключен (ключ сохранён)") : t("Выключен"));
  }
  // Reflect config — don't stomp fields the user is editing
  if ($("#llmEnabled")) $("#llmEnabled").checked = !!s.enabled;
  if (!$("#llmApiKey").value && s.api_key_set) $("#llmApiKey").placeholder = "(ключ сохранён — впиши чтобы заменить)";
  if (!$("#llmBaseUrl").value) $("#llmBaseUrl").value = s.base_url || "";
  if (!$("#llmModel").value) $("#llmModel").value = s.model || "";
  if (!$("#llmFallback").value && s.fallback_models) $("#llmFallback").value = s.fallback_models;
  if (typeof s.context_memory === "boolean") $("#llmContextMemory").checked = s.context_memory;
  if (!$("#llmSystemPrompt").value) $("#llmSystemPrompt").value = s.system_prompt || "";
  if (s.max_tokens != null && $("#llmMaxTokens").value === "200") $("#llmMaxTokens").value = s.max_tokens;
  if (s.max_reply_chars != null && $("#llmMaxChars").value === "600") $("#llmMaxChars").value = s.max_reply_chars;
  if (s.temperature != null && $("#llmTemp").value === "0.6") $("#llmTemp").value = s.temperature;
}

function setLlmStatus(kind, text) {
  const dot = $("#llmStatus .tg-dot");
  const txt = $("#llmStatusText");
  if (dot) dot.className = `tg-dot tg-dot-${kind}`;
  if (txt) txt.textContent = text;
}

async function testLlm() {
  const out = $("#llmTestResult");
  out.hidden = false; out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ Сохраняю и проверяю связь с LLM…</span>`;
  await saveLlmConfig();
  try {
    const r = await api("/api/llm/test", { method: "POST" });
    if (r.ok) {
      out.className = "tg-test-result ok";
      out.innerHTML = `✅ <strong>Связь есть</strong> · ${r.elapsed_seconds?.toFixed?.(1) ?? "?"} сек<div class="muted" style="margin-top:4px">Ответ: ${escapeHtml(r.answer || "")}</div>`;
    } else {
      out.className = "tg-test-result err";
      out.innerHTML = `❌ <strong>Не удалось</strong><div class="muted" style="margin-top:4px">${escapeHtml(r.error || "—")}</div>`;
    }
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
  refreshLlmStatus();
}

async function askLlm() {
  const q = $("#llmAskInput").value.trim();
  if (!q) return;
  const out = $("#llmAskResult");
  out.hidden = false;
  out.innerHTML = `<span class="muted">🤔 Думаю…</span>`;
  const btn = $("#llmAskBtn"); btn.disabled = true;
  try {
    const r = await api("/api/llm/ask", { method: "POST", body: { question: q } });
    out.innerHTML = `<div class="llm-answer">${escapeHtml(r.answer || "")}</div>`;
  } catch (e) {
    out.innerHTML = `<span class="muted" style="color: var(--danger)">${escapeHtml(e.message)}</span>`;
  } finally { btn.disabled = false; }
}

// ---------- Telegram status-bot (pinned message) ----------

function wireTgStatusBotPanel() {
  $("#tgsSave")?.addEventListener("click", saveTgStatusConfig);
  $("#tgsEnabled")?.addEventListener("change", async (e) => {
    if (e.target.checked) await saveTgStatusConfig();
    const ep = e.target.checked ? "/api/tg-status/start" : "/api/tg-status/stop";
    try {
      const r = await api(ep, { method: "POST" });
      if (r && r.ok === false && r.error) toast(r.error, "err");
      else toast(e.target.checked ? "Запущен" : "Остановлен", "ok");
    } catch (err) { toast(err.message, "err"); }
    refreshTgStatusBot();
  });
  $("#tgsStart")?.addEventListener("click", async () => {
    await saveTgStatusConfig();
    try {
      const r = await api("/api/tg-status/start", { method: "POST" });
      if (!r.ok && r.error) toast(r.error, "err"); else toast("Запущен", "ok");
    } catch (e) { toast(e.message, "err"); }
    refreshTgStatusBot();
  });
  $("#tgsStop")?.addEventListener("click", async () => {
    try {
      await api("/api/tg-status/stop", { method: "POST" });
      toast("Остановлен", "ok");
    } catch (e) { toast(e.message, "err"); }
    refreshTgStatusBot();
  });
  $("#tgsUpdateNow")?.addEventListener("click", async () => {
    const btn = $("#tgsUpdateNow"); btn.disabled = true; btn.textContent = "⏳ Обновляю…";
    try {
      const r = await api("/api/tg-status/update-now", { method: "POST" });
      if (r.ok) toast("Сообщение обновлено", "ok");
      else toast("Ошибка: " + (r.error || "—"), "err");
    } catch (e) { toast(e.message, "err"); }
    finally { btn.disabled = false; btn.textContent = "🔄 Обновить сейчас"; }
    refreshTgStatusBot();
  });
  $("#tgsReset")?.addEventListener("click", async () => {
    if (!confirm("Забыть текущий закреп? Следующее обновление создаст новое сообщение.")) return;
    try {
      await api("/api/tg-status/reset-message", { method: "POST" });
      toast("Сброшено — следующее сообщение будет новым", "ok");
    } catch (e) { toast(e.message, "err"); }
    refreshTgStatusBot();
  });
}

async function saveTgStatusConfig() {
  const payload = {
    telegram_status: {
      bot_token:       $("#tgsToken").value.trim(),
      chat_id:         $("#tgsChatId").value.trim(),
      update_seconds:  Math.max(15, parseInt($("#tgsUpdateSec").value, 10) || 60),
      auto_pin:        $("#tgsAutoPin").checked,
      commands_enabled: $("#tgsCommandsEnabled").checked,
      daily_time:      $("#tgsDailyTime").value || "09:00",
      admin_secret:    $("#tgsAdminSecret").value.trim(),
      show_weather:    $("#tgsShowWeather").checked,
      show_mesh_stats: $("#tgsShowMesh").checked,
      extra_text:      $("#tgsExtra").value.trim(),
    },
  };
  try {
    await api("/api/config", { method: "POST", body: payload });
    toast("Сохранено", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function refreshTgStatusBot() {
  let s;
  try { s = await api("/api/tg-status/status"); }
  catch (e) {
    setTgsStatus("err", tf("Ошибка: {0}", e.message));
    return;
  }
  const c = s.config || {};

  // Status dot/text
  if (s.running) {
    const last = s.last_success_ts ? new Date(s.last_success_ts * 1000).toLocaleTimeString() : "—";
    setTgsStatus("ok", tf("Работает · последнее обновление {0} · всего: {1}", last, s.updates_count));
  } else if (s.last_error) {
    setTgsStatus("err", t("Остановлен · ") + s.last_error);
  } else {
    setTgsStatus("idle", c.bot_token_set && c.chat_id ? t("Готов запуститься") : t("Заполни bot_token и chat_id"));
  }

  // Reflect the enable toggle from real state (running > persisted enabled)
  if ($("#tgsEnabled")) $("#tgsEnabled").checked = !!(s.running || c.enabled);

  // Reflect config (only when fields are empty — don't stomp user typing)
  if (!$("#tgsToken").value && c.bot_token_set) $("#tgsToken").placeholder = "(сохранено)";
  if (!$("#tgsChatId").value && c.chat_id) $("#tgsChatId").value = c.chat_id;
  if (c.update_seconds != null && $("#tgsUpdateSec").value === "60") $("#tgsUpdateSec").value = c.update_seconds;
  if (typeof c.auto_pin === "boolean")  $("#tgsAutoPin").checked = c.auto_pin;
  if (typeof c.commands_enabled === "boolean") $("#tgsCommandsEnabled").checked = c.commands_enabled;
  if (c.daily_time && $("#tgsDailyTime")) $("#tgsDailyTime").value = c.daily_time;
  if (c.admin_secret != null && $("#tgsAdminSecret") && !$("#tgsAdminSecret").value) $("#tgsAdminSecret").value = c.admin_secret;
  if (typeof c.show_weather === "boolean") $("#tgsShowWeather").checked = c.show_weather;
  if (typeof c.show_mesh_stats === "boolean") $("#tgsShowMesh").checked = c.show_mesh_stats;
  if (c.extra_text && !$("#tgsExtra").value) $("#tgsExtra").value = c.extra_text;
}

function setTgsStatus(kind, text) {
  const dot = $("#tgsStatus .tg-dot");
  const txt = $("#tgsStatusText");
  if (dot) dot.className = `tg-dot tg-dot-${kind}`;
  if (txt) txt.textContent = text;
}

// ---------- Auto-update from git ----------

function wireUpdatePanel() {
  $("#updRefresh")?.addEventListener("click", refreshUpdateInfo);
  $("#updPull")?.addEventListener("click", runGitUpdate);
}

async function refreshUpdateInfo() {
  const txt = $("#updStatusText");
  if (!txt) return;
  txt.textContent = t("Запрашиваю информацию…");
  try {
    const r = await api("/api/system/info");
    if (!r.git_available) {
      txt.textContent = t("Git не найден или это не git-checkout: ") + (r.error || "—");
      return;
    }
    const behind = r.behind_count;
    let s = `${r.branch} · ${r.commit} · «${r.message || ""}»`;
    if (behind == null) s += t(" · (не могу проверить upstream)");
    else if (behind === 0) s += t(" · ✅ актуальная версия");
    else s += tf(" · ⬇️ доступно обновлений: {0}", behind);
    txt.textContent = s;
  } catch (e) {
    txt.textContent = tf("Ошибка: {0}", e.message);
  }
}

async function runGitUpdate() {
  if (!confirm("Подтянуть последний код из git и обновить зависимости?")) return;
  const restart = $("#updRestart").checked;
  const logEl = $("#updLog");
  const btn = $("#updPull");
  btn.disabled = true; btn.textContent = "⏳ Обновляю…";
  logEl.textContent = "";
  try {
    const r = await api("/api/system/update", { method: "POST", body: { restart } });
    let out = "";
    for (const s of r.steps || []) {
      out += `\n$ ${s.cmd}\n${s.ok ? "(ok)" : "(FAIL rc=" + s.rc + ")"}\n${s.out || ""}\n`;
    }
    out += `\nbefore: ${r.before}\nafter:  ${r.after}\nchanged: ${r.changed}`;
    if (r.restarting) out += `\n\n🔁 Сервис перезапускается через 1 сек…`;
    logEl.textContent = out.trim();
    if (r.ok && r.changed) toast(`Обновлено: ${r.before} → ${r.after}`, "ok");
    else if (r.ok) toast("Уже на последней версии", "ok");
    else toast(r.error || "Не удалось обновить", "err");
    refreshUpdateInfo();
  } catch (e) {
    logEl.textContent = "Ошибка: " + e.message;
    toast(e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "📥 Обновить";
  }
}
init().catch(e => toast(e.message, "err"));


// ---------- Telegram bridge (experimental) ----------

let TG_STATE = null;

function wireTelegramPanel() {
  // Toggle on the <summary> checkbox starts/stops the worker without saving config
  $("#tgEnabled")?.addEventListener("change", async (e) => {
    const on = e.target.checked;
    try {
      await api(on ? "/api/telegram/start" : "/api/telegram/stop", { method: "POST" });
      toast(on ? "Telegram-мост запущен" : "Telegram-мост остановлен", "ok");
    } catch (err) {
      toast(err.message, "err");
      // revert checkbox if the call failed
      e.target.checked = !on;
    }
    refreshTelegramStatus();
  });

  // Mode switcher — toggle the API-credentials block visibility
  $$('input[name="tgMode"]').forEach(r => {
    r.addEventListener("change", () => updateTgModeUi());
  });
  updateTgModeUi();

  $("#tgSave")?.addEventListener("click", saveTelegramConfig);
  $("#tgTest")?.addEventListener("click", testTelegramFetch);
  $("#tgTestSend")?.addEventListener("click", testTelegramSendMesh);
  $("#tgStart")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/telegram/start", { method: "POST" });
      if (!r.ok && r.error) toast(r.error, "err"); else toast("Запущено", "ok");
    } catch (e) { toast(e.message, "err"); }
    refreshTelegramStatus();
  });
  $("#tgStop")?.addEventListener("click", async () => {
    try {
      await api("/api/telegram/stop", { method: "POST" });
      toast("Остановлено", "ok");
    } catch (e) { toast(e.message, "err"); }
    refreshTelegramStatus();
  });
  $("#tgRefresh")?.addEventListener("click", refreshTelegramStatus);
}

function getTgMode() {
  const checked = document.querySelector('input[name="tgMode"]:checked');
  return checked ? checked.value : "web";
}

function updateTgModeUi() {
  const mode = getTgMode();
  const apiBlock = $("#tgApiBlock");
  const pollRow = $("#tgPollIntervalRow");
  const channelsHint = $("#tgChannelsHint");
  if (apiBlock) apiBlock.hidden = (mode !== "telethon");
  if (pollRow) pollRow.style.display = (mode === "web") ? "" : "none";
  if (channelsHint) {
    channelsHint.innerHTML = t(mode === "web"
      ? `По одному в строке. В режиме «Без API» — только <code>@username</code> публичных каналов.`
      : `По одному в строке. Можно <code>@username</code> или числовой ID канала (<code>-1001234567890</code>).`);
  }
}

async function saveTelegramConfig() {
  const mode = getTgMode();
  const apiId = parseInt($("#tgApiId").value, 10);
  const apiHash = $("#tgApiHash").value.trim();
  const channels = $("#tgChannels").value
    .split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const keywords = $("#tgKeywords").value
    .split(",").map(s => s.trim()).filter(Boolean);
  const geoFilter = $("#tgGeoFilter").value
    .split(",").map(s => s.trim()).filter(Boolean);
  const minIv = Math.max(0, parseInt($("#tgMinInterval").value, 10) || 60);
  const pollIv = Math.max(15, parseInt($("#tgPollInterval").value, 10) || 60);
  const prefix = $("#tgPrefix").value.trim() || "🚨 TG";

  const broadcastTo = $("#tgDest").value || "broadcast";
  const channelIndex = parseInt($("#tgChannelIndex").value, 10);
  const stripEmoji = $("#tgStripEmoji").checked;
  const includeSource = $("#tgIncludeSource").checked;
  const stripSelfSig = $("#tgStripSelfSig").checked;
  const blocklist = $("#tgBlocklist").value
    .split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const maxChars = parseInt($("#tgMaxChars").value, 10);
  const maxAts = parseInt($("#tgMaxAts").value, 10);
  const maxUrls = parseInt($("#tgMaxUrls").value, 10);
  const keepParas = parseInt($("#tgKeepParas").value, 10);
  const summarize = $("#tgSummarize").checked;
  const summarizeMin = parseInt($("#tgSummarizeMin").value, 10);
  const summarizeTarget = parseInt($("#tgSummarizeTarget").value, 10);

  const payload = {
    telegram: {
      mode,
      api_id: Number.isFinite(apiId) ? apiId : null,
      api_hash: apiHash,
      channels,
      keywords,
      geo_filter: geoFilter,
      min_interval_seconds: minIv,
      poll_interval_seconds: pollIv,
      forward_prefix: prefix,
      broadcast_to: broadcastTo,
      channel_index: Number.isFinite(channelIndex) ? channelIndex : 0,
      strip_emoji: stripEmoji,
      include_source: includeSource,
      strip_self_signature: stripSelfSig,
      blocklist_lines: blocklist,
      max_message_chars: Number.isFinite(maxChars) ? maxChars : 500,
      max_at_mentions: Number.isFinite(maxAts) ? maxAts : 5,
      max_urls: Number.isFinite(maxUrls) ? maxUrls : 3,
      keep_first_paragraphs: Number.isFinite(keepParas) ? keepParas : 0,
      summarize: summarize,
      summarize_min_chars: Number.isFinite(summarizeMin) ? summarizeMin : 200,
      summarize_target_chars: Number.isFinite(summarizeTarget) ? summarizeTarget : 100,
    }
  };
  try {
    await api("/api/config", { method: "POST", body: payload });
    toast("Сохранено", "ok");
  } catch (e) { toast(e.message, "err"); }
  refreshTelegramStatus();
}

async function testTelegramFetch() {
  // Proxy now lives in the central «Прокси» tab — the server uses it directly.
  const out = $("#tgTestResult");
  out.hidden = false;
  out.className = "tg-test-result";
  out.innerHTML = `<span class="muted">⏳ Пробую открыть t.me/s/durov…</span>`;
  try {
    const r = await api("/api/telegram/test", { method: "POST", body: { channel: "durov" } });
    const via = r.via_proxy ? `через прокси (${escapeHtml(r.proxy || "?")})` : "напрямую";
    const elapsed = r.elapsed_seconds != null ? `${r.elapsed_seconds.toFixed(1)} сек` : "—";
    if (r.ok) {
      const msgs = r.messages_seen != null ? `, сообщений распознано: ${r.messages_seen}` : "";
      out.className = "tg-test-result ok";
      out.innerHTML =
        `✅ <strong>Связь есть</strong> — ${via}, ${elapsed}` +
        `<div class="muted" style="margin-top:4px">HTTP ${r.status_code}, ${r.bytes} байт${msgs}</div>`;
    } else {
      out.className = "tg-test-result err";
      const why = r.error || r.hint || `HTTP ${r.status_code}`;
      out.innerHTML =
        `❌ <strong>Не удалось</strong> — ${via}, ${elapsed}` +
        `<div class="muted" style="margin-top:4px">${escapeHtml(why)}</div>`;
    }
  } catch (e) {
    out.className = "tg-test-result err";
    out.innerHTML = `❌ ${escapeHtml(e.message)}`;
  }
  refreshTelegramStatus();
}

let TG_SEEN_FILTER = "all";

function renderTgSeen(items) {
  const el = $("#tgSeen");
  const stats = $("#tgSeenStats");
  if (!el) return;
  const filter = $("#tgSeenFilter")?.value || "all";
  TG_SEEN_FILTER = filter;
  // Aggregate counts for the stats badge
  const counts = { forwarded: 0, throttled: 0, no_keyword: 0, no_geo: 0, test: 0 };
  for (const it of items) counts[it.status] = (counts[it.status] || 0) + 1;
  if (stats) {
    stats.textContent =
      `всего ${items.length} · ✅ ${counts.forwarded||0} · ⏸ ${counts.throttled||0} · ⊘ ${counts.no_keyword||0} · 📍 ${counts.no_geo||0}`;
  }
  const filtered = filter === "all" ? items : items.filter(i => i.status === filter);
  if (!filtered.length) {
    el.innerHTML = `<div class="muted">${items.length === 0 ? "Пока ничего." : "По выбранному фильтру пусто."}</div>`;
    return;
  }
  el.innerHTML = filtered.map(it => {
    const time = new Date(it.ts * 1000).toLocaleTimeString();
    const date = new Date(it.ts * 1000).toLocaleDateString();
    const statusBadge = {
      forwarded:  `<span class="tg-seen-badge ok">✅ отправлено</span>`,
      throttled:  `<span class="tg-seen-badge warn">⏸ задросселлено</span>`,
      no_keyword: `<span class="tg-seen-badge mute">⊘ нет ключа</span>`,
      no_geo:     `<span class="tg-seen-badge mute">📍 не прошёл гео</span>`,
      spam_filter:`<span class="tg-seen-badge warn">🛑 спам-фильтр</span>`,
      test:       `<span class="tg-seen-badge ok">🧪 тест</span>`,
    }[it.status] || `<span class="tg-seen-badge mute">${escapeHtml(it.status)}</span>`;
    const kwInfo = it.keyword
      ? `<span class="tg-seen-meta">«${escapeHtml(it.keyword)}»${it.geo ? ` · 📍${escapeHtml(it.geo)}` : ""}</span>`
      : "";
    return `<div class="tg-seen-item">
      <div class="tg-seen-head">
        ${statusBadge}
        <span class="tg-seen-channel">${escapeHtml(it.channel)}</span>
        ${kwInfo}
        <span class="tg-seen-ts muted">${date} ${time}</span>
      </div>
      <div class="tg-seen-text">${escapeHtml(it.text || "")}</div>
    </div>`;
  }).join("");
}

// Re-render when the filter changes (use cached state — refresh comes from poll)
document.addEventListener("change", (e) => {
  if (e.target.id === "tgSeenFilter") {
    if (TG_STATE) renderTgSeen(TG_STATE.recent_seen || []);
  }
});

async function testTelegramSendMesh() {
  if (!confirm("Отправить тестовое сообщение в выбранный канал/адресат mesh?")) return;
  const btn = $("#tgTestSend");
  if (btn) { btn.disabled = true; btn.textContent = "Отправляю…"; }
  try {
    const r = await api("/api/telegram/test_send", { method: "POST", body: {} });
    if (r.ok) {
      const dest = r.broadcast_to === "broadcast" ? "broadcast" : r.broadcast_to;
      toast(`Тест отправлен · канал ${r.channel_index} · ${dest}`, "ok");
    } else {
      toast("Ошибка: " + (r.error || "неизвестно"), "err");
    }
  } catch (e) {
    toast(e.message, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🧪 Тест в mesh"; }
  }
  refreshTelegramStatus();
}

async function refreshTelegramStatus() {
  // Make sure the channel/destination dropdowns are filled even if the user
  // opened "Прочее" without the dashboard having loaded (falls back to 0–7).
  populateTgChannelSelect();
  if (KNOWN_NODES.length) populateDestinationSelectors(KNOWN_NODES);
  try {
    TG_STATE = await api("/api/telegram/status");
  } catch (e) {
    setTgStatus("err", `Ошибка: ${e.message}`);
    return;
  }
  const s = TG_STATE;
  const mode = s.config?.mode || s.mode || "web";
  const modeLbl = mode === "telethon" ? "API" : t("Без API");

  // Pretty status dot + text
  if (s.running) {
    const matched = s.matched_count ?? 0;
    const who = mode === "telethon" ? ` · ${s.username || "—"}` : "";
    setTgStatus("ok", tf("Работает ({0}){1} · совпадений: {2}", modeLbl, who, matched));
  } else if (mode === "telethon" && !s.telethon_available) {
    setTgStatus("err", t("Библиотека telethon не установлена. Запусти на Pi: pip install telethon"));
  } else if (mode === "telethon" && !s.session_exists) {
    setTgStatus("warn", t("Сессия не создана. SSH в Pi и запусти python telegram_setup.py"));
  } else if (s.last_error) {
    setTgStatus("err", s.last_error);
  } else {
    setTgStatus("idle", tf("Мост остановлен ({0})", modeLbl));
  }
  // Reflect checkbox state to match worker state
  const cb = $("#tgEnabled");
  if (cb) cb.checked = !!s.running;

  // Populate config fields (only if currently empty — avoid stomping user typing)
  const cfg = s.config || {};
  // Mode radio reflects persisted config
  const modeFromCfg = cfg.mode || "web";
  const modeRadio = document.querySelector(`input[name="tgMode"][value="${modeFromCfg}"]`);
  if (modeRadio && !modeRadio.checked) {
    modeRadio.checked = true;
    updateTgModeUi();
  }
  if (!$("#tgApiId").value && cfg.api_id_set) $("#tgApiId").placeholder = "(сохранено)";
  if (!$("#tgApiHash").value && cfg.api_hash_set) $("#tgApiHash").placeholder = "(сохранено)";
  if (!$("#tgChannels").value) $("#tgChannels").value = (cfg.channels || []).join("\n");
  if (!$("#tgKeywords").value) $("#tgKeywords").value = (cfg.keywords || []).join(", ");
  if (!$("#tgGeoFilter").value && (cfg.geo_filter || []).length) {
    $("#tgGeoFilter").value = (cfg.geo_filter || []).join(", ");
  }
  if (cfg.min_interval_seconds != null && $("#tgMinInterval").value === "60") {
    $("#tgMinInterval").value = cfg.min_interval_seconds;
  }
  if (cfg.poll_interval_seconds != null && $("#tgPollInterval").value === "60") {
    $("#tgPollInterval").value = cfg.poll_interval_seconds;
  }
  if (cfg.forward_prefix && $("#tgPrefix").value === "🚨 TG") {
    $("#tgPrefix").value = cfg.forward_prefix;
  }
  if ($("#tgStripEmoji") && typeof cfg.strip_emoji === "boolean") {
    $("#tgStripEmoji").checked = cfg.strip_emoji;
  }
  if ($("#tgIncludeSource") && typeof cfg.include_source === "boolean") {
    $("#tgIncludeSource").checked = cfg.include_source;
  }
  if ($("#tgStripSelfSig") && typeof cfg.strip_self_signature === "boolean") {
    $("#tgStripSelfSig").checked = cfg.strip_self_signature;
  }
  if ($("#tgBlocklist") && !$("#tgBlocklist").value && (cfg.blocklist_lines || []).length) {
    $("#tgBlocklist").value = (cfg.blocklist_lines || []).join("\n");
  }
  if ($("#tgMaxChars") && cfg.max_message_chars != null && $("#tgMaxChars").value === "500") {
    $("#tgMaxChars").value = cfg.max_message_chars;
  }
  if ($("#tgMaxAts") && cfg.max_at_mentions != null && $("#tgMaxAts").value === "5") {
    $("#tgMaxAts").value = cfg.max_at_mentions;
  }
  if ($("#tgMaxUrls") && cfg.max_urls != null && $("#tgMaxUrls").value === "3") {
    $("#tgMaxUrls").value = cfg.max_urls;
  }
  if ($("#tgKeepParas") && cfg.keep_first_paragraphs != null && $("#tgKeepParas").value === "0") {
    $("#tgKeepParas").value = cfg.keep_first_paragraphs;
  }
  if ($("#tgSummarize") && typeof cfg.summarize === "boolean") {
    $("#tgSummarize").checked = cfg.summarize;
  }
  if ($("#tgSummarizeMin") && cfg.summarize_min_chars != null && $("#tgSummarizeMin").value === "200") {
    $("#tgSummarizeMin").value = cfg.summarize_min_chars;
  }
  if ($("#tgSummarizeTarget") && cfg.summarize_target_chars != null && $("#tgSummarizeTarget").value === "100") {
    $("#tgSummarizeTarget").value = cfg.summarize_target_chars;
  }

  // Make sure the destination + channel selects have the freshest options
  // available, then restore the persisted selection.
  if ($("#tgDest") && (!$("#tgDest").value || $("#tgDest").value === "broadcast")) {
    if (cfg.broadcast_to) $("#tgDest").value = cfg.broadcast_to;
  }
  if ($("#tgChannelIndex") && cfg.channel_index != null) {
    const want = String(cfg.channel_index);
    if (Array.from($("#tgChannelIndex").options).some(o => o.value === want)) {
      $("#tgChannelIndex").value = want;
    }
  }

  // Render the debug seen-feed (every parsed message, with reason)
  renderTgSeen(s.recent_seen || []);

  // Render recent matches
  const hist = $("#tgHistory");
  if (hist) {
    const items = s.recent_matches || [];
    if (!items.length) {
      hist.innerHTML = `<div class="muted">Ничего пока не было.</div>`;
    } else {
      hist.innerHTML = items.map(m => {
        const ts = new Date(m.ts * 1000).toLocaleString();
        const throttled = m.throttled
          ? `<span class="tg-throttled" title="не отправлено в mesh из-за паузы">⏸ паузой</span>`
          : "";
        const preview = (m.text || "").replace(/\s+/g, " ").slice(0, 240);
        return `<div class="tg-match">
          <div class="tg-match-head">
            <span class="tg-match-channel">📡 ${escapeHtml(m.channel)}</span>
            <span class="tg-match-kw">«${escapeHtml(m.keyword)}»</span>
            <span class="tg-match-ts muted">${ts}</span>
            ${throttled}
          </div>
          <div class="tg-match-text">${escapeHtml(preview)}</div>
        </div>`;
      }).join("");
    }
  }
}

function setTgStatus(kind, text) {
  const dot = $("#tgStatusDot");
  const txt = $("#tgStatusText");
  if (dot) dot.className = `tg-dot tg-dot-${kind}`;
  if (txt) txt.textContent = text;
}

// ---------- Ether rail (persistent broadcast feed on wide screens) ----------
// Renders straight from ALL_MESSAGES (maintained by pollChat) — no extra
// network traffic, no duplicate notification sounds.

// Channel picker for rail sends: real channel names when known, 0–7 fallback.
// Selection is remembered so «куда пишу» is always explicit and stable.
function populateRailChannel() {
  const sel = $("#railChannel");
  if (!sel) return;
  const prev = sel.value || (() => { try { return localStorage.getItem("railChannel") || "0"; } catch (e) { return "0"; } })();
  sel.innerHTML = "";
  const chans = (KNOWN_CHANNELS || []).filter(c => c && c.index != null);
  if (chans.length) {
    for (const ch of chans) {
      const opt = document.createElement("option");
      opt.value = String(ch.index);
      opt.textContent = `ch${ch.index}${ch.name ? " · " + ch.name : ""}${ch.role === "primary" ? " ★" : ""}`;
      sel.appendChild(opt);
    }
  } else {
    for (let i = 0; i < 8; i++) {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = `ch${i}`;
      sel.appendChild(opt);
    }
  }
  if (prev && Array.from(sel.options).some(o => o.value === prev)) sel.value = prev;
}

(function initEtherRail() {
  const rail = $("#etherRail");
  if (!rail) return;

  populateRailChannel();   // fallback 0–7 until /api/channels arrives
  $("#railChannel")?.addEventListener("change", (e) => {
    try { localStorage.setItem("railChannel", e.target.value); } catch (err) {}
  });

  const apply = (on) => {
    document.documentElement.classList.toggle("rail-off", !on);
    $("#railToggle")?.classList.toggle("active", on);
  };
  let on = true;
  try { on = localStorage.getItem("etherRail") !== "off"; } catch (e) {}
  apply(on);
  const setState = (v) => {
    try { localStorage.setItem("etherRail", v ? "on" : "off"); } catch (e) {}
    apply(v);
  };
  $("#railToggle")?.addEventListener("click", () =>
    setState(document.documentElement.classList.contains("rail-off")));
  $("#railCollapse")?.addEventListener("click", () => setState(false));

  let lastRendered = 0;
  const isBroadcast = (m) => !m.is_reaction &&
    (!m.to_id || m.to_id === "broadcast" || m.to_id === "^all");

  function render() {
    if (rail.offsetParent === null) return;         // hidden (collapsed/narrow)
    const feed = $("#railFeed");
    if (!feed) return;
    let fresh = ALL_MESSAGES.filter(m => isBroadcast(m) && m.id > lastRendered);
    if (!fresh.length) return;
    if (lastRendered === 0) {
      feed.innerHTML = "";
      fresh = fresh.slice(-40);                     // first paint: newest 40
      if (fresh.length) lastRendered = 0;           // ids set below
    }
    for (const m of fresh) {
      if (m.id > lastRendered) lastRendered = m.id;
      const div = document.createElement("div");
      div.className = "er-item" + (m.incoming ? "" : " out");
      const time = new Date((m.time || 0) * 1000)
        .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      div.innerHTML =
        `<div class="er-meta"><span class="er-from">${escapeHtml(m.from_name || m.from_id || "?")}</span>` +
        `<span class="er-ch">ch${m.channel ?? 0}</span>` +
        `<span class="er-time">${time}</span></div>` +
        `<div class="er-text">${linkify(m.text)}</div>`;
      feed.appendChild(div);
    }
    while (feed.children.length > 60) feed.removeChild(feed.firstChild);
    feed.scrollTop = feed.scrollHeight;
  }

  render();
  setInterval(render, 3000);

  async function railSend() {
    const inp = $("#railInput");
    const text = (inp?.value || "").trim();
    if (!text) return;
    const channel = parseInt($("#railChannel")?.value, 10) || 0;
    try {
      await api("/api/chat/send", { method: "POST", body: { text, destination: "broadcast", channel } });
      inp.value = "";
      pollChat();          // pick the sent message up quickly
      setTimeout(render, 400);
    } catch (e) { toast(e.message, "err"); }
  }
  $("#railSend")?.addEventListener("click", railSend);
  $("#railInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") railSend(); });
})();
