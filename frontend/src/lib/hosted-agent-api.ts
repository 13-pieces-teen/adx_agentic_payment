import { CONNECTOR_API_BASE_URL } from '@/lib/connector-api';

export type HostedAgentStatus =
  | 'provisioning'
  | 'ready'
  | 'degraded'
  | 'disabled';

export type ThinkingMode = 'unsupported' | 'optional' | 'always_on';

export interface HostedModelCapability {
  providerId: string;
  modelId: string;
  displayName: string;
  supportsStructuredOutput: boolean;
  thinkingMode: ThinkingMode;
  thinkingCanToggle: boolean;
  effectiveThinkingDefault: boolean;
  maxOutputTokens: number;
  requestTimeoutCapMs: number;
  schemaVersion: string;
}

export interface HostedAgentCapabilities {
  creationEnabled: boolean;
  reasonCodes: string[];
  registryVersion: string;
  models: HostedModelCapability[];
  schemaVersion: string;
}

export interface HostedAgentSummary {
  agentId: string;
  displayName: string;
  providerId: string;
  modelId: string;
  thinkingEnabled: boolean;
  provisioningStatus: HostedAgentStatus;
  routeStatus: HostedAgentStatus;
  createdAt: string;
  updatedAt: string;
  schemaVersion: string;
}

export interface CredentialMetadata {
  credentialId: string;
  providerId: string;
  status:
    | 'pending_write'
    | 'stored'
    | 'pending_validation'
    | 'valid'
    | 'invalid'
    | 'revoking'
    | 'revoked';
  fingerprintHint: string;
  createdAt: string;
  updatedAt: string;
  schemaVersion: string;
}

interface HostedAgentListResponse {
  agents: HostedAgentSummary[];
  total: number;
}

const SAFE_ERROR_CODES = new Set([
  'application_json_required',
  'invalid_request',
  'idempotency_key_required',
  'idempotency_conflict',
  'credential_not_found',
  'credential_not_usable',
  'provider_mismatch',
  'credential_ingress_unavailable',
  'credential_write_recovery_required',
  'hosted_agents_disabled',
  'non_durable_repository_forbidden',
  'repository_unavailable',
  'secret_store_unavailable',
  'agent_not_found',
]);

export type HostedAgentApiErrorCode =
  | 'authentication_required'
  | 'csrf_required'
  | 'request_failed'
  | 'network_unavailable'
  | string;

export class HostedAgentApiError extends Error {
  constructor(
    public readonly code: HostedAgentApiErrorCode,
    public readonly status: number,
  ) {
    super(code);
    this.name = 'HostedAgentApiError';
  }
}

function readCookie(name: string): string {
  if (typeof document === 'undefined') return '';
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie
    .split(';')
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : '';
}

function safeErrorCode(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null;
  const detail = Reflect.get(body, 'detail');
  if (!detail || typeof detail !== 'object') return null;
  const code = Reflect.get(detail, 'code');
  return typeof code === 'string' && SAFE_ERROR_CODES.has(code) ? code : null;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  options?: { csrf?: boolean },
): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('Accept', 'application/json');
  if (init?.body) headers.set('Content-Type', 'application/json');

  if (options?.csrf) {
    const csrfToken = readCookie('adx_csrf');
    if (!csrfToken) {
      throw new HostedAgentApiError('csrf_required', 403);
    }
    headers.set('X-CSRF-Token', csrfToken);
  }

  let response: Response;
  try {
    response = await fetch(`${CONNECTOR_API_BASE_URL}${path}`, {
      ...init,
      credentials: 'include',
      headers,
    });
  } catch {
    throw new HostedAgentApiError('network_unavailable', 0);
  }

  if (!response.ok) {
    let code: string | null = null;
    try {
      // Only the allowlisted machine code is retained. Response text and
      // arbitrary detail fields are never surfaced to the UI.
      code = safeErrorCode(await response.json());
    } catch {
      // The HTTP status is enough to choose a safe local message.
    }

    if (response.status === 401) code = 'authentication_required';
    if (response.status === 403 && !code) code = 'csrf_required';
    throw new HostedAgentApiError(code || 'request_failed', response.status);
  }

  return response.json() as Promise<T>;
}

export function createHostedIdempotencyKey(
  operation: 'credential' | 'agent',
): string {
  const suffix =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `hosted-${operation}-${suffix}`.slice(0, 128);
}

export async function getHostedAgentCapabilities(): Promise<HostedAgentCapabilities> {
  return request<HostedAgentCapabilities>(
    '/api/hosted-agents/capabilities',
  );
}

export async function getHostedAgents(): Promise<HostedAgentSummary[]> {
  const response = await request<HostedAgentListResponse>(
    '/api/hosted-agents?scope=mine',
  );
  return response.agents || [];
}

export async function createModelCredential(
  input: {
    providerId: string;
    apiKey: string;
  },
  idempotencyKey: string,
): Promise<CredentialMetadata> {
  return request<CredentialMetadata>(
    '/api/model-credentials',
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(input),
    },
    { csrf: true },
  );
}

export async function createHostedAgent(
  input: {
    displayName: string;
    credentialId: string;
    providerId: string;
    modelId: string;
    thinkingEnabled: boolean;
    strategyInstructions: string;
  },
  idempotencyKey: string,
): Promise<HostedAgentSummary> {
  return request<HostedAgentSummary>(
    '/api/hosted-agents',
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(input),
    },
    { csrf: true },
  );
}
