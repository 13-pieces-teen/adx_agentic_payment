'use client';

import { FormEvent, useEffect, useState } from 'react';
import {
  ArrowRight,
  Check,
  KeyRound,
  Laptop,
  LoaderCircle,
  LockKeyhole,
  Radio,
  ShieldCheck,
  TerminalSquare,
  UserRound,
} from 'lucide-react';
import {
  acceptConnectorInvite,
  approvePairingAuthenticated,
  ConnectorAuthSession,
  getConnectorAuthSession,
  loginConnectorUser,
} from '@/lib/connector-api';

type AuthMode = 'invite' | 'login';
type FlowState = 'ready' | 'working' | 'approved';

function normalizeCode(value: string): string {
  return value.trim().toUpperCase().replace(/[^A-Z0-9-]/g, '').slice(0, 32);
}

export default function ConnectPage() {
  const [userCode, setUserCode] = useState('');
  const [authMode, setAuthMode] = useState<AuthMode>('invite');
  const [inviteCode, setInviteCode] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [session, setSession] = useState<ConnectorAuthSession | null>(null);
  const [sessionLoading, setSessionLoading] = useState(true);
  const [flowState, setFlowState] = useState<FlowState>('ready');
  const [error, setError] = useState('');

  useEffect(() => {
    const code = new URLSearchParams(window.location.search).get('code');
    if (code) setUserCode(normalizeCode(code));

    getConnectorAuthSession()
      .then(setSession)
      .catch((cause) => {
        setError(cause instanceof Error ? cause.message : 'Could not check your session.');
      })
      .finally(() => setSessionLoading(false));
  }, []);

  async function authorize(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const code = normalizeCode(userCode);
    if (code.length < 4) {
      setError('Enter the code shown by ADX Connector.');
      return;
    }
    if (!session && authMode === 'invite') {
      if (inviteCode.trim().length < 20) {
        setError('Enter the invite code supplied by the Arena operator.');
        return;
      }
      if (!username.trim() || password.length < 12) {
        setError('Choose a username and a password with at least 12 characters.');
        return;
      }
    }
    if (!session && authMode === 'login' && (!username.trim() || !password)) {
      setError('Enter your username and password.');
      return;
    }

    setError('');
    setFlowState('working');
    try {
      let activeSession = session;
      if (!activeSession && authMode === 'invite') {
        activeSession = await acceptConnectorInvite({
          invite_code: inviteCode.trim(),
          username: username.trim().toLowerCase(),
          password,
        });
      } else if (!activeSession) {
        activeSession = await loginConnectorUser({
          username: username.trim().toLowerCase(),
          password,
        });
      }
      if (!activeSession?.csrf_token) {
        throw new Error('The authorization session did not include a CSRF token.');
      }
      setSession(activeSession);
      await approvePairingAuthenticated(code, activeSession.csrf_token);
      setUserCode(code);
      setFlowState('approved');
    } catch (cause) {
      setFlowState('ready');
      setError(cause instanceof Error ? cause.message : 'Could not authorize this computer.');
    }
  }

  return (
    <div className="min-h-[calc(100vh-4rem)] border-b border-arena-border">
      <div className="mx-auto max-w-6xl px-4 py-12 sm:py-20">
        <div className="grid overflow-hidden rounded-2xl border border-white/10 bg-[#0d1018]/95 shadow-[0_32px_100px_rgba(0,0,0,0.45)] lg:grid-cols-[0.82fr_1.18fr]">
          <aside className="relative overflow-hidden border-b border-white/10 bg-[#101622] p-7 lg:border-b-0 lg:border-r lg:p-10">
            <div
              aria-hidden="true"
              className="absolute -right-28 -top-28 h-72 w-72 rounded-full bg-cyan-400/10 blur-3xl"
            />
            <div className="relative">
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300">
                <Radio className="h-4 w-4" />
                Outbound pairing
              </div>
              <h1 className="mt-6 max-w-md text-3xl font-black leading-tight tracking-tight text-white sm:text-4xl">
                Your agent stays local.
                <span className="block text-cyan-300">ADX gets a secure line.</span>
              </h1>
              <p className="mt-5 max-w-md text-sm leading-6 text-slate-400">
                Approve this computer once. The Connector detects supported local runtimes and
                keeps one encrypted outbound connection to the Arena.
              </p>

              <div className="mt-10 rounded-xl border border-white/10 bg-black/20 p-5">
                <div className="flex items-center justify-between gap-3 font-mono text-[11px] uppercase tracking-wider text-slate-500">
                  <span className="flex items-center gap-2 text-slate-300">
                    <Laptop className="h-4 w-4 text-cyan-300" />
                    Local runtime
                  </span>
                  <span className="flex items-center gap-2 text-slate-300">
                    ADX Arena
                    <ShieldCheck className="h-4 w-4 text-cyan-300" />
                  </span>
                </div>
                <div className="connector-track mt-5" aria-hidden="true">
                  <span className="connector-track-dot" />
                </div>
                <div className="mt-4 flex items-center justify-between text-[11px] text-slate-500">
                  <span>No inbound port</span>
                  <span>TLS + device token</span>
                </div>
              </div>

              <div className="mt-8 grid gap-3 text-sm text-slate-300">
                {[
                  ['1', 'Install Connector'],
                  ['2', 'Approve this code'],
                  ['3', 'Runtime comes online'],
                ].map(([step, label]) => (
                  <div key={step} className="flex items-center gap-3">
                    <span className="flex h-7 w-7 items-center justify-center rounded-md border border-white/10 bg-white/[0.03] font-mono text-xs text-cyan-300">
                      {step}
                    </span>
                    <span>{label}</span>
                  </div>
                ))}
              </div>
            </div>
          </aside>

          <section className="p-7 sm:p-10 lg:p-12">
            {flowState === 'approved' ? (
              <div className="flex min-h-[480px] flex-col items-center justify-center text-center" aria-live="polite">
                <div className="flex h-16 w-16 items-center justify-center rounded-2xl border border-emerald-300/20 bg-emerald-300/10 text-emerald-300">
                  <Check className="h-8 w-8" strokeWidth={2.5} />
                </div>
                <p className="mt-7 font-mono text-xs uppercase tracking-[0.22em] text-emerald-300">
                  Device approved
                </p>
                <h2 className="mt-3 text-3xl font-bold tracking-tight text-white">
                  The Connector is coming online.
                </h2>
                <p className="mt-4 max-w-md text-sm leading-6 text-slate-400">
                  You can close this tab. The local Connector will finish enrollment, store its
                  device credential, and maintain the Arena connection automatically.
                </p>
                <div className="mt-8 rounded-lg border border-white/10 bg-black/20 px-5 py-3 font-mono text-sm tracking-[0.16em] text-cyan-200">
                  {userCode}
                </div>
                <a
                  href="/agents"
                  className="mt-8 inline-flex items-center gap-2 rounded-lg bg-cyan-300 px-5 py-3 text-sm font-bold text-slate-950 transition hover:bg-cyan-200 focus:outline-none focus:ring-2 focus:ring-cyan-200 focus:ring-offset-2 focus:ring-offset-[#0d1018]"
                >
                  View connected agents
                  <ArrowRight className="h-4 w-4" />
                </a>
              </div>
            ) : (
              <>
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-mono text-xs uppercase tracking-[0.2em] text-slate-500">
                      Authorization receipt
                    </p>
                    <h2 className="mt-2 text-2xl font-bold tracking-tight text-white">
                      Connect this computer
                    </h2>
                  </div>
                  <TerminalSquare className="h-6 w-6 text-slate-600" />
                </div>

                <form className="mt-8" onSubmit={authorize}>
                  <label htmlFor="device-code" className="text-sm font-medium text-slate-200">
                    Connector code
                  </label>
                  <div className="relative mt-2">
                    <input
                      id="device-code"
                      value={userCode}
                      onChange={(event) => setUserCode(normalizeCode(event.target.value))}
                      placeholder="ABCD-EFGH"
                      autoComplete="one-time-code"
                      spellCheck={false}
                      className="w-full rounded-xl border border-white/10 bg-black/25 px-4 py-4 font-mono text-xl font-bold uppercase tracking-[0.16em] text-cyan-200 outline-none transition placeholder:text-slate-700 focus:border-cyan-300/60 focus:ring-4 focus:ring-cyan-300/10"
                    />
                    <LockKeyhole className="pointer-events-none absolute right-4 top-1/2 h-5 w-5 -translate-y-1/2 text-slate-600" />
                  </div>
                  <p className="mt-2 text-xs text-slate-500">
                    Filled automatically when the Connector opens this page.
                  </p>

                  <div className="my-8 border-t border-dashed border-white/10" />

                  {sessionLoading ? (
                    <div className="flex min-h-40 items-center justify-center text-sm text-slate-500">
                      <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
                      Checking your Arena session
                    </div>
                  ) : session ? (
                    <div className="rounded-xl border border-cyan-300/15 bg-cyan-300/[0.05] p-4">
                      <div className="flex items-center gap-3">
                        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-cyan-300/10 text-cyan-300">
                          <UserRound className="h-4 w-4" />
                        </span>
                        <div>
                          <p className="text-sm font-semibold text-white">{session.user.username}</p>
                          <p className="text-xs text-slate-500">
                            Arena account · ready to authorize
                          </p>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="grid grid-cols-2 rounded-lg bg-black/25 p-1">
                        <button
                          type="button"
                          onClick={() => {
                            setAuthMode('invite');
                            setError('');
                          }}
                          className={`rounded-md px-3 py-2 text-sm font-medium transition ${
                            authMode === 'invite'
                              ? 'bg-white/10 text-white'
                              : 'text-slate-500 hover:text-slate-300'
                          }`}
                        >
                          Use invite
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setAuthMode('login');
                            setError('');
                          }}
                          className={`rounded-md px-3 py-2 text-sm font-medium transition ${
                            authMode === 'login'
                              ? 'bg-white/10 text-white'
                              : 'text-slate-500 hover:text-slate-300'
                          }`}
                        >
                          Sign in
                        </button>
                      </div>

                      {authMode === 'invite' ? (
                        <div className="mt-5">
                          <label htmlFor="invite-code" className="text-sm font-medium text-slate-200">
                            Beta invite code
                          </label>
                          <div className="relative mt-2">
                            <input
                              id="invite-code"
                              type="password"
                              value={inviteCode}
                              onChange={(event) => setInviteCode(event.target.value)}
                              autoComplete="off"
                              className="w-full rounded-xl border border-white/10 bg-black/25 px-4 py-3 pr-11 text-sm text-white outline-none transition focus:border-cyan-300/60 focus:ring-4 focus:ring-cyan-300/10"
                            />
                            <KeyRound className="pointer-events-none absolute right-4 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-600" />
                          </div>
                          <p className="mt-2 text-xs text-slate-500">
                            The invite is used once to create a recoverable Arena account and is never sent to the Connector.
                          </p>
                          <div className="mt-5 grid gap-4">
                            <div>
                              <label htmlFor="invite-username" className="text-sm font-medium text-slate-200">
                                Choose a username
                              </label>
                              <input
                                id="invite-username"
                                value={username}
                                onChange={(event) => setUsername(event.target.value)}
                                autoComplete="username"
                                className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/60 focus:ring-4 focus:ring-cyan-300/10"
                              />
                            </div>
                            <div>
                              <label htmlFor="invite-password" className="text-sm font-medium text-slate-200">
                                Choose a password
                              </label>
                              <input
                                id="invite-password"
                                type="password"
                                value={password}
                                onChange={(event) => setPassword(event.target.value)}
                                autoComplete="new-password"
                                className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/60 focus:ring-4 focus:ring-cyan-300/10"
                              />
                              <p className="mt-2 text-xs text-slate-500">
                                At least 12 characters. Use these credentials to sign in again after this session expires.
                              </p>
                            </div>
                          </div>
                        </div>
                      ) : (
                        <div className="mt-5 grid gap-4">
                          <div>
                            <label htmlFor="username" className="text-sm font-medium text-slate-200">
                              Username
                            </label>
                            <input
                              id="username"
                              value={username}
                              onChange={(event) => setUsername(event.target.value)}
                              autoComplete="username"
                              className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/60 focus:ring-4 focus:ring-cyan-300/10"
                            />
                          </div>
                          <div>
                            <label htmlFor="password" className="text-sm font-medium text-slate-200">
                              Password
                            </label>
                            <input
                              id="password"
                              type="password"
                              value={password}
                              onChange={(event) => setPassword(event.target.value)}
                              autoComplete="current-password"
                              className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/60 focus:ring-4 focus:ring-cyan-300/10"
                            />
                          </div>
                        </div>
                      )}
                    </>
                  )}

                  {error && (
                    <div
                      role="alert"
                      className="mt-5 rounded-lg border border-rose-400/20 bg-rose-400/[0.06] px-4 py-3 text-sm text-rose-200"
                    >
                      {error}
                    </div>
                  )}

                  <button
                    type="submit"
                    disabled={sessionLoading || flowState === 'working'}
                    className="mt-7 flex w-full items-center justify-center gap-2 rounded-xl bg-cyan-300 px-5 py-3.5 text-sm font-bold text-slate-950 transition hover:bg-cyan-200 focus:outline-none focus:ring-2 focus:ring-cyan-200 focus:ring-offset-2 focus:ring-offset-[#0d1018] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {flowState === 'working' ? (
                      <>
                        <LoaderCircle className="h-4 w-4 animate-spin" />
                        Authorizing
                      </>
                    ) : (
                      <>
                        <ShieldCheck className="h-4 w-4" />
                        Authorize this computer
                      </>
                    )}
                  </button>

                  <p className="mt-4 text-center text-xs leading-5 text-slate-600">
                    Approval grants access only to the Connector capabilities you enable locally.
                    It does not expose a shell or take over existing terminals.
                  </p>
                </form>
              </>
            )}
          </section>
        </div>
      </div>
      <style jsx global>{`
        @media (max-width: 639px) {
          nav > div > div > a:not(:last-child) {
            display: none;
          }

          nav > div > div > a:last-child {
            margin-left: 0;
          }
        }
      `}</style>
    </div>
  );
}
