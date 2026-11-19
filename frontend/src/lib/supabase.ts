// Supabase is used for LOGIN ONLY (D10). All data goes through our backend with the token.
// Created lazily, so mock mode works without any Supabase settings.

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let client: SupabaseClient | null = null;

export function supabase(): SupabaseClient {
  if (client) return client;
  const url = import.meta.env.VITE_SUPABASE_URL;
  const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) {
    throw new Error("VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY must be set in .env (or use mocks)");
  }
  // The login lives in sessionStorage: it survives a refresh (F5) but not closing the tab or the
  // browser, so the next person on a shared lab computer gets the sign-in page, not your account.
  client = createClient(url, key, {
    auth: { persistSession: true, autoRefreshToken: true, storage: window.sessionStorage },
  });
  return client;
}
