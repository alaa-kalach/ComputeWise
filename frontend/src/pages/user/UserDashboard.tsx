// C4: the student's dashboard. One section per group, because a student in two professors'
// groups has a separate budget under each. In each: the team budget (and his own limit, if
// he has one), his OpenRouter key, and his RunPod pods, with access requests where needed.

import { useEffect } from "react";

import { BudgetMeter, BudgetTag, Loading, Monogram, Notice, Tag } from "../../components/ui";
import {
  getGroupBudget,
  listAccessRequests,
  listMyKeys,
  listNotifications,
  listPods,
} from "../../lib/api/endpoints";
import { useAuth } from "../../lib/auth";
import { budgetLevel, fraction, usd } from "../../lib/money";
import { useAsync } from "../../lib/useAsync";
import { KeysPanel } from "./KeysPanel";
import { PodsPanel } from "./PodsPanel";
import type { GroupData } from "./shared";

export function UserDashboard() {
  const { me } = useAuth();
  const memberships = me?.memberships ?? [];

  const { data, error, loading, reload } = useAsync(async () => {
    const [budgets, requests, keys, pods, notes] = await Promise.all([
      Promise.all(memberships.map((m) => getGroupBudget(m.group_id))),
      listAccessRequests(),
      listMyKeys(),
      listPods(),
      listNotifications(),
    ]);
    const mine = requests.filter((r) => r.user_id === me?.id);
    const groups: GroupData[] = memberships.map((m, i) => ({
      membership: m,
      budget: budgets[i],
      requests: mine.filter((r) => r.group_id === m.group_id),
      keys: keys.filter((k) => k.group_id === m.group_id),
      pods: pods.filter((p) => p.group_id === m.group_id && p.user_id === me?.id),
    }));
    return { groups, notes: notes.filter((n) => !n.read) };
  }, [me?.id]);

  // Mock tools change the world behind our back: refresh when they do.
  useEffect(() => {
    const onChange = () => void reload();
    window.addEventListener("mock:changed", onChange);
    return () => window.removeEventListener("mock:changed", onChange);
  }, [reload]);

  if (!me) return null;

  if (memberships.length === 0) {
    return (
      <section className="hero">
        <h1>You're not in a group yet.</h1>
        <p>Ask your professor to add you with {me.email}. Your groups and budgets will show up here.</p>
      </section>
    );
  }

  if (error && !data) return <Notice kind="error">Couldn't load your dashboard: {error.message}</Notice>;
  if (!data) return <Loading what="your groups" />;

  const active = data.groups.filter((g) => g.membership.status === "active");
  return (
    <>
      <div className="hero-band">
        <section className="hero" aria-busy={loading}>
          <h1>
            {active.length === 0 ? (
              "You're suspended in all your groups this month."
            ) : (
              <>
                You can spend{" "}
                {active.map((g, i) => (
                  <span key={g.membership.group_id}>
                    {i > 0 && (i === active.length - 1 ? " and " : ", ")}
                    <strong>{usd(g.budget.available)}</strong> in {g.membership.group_name}
                  </span>
                ))}{" "}
                this month.
              </>
            )}
          </h1>
          <p>Each group has its own budget from its professor, and spends only from it.</p>
        </section>

        <nav className="gauges" aria-label="Your groups">
          {data.groups.map((g) => {
            const t = g.budget.team;
            return (
              <a key={g.membership.group_id} className="gauge" href={`#g-${g.membership.group_id}`}
                data-level={budgetLevel(t.remaining, t.funded)}>
                <span className="gauge-name">{g.membership.group_name}</span>
                <span className="gauge-value">{usd(g.budget.available)}</span>
                <span className="gauge-track" aria-hidden="true">
                  <span style={{ width: `${fraction(t.remaining, t.funded) * 100}%` }} />
                </span>
                <span className="gauge-name">
                  {g.membership.status === "suspended" ? "Suspended" : `team has ${usd(t.remaining)} of ${usd(t.funded)}`}
                </span>
              </a>
            );
          })}
        </nav>

        {data.notes.length > 0 && (
          <div className="notices">
            {data.notes.map((n) => (
              <Notice key={n.id} kind={n.level === "info" ? "info" : n.level}>
                {n.message}
              </Notice>
            ))}
          </div>
        )}
      </div>

      {data.groups.map((g) => (
        <GroupSection key={g.membership.group_id} group={g} onChange={reload} />
      ))}
    </>
  );
}

function GroupSection({ group, onChange }: { group: GroupData; onChange: () => Promise<void> }) {
  const { membership, budget } = group;
  const suspended = membership.status === "suspended";
  return (
    <section className="group" id={`g-${membership.group_id}`} aria-labelledby={`h-${membership.group_id}`}>
      <div className="group-head">
        <Monogram name={membership.group_name} />
        <div className="group-title">
          <h2 id={`h-${membership.group_id}`}>{membership.group_name}</h2>
          <span className="muted">
            {budget.mine ? "Your own limit applies inside the team budget" : "You share the team budget"}
          </span>
        </div>
        {suspended ? <Tag tone="out">Suspended</Tag> : <BudgetTag remaining={budget.team.remaining} funded={budget.team.funded} />}
      </div>

      <BudgetMeter {...budget.team} mine={budget.mine} />

      {suspended ? (
        <Notice kind="warning">
          Your professor suspended you in this group. Your keys here are disabled and your pods are stopped. Ask your
          professor to reactivate you.
        </Notice>
      ) : (
        <div className="columns">
          <KeysPanel group={group} onChange={onChange} />
          <PodsPanel group={group} onChange={onChange} />
        </div>
      )}
    </section>
  );
}
