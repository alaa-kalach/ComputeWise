import { useState } from "react";

import { Button, Modal, Notice, Tag } from "../../../components/ui";
import {
  addMember, getGroup, listMemberBudgets, removeMember, setMemberLimit, setMemberStatus,
} from "../../../lib/api/endpoints";
import type { Group } from "../../../lib/api/types";
import { usd } from "../../../lib/money";
import { useAsync } from "../../../lib/useAsync";

export function MembersModal({
  open,
  group,
  onClose,
  onChange,
}: {
  open: boolean;
  group: Group;
  onClose: () => void;
  onChange: () => Promise<void>;
}) {
  // `where` = the student's user_id, or "add" for the add form: the error shows there.
  const [error, setError] = useState<{ where: string; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [limit, setLimit] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");

  const { data, reload } = useAsync(async () => {
    if (!open) return null;
    const [detail, budgets] = await Promise.all([getGroup(group.id), listMemberBudgets(group.id)]);
    return detail.members.map((mem) => ({ ...mem, budget: budgets.find((b) => b.user_id === mem.user_id) ?? null }));
  }, [open, group.id]);

  const run = async (where: string, id: string, action: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await action();
      await Promise.all([reload(), onChange()]);
      return true;
    } catch (e) {
      setError({ where, text: e instanceof Error ? e.message : "Something went wrong" });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const add = async () => {
    const ok = await run("add", "add", () => addMember(group.id, email.trim(), name.trim() || undefined));
    if (ok) {
      setEmail("");
      setName("");
    }
  };

  const saveLimit = async (userId: string, value: string | null, saved: string) => {
    const ok = await run(userId, `limit-${userId}`, () => setMemberLimit(group.id, userId, value));
    if (ok) setEditing(null);
    else setLimit(saved); // refused: show the limit that's actually saved
  };

  const errorAt = (where: string) =>
    error?.where === where ? <Notice kind="error">{error.text}</Notice> : null;

  return (
    <Modal open={open} title={`Members of ${group.name}`} onClose={onClose}>
      {!data ? (
        <p className="panel-empty">Loading members…</p>
      ) : data.length === 0 ? (
        <p className="panel-empty">No students yet. Add one below.</p>
      ) : (
        data.map((m) => {
          const suspended = m.status === "suspended";
          const saved = m.budget?.limit ? String(Number(m.budget.limit.funded)) : "";
          return (
            <div key={m.user_id} className="item">
              <div className="row">
                <div className="grow">
                  <div className="item-title">{m.name}</div>
                  <div className="item-meta">
                    {m.email} · spent {usd(m.budget?.spent ?? "0")} this month ·{" "}
                    {m.budget?.limit
                      ? `own limit: ${usd(m.budget.limit.remaining)} left of ${usd(m.budget.limit.funded)}`
                      : "shares the team budget"}
                  </div>
                </div>
                {suspended && <Tag tone="out">Suspended</Tag>}
              </div>

              {editing === m.user_id ? (
                <div className="row">
                  <input className="input" type="number" min="0" step="0.01" value={limit}
                    onChange={(e) => setLimit(e.target.value)} aria-label="Monthly limit (USD)" style={{ maxWidth: 160 }} />
                  <Button size="sm" variant="primary" disabled={limit === ""} busy={busy === `limit-${m.user_id}`}
                    onClick={() => saveLimit(m.user_id, limit, saved)}>Save</Button>
                  <Button size="sm" onClick={() => { setEditing(null); setError(null); }}>Cancel</Button>
                  {m.budget?.limit && (
                    <Button size="sm" variant="quiet" onClick={() => saveLimit(m.user_id, null, saved)}>
                      Remove limit
                    </Button>
                  )}
                </div>
              ) : (
                <div className="row">
                  <Button size="sm" onClick={() => {
                    setEditing(m.user_id);
                    setError(null);
                    setLimit(saved);
                  }}>
                    {m.budget?.limit ? "Change limit" : "Set limit"}
                  </Button>
                  <Button size="sm" busy={busy === `status-${m.user_id}`}
                    onClick={() => run(m.user_id, `status-${m.user_id}`, () =>
                      setMemberStatus(group.id, m.user_id, suspended ? "active" : "suspended"))}>
                    {suspended ? "Reactivate" : "Suspend"}
                  </Button>
                  <Button size="sm" variant="danger" busy={busy === `remove-${m.user_id}`}
                    onClick={() => run(m.user_id, `remove-${m.user_id}`, () => removeMember(group.id, m.user_id))}>
                    Remove
                  </Button>
                </div>
              )}

              {errorAt(m.user_id)}
            </div>
          );
        })
      )}

      <div className="item">
        <div className="item-title">Add a student</div>
        <div className="field">
          <label htmlFor="member-email">Email</label>
          <input id="member-email" className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="member-name">Name</label>
          <input id="member-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
          <span className="hint">Only needed if this person doesn't have an account yet; they'll get an invite email.</span>
        </div>
        {errorAt("add")}
        <div className="row">
          <Button variant="primary" disabled={!email.trim()} busy={busy === "add"} onClick={add}>Add</Button>
        </div>
      </div>
    </Modal>
  );
}