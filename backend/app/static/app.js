// UI strings come from the server (see i18n.py), already in the user's language.
const I18N = (() => {
  try {
    return JSON.parse(document.getElementById("i18n-data").textContent);
  } catch (error) {
    return {};
  }
})();

function t(key, vars) {
  let text = I18N[key] || key;
  for (const [name, value] of Object.entries(vars || {})) {
    text = text.split(`{${name}}`).join(value);
  }
  return text;
}

// The server's technical error codes (HTTPException.detail), shown in the user's language.
function errorText(detail) {
  return detail ? I18N[`js.err.${detail}`] || detail : null;
}

// E2E mode (see e2e.js): window.E2E exists only there; plaintext mode never touches it.
// If the page demands E2E (enabled #e2e-config) but the scripts are missing or still locked,
// the app fails closed: nothing is ever sent as plaintext.
const e2eRequired = (() => {
  try {
    const node = document.getElementById("e2e-config");
    return Boolean(node && JSON.parse(node.textContent).enabled);
  } catch (error) {
    return Boolean(document.getElementById("e2e-config"));
  }
})();
const e2eActive = () => Boolean(window.E2E && window.E2E.isActive());
const afterE2eReady = (fn) =>
  document.addEventListener("DOMContentLoaded", () => (window.e2eReady || Promise.resolve()).then(fn));
const PROMPT_MAX_LENGTH = 32000; // same plaintext limit as the server (models.PROMPT_MAX_LENGTH)

// Encrypts a prompt in E2E mode; a failure carries a user-facing message (e2eMessage).
const preparePrompt = (text, aad) => {
  const fail = (message) => {
    const error = new Error(message);
    error.e2eMessage = message;
    return Promise.reject(error);
  };
  if (e2eRequired) {
    if (!window.E2E) return fail(t("js.send_error"));
    if (!window.E2E.isActive()) return fail(window.E2E.errorText());
    if (text.length > PROMPT_MAX_LENGTH) return fail(t("js.send_error"));
    return E2E.encrypt(window.E2ECore.makePromptEnvelope(text), aad).catch((error) => {
      error.e2eMessage = error.message;
      throw error;
    });
  }
  return Promise.resolve(text);
};

function makeCountdown(statusNode, verbKey) {
  let countdownId = null;
  const stop = () => {
    if (countdownId) {
      clearInterval(countdownId);
      countdownId = null;
    }
  };
  const start = (remaining) => {
    remaining = Number.isFinite(remaining) ? remaining : 0;
    const hasTimeLeft = remaining > 0;
    stop();
    const tick = () => {
      if (remaining <= 0) {
        statusNode.textContent = t("js.wait_soon", { verb: t(verbKey) });
        stop();
        return;
      }
      statusNode.textContent = t("js.wait", { verb: t(verbKey), s: remaining });
      remaining -= 1;
    };
    tick();
    if (hasTimeLeft) countdownId = setInterval(tick, 1000);
  };
  return { start, stop };
}

document.addEventListener("DOMContentLoaded", () => {
  const status = document.getElementById("fetch-status");
  const loadMoreButton = document.getElementById("load-more");
  const loadAllButton = document.getElementById("load-all");
  if (!status || (!loadMoreButton && !loadAllButton)) return;

  const sessionId = (loadMoreButton || loadAllButton).dataset.sessionId;

  const setButtonsDisabled = (disabled) => {
    if (loadMoreButton) loadMoreButton.disabled = disabled;
    if (loadAllButton) loadAllButton.disabled = disabled;
  };

  const { start: startCountdown, stop: stopCountdown } = makeCountdown(status, "js.loading");

  const poll = async (jobId) => {
    try {
      const statusRes = await fetch(`/chats/${sessionId}/status?job_id=${jobId}`);
      if (!statusRes.ok) {
        throw new Error(`HTTP ${statusRes.status}`);
      }
      const data = await statusRes.json();
      if (data.status === "done" || data.status === "failed") {
        stopCountdown();
        status.textContent = data.status === "done" ? t("js.done_reload") : t("js.failed");
        if (data.status === "done") {
          location.reload();
        } else {
          setButtonsDisabled(false);
        }
      } else {
        setTimeout(() => poll(jobId), 3000);
      }
    } catch (error) {
      stopCountdown();
      status.textContent = t("js.conn_lost_page");
      setButtonsDisabled(false);
    }
  };

  const startFetch = async (full) => {
    setButtonsDisabled(true);
    status.textContent = `${t("js.loading")}…`;
    try {
      const url = `/chats/${sessionId}/fetch-full${full ? "?full=true" : ""}`;
      const res = await fetch(url, { method: "POST" });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const { job_id, eta_seconds } = await res.json();
      startCountdown(eta_seconds);
      poll(job_id);
    } catch (error) {
      stopCountdown();
      status.textContent = t("js.start_error");
      setButtonsDisabled(false);
    }
  };

  if (loadMoreButton) loadMoreButton.addEventListener("click", () => startFetch(false));
  if (loadAllButton) loadAllButton.addEventListener("click", () => startFetch(true));
});

document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("project-input");
  const listbox = document.getElementById("project-listbox");
  const dataScript = document.getElementById("project-paths-data");
  if (!input || !listbox || !dataScript) return;

  const { paths, homeDir } = JSON.parse(dataScript.textContent);
  let activeIndex = -1;

  const expandHome = (value) => {
    if (value === "~") return homeDir;
    if (value.startsWith("~/")) return homeDir.replace(/\/$/, "") + "/" + value.slice(2);
    return value;
  };

  const currentMatches = () => {
    const needle = expandHome(input.value).toLowerCase();
    if (!needle) return paths;
    return paths.filter((p) => p.toLowerCase().includes(needle));
  };

  const select = (path) => {
    input.value = path;
    listbox.hidden = true;
    activeIndex = -1;
    input.setAttribute("aria-expanded", "false");
    input.form.requestSubmit();
  };

  const render = (matches) => {
    listbox.innerHTML = "";
    matches.forEach((path, i) => {
      const li = document.createElement("li");
      li.textContent = path;
      li.setAttribute("role", "option");
      if (i === activeIndex) li.classList.add("active");
      li.addEventListener("mousedown", (event) => {
        event.preventDefault();
        select(path);
      });
      listbox.appendChild(li);
    });
    listbox.hidden = matches.length === 0;
    input.setAttribute("aria-expanded", matches.length > 0 ? "true" : "false");
  };

  input.addEventListener("input", () => {
    activeIndex = -1;
    render(currentMatches());
  });

  input.addEventListener("focus", () => {
    render(currentMatches());
  });

  input.addEventListener("keydown", (event) => {
    const matches = currentMatches();
    if (listbox.hidden && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      render(matches);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activeIndex = Math.min(activeIndex + 1, matches.length - 1);
      render(matches);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      render(matches);
    } else if (event.key === "Enter") {
      if (activeIndex >= 0 && matches[activeIndex]) {
        event.preventDefault();
        select(matches[activeIndex]);
      }
    } else if (event.key === "Escape") {
      listbox.hidden = true;
      activeIndex = -1;
      input.setAttribute("aria-expanded", "false");
    }
  });

  input.addEventListener("blur", () => {
    setTimeout(() => {
      listbox.hidden = true;
      input.setAttribute("aria-expanded", "false");
    }, 100);
  });
});

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("command-form");
  if (!form) return;
  const sessionId = form.dataset.sessionId;
  const status = document.getElementById("command-status");
  const button = form.querySelector("button[type=submit]");
  const countdown = makeCountdown(status, "js.sending");

  const poll = (jobId) => {
    fetch(`/jobs/${jobId}/status`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (data.status === "done") {
          countdown.stop();
          status.textContent = t("js.command_done");
        } else if (data.status === "failed") {
          countdown.stop();
          const msg = e2eActive() ? E2E.decryptResult(data.result_text, jobId) : Promise.resolve(data.result_text);
          msg.then((text) => {
            status.textContent = t("js.failed_detail", { msg: text || t("js.unknown_error") });
          });
          button.disabled = false;
        } else {
          setTimeout(() => poll(jobId), 3000);
        }
      })
      .catch(() => {
        countdown.stop();
        status.textContent = t("js.conn_lost");
        button.disabled = false;
      });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = `${t("js.sending")}…`;
    const plainPrompt = form.prompt.value;
    const prepared = preparePrompt(plainPrompt, e2eActive() && E2E.aad.resumePrompt(sessionId));
    prepared
      .then((prompt) =>
        fetch(`/chats/${sessionId}/command`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt }),
        })
      )
      .then((res) =>
        res
          .json()
          .catch(() => ({}))
          .then((data) => ({ ok: res.ok, data }))
      )
      .then(({ ok, data }) => {
        if (!ok) {
          status.textContent = errorText(data && data.detail) || t("js.send_error");
          button.disabled = false;
          return;
        }
        countdown.start(data.eta_seconds);
        poll(data.job_id);
      })
      .catch((error) => {
        countdown.stop();
        status.textContent = (error && error.e2eMessage) || t("js.send_error");
        button.disabled = false;
      });
  });
});

document.addEventListener("DOMContentLoaded", () => {
  const select = document.getElementById("project-select");
  if (!select) return;
  const last = localStorage.getItem("lastProject");
  if (last && [...select.options].some((option) => option.value === last)) {
    select.value = last;
  }
});

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("new-session-form");
  if (!form) return;
  const status = document.getElementById("command-status");
  const button = form.querySelector("button[type=submit]");
  const countdown = makeCountdown(status, "js.sending");

  const poll = (jobId, projectPath) => {
    fetch(`/jobs/${jobId}/status`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (data.status === "done") {
          countdown.stop();
          status.textContent = t("js.new_done");
          button.disabled = false;
        } else if (data.status === "failed") {
          countdown.stop();
          const msg = e2eActive() ? E2E.decryptResult(data.result_text, jobId) : Promise.resolve(data.result_text);
          msg.then((text) => {
            status.textContent = t("js.failed_detail", { msg: text || t("js.unknown_error") });
          });
          button.disabled = false;
        } else {
          setTimeout(() => poll(jobId, projectPath), 3000);
        }
      })
      .catch(() => {
        countdown.stop();
        status.textContent = t("js.conn_lost");
        button.disabled = false;
      });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = `${t("js.sending")}…`;
    const projectPath = form.project_path.value;
    const plainPrompt = form.prompt.value;
    const tool = form.tool.value;
    const prepared = preparePrompt(plainPrompt, e2eActive() && E2E.aad.newSessionPrompt(projectPath, tool));
    prepared
      .then((prompt) =>
        fetch("/projects/command", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_path: projectPath, tool, prompt }),
        })
      )
      .then((res) =>
        res
          .json()
          .catch(() => ({}))
          .then((data) => ({ ok: res.ok, data }))
      )
      .then(({ ok, data }) => {
        if (!ok) {
          status.textContent = errorText(data && data.detail) || t("js.send_error");
          button.disabled = false;
          return;
        }
        localStorage.setItem("lastProject", projectPath);
        countdown.start(data.eta_seconds);
        poll(data.job_id, projectPath);
      })
      .catch((error) => {
        countdown.stop();
        status.textContent = (error && error.e2eMessage) || t("js.send_error");
        button.disabled = false;
      });
  });
});

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("form.toolbar");
  if (!form) return;

  form.querySelectorAll("select").forEach((select) => {
    select.addEventListener("change", () => form.requestSubmit());
  });

  form.querySelectorAll('input[type="text"]').forEach((input) => {
    input.setAttribute("enterkeyhint", "search");
    input.addEventListener("blur", (event) => {
      if (input.id === "e2e-search") return; // searches run via the agent (e2e.js), not on blur
      if (event.relatedTarget && event.relatedTarget.closest("a")) return;
      if (input.value !== input.defaultValue) form.requestSubmit();
    });
  });
});

// Dark/light switch (Settings page). The choice is per device, kept in localStorage and
// applied before first paint by the inline script in base.html.
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("theme-switch");
  if (!toggle) return;

  const effectiveTheme = () => {
    const explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "light" || explicit === "dark") return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };

  toggle.checked = effectiveTheme() === "dark";
  toggle.addEventListener("change", () => {
    const theme = toggle.checked ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("theme", theme);
    } catch (error) {
      /* private mode: the choice just won't persist */
    }
  });
});

afterE2eReady(() => {
  document.querySelectorAll(".copy-button").forEach((button) => {
    const content = button.closest(".message").querySelector(".message-content");
    button.addEventListener("click", async () => {
      const html = content.innerHTML;
      const text = content.textContent;
      try {
        if (window.ClipboardItem) {
          await navigator.clipboard.write([
            new ClipboardItem({
              "text/html": new Blob([html], { type: "text/html" }),
              "text/plain": new Blob([text], { type: "text/plain" }),
            }),
          ]);
        } else {
          await navigator.clipboard.writeText(text);
        }
        button.textContent = "✓";
      } catch (error) {
        button.textContent = "✗";
      }
      setTimeout(() => {
        button.textContent = "📋";
      }, 1500);
    });
  });
});

// Voice input for the prompt textareas. Feature-detected: on browsers without
// SpeechRecognition (notably iOS Safari, which has never implemented it) the
// mic button simply stays hidden — no dead UI, no error.
document.addEventListener("DOMContentLoaded", () => {
  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognitionImpl) return;

  document.querySelectorAll(".mic-button").forEach((button) => {
    const textarea = button.closest(".prompt-row")?.querySelector("textarea");
    if (!textarea) return;
    button.hidden = false;

    let recognition = null;
    let baseValue = "";

    const stopListening = () => {
      button.classList.remove("listening");
      if (recognition) {
        recognition.onresult = null;
        recognition.onerror = null;
        recognition.onend = null;
        recognition.stop();
        recognition = null;
      }
    };

    button.addEventListener("click", () => {
      if (recognition) {
        stopListening();
        return;
      }
      baseValue = textarea.value;
      recognition = new SpeechRecognitionImpl();
      recognition.lang = t("js.speech_lang");
      recognition.interimResults = true;
      recognition.continuous = true;

      recognition.onresult = (event) => {
        let transcript = "";
        for (let i = 0; i < event.results.length; i++) {
          transcript += event.results[i][0].transcript;
        }
        textarea.value = baseValue ? `${baseValue} ${transcript}` : transcript;
      };
      recognition.onerror = stopListening;
      recognition.onend = stopListening;

      recognition.start();
      button.classList.add("listening");
    });
  });
});

// Manual reload: the app runs as an iOS home-screen PWA, which gets no native
// reload gesture at all (no browser chrome, and standalone mode ignores the
// bounce-to-refresh a normal Safari tab has), so it needs an explicit button.
document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("reload-button");
  if (!button) return;
  button.addEventListener("click", () => location.reload());
});

// Message tables: click a header to sort (asc → desc → original order), frontend-only.
// Numeric columns (incl. %, currency, thousands separators) are right-aligned and
// sorted numerically; everything else sorts as text with natural/locale ordering.
afterE2eReady(() => {
  const parseNumber = (raw) => {
    let text = raw.trim().replace(/[\s\u00a0%€$£]/g, "");
    if (!/^[-+−]?[\d.,]*\d[\d.,]*$/.test(text)) return null;
    text = text.replace("−", "-");
    const lastComma = text.lastIndexOf(",");
    const lastDot = text.lastIndexOf(".");
    if (lastComma > -1 && lastDot > -1) {
      // The later separator is the decimal one.
      text = lastComma > lastDot ? text.replace(/\./g, "").replace(",", ".") : text.replace(/,/g, "");
    } else if (lastComma > -1) {
      text = /^[-+]?\d{1,3}(,\d{3})+$/.test(text) ? text.replace(/,/g, "") : text.replace(",", ".");
    } else if (/^[-+]?\d{1,3}(\.\d{3}){2,}$/.test(text)) {
      text = text.replace(/\./g, "");
    }
    const value = Number(text);
    return Number.isFinite(value) ? value : null;
  };
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  document.querySelectorAll(".message-content table").forEach((table) => {
    const headRow = table.tHead && table.tHead.rows[0];
    const body = table.tBodies[0];
    if (!headRow || !body) return;
    const rows = [...body.rows];
    const originalOrder = rows.slice();
    const headers = [...headRow.cells];

    const columns = headers.map((_, index) => {
      const values = rows.map((row) => (row.cells[index] ? row.cells[index].textContent : ""));
      const filled = values.filter((v) => v.trim() !== "");
      const numbers = values.map(parseNumber);
      const numeric = filled.length > 0 && filled.every((v) => parseNumber(v) !== null);
      return { numeric, numbers };
    });

    columns.forEach((column, index) => {
      if (!column.numeric) return;
      headers[index].classList.add("num");
      rows.forEach((row) => row.cells[index] && row.cells[index].classList.add("num"));
    });

    const sortBy = (index, direction) => {
      headers.forEach((h, i) => {
        if (i === index && direction) h.setAttribute("aria-sort", direction);
        else h.removeAttribute("aria-sort");
      });
      let ordered = originalOrder;
      if (direction) {
        const sign = direction === "ascending" ? 1 : -1;
        const column = columns[index];
        const key = (row) => (row.cells[index] ? row.cells[index].textContent.trim() : "");
        ordered = rows.slice().sort((a, b) => {
          if (column.numeric) {
            const na = column.numbers[rows.indexOf(a)];
            const nb = column.numbers[rows.indexOf(b)];
            if (na === null && nb === null) return 0;
            if (na === null) return 1;
            if (nb === null) return -1;
            return sign * (na - nb);
          }
          return sign * collator.compare(key(a), key(b));
        });
      }
      ordered.forEach((row) => body.appendChild(row));
    };

    headers.forEach((header, index) => {
      header.classList.add("sortable");
      header.tabIndex = 0;
      header.setAttribute("role", "columnheader");
      const toggle = () => {
        const current = header.getAttribute("aria-sort");
        sortBy(index, current === "ascending" ? "descending" : current === "descending" ? null : "ascending");
      };
      header.addEventListener("click", toggle);
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggle();
        }
      });
    });
  });
});

// Chat images: a button stands in for an image the server doesn't hold yet. A click queues
// a job, the agent uploads the file, and we poll until the image can replace the button.
document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".image-fetch");
    if (!button || button.getAttribute("aria-busy") === "true") return;
    const sessionId = button.dataset.sessionId;
    const label = button.querySelector(".image-fetch-label");
    // The plain label is remembered once: retries must not pile messages onto the last one.
    if (!label.dataset.original) label.dataset.original = label.textContent;
    const original = label.dataset.original;
    const fail = (message) => {
      button.removeAttribute("aria-busy");
      label.textContent = `${original} — ${message}`;
    };
    const render = (url) => {
      const link = document.createElement("a");
      link.className = "chat-image";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      const img = document.createElement("img");
      img.src = url;
      img.alt = original;
      link.appendChild(img);
      button.replaceWith(link);
    };
    // In E2E mode the server holds ciphertext: fetch, decrypt and show it as a blob: URL.
    const show = (url) => {
      if (e2eRequired && !e2eActive()) return fail(t("js.image_failed"));
      if (!e2eActive()) return render(url);
      return E2E.loadImage(sessionId, button.dataset.path, url).then(render, () => fail(t("js.image_failed")));
    };

    button.setAttribute("aria-busy", "true");
    try {
      const res = await fetch(`/chats/${encodeURIComponent(sessionId)}/fetch-image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: button.dataset.path }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return fail(errorText(data.detail) || t("js.image_failed"));
      if (data.available) return show(data.url);

      label.textContent = t("js.image_waiting", { s: data.eta_seconds });
      const poll = async () => {
        try {
          const statusRes = await fetch(
            `/chats/${encodeURIComponent(sessionId)}/status?job_id=${encodeURIComponent(data.job_id)}`
          );
          if (!statusRes.ok) throw new Error(`HTTP ${statusRes.status}`);
          const job = await statusRes.json();
          if (job.status === "done") return show(data.url);
          if (job.status === "failed") return fail(t("js.image_failed"));
          setTimeout(poll, 2000);
        } catch (error) {
          fail(t("js.conn_lost"));
        }
      };
      poll();
    } catch (error) {
      fail(t("js.conn_lost"));
    }
  });
});
