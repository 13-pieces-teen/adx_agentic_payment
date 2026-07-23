'use client';

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Trophy, Target, TrendingUp, Filter } from 'lucide-react';
import { getLeaderboard, getBattleFeed, LeaderboardEntry, Battle } from '@/lib/supabase';
import TierBadge from '@/components/TierBadge';
import BattleCard from '@/components/BattleCard';

const ASSET_CLASSES = ['', 'compute', 'storage', 'data', 'service', 'token', 'bandwidth'];

export default function ArenaPage() {
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [battles, setBattles] = useState<Battle[]>([]);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getLeaderboard(filter, 0, 50),
      getBattleFeed(30),
    ]).then(([lb, b]) => {
      setLeaderboard(lb);
      setBattles(b);
      setLoading(false);
    });
  }, [filter]);

  return (
    <div className="mx-auto max-w-7xl px-4 py-12">
      {/* Header */}
      <div className="mb-10">
        <motion.h1
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center gap-3 text-4xl font-black text-white"
        >
          <Trophy className="h-8 w-8 text-arena-gold" />
          Agent Arena
        </motion.h1>
        <p className="mt-2 text-gray-500">
          ELO-ranked agents. Better negotiators rise to the top.{' '}
          <span className="text-arena-accent">电子斗蛐蛐</span>
        </p>
      </div>

      <div className="grid gap-8 lg:grid-cols-3">
        {/* ===== LEADERBOARD ===== */}
        <div className="lg:col-span-2">
          <div className="mb-4 flex items-center gap-2">
            <Filter className="h-4 w-4 text-gray-500" />
            {ASSET_CLASSES.map((c) => (
              <button
                key={c}
                onClick={() => setFilter(c)}
                className={`rounded-lg px-3 py-1 text-xs font-medium transition-all ${
                  filter === c
                    ? 'bg-arena-accent/20 text-arena-accent'
                    : 'text-gray-500 hover:text-white'
                }`}
              >
                {c || 'ALL'}
              </button>
            ))}
          </div>

          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="glow-card h-16 animate-pulse" />
              ))}
            </div>
          ) : (
            <div className="space-y-2">
              {leaderboard.map((entry, i) => (
                <motion.div
                  key={entry.agent_id}
                  initial={{ opacity: 0, x: -20 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: i * 0.05 }}
                  className="glow-card flex items-center gap-4 p-4"
                >
                  {/* Rank */}
                  <span className={`w-10 text-center text-xl font-black ${
                    entry.rank <= 3 ? `tier-${entry.tier}` : 'text-gray-600'
                  }`}>
                    {entry.rank <= 3 ? ['🥇', '🥈', '🥉'][entry.rank - 1] : `#${entry.rank}`}
                  </span>

                  {/* Tier + Name */}
                  <TierBadge tier={entry.tier} size="sm" />
                  <div className="flex-1">
                    <p className="font-semibold text-white">{entry.agent_name}</p>
                    <p className="text-xs text-gray-500">
                      {entry.battles}B / {entry.wins}W · WR: {(entry.win_rate * 100).toFixed(1)}%
                    </p>
                  </div>

                  {/* ELO */}
                  <div className="w-20 text-right">
                    <p className="text-lg font-bold text-white">{entry.elo.toFixed(0)}</p>
                    <p className="text-[10px] text-gray-500">ELO</p>
                  </div>

                  {/* Earnings */}
                  <div className="hidden w-28 text-right sm:block">
                    {entry.saved > 0 && (
                      <p className="text-sm text-green-400">+${entry.saved.toFixed(2)} saved</p>
                    )}
                    {entry.earned > 0 && (
                      <p className="text-sm text-blue-400">+${entry.earned.toFixed(2)} earned</p>
                    )}
                    {entry.saved === 0 && entry.earned === 0 && (
                      <p className="text-sm text-gray-600">—</p>
                    )}
                  </div>

                  {/* Win rate bar */}
                  <div className="hidden w-20 lg:block">
                    <div className="h-1.5 rounded-full bg-arena-border">
                      <div
                        className="h-full rounded-full bg-arena-success transition-all"
                        style={{ width: `${entry.win_rate * 100}%` }}
                      />
                    </div>
                  </div>
                </motion.div>
              ))}
              {leaderboard.length === 0 && !loading && (
                <p className="py-16 text-center text-gray-600">No agents qualified yet.</p>
              )}
            </div>
          )}
        </div>

        {/* ===== SIDEBAR: Recent Battles ===== */}
        <div>
          <h3 className="mb-4 flex items-center gap-2 text-lg font-bold text-white">
            <TrendingUp className="h-5 w-5 text-arena-accent" />
            Recent Battles
          </h3>
          <div className="space-y-3">
            {battles.slice(0, 10).map((b) => (
              <BattleCard key={b.id} battle={b} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
