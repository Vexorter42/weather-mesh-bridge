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
  return parts.length ? `<div class="rf-meta">${parts.join(" · ")}</div>` : "";
}

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
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
let CURRENT_TAB = "settings";
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
    }
  });
});

// ---------- Mesh status ----------
async function refreshMeshStatus() {
  const el = $("#meshStatus");
  try {
    const s = await api("/api/mesh/status");
    const where = s.connection_type === "tcp"
      ? `${s.tcp_host || "—"}:${s.tcp_port || 4403}`
      : (s.resolved_path || "не найден");
    if (s.connected) {
      el.className = "status ok";
      el.textContent = `📡 ${where} · узлов: ${s.nodes_known ?? 0}`;
    } else {
      el.className = "status";
      el.textContent = `📡 ${where} · отключён`;
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
  CONFIG = await api("/api/config", { method: "POST", body: { message } });
  toast("Стиль сообщений сохранён", "ok");
}
$("#useEmojis").addEventListener("change", saveMessageStyle);
$("#includeHeader").addEventListener("change", saveMessageStyle);

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

// ---------- Chat ----------
let LAST_MSG_ID = 0;
let UNREAD = 0;

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
  const t = new Date(m.time * 1000).toLocaleTimeString();
  // Action buttons: reply + reaction. Both need a real mesh msg_id to target.
  const actions = m.msg_id
    ? `<button class="reply-btn" title="Ответить" data-msg-id="${m.msg_id}">↩</button>` +
      `<button class="react-btn" title="Поставить реакцию" data-msg-id="${m.msg_id}">+</button>`
    : "";
  div.innerHTML =
    `<div class="meta">` +
      `<span class="from">${escapeHtml(m.from_name || "?")}</span>` +
      `<span class="ch">ch${m.channel ?? 0}</span>` +
      `<span>${t}</span>` +
      actions +
    `</div>` +
    buildReplyQuote(m, log) +
    `<div class="text">${escapeHtml(m.text)}</div>` +
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
    const data = await api(`/api/chat/messages?since=${LAST_MSG_ID}`);
    if (!data.messages?.length) return;
    const firstPoll = LAST_MSG_ID === 0;
    for (const m of data.messages) {
      appendChatMessage(m);
      if (m.id > LAST_MSG_ID) LAST_MSG_ID = m.id;
      // Reactions don't bump the unread badge — they update an existing message.
      if (m.incoming && !m.is_reaction && CURRENT_TAB !== "chat") {
        UNREAD += 1;
      }
      // Browser notification for new incoming text messages only.
      // Skip notifying for the initial backlog when page just loaded.
      if (!firstPoll && m.incoming && !m.is_reaction) {
        notifyIncoming(m);
      }
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

function notifyIncoming(m) {
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

// ---------- Reaction picker ----------
const QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "👀"];
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
  picker.innerHTML = QUICK_REACTIONS
    .map(e => `<button data-emoji="${escapeHtml(e)}">${e}</button>`)
    .join("");
  reactionPickerEl = picker;

  // Position picker near the button
  document.body.appendChild(picker);
  const rect = btn.getBoundingClientRect();
  const pw = picker.offsetWidth;
  let left = rect.left + window.scrollX + rect.width / 2 - pw / 2;
  // keep within viewport
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  const top = rect.bottom + window.scrollY + 6;
  picker.style.left = left + "px";
  picker.style.top = top + "px";

  picker.addEventListener("click", async (e) => {
    const tgt = e.target.closest("button[data-emoji]");
    if (!tgt) return;
    const emoji = tgt.dataset.emoji;
    const reply_to = reactionTargetMsgId;
    closeReactionPicker();
    if (!emoji || !reply_to) return;
    try {
      await api("/api/chat/react", {
        method: "POST",
        body: { emoji, reply_to: parseInt(reply_to, 10) },
      });
      // Don't toast — the chip will appear under the message via pollChat.
      pollChat();
    } catch (err) { toast(err.message, "err"); }
  });
}

document.addEventListener("click", (e) => {
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
  try {
    if (REPLY_TO) {
      await api("/api/chat/reply", {
        method: "POST",
        body: { text, reply_to: REPLY_TO.msg_id },
      });
      cancelReply();
    } else {
      await api("/api/chat/send", { method: "POST", body: { text } });
    }
    $("#chatInput").value = "";
    pollChat();
  } catch (e) { toast(e.message, "err"); }
});
$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#chatSend").click(); }
  if (e.key === "Escape" && REPLY_TO) { e.preventDefault(); cancelReply(); }
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
  updateConnectionFields();
  renderCurrentCity();
  buildDayButtons($("#newDays"), DAYS.map(d => d.k));
  buildFieldChips($("#newFields"), ALL_FIELDS.map(f => f.key));
  buildFieldChips($("#manualFields"), ALL_FIELDS.map(f => f.key));
  await refreshSlots();
  refreshMeshStatus();
  refreshNotifUi();
  pollChat();
  setInterval(refreshMeshStatus, 15000);
  setInterval(pollChat, 4000);
}
init().catch(e => toast(e.message, "err"));
