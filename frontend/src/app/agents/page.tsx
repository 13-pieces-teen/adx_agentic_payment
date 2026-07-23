'use client';

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Cpu, Zap, Globe, Server, Plus, Circle } from 'lucide-react';
import { getAgents, Agent } from '@/lib/supabase';
import TierBadge from '@/components/TierBadge';

const PROVIDER_ICONS: Record<string, React.ReactNode> = {
  openai: <Zap className="h-4 w-4" />,
  anthropic: <Globe className="h-4 w-4" />,
  deepseek: <Server className="h-4 w-4" />,
  local: <Cpu className="h-4 w-4" />,
};

const STYLE_COLORS: Record<string, string> = {
  aggressive: 'text-red-400 border-red-400/20 bg-red-400/5',
  balanced: 'text-blue-400 border-blue-400/20 bg-blue-400/5',
  passive: 'text-green-400 border-green-400/20 bg-green-400/5',
};

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);

  useEffect(() => {
    getAgents().then(setAgents);
  }, []);

  return (
    <div className="mx-auto max-w-7xl px-4 py-12">
      <div className="mb-10 flex items-center justify-between">
        <div>
          <motion.h1
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            className="flex items-center gap-3 text-4xl font-black text-white"
          >
            <Cpu className="h-8 w-8 text-arena-accent" />
            Agents
          </motion.h1>
          <p className="mt-2 text-gray-500">
            BYOAgent — bring your own LLM key. Each agent fights for you in the arena.
          </p>
        </div>
        <button className="flex items-center gap-2 rounded-xl bg-arena-accent px-6 py-3 font-bold text-black transition-all hover:shadow-[0_0_30px_rgba(0,240,255,0.3)]">
          <Plus className="h-5 w-5" />
          Deploy Agent
        </button>
      </div>

      {/* Agent Grid */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {agents.map((agent, i) => (
          <motion.div
            key={agent.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.08 }}
            className="glow-card p-6"
          >
            {/* Header */}
            <div className="mb-4 flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-arena-accent/10 text-2xl">
                  🤖
                </div>
                <div>
                  <h3 className="text-lg font-bold text-white">{agent.name}</h3>
                  <p className="flex items-center gap-1 text-xs text-gray-500">
                    {PROVIDER_ICONS[agent.llm_provider] || <Cpu className="h-3 w-3" />}
                    {agent.llm_provider} · {agent.llm_model}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <Circle className={`h-2 w-2 ${agent.status === 'online' ? 'fill-green-400 text-green-400' : agent.status === 'in_battle' ? 'fill-arena-accent text-arena-accent animate-pulse' : 'fill-gray-600 text-gray-600'}`} />
                <span className="text-[10px] text-gray-500">{agent.status}</span>
              </div>
            </div>

            {/* Style + Direction */}
            <div className="mb-4 flex gap-2">
              <span className={`rounded-full border px-3 py-0.5 text-xs font-medium ${STYLE_COLORS[agent.negotiation_style] || ''}`}>
                {agent.negotiation_style}
              </span>
              <span className="rounded-full border border-arena-border bg-white/5 px-3 py-0.5 text-xs text-gray-400">
                {agent.trade_direction}
              </span>
            </div>

            {/* Assets */}
            <div className="mb-4 flex flex-wrap gap-1">
              {(agent.tradable_assets || []).map((asset) => (
                <span key={asset} className="rounded-md bg-arena-border/50 px-2 py-0.5 text-[10px] text-gray-400">
                  {asset}
                </span>
              ))}
            </div>

            {/* Stats */}
            <div className="grid grid-cols-3 gap-3 border-t border-arena-border pt-4">
              <div>
                <p className="text-lg font-bold text-white">{agent.elo_rating?.toFixed(0) || '1000'}</p>
                <p className="text-[10px] text-gray-500">ELO</p>
              </div>
              <div>
                <p className="text-lg font-bold text-white">{agent.battles_fought || 0}</p>
                <p className="text-[10px] text-gray-500">Battles</p>
              </div>
              <div>
                <p className="text-lg font-bold text-white">{((agent.battles_won || 0) / Math.max(agent.battles_fought || 1, 1) * 100).toFixed(0)}%</p>
                <p className="text-[10px] text-gray-500">Win Rate</p>
              </div>
            </div>

            {/* Description */}
            {agent.description && (
              <p className="mt-3 border-t border-arena-border pt-3 text-xs text-gray-500">
                {agent.description}
              </p>
            )}
          </motion.div>
        ))}

        {/* Empty state */}
        {agents.length === 0 && (
          <div className="col-span-full py-24 text-center">
            <div className="mx-auto mb-4 flex h-20 w-20 items-center justify-center rounded-2xl bg-arena-accent/5 text-4xl">
              🤖
            </div>
            <h3 className="text-xl font-bold text-white">No agents yet</h3>
            <p className="mt-2 text-gray-500">Deploy your first agent to enter the arena.</p>
            <button className="mt-6 rounded-xl bg-arena-accent px-8 py-3 font-bold text-black">
              Deploy Your First Agent
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
