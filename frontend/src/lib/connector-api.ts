export const CONNECTOR_API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_URL || ''
).replace(/\/$/, '');

export type PairingStatus = 'pending' | 'approved' | 'consumed' | 'expired' | string;
export type DeviceStatus = 'online' | 'offline' | 'degraded' | string;
export type RuntimeKind = 'claude_code' | 'codex' | 'claude' | string;
export type BindingStatus =
  | 'available'
  | 'starting'
  | 'running'
  | 'degraded'
  | 'stopped'
  | string;

export type ConnectorCommandAction =
  | 'runtime.probe'
  | 'session.start'
  | 'session.resume'
  | 'task.dispatch'
  | 'task.cancel'
  | 'session.stop';

export interface Pairing {
  pairing_id: string;
  user_code: string;
  device_code?: string;
  verification_uri?: string;
  expires_at: string;
  status: PairingStatus;
}

export interface ConnectorRuntime {
  runtime_id: string;
  kind: RuntimeKind;
  display_name: string;
  version?: string;
  executable_path: string;
  available: boolean;
  capabilities: string[];
  auth_modes: string[];
  detected_at?: string;
}

export interface ConnectorDevice {
  device_id: string;
  name: string;
  status: DeviceStatus;
  last_seen_at?: string;
  platform?: string;
  connector_version?: string;
  runtimes: ConnectorRuntime[];
}

export interface AgentBinding {
  binding_id: string;
  device_id: string;
  runtime_id: string;
  runtime_kind?: string;
  agent_id?: string;
  display_name: string;
  status: BindingStatus;
  binding_epoch?: number;
  last_session_id?: string;
  last_task_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ConnectorCommand {
  command_id: string;
  binding_id: string;
  action: ConnectorCommandAction;
  status: string;
  created_at?: string;
}

export interface RuntimeEvent {
  event_id?: string;
  binding_id?: string;
  session_id?: string;
  event_type?: string;
  type?: string;
  level?: string;
  data?: Record<string, unknown>;
  sequence?: number;
  received_at?: string;
  occurred_at?: string;
}

interface ListEnvelope<T> {
  devices?: T[];
  bindings?: T[];
  events?: T[];
  items?: T[];
}

interface ApiErrorBody {
  detail?: string;
  message?: string;
}

export class ConnectorApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = 'ConnectorApiError';
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

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || 'GET').toUpperCase();
  const headers = new Headers(init?.headers);
  headers.set('Accept', 'application/json');
  if (init?.body) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && !headers.has('X-CSRF-Token')) {
    const csrfToken = readCookie('adx_csrf');
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken);
  }

  const response = await fetch(`${CONNECTOR_API_BASE_URL}${path}`, {
    ...init,
    credentials: 'include',
    headers,
  });

  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // The HTTP status still gives the user a useful next step.
    }

    throw new ConnectorApiError(
      body.detail || body.message || `Connector API returned ${response.status}.`,
      response.status,
    );
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function unwrapList<T>(value: T[] | ListEnvelope<T>, key: keyof ListEnvelope<T>): T[] {
  if (Array.isArray(value)) return value;
  return value[key] || value.items || [];
}

function normalizeRuntime(runtime: ConnectorRuntime): ConnectorRuntime {
  return {
    ...runtime,
    display_name: runtime.display_name || runtime.kind || 'Agent runtime',
    executable_path: runtime.executable_path || '',
    available: runtime.available !== false,
    capabilities: runtime.capabilities || [],
    auth_modes: runtime.auth_modes || [],
  };
}

function normalizeDevice(device: ConnectorDevice): ConnectorDevice {
  const rawDevice = device as ConnectorDevice & {
    hostname?: string;
    inventory?: { runtimes?: ConnectorRuntime[] };
  };

  return {
    ...device,
    name: device.name || rawDevice.hostname || 'Local computer',
    status: device.status || 'offline',
    runtimes: (device.runtimes || rawDevice.inventory?.runtimes || []).map(normalizeRuntime),
  };
}

export async function createPairing(input?: {
  owner_id?: string;
  device_name?: string;
}): Promise<Pairing> {
  return apiRequest<Pairing>('/api/connectors/pairings', {
    method: 'POST',
    body: JSON.stringify(input || {}),
  });
}

export async function approvePairing(userCode: string, ownerId?: string): Promise<Pairing> {
  return apiRequest<Pairing>(`/api/connectors/pairings/${encodeURIComponent(userCode)}/approve`, {
    method: 'POST',
    body: JSON.stringify(ownerId ? { owner_id: ownerId } : {}),
  });
}

export interface ConnectorAuthUser {
  user_id: string;
  username: string;
  temporary: boolean;
}

export interface ConnectorAuthSession {
  user: ConnectorAuthUser;
  csrf_token: string;
}

export async function getConnectorAuthSession(): Promise<ConnectorAuthSession | null> {
  try {
    return await apiRequest<ConnectorAuthSession>('/api/auth/session');
  } catch (error) {
    if (error instanceof ConnectorApiError && error.status === 401) return null;
    throw error;
  }
}

export async function acceptConnectorInvite(input: {
  invite_code: string;
  username: string;
  password: string;
}): Promise<ConnectorAuthSession> {
  return apiRequest<ConnectorAuthSession>('/api/auth/invite', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function loginConnectorUser(input: {
  username: string;
  password: string;
}): Promise<ConnectorAuthSession> {
  return apiRequest<ConnectorAuthSession>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function approvePairingAuthenticated(
  userCode: string,
  csrfToken: string,
): Promise<Pairing> {
  return apiRequest<Pairing>(
    `/api/connectors/pairings/${encodeURIComponent(userCode)}/approve`,
    {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({}),
    },
  );
}

export async function logoutConnectorUser(csrfToken: string): Promise<void> {
  await apiRequest<void>('/api/auth/logout', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  });
}

export async function listConnectorDevices(): Promise<ConnectorDevice[]> {
  const response = await apiRequest<ConnectorDevice[] | ListEnvelope<ConnectorDevice>>(
    '/api/connectors/devices',
  );
  return unwrapList(response, 'devices').map(normalizeDevice);
}

export async function getConnectorDevice(deviceId: string): Promise<ConnectorDevice> {
  return normalizeDevice(
    await apiRequest<ConnectorDevice>(
      `/api/connectors/devices/${encodeURIComponent(deviceId)}`,
    ),
  );
}

export async function revokeConnectorDevice(
  deviceId: string,
  ownerId = 'demo-user',
): Promise<ConnectorDevice> {
  return normalizeDevice(
    await apiRequest<ConnectorDevice>(
      `/api/connectors/devices/${encodeURIComponent(deviceId)}/revoke`,
      {
        method: 'POST',
        body: JSON.stringify({ owner_id: ownerId }),
      },
    ),
  );
}

export async function createBinding(
  deviceId: string,
  input: { runtime_id: string; agent_id?: string; display_name?: string },
): Promise<AgentBinding> {
  return apiRequest<AgentBinding>(
    `/api/connectors/devices/${encodeURIComponent(deviceId)}/bindings`,
    {
      method: 'POST',
      body: JSON.stringify(input),
    },
  );
}

export async function listBindings(): Promise<AgentBinding[]> {
  const response = await apiRequest<AgentBinding[] | ListEnvelope<AgentBinding>>(
    '/api/connectors/bindings',
  );
  return unwrapList(response, 'bindings');
}

export async function sendBindingCommand(
  bindingId: string,
  input: {
    action: ConnectorCommandAction;
    payload?: Record<string, unknown>;
    idempotency_key?: string;
  },
): Promise<ConnectorCommand> {
  return apiRequest<ConnectorCommand>(
    `/api/connectors/bindings/${encodeURIComponent(bindingId)}/commands`,
    {
      method: 'POST',
      body: JSON.stringify(input),
    },
  );
}

export async function listBindingEvents(bindingId: string): Promise<RuntimeEvent[]> {
  const response = await apiRequest<RuntimeEvent[] | ListEnvelope<RuntimeEvent>>(
    `/api/connectors/bindings/${encodeURIComponent(bindingId)}/events`,
  );
  return unwrapList(response, 'events');
}
