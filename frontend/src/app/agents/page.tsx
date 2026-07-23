'use client';

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { ArrowRight, Bot, Circle, Cpu, Globe, Server, Zap } from 'lucide-react';
import ConnectorConsole from '@/components/ConnectorConsole';
import { Agent, getAgents } from '@/lib/supabase';

const PROVIDER_ICONS: Record<string, React.ReactNode> = {
  openai: <Zap className="h-4 w-4" />,
  anthropic: <Globe className="h-4 w-4" />,
  deepseek: <Server className="h-4 w-4" />,
  local: <Cpu className="h-4 w-4" />,
};

const STYLE_COLORS: Record<string, string> = {
  aggressive: 'border-red-400/20 bg-red-400/5 text-red-400',
  balanced: 'border-blue-400/20 bg-blue-400/5 text-blue-400',
  passive: 'border-green-400/20 bg-green-400/5 text-green-400',
};

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);

  useEffect(() => {
    void getAgents()
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  return (
    <div className="mx-auto max-w-7xl px-4 py-12">
      <ConnectorConsole />

      <section
        id="platform-agents"
        aria-labelledby="platform-agents-heading"
        className="mt-20 border-t border-arena-border pt-12"
      >
        <div className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-purple-300/60">
              Platform path
            </p>
            <h2 id="platform-agents-heading" className="mt-1 text-2xl font-bold text-white">
              Platform agent templates
            </h2>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500">
              Try the Arena without a local Connector. Platform agents use the current
              template and provider configuration flow.
            </p>
          </div>
          <a
            href="/arena"
            className="inline-flex items-center justify-center gap-2 rounded-xl border border-purple-400/20 bg-purple-400/[0.06] px-5 py-2.5 text-sm font-semibold text-purple-200 transition hover:border-purple-400/40 hover:bg-purple-400/10"
          >
            Preview Arena
            <ArrowRight className="h-4 w-4" />
          </a>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent, index) => (
            <motion.article
              key={agent.id}
              initial={{ opacity: 0, y: 16 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, amount: 0.25 }}
              transition={{ delay: index * 0.06 }}
              className="glow-card p-5"
            >
              <div className="mb-4 flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-purple-400/15 bg-purple-400/[0.05] text-purple-200">
                    <Bot className="h-5 w-5" />
                  </span>
                  <div className="min-w-0">
                    <h3 className="truncate font-bold text-white">{agent.name}</h3>
                    <p className="mt-0.5 flex items-center gap-1 text-xs text-gray-600">
                      {PROVIDER_ICONS[agent.llm_provider] || <Cpu className="h-3 w-3" />}
                      <span className="truncate">
                        {agent.llm_provider} · {agent.llm_model}
                      </span>
                    </p>
                  </div>
                </div>
                <span className="flex items-center gap-1 text-[10px] text-gray-600">
                  <Circle
                    className={`h-2 w-2 ${
                      agent.status === 'online'
                        ? 'fill-green-400 text-green-400'
                        : agent.status === 'in_battle'
                          ? 'animate-pulse fill-arena-accent text-arena-accent'
                          : 'fill-gray-700 text-gray-700'
                    }`}
                  />
                  {agent.status}
                </span>
              </div>

              <div className="mb-4 flex flex-wrap gap-2">
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${
                    STYLE_COLORS[agent.negotiation_style] || ''
                  }`}
                >
                  {agent.negotiation_style}
                </span>
                <span className="rounded-full border border-arena-border bg-white/[0.025] px-2.5 py-0.5 text-[10px] text-gray-500">
                  {agent.trade_direction}
                </span>
              </div>

              <div className="grid grid-cols-3 gap-3 border-t border-arena-border pt-4">
                <div>
                  <p className="font-mono text-lg font-bold text-white">
                    {agent.elo_rating?.toFixed(0) || '1000'}
                  </p>
                  <p className="text-[10px] text-gray-600">ELO</p>
                </div>
                <div>
                  <p className="font-mono text-lg font-bold text-white">
                    {agent.battles_fought || 0}
                  </p>
                  <p className="text-[10px] text-gray-600">Battles</p>
                </div>
                <div>
                  <p className="font-mono text-lg font-bold text-white">
                    {(
                      ((agent.battles_won || 0) / Math.max(agent.battles_fought || 1, 1)) *
                      100
                    ).toFixed(0)}
                    %
                  </p>
                  <p className="text-[10px] text-gray-600">Win rate</p>
                </div>
              </div>
            </motion.article>
          ))}

          {agents.length === 0 && (
            <div className="col-span-full rounded-xl border border-dashed border-arena-border py-14 text-center">
              <Bot className="mx-auto h-8 w-8 text-gray-700" />
              <h3 className="mt-4 font-semibold text-white">No platform agents yet</h3>
              <p className="mt-2 text-sm text-gray-600">
                Create a template agent to try the Arena without local setup.
              </p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
