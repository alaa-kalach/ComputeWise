// Who is signed in, and which view they are looking at.
//
// Real mode: Supabase email + password login, then GET /me. A login with no ComputeWise
// account gets 403 from /me (sign-up is off, D10) and is signed out with that message.
// Mock mode: pick a seed person; no password, no backend.
//
// `role` comes from /me; a person with several roles (D21) switches view with setView, and
// the choice is remembered in this browser.

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, USE_MOCKS, setTokenGetter } from "./api/client";
import { getMe } from "./api/endpoints";
import type { Me, Role } from "./api/types";
import { supabase } from "./supabase";

interface AuthState {
  me: Me | null;
  loading: boolean;
  error: string | null;
  view: Role | null;
  setView: (role: Role) => void;
  signIn: (email: string, password: string) => Promise<void>;
  signInAs: (personaKey: string) => Promise<void>; // mock mode only
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);
const VIEW_KEY = "computewise.view";

function storedView(): Role | null {
  try {
    return (localStorage.getItem(VIEW_KEY) as Role | null) ?? null;
  } catch {
    return null;
  }
}

function rememberView(role: Role | null): void {
  try {
    if (role) localStorage.setItem(VIEW_KEY, role);
    else localStorage.removeItem(VIEW_KEY);
  } catch {
    /* storage blocked: the view just isn't remembered */
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setViewState] = useState<Role | null>(null);

  const loadMe = useCallback(async () => {
    try {
      const m = await getMe();
      setMe(m);
      const saved = storedView();
      setViewState(saved && m.roles.includes(saved) ? saved : m.role);
      setError(null);
    } catch (e) {
      setMe(null);
      setError(e instanceof ApiError && e.status === 403 ? e.message : "Couldn't load your account. Try again.");
      if (!USE_MOCKS) await supabase().auth.signOut();
    }
  }, []);

  useEffect(() => {
    if (USE_MOCKS) {
      setLoading(false);
      return;
    }
    const sb = supabase();
    setTokenGetter(async () => (await sb.auth.getSession()).data.session?.access_token ?? null);
    let active = true;
    sb.auth.getSession().then(async ({ data }) => {
      if (data.session) await loadMe();
      if (active) setLoading(false);
    });
    const { data: sub } = sb.auth.onAuthStateChange((event) => {
      if (event === "SIGNED_OUT") setMe(null);
    });
    return () => {
      active = false;
      sub.subscription.unsubscribe();
    };
  }, [loadMe]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      setError(null);
      const { error: err } = await supabase().auth.signInWithPassword({ email, password });
      if (err) {
        setError(err.message === "Invalid login credentials" ? "Wrong email or password." : err.message);
        return;
      }
      await loadMe();
    },
    [loadMe],
  );

  const signInAs = useCallback(
    async (personaKey: string) => {
      const { setMockPersona } = await import("../mocks");
      setMockPersona(personaKey);
      rememberView(null);
      await loadMe();
    },
    [loadMe],
  );

  const signOut = useCallback(async () => {
    if (USE_MOCKS) {
      const { setMockPersona } = await import("../mocks");
      setMockPersona(null);
    } else {
      await supabase().auth.signOut();
    }
    rememberView(null);
    setMe(null);
    setViewState(null);
  }, []);

  const setView = useCallback((role: Role) => {
    rememberView(role);
    setViewState(role);
  }, []);

  const value = useMemo(
    () => ({ me, loading, error, view, setView, signIn, signInAs, signOut }),
    [me, loading, error, view, setView, signIn, signInAs, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
