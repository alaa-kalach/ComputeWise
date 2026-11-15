import { useState, type FormEvent } from "react";

import { BrandMark, Button, Notice } from "../components/ui";
import { USE_MOCKS } from "../lib/api/client";
import { useAuth } from "../lib/auth";
import { PERSONAS } from "../mocks/world";

const ROLE_TEXT = { user: "Student", professor: "Professor", department_admin: "Department" } as const;

export function SignIn() {
  const { signIn, signInAs, error } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    await signIn(email.trim(), password);
    setBusy(false);
  };

  return (
    <main className="signin">
      <div className="signin-box">
        <BrandMark />
        <h1>ComputeWise</h1>
        {USE_MOCKS ? (
          <>
            <p className="lede">Mock mode: pick someone from the seed data. Nothing here touches a real provider.</p>
            {error && <Notice kind="error">{error}</Notice>}
            <div className="personas">
              {PERSONAS.map((p) => (
                <button key={p.key} type="button" className="persona" onClick={() => signInAs(p.key)}>
                  <span className="name">{p.name}</span>
                  <span className="role">{p.roles.map((r) => ROLE_TEXT[r]).join(" + ")}</span>
                  <span className="blurb">{p.blurb}</span>
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <p className="lede">Sign in with the account your professor or department created for you.</p>
            <form onSubmit={submit}>
              {error && <Notice kind="error">{error}</Notice>}
              <div className="field">
                <label htmlFor="email">Email</label>
                <input id="email" className="input" type="email" autoComplete="email" required
                  value={email} onChange={(e) => setEmail(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="password">Password</label>
                <input id="password" className="input" type="password" autoComplete="current-password" required
                  value={password} onChange={(e) => setPassword(e.target.value)} />
              </div>
              <Button type="submit" variant="primary" busy={busy}>
                Sign in
              </Button>
              <p className="muted" style={{ fontSize: 14 }}>
                No account yet? Ask your professor to add you. You'll get an email to set your password.
              </p>
            </form>
          </>
        )}
      </div>
    </main>
  );
}
