/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "false" = talk to the real backend; anything else (or unset) = mocks, no backend needed. */
  readonly VITE_USE_MOCKS?: string;
  readonly VITE_API_URL?: string;
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_PUBLISHABLE_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
