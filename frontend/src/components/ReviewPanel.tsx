import { useState } from "react";

import { ApiError, api } from "../lib/api";
import type { RunReview } from "../lib/types";
import { EvidenceList } from "./EvidenceList";
import { StatusBadge } from "./StatusBadge";

export function ReviewPanel({
  review,
  actorId,
  onChanged
}: {
  review: RunReview;
  actorId: string;
  onChanged: () => Promise<void> | void;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decideFinding(findingId: string, decision: "APPROVED" | "REJECTED") {
    setPending(findingId);
    setError(null);
    try {
      await api.reviewFinding(review.run_id, findingId, decision, actorId);
      await onChanged();
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setPending(null);
    }
  }

  async function decideUpdate(
    decision: "APPROVED" | "REJECTED",
    factId: string | null = null
  ) {
    if (!review.update_change_set) return;
    const pendingKey = factId ? `update:${factId}` : "update";
    setPending(pendingKey);
    setError(null);
    try {
      await api.reviewUpdate(
        review.run_id,
        decision,
        actorId,
        review.update_change_set.change_set_fingerprint,
        factId
      );
      await onChanged();
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setPending(null);
    }
  }

  return (
    <section className="stack-lg" aria-label="Human review">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Human gate</span>
          <h2>Review findings</h2>
          <p className="muted">Approve or reject each finding independently. Decisions are durable and idempotent.</p>
        </div>
        <div className="review-state">
          <StatusBadge value={review.review_complete ? "REVIEW_COMPLETE" : "REVIEW_REQUIRED"} />
          {review.commit_ready ? <StatusBadge value="COMMIT_READY" /> : null}
        </div>
      </div>

      {error ? <div className="alert alert-error">{error}</div> : null}

      {review.findings.length === 0 ? (
        <div className="empty-state">No findings require item-level review for this run.</div>
      ) : (
        <div className="review-grid">
          {review.findings.map((finding) => (
            <article className="review-card" key={finding.finding_id}>
              <header>
                <div>
                  <div className="review-tags">
                    <StatusBadge value={finding.finding_type} />
                    <StatusBadge value={finding.severity} />
                    <StatusBadge value={finding.status} />
                  </div>
                  <h3>{finding.title}</h3>
                  {finding.description ? <p>{finding.description}</p> : null}
                </div>
                <span className="mono tiny">{finding.finding_id}</span>
              </header>

              <EvidenceList evidence={finding.evidence} />

              {finding.decision ? (
                <div className="decision-summary">
                  <StatusBadge value={finding.decision.decision} />
                  <span>
                    by {finding.decision.actor_id} · {formatDate(finding.decision.decided_at)}
                  </span>
                </div>
              ) : (
                <div className="button-row">
                  <button
                    className="button button-primary"
                    disabled={pending !== null}
                    onClick={() => void decideFinding(finding.finding_id, "APPROVED")}
                    type="button"
                  >
                    {pending === finding.finding_id ? "Saving…" : "Approve"}
                  </button>
                  <button
                    className="button button-danger-outline"
                    disabled={pending !== null}
                    onClick={() => void decideFinding(finding.finding_id, "REJECTED")}
                    type="button"
                  >
                    Reject
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {review.update_change_set ? (
        <article className="update-review-card">
          <div className="section-heading compact">
            <div>
              <span className="eyebrow">Focused update</span>
              <h2>Review UPDATE changes item by item</h2>
              <p className="muted">Each material Fact change is fingerprint-bound and can be approved or rejected independently.</p>
            </div>
            <StatusBadge value={review.update_change_set.durable ? "DURABLE" : "PREVIEW"} />
          </div>
          <div className="fingerprint mono">{review.update_change_set.change_set_fingerprint}</div>

          {review.update_change_set.items.length ? (
            <div className="review-grid">
              {review.update_change_set.items.map((item) => {
                const pendingKey = `update:${item.fact_id}`;
                const previousValues = item.baseline_facts.map((fact) => fact.display_value);
                const duplicateValue = item.baseline_facts.some(
                  (fact) => jsonEqual(fact.display_value, item.fact.display_value)
                );
                return (
                  <article className="review-card" key={item.fact_id}>
                    <header>
                      <div>
                        <div className="review-tags">
                          <StatusBadge value={item.kind} />
                          {duplicateValue ? <StatusBadge value="SAME_VALUE" /> : null}
                        </div>
                        <h3>{displayConcept(item.fact.concept, item.fact.subject, item.fact.predicate)}</h3>
                      </div>
                      <span className="mono tiny">{item.fact_id}</span>
                    </header>

                    <div className="stack-sm">
                      {item.kind === "FACT_CREATED" ? (
                        <>
                          {previousValues.length ? (
                            <div>
                              <div className="eyebrow">Committed baseline</div>
                              {item.baseline_facts.map((baselineFact) => (
                                <div className="stack-sm" key={baselineFact.fact_id}>
                                  <strong>{formatValue(baselineFact.display_value)}</strong>
                                  {baselineFact.evidence.map((evidence) => (
                                    <blockquote key={evidence.evidence_id}>
                                      <div className="mono tiny">{evidence.logical_key}</div>
                                      <div>{evidence.source_text}</div>
                                    </blockquote>
                                  ))}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <p className="muted">No committed baseline Fact uses this concept.</p>
                          )}
                          <div>
                            <div className="eyebrow">Proposed value</div>
                            <strong>{formatValue(item.fact.display_value)}</strong>
                          </div>
                          {duplicateValue ? (
                            <p className="muted">Same value already exists in the committed baseline.</p>
                          ) : null}
                        </>
                      ) : (
                        <div>
                          <div className="eyebrow">Proposed removal</div>
                          <strong>{formatValue(item.fact.display_value)}</strong>
                        </div>
                      )}

                      {item.fact.evidence.length ? (
                        <div>
                          <div className="eyebrow">Exact source evidence</div>
                          {item.fact.evidence.map((evidence) => (
                            <blockquote key={evidence.evidence_id}>
                              <div className="mono tiny">{evidence.logical_key}</div>
                              <div>{evidence.source_text}</div>
                            </blockquote>
                          ))}
                        </div>
                      ) : (
                        <p className="muted">No direct evidence is attached to this Fact.</p>
                      )}
                    </div>

                    <details>
                      <summary>Immutable review payload</summary>
                      <pre className="manifest">{JSON.stringify(item.payload, null, 2)}</pre>
                    </details>
                    {item.decision ? (
                      <div className="decision-summary">
                        <StatusBadge value={item.decision.decision} />
                        <span>
                          by {item.decision.actor_id} · {formatDate(item.decision.decided_at)}
                        </span>
                      </div>
                    ) : (
                      <div className="button-row">
                        <button
                          className="button button-primary"
                          disabled={pending !== null || !review.finding_review_complete}
                          onClick={() => void decideUpdate("APPROVED", item.fact_id)}
                          type="button"
                        >
                          {pending === pendingKey ? "Saving…" : "Approve change"}
                        </button>
                        <button
                          className="button button-danger-outline"
                          disabled={pending !== null || !review.finding_review_complete}
                          onClick={() => void decideUpdate("REJECTED", item.fact_id)}
                          type="button"
                        >
                          Reject change
                        </button>
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          ) : review.update_change_set.decision ? (
            <div className="decision-summary">
              <StatusBadge value={review.update_change_set.decision.decision} />
              <span>
                by {review.update_change_set.decision.actor_id} · {formatDate(review.update_change_set.decision.decided_at)}
              </span>
            </div>
          ) : (
            <div className="button-row">
              <button
                className="button button-primary"
                disabled={pending !== null || !review.finding_review_complete}
                onClick={() => void decideUpdate("APPROVED")}
                type="button"
              >
                {pending === "update" ? "Saving…" : "Approve update"}
              </button>
              <button
                className="button button-danger-outline"
                disabled={pending !== null || !review.finding_review_complete}
                onClick={() => void decideUpdate("REJECTED")}
                type="button"
              >
                Reject update
              </button>
            </div>
          )}

          <details>
            <summary>Full immutable change-set manifest</summary>
            <pre className="manifest">{JSON.stringify(review.update_change_set.manifest, null, 2)}</pre>
          </details>
        </article>
      ) : null}
    </section>
  );
}

function messageFor(error: unknown) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error instanceof Error ? error.message : "Unexpected review error";
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function formatValue(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function jsonEqual(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function displayConcept(concept: string, subject: string, predicate: string) {
  const value = concept || predicate || subject;
  return value.replace(/\b\w/g, (character) => character.toUpperCase());
}
