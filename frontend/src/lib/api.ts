import type {
  AdvanceRunResult,
  ApiErrorEnvelope,
  Document,
  DocumentVersion,
  Fact,
  FinalReport,
  Finding,
  IngestResult,
  Pile,
  RuleSet,
  Run,
  RunMetric,
  RunReview
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function decodeError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as Partial<ApiErrorEnvelope>;
    if (body.error?.code && body.error.message) {
      return new ApiError(response.status, body.error.code, body.error.message, body.error.details ?? {});
    }
  } catch {
    // Fall through to the status-based message below.
  }
  return new ApiError(response.status, "HTTP_ERROR", `${response.status} ${response.statusText}`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    throw await decodeError(response);
  }
  return (await response.json()) as T;
}

export const api = {
  health: async (): Promise<boolean> => {
    const response = await fetch("/health");
    if (!response.ok) return false;
    const payload = (await response.json()) as { status?: string };
    return payload.status === "ok";
  },

  createPile: (payload: {
    name: string;
    description: string | null;
    watch_enabled: boolean;
    watch_location: string | null;
  }) => request<Pile>("/api/v1/piles", { method: "POST", body: JSON.stringify(payload) }),

  getPile: (pileId: string) => request<Pile>(`/api/v1/piles/${pileId}`),
  listDocuments: (pileId: string) => request<Document[]>(`/api/v1/piles/${pileId}/documents`),
  listRuns: (pileId: string) => request<Run[]>(`/api/v1/piles/${pileId}/runs`),
  listRuleSets: (pileId: string) => request<RuleSet[]>(`/api/v1/piles/${pileId}/rule-sets`),

  createRuleSet: (
    pileId: string,
    payload: {
      name: string;
      description: string | null;
      version: number;
      rules: Array<{
        rule_key: string;
        title: string;
        description: string | null;
        severity: string;
        definition: unknown;
      }>;
    }
  ) => request<RuleSet>(`/api/v1/piles/${pileId}/rule-sets`, { method: "POST", body: JSON.stringify(payload) }),

  ingestDocument: (pileId: string, logicalKey: string, file: File) => {
    const body = new FormData();
    body.append("logical_key", logicalKey);
    body.append("file", file);
    return request<IngestResult>(`/api/v1/piles/${pileId}/documents`, { method: "POST", body });
  },

  getDocumentVersions: (documentId: string) =>
    request<DocumentVersion[]>(`/api/v1/documents/${documentId}/versions`),

  getRun: (runId: string) => request<Run>(`/api/v1/runs/${runId}`),
  getRunFacts: (runId: string) => request<Fact[]>(`/api/v1/runs/${runId}/facts`),
  getRunFindings: (runId: string) => request<Finding[]>(`/api/v1/runs/${runId}/findings`),
  getRunReview: (runId: string) => request<RunReview>(`/api/v1/runs/${runId}/review`),
  getRunMetrics: (runId: string) => request<RunMetric[]>(`/api/v1/runs/${runId}/metrics`),

  advanceRun: (runId: string, sourceRuleSetIds: string[], sourceRuleIds: string[]) =>
    request<AdvanceRunResult>(`/api/v1/runs/${runId}/advance`, {
      method: "POST",
      body: JSON.stringify({
        source_rule_set_ids: sourceRuleSetIds.length ? sourceRuleSetIds : null,
        source_rule_ids: sourceRuleIds.length ? sourceRuleIds : null
      })
    }),

  reviewFinding: (
    runId: string,
    findingId: string,
    decision: "APPROVED" | "REJECTED",
    actorId: string
  ) =>
    request(`/api/v1/runs/${runId}/findings/${findingId}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        actor_type: "HUMAN",
        actor_id: actorId,
        idempotency_key: `review:${runId}:${findingId}`
      })
    }),

  reviewUpdate: (
    runId: string,
    decision: "APPROVED" | "REJECTED",
    actorId: string,
    fingerprint: string
  ) =>
    request(`/api/v1/runs/${runId}/update/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        actor_type: "HUMAN",
        actor_id: actorId,
        idempotency_key: `update-review:${runId}:${fingerprint.replace("sha256:", "")}`,
        expected_change_set_fingerprint: fingerprint
      })
    }),

  getFinalReport: (runId: string) => request<FinalReport>(`/api/v1/runs/${runId}/report`),

  getFinalReportContent: async (runId: string): Promise<string> => {
    const response = await fetch(`/api/v1/runs/${runId}/report/content`);
    if (!response.ok) throw await decodeError(response);
    return response.text();
  }
};
