import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/public-sans/latin-300.css";
import "@fontsource/public-sans/latin-500.css";
import "@fontsource/public-sans/latin-600.css";
import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
