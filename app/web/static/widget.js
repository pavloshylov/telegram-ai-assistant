/*
 * AI assistant chat widget — one file, no build step, no dependencies.
 *
 * Embed:
 *   <script src="https://YOUR-SERVER/widget.js" data-color="#2e7d5b" data-lang="auto" defer></script>
 *
 * Attributes (all optional):
 *   data-api       base URL of the assistant API (default: where widget.js was loaded from)
 *   data-color     brand colour of the bubble and header, e.g. #2e7d5b
 *   data-title     header title (default: assistant and business name from the server)
 *   data-lang      en | uk | auto — language of the buttons and hints (the assistant itself answers in the visitor's language)
 *   data-position  right | left
 *   data-open      "true" opens the chat on load
 *
 * Everything the server or the model sends is inserted with textContent, never as HTML. The UI lives in a
 * shadow root, so the host site's CSS cannot break it.
 */
(function () {
  "use strict";
  if (window.AssistantWidget) return;

  var script = document.currentScript;
  var data = script ? script.dataset : {};
  var apiBase = (data.api || (script && script.src ? new URL(script.src).origin : "")).replace(/\/$/, "");
  var color = /^#[0-9a-fA-F]{6}$/.test(data.color || "") ? data.color : "#2e7d5b";
  var side = data.position === "left" ? "left" : "right";

  var STRINGS = {
    en: {
      placeholder: "Type your message…", send: "Send", open: "Open chat", close: "Close chat", assistant: "AI assistant",
      human: "👤 Talk to a person", phone: "Phone number", name: "Name (optional)", submit: "Send my number",
      cancel: "Cancel", sources: "Sources", offline: "Connection problem. Please try again.",
      badPhone: "Please enter a valid phone number.", examples: "Try one of these:",
    },
    uk: {
      placeholder: "Напишіть повідомлення…", send: "Надіслати", open: "Відкрити чат", close: "Закрити чат",
      assistant: "AI-асистент", human: "👤 Поговорити з людиною", phone: "Номер телефону", name: "Ім'я (необов'язково)",
      submit: "Надіслати номер", cancel: "Скасувати", sources: "Джерела",
      offline: "Проблема зі зв'язком. Спробуйте ще раз.", badPhone: "Введіть коректний номер телефону.",
      examples: "Спробуйте:",
    },
  };
  var lang = data.lang === "uk" || data.lang === "en" ? data.lang : /^uk/i.test(navigator.language || "") ? "uk" : "en";
  var T = STRINGS[lang];

  // ---------------------------------------------------------------- session and transcript
  var storeKey = "assistant-widget:" + (apiBase || location.origin);
  var store = (function () {
    try { return window.localStorage; } catch (e) { return null; }
  })();
  function load() {
    try { return JSON.parse(store.getItem(storeKey)) || {}; } catch (e) { return {}; }
  }
  function save() {
    try { store.setItem(storeKey, JSON.stringify({ id: sessionId, log: transcript.slice(-40) })); } catch (e) { /* private mode */ }
  }
  var saved = store ? load() : {};
  var sessionId = typeof saved.id === "string" && /^[A-Za-z0-9_-]{16,64}$/.test(saved.id) ? saved.id : newId();
  var transcript = Array.isArray(saved.log) ? saved.log : [];

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var s = "";
    for (var i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
    return s;
  }

  // ---------------------------------------------------------------- DOM helpers
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /** Plain text with **bold** and paragraph breaks; safe: only text nodes and <strong>. */
  function richText(parent, text) {
    String(text).split(/\n{2,}/).forEach(function (para) {
      var p = el("p");
      para.split("\n").forEach(function (line, i) {
        if (i) p.appendChild(document.createElement("br"));
        line.split(/\*\*(.+?)\*\*/g).forEach(function (part, j) {
          if (!part) return;
          if (j % 2) p.appendChild(el("strong", null, part));
          else p.appendChild(document.createTextNode(part));
        });
      });
      parent.appendChild(p);
    });
  }

  function textColorOn(hex) {
    var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.62 ? "#10201a" : "#ffffff";
  }

  var CSS = [
    ":host{all:initial}",
    "*{box-sizing:border-box;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}",
    ".launcher{position:fixed;bottom:20px;" + side + ":20px;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;",
    "background:var(--c);color:var(--on);box-shadow:0 6px 24px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center;z-index:2147483000}",
    ".launcher:focus-visible,.send:focus-visible,button:focus-visible{outline:3px solid #ffbf47;outline-offset:2px}",
    ".launcher svg{width:28px;height:28px}",
    ".panel{position:fixed;bottom:90px;" + side + ":20px;width:380px;max-width:calc(100vw - 24px);height:min(640px,calc(100vh - 110px));",
    "background:#fff;color:#1d2529;border-radius:16px;box-shadow:0 12px 48px rgba(0,0,0,.28);display:none;flex-direction:column;overflow:hidden;z-index:2147483000}",
    ".panel.open{display:flex}",
    ".head{background:var(--c);color:var(--on);padding:12px 14px;display:flex;align-items:center;gap:10px}",
    ".avatar{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.22);display:flex;align-items:center;justify-content:center;font-weight:700}",
    ".head .t{flex:1;min-width:0}.head .n{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".head .s{font-size:12px;opacity:.85}",
    ".x{background:none;border:0;color:inherit;font-size:24px;line-height:1;cursor:pointer;padding:4px 8px}",
    ".log{flex:1;overflow-y:auto;padding:12px;background:#f4f6f5;display:flex;flex-direction:column;gap:8px}",
    ".msg{max-width:86%;padding:8px 12px;border-radius:14px;font-size:14.5px;line-height:1.4;overflow-wrap:anywhere}",
    ".msg p{margin:0 0 6px}.msg p:last-child{margin:0}",
    ".bot{background:#fff;align-self:flex-start;border-bottom-left-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,.08)}",
    ".me{background:var(--c);color:var(--on);align-self:flex-end;border-bottom-right-radius:4px}",
    ".src{font-size:12px;color:#5b6a63;margin-top:6px;font-style:italic}",
    ".card{align-self:flex-start;width:86%;background:#fff;border:1px solid #dfe5e2;border-radius:12px;padding:10px 12px;font-size:14px}",
    ".card .ct{font-weight:600;margin-bottom:4px}.card .cl{color:#33413a;margin:1px 0}",
    ".card button,.chip,.form button,.human{font:inherit;cursor:pointer}",
    ".card .act{margin-top:8px;width:100%;padding:8px;border-radius:8px;border:1px solid var(--c);background:#fff;color:var(--c);font-weight:600}",
    ".chips{display:flex;flex-direction:column;gap:6px;align-self:flex-start;max-width:92%}",
    ".chip{text-align:left;background:#fff;border:1px solid var(--c);color:var(--c);padding:7px 12px;border-radius:18px;font-size:13.5px}",
    ".hint{font-size:12px;color:#5b6a63;align-self:flex-start}",
    ".form{align-self:flex-start;width:92%;background:#fff;border:1px solid #dfe5e2;border-radius:12px;padding:10px 12px;display:flex;flex-direction:column;gap:8px}",
    ".form label{font-size:12px;color:#5b6a63;display:flex;flex-direction:column;gap:3px}",
    ".form input{font-size:16px;padding:8px 10px;border:1px solid #c9d2ce;border-radius:8px}",
    ".form .row{display:flex;gap:8px}.form .go{flex:1;padding:9px;border:0;border-radius:8px;background:var(--c);color:var(--on);font-weight:600}",
    ".form .no{padding:9px 12px;border:1px solid #c9d2ce;border-radius:8px;background:#fff;color:#33413a}",
    ".err{color:#b3261e;font-size:13px}",
    ".typing{align-self:flex-start;background:#fff;padding:10px 14px;border-radius:14px;display:flex;gap:4px}",
    ".typing i{width:7px;height:7px;border-radius:50%;background:#9aa8a1;animation:b 1.2s infinite}",
    ".typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}",
    "@keyframes b{0%,60%,100%{transform:translateY(0);opacity:.5}30%{transform:translateY(-4px);opacity:1}}",
    ".bar{padding:6px 10px 0;background:#fff;border-top:1px solid #e6ebe8}",
    ".human{background:none;border:0;color:var(--c);font-size:13px;padding:4px 2px}",
    ".inp{display:flex;gap:8px;padding:6px 10px 10px;background:#fff;align-items:flex-end}",
    ".inp textarea{flex:1;resize:none;font-size:16px;line-height:1.35;padding:9px 12px;border:1px solid #c9d2ce;border-radius:18px;max-height:96px;outline:0}",
    ".inp textarea:focus{border-color:var(--c)}",
    ".send{border:0;border-radius:50%;width:40px;height:40px;background:var(--c);color:var(--on);cursor:pointer;font-size:18px}",
    ".send:disabled{opacity:.5;cursor:default}",
    "@media (max-width:480px){.panel{inset:0;width:100vw;max-width:none;height:100dvh;border-radius:0;bottom:0}",
    ".launcher{bottom:14px;" + side + ":14px}}",
    "@media (prefers-reduced-motion:reduce){.typing i{animation:none}}",
  ].join("");

  // ---------------------------------------------------------------- build the UI
  var host = document.createElement("div");
  host.setAttribute("data-assistant-widget", "");
  var root = host.attachShadow({ mode: "open" });
  root.appendChild(el("style", null, CSS));
  var wrap = el("div");
  wrap.style.setProperty("--c", color);
  wrap.style.setProperty("--on", textColorOn(color));
  root.appendChild(wrap);

  var launcher = el("button", "launcher");
  launcher.type = "button";
  launcher.setAttribute("aria-label", T.open);
  launcher.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>';

  var panel = el("section", "panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", T.assistant);
  var head = el("div", "head");
  var avatar = el("div", "avatar", "AI");
  var titleBox = el("div", "t");
  var nameEl = el("div", "n", "…");
  titleBox.appendChild(nameEl);
  titleBox.appendChild(el("div", "s", T.assistant));
  var closeBtn = el("button", "x", "×");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", T.close);
  head.appendChild(avatar); head.appendChild(titleBox); head.appendChild(closeBtn);

  var log = el("div", "log");
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  var bar = el("div", "bar");
  var humanBtn = el("button", "human", T.human);
  humanBtn.type = "button";
  bar.appendChild(humanBtn);
  var inp = el("div", "inp");
  var box = el("textarea");
  box.rows = 1;
  box.placeholder = T.placeholder;
  box.setAttribute("aria-label", T.placeholder);
  box.maxLength = 1000;
  var sendBtn = el("button", "send", "➤");
  sendBtn.type = "button";
  sendBtn.setAttribute("aria-label", T.send);
  inp.appendChild(box); inp.appendChild(sendBtn);
  panel.appendChild(head); panel.appendChild(log); panel.appendChild(bar); panel.appendChild(inp);
  wrap.appendChild(panel); wrap.appendChild(launcher);

  // ---------------------------------------------------------------- rendering
  var config = null, busy = false, chipsEl = null;

  function scrollDown() { log.scrollTop = log.scrollHeight; }

  function addUser(text) {
    var m = el("div", "msg me");
    richText(m, text);
    log.appendChild(m);
    scrollDown();
  }

  function addBot(reply, sources) {
    var m = el("div", "msg bot");
    richText(m, reply);
    if (sources && sources.length) {
      m.appendChild(el("div", "src", T.sources + ": " + sources.map(function (s) { return "[" + s.n + "] " + s.title; }).join("; ")));
    }
    log.appendChild(m);
    scrollDown();
  }

  function addCard(card) {
    var c = el("div", "card");
    c.appendChild(el("div", "ct", card.title));
    card.lines.forEach(function (line) {
      var l = el("div", "cl");
      line.split(/\*\*(.+?)\*\*/g).forEach(function (part, j) {
        if (!part) return;
        l.appendChild(j % 2 ? el("strong", null, part) : document.createTextNode(part));
      });
      c.appendChild(l);
    });
    card.buttons.forEach(function (b) {
      var btn = el("button", "act", b.text);
      btn.type = "button";
      btn.addEventListener("click", function () { askPhone(b.action, card.title); });
      c.appendChild(btn);
    });
    log.appendChild(c);
    scrollDown();
  }

  function renderEntry(e) {
    if (e.k === "user") addUser(e.text);
    else if (e.k === "bot") { addBot(e.reply, e.sources); (e.cards || []).forEach(addCard); }
    else if (e.k === "note") addBot(e.text);
  }

  function showTyping() {
    var t = el("div", "typing");
    t.appendChild(el("i")); t.appendChild(el("i")); t.appendChild(el("i"));
    log.appendChild(t); scrollDown();
    return t;
  }

  function removeChips() { if (chipsEl) { chipsEl.remove(); chipsEl = null; } }

  function showChips() {
    if (!config || !config.examples || transcript.some(function (e) { return e.k === "user"; })) return;
    chipsEl = el("div", "chips");
    chipsEl.appendChild(el("div", "hint", T.examples));
    config.examples.forEach(function (q) {
      var chip = el("button", "chip", q);
      chip.type = "button";
      chip.addEventListener("click", function () { send(q); });
      chipsEl.appendChild(chip);
    });
    log.appendChild(chipsEl); scrollDown();
  }

  // ---------------------------------------------------------------- network
  function call(path, body, timeoutMs) {
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, timeoutMs || 100000);
    return fetch(apiBase + path, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    }).then(function (r) {
      clearTimeout(timer);
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, json: j }; });
    }, function (err) { clearTimeout(timer); throw err; });
  }

  function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true;
    removeChips();
    addUser(text);
    transcript.push({ k: "user", text: text });
    box.value = ""; autosize();
    var typing = showTyping();
    call("/api/chat", { session_id: sessionId, message: text }).then(function (res) {
      typing.remove();
      var j = res.json;
      if (res.ok || res.status === 429) {
        var entry = { k: "bot", reply: j.reply || T.offline, sources: j.sources || [], cards: j.cards || [] };
        transcript.push(entry); renderEntry(entry);
        if (j.handoff) askPhone("human");
      } else {
        addBot(T.offline);
      }
    }).catch(function () {
      typing.remove(); addBot(T.offline);
    }).then(function () {
      busy = false; sendBtn.disabled = false; save(); box.focus();
    });
  }

  function askPhone(action, itemTitle) {
    var prefix = action.split(":")[0];
    var prompt = (config && config.lead_prompts && config.lead_prompts[prefix]) || "";
    if (prompt) addBot(prompt);
    var form = el("form", "form");
    var l1 = el("label", null, T.phone), phone = el("input");
    phone.type = "tel"; phone.autocomplete = "tel"; phone.required = true; phone.maxLength = 40; phone.inputMode = "tel";
    l1.appendChild(phone);
    var l2 = el("label", null, T.name), name = el("input");
    name.type = "text"; name.autocomplete = "name"; name.maxLength = 80;
    l2.appendChild(name);
    var err = el("div", "err"); err.setAttribute("role", "alert");
    var row = el("div", "row");
    var go = el("button", "go", T.submit); go.type = "submit";
    var no = el("button", "no", T.cancel); no.type = "button";
    row.appendChild(go); row.appendChild(no);
    form.appendChild(l1); form.appendChild(l2); form.appendChild(err); form.appendChild(row);
    no.addEventListener("click", function () { form.remove(); });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      err.textContent = ""; go.disabled = true;
      call("/api/lead", { session_id: sessionId, action: action, phone: phone.value, name: name.value }, 30000).then(function (res) {
        if (res.ok) {
          form.remove();
          var entry = { k: "note", text: "✅ " + res.json.message };
          transcript.push(entry); renderEntry(entry); save();
        } else {
          err.textContent = res.status === 422 ? T.badPhone : (typeof res.json.detail === "string" ? res.json.detail : T.offline);
          go.disabled = false;
        }
      }, function () { err.textContent = T.offline; go.disabled = false; });
    });
    log.appendChild(form); scrollDown();
    phone.focus();
  }

  // ---------------------------------------------------------------- open / close
  function autosize() {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 96) + "px";
  }

  function open() {
    panel.classList.add("open");
    launcher.style.display = window.matchMedia("(max-width:480px)").matches ? "none" : "";
    launcher.setAttribute("aria-expanded", "true");
    box.focus();
  }
  function close() {
    panel.classList.remove("open");
    launcher.style.display = "";
    launcher.setAttribute("aria-expanded", "false");
    launcher.focus();
  }

  launcher.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  panel.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  sendBtn.addEventListener("click", function () { send(box.value); });
  humanBtn.addEventListener("click", function () { askPhone("human"); });
  box.addEventListener("input", autosize);
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(box.value); }
  });

  // ---------------------------------------------------------------- start
  function start() {
    document.body.appendChild(host);
    call("/api/config", null, 15000).then(function (res) {
      if (!res.ok) throw new Error("config");
      config = res.json;
      nameEl.textContent = data.title || config.assistant + " · " + config.business;
      avatar.textContent = (config.assistant || "AI").charAt(0).toUpperCase();
      if (lang === "en") humanBtn.textContent = config.human_button || T.human;
      addBot(config.welcome);
      transcript.forEach(renderEntry);
      showChips();
      if (data.open === "true") open();
    }).catch(function () {
      nameEl.textContent = T.assistant;
      addBot(T.offline);
    });
  }

  window.AssistantWidget = { open: open, close: close };
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
})();
