import { FormEvent, useState } from "react";

import { ApiError, api } from "../lib/api";
import type { Pile } from "../lib/types";
import type { RecentPile } from "../lib/recentPiles";

export function PileLanding({
  recentPiles,
  onOpen
}: {
  recentPiles: RecentPile[];
  onOpen: (pile: Pile) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [pileId, setPileId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function createPile(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createPile({
        name: name.trim(),
        description: description.trim() || null,
        watch_enabled: false,
        watch_location: null
      });
      onOpen(created);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  async function openPile(event: FormEvent) {
    event.preventDefault();
    if (!pileId.trim()) return;
    setBusy(true);
    setError(null);
    try {
      onOpen(await api.getPile(pileId.trim()));
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="landing-shell">
      <section className="hero-card">
        <span className="eyebrow">Task 1 · review console</span>
        <h1>Evidence-first document analysis with a real human gate.</h1>
        <p>
          Upload source documents, inspect the five-stage run, review exact evidence, and commit only after explicit approval.
        </p>
        <div className="hero-pills">
          <span>INGEST</span><span>UNDERSTAND</span><span>EXAMINE</span><span>REVIEW</span><span>COMMIT</span>
        </div>
      </section>

      {error ? <div className="alert alert-error landing-alert">{error}</div> : null}

      <div className="landing-grid">
        <form className="panel" onSubmit={createPile}>
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">New workspace</span>
              <h2>Create a pile</h2>
            </div>
          </div>
          <label>
            Name
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Acme contracts" required />
          </label>
          <label>
            Description
            <textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What belongs in this pile?" rows={3} />
          </label>
          <button className="button button-primary" disabled={busy} type="submit">Create pile</button>
        </form>

        <section className="panel">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">Existing workspace</span>
              <h2>Open a pile</h2>
            </div>
          </div>
          <form className="inline-form" onSubmit={openPile}>
            <input value={pileId} onChange={(event) => setPileId(event.target.value)} placeholder="Pile UUID" aria-label="Pile ID" />
            <button className="button button-secondary" disabled={busy} type="submit">Open</button>
          </form>

          <div className="recent-list">
            <h3>Recent piles</h3>
            {recentPiles.length ? recentPiles.map((pile) => (
              <button
                className="recent-pile"
                key={pile.pile_id}
                onClick={() => {
                  setPileId(pile.pile_id);
                  void api.getPile(pile.pile_id).then(onOpen).catch((caught) => setError(messageFor(caught)));
                }}
                type="button"
              >
                <strong>{pile.name}</strong>
                <span className="mono tiny">{pile.pile_id}</span>
              </button>
            )) : <p className="muted">Piles you open in this browser appear here.</p>}
          </div>
        </section>
      </div>
    </main>
  );
}

function messageFor(error: unknown) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error instanceof Error ? error.message : "Unexpected API error";
}
