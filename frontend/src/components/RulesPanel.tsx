import { FormEvent, useState } from "react";

import { ApiError, api } from "../lib/api";
import type { RuleSet } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

const EXAMPLE_RULES = `[
  {
    "rule_key": "payment-term",
    "title": "Payment term maximum",
    "description": "Flag terms longer than allowed",
    "severity": "HIGH",
    "definition": {"max_days": 45}
  }
]`;

export function RulesPanel({ pileId, ruleSets, onChanged }: { pileId: string; ruleSets: RuleSet[]; onChanged: () => Promise<void> }) {
  const [name, setName] = useState("Contract playbook");
  const [version, setVersion] = useState(1);
  const [rulesJson, setRulesJson] = useState(EXAMPLE_RULES);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const parsed = JSON.parse(rulesJson) as unknown;
      if (!Array.isArray(parsed) || parsed.length === 0) throw new Error("Rules JSON must be a non-empty array.");
      await api.createRuleSet(pileId, {
        name: name.trim(),
        description: null,
        version,
        rules: parsed as Array<{ rule_key: string; title: string; description: string | null; severity: string; definition: unknown }>
      });
      await onChanged();
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stack-lg">
      <form className="panel" onSubmit={create}>
        <div className="section-heading compact">
          <div>
            <span className="eyebrow">Evaluation scope</span>
            <h2>Create a rule set</h2>
          </div>
        </div>
        <div className="form-grid-two">
          <label>Name<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>Version<input min={1} onChange={(event) => setVersion(Number(event.target.value))} type="number" value={version} required /></label>
        </div>
        <label>Rules JSON<textarea className="code-input" rows={11} value={rulesJson} onChange={(event) => setRulesJson(event.target.value)} /></label>
        {error ? <div className="alert alert-error">{error}</div> : null}
        <button className="button button-primary" disabled={busy} type="submit">{busy ? "Creating…" : "Create rule set"}</button>
      </form>

      <section className="panel">
        <div className="section-heading compact"><div><span className="eyebrow">Available scope</span><h2>Rule sets</h2></div><span className="count-pill">{ruleSets.length}</span></div>
        {!ruleSets.length ? <div className="empty-state">Create at least one rule set before advancing semantic stages.</div> : (
          <div className="rule-set-grid">
            {ruleSets.map((set) => (
              <article className="rule-set-card" key={set.rule_set_id}>
                <header><div><strong>{set.name}</strong><span>v{set.version}</span></div><span className="mono tiny">{set.rule_set_id}</span></header>
                {set.rules.map((rule) => (
                  <div className="rule-row" key={rule.rule_id}>
                    <div><strong>{rule.title}</strong><span className="muted">{rule.rule_key}</span></div>
                    <StatusBadge value={rule.severity} />
                  </div>
                ))}
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}

function messageFor(error: unknown) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error instanceof Error ? error.message : "Unexpected rule-set error";
}
