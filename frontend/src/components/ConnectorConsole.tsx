'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ArrowRight,
  Bot,
  Check,
  ChevronDown,
  ChevronUp,
  CircleStop,
  Clock3,
  CloudCog,
  Copy,
  KeyRound,
  Laptop,
  Link2,
  Loader2,
  Pause,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldCheck,
  TerminalSquare,
  Wifi,
  WifiOff,
  X,
} from 'lucide-react';
import {
  AgentBinding,
  ConnectorCommandAction,
  CONNECTOR_API_BASE_URL,
  ConnectorDevice,
  ConnectorRuntime,
  Pairing,
  RuntimeEvent,
  approvePairing,
  createBinding,
  createPairing,
  listBindingEvents,
  listBindings,
  listConnectorDevices,
  revokeConnectorDevice,
  sendBindingCommand,
} from '@/lib/connector-api';

const REFRESH_INTERVAL_MS = 8_000;
const CONNECTOR_DEMO_ENABLED =
  process.env.NEXT_PUBLIC_CONNECTOR_DEMO === 'true';
// Replace with the authenticated platform user once the existing login flow exposes it.
const CURRENT_OWNER_ID = 'demo-user';

function formatTimestamp(value?: string): string {
  if (!value) return 'Not reported';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function commandIdempotencyKey(bindingId: string, action: ConnectorCommandAction): string {
  const suffix =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${bindingId}:${action}:${suffix}`.slice(0, 128);
}

function runtimeLabel(kind: string): string {
  if (kind === 'claude_code') return 'Claude Code';
  if (kind === 'codex') return 'Codex';
  return kind.replaceAll('_', ' ');
}

function executableLabel(value: string): string {
  if (!value) return 'Executable path not reported';
  const basename = value.split(/[\\/]/).filter(Boolean).pop();
  return basename ? `…/${basename}` : 'Executable detected';
}

function eventSummary(event: RuntimeEvent): string {
  if (!event.data || Object.keys(event.data).length === 0) {
    return 'No additional event data';
  }
  const raw = JSON.stringify(event.data);
  return raw.length > 140 ? `${raw.slice(0, 137)}…` : raw;
}

interface RuntimeRowProps {
  device: ConnectorDevice;
  runtime: ConnectorRuntime;
  binding?: AgentBinding;
  busyAction?: ConnectorCommandAction | 'binding';
  events?: RuntimeEvent[];
  eventsOpen: boolean;
  taskDraft: string;
  workspaceDraft: string;
  onBind: () => void;
  onAction: (action: ConnectorCommandAction, payload?: Record<string, unknown>) => void;
  onTaskDraftChange: (value: string) => void;
  onWorkspaceDraftChange: (value: string) => void;
  onToggleEvents: () => void;
}

function RuntimeRow({
  device,
  runtime,
  binding,
  busyAction,
  events = [],
  eventsOpen,
  taskDraft,
  workspaceDraft,
  onBind,
  onAction,
  onTaskDraftChange,
  onWorkspaceDraftChange,
  onToggleEvents,
}: RuntimeRowProps) {
  const isOnline = device.status === 'online';
  const sessionId = binding?.last_session_id;
  const taskId = binding?.last_task_id;
  const canControl = Boolean(binding && runtime.available && isOnline);
  const taskExecutionEnabled = runtime.capabilities.includes('task.dispatch');
  const canManageSession = canControl && taskExecutionEnabled;
  const taskOptInFlag =
    runtime.kind === 'claude_code'
      ? '--unsafe-enable-claude-tasks'
      : '--enable-codex-tasks';

  return (
    <div className="border-t border-arena-border/80 first:border-t-0">
      <div className="grid gap-4 p-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-arena-border bg-black/20 text-arena-accent">
              <TerminalSquare className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="font-semibold text-white">{runtime.display_name}</h4>
                <span className="rounded-full border border-arena-border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-gray-500">
                  {runtimeLabel(runtime.kind)}
                </span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                    runtime.available
                      ? 'bg-arena-success/10 text-arena-success'
                      : 'bg-arena-danger/10 text-arena-danger'
                  }`}
                >
                  {runtime.available ? 'Detected' : 'Unavailable'}
                </span>
                {!taskExecutionEnabled && (
                  <span className="rounded-full bg-arena-gold/10 px-2 py-0.5 text-[10px] font-medium text-arena-gold">
                    Detection only
                  </span>
                )}
              </div>
              <p className="mt-1 truncate font-mono text-[11px] text-gray-600">
                {executableLabel(runtime.executable_path)}
                {runtime.version ? ` · ${runtime.version}` : ''}
              </p>
            </div>
          </div>

          {(runtime.capabilities.length > 0 || runtime.auth_modes.length > 0) && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {runtime.capabilities.slice(0, 4).map((capability) => (
                <span
                  key={capability}
                  className="rounded-md bg-white/[0.035] px-2 py-1 text-[10px] text-gray-500"
                >
                  {capability}
                </span>
              ))}
              {runtime.auth_modes.slice(0, 2).map((authMode) => (
                <span
                  key={authMode}
                  className="flex items-center gap-1 rounded-md bg-arena-accent/[0.045] px-2 py-1 text-[10px] text-arena-accent/70"
                >
                  <KeyRound className="h-2.5 w-2.5" />
                  {authMode}
                </span>
              ))}
            </div>
          )}
        </div>

        {!binding ? (
          <div className="grid min-w-[220px] gap-2">
            <div className="rounded-lg border border-arena-border bg-black/20 px-3 py-2">
              <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-gray-600">
                Control-plane identity
              </p>
              <p className="mt-1 text-xs leading-5 text-gray-400">
                ADX assigns this binding. Arena trading identity stays separate until the
                persistent ownership service is connected.
              </p>
            </div>
            <button
              type="button"
              onClick={onBind}
              disabled={!runtime.available || busyAction === 'binding'}
              className="inline-flex items-center justify-center gap-2 rounded-lg bg-arena-accent px-4 py-2 text-sm font-bold text-black transition hover:shadow-[0_0_24px_rgba(0,240,255,0.2)] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busyAction === 'binding' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Link2 className="h-4 w-4" />
              )}
              Bind runtime
            </button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2 md:justify-end">
            <span
              className={`rounded-full border px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.12em] ${
                binding.status === 'running'
                  ? 'border-arena-success/20 bg-arena-success/5 text-arena-success'
                  : binding.status === 'degraded'
                    ? 'border-arena-danger/20 bg-arena-danger/5 text-arena-danger'
                    : 'border-arena-border bg-white/[0.025] text-gray-400'
              }`}
            >
              {binding.status}
            </span>
            <button
              type="button"
              onClick={() => onAction('runtime.probe')}
              disabled={!canControl || Boolean(busyAction)}
              title="Ask the Connector to re-check this runtime"
              className="rounded-lg border border-arena-border p-2 text-gray-400 transition hover:border-arena-accent/30 hover:text-white disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busyAction === 'runtime.probe' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
            </button>
            <button
              type="button"
              onClick={onToggleEvents}
              className="inline-flex items-center gap-1.5 rounded-lg border border-arena-border px-3 py-2 text-xs text-gray-400 transition hover:border-arena-accent/30 hover:text-white"
            >
              <Activity className="h-3.5 w-3.5" />
              Events
              {eventsOpen ? (
                <ChevronUp className="h-3.5 w-3.5" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" />
              )}
            </button>
          </div>
        )}
      </div>

      {binding && (
        <div className="border-t border-arena-border/60 bg-black/[0.12] px-5 py-4">
          {!taskExecutionEnabled && (
            <div className="mb-3 rounded-lg border border-arena-gold/20 bg-arena-gold/[0.04] px-3 py-2 text-[11px] leading-5 text-arena-gold/80">
              Local task execution is off. Restart this trusted Connector with{' '}
              <span className="font-mono">{taskOptInFlag}</span> to enable managed
              sessions for this Runtime.
            </div>
          )}
          <div className="mb-3">
            <label
              htmlFor={`workspace-${binding.binding_id}`}
              className="mb-1.5 block text-xs font-medium text-gray-400"
            >
              Managed session workspace
            </label>
            <input
              id={`workspace-${binding.binding_id}`}
              value={workspaceDraft}
              onChange={(event) => onWorkspaceDraftChange(event.target.value)}
              placeholder="Absolute path inside a local --allow-root"
              disabled={!canManageSession}
              className="w-full rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2 font-mono text-xs text-gray-300 outline-none transition placeholder:font-sans placeholder:text-gray-700 focus:border-arena-accent/50 disabled:cursor-not-allowed disabled:opacity-45"
            />
            <p className="mt-1.5 text-[10px] leading-4 text-gray-600">
              Required. The Connector resolves this path locally and rejects it unless it is
              contained by an <span className="font-mono">--allow-root</span>.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() =>
                workspaceDraft.trim() &&
                onAction('session.start', {
                  working_directory: workspaceDraft.trim(),
                })
              }
              disabled={!canManageSession || !workspaceDraft.trim() || Boolean(busyAction)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-arena-success/10 px-3 py-2 text-xs font-medium text-arena-success transition hover:bg-arena-success/15 disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busyAction === 'session.start' ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              Start managed session
            </button>
            <button
              type="button"
              onClick={() =>
                sessionId &&
                taskId &&
                onAction('task.cancel', {
                  session_id: sessionId,
                  request_id: taskId,
                })
              }
              disabled={!canManageSession || !sessionId || !taskId || Boolean(busyAction)}
              title={taskId ? `Cancel task ${taskId}` : 'No active task has been reported'}
              className="inline-flex items-center gap-1.5 rounded-lg border border-arena-border px-3 py-2 text-xs text-gray-400 transition hover:border-arena-gold/30 hover:text-arena-gold disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busyAction === 'task.cancel' ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Pause className="h-3.5 w-3.5" />
              )}
              Cancel active task
            </button>
            <button
              type="button"
              onClick={() =>
                sessionId &&
                onAction('session.stop', {
                  session_id: sessionId,
                  reason: 'Stopped from ADX Arena',
                })
              }
              disabled={!canManageSession || !sessionId || Boolean(busyAction)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-arena-danger/20 px-3 py-2 text-xs text-arena-danger/80 transition hover:bg-arena-danger/5 hover:text-arena-danger disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busyAction === 'session.stop' ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <CircleStop className="h-3.5 w-3.5" />
              )}
              Stop session
            </button>
            <span className="ml-auto font-mono text-[10px] text-gray-600">
              {sessionId ? `session ${sessionId}` : 'Start a session to dispatch tasks'}
            </span>
          </div>

          <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
            <label className="sr-only" htmlFor={`task-${binding.binding_id}`}>
              Task for {binding.display_name}
            </label>
            <textarea
              id={`task-${binding.binding_id}`}
              rows={2}
              value={taskDraft}
              onChange={(event) => onTaskDraftChange(event.target.value)}
              placeholder={
                sessionId
                  ? 'Describe the Arena task for this managed session…'
                  : 'Start a managed session before dispatching a task.'
              }
              disabled={!canManageSession || !sessionId}
              className="min-h-[58px] resize-none rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2 text-sm text-gray-200 outline-none transition placeholder:text-gray-700 focus:border-arena-accent/50 disabled:cursor-not-allowed disabled:opacity-45"
            />
            <button
              type="button"
              onClick={() =>
                sessionId &&
                taskDraft.trim() &&
                onAction('task.dispatch', {
                  session_id: sessionId,
                  prompt: taskDraft.trim(),
                })
              }
              disabled={
                !canManageSession ||
                !sessionId ||
                !taskDraft.trim() ||
                Boolean(busyAction)
              }
              className="inline-flex items-center justify-center gap-2 rounded-lg bg-arena-accent px-4 py-2 text-sm font-bold text-black transition disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busyAction === 'task.dispatch' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
              Dispatch
            </button>
          </div>

          <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-center">
            <p className="min-w-0 flex-1 text-[11px] leading-5 text-gray-600">
              Resume uses only the provider token captured from this Connector-owned session.
              ADX cannot supply or replace that token.
            </p>
            <button
              type="button"
              onClick={() =>
                sessionId &&
                onAction('session.resume', {
                  session_id: sessionId,
                })
              }
              disabled={
                !canManageSession ||
                !sessionId ||
                binding.status !== 'stopped' ||
                Boolean(busyAction)
              }
              className="inline-flex items-center justify-center gap-2 rounded-lg border border-arena-border px-4 py-2 text-xs font-medium text-gray-400 transition hover:border-arena-accent/30 hover:text-white disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busyAction === 'session.resume' ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RotateCcw className="h-3.5 w-3.5" />
              )}
              Resume managed session
            </button>
          </div>

          <p className="mt-3 flex items-start gap-2 text-[11px] leading-5 text-gray-600">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-arena-accent/50" />
            Controls apply only to sessions launched by this Connector. ADX does not take over
            Claude Code, Codex, or terminal windows you already opened.
          </p>
        </div>
      )}

      {binding && eventsOpen && (
        <div className="border-t border-arena-border/60 bg-[#0b0b11] px-5 py-4">
          <div className="mb-3 flex items-center justify-between">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-gray-500">
              Runtime event stream
            </p>
            <span className="text-[10px] text-gray-600">{events.length} recent</span>
          </div>
          {events.length === 0 ? (
            <p className="rounded-lg border border-dashed border-arena-border px-3 py-5 text-center text-xs text-gray-600">
              No runtime events have been reported for this binding.
            </p>
          ) : (
            <ol className="max-h-56 space-y-2 overflow-y-auto pr-1">
              {[...events].reverse().map((event, index) => (
                <li
                  key={event.event_id || `${event.occurred_at}-${index}`}
                  className="grid gap-1 rounded-lg border border-arena-border/70 bg-white/[0.018] px-3 py-2 sm:grid-cols-[145px_minmax(0,1fr)]"
                >
                  <span className="font-mono text-[10px] text-gray-600">
                    {formatTimestamp(event.occurred_at || event.received_at)}
                  </span>
                  <span className="min-w-0">
                    <span className="text-xs font-medium text-gray-300">
                      {event.event_type || event.type || 'runtime.event'}
                    </span>
                    <span className="ml-2 break-all font-mono text-[10px] text-gray-600">
                      {eventSummary(event)}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

export default function ConnectorConsole() {
  const [deviceName, setDeviceName] = useState('My computer');
  const [approvalCode, setApprovalCode] = useState('');
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [devices, setDevices] = useState<ConnectorDevice[]>([]);
  const [bindings, setBindings] = useState<AgentBinding[]>([]);
  const [events, setEvents] = useState<Record<string, RuntimeEvent[]>>({});
  const [openEvents, setOpenEvents] = useState<Record<string, boolean>>({});
  const [taskDrafts, setTaskDrafts] = useState<Record<string, string>>({});
  const [workspaceDrafts, setWorkspaceDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, ConnectorCommandAction | 'binding'>>({});
  const [pairingBusy, setPairingBusy] = useState(false);
  const [revokingDeviceId, setRevokingDeviceId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [copied, setCopied] = useState(false);
  const [commandCopied, setCommandCopied] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [publicOrigin, setPublicOrigin] = useState(CONNECTOR_API_BASE_URL);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const [nextDevices, nextBindings] = await Promise.all([
        listConnectorDevices(),
        listBindings(),
      ]);
      setDevices(nextDevices);
      setBindings(nextBindings);
      setApiError(null);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Connector API is unavailable.');
    } finally {
      if (!quiet) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    if (!publicOrigin) setPublicOrigin(window.location.origin);
  }, [publicOrigin]);

  useEffect(() => {
    void refresh();
    const refreshTimer = window.setInterval(() => void refresh(true), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(refreshTimer);
  }, [refresh]);

  useEffect(() => {
    if (!pairing) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  useEffect(() => {
    const bindingIds = Object.entries(openEvents)
      .filter(([, isOpen]) => isOpen)
      .map(([bindingId]) => bindingId);
    if (bindingIds.length === 0) return;

    let cancelled = false;
    const refreshEvents = async () => {
      const results = await Promise.allSettled(
        bindingIds.map(async (bindingId) => ({
          bindingId,
          events: await listBindingEvents(bindingId),
        })),
      );
      if (cancelled) return;
      setEvents((current) => {
        const next = { ...current };
        results.forEach((result) => {
          if (result.status === 'fulfilled') {
            next[result.value.bindingId] = result.value.events;
          }
        });
        return next;
      });
    };

    void refreshEvents();
    const timer = window.setInterval(() => void refreshEvents(), REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [openEvents]);

  const onlineCount = devices.filter((device) => device.status === 'online').length;
  const runtimeCount = devices.reduce((total, device) => total + device.runtimes.length, 0);
  const pairingSeconds = pairing
    ? Math.max(0, Math.floor((new Date(pairing.expires_at).getTime() - now) / 1_000))
    : 0;

  const bindingByRuntime = useMemo(() => {
    return new Map(
      bindings.map((binding) => [
        `${binding.device_id}:${binding.runtime_id}`,
        binding,
      ]),
    );
  }, [bindings]);

  async function handleCreatePairing() {
    setPairingBusy(true);
    setApiError(null);
    setNotice(null);
    try {
      const nextPairing = await createPairing({
        owner_id: CURRENT_OWNER_ID,
        device_name: deviceName.trim() || 'Local computer',
      });
      setPairing(nextPairing);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not create a pairing code.');
    } finally {
      setPairingBusy(false);
    }
  }

  async function handleApprovePairing() {
    if (!pairing) return;
    setPairingBusy(true);
    setApiError(null);
    try {
      const approved = await approvePairing(pairing.user_code, CURRENT_OWNER_ID);
      setPairing((current) => (current ? { ...current, ...approved } : approved));
      setNotice('Pairing approved. The Connector can now complete device enrollment.');
      void refresh(true);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not approve this pairing.');
    } finally {
      setPairingBusy(false);
    }
  }

  async function handleApproveConnectorCode() {
    const userCode = approvalCode.trim().toUpperCase();
    if (!userCode) return;
    setPairingBusy(true);
    setApiError(null);
    setNotice(null);
    try {
      const approved = await approvePairing(userCode, CURRENT_OWNER_ID);
      setApprovalCode('');
      setPairing((current) =>
        current?.user_code === approved.user_code ? { ...current, ...approved } : current,
      );
      setNotice(
        `Pairing ${approved.user_code} approved. The Connector can now enroll this computer.`,
      );
      void refresh(true);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not approve this pairing.');
    } finally {
      setPairingBusy(false);
    }
  }

  async function handleCopyCode() {
    if (!pairing) return;
    await navigator.clipboard.writeText(pairing.user_code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_500);
  }

  async function handleCopyStartCommand() {
    await navigator.clipboard.writeText(
      `adx-connector connect --server ${publicOrigin || window.location.origin}`,
    );
    setCommandCopied(true);
    window.setTimeout(() => setCommandCopied(false), 1_500);
  }

  async function handleCreateBinding(device: ConnectorDevice, runtime: ConnectorRuntime) {
    const key = `${device.device_id}:${runtime.runtime_id}`;
    setBusy((current) => ({ ...current, [key]: 'binding' }));
    setApiError(null);
    try {
      await createBinding(device.device_id, {
        runtime_id: runtime.runtime_id,
        display_name: `${runtime.display_name} on ${device.name}`,
      });
      setNotice(`${runtime.display_name} now has an ADX control-plane binding.`);
      await refresh(true);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not bind this runtime.');
    } finally {
      setBusy((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    }
  }

  async function handleRevokeDevice(device: ConnectorDevice) {
    const confirmed = window.confirm(
      `Revoke ${device.name}? Its Connector token and active bindings will stop working.`,
    );
    if (!confirmed) return;

    setRevokingDeviceId(device.device_id);
    setApiError(null);
    try {
      await revokeConnectorDevice(device.device_id, CURRENT_OWNER_ID);
      setNotice(`${device.name} was revoked.`);
      await refresh(true);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not revoke this device.');
    } finally {
      setRevokingDeviceId(null);
    }
  }

  async function handleCommand(
    binding: AgentBinding,
    action: ConnectorCommandAction,
    payload: Record<string, unknown> = {},
  ) {
    setBusy((current) => ({ ...current, [binding.binding_id]: action }));
    setApiError(null);
    try {
      await sendBindingCommand(binding.binding_id, {
        action,
        payload,
        idempotency_key: commandIdempotencyKey(binding.binding_id, action),
      });
      setNotice(`${action} was queued for ${binding.display_name}.`);
      if (action === 'task.dispatch') {
        setTaskDrafts((current) => ({ ...current, [binding.binding_id]: '' }));
      }
      await refresh(true);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : `Could not queue ${action}.`);
    } finally {
      setBusy((current) => {
        const next = { ...current };
        delete next[binding.binding_id];
        return next;
      });
    }
  }

  async function handleToggleEvents(binding: AgentBinding) {
    const nextOpen = !openEvents[binding.binding_id];
    setOpenEvents((current) => ({ ...current, [binding.binding_id]: nextOpen }));
    if (!nextOpen) return;

    try {
      const nextEvents = await listBindingEvents(binding.binding_id);
      setEvents((current) => ({ ...current, [binding.binding_id]: nextEvents }));
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not load runtime events.');
    }
  }

  return (
    <section id="connect" aria-labelledby="connector-heading">
      <div className="mb-8 flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.22em] text-arena-accent/70">
            Agent entry
          </p>
          <h1
            id="connector-heading"
            className="mt-2 text-4xl font-black tracking-tight text-white sm:text-5xl"
          >
            Bring a runtime into the Arena.
          </h1>
          <p className="mt-3 max-w-3xl text-sm leading-6 text-gray-400 sm:text-base">
            Pair a Connector running on your computer, or start with a platform template.
            Local runtimes keep their own configuration while ADX receives a controlled,
            auditable event stream.
          </p>
        </div>

        <div className="grid grid-cols-3 gap-px overflow-hidden rounded-xl border border-arena-border bg-arena-border lg:min-w-[360px]">
          {[
            { label: 'Devices online', value: onlineCount },
            { label: 'Runtimes found', value: runtimeCount },
            { label: 'Runtime bindings', value: bindings.length },
          ].map((item) => (
            <div key={item.label} className="bg-arena-card px-4 py-3 text-center">
              <p className="font-mono text-lg font-bold text-white">{item.value}</p>
              <p className="mt-1 text-[10px] text-gray-600">{item.label}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.65fr)_minmax(280px,0.7fr)]">
        <div className="glow-card overflow-hidden">
          <div className="border-b border-arena-border p-5 sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="flex items-center gap-2">
                  <Radio className="h-5 w-5 text-arena-accent" />
                  <h2 className="text-lg font-bold text-white">Connect a local runtime</h2>
                  <span className="rounded-full bg-arena-accent/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-arena-accent">
                    Recommended
                  </span>
                </div>
                <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500">
                  One outbound connection discovers supported Claude Code and Codex
                  installations, then multiplexes Connector-managed sessions.
                </p>
              </div>
              <span className="flex items-center gap-1.5 rounded-full border border-arena-border px-2.5 py-1 text-[10px] text-gray-500">
                <ShieldCheck className="h-3 w-3 text-arena-success" />
                No inbound port
              </span>
            </div>

            <div className="mt-6 grid grid-cols-[auto_minmax(60px,1fr)_auto_minmax(60px,1fr)_auto] items-center gap-2">
              {[
                { icon: Laptop, label: 'Your computer' },
                { icon: Radio, label: 'Outbound WSS' },
                { icon: CloudCog, label: 'ADX Gateway' },
              ].map((node, index) => (
                <div key={node.label} className="contents">
                  <div className="flex min-w-0 flex-col items-center gap-2 text-center">
                    <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-arena-border bg-black/20 text-arena-accent">
                      <node.icon className="h-4 w-4" />
                    </span>
                    <span className="hidden text-[10px] text-gray-600 sm:block">{node.label}</span>
                  </div>
                  {index < 2 && (
                    <div className="connector-track" aria-hidden="true">
                      <span className="connector-track-dot" />
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="p-5 sm:p-6">
            {!pairing ? (
              <div>
                <div className="rounded-xl border border-arena-accent/20 bg-arena-accent/[0.025] p-4">
                  <div className="mb-3 flex items-start gap-3">
                    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-arena-accent text-black">
                      <TerminalSquare className="h-3.5 w-3.5" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold text-white">
                        Start the Connector, then approve its code
                      </p>
                      <p className="mt-1 text-xs leading-5 text-gray-500">
                        The CLI creates the pairing request and keeps the private device code.
                        Confirm only when both screens show the same user code.
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={handleCopyStartCommand}
                    className="mb-3 flex w-full items-center gap-3 overflow-hidden rounded-lg border border-arena-border bg-[#09090e] px-3 py-2.5 text-left transition hover:border-arena-accent/25"
                    title="Copy Connector start command"
                  >
                    <span className="text-arena-success">$</span>
                    <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-400">
                      adx-connector connect --server {publicOrigin || 'this-arena-url'}
                    </code>
                    {commandCopied ? (
                      <Check className="h-3.5 w-3.5 shrink-0 text-arena-success" />
                    ) : (
                      <Copy className="h-3.5 w-3.5 shrink-0 text-gray-600" />
                    )}
                  </button>
                  <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                    <label className="sr-only" htmlFor="connector-approval-code">
                      Pairing code shown by the Connector
                    </label>
                    <input
                      id="connector-approval-code"
                      value={approvalCode}
                      maxLength={32}
                      onChange={(event) =>
                        setApprovalCode(event.target.value.toUpperCase())
                      }
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') void handleApproveConnectorCode();
                      }}
                      placeholder="Enter code shown by Connector"
                      autoComplete="one-time-code"
                      className="rounded-lg border border-arena-border bg-arena-bg/80 px-3 py-2.5 font-mono text-sm uppercase tracking-[0.12em] text-white outline-none transition placeholder:font-sans placeholder:normal-case placeholder:tracking-normal placeholder:text-gray-700 focus:border-arena-accent/50"
                    />
                    <button
                      type="button"
                      onClick={handleApproveConnectorCode}
                      disabled={pairingBusy || !approvalCode.trim()}
                      className="inline-flex items-center justify-center gap-2 rounded-lg bg-arena-accent px-5 py-2.5 text-sm font-bold text-black transition hover:shadow-[0_0_28px_rgba(0,240,255,0.2)] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {pairingBusy ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Check className="h-4 w-4" />
                      )}
                      Approve Connector
                    </button>
                  </div>
                </div>

                <div className="my-4 flex items-center gap-3">
                  <span className="h-px flex-1 bg-arena-border" />
                  <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-gray-700">
                    Demo and API testing
                  </span>
                  <span className="h-px flex-1 bg-arena-border" />
                </div>

                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
                  <div>
                    <label
                      htmlFor="connector-device-name"
                      className="mb-1.5 block text-xs font-medium text-gray-500"
                    >
                      Generate a demo pairing request for
                    </label>
                    <input
                      id="connector-device-name"
                      value={deviceName}
                      maxLength={128}
                      onChange={(event) => setDeviceName(event.target.value)}
                      className="w-full rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2.5 text-sm text-white outline-none transition placeholder:text-gray-700 focus:border-arena-accent/50"
                    />
                  </div>
                  {CONNECTOR_DEMO_ENABLED && (
                    <button
                      type="button"
                      onClick={handleCreatePairing}
                      disabled={pairingBusy}
                      className="mt-auto inline-flex items-center justify-center gap-2 rounded-lg border border-arena-border px-5 py-2.5 text-sm font-medium text-gray-400 transition hover:border-arena-accent/30 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {pairingBusy ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Link2 className="h-4 w-4" />
                      )}
                      Generate demo code
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <div className="rounded-xl border border-arena-accent/20 bg-arena-accent/[0.035] p-4">
                <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-arena-accent/70">
                      Confirm this code
                    </p>
                    <button
                      type="button"
                      onClick={handleCopyCode}
                      className="mt-1 flex items-center gap-3 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-arena-accent"
                      title="Copy pairing code"
                    >
                      <span className="font-mono text-3xl font-bold tracking-[0.18em] text-white sm:text-4xl">
                        {pairing.user_code}
                      </span>
                      {copied ? (
                        <Check className="h-4 w-4 text-arena-success" />
                      ) : (
                        <Copy className="h-4 w-4 text-gray-500" />
                      )}
                    </button>
                    <p className="mt-2 flex items-center gap-1.5 text-xs text-gray-500">
                      <Clock3 className="h-3.5 w-3.5" />
                      {pairingSeconds > 0
                        ? `Expires in ${Math.floor(pairingSeconds / 60)}:${String(
                            pairingSeconds % 60,
                          ).padStart(2, '0')}`
                        : 'This code has expired'}
                    </p>
                  </div>

                  <div className="flex flex-col items-stretch gap-2 sm:items-end">
                    <span
                      className={`self-start rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] sm:self-end ${
                        pairing.status === 'approved'
                          ? 'bg-arena-success/10 text-arena-success'
                          : 'bg-arena-gold/10 text-arena-gold'
                      }`}
                    >
                      {pairing.status}
                    </span>
                    {pairing.status === 'pending' && pairingSeconds > 0 && (
                      <button
                        type="button"
                        onClick={handleApprovePairing}
                        disabled={pairingBusy}
                        className="inline-flex items-center justify-center gap-2 rounded-lg bg-arena-accent px-4 py-2.5 text-sm font-bold text-black disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {pairingBusy ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Check className="h-4 w-4" />
                        )}
                        Approve pairing
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => setPairing(null)}
                      className="text-xs text-gray-600 transition hover:text-gray-400"
                    >
                      Back to Connector code
                    </button>
                  </div>
                </div>
                <p className="mt-4 border-t border-arena-accent/10 pt-3 text-xs leading-5 text-gray-500">
                  This browser-created code tests the approval UI only. It cannot enroll a
                  Connector because the CLI did not create and retain its private device code.
                  For a real connection, use the code printed by{' '}
                  <span className="font-mono text-gray-400">adx-connector run</span>.
                </p>
              </div>
            )}
          </div>
        </div>

        <a
          href="#platform-agents"
          className="group glow-card flex min-h-[300px] flex-col justify-between overflow-hidden p-6 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-arena-accent"
        >
          <div>
            <span className="flex h-11 w-11 items-center justify-center rounded-xl border border-purple-400/20 bg-purple-400/[0.06] text-purple-300">
              <Bot className="h-5 w-5" />
            </span>
            <p className="mt-6 font-mono text-[10px] uppercase tracking-[0.18em] text-purple-300/70">
              Quick trial
            </p>
            <h2 className="mt-2 text-xl font-bold text-white">Use a platform template</h2>
            <p className="mt-3 text-sm leading-6 text-gray-500">
              Skip local setup and try the full Arena flow with an agent template managed
              on ADX.
            </p>
          </div>
          <span className="mt-8 flex items-center gap-2 text-sm font-medium text-purple-300 transition group-hover:gap-3">
            Browse platform agents
            <ArrowRight className="h-4 w-4" />
          </span>
        </a>
      </div>

      {(apiError || notice) && (
        <div
          className={`mt-4 flex items-start gap-3 rounded-xl border px-4 py-3 text-sm ${
            apiError
              ? 'border-arena-danger/20 bg-arena-danger/5 text-red-300'
              : 'border-arena-success/20 bg-arena-success/5 text-green-300'
          }`}
          role="status"
        >
          {apiError ? (
            <X className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <Check className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <span className="flex-1">{apiError || notice}</span>
          {apiError && (
            <button
              type="button"
              onClick={() => void refresh()}
              className="rounded p-1 text-current transition hover:bg-white/5"
              aria-label="Retry Connector API"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
          )}
        </div>
      )}

      <div className="mt-10">
        <div className="mb-4 flex items-end justify-between gap-4">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-gray-600">
              Device inventory
            </p>
            <h2 className="mt-1 text-xl font-bold text-white">Local Connectors</h2>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={refreshing}
            className="inline-flex items-center gap-2 rounded-lg border border-arena-border px-3 py-2 text-xs text-gray-500 transition hover:border-arena-accent/30 hover:text-white disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {refreshing && devices.length === 0 ? (
          <div className="glow-card flex min-h-40 items-center justify-center text-sm text-gray-600">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            Loading Connector inventory…
          </div>
        ) : devices.length === 0 ? (
          <div className="glow-card border-dashed px-6 py-12 text-center">
            <Laptop className="mx-auto h-8 w-8 text-gray-700" />
            <h3 className="mt-4 font-semibold text-white">No paired computers yet</h3>
            <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-gray-600">
              Start the Connector, approve its code above, and keep it running while it
              reports Claude Code or Codex installations.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {devices.map((device) => (
              <article key={device.device_id} className="glow-card overflow-hidden">
                <header className="flex flex-col gap-3 border-b border-arena-border bg-white/[0.012] p-5 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-center gap-3">
                    <span
                      className={`flex h-10 w-10 items-center justify-center rounded-xl border ${
                        device.status === 'online'
                          ? 'border-arena-success/20 bg-arena-success/5 text-arena-success'
                          : 'border-arena-border bg-black/20 text-gray-600'
                      }`}
                    >
                      {device.status === 'online' ? (
                        <Wifi className="h-4 w-4" />
                      ) : (
                        <WifiOff className="h-4 w-4" />
                      )}
                    </span>
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="font-semibold text-white">{device.name}</h3>
                        <span
                          className={`flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.12em] ${
                            device.status === 'online'
                              ? 'text-arena-success'
                              : 'text-gray-600'
                          }`}
                        >
                          <span
                            className={`h-1.5 w-1.5 rounded-full ${
                              device.status === 'online'
                                ? 'bg-arena-success'
                                : 'bg-gray-700'
                            }`}
                          />
                          {device.status}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-gray-600">
                        {device.platform || 'Platform not reported'} · Last seen{' '}
                        {formatTimestamp(device.last_seen_at)}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="hidden font-mono text-[10px] text-gray-700 md:inline">
                      {device.device_id}
                    </span>
                    {device.status !== 'revoked' && (
                      <button
                        type="button"
                        onClick={() => void handleRevokeDevice(device)}
                        disabled={revokingDeviceId === device.device_id}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-arena-danger/15 px-2.5 py-1.5 text-[10px] text-arena-danger/60 transition hover:border-arena-danger/30 hover:bg-arena-danger/5 hover:text-arena-danger disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {revokingDeviceId === device.device_id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <CircleStop className="h-3 w-3" />
                        )}
                        Revoke
                      </button>
                    )}
                  </div>
                </header>

                {device.runtimes.length === 0 ? (
                  <div className="px-5 py-10 text-center text-sm text-gray-600">
                    This Connector has not reported a supported runtime yet.
                  </div>
                ) : (
                  device.runtimes.map((runtime) => {
                    const binding = bindingByRuntime.get(
                      `${device.device_id}:${runtime.runtime_id}`,
                    );
                    const busyKey = binding?.binding_id || `${device.device_id}:${runtime.runtime_id}`;
                    return (
                      <RuntimeRow
                        key={runtime.runtime_id}
                        device={device}
                        runtime={runtime}
                        binding={binding}
                        busyAction={busy[busyKey]}
                        events={binding ? events[binding.binding_id] : []}
                        eventsOpen={binding ? Boolean(openEvents[binding.binding_id]) : false}
                        taskDraft={binding ? taskDrafts[binding.binding_id] || '' : ''}
                        workspaceDraft={
                          binding ? workspaceDrafts[binding.binding_id] || '' : ''
                        }
                        onBind={() => void handleCreateBinding(device, runtime)}
                        onAction={(action, payload) =>
                          binding && void handleCommand(binding, action, payload)
                        }
                        onTaskDraftChange={(value) =>
                          binding &&
                          setTaskDrafts((current) => ({
                            ...current,
                            [binding.binding_id]: value,
                          }))
                        }
                        onWorkspaceDraftChange={(value) =>
                          binding &&
                          setWorkspaceDrafts((current) => ({
                            ...current,
                            [binding.binding_id]: value,
                          }))
                        }
                        onToggleEvents={() => binding && void handleToggleEvents(binding)}
                      />
                    );
                  })
                )}
              </article>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
