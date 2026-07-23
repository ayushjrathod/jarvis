import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import AskScreen from "./AskScreen.jsx";
import "./styles.css";

// /ask is the ask-about-my-screen popup; everything else is the dashboard.
const Page = location.pathname === "/ask" ? AskScreen : App;
createRoot(document.getElementById("root")).render(<Page />);
