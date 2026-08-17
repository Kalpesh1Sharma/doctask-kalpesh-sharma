import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../lib/api";
import type { Document, Pile, RuleSet, Run } from "../lib/types";
import { DocumentsPanel } from "./DocumentsPanel";
import { RulesPanel } from "./RulesPanel";
import { RunWorkspace } from "./RunWorkspace";
import { StatusBadge } from "./StatusBadge";

type Section = "runs" | "documents" | "rules";

export function PileWorkspace({ pile, onBack }: { pile: Pile; onBack: () => void }) {
  const [section, setSection] = useState<Section>("runs");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(pile.latest_run_id);
  const [actorId, setActorId] = useState(() => localStorage.getItem("task1.reviewer-id") ?? "reviewer@example.test");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [nextDocuments, nextRuns, nextRuleSets] = await Promise.all([
        api.listDocuments(pile.pile_id),
        api.listRuns(pile.pile_id),
        api.listRuleSets(pile.pile_id)
      ]);
      setDocuments(nextDocuments);
      setRuns(nextRuns);
      setRuleSets(nextRuleSets);
      if (!selectedRunId && nextRuns.length) setSelectedRunId(nextRuns[0].run_id);
    } catch (caught) {
      setError(messageFor(caught));
    }
  }, [pile.pile_id, selectedRunId]);

  useEffect(() => { void refresh(); }, [refresh]);

  function updateActor(value: string) {
    setActorId(value);
    localStorage.setItem("task1.reviewer-id", value);
  }

  function selectRun(runId: string) {
    setSelectedRunId(runId);
    setSection("runs");
  }

  function replaceRun(updated: Run) {
    setRuns((current) => current.map((run) => run.run_id === updated.run_id ? updated : run));
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={onBack} type="button">
          <span className="brand-mark">S</span>
          <span><strong>SuperDocs</strong><small>Task 1 review</small></span>
        </button>

        <div className="pile-summary">
          <span className="eyebrow">Active pile</span>
          <h2>{pile.name}</h2>
          <span className="mono tiny">{pile.pile_id}</span>
        </div>

        <nav className="side-nav">
          <button className={section === "runs" ? "active" : ""} onClick={() => setSection("runs")} type="button"><span>Runs</span><strong>{runs.length}</strong></button>
          <button className={section === "documents" ? "active" : ""} onClick={() => setSection("documents")} type="button"><span>Documents</span><strong>{documents.length}</strong></button>
          <button className={section === "rules" ? "active" : ""} onClick={() => setSection("rules")} type="button"><span>Rules</span><strong>{ruleSets.length}</strong></button>
        </nav>

        <div className="sidebar-footer">
          <label>
            Reviewer identity
            <input value={actorId} onChange={(event) => updateActor(event.target.value)} />
          </label>
          <button className="text-button" onClick={onBack} type="button">Switch pile</button>
        </div>
      </aside>

      <main className="workspace-main">
        <header className="topbar">
          <div>
            <span className="eyebrow">{section}</span>
            <h1>{section === "runs" ? "Workflow runs" : section === "documents" ? "Documents & versions" : "Rule sets"}</h1>
          </div>
          <div className="topbar-actions">
            <StatusBadge value="POSTGRESQL_AUTHORITATIVE" />
            <button className="button button-secondary" onClick={() => void refresh()} type="button">Refresh</button>
          </div>
        </header>

        {error ? <div className="alert alert-error">{error}</div> : null}

        {section === "documents" ? <DocumentsPanel documents={documents} onChanged={refresh} onRunCreated={selectRun} pileId={pile.pile_id} /> : null}
        {section === "rules" ? <RulesPanel onChanged={refresh} pileId={pile.pile_id} ruleSets={ruleSets} /> : null}
        {section === "runs" ? (
          <div className="runs-layout">
            <aside className="run-list panel">
              <div className="section-heading compact"><div><span className="eyebrow">History</span><h2>Runs</h2></div></div>
              {!runs.length ? <div className="empty-state">Upload a document to create the first run.</div> : runs.map((run) => (
                <button className={selectedRunId === run.run_id ? "run-list-item active" : "run-list-item"} key={run.run_id} onClick={() => setSelectedRunId(run.run_id)} type="button">
                  <div><strong>{run.run_type}</strong><StatusBadge value={run.status} /></div>
                  <span className="mono tiny">{run.run_id}</span>
                  <span className="muted">{formatDate(run.created_at)}</span>
                </button>
              ))}
            </aside>
            <div className="run-detail">
              {selectedRunId ? <RunWorkspace actorId={actorId} onRunRefreshed={replaceRun} ruleSets={ruleSets} runId={selectedRunId} /> : <section className="panel empty-state">Select a run to inspect its five-stage workflow.</section>}
            </div>
          </div>
        ) : null}
      </main>
    </div>
  );
}

function formatDate(value: string) { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); }
function messageFor(error: unknown) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error instanceof Error ? error.message : "Unexpected workspace error";
}
