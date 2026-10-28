/*
 * RAVAN mobile — application controller v2.0
 *
 * One request at a time, one voice at a time.
 * Supports:
 *   - Auto-growing message input with keyboard accommodation
 *   - Intelligent face resizing & collapsing priority system
 *   - Chat auto-scrolling with "Scroll to latest" badge
 *   - File attachments (PDF, images, TXT, DOCX, CSV)
 *   - Floating RAVAN assistant native bridge
 *   - Audio/mic conflict prevention
 */

import { Api, ApiError, loadLocalSettings, saveLocalSettings } from "./api.js";
import { Avatar } from "./avatar.js";
import { Voice, estimateSpeechSeconds } from "./speech.js";
import { Actions } from "./actions.js";

const $ = (id) => document.getElementById(id);

const LABELS = {
  idle: "STANDBY",
  listening: "LISTENING",
  thinking: "THINKING",
  speaking: "SPEAKING",
  offline: "OFFLINE",
  error: "CONNECTION ERROR"
};

const settings = loadLocalSettings();
const sessionId = (() => {
  try {
    const existing = sessionStorage.getItem("ravan.session");
    if (existing) return existing;
    const id = newId();
    sessionStorage.setItem("ravan.session", id);
    return id;
  } catch {
    return newId();
  }
})();

let api;
let avatar;
let voice;
let actions;

let phase = "idle";
let busy = false;
let muted = Boolean(settings.muted);
let voiceEnabled = settings.voiceEnabled !== false;
let offline = false;
let abort = null;
let speechToken = 0;
let pendingConfirm = null;
let warnedAuth = false;
let warnedMock = false;
let toastTimer = 0;
let lastBackend = "";

// Face & Layout state
let faceManualHidden = Boolean(settings.faceHidden);
let isKeyboardOpen = false;
let currentAttachment = null;

function newId() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// ── UI primitives ───────────────────────────────────────────────────────────

function setPhase(name) {
  phase = name;
  document.body.dataset.state = name === "offline" ? "error" : name;
  const stateLabel = $("state-label");
  if (stateLabel) stateLabel.textContent = LABELS[name] || name.toUpperCase();
  const avatarState =
    name === "idle" ? "STANDBY"
    : name === "offline" || name === "error" ? "OFFLINE"
    : name.toUpperCase();
  avatar?.setState(avatarState);
  $("mic-btn")?.setAttribute("aria-pressed", String(name === "listening"));
  updateStopButton();
}

function syncPhase() {
  if (voice?.listening) setPhase("listening");
  else if (voice?.speaking) setPhase("speaking");
  else if (busy) setPhase("thinking");
  else setPhase(offline ? "offline" : "idle");
}

function updateStopButton() {
  const visible = Boolean(busy || voice?.speaking);
  const stopBtn = $("stop-btn");
  if (stopBtn) stopBtn.hidden = !visible;
  const sendBtn = $("send-btn");
  if (sendBtn) sendBtn.disabled = busy;
}

function toast(message, ms = 3200) {
  const el = $("toast");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, ms);
}

// ── Intelligent Chat Scrolling & Cards ──────────────────────────────────────

function isNearBottom() {
  const transcript = $("transcript");
  if (!transcript) return true;
  return (transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight) < 80;
}

function scrollChatToBottom(smooth = true) {
  const transcript = $("transcript");
  if (!transcript) return;
  transcript.scrollTo({
    top: transcript.scrollHeight,
    behavior: smooth ? "smooth" : "auto"
  });
  const scrollBtn = $("scroll-bottom-btn");
  if (scrollBtn) scrollBtn.hidden = true;
}

function formatBytes(bytes) {
  if (!bytes || bytes < 0) return "0 B";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function getFileIcon(mime = "", name = "") {
  if (mime.startsWith("image/")) return "🖼️";
  if (mime.includes("pdf") || name.endsWith(".pdf")) return "📄";
  if (name.endsWith(".csv")) return "📊";
  if (name.endsWith(".docx") || name.endsWith(".doc")) return "📝";
  return "📎";
}

function addCard(kind, who, text, { attachment = null } = {}) {
  const transcript = $("transcript");
  $("empty-hint").hidden = true;
  const wasBottom = isNearBottom();

  const card = document.createElement("div");
  card.className = `card ${kind}`;

  if (who) {
    const label = document.createElement("span");
    label.className = "who";
    label.textContent = who;
    card.appendChild(label);
  }

  // If this card carried an attachment, show attachment badge
  if (attachment) {
    const badge = document.createElement("div");
    badge.className = "card-att-badge";
    const icon = document.createElement("span");
    icon.className = "badge-icon";
    icon.textContent = getFileIcon(attachment.mime_type, attachment.name);
    const name = document.createElement("span");
    name.className = "badge-name";
    name.textContent = `${attachment.name} (${formatBytes(attachment.size)})`;
    badge.appendChild(icon);
    badge.appendChild(name);
    card.appendChild(badge);
  }

  if (text) {
    const body = document.createElement("div");
    body.textContent = text;
    card.appendChild(body);
  }

  transcript.appendChild(card);

  while (transcript.querySelectorAll(".card").length > 100) {
    transcript.querySelector(".card")?.remove();
  }

  if (wasBottom || kind === "user") {
    scrollChatToBottom(true);
  }

  updateFaceSpace();
  return card;
}

function describeCall(call) {
  if (call.name === "open_app") return `Open ${actions.label(call.args?.app)}`;
  if (call.name === "open_url") return "Open link";
  if (call.name === "play_on_youtube") return `YouTube search “${call.args?.query || ""}”`;
  if (call.name === "compose_whatsapp_message") return "WhatsApp message";
  return call.name;
}

function addActionCard(call, out) {
  const mark = out.ok ? "•" : "!";
  addCard(out.ok ? "sys" : "err", null, `${mark} ${describeCall(call)} — ${out.detail}`);
}

function setNet(kind, text) {
  const el = $("net-state");
  if (!el) return;
  el.className = `net ${kind}`.trim();
  el.textContent = text;
}

function setOffline(message) {
  offline = true;
  $("offline-text").textContent = message;
  $("offline-banner").hidden = false;
  setNet("offline", "OFFLINE");
  if (!busy && !voice?.speaking && !voice?.listening) setPhase("offline");
}

function setOnline(mode) {
  offline = false;
  $("offline-banner").hidden = true;
  const live = mode !== "mock";
  setNet(live ? "online" : "mock", live ? "LIVE" : "MOCK");
  $("about-mode").textContent = `${live ? "LIVE" : "MOCK"} · ${api.describe}`;
  if (!busy && !voice?.speaking && !voice?.listening) setPhase("idle");
}

// ── Face Resizing & Priority System ─────────────────────────────────────────

function updateFaceSpace(kbOpen) {
  if (typeof kbOpen === "boolean") isKeyboardOpen = kbOpen;
  const stage = $("stage");
  if (!stage) return;
  const transcript = $("transcript");
  const cardCount = transcript ? transcript.querySelectorAll(".card").length : 0;
  const vh = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  const faceToggle = $("face-toggle-btn");

  // Priority 3: Forced collapsed when keyboard is open, manually hidden, or tiny height (< 420px)
  if (faceManualHidden || isKeyboardOpen || vh < 420) {
    stage.className = "stage mode-collapsed";
    avatar?.setVisible(false);
    faceToggle?.classList.remove("active");
  } else if (cardCount > 1 || vh < 680) {
    // Priority 2: Limited space (active chat or compact screen) -> Compact Face
    stage.className = "stage mode-compact";
    avatar?.setVisible(true);
    faceToggle?.classList.add("active");
  } else {
    // Priority 1: Spacious Normal Mode
    stage.className = "stage mode-normal";
    avatar?.setVisible(true);
    faceToggle?.classList.add("active");
  }
}

function toggleFaceManual() {
  faceManualHidden = !faceManualHidden;
  saveLocalSettings({ faceHidden: faceManualHidden });
  updateFaceSpace();
  toast(faceManualHidden ? "Face collapsed. Tap Face to restore." : "Face restored.");
}

// ── Auto-Growing Input & Attachments ────────────────────────────────────────

function adjustInputHeight() {
  const input = $("text-input");
  if (!input) return;
  input.style.height = "auto";
  const target = Math.min(120, Math.max(42, input.scrollHeight));
  input.style.height = `${target}px`;
}

function handleFileSelected(file) {
  if (!file) return;
  const maxBytes = 10 * 1024 * 1024; // 10MB
  if (file.size > maxBytes) {
    toast("File is too large (maximum 10 MB).", 4000);
    return;
  }

  const isText =
    file.type.startsWith("text/") ||
    /\.(txt|csv|json|md|py|js|html|xml)$/i.test(file.name);

  const reader = new FileReader();
  reader.onload = () => {
    currentAttachment = {
      name: file.name,
      size: file.size,
      mime_type: file.type || (isText ? "text/plain" : "application/octet-stream"),
      ...(isText ? { text_content: String(reader.result) } : { data: String(reader.result) })
    };

    $("att-icon").textContent = getFileIcon(currentAttachment.mime_type, file.name);
    $("att-name").textContent = file.name;
    $("att-size").textContent = formatBytes(file.size);
    $("attachment-preview").hidden = false;
    updateFaceSpace();
    $("text-input")?.focus();
  };

  reader.onerror = () => {
    toast("Could not read the selected file.", 3500);
  };

  if (isText) reader.readAsText(file);
  else reader.readAsDataURL(file);
}

function clearAttachment() {
  currentAttachment = null;
  const fileInput = $("file-input");
  if (fileInput) fileInput.value = "";
  const preview = $("attachment-preview");
  if (preview) preview.hidden = true;
  updateFaceSpace();
}

// ── Backend health & conversation ───────────────────────────────────────────

async function checkHealth({ attempts = 3, quiet = false } = {}) {
  if (!api.configured) {
    setOffline("No backend configured. Open Settings and add your Render URL.");
    return false;
  }
  if (!quiet) setNet("", "CONNECTING");
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const health = await api.health();
      setOnline(health.mode);
      if (health.mode === "mock" && !warnedMock) {
        warnedMock = true;
        addCard(
          "sys",
          null,
          "Backend is in mock mode: replies are canned and no Gemini call is made. Set GEMINI_API_KEY on Render for live answers."
        );
      }
      if (health.auth_required && !api.token && !warnedAuth) {
        warnedAuth = true;
        addCard("sys", null, "This backend requires an access token. Add it in Settings before sending.");
      }
      return true;
    } catch (err) {
      const retryable = err.code === "network" || err.code === "timeout";
      if (retryable && attempt < attempts - 1) {
        await new Promise((resolve) => setTimeout(resolve, 2200));
        continue;
      }
      setOffline(
        err.code === "config"
          ? err.message
          : "Backend unreachable. It may be waking up — Retry, or check the URL in Settings."
      );
      return false;
    }
  }
  return false;
}

function startHealthPolling() {
  setInterval(() => {
    if (document.hidden) return;
    checkHealth({ attempts: 1, quiet: true });
  }, 60000);
}

async function runChain(payload) {
  if (busy) {
    toast("Still answering the previous request.");
    return;
  }
  busy = true;
  updateStopButton();
  syncPhase();
  try {
    await step(payload, 0);
  } catch (err) {
    if (err?.code !== "aborted") presentError(err);
  } finally {
    busy = false;
    abort = null;
    updateStopButton();
    syncPhase();
  }
}

async function step(payload, depth) {
  if (depth > 3) {
    addCard("err", null, "That needed too many chained actions, so I stopped. Try a simpler request.");
    return;
  }
  const controller = new AbortController();
  abort = controller;
  syncPhase();

  const res = await api.chat(
    {
      sessionId,
      requestId: newId(),
      text: payload.text,
      toolResults: payload.toolResults,
      localTime: new Date().toString(),
      attachment: payload.attachment
    },
    { signal: controller.signal }
  );

  await handleReply(res, depth);
}

async function handleReply(res, depth) {
  if (res.reply) {
    addCard("ai", "RAVAN", res.reply);
    speakReply(res.reply, res.visemes);
  }

  if (res.confirmation) {
    const others = res.calls.filter((call) => !call.confirm);
    const otherResults = [];
    for (const call of others) {
      const out = await actions.execute(call);
      otherResults.push({ name: call.name, call_id: call.id, result: out });
      addActionCard(call, out);
    }
    pendingConfirm = { confirmation: res.confirmation, calls: res.calls, otherResults };
    showConfirm(res.confirmation);
    return;
  }

  const calls = (res.calls || []).filter((call) => !call.confirm);
  if (!calls.length) return;

  const results = [];
  for (const call of calls) {
    const out = await actions.execute(call);
    results.push({ name: call.name, call_id: call.id, result: out });
    addActionCard(call, out);
  }
  if (results.length && depth < 3) {
    await step({ toolResults: results }, depth + 1);
  }
}

async function speakReply(text, visemes) {
  if (!voiceEnabled || !String(text).trim()) return;
  const token = ++speechToken;
  voice.stopSpeaking();
  avatar?.stopSpeech();

  const seconds = estimateSpeechSeconds(text);
  avatar?.beginSpeech(visemes, seconds);
  const speaking = voice.speak(text);
  updateStopButton();
  syncPhase();

  try {
    await speaking;
  } finally {
    if (token === speechToken) {
      avatar?.endSpeech();
      updateStopButton();
      syncPhase();
    }
  }
}

function interrupt() {
  speechToken += 1;
  avatar?.stopSpeech();
  voice?.stopSpeaking();
  voice?.stopListening();
  const wasBusy = busy;
  busy = false;
  if (abort) {
    abort.abort();
    abort = null;
  }
  updateStopButton();
  syncPhase();
  if (wasBusy) toast("Stopped.");
}

function presentError(err) {
  if (err instanceof ApiError) {
    if (err.code === "auth") {
      addCard("err", null, "The backend rejected the request. Add the access token in Settings.");
      toast("Access token required.");
      openSettings();
      return;
    }
    if (err.code === "busy") {
      toast(err.message);
      return;
    }
    if (err.code === "rate") {
      addCard("err", null, `${err.message}${err.retryAfter ? ` (try again in ${err.retryAfter}s)` : ""}`);
      return;
    }
    if (err.code === "network" || err.code === "timeout") {
      addCard("err", null, err.message);
      setOffline("Backend unreachable. It may be waking up — Retry, or check the URL in Settings.");
      return;
    }
    addCard("err", null, err.message);
    return;
  }
  addCard("err", null, "Something went wrong on this device. Try again.");
}

function sendCurrentTurn() {
  const input = $("text-input");
  const text = input ? input.value.trim() : "";
  const att = currentAttachment;

  if (!text && !att) return;
  if (text.length > 4000) {
    toast("That message is too long (4000 characters max).");
    return;
  }
  if (busy) {
    toast("Still answering the previous request.");
    return;
  }

  if (voice?.listening) voice.stopListening();

  addCard("user", "YOU", text, { attachment: att });

  if (input) {
    input.value = "";
    adjustInputHeight();
  }
  clearAttachment();

  runChain({ text, attachment: att });
}

// ── WhatsApp confirmation ───────────────────────────────────────────────────

function showConfirm(confirmation) {
  $("confirm-contact").textContent = confirmation.contact || "choose a contact in WhatsApp";
  $("confirm-message").textContent = confirmation.message || "";
  $("confirm-sheet").hidden = false;
  $("confirm-ok").disabled = false;
  $("confirm-cancel").disabled = false;
  $("confirm-ok").focus();
}

function hideConfirm() {
  $("confirm-sheet").hidden = true;
}

async function resolveWhatsApp(confirmed) {
  const pending = pendingConfirm;
  pendingConfirm = null;
  $("confirm-ok").disabled = true;
  $("confirm-cancel").disabled = true;
  hideConfirm();
  if (!pending) return;

  const deadline = Date.now() + 2000;
  while (busy && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }

  const call =
    pending.calls.find((c) => c.id === pending.confirmation.call_id) ||
    pending.calls.find((c) => c.name === "compose_whatsapp_message");
  if (!call) return;

  if (!confirmed) {
    addCard("sys", null, "WhatsApp cancelled. Nothing was opened or sent.");
    await runChain({
      toolResults: [
        ...pending.otherResults,
        {
          name: call.name,
          call_id: call.id,
          result: { ok: false, detail: "The user cancelled. Nothing was sent." }
        }
      ]
    });
    return;
  }

  const out = await actions.execute(call, { confirmed: true });
  addActionCard(call, out);
  await runChain({
    toolResults: [
      ...pending.otherResults,
      { name: call.name, call_id: call.id, result: out }
    ]
  });
}

// ── Microphone, Audio & Mute Coordination ───────────────────────────────────

function setMuted(next, { announce = true } = {}) {
  muted = Boolean(next);
  saveLocalSettings({ muted });
  $("muted-chip").hidden = !muted;
  $("mic-btn").classList.toggle("is-muted", muted);
  $("cfg-mic-muted").checked = muted;
  if (muted) voice?.stopListening();
  actions?.setMicMuted(muted);
  if (announce) toast(muted ? "Microphone muted." : "Microphone active.");
  syncPhase();
}

async function toggleMic() {
  if (!voice) return;
  if (voice.listening) {
    voice.stopListening();
    return;
  }

  // Interruption: if AI is speaking or thinking, abort immediately before opening mic
  if (busy || voice.speaking) interrupt();
  if (muted) setMuted(false, { announce: false });

  if (!voice.canListen) {
    toast("Voice recognition is not available here. Type your command instead.");
    return;
  }

  speechToken += 1;
  voice.stopSpeaking();
  avatar?.stopSpeech();

  const allowed = await voice.ensureMicPermission();
  if (!allowed) {
    toast("Microphone permission required for voice.", 3500);
    return;
  }
  if (muted) return;

  $("text-input").placeholder = "Listening…";
  voice.listenOnce({ lang: navigator.language || "en-US" });
}

// ── Settings & Floating Assistant ───────────────────────────────────────────

function openSettings() {
  $("settings-sheet").hidden = false;
  $("settings-close").focus();
}

function closeSettings() {
  const backend = $("cfg-backend").value.trim();
  const token = $("cfg-token").value.trim();
  const changed = backend !== lastBackend || token !== api.token;
  try {
    api.setConfig({ backendUrl: backend, apiToken: token });
    saveLocalSettings({ backendUrl: backend, apiToken: token });
  } catch (err) {
    toast(err.message, 4200);
    return;
  }
  lastBackend = backend;
  $("settings-sheet").hidden = true;
  if (changed) checkHealth();
}

async function toggleFloating(wanted) {
  const checkbox = $("cfg-floating");
  const quickBtn = $("quick-floating-btn");
  if (checkbox) checkbox.disabled = true;
  if (quickBtn) quickBtn.disabled = true;

  try {
    const out = await actions.setFloating(wanted);
    if (!out?.ok) {
      if (checkbox) checkbox.checked = !wanted;
      if (quickBtn) quickBtn.classList.toggle("active", !wanted);
      if (out?.permissionRequired) {
        toast("Allow “display over other apps” for RAVAN in Android settings, then try again.", 5000);
      } else {
        toast(out?.detail || "Could not toggle floating assistant.", 4200);
      }
      return;
    }
    const active = Boolean(out.enabled);
    if (checkbox) checkbox.checked = active;
    if (quickBtn) quickBtn.classList.toggle("active", active);
    saveLocalSettings({ floating: active });
    toast(active ? "Floating assistant enabled." : "Floating assistant hidden.");
  } finally {
    if (checkbox) checkbox.disabled = false;
    if (quickBtn) quickBtn.disabled = false;
  }
}

async function syncNativeState() {
  if (!actions.isNative) return;
  $("native-section").hidden = false;
  const state = await actions.floatingState();
  if (!state) return;
  const isEnabled = Boolean(state.enabled);
  $("cfg-floating").checked = isEnabled;
  $("quick-floating-btn")?.classList.toggle("active", isEnabled);

  if (typeof state.muted === "boolean" && state.muted !== muted) {
    setMuted(state.muted, { announce: false });
  }
  if (state.canDraw === false) {
    $("floating-note").textContent =
      "Overlay permission is not granted yet. Enabling asks for it once: allow “display over other apps”, then RAVAN's floating icon can appear.";
  }
}

// ── Wiring & Event Handlers ─────────────────────────────────────────────────

function wire() {
  // Input & Send
  $("send-btn").addEventListener("click", sendCurrentTurn);
  $("text-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendCurrentTurn();
    }
  });
  $("text-input").addEventListener("input", adjustInputHeight);

  // File Attachments
  $("attach-btn").addEventListener("click", () => {
    $("file-input").click();
  });
  $("file-input").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file) handleFileSelected(file);
  });
  $("att-remove").addEventListener("click", clearAttachment);

  // Mic & Stop
  $("mic-btn").addEventListener("click", toggleMic);
  $("stop-btn").addEventListener("click", interrupt);
  $("muted-chip").addEventListener("click", () => setMuted(false));

  // Face toggle & Quick floating toggle
  $("face-toggle-btn").addEventListener("click", toggleFaceManual);
  $("quick-floating-btn").addEventListener("click", () => {
    const currentState = $("quick-floating-btn").classList.contains("active");
    toggleFloating(!currentState);
  });

  // Settings
  $("settings-btn").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-sheet").addEventListener("click", (event) => {
    if (event.target === $("settings-sheet")) closeSettings();
  });

  $("cfg-voice").addEventListener("change", (event) => {
    voiceEnabled = event.target.checked;
    saveLocalSettings({ voiceEnabled });
    if (!voiceEnabled) {
      speechToken += 1;
      voice.stopSpeaking();
      avatar?.stopSpeech();
      updateStopButton();
      syncPhase();
    }
  });

  $("cfg-mic-muted").addEventListener("change", (event) => {
    setMuted(event.target.checked);
  });

  $("cfg-floating").addEventListener("change", (event) => {
    toggleFloating(event.target.checked);
  });

  $("btn-exit").addEventListener("click", async () => {
    if (!window.confirm("Exit RAVAN and hide the floating assistant?")) return;
    const exited = await actions.exit();
    if (!exited) toast("Exit handled by Android.");
  });

  $("offline-retry").addEventListener("click", () => {
    if (!api.configured) openSettings();
    else checkHealth({ attempts: 2 });
  });

  // WhatsApp Confirm
  $("confirm-ok").addEventListener("click", () => resolveWhatsApp(true));
  $("confirm-cancel").addEventListener("click", () => resolveWhatsApp(false));
  $("confirm-sheet").addEventListener("click", (event) => {
    if (event.target === $("confirm-sheet")) resolveWhatsApp(false);
  });

  // Visual Viewport & Keyboard Resizing
  if (window.visualViewport) {
    const handleViewport = () => {
      const vh = window.visualViewport.height;
      document.documentElement.style.setProperty("--vvh", `${vh}px`);
      const diff = window.innerHeight - vh;
      const kb = diff > 120;
      document.body.classList.toggle("keyboard-open", kb);
      updateFaceSpace(kb);
      if (kb) {
        setTimeout(() => scrollChatToBottom(false), 60);
      }
    };
    window.visualViewport.addEventListener("resize", handleViewport);
    window.visualViewport.addEventListener("scroll", handleViewport);
  }

  // Network & Visibility
  window.addEventListener("online", () => checkHealth({ attempts: 1, quiet: true }));
  window.addEventListener("offline", () => setOffline("Your device is offline."));

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      voice?.stopListening();
      avatar?.setVisible(false);
    } else {
      updateFaceSpace();
      if (offline) checkHealth({ attempts: 1, quiet: true });
      syncNativeState();
    }
  });

  const capApp = window.Capacitor?.Plugins?.App;
  if (capApp?.addListener) {
    capApp.addListener("appStateChange", ({ isActive }) => {
      if (!isActive) {
        voice?.stopListening();
        avatar?.setVisible(false);
      } else {
        updateFaceSpace();
        if (offline) checkHealth({ attempts: 1, quiet: true });
      }
    }).catch?.(() => {});
  }
}

async function boot() {
  api = new Api();
  actions = new Actions();

  $("cfg-backend").value = settings.backendUrl || window.RAVAN_CONFIG?.backendUrl || "https://ravanai-wn4a.onrender.com";
  $("cfg-token").value = settings.apiToken || "";
  $("cfg-voice").checked = voiceEnabled;
  $("cfg-mic-muted").checked = muted;
  $("about-version").textContent = `RAVAN ${window.RAVAN_CONFIG?.version || "mobile v2.0"}`;
  lastBackend = $("cfg-backend").value.trim();

  voice = new Voice({
    onListenState: (state) => {
      if (state === "listening") {
        setPhase("listening");
      } else {
        const input = $("text-input");
        if (input) input.placeholder = "Type a message or command…";
        syncPhase();
      }
    },
    onPartial: (text) => {
      const input = $("text-input");
      if (input) input.placeholder = text ? `“${text}”` : "Listening…";
    },
    onFinal: (text) => {
      const clean = String(text || "").trim();
      const input = $("text-input");
      if (input) input.placeholder = "Type a message or command…";
      if (!clean || muted) {
        syncPhase();
        return;
      }
      if (input) input.value = clean;
      sendCurrentTurn();
    },
    onError: (message) => {
      toast(message, 4200);
      syncPhase();
    }
  });

  avatar = new Avatar($("avatar"));
  try {
    await avatar.load("assets/head.json");
    avatar.start();
  } catch {
    addCard("err", null, "The animated face could not load. Voice and text still work.");
  }

  wire();
  setMuted(muted, { announce: false });
  setPhase("idle");
  updateStopButton();
  updateFaceSpace();

  if (actions.isNative) {
    syncNativeState();
    actions.onMicToggled((event) => {
      const next = Boolean(event?.muted);
      if (next !== muted) {
        setMuted(next, { announce: false });
        toast(next ? "Microphone muted from floating icon." : "Microphone active.");
      }
    });
  }

  await checkHealth();
  startHealthPolling();
}

window.addEventListener("error", (event) => {
  if (event?.error) console.error("[RAVAN]", event.error);
});

window.__ravan = {
  send: (text) => {
    $("text-input").value = text;
    sendCurrentTurn();
  },
  phase: () => phase,
  muted: () => muted,
  speak: (text) => speakReply(text, []),
  confirm: (ok) => resolveWhatsApp(Boolean(ok)),
  avatar: () => avatar,
  updateFaceSpace: () => updateFaceSpace()
};

boot().catch((err) => {
  console.error("[RAVAN] boot failed", err);
  document.body.dataset.state = "error";
  const label = document.getElementById("state-label");
  if (label) label.textContent = "STARTUP ERROR";
});
