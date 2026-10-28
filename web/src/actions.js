/*
 * Executes the assistant's tool calls on the device.
 *
 * On Android every call goes to the native PhoneActions plugin (Kotlin), which
 * uses intents — this file never runs shell commands and never touches a
 * Windows-style path. In a plain browser the same calls degrade to opening a
 * web page, and anything that has no honest web equivalent reports that it
 * needs the Android build instead of pretending to succeed.
 *
 * WhatsApp is the one action that must never happen without confirmation:
 * app.js shows the sheet, and only the confirmed call reaches `execute`.
 */

const APP_WEB = {
  chrome: "https://www.google.com",
  youtube: "https://www.youtube.com",
  whatsapp: "https://web.whatsapp.com",
  maps: "https://www.google.com/maps",
  gmail: "https://mail.google.com",
  spotify: "https://open.spotify.com",
  instagram: "https://www.instagram.com",
  telegram: "https://web.telegram.org"
};

const APP_LABEL = {
  chrome: "Chrome",
  youtube: "YouTube",
  whatsapp: "WhatsApp",
  settings: "Settings",
  downloads: "Downloads",
  camera: "Camera",
  maps: "Maps",
  gmail: "Gmail",
  spotify: "Spotify",
  instagram: "Instagram",
  telegram: "Telegram",
  calculator: "Calculator",
  clock: "Clock"
};

function plugin() {
  const cap = window.Capacitor;
  if (!cap || typeof cap.isNativePlatform !== "function" || !cap.isNativePlatform()) return null;
  return cap.Plugins?.PhoneActions || null;
}

function openWeb(url) {
  // A window.open() call with `noopener` returns null even when the tab opens,
  // so its return value cannot be used as a success signal. An anchor click is
  // the one form every browser treats as a real navigation.
  try {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    document.body.appendChild(a);
    a.click();
    a.remove();
    return true;
  } catch {
    return false;
  }
}

function isPhoneNumber(value) {
  const digits = String(value || "").replace(/[^\d]/g, "");
  return digits.length >= 7 && digits.length <= 15;
}

export class Actions {
  constructor() {
    this.plugin = plugin();
  }

  get isNative() {
    return Boolean(this.plugin);
  }

  label(name) {
    return APP_LABEL[name] || name;
  }

  async execute(call, { confirmed = false } = {}) {
    const name = call?.name;
    const args = call?.args || {};
    try {
      if (name === "open_app") return await this.openApp(args.app);
      if (name === "open_url") return await this.openUrl(args.url);
      if (name === "play_on_youtube") return await this.youtube(args.query);
      if (name === "compose_whatsapp_message") {
        if (!confirmed) {
          return { ok: false, detail: "WhatsApp needs your confirmation before it opens." };
        }
        return await this.whatsapp(args.contact, args.message);
      }
      return { ok: false, detail: "That action is not available in this build." };
    } catch (err) {
      return {
        ok: false,
        detail: `The action could not be completed (${String(err?.message || err).slice(0, 120)}).`
      };
    }
  }

  async openApp(app) {
    const key = String(app || "").toLowerCase();
    const label = APP_LABEL[key] || key;
    if (this.plugin) {
      return await this.plugin.openApp({ app: key });
    }
    const url = APP_WEB[key];
    if (!url) {
      return {
        ok: false,
        detail: `${label} can only be opened by the Android app, not from a web page.`
      };
    }
    return openWeb(url)
      ? { ok: true, detail: `Opened ${label} on the web.` }
      : { ok: false, detail: `The browser blocked the tab. Open ${url} yourself.` };
  }

  async openUrl(url) {
    if (!/^https:\/\//i.test(String(url || ""))) {
      return { ok: false, detail: "Only https links can be opened." };
    }
    if (this.plugin) return await this.plugin.openUrl({ url });
    return openWeb(url)
      ? { ok: true, detail: "Opened the link in a browser tab." }
      : { ok: false, detail: `The browser blocked the tab. Open ${url} yourself.` };
  }

  async youtube(query) {
    const q = String(query || "").trim();
    if (!q) return { ok: false, detail: "No video title was given." };
    if (this.plugin) return await this.plugin.openYouTubeSearch({ query: q });
    const url = `https://www.youtube.com/results?search_query=${encodeURIComponent(q)}`;
    return openWeb(url)
      ? { ok: true, detail: `YouTube search opened for “${q}”. Playback starts when you pick a result.` }
      : { ok: false, detail: `The browser blocked the tab. Search YouTube for “${q}” yourself.` };
  }

  async whatsapp(contact, message) {
    const name = String(contact || "").trim();
    const text = String(message || "").trim();
    if (!text) return { ok: false, detail: "There was no message text to compose." };

    if (this.plugin) {
      return await this.plugin.composeWhatsApp({
        contact: isPhoneNumber(name) ? name : "",
        contactHint: isPhoneNumber(name) ? "" : name,
        message: text
      });
    }

    const base = isPhoneNumber(name)
      ? `https://wa.me/${name.replace(/[^\d]/g, "")}?text=${encodeURIComponent(text)}`
      : `https://wa.me/?text=${encodeURIComponent(text)}`;
    return openWeb(base)
      ? { ok: true, detail: "WhatsApp opened with the message ready. Nothing has been sent." }
      : { ok: false, detail: `The browser blocked the tab. Open ${base} yourself.` };
  }

  async setFloating(enabled) {
    if (!this.plugin) return { ok: false, detail: "The floating icon needs the Android app." };
    return await this.plugin.setFloatingIcon({ enabled: Boolean(enabled) });
  }

  async floatingState() {
    if (!this.plugin) return null;
    try {
      return await this.plugin.getFloatingState();
    } catch {
      return null;
    }
  }

  async setMicMuted(muted) {
    if (!this.plugin) return;
    try {
      await this.plugin.setMicMuted({ muted: Boolean(muted) });
    } catch {
      /* the overlay is optional; the in-app state is authoritative */
    }
  }

  async onMicToggled(handler) {
    if (!this.plugin?.addListener) return () => {};
    try {
      const handle = await this.plugin.addListener("micToggled", handler);
      return () => handle?.remove?.();
    } catch {
      return () => {};
    }
  }

  async exit() {
    if (!this.plugin) return false;
    try {
      await this.plugin.exitApp();
      return true;
    } catch {
      return false;
    }
  }
}
