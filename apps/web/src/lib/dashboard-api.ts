import type { components } from "./api-types";

// A7's API_BASE_URL pattern, copied (not imported) per the A8 specification.
const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/$/, "");
const API_PREFIX = API_BASE_URL === "" ? "/api" : "";

function apiPath(path: string): string {
  return `${API_BASE_URL}${API_PREFIX}${path}`;
}

const REQUEST_TIMEOUT_MS = 5000;

export type DashboardErrorKind = "network" | "timeout" | "unavailable" | "client" | "server";

export class DashboardApiError extends Error {
  status: number;
  kind: DashboardErrorKind;

  constructor(status: number, kind: DashboardErrorKind, message: string) {
    super(message);
    this.name = "DashboardApiError";
    this.status = status;
    this.kind = kind;
  }
}

type Schemas = components["schemas"];
export type DashboardAssessmentsResponse = Schemas["DashboardAssessmentsResponse"];
export type DashboardAssessmentItem = Schemas["DashboardAssessmentItem"];
export type DashboardAuditItem = Schemas["DashboardAuditItem"];
export type AuditVerificationResponse = Schemas["AuditVerificationResponse"];

export function maskVpa(handle: string | null | undefined): string | null {
  if (handle === null || handle === undefined) {
    return null;
  }
  return `•••@${handle}`;
}

export function truncateId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

function errorForStatus(status: number): DashboardApiError {
  if (status === 503) {
    return new DashboardApiError(status, "unavailable", "Dashboard service is unavailable.");
  }
  if (status >= 400 && status < 500) {
    return new DashboardApiError(status, "client", `Request failed (HTTP ${status}).`);
  }
  return new DashboardApiError(status, "server", `Dashboard service error (HTTP ${status}).`);
}

async function dashboardFetch<Output>(path: string): Promise<Output> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(apiPath(path), {
      headers: { "X-Request-ID": crypto.randomUUID() },
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new DashboardApiError(0, "timeout", "Request timed out.");
    }
    throw new DashboardApiError(0, "network", "Network error.");
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) {
    throw errorForStatus(response.status);
  }
  const data = (await response.json()) as Output;
  if (data === null || typeof data !== "object") {
    throw new DashboardApiError(response.status, "server", "Unexpected response shape.");
  }
  return data;
}

export async function fetchAssessments(params?: {
  limit?: number;
  offset?: number;
  decision?: string;
}): Promise<DashboardAssessmentsResponse> {
  const query = new URLSearchParams();
  if (params?.limit !== undefined) {
    query.set("limit", String(params.limit));
  }
  if (params?.offset !== undefined) {
    query.set("offset", String(params.offset));
  }
  if (params?.decision !== undefined) {
    query.set("decision", params.decision);
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return dashboardFetch<DashboardAssessmentsResponse>(`/v1/dashboard/assessments${suffix}`);
}

export async function fetchAuditEvents(
  aggregateType: string,
  aggregateId: string
): Promise<DashboardAuditItem[]> {
  const query = new URLSearchParams({
    aggregate_type: aggregateType,
    aggregate_id: aggregateId,
  });
  return dashboardFetch<DashboardAuditItem[]>(`/v1/dashboard/audit-events?${query.toString()}`);
}

export async function verifyAuditChain(
  aggregateType: string,
  aggregateId: string
): Promise<AuditVerificationResponse> {
  const query = new URLSearchParams({
    aggregate_type: aggregateType,
    aggregate_id: aggregateId,
  });
  return dashboardFetch<AuditVerificationResponse>(
    `/v1/dashboard/audit-events/verify?${query.toString()}`
  );
}
