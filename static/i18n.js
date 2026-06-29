/* Lightweight i18n for the (Russian-first) UI.
 *
 * Strategy: Russian text stays in the HTML as the source. The dictionary below
 * maps each Russian string → English. When EN is selected we walk the DOM once
 * and translate text nodes + placeholder/title/aria-label attributes; dynamic
 * strings in app.js go through the global t() with the same dictionary.
 * Russian is the default and a no-op (zero overhead, nothing to translate).
 *
 * Switching language reloads the page (simplest correct way to re-render).
 */
(function () {
  "use strict";
  const KEY = "lang";
  function getLang() { try { return localStorage.getItem(KEY) || "ru"; } catch (e) { return "ru"; } }
  const LANG = getLang();

  // ru → en. Keys are the trimmed Russian source strings (emoji kept).
  const EN = {
    // ---- nav / header ----
    "Главная": "Home",
    "Карта": "Map",
    "Настройки": "Settings",
    "Чат": "Chat",
    "Прочее": "More",
    "Прокси": "Proxy",
    "Светлая / тёмная тема": "Light / dark theme",
    "Переключить тему": "Toggle theme",
    "Переключить язык": "Switch language",

    // ---- card headings (Home) ----
    "🌤 Погода сейчас": "🌤 Weather now",
    "📈 Почасовой прогноз (24ч)": "📈 Hourly forecast (24h)",
    "📈 За последние сутки": "📈 Last 24 hours",
    "📡 Загрузка эфира (LoRa)": "📡 Airtime load (LoRa)",
    "🕒 Последняя активность": "🕒 Recent activity",
    "🛰 Узлы mesh-сети": "🛰 Mesh nodes",
    // ---- headings (Map) ----
    "🗺 Карта узлов": "🗺 Node map",
    // ---- headings (Settings) ----
    "📍 Город": "📍 City",
    "✉️ Стиль сообщения": "✉️ Message style",
    "📡 Heltec V4 (Meshtastic)": "📡 Heltec V4 (Meshtastic)",
    "⚠️ Предупреждения о погоде": "⚠️ Weather alerts",
    "🕒 Расписание": "🕒 Schedule",
    "🚀 Отправить сейчас": "🚀 Send now",
    // ---- headings (Misc) ----
    "🩺 Состояние системы": "🩺 System health",
    "💾 Бэкап настроек": "💾 Settings backup",
    "🧪 Экспериментальное": "🧪 Experimental",
    // ---- headings (Proxy) ----
    "🌐 Прокси": "🌐 Proxy",

    // ---- profile-section sub-headers ----
    "Подключение": "Connection",
    "Поведение": "Behaviour",
    "Настройка бота (одноразово)": "Bot setup (one-time)",
    "Параметры обновления": "Update settings",
    "Адрес прокси": "Proxy address",
    "Что пускать через прокси": "What goes through the proxy",

    // ---- common buttons ----
    "Сохранить": "Save",
    "Обновить": "Refresh",
    "Проверить связь": "Test connection",
    "Проверить сейчас": "Check now",
    "Отправить": "Send",
    "Найти": "Find",
    "Предпросмотр": "Preview",
    "Создать слот": "Add slot",
    "Спросить": "Ask",
    "⛔ Отключить": "⛔ Disconnect",
    "⚙️ Настройки Heltec": "⚙️ Heltec settings",
    "↻ Обновить": "↻ Refresh",
    "🔍 Проверить связь": "🔍 Test connection",
    "🌧 Проверить сейчас": "🌧 Check now",
    "💾 Сохранить": "💾 Save",
    "⬇️ Скачать бэкап": "⬇️ Download backup",
    "⬆️ Восстановить из файла": "⬆️ Restore from file",
    "↻ Проверить обновления": "↻ Check for updates",
    "📥 Обновить": "📥 Update",
    "▶ Запустить бота": "▶ Start bot",
    "⏹ Остановить": "⏹ Stop",
    "🔍 Проверить": "🔍 Test",
    "📥 Загрузить выходы": "📥 Load exits",
    "🌍 Переключить": "🌍 Switch",
    "Запросить разрешение": "Request permission",
  };

  function tr(s) {
    if (LANG !== "en" || !s) return s;
    const hit = EN[String(s).trim()];
    return hit || s;
  }
  // Exposed for app.js dynamic strings.
  window.t = tr;
  window.__lang = LANG;

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "CODE", "TEXTAREA", "NOSCRIPT", "OPTION"]);

  function translateAttr(attr) {
    document.querySelectorAll("[" + attr + "]").forEach((el) => {
      const cur = el.getAttribute(attr);
      const en = EN[(cur || "").trim()];
      if (en) el.setAttribute(attr, en);
    });
  }

  function apply() {
    if (LANG !== "en") return;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        if (n.parentElement && SKIP_TAGS.has(n.parentElement.tagName)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    for (const node of nodes) {
      const raw = node.nodeValue;
      const en = EN[raw.trim()];
      if (en && en !== raw.trim()) {
        const lead = (raw.match(/^\s*/) || [""])[0];
        const trail = (raw.match(/\s*$/) || [""])[0];
        node.nodeValue = lead + en + trail;
      }
    }
    translateAttr("placeholder");
    translateAttr("title");
    translateAttr("aria-label");
    document.documentElement.setAttribute("lang", "en");
  }

  document.addEventListener("DOMContentLoaded", () => {
    apply();
    const btn = document.getElementById("langToggle");
    if (btn) {
      btn.textContent = LANG === "en" ? "RU" : "EN";
      btn.addEventListener("click", () => {
        try { localStorage.setItem(KEY, LANG === "en" ? "ru" : "en"); } catch (e) {}
        location.reload();
      });
    }
  });
})();
