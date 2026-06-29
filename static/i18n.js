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

    // ---- Home / dashboard ----
    "Навигация": "Navigation",
    "узлов": "nodes",
    "отправителей · 24ч": "senders · 24h",
    "сообщений всего": "messages total",
    "Загружаю прогноз…": "Loading forecast…",
    "Загружаю график…": "Loading chart…",
    "температура °C ·": "temperature °C ·",
    "вероятность осадков %": "precipitation chance %",
    "Отправлено": "Sent",
    "сводок и сообщений": "summaries and messages",
    "Получено": "Received",
    "из mesh-сети": "from the mesh",
    "Средний RSSI": "Average RSSI",
    "сила сигнала входящих": "signal strength of incoming",
    "Средние прыжки": "Average hops",
    "сколько hops в среднем": "average hop count",
    "Сколько эфира занято в твоём канале. Выше ~25% — пакеты начинают теряться и растут задержки; бот сам притормаживает рассылки.": "How busy your channel's airtime is. Above ~25% packets start dropping and latency grows; the bot throttles its own broadcasts.",
    "Занятость канала": "Channel utilization",
    "Наша передача (air-util TX)": "Our transmit (air-util TX)",
    "Пакетов ↑ за час": "Packets ↑ per hour",
    "— за сутки": "— per day",
    "Пакетов ↓ за час": "Packets ↓ per hour",
    "↑ Бот отправил:": "↑ Bot sent:",
    "↓ Бот получил:": "↓ Bot received:",
    "Последние известные ноды, в порядке свежести. Кликни узел — откроется его профиль с телеметрией.": "Most recently heard nodes, freshest first. Click a node to open its profile with telemetry.",
    "Загрузка…": "Loading…",

    // ---- Map ----
    "Показываю узлы mesh-сети, у которых есть позиция. Они приходят, когда нода присылает": "Showing mesh nodes that have a position. These arrive when a node sends",
    ". В Meshtastic-приложении выставь Position → Smart broadcast, чтобы соседи делились координатами.": ". In the Meshtastic app set Position → Smart broadcast so neighbours share coordinates.",
    "📶 Покрытие (SNR)": "📶 Coverage (SNR)",
    "🌧 Радар осадков": "🌧 Precipitation radar",
    "Запустить анимацию": "Play animation",

    // ---- Settings: city / message / commands ----
    "Поиск работает по любому языку. Выбери из списка — координаты подставятся автоматически.": "Search works in any language. Pick from the list — coordinates fill in automatically.",
    "Например: Москва, Сочи, Berlin…": "e.g. Moscow, Sochi, Berlin…",
    "Применяется и к ручной отправке, и к расписанию.": "Applies to both manual sends and the schedule.",
    "Использовать смайлики в сообщениях": "Use emojis in messages",
    "Добавлять заголовок «Погода — Город»": "Add a “Weather — City” header",
    "Отвечать на команды в чате (/погода, /ping, /nodes, /help). `!` тоже работает.": "Reply to chat commands (/погода, /ping, /nodes, /help). `!` works too.",
    "⏱ Пауза перед ответом на команду (случайная в диапазоне) — мгновенный ответ часто «сталкивается» в LoRa-эфире и теряется. 5–10 сек дают каналу освободиться.": "⏱ Delay before replying to a command (random within the range) — an instant reply often “collides” on the LoRa air and is lost. 5–10 s lets the channel clear.",
    "Задержка ответа от, сек": "Reply delay from, sec",
    "до, сек": "to, sec",

    // ---- Settings: Heltec ----
    "Тип подключения": "Connection type",
    "USB-кабель": "USB cable",
    "Устройство": "Device",
    "Подключи Heltec к WiFi через приложение Meshtastic (Module Config → MQTT/HTTP/WiFi). Узнай IP в том же приложении или на роутере. Порт по умолчанию 4403.": "Connect the Heltec to WiFi via the Meshtastic app (Module Config → MQTT/HTTP/WiFi). Find its IP in the same app or on your router. Default port is 4403.",
    "IP адрес Heltec": "Heltec IP address",
    "Порт": "Port",
    "Канал": "Channel",
    "(индекс канала Meshtastic, обычно 0)": "(Meshtastic channel index, usually 0)",
    "Пауза между частями длинного сообщения, сек": "Delay between parts of a long message, sec",
    "(LoRa-эфир медленный — для длинных сводок ставь 8–15 сек)": "(LoRa air is slow — for long summaries use 8–15 sec)",

    // ---- Settings: alerts ----
    "Раз в N минут бот проверяет прогноз. При выполнении любого условия — шлёт алерт в mesh (один раз в сутки на тип). Безопасный спам-фильтр: даже если условия держатся весь день, каждое предупреждение придёт максимум один раз за календарные сутки.": "Every N minutes the bot checks the forecast. When any condition is met it sends an alert to the mesh (once per type per day). Safe spam filter: even if conditions persist all day, each alert is sent at most once per calendar day.",
    "Включить предупреждения": "Enable alerts",
    "Гроза (по текущей погоде)": "Thunderstorm (from current weather)",
    "Туман (по WMO-коду или видимости ниже порога)": "Fog (by WMO code or visibility below threshold)",
    "Гололёд (ледяной дождь / осадки при 0°C / перепад через 0°C)": "Ice (freezing rain / precipitation at 0°C / crossing 0°C)",
    "Сильный ветер от, м/с": "Strong wind from, m/s",
    "Вероятность осадков от, %": "Precipitation chance from, %",
    "Заморозки до, °C": "Frost down to, °C",
    "Жара от, °C": "Heat from, °C",
    "Туман от видимости ниже, м": "Fog when visibility below, m",
    "Интервал проверки, мин": "Check interval, min",

    // ---- Settings: schedule / send ----
    "Несколько слотов с собственным набором полей. Например: 08:00 — короткая сводка, 21:00 — прогноз на завтра (утро и вечер).": "Several slots, each with its own set of fields. e.g. 08:00 — short summary, 21:00 — tomorrow's forecast (morning and evening).",
    "+ Добавить слот": "+ Add slot",
    "Время": "Time",
    "Часовой пояс": "Time zone",
    "Europe/Moscow (МСК)": "Europe/Moscow (MSK)",

    // ---- Chat ----
    "Уведомления": "Notifications",
    "🔊 Звук": "🔊 Sound",
    "Проиграть тестовый звук": "Play a test sound",
    "🔎 Поиск в чате…": "🔎 Search chat…",
    "Сбросить поиск": "Clear search",
    "Чаты": "Chats",
    "Назад к списку": "Back to list",
    "Выберите чат слева": "Pick a chat on the left",
    "Выберите канал или собеседника в списке слева.": "Pick a channel or contact from the list on the left.",
    "↪ Ответ для": "↪ Reply to",
    "Отменить ответ": "Cancel reply",
    "Выбери чат и начни писать…  (Shift+Enter — новая строка)": "Pick a chat and start typing…  (Shift+Enter — new line)",

    // ---- Misc: health / backup ----
    "Сводка «всё ли живо»: нода, погода, прокси, диск. Обновляется при открытии вкладки.": "A quick “is everything alive” summary: node, weather, proxy, disk. Refreshes when you open the tab.",
    "Загружаю…": "Loading…",
    "Скачай бэкап перед переездом/обновлением — внутри": "Download a backup before migrating/updating — it contains",
    "(ключи, прокси, Telegram), пресеты и состояние алертов. Восстановление перезапишет настройки и перезапустит сервис. Авто-бэкап делается раз в сутки (хранятся 14 последних на малине, папка": "(keys, proxy, Telegram), presets and alert state. Restoring overwrites settings and restarts the service. An auto-backup runs daily (the last 14 are kept on the Pi, folder",
    "Включить базы данных (чат + история телеметрии) — архив крупнее": "Include databases (chat + telemetry history) — larger archive",
    "Фичи в разработке. Включай только то, что понимаешь — оно может ломаться, есть лишние зависимости и т.п.": "Features in development. Enable only what you understand — it may break, need extra dependencies, etc.",

    // ---- Misc: nowcast ----
    "Радар-нокаст «дождь идёт к тебе»": "Rain nowcast “rain is coming”",
    "— заранее предупреждает в mesh о приближающихся осадках": "— warns the mesh in advance about approaching precipitation",
    "Берёт поминутный прогноз осадков Open-Meteo для твоих координат и, если дождь/снег приближается в ближайший час, шлёт короткое сообщение:": "Takes Open-Meteo's per-minute precipitation forecast for your coordinates and, if rain/snow is approaching within the hour, sends a short message:",
    ". Один и тот же подход анонсируется один раз (тихий интервал). Координаты берутся из «Настроек».": ". The same approaching event is announced once (quiet window). Coordinates come from “Settings”.",
    "Загружаю состояние…": "Loading status…",
    "Проверять каждые": "Check every",
    "(мин)": "(min)",
    "Смотреть вперёд": "Look ahead",
    "Порог осадков": "Precipitation threshold",
    "(мм за 15 мин)": "(mm per 15 min)",
    "Не повторять": "Don't repeat for",
    "Предупреждать, если дождь": "Warn if rain is",
    "уже идёт": "already falling",
    "(и сколько ещё продлится)": "(and how much longer it lasts)",

    // ---- Misc: LLM ----
    "ИИ-ассистент": "AI assistant",
    "— команда": "— the command",
    "в чате (OpenAI-совместимый API)": "in chat (OpenAI-compatible API)",
    "Любой в mesh-сети пишет": "Anyone on the mesh types",
    "— бот спрашивает LLM и шлёт краткий ответ в эфир. Работает с любым OpenAI-совместимым API: NVIDIA build, OpenAI, OpenRouter, локальный Ollama и т.п.": "— the bot asks the LLM and sends a short answer over the air. Works with any OpenAI-compatible API: NVIDIA build, OpenAI, OpenRouter, local Ollama, etc.",
    "→ ключ вида": "→ a key like",
    ", модель": ", model",
    "(без «размышлений» — отвечает сразу).": "(no “reasoning” — answers immediately).",
    "— reasoning-модель: думает долго, нужен": "— a reasoning model: thinks long, needs",
    "≥ 1500. Для эфира лучше": "≥ 1500. For the air, prefer",
    "API ключ": "API key",
    "(хранится локально в config.json, не в git)": "(stored locally in config.json, not in git)",
    "Модель": "Model",
    "Запасные модели": "Fallback models",
    "(через запятую — пробуются по очереди, если основная упала/пустая)": "(comma-separated — tried in order if the primary fails/returns empty)",
    "🌐 Прокси для LLM-API настраивается во вкладке": "🌐 Proxy for the LLM API is set on the",
    "(тумблер «LLM»).": "tab (the “LLM” toggle).",
    "System-промпт": "System prompt",
    "(инструкция модели — держи ответы короткими для LoRa!)": "(model instruction — keep answers short for LoRa!)",
    "Макс. токенов ответа": "Max answer tokens",
    "Макс. символов в mesh": "Max chars in mesh",
    "(жёсткий обрез)": "(hard cut-off)",
    "(0 — строго, 1 — креативно)": "(0 — strict, 1 — creative)",
    "Помнить контекст диалога": "Remember conversation context",
    "— бот держит последние реплики каждого узла (30 мин), чтобы понимать уточняющие вопросы по": "— the bot keeps each node's recent messages (30 min) to understand follow-up questions for",
    "Пробный вопрос": "Test question",
    "(только в браузере, не в эфир)": "(browser only, not over the air)",
    "Спроси что-нибудь…": "Ask something…",

    // ---- Misc: git update ----
    "🔄 Обновление из git": "🔄 Update from git",
    "— подтянуть свежий код с GitHub": "— pull the latest code from GitHub",
    "Тянет последнюю версию бота из git-репозитория. Если изменился": "Pulls the latest bot version from the git repo. If",
    "— переустанавливает зависимости. Опционально перезапускает сам сервис (нужен systemd с": "changed, it reinstalls dependencies. Optionally restarts the service itself (needs systemd with",
    "или": "or",
    "Загружаю инфо…": "Loading info…",
    "Перезапустить сервис после обновления": "Restart the service after updating",

    // ---- Misc: status bot ----
    "Статус-бот в Telegram-чате": "Status bot in a Telegram chat",
    "— редактирует закреплённое сообщение с погодой и онлайн-статусом": "— edits a pinned message with weather and online status",
    "Бот живёт в указанном чате и каждые N секунд переписывает заранее закреплённое сообщение свежими данными: онлайн-статусом, текущей погодой, статистикой mesh. Когда бот падает — сообщение остаётся со старым временем, а по желанию systemd-хук помечает его как": "The bot lives in the given chat and every N seconds rewrites a pre-pinned message with fresh data: online status, current weather, mesh stats. When the bot goes down the message keeps its old timestamp, and optionally a systemd hook marks it as",
    "1. Открой": "1. Open",
    "→ скопируй": "→ copy the",
    "2. Добавь бота в нужный чат и сделай его": "2. Add the bot to the chat and make it an",
    "админом": "admin",
    "с правом «Pin messages».": "with the “Pin messages” right.",
    "3. Узнай": "3. Find the",
    "чата — например через": "of the chat — e.g. via",
    "(для группы — переслать туда сообщение и спросить ID), либо": "(for a group — forward a message there and ask for the ID), or",
    "для супергрупп.": "for supergroups.",
    "(вид": "(like",
    "(число; для групп обычно отрицательное)": "(a number; usually negative for groups)",
    "Интервал обновления, сек": "Update interval, sec",
    "(минимум 15)": "(minimum 15)",
    "🌐 Прокси для статус-бота настраивается во вкладке": "🌐 Proxy for the status bot is set on the",
    "(тумблер «Статус-бот»).": "tab (the “Status bot” toggle).",
    "Автоматически закрепить сообщение при первой отправке (нужно право": "Auto-pin the message on first send (needs the right",
    "Показывать погоду (температура, ощущение, влажность, ветер)": "Show weather (temperature, feels-like, humidity, wind)",
    "Показывать статистику mesh-сети (узлы / онлайн)": "Show mesh stats (nodes / online)",
    "Доп. текст внизу": "Extra text at the bottom",
    "(одна-две строки, например подсказка чату)": "(one or two lines, e.g. a hint for the chat)",
    "Только для ответов с бота. Если вдруг не увидели текст в интерфейсе мештастика.": "Only for replies from the bot. In case the text isn't visible in the Meshtastic app.",
    "🔄 Обновить сейчас": "🔄 Update now",
    "Забыть закреп — следующее сообщение будет новым": "Forget the pin — the next message will be new",
    "✂ Сбросить закреп": "✂ Reset pin",
    "Совет:": "Tip:",
    "сначала создай бота, добавь в чат, нажми «Сохранить» — потом «Запустить». Бот сразу пошлёт сообщение и попробует его закрепить. Если у него нет права закреплять — закрепи руками,": "first create the bot, add it to the chat, click “Save” — then “Start”. The bot sends a message right away and tries to pin it. If it can't pin — pin it by hand,",
    "бот всё равно запомнит.": "the bot will remember it anyway.",

    // ---- Misc: Telegram bridge ----
    "Telegram-мост": "Telegram bridge",
    "— пересылать сообщения из выбранных Telegram-каналов в mesh": "— forward messages from selected Telegram channels into the mesh",
    "Бот слушает указанные Telegram-каналы и при появлении ключевого слова шлёт сообщение в mesh. Подходит для публичных каналов, куда обычного бота не добавить (например официальные информационные каналы).": "The bot watches the given Telegram channels and forwards a message to the mesh when a keyword appears. Good for public channels you can't add a regular bot to (e.g. official info channels).",
    "Режим работы": "Mode",
    "Без API": "No API",
    "(рекомендуется)": "(recommended)",
    "Парсит публичные превью-страницы каналов на t.me. Не нужен api_id, не нужна авторизация — работает с любого IP. Только публичные каналы по @username.": "Parses public channel preview pages on t.me. No api_id, no auth needed — works from any IP. Public channels by @username only.",
    "С API": "With API",
    "(для приватных каналов)": "(for private channels)",
    "MTProto через Telethon. Нужны api_id / api_hash с my.telegram.org + одноразовая авторизация по SMS через SSH. Видит каналы по числовому ID и приватные каналы, в которые ты подписан.": "MTProto via Telethon. Needs api_id / api_hash from my.telegram.org + a one-time SMS login over SSH. Sees channels by numeric ID and private channels you're subscribed to.",
    "Авторизация (одноразово, через SSH)": "Authorization (one-time, over SSH)",
    "1. Возьми": "1. Get",
    "и": "and",
    "на": "at",
    "2. Сохрани их ниже, нажми «Сохранить».": "2. Save them below, click “Save”.",
    "3. На Pi через SSH запусти один раз:": "3. On the Pi over SSH, run once:",
    "Введи телефон + SMS-код (и 2FA пароль если включён). Сессия сохранится.": "Enter your phone + SMS code (and 2FA password if enabled). The session is saved.",
    "Если t.me у твоего провайдера зарезано — настрой общий прокси во вкладке": "If t.me is blocked by your ISP — set up the shared proxy on the",
    "и включи там тумблер «Telegram-мост».": "tab and enable the “Telegram bridge” toggle.",
    "🔍 Проверить доступ к t.me": "🔍 Test t.me access",
    "Каналы для прослушки": "Channels to watch",
    "По одному в строке. В режиме «Без API» — только": "One per line. In “No API” mode — only",
    "публичных каналов.": "public channels.",
    "Ключевые слова": "Keywords",
    "Через запятую. Совпадение по подстроке, без учёта регистра. Хотя бы одно слово должно быть в сообщении.": "Comma-separated. Substring match, case-insensitive. At least one word must be in the message.",
    "ключевое слово 1, ключевое слово 2, …": "keyword 1, keyword 2, …",
    "Гео-фильтр": "Geo filter",
    "(опционально — сужает алерты до твоего региона)": "(optional — narrows alerts to your region)",
    "Если задан — сообщение должно содержать": "If set — the message must contain",
    "ключевое слово,": "a keyword,",
    "хотя бы одно из этих. Пусто — пропускать любой матч по ключевым словам.": "and at least one of these. Empty — pass any keyword match.",
    "Пример:": "Example:",
    "Чувашия, Чебоксары, Нижний Новгород": "Chuvashia, Cheboksary, Nizhny Novgorod",
    "Куда транслировать в mesh": "Where to broadcast in the mesh",
    "Найденные сообщения уйдут в этот канал Meshtastic и этому адресату.": "Matched messages go to this Meshtastic channel and recipient.",
    "Канал Meshtastic": "Meshtastic channel",
    "Адресат": "Recipient",
    "Пауза между пересылками от одного канала, сек": "Delay between forwards from one channel, sec",
    "Префикс в mesh-сообщении": "Prefix in the mesh message",
    "Интервал опроса каналов, сек": "Channel poll interval, sec",
    "(только для «Без API»)": "(only for “No API”)",
    "Убирать эмодзи": "Strip emojis",
    "из пересылаемых сообщений (экономит до ~50% места в LoRa-пакете)": "from forwarded messages (saves up to ~50% of LoRa packet space)",
    "Включать имя канала (@username)": "Include the channel name (@username)",
    "в заголовок сообщения. Сними галку — будет только текст без указания источника.": "in the message header. Uncheck it — only the text, no source.",
    "Удалять самоподпись канала": "Remove the channel's self-signature",
    "— строки типа «Радар Чувашия — @radar_chuvashiya» (где упомянут собственный username канала)": "— lines like “Radar Chuvashia — @radar_chuvashiya” (mentioning the channel's own username)",
    "Хочешь супер-короткое сообщение?": "Want a super-short message?",
    "Очисти поле «Префикс в mesh-сообщении» (станет:": "Clear the “Prefix in the mesh message” field (becomes:",
    "), а если ещё и снять галку выше — пойдёт только сам текст без всякой обвязки.": "), and if you also uncheck the box above — only the text goes, with no wrapping.",
    "Чёрный список фраз": "Phrase blocklist",
    "(удаляются полные строки)": "(whole lines are removed)",
    "По одной фразе в строке. Если в сообщении найдётся хотя бы одна — вся строка вычищается из mesh-пакета. Регистр не важен. Для регулярных выражений начни строку с": "One phrase per line. If a message contains any of them — the whole line is stripped from the mesh packet. Case-insensitive. For regular expressions start the line with",
    "Примеры:": "Examples:",
    "Обход белых списков @Internet_Boost_bot Подписаться:": "Whitelist bypass\n@Internet_Boost_bot\nSubscribe:",
    "Защита от спама": "Spam protection",
    "(промо-постов в формате «алерта»)": "(promo posts disguised as “alerts”)",
    "Каналы любят прятать рекламу/призывы в посты-«алерты» (типа «ТРЕВОГА» + список 80 городов с @username). Эти лимиты сразу режут такое.": "Channels love hiding ads/calls-to-action in “alert”-style posts (like “ALERT” + a list of 80 cities with @usernames). These limits cut that immediately.",
    "Макс. длина сообщения, симв": "Max message length, chars",
    "(0 = без лимита)": "(0 = no limit)",
    "Оставить только первые N абзацев": "Keep only the first N paragraphs",
    "(0 = все)": "(0 = all)",
    "Макс. @упоминаний в сообщении": "Max @mentions per message",
    "Макс. ссылок в сообщении": "Max links per message",
    "Сжатие через ИИ": "AI compression",
    "(нужен настроенный «ИИ-ассистент»)": "(needs the “AI assistant” configured)",
    "Сжимать длинные сообщения LLM": "Compress long messages with the LLM",
    "— бот прогоняет длинный пост через ИИ и шлёт в mesh короткую выжимку (суть: что/где/когда). При сбое ИИ — отправляется оригинал.": "— the bot runs a long post through the AI and sends a short digest to the mesh (what/where/when). If the AI fails — the original is sent.",
    "Сжимать, если длиннее": "Compress if longer than",
    "(символов)": "(characters)",
    "Сжимать до": "Compress to",
    "(символов, ~100 = один LoRa-пакет)": "(characters, ~100 = one LoRa packet)",
    "▶ Запустить мост": "▶ Start bridge",
    "Отправить тестовое сообщение в mesh": "Send a test message to the mesh",
    "🧪 Тест в mesh": "🧪 Test to mesh",
    "Последние совпадения": "Recent matches",
    "Ничего пока не было.": "Nothing yet.",
    "🔬 Все увиденные сообщения (отладка)": "🔬 All seen messages (debug)",
    "— пропущенные тоже": "— including skipped ones",
    "Сюда падает каждое сообщение, которое бот видит на канале — даже если фильтр его отбраковал. Если канал постит, а тут пусто — значит бот канал не видит (прокси/сеть/невалидный @username).": "Every message the bot sees on a channel lands here — even if the filter rejected it. If a channel posts but this stays empty, the bot can't see the channel (proxy/network/invalid @username).",
    "Все": "All",
    "✅ Отправлены": "✅ Forwarded",
    "⏸ Задросселлены": "⏸ Throttled",
    "⊘ Нет ключевого слова": "⊘ No keyword",
    "📍 Не прошёл гео": "📍 Failed geo",
    "🛑 Спам-фильтр": "🛑 Spam filter",
    "Пока ничего не приходило. Подожди опроса каналов (60 сек по умолчанию).": "Nothing has arrived yet. Wait for the channel poll (60 s by default).",

    // ---- Proxy tab ----
    "Один общий прокси (VLESS/Xray, SSH-туннель и т.п.) на всё приложение. Ниже выбираешь, какие сервисы через него ходят — остальные идут напрямую. Полезно, если провайдер режет Open-Meteo, RainViewer, t.me или LLM-API.": "One shared proxy (VLESS/Xray, SSH tunnel, etc.) for the whole app. Below you choose which services go through it — the rest go direct. Useful if your ISP blocks Open-Meteo, RainViewer, t.me or the LLM API.",
    "VLESS-подписка (Xray)": "VLESS subscription (Xray)",
    "— выбор страны без SSH": "— pick a country without SSH",
    "Вставь ссылку-подписку — бот сам поднимет локальный туннель (": "Paste a subscription link — the bot raises a local tunnel (",
    ") и даст выбрать страну-выход. Ссылка хранится только на малине, в git не попадает.": ") and lets you pick an exit country. The link stays only on the Pi, never in git.",
    "https://…/подписка": "https://…/subscription",
    "— сначала загрузи подписку —": "— load a subscription first —",
    "Авто-переключение": "Auto-switch",
    "— если текущий выход отвалится, бот сам перейдёт на следующий": "— if the current exit dies, the bot moves to the next one",
    "(заполняется автоматически при выборе выхода)": "(filled automatically when you pick an exit)",
    "Формат URL. Примеры:": "URL format. Examples:",
    "— локальный SOCKS5 (Xray/VLESS,": "— local SOCKS5 (Xray/VLESS,",
    "— удалённый с авторизацией": "— remote with auth",
    "— обычный HTTP-прокси": "— a plain HTTP proxy",
    "Пусто — всё ходит напрямую (тумблеры ниже игнорируются).": "Empty — everything goes direct (the toggles below are ignored).",
    "socks5://127.0.0.1:10808 или пусто": "socks5://127.0.0.1:10808 or empty",
    "Погода": "Weather",
    "— Open-Meteo (прогноз, качество воздуха, УФ, вода, вчера)": "— Open-Meteo (forecast, air quality, UV, water, yesterday)",
    "Радар осадков": "Precipitation radar",
    "— тайлы RainViewer": "— RainViewer tiles",
    "— чтение каналов (t.me / MTProto)": "— reading channels (t.me / MTProto)",
    "— запросы к ИИ-API (": "— requests to the AI API (",
    ", сжатие алертов)": ", alert compression)",
    "Статус-бот": "Status bot",
    "— Telegram-бот, правящий закреплённое сообщение": "— the Telegram bot editing the pinned message",
    "⚠️ Прокси часто общий и rate-лимитится. Если Open-Meteo отдаёт": "⚠️ The proxy is often shared and rate-limited. If Open-Meteo returns",
    "— сервер кэширует прогноз и отдаёт последнюю удачную выдачу.": "— the server caches the forecast and serves the last good response.",

    // ---- Heltec modal / node profile ----
    "⚙️ Настройки Heltec V4": "⚙️ Heltec V4 settings",
    "Закрыть": "Close",
    "Запрашиваю настройки у ноды…": "Requesting settings from the node…",
    "Идентификация": "Identity",
    "Длинное имя": "Long name",
    "(до 39 символов)": "(up to 39 characters)",
    "Короткое имя": "Short name",
    "(ровно 4 символа, видно в эфире)": "(exactly 4 characters, shown on air)",
    "Радио (LoRa)": "Radio (LoRa)",
    "Регион": "Region",
    "(частотный диапазон LoRa)": "(LoRa frequency band)",
    "Модем-пресет": "Modem preset",
    "(скорость ↔ дальность)": "(speed ↔ range)",
    "(1–7, число ретрансляций)": "(1–7, number of relays)",
    "(0 = по умолчанию)": "(0 = default)",
    "Передатчик включён (TX enabled)": "Transmitter on (TX enabled)",
    "Роль в сети": "Network role",
    "Роль ноды": "Node role",
    "Информация": "Info",
    "Прошивка": "Firmware",
    "⟳ Перезагрузить ноду": "⟳ Reboot node",
    "💾 Применить": "💾 Apply",
    "Профиль узла": "Node profile",
    "💬 Написать DM": "💬 Send DM",
    "🗺 Показать на карте": "🗺 Show on map",
    "Запустить сейчас (для теста)": "Run now (for testing)",
    "Удалить": "Delete",

    // ---- app.js: toasts ----
    "Бэкап скачивается…": "Backup downloading…",
    "Выбери хотя бы один день": "Pick at least one day",
    "Выбери хотя бы одно поле": "Pick at least one field",
    "Город сохранён": "City saved",
    "Запущен": "Started",
    "Запущено": "Started",
    "Карта ещё не загрузилась": "The map hasn't loaded yet",
    "Карта не готова — открой вкладку Карта и попробуй снова": "Map not ready — open the Map tab and try again",
    "Команда reboot отправлена. Через 5 секунд устройство перезагрузится.": "Reboot command sent. The device restarts in 5 seconds.",
    "Настройки предупреждений сохранены": "Alert settings saved",
    "Не удалось загрузить радар: ": "Failed to load radar: ",
    "Нет данных traceroute для отрисовки": "No traceroute data to draw",
    "Нечего применять — ничего не изменилось": "Nothing to apply — nothing changed",
    "Ни у тебя, ни у получателя нет координат — рисовать нечего 😕": "Neither you nor the recipient has coordinates — nothing to draw 😕",
    "Остановлен": "Stopped",
    "Остановлено": "Stopped",
    "Ошибка: ": "Error: ",
    "Подключаюсь к Heltec…": "Connecting to Heltec…",
    "Применено: ": "Applied: ",
    "Прокси сохранён": "Proxy saved",
    "Радар: нет кадров": "Radar: no frames",
    "Сброшено — следующее сообщение будет новым": "Reset — the next message will be new",
    "Связь с Heltec закрыта": "Heltec connection closed",
    "Слот обновлён": "Slot updated",
    "Слот создан": "Slot created",
    "Сначала загрузи подписку и выбери страну": "Load a subscription and pick a country first",
    "Сообщение обновлено": "Message updated",
    "Сохранено": "Saved",
    "У бота нет координат — стрелка пойдёт не от тебя": "The bot has no coordinates — the arrow won't start from you",
    "У получателя нет координат — точка назначения пропадает": "The recipient has no coordinates — the destination is dropped",
    "У узла нет координат — он не появится на карте": "The node has no coordinates — it won't show on the map",
    "Уже на последней версии": "Already on the latest version",
    "Узел больше не виден боту": "The node is no longer visible to the bot",
    "Условий для предупреждений сейчас нет": "No alert conditions right now",

    // ---- app.js: dashboard / health statuses ----
    "Подключено": "Connected",
    "Нет связи": "No link",
    "Heltec на связи": "Heltec is online",
    "Проверь настройки": "Check settings",
    "Нода Heltec": "Heltec node",
    "Погода (Open-Meteo)": "Weather (Open-Meteo)",
    "Прокси": "Proxy",
    "Диск": "Disk",
    "Аптайм сервиса": "Service uptime",
    "Версия": "Version",
    "нет связи": "no link",
    "город не задан": "city not set",
    "нет свежих данных": "no fresh data",
    "напрямую (без прокси)": "direct (no proxy)",
    "на связи": "online",
    "онлайн": "online",
    "ок": "ok",
    "через прокси · выход": "via proxy · exit",
  };

  function norm(s) { return String(s).trim().replace(/\s+/g, " "); }
  function tr(s) {
    if (LANG !== "en" || !s) return s;
    const hit = EN[norm(s)];
    return hit || s;
  }
  // Exposed for app.js dynamic strings.
  window.t = tr;
  window.__lang = LANG;

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "CODE", "TEXTAREA", "NOSCRIPT", "OPTION"]);

  function translateAttr(attr) {
    document.querySelectorAll("[" + attr + "]").forEach((el) => {
      const cur = el.getAttribute(attr);
      const en = EN[norm(cur || "")];
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
      const en = EN[norm(raw)];
      if (en) {
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
