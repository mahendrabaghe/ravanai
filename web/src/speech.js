/*
 * Voice in, voice out.
 *
 * Two implementations, one interface:
 *   • Android (Capacitor): @capacitor-community/speech-recognition and
 *     @capacitor-community/text-to-speech. These use the platform's own
 *     recogniser and TTS engine — the same OS services the desktop app's
 *     engine selection is reaching for — with no second assistant loop.
 *   • Browser: Web Speech API where it exists (Chrome, Edge, Safari). Firefox
 *     has no recognition; typing remains available everywhere.
 *
 * Listening is single-shot by design. Nothing here opens a microphone on its
 * own, and `stop()` is safe to call at any time.
 */

const SpeechRecognitionCtor =
  window.SpeechRecognition || window.webkitSpeechRecognition || null;

export function estimateSpeechSeconds(text) {
  const words = String(text || "").trim().split(/\s+/).filter(Boolean).length;
  return Math.min(30, Math.max(1, 0.35 + words * 0.34));
}

function plugin(name) {
  const cap = window.Capacitor;
  if (!cap || typeof cap.isNativePlatform !== "function" || !cap.isNativePlatform()) {
    return null;
  }
  return cap.Plugins?.[name] || null;
}

export class Voice {
  constructor(handlers = {}) {
    this.on = handlers;
    this.recognition = null;
    this.micPlugin = plugin("SpeechRecognition");
    this.ttsPlugin = plugin("TextToSpeech");
    this.speaking = false;
    this.listening = false;
    this._stopRecognition = null;
    this._utterance = null;
    this._speakTimer = 0;
    this._voice = null;
  }

  get native() {
    return Boolean(this.micPlugin || this.ttsPlugin);
  }

  get canListen() {
    return Boolean(this.micPlugin || SpeechRecognitionCtor);
  }

  get canSpeak() {
    return Boolean(this.ttsPlugin || window.speechSynthesis);
  }

  /** Resolve the best available voice once; safe to await before speaking. */
  async primeVoices() {
    if (!window.speechSynthesis) return;
    const pick = () => {
      const voices = window.speechSynthesis.getVoices() || [];
      const lang = (navigator.language || "en-US").toLowerCase();
      const sameLang = voices.filter((v) => v.lang?.toLowerCase().startsWith(lang.slice(0, 2)));
      const preferred = sameLang.find((v) => /google|samantha|aria|zira|natural/i.test(v.name));
      this._voice = preferred || sameLang[0] || voices[0] || null;
    };
    pick();
    if (!window.speechSynthesis.getVoices()?.length) {
      await new Promise((resolve) => {
        const done = () => {
          window.speechSynthesis.removeEventListener("voiceschanged", done);
          pick();
          resolve();
        };
        window.speechSynthesis.addEventListener("voiceschanged", done);
        setTimeout(done, 800);
      });
    }
  }

  async ensureMicPermission() {
    if (this.micPlugin) {
      try {
        const state = await this.micPlugin.checkPermissions();
        if (state.speechRecognition === "granted") return true;
        const asked = await this.micPlugin.requestPermissions();
        return asked.speechRecognition === "granted";
      } catch {
        return false;
      }
    }
    if (!navigator.mediaDevices?.getUserMedia) return false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      return true;
    } catch {
      return false;
    }
  }

  /**
   * One utterance. Resolves with the final transcript, or rejects with a
   * user-safe Error. `onPartial` receives interim text as it arrives.
   */
  listenOnce({ lang = navigator.language || "en-US", timeoutMs = 15000 } = {}) {
    if (this.listening) this.stopListening();
    this.listening = true;
    this.on.onListenState?.("listening");

    const finish = (fn) => {
      if (!this.listening) return;
      this.listening = false;
      this._stopRecognition = null;
      this.on.onListenState?.("stopped");
      fn();
    };

    if (this.micPlugin) {
      return this._listenNative(lang, timeoutMs);
    }
    return this._listenWeb(finish, lang, timeoutMs);
  }

  async _listenNative(lang, timeoutMs) {
    const pluginRef = this.micPlugin;
    let settled = false;
    let lastText = "";
    let silenceTimer = 0;
    let hardTimer = 0;
    let listeners = [];

    const cleanup = async () => {
      clearTimeout(silenceTimer);
      clearTimeout(hardTimer);
      for (const handle of listeners) {
        try {
          await handle.remove();
        } catch {
          /* already gone */
        }
      }
      listeners = [];
      try {
        await pluginRef.stop();
      } catch {
        /* not listening */
      }
    };

    const settle = (err, text) => {
      if (settled) return;
      settled = true;
      if (this.listening) {
        this.listening = false;
        this._stopRecognition = null;
        this.on.onListenState?.("stopped");
      }
      cleanup();
      if (err) this.on.onError?.(err);
      else if (text) this.on.onFinal?.(text);
    };

    try {
      const partial = await pluginRef.addListener("partialResults", (event) => {
        const text = String(event?.matches?.[0] || "").trim();
        if (!text) return;
        lastText = text;
        this.on.onPartial?.(text);
        clearTimeout(silenceTimer);
        silenceTimer = setTimeout(() => settle(null, lastText), 1100);
      });
      const state = await pluginRef.addListener("listeningState", (event) => {
        if (String(event?.status || "").includes("stopped") && lastText) {
          settle(null, lastText);
        }
      });
      listeners = [partial, state];

      // Cancellation discards whatever was captured. Text already sent to the
      // input by an earlier turn is never re-submitted by a stop.
      this._stopRecognition = () => settle(null, "");

      hardTimer = setTimeout(() => {
        if (lastText) settle(null, lastText);
        else settle("I did not catch that. Try again, a little closer to the phone.");
      }, timeoutMs);

      await pluginRef.start({
        language: lang,
        maxResults: 1,
        partialResults: true,
        popup: false
      });
    } catch (err) {
      settle(
        /permission|denied/i.test(String(err?.message || err))
          ? "Microphone permission is needed for voice. You can allow it in Android settings, or type instead."
          : "Voice recognition is not available right now. Typing still works."
      );
    }
  }

  _listenWeb(finish, lang, timeoutMs) {
    if (!SpeechRecognitionCtor) {
      finish(() => this.on.onError?.("This browser has no speech recognition. Type instead, or use the Android app."));
      return Promise.resolve();
    }

    return new Promise((resolve) => {
      const rec = new SpeechRecognitionCtor();
      this.recognition = rec;
      rec.lang = lang;
      rec.interimResults = true;
      rec.continuous = false;
      rec.maxAlternatives = 1;

      let finalText = "";
      let lastText = "";
      let settled = false;
      let cancelled = false;

      const stop = () => {
        if (!settled) {
          settled = true;
          finish(() => {
            if (!cancelled && (finalText || lastText)) this.on.onFinal?.(finalText || lastText);
            else if (!cancelled) this.on.onError?.("I did not catch that. Try again.");
          });
        }
        resolve();
      };

      // A deliberate stop discards the utterance: a partial transcript must
      // never be sent just because the user pressed stop.
      this._stopRecognition = () => {
        cancelled = true;
        try {
          rec.stop();
        } catch {
          stop();
        }
      };

      rec.onresult = (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const result = event.results[i];
          const text = result[0]?.transcript || "";
          if (result.isFinal) finalText += text;
          else interim += text;
        }
        lastText = (finalText + interim).trim();
        if (lastText) this.on.onPartial?.(lastText);
      };
      rec.onerror = (event) => {
        const code = event.error || "";
        if (settled) return;
        if (code === "aborted" || code === "no-speech") {
          settled = true;
          finish(() => this.on.onError?.("I did not hear anything. Tap the mic and try again."));
          resolve();
          return;
        }
        settled = true;
        finish(() => this.on.onError?.(
          /not-allowed|service-not-allowed/.test(code)
            ? "Microphone access was blocked. Allow it for this site to use voice, or type instead."
            : "Speech recognition failed. Typing still works."
        ));
        resolve();
      };
      rec.onend = stop;

      try {
        rec.start();
      } catch {
        settled = true;
        finish(() => this.on.onError?.("The microphone is already in use. Try again in a moment."));
        resolve();
        return;
      }

      setTimeout(() => {
        if (!settled) {
          try {
            rec.stop();
          } catch {
            stop();
          }
        }
      }, timeoutMs);
    });
  }

  stopListening() {
    const stop = this._stopRecognition;
    this._stopRecognition = null;
    if (this.listening) {
      this.listening = false;
      this.on.onListenState?.("stopped");
    }
    if (stop) {
      stop();
      return;
    }
    try {
      this.recognition?.stop();
    } catch {
      /* already stopped */
    }
    try {
      this.micPlugin?.stop();
    } catch {
      /* already stopped */
    }
  }

  /**
   * Speak `text`. Resolves when playback ends (or immediately on failure, so
   * a TTS engine with no voice data never blocks the conversation).
   */
  async speak(text, { onStart, onEnd } = {}) {
    const clean = String(text || "").trim();
    if (!clean || !this.canSpeak) {
      onEnd?.();
      return;
    }

    this.stopSpeaking();
    this.speaking = true;
    onStart?.();

    const done = () => {
      if (!this.speaking) return;
      this.speaking = false;
      clearTimeout(this._speakTimer);
      onEnd?.();
    };

    if (this.ttsPlugin) {
      // Never let a missing engine or voice hold the UI in SPEAKING forever.
      this._speakTimer = setTimeout(done, estimateSpeechSeconds(clean) * 1000 + 12000);
      try {
        await this.ttsPlugin.speak({
          text: clean,
          lang: navigator.language || "en-US",
          rate: 1.0,
          pitch: 1.0,
          volume: 1.0,
          category: "ambient"
        });
      } catch {
        /* fall through to the browser engine if there is one */
        if (window.speechSynthesis) {
          await this._speakWeb(clean, done);
          return;
        }
      }
      done();
      return;
    }

    await this._speakWeb(clean, done);
  }

  _speakWeb(text, done) {
    if (!window.speechSynthesis) {
      done();
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const utter = new SpeechSynthesisUtterance(text);
      this._utterance = utter;
      utter.voice = this._voice;
      utter.lang = this._voice?.lang || navigator.language || "en-US";
      utter.rate = 1.0;
      utter.pitch = 1.0;
      utter.volume = 1.0;
      utter.onend = () => {
        this._utterance = null;
        done();
        resolve();
      };
      utter.onerror = () => {
        this._utterance = null;
        done();
        resolve();
      };
      this._speakTimer = setTimeout(() => {
        try {
          window.speechSynthesis.cancel();
        } catch {
          /* nothing to cancel */
        }
        done();
        resolve();
      }, estimateSpeechSeconds(text) * 1000 + 12000);
      try {
        window.speechSynthesis.speak(utter);
      } catch {
        done();
        resolve();
      }
    });
  }

  stopSpeaking() {
    if (!this.speaking && !this._utterance) return;
    this.speaking = false;
    clearTimeout(this._speakTimer);
    try {
      window.speechSynthesis?.cancel();
    } catch {
      /* not supported */
    }
    try {
      this.ttsPlugin?.stop();
    } catch {
      /* nothing speaking */
    }
    this._utterance = null;
  }
}
