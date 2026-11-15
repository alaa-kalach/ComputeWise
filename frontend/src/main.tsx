import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { USE_MOCKS } from "./lib/api/client";
import "./styles.css";

async function start() {
  if (USE_MOCKS) {
    const { installMocks } = await import("./mocks");
    installMocks();
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
