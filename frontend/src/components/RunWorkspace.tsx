import { useEffect, useMemo, useState } from "react";

import { ApiError, api } from "../lib/api";
import type { Fact, FinalReport, Finding, RuleSet, Run, RunMetric, RunReview } from "../lib/types";
import { EvidenceList } from "./EvidenceList";
import { ReviewPanel } from "./ReviewPanel";
import { RunPipeline } from "./RunPipeline";
import { StatusBadge } from "./StatusBadge";

type Tab = "overview" | "facts" | "findings" | "review" | "metrics" | "report";

export function RunWorkspace({
  runId,
  ruleSets,
  actorId,
  onRunRefreshed
}: {
  runId: string;
  ruleSets: RuleSet[];
  actorId: string;
  onRunRefreshed: (run: Run) => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const [run, setRun] = useState<Run | null>(null);
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [review, setReview] = useState<RunReview | null>(null);
  const [metrics, setMetrics] = useState<RunMetric[] | null>(null);
  const [report, setReport] = useState<FinalReport | null>(null);
  const [reportContent, setReportContent] = useState<string | null>(null);
  const [selectedRuleSetIds, setSelectedRuleSetIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedRuleIds = useMemo(
    () => ruleSets.filter((set) => selectedRuleSetIds.includes(set.rule_set_id)).flatMap((set) => set.rules.map((rule) => rule.rule_id)),
    [ruleSets, selectedRuleSetIds]
  );

  useEffect(() => {
    setFacts(null);
    setFindings(null);
    setReview(null);
    setMetrics(null);
    setReport(null);
    setReportContent(null);
    setTab("overview");
    void refreshRun();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  async function refreshRun() {
    setError(null);
    try {
      const next = await api.getRun(runId);
      setRun(next);
      onRunRefreshed(next);
      return next;
    } catch (caught) {
      setError(messageFor(caught));
      return null;
    }
  }

  async function refreshReview() {
    try {
      const nextReview = await api.getRunReview(runId);
      setReview(nextReview);
      const nextRun = await refreshRun();
      if (nextReview.commit_ready && nextRun?.status === "REVIEW_REQUIRED") {
        await reconcileWorkerCommit();
      }
    } catch (caught) {
      setError(messageFor(caught));
    }
  }

  async function loadTab(next: Tab) {
    setTab(next);
    setError(null);
    try {
      if (next === "facts" && facts === null) setFacts(await api.getRunFacts(runId));
      if (next === "findings" && findings === null) setFindings(await api.getRunFindings(runId));
      if (next === "review") setReview(await api.getRunReview(runId));
      if (next === "metrics") setMetrics(await api.getRunMetrics(runId));
      if (next === "report") {
        const [metadata, content] = await Promise.all([api.getFinalReport(runId), api.getFinalReportContent(runId)]);
        setReport(metadata);
        setReportContent(content);
      }
    } catch (caught) {
      setError(messageFor(caught));
    }
  }

  async function advance() {
    setBusy(true);
    setError(null);
    try {
      const current = await refreshRun();
      if (current?.status === "COMPLETED") {
        await showCompletedRun();
        return;
      }

      await api.advanceRun(runId, selectedRuleSetIds, selectedRuleIds);
      clearDerivedRunState();
      const next = await refreshRun();
      if (next?.status === "REVIEW_REQUIRED") {
        setTab("review");
        setReview(await api.getRunReview(runId));
      } else if (next?.status === "COMPLETED") {
        await showCompletedRun();
      }
    } catch (caught) {
      // The background worker may win the COMMIT race between the UI's last
      // read and this manual advance. Re-read PostgreSQL-backed run state
      // before surfacing an integrity/conflict error. If COMMIT already won,
      // the user's requested outcome is complete and the stale POST is benign.
      const current = await refreshRun();
      if (current?.status === "COMPLETED") {
        await showCompletedRun();
        return;
      }
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  async function reconcileWorkerCommit() {
    for (let attempt = 0; attempt < 4; attempt += 1) {
      await delay(250);
      const current = await refreshRun();
      if (!current || current.status !== "REVIEW_REQUIRED") {
        if (current?.status === "COMPLETED") await showCompletedRun();
        return current;
      }
    }
    return run;
  }

  function clearDerivedRunState() {
    setFacts(null);
    setFindings(null);
    setReview(null);
    setMetrics(null);
    setReport(null);
    setReportContent(null);
  }

  async function showCompletedRun() {
    clearDerivedRunState();
    setTab("report");
    const [metadata, content] = await Promise.all([api.getFinalReport(runId), api.getFinalReportContent(runId)]);
    setReport(metadata);
    setReportContent(content);
    setError(null);
  }

  if (!run) {
    return <section className="panel">{error ? <div className="alert alert-error">{error}</div> : "Loading run…"}</section>;
  }

  return (
    <section className="run-workspace stack-lg">
      <section className="run-hero panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">{run.run_type} run</span>
            <h1>Run {shortId(run.run_id)}</h1>
            <div className="run-identifiers">
              <span className="mono tiny">{run.run_id}</span>
              {run.baseline_run_id ? <span>baseline {shortId(run.baseline_run_id)}</span> : null}
              {run.blocked_on_run_id ? <span>blocked on {shortId(run.blocked_on_run_id)}</span> : null}
            </div>
          </div>
          <StatusBadge value={run.status} />
        </div>

        <RunPipeline run={run} />

        <div className="advance-zone">
          <div className="rule-scope-picker">
            <strong>Rule scope for semantic stages</strong>
            <div className="check-list">
              {ruleSets.length ? ruleSets.map((set) => (
                <label className="check-item" key={set.rule_set_id}>
                  <input
                    checked={selectedRuleSetIds.includes(set.rule_set_id)}
                    onChange={(event) => setSelectedRuleSetIds((current) => event.target.checked ? [...current, set.rule_set_id] : current.filter((id) => id !== set.rule_set_id))}
                    type="checkbox"
                  />
                  <span>{set.name} v{set.version} · {set.rules.length} rules</span>
                </label>
              )) : <span className="muted">No rule sets available yet.</span>}
            </div>
          </div>
          <button className="button button-primary button-large" disabled={busy || run.status === "COMPLETED"} onClick={() => void advance()} type="button">
            {busy ? "Advancing…" : run.status === "REVIEW_REQUIRED" ? "Advance after review" : run.status === "COMPLETED" ? "Run completed" : "Advance one stage"}
          </button>
        </div>
        {error ? <div className="alert alert-error">{error}</div> : null}
      </section>

      <nav className="tab-bar" aria-label="Run details">
        {(["overview", "facts", "findings", "review", "metrics", "report"] as Tab[]).map((item) => (
          <button className={tab === item ? "tab active" : "tab"} key={item} onClick={() => void loadTab(item)} type="button">
            {item}
          </button>
        ))}
      </nav>

      {tab === "overview" ? <Overview run={run} /> : null}
      {tab === "facts" ? <FactsPanel facts={facts} /> : null}
      {tab === "findings" ? <FindingsPanel findings={findings} /> : null}
      {tab === "review" ? review ? <ReviewPanel actorId={actorId} review={review} onChanged={refreshReview} /> : <LoadingPanel /> : null}
      {tab === "metrics" ? <MetricsPanel metrics={metrics} /> : null}
      {tab === "report" ? <ReportPanel report={report} content={reportContent} /> : null}
    </section>
  );
}

function Overview({ run }: { run: Run }) {
  return (
    <div className="metric-cards">
      <Metric label="Run type" value={run.run_type} />
      <Metric label="Status" value={run.status} />
      <Metric label="Attempts" value={String(run.steps.length)} />
      <Metric label="COMMIT required" value={run.commit_required ? "Yes" : "Historical compatibility"} />
    </div>
  );
}

function FactsPanel({ facts }: { facts: Fact[] | null }) {
  if (facts === null) return <LoadingPanel />;
  if (!facts.length) return <section className="panel empty-state">No facts are present for this run.</section>;
  return (
    <section className="stack-lg">
      {facts.map((fact) => (
        <article className="panel fact-card" key={fact.fact_id}>
          <div className="section-heading compact">
            <div><span className="eyebrow">{fact.relation}</span><h3>{fact.subject} · {fact.predicate}</h3></div>
            <StatusBadge value={fact.status} />
          </div>
          <pre className="object-value">{JSON.stringify(fact.object_value, null, 2)}</pre>
          <EvidenceList evidence={fact.evidence} />
        </article>
      ))}
    </section>
  );
}

function FindingsPanel({ findings }: { findings: Finding[] | null }) {
  if (findings === null) return <LoadingPanel />;
  if (!findings.length) return <section className="panel empty-state">No findings are present for this run.</section>;
  return (
    <section className="stack-lg">
      {findings.map((finding) => (
        <article className="panel finding-card" key={finding.finding_id}>
          <div className="review-tags"><StatusBadge value={finding.finding_type} /><StatusBadge value={finding.severity} /><StatusBadge value={finding.status} /></div>
          <h3>{finding.title}</h3>
          {finding.description ? <p>{finding.description}</p> : null}
          <EvidenceList evidence={finding.evidence} />
        </article>
      ))}
    </section>
  );
}

function MetricsPanel({ metrics }: { metrics: RunMetric[] | null }) {
  if (metrics === null) return <LoadingPanel />;
  return (
    <section className="panel table-wrap">
      <table>
        <thead><tr><th>Stage</th><th>Attempt</th><th>Duration</th><th>Input tokens</th><th>Output tokens</th><th>Estimated cost</th></tr></thead>
        <tbody>
          {metrics.map((metric) => (
            <tr key={metric.metric_id}>
              <td><StatusBadge value={metric.stage} /></td><td>{metric.attempt}</td><td>{metric.duration_ms} ms</td><td>{metric.input_tokens ?? "—"}</td><td>{metric.output_tokens ?? "—"}</td><td>{metric.estimated_cost ?? "—"}</td>
            </tr>
          ))}
          {!metrics.length ? <tr><td colSpan={6}>No closed-attempt metrics yet.</td></tr> : null}
        </tbody>
      </table>
    </section>
  );
}

function ReportPanel({ report, content }: { report: FinalReport | null; content: string | null }) {
  if (!report || content === null) return <LoadingPanel />;
  return (
    <section className="stack-lg">
      <div className="metric-cards">
        <Metric label="Output" value={shortId(report.output_id)} />
        <Metric label="Hash" value={shortHash(report.content_hash)} />
        <Metric label="Render input" value={shortHash(report.render_input_fingerprint)} />
        <Metric label="Focused update proof" value={report.focused_update ? "Present" : "Not applicable"} />
      </div>
      <section className="panel report-panel">
        <div className="section-heading compact"><div><span className="eyebrow">Verified committed artifact</span><h2>Final report</h2></div><a className="button button-secondary" href={`/api/v1/runs/${report.run_id}/report/content`} download={`report-${report.run_id}.md`}>Download Markdown</a></div>
        <pre className="report-content">{content}</pre>
      </section>
      {report.focused_update ? <section className="panel"><span className="eyebrow">Preservation proof</span><pre className="manifest">{JSON.stringify(report.focused_update, null, 2)}</pre></section> : null}
    </section>
  );
}

function LoadingPanel() { return <section className="panel">Loading…</section>; }
function delay(milliseconds: number) { return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds)); }
function Metric({ label, value }: { label: string; value: string }) { return <div className="metric-card"><span>{label}</span><strong>{value}</strong></div>; }
function shortId(value: string) { return `${value.slice(0, 8)}…${value.slice(-4)}`; }
function shortHash(value: string) { return `${value.slice(0, 15)}…${value.slice(-8)}`; }
function messageFor(error: unknown) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error instanceof Error ? error.message : "Unexpected run error";
}
