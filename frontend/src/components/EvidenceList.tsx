import type { Evidence } from "../lib/types";

export function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) {
    return <p className="muted">No evidence attached.</p>;
  }

  return (
    <div className="evidence-list">
      {evidence.map((item) => (
        <figure className="evidence-card" key={item.evidence_id}>
          <blockquote>{item.source_text}</blockquote>
          <figcaption>
            <span>Version {shortId(item.document_version_id)}</span>
            {item.page_number !== null ? <span>Page {item.page_number}</span> : null}
            {item.start_offset !== null && item.end_offset !== null ? (
              <span>
                Chars {item.start_offset}–{item.end_offset}
              </span>
            ) : null}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

function shortId(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}
