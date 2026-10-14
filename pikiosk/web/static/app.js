/* Pi Video Kiosk touch grid. Talks only to the loopback API. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const grid = $("grid");
  const empty = $("empty");
  const banner = $("banner");
  const toast = $("toast");
  const locked = $("locked");
  const hotspot = $("hotspot");
  const pinpad = $("pinpad");

  const ui = {
    columns: 3,
    show_titles: false,
    locked_screen: "black",
    parent_unlock: false,
    hold_corner: "top_right",
    hold_s: 5,
    device_name: "",
    catalog_version: "",
  };

  let cards = new Map();
  let state = null;
  let catalogVersion = "";
  let tapLockUntil = 0;
  let toastTimer = null;
  let lastErrorKey = "";
  let clockTimer = null;
  let holdTimer = null;
  let pin = "";
  let pinBusy = false;

  const prevent = (event) => event.preventDefault();
  document.addEventListener("contextmenu", prevent);
  document.addEventListener("selectstart", prevent);
  document.addEventListener("dragstart", prevent);
  document.addEventListener("gesturestart", prevent);

  /* ---------------------------------------------------------------- data */

  async function getJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${url} ${response.status}`);
    return response.json();
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try {
      data = await response.json();
    } catch (_error) {
      data = null;
    }
    return { status: response.status, data };
  }

  async function loadUi() {
    try {
      Object.assign(ui, await getJson("/api/ui"));
    } catch (_error) {
      /* defaults stand */
    }
    document.documentElement.style.setProperty("--cols", String(Math.max(1, Math.min(6, ui.columns || 3))));
    layout();
    hotspot.hidden = !ui.parent_unlock;
    hotspot.className = `hotspot ${ui.hold_corner || "top_right"}`;
    locked.querySelector(".locked-name").textContent = ui.device_name || "";
  }

  async function loadCatalog() {
    let items;
    try {
      items = await getJson("/api/catalog");
    } catch (_error) {
      setTimeout(loadCatalog, 3000);
      return;
    }
    render(items);
    if (state) apply(state);
  }

  /* -------------------------------------------------------------- render */

  function layout() {
    // Rows that fit whole 16:9-ish cards at this size (3 at 800x480 with 3 columns).
    const cols = Math.max(1, Math.min(6, ui.columns || 3));
    const pad = 16;
    const gap = 16;
    const width = window.innerWidth;
    const height = window.innerHeight;
    const colWidth = (width - 2 * pad - (cols - 1) * gap) / cols;
    const cardHeight = colWidth * 9 / 16;
    const rows = Math.max(1, Math.floor((height - 2 * pad + gap) / (cardHeight + gap)));
    document.documentElement.style.setProperty("--rows", String(rows));
  }

  window.addEventListener("resize", layout);

  function hue(text) {
    let h = 0;
    for (const ch of text) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return h;
  }

  function card(item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "card";
    button.dataset.name = item.name;
    button.setAttribute("aria-label", item.name);
    button.style.setProperty("--placeholder", `hsl(${hue(item.name)} 45% 32%)`);

    const placeholder = document.createElement("div");
    placeholder.className = "placeholder";
    placeholder.textContent = item.type === "file" ? "▶" : (item.name.trim()[0] || "▶").toUpperCase();
    button.appendChild(placeholder);

    if (item.thumb) {
      const img = document.createElement("img");
      img.className = "thumb";
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      img.draggable = false;
      img.src = item.thumb;
      img.addEventListener("error", () => img.remove());
      button.appendChild(img);
    }

    const shade = document.createElement("div");
    shade.className = "shade";
    button.appendChild(shade);

    if (item.type === "playlist") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.innerHTML = '<i class="stack"></i>Playlist';
      button.appendChild(badge);
    } else if (item.type === "file") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "Video file";
      button.appendChild(badge);
    }

    if (ui.show_titles) {
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = item.name;
      button.appendChild(title);
    }

    const busy = document.createElement("div");
    busy.className = "busy";
    busy.innerHTML = '<div class="spinner"></div><div class="label">Getting ready…</div><div class="bar"><i></i></div>';
    button.appendChild(busy);

    button.addEventListener("click", () => tap(item.name, button));
    return button;
  }

  function render(items) {
    cards = new Map();
    const nodes = [];
    for (const item of items) {
      const node = card(item);
      cards.set(item.name, node);
      nodes.push(node);
    }
    grid.replaceChildren(...nodes);
    empty.hidden = items.length > 0;
  }

  /* ---------------------------------------------------------------- taps */

  async function tap(name, button) {
    const now = Date.now();
    if (now < tapLockUntil) return;
    if (state && state.ui_locked) return;
    tapLockUntil = now + 1500;
    button.classList.add("loading");
    setProgress(button, null);
    const result = await postJson("/api/play", { name });
    if (result.status === 429) return;
    if (result.status === 403) {
      button.classList.remove("loading");
      showToast(result.data && result.data.error === "locked" ? "The kiosk is locked right now." : "That one is not allowed.");
      return;
    }
    if (result.status >= 400) {
      button.classList.remove("loading");
      showToast("Something went wrong. Try again.");
      return;
    }
    if (result.data) apply(result.data);
  }

  function setProgress(button, percent) {
    const label = button.querySelector(".label");
    const bar = button.querySelector(".bar > i");
    if (percent === null || percent === undefined) {
      label.textContent = "Getting ready…";
      bar.style.width = "0%";
      return;
    }
    label.textContent = percent >= 100 ? "Starting…" : `Getting ready… ${percent}%`;
    bar.style.width = `${Math.max(2, Math.min(100, percent))}%`;
  }

  /* --------------------------------------------------------------- state */

  function friendlyError(kind, detail) {
    switch (kind) {
      case "network":
        return "No internet right now. Try again in a bit.";
      case "unavailable":
        return "That video is not available any more.";
      case "age_restricted":
        return "That video is for grown-ups.";
      case "geo":
        return "That video is not available here.";
      case "bot_check":
      case "extractor":
        return "YouTube is being difficult. Try again in a minute.";
      case "player":
        return "Could not play that one. Try again.";
      case "not_cached":
        return "That video is not downloaded yet.";
      default:
        return detail ? `Could not play: ${detail}` : "Could not play that one.";
    }
  }

  function apply(next) {
    state = next;
    if (next.catalog_version && next.catalog_version !== catalogVersion) {
      const first = catalogVersion === "";
      catalogVersion = next.catalog_version;
      if (!first) loadCatalog();
    }
    setLocked(Boolean(next.ui_locked));

    for (const [name, node] of cards) {
      const selected = next.selected === name;
      const loading = selected && next.player === "loading";
      node.classList.toggle("selected", selected && next.player !== "idle" && next.player !== "error");
      node.classList.toggle("playing", selected && (next.player === "playing" || next.player === "paused"));
      node.classList.toggle("loading", loading);
      if (loading) setProgress(node, next.download_progress);
    }

    if (next.player === "error" && next.last_error) {
      const key = `${next.last_error}|${next.error_detail || ""}`;
      if (key !== lastErrorKey) {
        lastErrorKey = key;
        showToast(friendlyError(next.last_error, ""));
      }
    } else if (next.player !== "error") {
      lastErrorKey = "";
    }
  }

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toast.hidden = true;
    }, 6000);
  }

  /* -------------------------------------------------------------- locked */

  function setLocked(on) {
    locked.hidden = !on;
    const logo = locked.querySelector(".locked-logo");
    const clock = locked.querySelector(".locked-clock");
    logo.hidden = !(on && ui.locked_screen === "logo");
    clock.hidden = !(on && ui.locked_screen === "clock");
    if (on && ui.locked_screen === "clock") {
      tickClock();
      if (!clockTimer) clockTimer = setInterval(tickClock, 15000);
    } else if (clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
    if (!on) hidePin();
  }

  function tickClock() {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    locked.querySelector(".clock-time").textContent = `${hh}:${mm}`;
    locked.querySelector(".clock-date").textContent = now.toLocaleDateString(undefined, {
      weekday: "long",
      day: "numeric",
      month: "long",
    });
  }

  /* ------------------------------------------------------------- PIN pad */

  function startHold() {
    cancelHold();
    holdTimer = setTimeout(showPin, Math.max(1, ui.hold_s || 5) * 1000);
  }

  function cancelHold() {
    if (holdTimer) clearTimeout(holdTimer);
    holdTimer = null;
  }

  hotspot.addEventListener("pointerdown", startHold);
  hotspot.addEventListener("pointerup", cancelHold);
  hotspot.addEventListener("pointercancel", cancelHold);
  hotspot.addEventListener("pointerleave", cancelHold);

  function showPin() {
    pin = "";
    pinBusy = false;
    drawPin();
    setPinMessage("");
    pinpad.hidden = false;
  }

  function hidePin() {
    pinpad.hidden = true;
    pin = "";
  }

  function drawPin() {
    const dots = pinpad.querySelector(".pin-dots");
    dots.replaceChildren(...Array.from({ length: pin.length }, () => document.createElement("i")));
  }

  function setPinMessage(text) {
    const node = pinpad.querySelector(".pin-message");
    node.textContent = text;
    node.hidden = !text;
  }

  async function submitPin() {
    if (pinBusy || !pin) return;
    pinBusy = true;
    const result = await postJson("/api/unlock", { pin });
    pinBusy = false;
    if (result.status === 200) {
      hidePin();
      return;
    }
    pin = "";
    drawPin();
    const cardNode = pinpad.querySelector(".pin-card");
    cardNode.classList.remove("shake");
    void cardNode.offsetWidth;
    cardNode.classList.add("shake");
    if (result.status === 429) {
      setPinMessage("Too many tries. Wait a few minutes.");
    } else if (result.status === 403 && result.data && result.data.error === "disabled") {
      setPinMessage("Parent unlock is switched off.");
    } else {
      setPinMessage("Wrong PIN.");
    }
  }

  pinpad.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-key]");
    if (!button) return;
    const key = button.dataset.key;
    if (key === "cancel") {
      hidePin();
    } else if (key === "back") {
      pin = pin.slice(0, -1);
      drawPin();
    } else if (key === "ok") {
      submitPin();
    } else if (pin.length < 8) {
      pin += key;
      drawPin();
      setPinMessage("");
    }
  });

  /* ------------------------------------------------------------- socket */

  function connect() {
    let socket;
    try {
      socket = new WebSocket(`ws://${location.host}/ws`);
    } catch (_error) {
      setTimeout(connect, 2000);
      return;
    }
    const heartbeat = setInterval(() => {
      if (socket.readyState === 1) socket.send("heartbeat");
    }, 30000);
    socket.onopen = () => {
      banner.hidden = true;
      loadCatalog();
    };
    socket.onmessage = (event) => {
      try {
        apply(JSON.parse(event.data));
      } catch (_error) {
        /* ignore malformed frames */
      }
    };
    socket.onclose = () => {
      clearInterval(heartbeat);
      banner.hidden = false;
      setTimeout(connect, 2000);
    };
    socket.onerror = () => socket.close();
  }

  /* --------------------------------------------------------------- boot */

  loadUi().then(() => {
    connect();
  });
})();
