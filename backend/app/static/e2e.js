// Browser side of optional end-to-end encryption (DOM integration).
// Runs only when the page carries an enabled #e2e-config; crypto lives in e2e-core.js.
// Spec: docs/superpowers/specs/2026-10-09-e2e-encryption-design.md ("Browser").
(function () {
  "use strict";

  const configNode = document.getElementById("e2e-config");
  if (!configNode) return;
  let config;
  try {
    config = JSON.parse(configNode.textContent);
  } catch (error) {
    return;
  }
  if (!config || !config.enabled) return;

  const Core = window.E2ECore;
  const DB_NAME = "ai-remote-e2e";
  const STORE = "keys";
  const RECORD_ID = "current";
  const SEARCH_KEY = "e2eSearchQuery";
  const paramsMissing = !(config.salt && config.kdf && config.key_check);

  let encKey = null;
  let resolveReady;
  // Published right away so app.js can tell "E2E page, still locked" from "E2E scripts missing".
  window.E2E = {
    isActive: () => false,
    errorText: () => (paramsMissing ? i18nText("js.e2e.not_set_up") : i18nText("js.e2e.locked")),
  };
  window.e2eReady = new Promise((resolve) => {
    resolveReady = resolve;
  });

  const i18nText = (key) => tr(key);

  const i18n = (() => {
    try {
      return JSON.parse(document.getElementById("i18n-data").textContent);
    } catch (error) {
      return {};
    }
  })();
  const tr = (key, vars) => {
    let text = i18n[key] || key;
    for (const [name, value] of Object.entries(vars || {})) text = text.split(`{${name}}`).join(value);
    return text;
  };

  // e2e-core.js (or a vendor lib) failed to load: stay inactive; app.js then refuses to send.
  if (!Core) {
    resolveReady();
    return;
  }

  // ---- IndexedDB key record ----------------------------------------------------------

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function dbRun(mode, fn) {
    const db = await openDb();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const req = fn(tx.objectStore(STORE));
        tx.oncomplete = () => resolve(req ? req.result : undefined);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  }

  const loadRecord = () => dbRun("readonly", (s) => s.get(RECORD_ID));
  const saveRecord = (record) => dbRun("readwrite", (s) => s.put({ id: RECORD_ID, ...record }));
  const deleteRecord = () => dbRun("readwrite", (s) => s.delete(RECORD_ID));

  async function loadUsableKey() {
    let record;
    try {
      record = await loadRecord();
    } catch (error) {
      return null;
    }
    if (!record) return null;
    if (record.key && record.key_check === config.key_check && record.binding === config.binding) {
      return record.key;
    }
    try {
      await deleteRecord();
    } catch (error) {
      /* ignore */
    }
    return null;
  }

  // ---- UI: banner and unlock overlay -------------------------------------------------

  function showBanner() {
    const banner = document.createElement("div");
    banner.className = "e2e-banner";
    banner.setAttribute("role", "alert");
    banner.textContent = tr("js.e2e.not_set_up");
    document.body.insertBefore(banner, document.body.firstChild);
  }

  function showOverlay() {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "e2e-overlay";
      const form = document.createElement("form");
      form.className = "e2e-unlock";
      form.setAttribute("role", "dialog");
      form.setAttribute("aria-modal", "true");
      form.setAttribute("aria-labelledby", "e2e-unlock-title");

      const title = document.createElement("h2");
      title.id = "e2e-unlock-title";
      title.textContent = tr("js.e2e.unlock_title");

      const label = document.createElement("label");
      label.textContent = tr("js.e2e.passphrase");
      const input = document.createElement("input");
      input.type = "password";
      input.autocomplete = "current-password";
      input.required = true;
      input.autofocus = true;
      label.appendChild(input);

      const submit = document.createElement("button");
      submit.type = "submit";
      submit.textContent = tr("js.e2e.unlock");

      const status = document.createElement("p");
      status.className = "e2e-unlock-status";
      status.setAttribute("role", "status");

      form.append(title, label, submit, status);
      overlay.appendChild(form);
      document.body.appendChild(overlay);
      input.focus();

      // Minimal focus trap: the page behind is inert while the overlay is up.
      overlay.addEventListener("keydown", (event) => {
        if (event.key === "Tab") {
          event.preventDefault();
          (document.activeElement === input ? submit : input).focus();
        }
      });

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const passphrase = input.value;
        if (!passphrase) return;
        submit.disabled = true;
        input.disabled = true;
        status.textContent = tr("js.e2e.deriving");
        await new Promise((r) => setTimeout(r, 30)); // let the status paint before the KDF blocks
        try {
          const master = await Core.deriveMaster(passphrase, config.salt, config.kdf, window.hashwasm.argon2id);
          const keys = await Core.importKeys(master);
          if (!Core.constantTimeEqual(keys.check, config.key_check)) {
            status.textContent = tr("js.e2e.wrong_passphrase");
          } else {
            try {
              await saveRecord({ key: keys.encKey, key_check: config.key_check, binding: config.binding });
            } catch (error) {
              /* storage unavailable: the key still works for this page view */
            }
            input.value = "";
            overlay.remove();
            resolve(keys.encKey);
            return;
          }
        } catch (error) {
          // Library/derivation failure, not a wrong passphrase (that is the key_check branch).
          status.textContent = tr("js.e2e.decrypt_failed");
        }
        input.value = "";
        input.disabled = false;
        submit.disabled = false;
        input.focus();
      });
    });
  }

  // ---- Sanitizing --------------------------------------------------------------------

  // Same tag list as render_core.py (bleach) plus "button" for the image-fetch placeholder.
  const ALLOWED_TAGS = [
    "p", "br", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "hr", "table", "thead", "tbody", "tr", "th", "td", "span", "div",
    "button",
  ];
  const PURIFY_CONFIG = {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ["href", "title", "class", "type", "data-session-id", "data-path"],
    ALLOW_DATA_ATTR: false,
    // DOMPurify vets every non-data attribute value as a URI, and "claude-code:<id>" looks
    // like one with an unknown scheme — without this the image buttons lose their session.
    ADD_URI_SAFE_ATTR: ["data-session-id", "data-path"],
    FORBID_TAGS: ["img", "style", "script", "svg", "math"],
  };

  if (window.DOMPurify && window.DOMPurify.addHook) {
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A" && node.hasAttribute("href")) node.setAttribute("rel", "noopener noreferrer");
    });
  }

  function setHtml(element, html) {
    if (!window.DOMPurify) throw new Error("DOMPurify missing");
    element.innerHTML = window.DOMPurify.sanitize(html, PURIFY_CONFIG);
  }

  // ---- Decryption of server-rendered ciphertext --------------------------------------

  function markFailed(element) {
    element.textContent = tr("js.e2e.decrypt_failed");
    element.classList.add("e2e-decrypt-failed");
  }

  async function decryptElement(element) {
    try {
      const plain = await Core.decryptText(encKey, element.dataset.e2e, element.dataset.e2eAad || "");
      switch (element.dataset.e2eKind) {
        case "text":
          element.textContent = plain;
          break;
        case "preview-text":
          element.textContent = JSON.parse(plain).text || "";
          break;
        case "html":
          setHtml(element, plain);
          break;
        case "preview-html":
          setHtml(element, JSON.parse(plain).html || "");
          break;
        default:
          throw new Error("unknown kind");
      }
    } catch (error) {
      markFailed(element);
    }
  }

  function imageKeys() {
    try {
      return new Set(JSON.parse(document.getElementById("image-keys-data").textContent));
    } catch (error) {
      return new Set();
    }
  }

  function basename(path) {
    return path.split("/").filter(Boolean).pop() || path;
  }

  async function loadImage(sessionId, path, url) {
    const key = await Core.pathKey(sessionId, path);
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const bytes = new Uint8Array(await res.arrayBuffer());
    const plain = await Core.decryptBytes(encKey, bytes, Core.aad.image(sessionId, key));
    const mime = Core.sniffImageMime(plain);
    if (!mime) throw new Error("not an image");
    return URL.createObjectURL(new Blob([plain], { type: mime }));
  }

  function imageLink(blobUrl, name) {
    const link = document.createElement("a");
    link.className = "chat-image";
    link.href = blobUrl;
    link.target = "_blank";
    link.rel = "noopener";
    const img = document.createElement("img");
    img.src = blobUrl;
    img.alt = name;
    img.loading = "lazy";
    link.appendChild(img);
    return link;
  }

  async function autoLoadImages() {
    const available = imageKeys();
    if (!available.size) return;
    const buttons = document.querySelectorAll(".image-fetch");
    await Promise.all(
      Array.from(buttons, async (button) => {
        const sessionId = button.dataset.sessionId;
        const path = button.dataset.path;
        try {
          const key = await Core.pathKey(sessionId, path);
          if (!available.has(key)) return;
          const blobUrl = await loadImage(sessionId, path, `/chats/${encodeURIComponent(sessionId)}/images/${key}`);
          button.replaceWith(imageLink(blobUrl, basename(path)));
        } catch (error) {
          const label = button.querySelector(".image-fetch-label");
          if (label) label.textContent = `${label.textContent} — ${tr("js.image_failed")}`;
        }
      })
    );
  }

  async function decryptJobs() {
    const payloads = document.querySelectorAll("[data-e2e-job-payload]");
    const results = document.querySelectorAll("[data-e2e-job-result]");
    await Promise.all([
      ...Array.from(payloads, async (node) => {
        try {
          const payload = JSON.parse(node.textContent);
          const type = node.dataset.type;
          let field = null;
          let aad = null;
          if (type === "resume_message") {
            field = "prompt";
            aad = Core.aad.resumePrompt(node.dataset.target);
          } else if (type === "new_session") {
            field = "prompt";
            aad = Core.aad.newSessionPrompt(node.dataset.target, payload.tool);
          } else if (type === "search") {
            field = "query";
            aad = Core.aad.searchQuery();
          }
          if (!field || !Core.isCiphertext(payload[field])) return;
          payload[field] = await Core.decryptText(encKey, payload[field], aad);
          node.textContent = JSON.stringify(payload);
        } catch (error) {
          markFailed(node);
        }
      }),
      ...Array.from(results, async (node) => {
        const raw = node.textContent;
        if (!Core.isCiphertext(raw)) return;
        try {
          node.textContent = await Core.decryptText(encKey, raw, Core.aad.jobResult(node.dataset.jobId));
        } catch (error) {
          markFailed(node);
        }
      }),
    ]);
  }

  async function decryptPage() {
    await Promise.all(Array.from(document.querySelectorAll("[data-e2e]"), decryptElement));
    await Promise.all([autoLoadImages(), decryptJobs()]);
  }

  // ---- Helpers for app.js ------------------------------------------------------------

  function errorText() {
    return paramsMissing ? tr("js.e2e.not_set_up") : tr("js.e2e.locked");
  }

  async function encrypt(text, aad) {
    if (!encKey) throw new Error(errorText());
    return Core.encryptText(encKey, text, aad);
  }

  async function decrypt(value, aad) {
    if (!encKey) throw new Error(errorText());
    return Core.decryptText(encKey, value, aad);
  }

  // Job result text: ciphertext is decrypted, server-written text ("timed out") passes through.
  async function decryptResult(text, jobId) {
    if (!Core.isCiphertext(text)) return text;
    try {
      return await decrypt(text, Core.aad.jobResult(jobId));
    } catch (error) {
      return tr("js.e2e.decrypt_failed");
    }
  }

  Object.assign(window.E2E, {
    isActive: () => encKey !== null,
    encrypt,
    decrypt,
    decryptResult,
    errorText,
    loadImage: (sessionId, path, url) => {
      if (!encKey) return Promise.reject(new Error(errorText()));
      return loadImage(sessionId, path, url);
    },
    aad: Core.aad,
  });

  // ---- Encrypted search (list page) --------------------------------------------------

  function setupSearch() {
    const input = document.getElementById("e2e-search");
    if (!input) return;
    const form = input.form;
    const params = new URLSearchParams(location.search);
    let stored = null;
    try {
      stored = sessionStorage.getItem(SEARCH_KEY);
    } catch (error) {
      /* ignore */
    }
    if (params.get("ids") && stored) input.value = stored;

    let note = form.parentNode.querySelector(".e2e-search-status");
    if (!note) {
      note = document.createElement("p");
      note.className = "search-note e2e-search-status";
      note.setAttribute("role", "status");
      note.hidden = true;
      form.insertAdjacentElement("afterend", note);
    }
    const say = (text) => {
      note.hidden = !text;
      note.textContent = text || "";
    };

    const idsInput = form.querySelector('input[name="ids"]');
    let busy = false;

    const navigate = (ids) => {
      const target = new URLSearchParams();
      for (const [name, value] of new FormData(form).entries()) {
        if (name !== "ids" && typeof value === "string" && value !== "") target.append(name, value);
      }
      if (ids) target.set("ids", ids.join(","));
      const qs = target.toString();
      location.href = qs ? `/?${qs}` : "/";
    };

    const pollJob = async (jobId, countdown) => {
      for (;;) {
        await new Promise((r) => setTimeout(r, 2500));
        const res = await fetch(`/jobs/${encodeURIComponent(jobId)}/status`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.status === "done") {
          countdown && countdown.stop();
          return data.result_text;
        }
        if (data.status === "failed") {
          countdown && countdown.stop();
          throw new Error("failed");
        }
      }
    };

    form.addEventListener("submit", async (event) => {
      const query = input.value.trim();
      if (!query) {
        // Cleared box: plain filter submit, without a stale search restriction.
        if (idsInput) idsInput.remove();
        try {
          sessionStorage.removeItem(SEARCH_KEY);
        } catch (error) {
          /* ignore */
        }
        return;
      }
      // A filter change while the shown results belong to this query keeps them as they are.
      if (idsInput && query === stored) return;
      event.preventDefault();
      if (busy) return;
      busy = true;
      let countdown = null;
      try {
        say(`${tr("js.e2e.searching")}`);
        const ciphertext = await encrypt(query, Core.aad.searchQuery());
        const res = await fetch("/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: ciphertext }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const { job_id, eta_seconds } = await res.json();
        if (typeof makeCountdown === "function") {
          countdown = makeCountdown(note, "js.e2e.search_verb");
          countdown.start(eta_seconds);
        }
        const resultText = await pollJob(job_id, countdown);
        const parsed = JSON.parse(await decrypt(resultText, Core.aad.jobResult(job_id)));
        const ids = Array.isArray(parsed.ids) ? parsed.ids.filter((id) => typeof id === "string").slice(0, 100) : [];
        if (!ids.length) {
          say(tr("js.e2e.no_results"));
          return;
        }
        try {
          sessionStorage.setItem(SEARCH_KEY, query);
        } catch (error) {
          /* ignore */
        }
        navigate(ids);
      } catch (error) {
        countdown && countdown.stop();
        say(tr("js.e2e.search_failed"));
      } finally {
        busy = false;
      }
    });
  }

  // ---- Logout ------------------------------------------------------------------------

  function setupLogout() {
    document.querySelectorAll('form[action="/logout"]').forEach((form) => {
      form.addEventListener("submit", async (event) => {
        if (form.dataset.e2eCleared) return;
        event.preventDefault();
        try {
          await deleteRecord();
        } catch (error) {
          /* nothing stored, or storage unavailable */
        }
        encKey = null;
        form.dataset.e2eCleared = "1";
        form.submit();
      });
    });
  }

  // ---- Start -------------------------------------------------------------------------

  async function start() {
    setupLogout();
    try {
      if (paramsMissing) {
        showBanner();
        return;
      }
      encKey = await loadUsableKey();
      if (!encKey) encKey = await showOverlay();
      setupSearch();
      await decryptPage();
    } catch (error) {
      /* e2eReady must resolve regardless */
    } finally {
      resolveReady();
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
