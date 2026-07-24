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
