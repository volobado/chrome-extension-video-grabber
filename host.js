// Video Grabber — общее для попапа и дашборда: переводы и связь с нативным хостом.

const HOST_NAME = "com.videograbber.ytdlp";

// Короткий доступ к переводам.
const t = (key, ...args) => chrome.i18n.getMessage(key, args.length ? args : undefined) || key;

function applyI18n() {
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-title]")) {
    el.title = t(el.dataset.i18nTitle);
  }
  for (const el of document.querySelectorAll("[data-i18n-placeholder]")) {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  }
}

// Один порт на всё время жизни страницы: каждое подключение поднимает python,
// поэтому опрос статуса по новому порту был бы расточительным.
const host = {
  port: null,
  waiting: [],

  connect() {
    if (this.port) return this.port;
    this.port = chrome.runtime.connectNative(HOST_NAME);
    this.port.onMessage.addListener((msg) => {
      const w = this.waiting.shift();
      if (w) w.resolve(msg);
    });
    this.port.onDisconnect.addListener(() => {
      const err = chrome.runtime.lastError;
      this.port = null;
      const pending = this.waiting.splice(0);
      // Хост уходит сам после каждого ответа только в старых версиях; здесь обрыв
      // с незакрытыми запросами — настоящая ошибка, без запросов — просто закрытие.
      for (const w of pending) w.reject(new Error(err ? err.message : "disconnected"));
    });
    return this.port;
  },

  send(msg) {
    return new Promise((resolve, reject) => {
      let port;
      try {
        port = this.connect();
      } catch (e) {
        reject(new Error(t("hostNotInstalled")));
        return;
      }
      this.waiting.push({ resolve, reject });
      try {
        port.postMessage(msg);
      } catch (e) {
        this.waiting.pop();
        this.port = null;
        reject(e);
      }
    });
  },
};

// Повтор упавшей задачи с теми же настройками. Куки берём свежие: старые служба уже удалила.
// replaces — старая запись уходит из списка, новая встаёт в конец очереди.
async function retryTask(task) {
  let cookies = "";
  try {
    const r = await chrome.runtime.sendMessage({ type: "GET_COOKIES", url: task.url });
    cookies = (r && r.cookies) || "";
  } catch { /* без кук */ }
  return host.send({
    cmd: "enqueue",
    url: task.url,
    format: task.format,
    cookies,
    title: task.title,
    dir: task.dir,
    index: task.index,
    playlist_title: task.playlist,
    filename: task.name || "",
    playlist: task.kind === "playlist",
    replaces: task.id,
  });
}

// Дашборд — одна вкладка: если уже открыт, переключаемся на него.
async function openDashboard() {
  const url = chrome.runtime.getURL("dashboard.html");
  const [tab] = await chrome.tabs.query({ url });
  if (tab) {
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url });
  }
}
