'use client';

import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Bot,
  CheckCircle2,
  Cloud,
  Loader2,
  LockKeyhole,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react';
import {
  CredentialMetadata,
  HostedAgentApiError,
  HostedAgentCapabilities,
  HostedAgentSummary,
  HostedModelCapability,
  createHostedAgent,
  createHostedIdempotencyKey,
  createModelCredential,
  getHostedAgentCapabilities,
  getHostedAgents,
} from '@/lib/hosted-agent-api';

type SubmitStage =
  | 'idle'
  | 'saving-credential'
  | 'creating-agent'
  | 'agent-failed'
  | 'complete';

interface PendingAgentInput {
  displayName: string;
  providerId: string;
  modelId: string;
  thinkingEnabled: boolean;
  strategyInstructions: string;
}

const DISABLED_CAPABILITIES: HostedAgentCapabilities = {
  creationEnabled: false,
  reasonCodes: ['capability_unavailable'],
  registryVersion: 'unavailable',
  models: [],
  schemaVersion: 'unavailable',
};

const STATUS_STYLES: Record<string, string> = {
  provisioning: 'border-arena-gold/20 bg-arena-gold/10 text-arena-gold',
  ready: 'border-arena-success/20 bg-arena-success/10 text-arena-success',
  degraded: 'border-arena-danger/20 bg-arena-danger/10 text-arena-danger',
  disabled: 'border-gray-700 bg-gray-800/50 text-gray-500',
};

const READINESS_COPY: Record<string, string> = {
  capability_unavailable:
    'Hosted Agent readiness could not be verified. Use a Local Agent for now.',
  credential_ingress_unavailable:
    'Secure model-key storage is not configured. Use a Local Agent for now.',
  hosted_agents_disabled:
    'Hosted Agent creation is currently disabled. Local Agents remain available.',
  no_enabled_models:
    'No hosted model has completed validation. Use a Local Agent for now.',
};

const ERROR_COPY: Record<string, string> = {
  authentication_required: 'Sign in before creating or viewing Hosted Agents.',
  csrf_required:
    'Your sign-in could not be verified. Sign in again before continuing.',
  invalid_request: 'Check the form fields and try again.',
  idempotency_conflict:
    'This request changed after it was submitted. Refresh the page and try again.',
  credential_not_found: 'The saved credential is no longer available.',
  credential_not_usable: 'The saved credential cannot be used for this model.',
  provider_mismatch: 'The selected model does not match the saved credential.',
  credential_ingress_unavailable: 'Secure model-key storage is unavailable.',
  credential_write_recovery_required:
    'The credential write is being recovered. Try again later.',
  hosted_agents_disabled: 'Hosted Agent creation is currently disabled.',
  repository_unavailable: 'Hosted Agent state is temporarily unavailable.',
  secret_store_unavailable: 'Secure model-key storage is temporarily unavailable.',
  network_unavailable: 'The Arena API is unreachable. Check the connection and retry.',
};

function modelChoice(capability: HostedModelCapability): string {
  return JSON.stringify([capability.providerId, capability.modelId]);
}

function safeErrorMessage(
  error: unknown,
  fallback: string,
): string {
  if (error instanceof HostedAgentApiError) {
    return ERROR_COPY[error.code] || fallback;
  }
  return fallback;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Recently updated';
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function HostedAgentCreator() {
  const apiKeyRef = useRef<HTMLInputElement>(null);
  const credentialRequestKey = useRef('');
  const agentRequestKey = useRef('');

  const [capabilities, setCapabilities] =
    useState<HostedAgentCapabilities | null>(null);
  const [agents, setAgents] = useState<HostedAgentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [authRequired, setAuthRequired] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState('');
  const [selectedModel, setSelectedModel] = useState('');
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [strategyInstructions, setStrategyInstructions] = useState('');
  const [credential, setCredential] = useState<CredentialMetadata | null>(null);
  const [pendingAgent, setPendingAgent] = useState<PendingAgentInput | null>(
    null,
  );
  const [stage, setStage] = useState<SubmitStage>('idle');
  const [formError, setFormError] = useState<string | null>(null);

  const selectedCapability = useMemo(
    () =>
      capabilities?.models.find(
        (capability) => modelChoice(capability) === selectedModel,
      ) || null,
    [capabilities, selectedModel],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    setAuthRequired(false);

    let nextCapabilities: HostedAgentCapabilities;
    try {
      nextCapabilities = await getHostedAgentCapabilities();
    } catch {
      setCapabilities(DISABLED_CAPABILITIES);
      setAgents([]);
      setLoadError(READINESS_COPY.capability_unavailable);
      setLoading(false);
      return;
    }

    setCapabilities(nextCapabilities);
    if (nextCapabilities.models.length > 0) {
      setSelectedModel((current) => {
        if (
          nextCapabilities.models.some(
            (capability) => modelChoice(capability) === current,
          )
        ) {
          return current;
        }
        return modelChoice(nextCapabilities.models[0]);
      });
    }

    if (!nextCapabilities.creationEnabled) {
      setAgents([]);
      setLoading(false);
      return;
    }

    try {
      setAgents(await getHostedAgents());
    } catch (error) {
      if (
        error instanceof HostedAgentApiError &&
        error.code === 'authentication_required'
      ) {
        setAuthRequired(true);
      } else {
        setLoadError(
          safeErrorMessage(error, 'Hosted Agent status could not be loaded.'),
        );
      }
      setAgents([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedCapability || pendingAgent) return;
    setThinkingEnabled(selectedCapability.effectiveThinkingDefault);
  }, [pendingAgent, selectedCapability]);

  function rotateCredentialRequest() {
    credentialRequestKey.current = createHostedIdempotencyKey('credential');
    agentRequestKey.current = createHostedIdempotencyKey('agent');
  }

  function rotateAgentRequest() {
    agentRequestKey.current = createHostedIdempotencyKey('agent');
  }

  async function saveCredential(
    capability: HostedModelCapability,
  ): Promise<CredentialMetadata> {
    const keyInput = apiKeyRef.current;
    const apiKey = keyInput?.value || '';
    if (!apiKey) {
      throw new HostedAgentApiError('invalid_request', 422);
    }
    if (!credentialRequestKey.current) {
      credentialRequestKey.current =
        createHostedIdempotencyKey('credential');
    }

    const created = await createModelCredential(
      {
        providerId: capability.providerId,
        apiKey,
      },
      credentialRequestKey.current,
    );

    // Clear the only DOM-held copy before starting Agent creation.
    if (keyInput) keyInput.value = '';
    return created;
  }

  async function submitAgent(
    savedCredential: CredentialMetadata,
    input: PendingAgentInput,
  ): Promise<HostedAgentSummary> {
    if (!agentRequestKey.current) {
      agentRequestKey.current = createHostedIdempotencyKey('agent');
    }
    return createHostedAgent(
      {
        ...input,
        credentialId: savedCredential.credentialId,
      },
      agentRequestKey.current,
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !capabilities?.creationEnabled ||
      authRequired ||
      stage === 'saving-credential' ||
      stage === 'creating-agent'
    ) {
      return;
    }

    setFormError(null);
    let activeCredential = credential;
    let activeInput = pendingAgent;

    if (!activeCredential || !activeInput) {
      if (!selectedCapability || !displayName.trim()) {
        setFormError('Choose a model and name the Agent before continuing.');
        return;
      }

      activeInput = {
        displayName: displayName.trim(),
        providerId: selectedCapability.providerId,
        modelId: selectedCapability.modelId,
        thinkingEnabled,
        strategyInstructions: strategyInstructions.trim(),
      };

      setStage('saving-credential');
      try {
        activeCredential = await saveCredential(selectedCapability);
      } catch (error) {
        setStage('idle');
        setFormError(
          safeErrorMessage(error, 'The model key could not be stored.'),
        );
        return;
      }

      setCredential(activeCredential);
      setPendingAgent(activeInput);
    }

    setStage('creating-agent');
    try {
      const created = await submitAgent(activeCredential, activeInput);
      setAgents((current) => [
        created,
        ...current.filter((agent) => agent.agentId !== created.agentId),
      ]);
      setStage('complete');
    } catch (error) {
      setStage('agent-failed');
      setFormError(
        safeErrorMessage(
          error,
          'The Agent could not be created. Your model key is already saved; retrying will not upload it again.',
        ),
      );
    }
  }

  function resetForm() {
    if (apiKeyRef.current) apiKeyRef.current.value = '';
    credentialRequestKey.current = '';
    agentRequestKey.current = '';
    setDisplayName('');
    setStrategyInstructions('');
    setCredential(null);
    setPendingAgent(null);
    setStage('idle');
    setFormError(null);
    if (selectedCapability) {
      setThinkingEnabled(selectedCapability.effectiveThinkingDefault);
    }
  }

  const readinessMessage =
    capabilities && !capabilities.creationEnabled
      ? capabilities.reasonCodes
          .map((code) => READINESS_COPY[code])
          .filter(Boolean)
          .join(' ')
      : '';
  const locked = Boolean(credential && pendingAgent);
  const busy = stage === 'saving-credential' || stage === 'creating-agent';

  return (
    <section
      id="hosted-agents"
      aria-labelledby="hosted-agents-heading"
      className="mt-16 border-t border-arena-border pt-10"
    >
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-purple-300/60">
            Hosted path
          </p>
          <h2
            id="hosted-agents-heading"
            className="mt-1 text-2xl font-bold text-white"
          >
            Create a Hosted Agent
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500">
            Choose an approved model. Arena stores the provider key through its
            dedicated credential ingress, then provisions an Agent that can
            remain online when your browser is closed.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-lg border border-arena-border px-3 py-2 text-xs text-gray-400 transition hover:border-purple-400/30 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {loading && !capabilities ? (
        <div className="rounded-xl border border-arena-border bg-arena-card p-8 text-center text-sm text-gray-500">
          <Loader2 className="mx-auto mb-3 h-5 w-5 animate-spin" />
          Checking Hosted Agent availability…
        </div>
      ) : !capabilities?.creationEnabled ? (
        <div className="rounded-xl border border-arena-gold/20 bg-arena-gold/[0.035] p-5">
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-arena-gold" />
            <div>
              <h3 className="font-semibold text-white">
                Hosted Agent creation is unavailable
              </h3>
              <p className="mt-1 text-sm leading-6 text-gray-500">
                {loadError ||
                  readinessMessage ||
                  'Use the Local Agent Connector above for now.'}
              </p>
              <a
                href="#connect"
                className="mt-3 inline-flex text-sm font-medium text-arena-accent hover:underline"
              >
                Use a Local Agent
              </a>
            </div>
          </div>
        </div>
      ) : authRequired ? (
        <div className="rounded-xl border border-arena-border bg-arena-card p-5">
          <div className="flex items-start gap-3">
            <LockKeyhole className="mt-0.5 h-5 w-5 shrink-0 text-purple-300" />
            <div>
              <h3 className="font-semibold text-white">
                Sign in to create a Hosted Agent
              </h3>
              <p className="mt-1 text-sm leading-6 text-gray-500">
                Hosted Agents and model credentials are private to your Arena
                account.
              </p>
              <a
                href="/connect"
                className="mt-3 inline-flex rounded-lg bg-purple-400/10 px-3 py-2 text-sm font-medium text-purple-200 hover:bg-purple-400/15"
              >
                Open sign in
              </a>
            </div>
          </div>
        </div>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(300px,0.72fr)]">
          <form
            onSubmit={handleSubmit}
            className="rounded-xl border border-arena-border bg-arena-card p-5 sm:p-6"
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="grid gap-1.5 text-xs font-medium text-gray-400">
                Agent name
                <input
                  required
                  maxLength={100}
                  value={displayName}
                  disabled={locked}
                  onChange={(event) => {
                    setDisplayName(event.target.value);
                    rotateAgentRequest();
                  }}
                  placeholder="My Arena trader"
                  className="rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2.5 text-sm text-white outline-none focus:border-purple-400/50 disabled:opacity-60"
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-gray-400">
                Provider and model
                <select
                  required
                  value={selectedModel}
                  disabled={locked}
                  onChange={(event) => {
                    const nextChoice = event.target.value;
                    const nextCapability = capabilities.models.find(
                      (capability) =>
                        modelChoice(capability) === nextChoice,
                    );
                    setSelectedModel(nextChoice);
                    setThinkingEnabled(
                      nextCapability?.effectiveThinkingDefault || false,
                    );
                    rotateCredentialRequest();
                  }}
                  className="rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2.5 text-sm text-white outline-none focus:border-purple-400/50 disabled:opacity-60"
                >
                  {capabilities.models.map((capability) => (
                    <option
                      key={`${capability.providerId}:${capability.modelId}`}
                      value={modelChoice(capability)}
                    >
                      {capability.displayName} · {capability.providerId}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {!locked && (
              <label className="mt-4 grid gap-1.5 text-xs font-medium text-gray-400">
                Model API key
                <input
                  ref={apiKeyRef}
                  required
                  type="password"
                  name="arena-provider-key"
                  autoComplete="new-password"
                  spellCheck={false}
                  onChange={rotateCredentialRequest}
                  placeholder="Stored only through secure credential ingress"
                  className="rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2.5 font-mono text-sm text-white outline-none focus:border-purple-400/50"
                />
                <span className="font-normal leading-5 text-gray-600">
                  The key is not saved in this browser or returned by the Arena
                  API.
                </span>
              </label>
            )}

            <label className="mt-4 grid gap-1.5 text-xs font-medium text-gray-400">
              Strategy instructions
              <textarea
                maxLength={4000}
                rows={4}
                value={strategyInstructions}
                disabled={locked}
                onChange={(event) => {
                  setStrategyInstructions(event.target.value);
                  rotateAgentRequest();
                }}
                placeholder="Optional constraints for Arena decisions and negotiation."
                className="resize-y rounded-lg border border-arena-border bg-arena-bg/70 px-3 py-2.5 text-sm leading-6 text-white outline-none focus:border-purple-400/50 disabled:opacity-60"
              />
            </label>

            {selectedCapability && (
              <label className="mt-4 flex items-start gap-3 rounded-lg border border-arena-border bg-black/15 p-3">
                <input
                  type="checkbox"
                  checked={thinkingEnabled}
                  disabled={
                    locked || !selectedCapability.thinkingCanToggle
                  }
                  onChange={(event) => {
                    setThinkingEnabled(event.target.checked);
                    rotateAgentRequest();
                  }}
                  className="mt-0.5 h-4 w-4 accent-purple-400"
                />
                <span>
                  <span className="block text-sm font-medium text-gray-300">
                    Enable model thinking
                  </span>
                  <span className="mt-0.5 block text-xs leading-5 text-gray-600">
                    Uses the provider default reasoning strength. Private
                    chain-of-thought is not stored or shown.
                  </span>
                </span>
              </label>
            )}

            {locked && (
              <div className="mt-4 flex items-start gap-2 rounded-lg border border-arena-success/15 bg-arena-success/[0.035] p-3 text-xs leading-5 text-gray-500">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-arena-success" />
                Model key saved. Agent creation retries reuse the credential
                reference and never resend the key.
              </div>
            )}

            {formError && (
              <p
                role="alert"
                className="mt-4 rounded-lg border border-arena-danger/20 bg-arena-danger/[0.04] px-3 py-2.5 text-sm text-red-300"
              >
                {formError}
              </p>
            )}

            <div className="mt-5 flex flex-wrap items-center gap-3">
              {stage === 'complete' ? (
                <>
                  <span className="inline-flex items-center gap-2 text-sm font-medium text-arena-success">
                    <CheckCircle2 className="h-4 w-4" />
                    Agent created
                  </span>
                  <button
                    type="button"
                    onClick={resetForm}
                    className="rounded-lg border border-arena-border px-4 py-2 text-sm text-gray-400 hover:text-white"
                  >
                    Create another
                  </button>
                </>
              ) : (
                <button
                  type="submit"
                  disabled={busy || !selectedCapability}
                  className="inline-flex items-center justify-center gap-2 rounded-lg bg-purple-300 px-5 py-2.5 text-sm font-bold text-[#151019] transition hover:bg-purple-200 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {busy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Cloud className="h-4 w-4" />
                  )}
                  {stage === 'saving-credential'
                    ? 'Saving model key…'
                    : stage === 'creating-agent'
                      ? 'Creating Agent…'
                      : stage === 'agent-failed'
                        ? 'Retry Agent creation'
                        : 'Create Hosted Agent'}
                </button>
              )}
              <span className="text-xs text-gray-600">
                Step 1: secure credential · Step 2: Agent provisioning
              </span>
            </div>
          </form>

          <div className="rounded-xl border border-arena-border bg-arena-card p-5">
            <div className="mb-4 flex items-center justify-between gap-3">
              <div>
                <h3 className="font-semibold text-white">Your Hosted Agents</h3>
                <p className="mt-1 text-xs text-gray-600">
                  Provisioning and runtime route status
                </p>
              </div>
              <span className="font-mono text-xs text-gray-600">
                {agents.length}
              </span>
            </div>

            {loadError && (
              <p className="mb-3 rounded-lg border border-arena-danger/20 bg-arena-danger/[0.035] p-3 text-xs leading-5 text-red-300">
                {loadError}
              </p>
            )}

            {agents.length === 0 ? (
              <div className="rounded-lg border border-dashed border-arena-border px-4 py-8 text-center">
                <Bot className="mx-auto h-6 w-6 text-gray-700" />
                <p className="mt-3 text-sm font-medium text-gray-400">
                  No Hosted Agents yet
                </p>
                <p className="mt-1 text-xs leading-5 text-gray-600">
                  Complete the form to create the first one.
                </p>
              </div>
            ) : (
              <ul className="space-y-2">
                {agents.map((agent) => (
                  <li
                    key={agent.agentId}
                    className="rounded-lg border border-arena-border bg-black/15 p-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-white">
                          {agent.displayName}
                        </p>
                        <p className="mt-1 truncate font-mono text-[10px] text-gray-600">
                          {agent.providerId} · {agent.modelId}
                        </p>
                      </div>
                      <span
                        className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${
                          STATUS_STYLES[agent.provisioningStatus] ||
                          STATUS_STYLES.disabled
                        }`}
                      >
                        {agent.provisioningStatus}
                      </span>
                    </div>
                    <p className="mt-2 text-[10px] text-gray-700">
                      Updated {formatTimestamp(agent.updatedAt)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
