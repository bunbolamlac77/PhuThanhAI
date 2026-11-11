export interface Health {
  status: "ok" | "degraded";
  product_version: string;
  contract_version: string;
  pid: number;
  uptime_s: number;
  bound_host: string;
  loopback_only: boolean;
  heavy_slot_busy: boolean;
  model_loaded_in_process: false;
  data_root_configured: boolean;
  web_bundle_present: boolean;
  offline_enforced: boolean;
  time: string;
}

export interface SessionInfo {
  capability: string;
  header_name: string;
  product_version: string;
}

export interface ContractCheck {
  status: "pass" | "fail";
  contracts_dir_present: boolean;
  kinds: Array<{
    name: string;
    schema_valid: boolean;
    semantic_valid: boolean;
    fixtures_checked: string[];
    negative_fixtures_rejected: string[];
    error: string | null;
  }>;
  portable_roundtrip_ok: boolean;
  checksum_tamper_rejected: boolean;
}

const API = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    credentials: "same-origin",
  });
  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    try {
      const body = (await response.json()) as { code?: string };
      if (body.code) code = body.code;
    } catch {
      // Body was not JSON; keep the status code.
    }
    throw new Error(code);
  }
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function fetchSession(): Promise<SessionInfo> {
  return request<SessionInfo>("/session");
}

export function fetchContracts(): Promise<ContractCheck> {
  return request<ContractCheck>("/contracts");
}

export async function recheckContracts(session: SessionInfo): Promise<ContractCheck> {
  return request<ContractCheck>("/contracts:recheck", {
    method: "POST",
    headers: { [session.header_name]: session.capability },
  });
}
