/*
 * RAVAN mobile — application controller.
 *
 * One request at a time, one voice at a time. Every turn goes through the same
 * path: append the user's words, ask the backend, speak the answer, run any
 * device action the model requested, post the result back for a follow-up.
 * WhatsApp is the single action that pauses for an explicit confirmation.
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

function newId() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// ── UI primitives ───────────────────────────────────────────────────────────

function setPhase(name) {
  phase = name;
  document.body.dataset.state = name === "offline" ? "error" : name;
  $("state-label").textContent = LABELS[name] || name.toUpperCase();
  const avatarState =
    name === "idle" ? "STANDBY"
    : name === "offline" || name === "error" ? "OFFLINE"
    : name.toUpperCase();
  avatar?.setState(avatarState);
  $("mic-btn").setAttribute("aria-pressed", String(name === "listening"));
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
  $("stop-btn").hidden = !visible;
  $("send-btn").disabled = busy;
}

function toast(message, ms = 3200) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, ms);
}

function addCard(kind, who, text, { speakable = false } = {}) {
  const transcript = $("transcript");
  $("empty-hint").hidden = true;
  const card = document.createElement("div");
  card.className = `card ${kind}`;
  if (who) {
    const label = document.createElement("span");
    label.className = "who";
    label.textContent = who;
    card.appendChild(label);
  }
  const body = document.createElement("div");
  body.textContent = text;
  card.appendChild(body);
  transcript.appendChild(card);

  while (transcript.querySelectorAll(".card").length > 80) {
    transcript.querySelector(".card")?.remove();
  }
  transcript.scrollTop = transcript.scrollHeight;
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

// ── backend health ──────────────────────────────────────────────────────────

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
          "Backend is in mock mode: replies are canned and no Gemini call is made. Set GEMINI_API_KEY on the server for live answers."
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
  // A minute is enough to notice a sleeping backend without keeping a free
  // instance awake around the clock; requests themselves refresh the state.
  setInterval(() => {
    if (document.hidden) return;
    checkHealth({ attempts: 1, quiet: true });
  }, 60000);
}

// ── conversation ────────────────────────────────────────────────────────────

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
      localTime: new Date().toString()
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

function sendText(raw) {
  const text = String(raw || "").trim();
  if (!text) return;
  if (text.length > 2000) {
    toast("That message is too long (2000 characters max).");
    return;
  }
  if (busy) {
    toast("Still answering the previous request.");
    return;
  }
  if (voice?.listening) voice.stopListening();
  addCard("user", "YOU", text);
  const input = $("text-input");
  input.value = "";
  input.placeholder = "Type a command…";
  runChain({ text });
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

  // The sheet is shown at the end of the reply chain, so `busy` is normally
  // already false; wait briefly rather than dropping the results if a tap
  // lands in the same frame the chain completes.
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

// ── microphone and mute ─────────────────────────────────────────────────────

function setMuted(next, { announce = true } = {}) {
  muted = Boolean(next);
  saveLocalSettings({ muted });
  $("muted-chip").hidden = !muted;
  $("mic-btn").classList.toggle("is-muted", muted);
  $("cfg-mic-muted").checked = muted;
  if (muted) voice?.stopListening();
  actions?.setMicMuted(muted);
  if (announce) toast(muted ? "Microphone muted. RAVAN will not listen." : "Microphone on.");
  syncPhase();
}

async function toggleMic() {
  if (!voice) return;
  if (voice.listening) {
    voice.stopListening();
    return;
  }
  if (busy) interrupt();
  if (muted) setMuted(false, { announce: false });

  if (!voice.canListen) {
    toast("Voice input is not available here. Type your command instead.");
    return;
  }

  speechToken += 1;
  voice.stopSpeaking();
  avatar?.stopSpeech();

  const allowed = await voice.ensureMicPermission();
  if (!allowed) {
    toast("Microphone permission is needed. Allow it to use voice, or type instead.");
    return;
  }
  if (muted) return;

  $("text-input").placeholder = "Listening…";
  voice.listenOnce({ lang: navigator.language || "en-US" });
}

// ── settings ────────────────────────────────────────────────────────────────

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
  const el = $("cfg-floating");
  el.disabled = true;
  try {
    const out = await actions.setFloating(wanted);
    if (!out?.ok) {
      el.checked = !wanted;
      if (out?.permissionRequired) {
        toast("Allow “display over other apps” for RAVAN, then try again.", 4800);
      } else {
        toast(out?.detail || "The floating icon could not be changed.", 4200);
      }
      return;
    }
    el.checked = Boolean(out.enabled);
    saveLocalSettings({ floating: el.checked });
    toast(el.checked ? "Floating RAVAN enabled." : "Floating RAVAN hidden.");
  } finally {
    el.disabled = false;
  }
}

async function syncNativeState() {
  if (!actions.isNative) return;
  $("native-section").hidden = false;
  const state = await actions.floatingState();
  if (!state) return;
  $("cfg-floating").checked = Boolean(state.enabled);
  if (typeof state.muted === "boolean" && state.muted !== muted) {
    setMuted(state.muted, { announce: false });
  }
  if (state.canDraw === false) {
    $("floating-note").textContent =
      "Overlay permission is not granted yet. Turning this on asks for it once: allow “display over other apps”, then RAVAN's icon can appear over other apps.";
  }
}

// ── wiring ──────────────────────────────────────────────────────────────────

function wire() {
  $("send-btn").addEventListener("click", () => sendText($("text-input").value));
  $("text-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendText($("text-input").value);
    }
  });
  $("mic-btn").addEventListener("click", () => toggleMic());
  $("stop-btn").addEventListener("click", () => interrupt());
  $("muted-chip").addEventListener("click", () => setMuted(false));

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
    if (!window.confirm("Exit RAVAN and hide the floating icon?")) return;
    const exited = await actions.exit();
    if (!exited) toast("Exit is handled by the Android build.");
  });

  $("offline-retry").addEventListener("click", () => {
    if (!api.configured) openSettings();
    else checkHealth({ attempts: 2 });
  });

  $("confirm-ok").addEventListener("click", () => resolveWhatsApp(true));
  $("confirm-cancel").addEventListener("click", () => resolveWhatsApp(false));
  $("confirm-sheet").addEventListener("click", (event) => {
    if (event.target === $("confirm-sheet")) resolveWhatsApp(false);
  });

  window.addEventListener("online", () => checkHealth({ attempts: 1, quiet: true }));
  window.addEventListener("offline", () => setOffline("Your device is offline."));

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      voice?.stopListening();
    } else {
      if (offline) checkHealth({ attempts: 1, quiet: true });
      syncNativeState();
    }
  });

  const capApp = window.Capacitor?.Plugins?.App;
  if (capApp?.addListener) {
    capApp.addListener("appStateChange", ({ isActive }) => {
      if (!isActive) voice?.stopListening();
      else if (offline) checkHealth({ attempts: 1, quiet: true });
    }).catch?.(() => {});
  }
}

async function boot() {
  api = new Api();
  actions = new Actions();

  $("cfg-backend").value = settings.backendUrl || window.RAVAN_CONFIG?.backendUrl || "";
  $("cfg-token").value = settings.apiToken || "";
  $("cfg-voice").checked = voiceEnabled;
  $("cfg-mic-muted").checked = muted;
  $("about-version").textContent = `RAVAN ${window.RAVAN_CONFIG?.version || "mobile"}`;
  lastBackend = $("cfg-backend").value.trim();

  voice = new Voice({
    onListenState: (state) => {
      if (state === "listening") {
        setPhase("listening");
      } else {
        $("text-input").placeholder = "Type a command…";
        syncPhase();
      }
    },
    onPartial: (text) => {
      $("text-input").placeholder = text ? `“${text}”` : "Listening…";
    },
    onFinal: (text) => {
      const clean = String(text || "").trim();
      $("text-input").placeholder = "Type a command…";
      if (!clean || muted) {
        syncPhase();
        return;
      }
      sendText(clean);
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

  if (actions.isNative) {
    syncNativeState();
    actions.onMicToggled((event) => {
      const next = Boolean(event?.muted);
      if (next !== muted) {
        setMuted(next, { announce: false });
        toast(next ? "Microphone muted from the floating icon." : "Microphone on.");
      }
    });
  }

  await checkHealth();
  startHealthPolling();
  $("text-input").focus();
}

window.addEventListener("error", (event) => {
  // A single failing frame must not take the whole interface down silently.
  if (event?.error) console.error("[RAVAN]", event.error);
});

window.__ravan = {
  send: (text) => sendText(text),
  phase: () => phase,
  muted: () => muted,
  speak: (text) => speakReply(text, []),
  confirm: (ok) => resolveWhatsApp(Boolean(ok)),
  avatar: () => avatar
};

boot().catch((err) => {
  console.error("[RAVAN] boot failed", err);
  document.body.dataset.state = "error";
  const label = document.getElementById("state-label");
  if (label) label.textContent = "STARTUP ERROR";
});
