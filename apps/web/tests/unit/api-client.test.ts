import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createRiskSession,
  recordTelemetry,
  requestPrecheck,
} from "../../src/lib/api-client";

const SESSION_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function jsonResponse(data: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const CREATE_BODY = {
  merchant_namespace: "demo_merchant",
  checkout_order_ref: "demo-3f2a9c1b",
  customer_reference: "cust-001",
  device_fingerprint: "fp-001",
  flow_started_at: "2026-01-15T10:00:00.000Z",
  mandate_intent: { max_amount_paise: 100000, frequency: "monthly", expire_at: null },
};

const SESSION = { risk_session_id: SESSION_ID, status: "CREATED" };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("api-client", () => {
  it("test_create_risk_session_posts_expected_body_and_headers", async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url, init });
        return jsonResponse(SESSION, 201);
      })
    );
    const result = await createRiskSession(CREATE_BODY);
    expect(result.session.risk_session_id).toBe(SESSION_ID);
    expect(result.idempotentReplay).toBe(false);
    expect(seen).toHaveLength(1);
    expect(seen[0].url).toBe("/api/v1/risk-sessions");
    expect(seen[0].init.method).toBe("POST");
    expect(JSON.parse(seen[0].init.body as string)).toEqual(CREATE_BODY);
  });

  it("test_create_risk_session_detects_idempotent_replay_header", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(SESSION, 200, { "X-Idempotent-Replay": "true" }))
    );
    const result = await createRiskSession(CREATE_BODY);
    expect(result.idempotentReplay).toBe(true);
  });

  it("test_record_telemetry_patches_the_telemetry_endpoint", async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url, init });
        return jsonResponse({ ...SESSION, status: "READY" }, 200);
      })
    );
    const body = { device_fingerprint: "fp-001", flow_completed_at: "2026-01-15T10:05:00.000Z" };
    const session = await recordTelemetry(SESSION_ID, body);
    expect(seen[0].url).toBe(`/api/v1/risk-sessions/${SESSION_ID}/telemetry`);
    expect(seen[0].init.method).toBe("PATCH");
    expect(JSON.parse(seen[0].init.body as string)).toEqual(body);
    expect(session.status).toBe("READY");
  });

  it("test_request_precheck_posts_with_no_body", async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url, init });
        return jsonResponse({ assessment_id: SESSION_ID, decision: "ALLOW" }, 200);
      })
    );
    await requestPrecheck(SESSION_ID);
    expect(seen[0].url).toBe(`/api/v1/risk-sessions/${SESSION_ID}/precheck`);
    expect(seen[0].init.method).toBe("POST");
    expect(seen[0].init.body).toBeUndefined();
  });

  it("test_every_request_sends_a_uuid_x_request_id_and_no_store", async () => {
    const seen: RequestInit[] = [];
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        seen.push(init);
        calls += 1;
        return calls === 1
          ? jsonResponse(SESSION, 201)
          : jsonResponse({ assessment_id: SESSION_ID }, 200);
      })
    );
    await createRiskSession(CREATE_BODY);
    await requestPrecheck(SESSION_ID);
    expect(seen).toHaveLength(2);
    const ids = new Set<string>();
    for (const init of seen) {
      const headers = new Headers(init.headers);
      const requestId = headers.get("X-Request-ID") ?? "";
      expect(requestId).toMatch(UUID_PATTERN);
      ids.add(requestId);
      expect(init.cache).toBe("no-store");
    }
    expect(ids.size).toBe(2);
  });

  it("test_timeout_maps_to_timeout_error_kind", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => {
              reject(new DOMException("aborted", "AbortError"));
            });
          })
      )
    );
    const pending = createRiskSession(CREATE_BODY);
    pending.catch(() => {});
    await vi.advanceTimersByTimeAsync(5000);
    await expect(pending).rejects.toMatchObject({ kind: "timeout" });
  });

  it("test_503_maps_to_unavailable_error_kind", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("unavailable", { status: 503 })));
    await expect(createRiskSession(CREATE_BODY)).rejects.toMatchObject({
      kind: "unavailable",
      status: 503,
    });
  });

  it("test_404_maps_to_client_error_kind", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("missing", { status: 404 })));
    const error = await requestPrecheck(SESSION_ID).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).kind).toBe("client");
  });

  it("test_500_maps_to_server_error_kind", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("boom", { status: 500 })));
    await expect(createRiskSession(CREATE_BODY)).rejects.toMatchObject({ kind: "server" });
  });

  it("test_fetch_rejection_maps_to_network_error_kind", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("failed to fetch");
      })
    );
    await expect(createRiskSession(CREATE_BODY)).rejects.toMatchObject({ kind: "network" });
  });

  it("test_error_message_never_contains_response_body", async () => {
    const marker = "secret-upstream-detail-xyz";
    vi.stubGlobal("fetch", vi.fn(async () => new Response(marker, { status: 400 })));
    const error = await createRiskSession(CREATE_BODY).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as Error).message).not.toContain(marker);
  });
});
