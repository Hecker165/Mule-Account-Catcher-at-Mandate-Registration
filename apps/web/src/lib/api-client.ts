import type { components } from "./api-types";

export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(
  /\/$/,
  ""
);

// Browsers use the same-origin /api proxy (see next.config.ts rewrites) unless
// an absolute NEXT_PUBLIC_API_BASE_URL base is configured for direct calls.
const API_PREFIX = API_BASE_URL === "" ? "/api" : "";

function apiPath(path: string): string {
  return `${API_BASE_URL}${API_PREFIX}${path}`;
}

const REQUEST_TIMEOUT_MS = 5000;

export type ApiErrorKind = "network" | "timeout" | "unavailable" | "client" | "server";

export class ApiError extends Error {
  status: number;
  kind: ApiErrorKind;

  constructor(status: number, kind: ApiErrorKind, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
  }
}

type Schemas = components["schemas"];
type RiskSession = Schemas["RiskSession"];
type RiskAssessment = Schemas["RiskAssessment"];
type RiskSessionCreateRequest = Schemas["RiskSessionCreateRequest"];
type RiskSessionTelemetryUpdateRequest = Schemas["RiskSessionTelemetryUpdateRequest"];

function errorForStatus(status: number): ApiError {
  if (status === 503) {
    return new ApiError(status, "unavailable", "Risk pre-check is unavailable.");
  }
  if (status >= 400 && status < 500) {
    return new ApiError(status, "client", `Request failed (HTTP ${status}).`);
  }
  return new ApiError(status, "server", `Risk service error (HTTP ${status}).`);
}

async function apiFetch<Output>(path: string, init?: { method?: string; body?: unknown }): Promise<{
  data: Output;
  headers: Headers;
}> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(apiPath(path), {
      method: init?.method ?? "GET",
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": crypto.randomUUID(),
      },
      body: init?.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(0, "timeout", "Request timed out.");
    }
    throw new ApiError(0, "network", "Network error.");
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) {
    throw errorForStatus(response.status);
  }
  const data = (await response.json()) as Output;
  if (data === null || typeof data !== "object") {
    throw new ApiError(response.status, "server", "Unexpected response shape.");
  }
  return { data, headers: response.headers };
}

function requireSessionId(session: RiskSession): string {
  if (!session.risk_session_id) {
    throw new ApiError(200, "server", "Unexpected response shape.");
  }
  return session.risk_session_id;
}

export async function createRiskSession(
  body: RiskSessionCreateRequest
): Promise<{ session: RiskSession; idempotentReplay: boolean }> {
  const { data, headers } = await apiFetch<RiskSession>("/v1/risk-sessions", {
    method: "POST",
    body,
  });
  requireSessionId(data);
  return { session: data, idempotentReplay: headers.get("X-Idempotent-Replay") === "true" };
}

export async function recordTelemetry(
  riskSessionId: string,
  body: RiskSessionTelemetryUpdateRequest
): Promise<RiskSession> {
  const { data } = await apiFetch<RiskSession>(
    `/v1/risk-sessions/${riskSessionId}/telemetry`,
    { method: "PATCH", body }
  );
  requireSessionId(data);
  return data;
}

export async function requestPrecheck(riskSessionId: string): Promise<RiskAssessment> {
  const { data } = await apiFetch<RiskAssessment>(
    `/v1/risk-sessions/${riskSessionId}/precheck`,
    { method: "POST" }
  );
  if (!data.assessment_id) {
    throw new ApiError(200, "server", "Unexpected response shape.");
  }
  return data;
}
