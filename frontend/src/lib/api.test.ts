import { afterEach, expect, test, vi } from "vitest";

import { ApiError, api } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

test("sends finding review with a backend-valid stable idempotency key", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } })
  );
  const runId = "22222222-2222-4222-8222-222222222222";
  const findingId = "33333333-3333-4333-8333-333333333333";

  await api.reviewFinding(runId, findingId, "APPROVED", "alice@example.test");

  const [, init] = fetchMock.mock.calls[0];
  const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
  expect(payload.idempotency_key).toBe(`review:${runId}:${findingId}`);
  expect(payload.actor_type).toBe("HUMAN");
  expect(payload.actor_id).toBe("alice@example.test");
});

test("sends UPDATE approval with the exact fingerprint and a stable valid idempotency key", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } })
  );
  const runId = "22222222-2222-4222-8222-222222222222";
  const fingerprint = `sha256:${"b".repeat(64)}`;
  await api.reviewUpdate(runId, "APPROVED", "alice@example.test", fingerprint);

  const [, init] = fetchMock.mock.calls[0];
  const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
  expect(payload.expected_change_set_fingerprint).toBe(fingerprint);
  expect(payload.idempotency_key).toBe(`update-review:${runId}:${"b".repeat(64)}`);
  expect(payload.actor_type).toBe("HUMAN");
  expect(payload.actor_id).toBe("alice@example.test");
});


test("sends item-level UPDATE review with the Fact identity in the idempotency key", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } })
  );
  const runId = "22222222-2222-4222-8222-222222222222";
  const factId = "33333333-3333-4333-8333-333333333333";
  const fingerprint = `sha256:${"c".repeat(64)}`;

  await api.reviewUpdate(runId, "REJECTED", "alice@example.test", fingerprint, factId);

  const [, init] = fetchMock.mock.calls[0];
  const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
  expect(payload.fact_id).toBe(factId);
  expect(payload.expected_change_set_fingerprint).toBe(fingerprint);
  expect(payload.idempotency_key).toBe(
    `update-item-review:${runId}:${factId}:${"c".repeat(64)}`
  );
});

test("preserves the stable API error code and details", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({ error: { code: "HUMAN_REVIEW_REQUIRED", message: "review first", details: { run_id: "run-1" } } }),
      { status: 409, headers: { "Content-Type": "application/json" } }
    )
  );

  try {
    await api.getRun("run-1");
    throw new Error("expected request to fail");
  } catch (caught) {
    expect(caught).toBeInstanceOf(ApiError);
    const error = caught as ApiError;
    expect(error.status).toBe(409);
    expect(error.code).toBe("HUMAN_REVIEW_REQUIRED");
    expect(error.details).toEqual({ run_id: "run-1" });
  }
});
