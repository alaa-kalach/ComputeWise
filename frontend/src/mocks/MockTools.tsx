// A small panel, mock mode only, to play the parts of the system that aren't built yet:
// the professor approving (C3) and the sync job recording spend (Person 2).

import { useState } from "react";

import { Button } from "../components/ui";
import { approvePendingFor, mockPersona, simulateSpend } from "./handlers";
import { GROUPS, MEMBERS, resetWorld } from "./world";

export function MockTools() {
  const [note, setNote] = useState<string | null>(null);
  const persona = mockPersona();
  const groups = persona ? GROUPS.filter((g) => MEMBERS[g.id].includes(persona.key)) : [];
  const done = (text: string) => {
    setNote(text);
    window.dispatchEvent(new Event("mock:changed"));
  };

  return (
    <details className="mocktools">
      <summary>Mock tools</summary>
      <div className="body">
        <p>Plays the parts that aren't built yet. Changes last until you reload.</p>
        {persona && groups.length > 0 && (
          <>
            <Button size="sm" onClick={() => done(`Approved ${approvePendingFor(persona.key)} pending request(s).`)}>
              Professor approves my pending requests
            </Button>
            {groups.map((g) => (
              <Button key={g.id} size="sm" onClick={() => (simulateSpend(persona.key, g.id, "5"), done(`Recorded $5 of spend in ${g.name}.`))}>
                Spend $5 in {g.name}
              </Button>
            ))}
          </>
        )}
        <Button size="sm" variant="quiet" onClick={() => (resetWorld(), done("Mock data reset."))}>
          Reset mock data
        </Button>
        {note && <p role="status">{note}</p>}
      </div>
    </details>
  );
}
