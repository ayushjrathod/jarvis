import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// dev server proxies API calls to the dispatcher; the production build is
// served BY the dispatcher (ui/dist mounted at /), so paths stay same-origin.
const dispatcher = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    // NOT /ask: Vite's SPA fallback must serve the dev index.html there
    proxy: Object.fromEntries(
      ["/task", "/tasks", "/events", "/vault", "/health", "/stt", "/screenshots"]
        .map((p) => [p, dispatcher])
    ),
  },
});
