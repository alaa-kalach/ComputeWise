import { setMockHandler } from "../lib/api/client";
import { mockFetch } from "./handlers";

export { PERSONAS, resetWorld } from "./world";
export { approvePendingFor, mockPersona, setMockPersona, simulateSpend } from "./handlers";

/** Plug the mock server into the API client (called once at start-up in mock mode). */
export function installMocks(): void {
  setMockHandler(mockFetch);
}
