import type { ReactNode } from "react";

import { BrandMark, Button } from "../components/ui";
import { USE_MOCKS } from "../lib/api/client";
import type { Role } from "../lib/api/types";
import { useAuth } from "../lib/auth";
import { MockTools } from "../mocks/MockTools";

const VIEW_TEXT: Record<Role, string> = { user: "Student", professor: "Professor", department_admin: "Department" };

/** The frame around every dashboard: who you are, a view switch if you hold several roles (D21). */
export function Layout({ children }: { children: ReactNode }) {
  const { me, view, setView, signOut } = useAuth();
  if (!me) return null;
  return (
    <>
      <header className="topbar">
        <div className="brand">
          <BrandMark />
          <div>
            ComputeWise{view && <span> / {VIEW_TEXT[view]}</span>}
          </div>
        </div>
        {me.roles.length > 1 && (
          <div className="viewswitch" role="group" aria-label="Switch view">
            {me.roles.map((r) => (
              <button key={r} type="button" aria-pressed={view === r} onClick={() => setView(r)}>
                {VIEW_TEXT[r]}
              </button>
            ))}
          </div>
        )}
        <span className="who">{me.name}</span>
        <Button size="sm" onClick={signOut}>
          Sign out
        </Button>
      </header>
      <main className="page">{children}</main>
      {USE_MOCKS && <MockTools />}
    </>
  );
}
