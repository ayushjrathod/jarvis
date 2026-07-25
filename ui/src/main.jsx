import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import AskScreen from "./AskScreen.jsx";
import Docs from "./Docs.jsx";
import "./styles.css";

// extra pages served by the dispatcher from the same SPA shell (each one also
// needs a route in dispatcher/main.py); everything else is the dashboard.
const PAGES = { "/ask": AskScreen, "/system-docs": Docs };
const Page = PAGES[location.pathname] ?? App;
createRoot(document.getElementById("root")).render(<Page />);

// PWA: register the service worker so the dashboard installs to a phone home
// screen (over Tailscale Serve, which is HTTPS) and opens offline. Harmless on
// plain-http loopback — localhost is a secure context too.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
