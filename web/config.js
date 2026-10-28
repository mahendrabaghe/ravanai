/*
 * Build-time configuration.
 *
 * This file is plain script so it can be rewritten by the GitHub Pages
 * workflow without a bundler: the workflow replaces the backendUrl value
 * below with the deployed Render URL. It contains no secrets — the optional
 * access token is entered by the user at runtime and kept in localStorage.
 *
 *   backendUrl: ""  → same origin (when FastAPI serves web/ at /app/)
 *   backendUrl: "https://ravan-ai.onrender.com" → GitHub Pages / APK
 */
window.RAVAN_CONFIG = {
  backendUrl: "",
  apiToken: "",
  version: "1.0.0"
};
