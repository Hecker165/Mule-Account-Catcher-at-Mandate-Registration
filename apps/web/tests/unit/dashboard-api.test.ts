import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DashboardApiError,
  fetchAssessments,
  fetchAuditEvents,
  maskVpa,
  truncateId,
  verifyAuditChain,
} from "../../src/lib/dashboard-api";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("dashboard-api", () => {
  it("test_fetch_assessments_calls_the_dashboard_endpoint_with_query_params", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        seen.push(url);
        return jsonResponse({ items: [], limit: 50, offset: 0 });
      })
    );
    const data = await fetchAssessments({ limit: 50, offset: 5, decision: "BLOCK" });
    expect(data.items).toEqual([]);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toContain("/api/v1/dashboard/assessments");
    expect(seen[0]).toContain("limit=50");
    expect(seen[0]).toContain("offset=5");
    expect(seen[0]).toContain("decision=BLOCK");
  });

  it("test_mask_vpa_renders_bullet_handle_format", () => {
    expect(maskVpa("upi")).toBe("•••@upi");
    expect(maskVpa("okhdfc")).toBe("•••@okhdfc");
  });

  it("test_mask_vpa_passes_through_null", () => {
    expect(maskVpa(null)).toBeNull();
  });

  it("test_truncate_id_shows_first_eight_chars", () => {
    expect(truncateId("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")).toBe("a1b2c3d4…");
  });

  it("test_verify_audit_chain_maps_the_verification_result", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ valid: true, checked_events: 3, first_invalid_sequence: null, reason: null })
      )
    );
    const result = await verifyAuditChain("risk_assessment", "some-id");
    expect(result.valid).toBe(true);
    expect(result.checked_events).toBe(3);
  });

  it("test_audit_events_error_kinds_match_api_error_taxonomy", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("missing", { status: 404 })));
    const error = await fetchAuditEvents("risk_assessment", "some-id").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(DashboardApiError);
    expect((error as DashboardApiError).kind).toBe("client");
  });

  it("test_every_request_sends_uuid_x_request_id_and_no_store", async () => {
    const seen: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        seen.push(init);
        return jsonResponse({ items: [], limit: 50, offset: 0 });
      })
    );
    await fetchAssessments({ limit: 10 });
    await verifyAuditChain("risk_assessment", "some-id");
    expect(seen).toHaveLength(2);
    const ids = new Set<string>();
    for (const init of seen) {
      const requestId = new Headers(init.headers).get("X-Request-ID") ?? "";
      expect(requestId).toMatch(UUID_PATTERN);
      ids.add(requestId);
      expect(init.cache).toBe("no-store");
    }
    expect(ids.size).toBe(2);
  });
});
