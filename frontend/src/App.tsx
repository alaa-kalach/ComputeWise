import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./app/Layout";
import { SignIn } from "./app/SignIn";
import { Loading } from "./components/ui";
import type { Role } from "./lib/api/types";
import { AuthProvider, useAuth } from "./lib/auth";
import { DepartmentDashboard } from "./pages/department/DepartmentDashboard";
import { ProfessorDashboard } from "./pages/professor/ProfessorDashboard";
import { UserDashboard } from "./pages/user/UserDashboard";

/** Each role's home page. The view switch (D21) moves between them. */
const HOME: Record<Role, string> = { user: "/me", professor: "/professor", department_admin: "/department" };

function Guard({ role, children }: { role: Role; children: JSX.Element }) {
  const { me, view } = useAuth();
  if (!me || !view) return <Navigate to="/" replace />;
  if (!me.roles.includes(role)) return <Navigate to={HOME[view]} replace />; // never show a view you don't hold
  if (view !== role) return <Navigate to={HOME[view]} replace />;
  return <Layout>{children}</Layout>;
}

function Routed() {
  const { me, loading, view } = useAuth();
  if (loading) {
    return (
      <main className="signin">
        <Loading what="your account" />
      </main>
    );
  }
  if (!me || !view) return <SignIn />;
  return (
    <Routes>
      <Route path="/me" element={<Guard role="user"><UserDashboard /></Guard>} />
      <Route path="/professor" element={<Guard role="professor"><ProfessorDashboard /></Guard>} />
      <Route path="/department" element={<Guard role="department_admin"><DepartmentDashboard /></Guard>} />
      <Route path="*" element={<Navigate to={HOME[view]} replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routed />
      </BrowserRouter>
    </AuthProvider>
  );
}
