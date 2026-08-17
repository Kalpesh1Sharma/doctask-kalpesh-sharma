import { FormEvent, useEffect, useState } from "react";

import { ApiError, api } from "../lib/api";
import type { Document, DocumentVersion, IngestResult } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

export function DocumentsPanel({
  pileId,
  documents,
  onChanged,
  onRunCreated
}: {
  pileId: string;
  documents: Document[];
  onChanged: () => Promise<void>;
  onRunCreated: (runId: string) => void;
}) {
  const [logicalKey, setLogicalKey] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [versions, setVersions] = useState<Record<string, DocumentVersion[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastIngest, setLastIngest] = useState<IngestResult | null>(null);

  useEffect(() => {
    if (!expanded || versions[expanded]) return;
    void api.getDocumentVersions(expanded)
      .then((items) => setVersions((current) => ({ ...current, [expanded]: items })))
      .catch((caught) => setError(messageFor(caught)));
  }, [expanded, versions]);

  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file || !logicalKey.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.ingestDocument(pileId, logicalKey.trim(), file);
      setLastIngest(result);
      setFile(null);
      await onChanged();
      if (result.created_run_ids.length) onRunCreated(result.created_run_ids[0]);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stack-lg">
      <form className="panel upload-panel" onSubmit={upload}>
        <div className="section-heading compact">
          <div>
            <span className="eyebrow">Source arrival</span>
            <h2>Upload a document</h2>
            <p className="muted">PDF, DOCX, or TXT. Re-uploading identical READY bytes exact-replays without reprocessing.</p>
          </div>
        </div>
        <div className="upload-grid">
          <label>
            Logical key
            <input value={logicalKey} onChange={(event) => setLogicalKey(event.target.value)} placeholder="master-contract" required />
          </label>
          <label>
            Source file
            <input accept=".pdf,.docx,.txt,text/plain,application/pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} type="file" required />
          </label>
          <button className="button button-primary" disabled={busy || !file} type="submit">{busy ? "Ingesting…" : "Ingest document"}</button>
        </div>
        {error ? <div className="alert alert-error">{error}</div> : null}
        {lastIngest ? (
          <div className="alert alert-success">
            {lastIngest.replayed ? "Exact READY replay." : "Document ingested."}
            {lastIngest.created_run_ids.length ? ` Created run ${lastIngest.created_run_ids[0]}.` : ""}
          </div>
        ) : null}
      </form>

      <section className="panel">
        <div className="section-heading compact">
          <div>
            <span className="eyebrow">Immutable history</span>
            <h2>Documents</h2>
          </div>
          <span className="count-pill">{documents.length}</span>
        </div>
        {!documents.length ? <div className="empty-state">No documents have been ingested yet.</div> : (
          <div className="data-list">
            {documents.map((document) => (
              <div className="data-row document-row" key={document.document_id}>
                <button className="row-main" onClick={() => setExpanded(expanded === document.document_id ? null : document.document_id)} type="button">
                  <div>
                    <strong>{document.logical_key}</strong>
                    <span className="mono tiny">{document.document_id}</span>
                  </div>
                  <span className="muted">{document.current_version_id ? `current ${shortId(document.current_version_id)}` : "no current version"}</span>
                </button>
                {expanded === document.document_id ? (
                  <div className="version-list">
                    {(versions[document.document_id] ?? []).map((version) => (
                      <div className="version-row" key={version.document_version_id}>
                        <div>
                          <strong>Version {version.version_number}</strong>
                          <span className="mono tiny">{version.document_version_id}</span>
                        </div>
                        <StatusBadge value={version.processing_status} />
                        <span>{version.mime_type ?? "unknown type"}</span>
                        <span>{formatBytes(version.size_bytes)}</span>
                      </div>
                    ))}
                    {!versions[document.document_id] ? <p className="muted">Loading versions…</p> : null}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}

function shortId(value: string) { return `${value.slice(0, 8)}…${value.slice(-4)}`; }
function formatBytes(value: number | null) {
  if (value === null) return "size unknown";
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}
function messageFor(error: unknown) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error instanceof Error ? error.message : "Unexpected ingestion error";
}
