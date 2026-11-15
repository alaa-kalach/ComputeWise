// The student's OpenRouter key in one group. The key itself is shown exactly once, right
// after he gets it; afterwards only its usage and limit are visible.

import { useState } from "react";

import { Button, CopyField, IconModels, Modal, Notice, Tag } from "../../components/ui";
import { getMyKey } from "../../lib/api/endpoints";
import type { IssuedKey } from "../../lib/api/types";
import { fraction, usd } from "../../lib/money";
import { AccessState } from "./RequestAccess";
import { hasApproved, latestRequest, type GroupData } from "./shared";

const KEY_STATUS = { active: ["Active", "ok"], capped: ["Limit reached", "low"], disabled: ["Disabled", "out"] } as const;

export function KeysPanel({ group, onChange }: { group: GroupData; onChange: () => Promise<void> }) {
  const { membership, keys, requests } = group;
  const request = latestRequest(requests, "openrouter");
  const approved = hasApproved(requests, "openrouter");
  const [issued, setIssued] = useState<IssuedKey | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const getKey = async () => {
    setBusy(true);
    setError(null);
    try {
      setIssued(await getMyKey(membership.group_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't create your key");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3>
        <span className="panel-icon"><IconModels /></span>
        AI models
      </h3>
      <p className="muted">An OpenRouter key with a spending limit from this group's budget.</p>

      {keys.length > 0 ? (
        <div className="panel-body">
          {keys.map((k) => {
            const [text, tone] = KEY_STATUS[k.status];
            return (
              <div className="item" key={k.id}>
                <div className="row">
                  <span className="item-title grow">Your key</span>
                  <Tag tone={tone}>{text}</Tag>
                </div>
                {k.limit_usd !== null && (
                  <div className="usage" aria-hidden="true">
                    <span style={{ width: `${fraction(k.usage_usd, k.limit_usd) * 100}%` }} />
                  </div>
                )}
                <div className="item-meta">
                  {usd(k.usage_usd)} used{k.limit_usd !== null ? ` of a ${usd(k.limit_usd)} limit` : ""}. The limit
                  follows your remaining budget.
                </div>
              </div>
            );
          })}
          <p className="item-meta">
            The key was shown once when you got it. If you lost it, ask your professor to revoke it so you can get a
            new one.
          </p>
        </div>
      ) : approved ? (
        <div className="panel-body">
          <p className="panel-empty">Your request was approved. Get your key now: it will be shown only once.</p>
          {error && <Notice kind="error">{error}</Notice>}
          <div>
            <Button variant="primary" busy={busy} onClick={getKey}>
              Get my key
            </Button>
          </div>
        </div>
      ) : (
        <AccessState provider="openrouter" groupId={membership.group_id} groupName={membership.group_name}
          request={request} onChange={onChange} />
      )}

      <Modal
        open={issued !== null}
        title="Your OpenRouter key"
        onClose={() => {
          setIssued(null);
          void onChange();
        }}
        footer={
          <Button variant="primary" onClick={() => { setIssued(null); void onChange(); }}>
            I've saved it
          </Button>
        }
      >
        <Notice kind="warning">
          Copy it now. ComputeWise doesn't keep the key, so it can't be shown again.
        </Notice>
        {issued && (
          <>
            <CopyField label="Key" value={issued.key} />
            <p className="muted">
              Spending limit: {usd(issued.limit_usd)}. Use it as the API key in any OpenRouter client, for example as
              the <code>OPENROUTER_API_KEY</code> environment variable.
            </p>
          </>
        )}
      </Modal>
    </div>
  );
}
