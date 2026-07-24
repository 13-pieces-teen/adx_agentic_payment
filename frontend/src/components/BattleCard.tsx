'use client';

import { motion } from 'framer-motion';
import { Swords, Clock, Target, Zap } from 'lucide-react';
import { Battle } from '@/lib/arena-api';

const OUTCOME_EMOJI: Record<string, string> = {
  buyer_win: '🟢',
  seller_win: '🔴',
  draw: '🟡',
  buyer_surrender: '🏳️',
  seller_surrender: '🏳️',
  timeout: '⏰',
};

const OUTCOME_LABEL: Record<string, string> = {
  buyer_win: 'BUYER WIN',
  seller_win: 'SELLER WIN',
  draw: 'DRAW',
  buyer_surrender: 'SURRENDER',
  seller_surrender: 'SURRENDER',
  timeout: 'TIMEOUT',
};

export default function BattleCard({ battle, isLive = false }: { battle: Battle; isLive?: boolean }) {
  const eloA = battle.agent_a_elo_delta || 0;
  const eloB = battle.agent_b_elo_delta || 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`glow-card p-4 ${isLive ? 'border-arena-accent/50 animate-glow' : ''}`}
    >
      <div className="flex items-center justify-between">
        {/* Left fighter */}
        <div className="flex items-center gap-3">
          <span className="text-2xl">🤖</span>
          <div>
            <p className="font-semibold text-white">{battle.agent_a_name}</p>
            <p className={`text-xs ${eloA >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {eloA >= 0 ? '+' : ''}{eloA.toFixed(0)} ELO
            </p>
          </div>
        </div>

        {/* VS */}
        <div className="flex flex-col items-center">
          <div className="flex items-center gap-2">
            <Swords className="h-4 w-4 text-gray-500" />
            <span className="text-xs font-bold text-gray-400">{OUTCOME_LABEL[battle.outcome] || battle.outcome}</span>
          </div>
          <span className="mt-1 text-lg font-bold text-white">
            {battle.final_price} {battle.currency}
          </span>
          <span className="text-xs text-gray-500">
            {battle.rounds_taken}r · {(battle.duration_seconds || 0).toFixed(1)}s
          </span>
        </div>

        {/* Right fighter */}
        <div className="flex items-center gap-3 text-right">
          <div>
            <p className="font-semibold text-white">{battle.agent_b_name}</p>
            <p className={`text-xs ${eloB >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {eloB >= 0 ? '+' : ''}{eloB.toFixed(0)} ELO
            </p>
          </div>
          <span className="text-2xl">🤖</span>
        </div>
      </div>

      {/* Footer */}
      <div className="mt-3 flex items-center gap-4 border-t border-arena-border pt-3 text-xs text-gray-500">
        <span className="flex items-center gap-1">
          <Target className="h-3 w-3" />
          {battle.asset_class} · {battle.quantity}u
        </span>
        <span className="flex items-center gap-1">
          <Clock className="h-3 w-3" />
          {new Date(battle.ended_at).toLocaleTimeString()}
        </span>
        {isLive && (
          <span className="flex items-center gap-1 text-arena-accent">
            <Zap className="h-3 w-3 animate-pulse" />
            LIVE
          </span>
        )}
      </div>
    </motion.div>
  );
}
