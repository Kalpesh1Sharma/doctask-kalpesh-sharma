export type ApiErrorEnvelope = {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
};

export type Pile = {
  pile_id: string;
  name: string;
  description: string | null;
  watch_enabled: boolean;
  watch_location: string | null;
  latest_run_id: string | null;
  created_at: string;
  updated_at: string;
};

export type Rule = {
  rule_id: string;
  rule_set_id: string;
  rule_key: string;
  title: string;
  description: string | null;
  severity: string;
  definition: unknown;
  created_at: string;
};

export type RuleSet = {
  rule_set_id: string;
  pile_id: string;
  name: string;
  description: string | null;
  version: number;
  created_at: string;
  rules: Rule[];
};

export type Document = {
  document_id: string;
  pile_id: string;
  logical_key: string;
  current_version_id: string | null;
  created_at: string;
  updated_at: string;
};

export type DocumentVersion = {
  document_version_id: string;
  document_id: string;
  version_number: number;
  content_hash: string;
  storage_uri: string;
  mime_type: string | null;
  size_bytes: number | null;
  processing_status: string;
  created_at: string;
};

export type IngestResult = {
  document_id: string;
  document_version_id: string;
  content_hash: string;
  storage_uri: string;
  replayed: boolean;
  created_run_ids: string[];
  reconciled_blocked_run_id: string | null;
};

export type RunStep = {
  step_id: string;
  stage: "INGEST" | "UNDERSTAND" | "EXAMINE" | "REVIEW" | "COMMIT";
  status: string;
  attempt: number;
  started_at: string | null;
  completed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  retry_operation_scope: string | null;
  retry_operation_key: string | null;
  retry_from_claim_generation: number | null;
  created_at: string;
};

export type Run = {
  run_id: string;
  pile_id: string;
  run_type: "INITIAL" | "UPDATE" | "MANUAL";
  status: string;
  trigger_document_version_id: string | null;
  parent_run_id: string | null;
  baseline_run_id: string | null;
  blocked_on_run_id: string | null;
  commit_required: boolean;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  steps: RunStep[];
};

export type AdvanceRunResult = {
  run_id: string;
  stage: RunStep["stage"];
  attempt: number;
  step_status: string;
  run_status: string;
  outcome: string;
  advanced: boolean;
  successor_attempt: number | null;
};

export type Evidence = {
  evidence_id: string;
  document_version_id: string;
  chunk_id: string;
  page_number: number | null;
  start_offset: number | null;
  end_offset: number | null;
  source_text: string;
  content_hash: string;
};

export type Fact = {
  run_id: string;
  fact_id: string;
  relation: string;
  origin_run_id: string;
  subject: string;
  predicate: string;
  object_value: unknown;
  status: string;
  evidence: Evidence[];
};

export type Finding = {
  run_id: string;
  finding_id: string;
  relation: string;
  origin_run_id: string;
  finding_type: string;
  rule_id: string | null;
  title: string;
  description: string | null;
  severity: string;
  status: string;
  evidence: Evidence[];
};

export type ReviewDecision = {
  review_decision_id: string;
  finding_id: string;
  decision: "APPROVED" | "REJECTED";
  actor_type: string;
  actor_id: string;
  idempotency_key: string;
  decided_at: string;
};

export type ReviewFinding = Finding & {
  decision: ReviewDecision | null;
};

export type UpdateReviewDecision = {
  update_review_decision_id: string;
  change_set_id: string;
  decision: "APPROVED" | "REJECTED";
  actor_type: string;
  actor_id: string;
  idempotency_key: string;
  decided_at: string;
};

export type UpdateItemReviewDecision = {
  update_item_review_decision_id: string;
  item_id: string;
  decision: "APPROVED" | "REJECTED";
  actor_type: string;
  actor_id: string;
  idempotency_key: string;
  decided_at: string;
};

export type UpdateFactEvidence = Evidence & {
  logical_key: string;
};

export type UpdateFactSnapshot = {
  fact_id: string;
  subject: string;
  predicate: string;
  object_value: unknown;
  display_value: unknown;
  status: "CONFIRMED" | "DISPUTED" | "SUPERSEDED";
  concept: string;
  evidence: UpdateFactEvidence[];
};

export type UpdateChangeItem = {
  item_id: string | null;
  fact_id: string;
  kind: "FACT_CREATED" | "FACT_REMOVED";
  payload: unknown;
  fact: UpdateFactSnapshot;
  baseline_facts: UpdateFactSnapshot[];
  decision: UpdateItemReviewDecision | null;
};

export type UpdateChangeSet = {
  change_set_id: string | null;
  durable: boolean;
  change_set_fingerprint: string;
  manifest: unknown;
  items: UpdateChangeItem[];
  decision: UpdateReviewDecision | null;
};


export type RunReviewAcknowledgement = {
  audit_event_id: string;
  actor_type: string;
  actor_id: string;
  idempotency_key: string;
  acknowledged_at: string;
};

export type RunReview = {
  run_id: string;
  run_type: Run["run_type"];
  run_status: string;
  findings: ReviewFinding[];
  finding_review_complete: boolean;
  update_change_set: UpdateChangeSet | null;
  run_review_acknowledgement: RunReviewAcknowledgement | null;
  review_complete: boolean;
  commit_ready: boolean;
};

export type RunMetric = {
  metric_id: string;
  run_id: string;
  stage: RunStep["stage"];
  attempt: number;
  duration_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: string | number | null;
  created_at: string;
};

export type FinalReport = {
  output_id: string;
  run_id: string;
  output_type: string;
  storage_uri: string;
  content_hash: string;
  created_at: string;
  render_input_fingerprint: string;
  focused_update: unknown | null;
};
