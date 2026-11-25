import { useState } from "react";

import { Button, IconGpu, IconModels, Notice, Tag } from "../../components/ui";
import { revokeCredential, revokeGrant } from "../../lib/api/endpoints";
import type { Credential, Grant, Professor } from "../../lib/api/types";
import { usd } from "../../lib/money";
import { ConfirmModal } from "../professor/modals/ConfirmModal";
import { AddCredentialModal } from "./modals/AddCredentialModal";
import { GrantModal } from "./modals/GrantModal";

type Pending =
  | { kind: "revoke"; credential: Credential }
  | { kind: "end"; credential: Credential; grant: Grant }
  | null;

/** A3/C6: the department's own provider accounts and who may use them. The key is never shown. */
export function CredentialsSection({
  credentials,
  professors,
  onChange,
}: {
  credentials: Credential[];
  professors: Professor[];
  onChange: () => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [granting, setGranting] = useState<{ credential: Credential; grant: Grant | null } | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [note, setNote] = useState<string | null>(null);

  const mine = credentials.filter((c) => c.owner_type === "department");
  const active = mine.filter((c) => c.status === "active");
  const revoked = mine.filter((c) => c.status === "revoked");

  const done = async (text: string) => {
    setNote(text);
    setAdding(false);
    setGranting(null);
    await onChange();
  };

  return (
    <section className="group" aria-labelledby="h-credentials">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-credentials">Department accounts</h2>
          <span className="muted">Provider accounts the department pays for, and the professors allowed to use them</span>
        </div>
        <div className="toolbar">
          <Button variant="primary" onClick={() => (setNote(null), setAdding(true))}>Add account</Button>
        </div>
      </div>

      {note && <Notice kind="info">{note}</Notice>}

      {active.length === 0 ? (
        <p className="panel-empty">No department accounts yet. Professors can still use their own.</p>
      ) : (
        <div className="columns">
          {active.map((c) => {
            const grants = c.grants.filter((g) => g.status === "active");
            return (
              <div key={c.id} className="panel">
                <h3>
                  <span className="panel-icon">{c.provider === "openrouter" ? <IconModels /> : <IconGpu />}</span>
                  <span className="grow">{c.label}</span>
                  <Tag tone="ok">Active</Tag>
                </h3>
                <p className="muted">
                  {c.provider === "openrouter" ? "OpenRouter management key" : "RunPod API key"} · the key is never shown
                </p>
                <div className="panel-body">
                  {grants.length === 0 ? (
                    <p className="panel-empty">Not granted to anyone yet.</p>
                  ) : (
                    grants.map((g) => (
                      <div key={g.id} className="item">
                        <div className="row">
                          <div className="grow">
                            <div className="item-title">{g.professor_name}</div>
                            <div className="item-meta">{g.ceiling_usd ? `Ceiling ${usd(g.ceiling_usd)}` : "No ceiling"}</div>
                          </div>
                          <Button size="sm" onClick={() => (setNote(null), setGranting({ credential: c, grant: g }))}
                            aria-label={`Change ${g.professor_name}'s ceiling on ${c.label}`}>
                            Ceiling
                          </Button>
                          <Button size="sm" variant="quiet" onClick={() => setPending({ kind: "end", credential: c, grant: g })}
                            aria-label={`End ${g.professor_name}'s grant on ${c.label}`}>
                            End
                          </Button>
                        </div>
                      </div>
                    ))
                  )}
                  <div className="row">
                    <Button size="sm" onClick={() => (setNote(null), setGranting({ credential: c, grant: null }))}
                      aria-label={`Grant ${c.label} to a professor`}>
                      Grant to a professor
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => setPending({ kind: "revoke", credential: c })}
                      aria-label={`Revoke ${c.label}`}>
                      Revoke
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <p className="item-meta">Revoking an account disables every key issued under it and stops its running pods.</p>
      {revoked.length > 0 && (
        <p className="item-meta">Revoked: {revoked.map((c) => c.label).join(", ")}. Their past spend stays in the reports.</p>
      )}

      <AddCredentialModal open={adding} onClose={() => setAdding(false)}
        onDone={(label) => done(`${label} was added. Grant it to the professors who should use it.`)} />

      <GrantModal credential={granting?.credential ?? null} grant={granting?.grant ?? null}
        professors={professors} onClose={() => setGranting(null)} onDone={done} />

      <ConfirmModal
        open={pending !== null}
        title={pending?.kind === "revoke" ? `Revoke ${pending.credential.label}?` : `End ${pending?.kind === "end" ? pending.grant.professor_name : ""}'s grant?`}
        confirmLabel={pending?.kind === "revoke" ? "Revoke account" : "End grant"}
        onCancel={() => setPending(null)}
        onConfirm={async () => {
          if (!pending) return;
          if (pending.kind === "revoke") {
            await revokeCredential(pending.credential.id);
            setNote(`${pending.credential.label} was revoked.`);
          } else {
            await revokeGrant(pending.credential.id, pending.grant.id);
            setNote(`${pending.grant.professor_name} can no longer use ${pending.credential.label}.`);
          }
          setPending(null);
          await onChange();
        }}
      >
        {pending?.kind === "revoke" ? (
          <p>
            Nobody can use this account any more. Every OpenRouter key issued under it is disabled and its running
            pods are stopped, in every professor's groups. This can't be undone; you would add the account again.
          </p>
        ) : (
          <p>
            His teams' keys on this account are disabled and their pods on it are stopped. He can switch to another
            account, or you can grant this one again later.
          </p>
        )}
      </ConfirmModal>
    </section>
  );
}
