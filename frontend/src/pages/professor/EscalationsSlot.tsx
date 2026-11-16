// Placeholder: Leen (C7) replaces the empty text with the agent's escalation cards.
export function EscalationsSlot() {
  return (
    <section className="group" aria-labelledby="h-alerts">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-alerts">Agent alerts</h2>
          <span className="muted">Suggestions from the budget assistant that need your decision</span>
        </div>
      </div>
      <p className="panel-empty">No alerts right now.</p>
    </section>
  );
}