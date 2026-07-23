'use client';

import { motion } from 'framer-motion';

const TIER_CONFIG: Record<string, { color: string; glow: string; icon: string }> = {
  master:  { color: 'tier-master', glow: 'shadow-[0_0_20px_rgba(192,96,240,0.5)]', icon: '👑' },
  diamond: { color: 'tier-diamond', glow: 'shadow-[0_0_20px_rgba(96,192,240,0.5)]', icon: '💎' },
  gold:    { color: 'tier-gold', glow: 'shadow-[0_0_20px_rgba(240,192,96,0.5)]', icon: '🥇' },
  silver:  { color: 'tier-silver', glow: 'shadow-[0_0_15px_rgba(160,176,192,0.4)]', icon: '🥈' },
  bronze:  { color: 'tier-bronze', glow: 'shadow-[0_0_15px_rgba(192,128,80,0.4)]', icon: '🥉' },
};

export default function TierBadge({ tier, size = 'md' }: { tier: string; size?: 'sm' | 'md' | 'lg' }) {
  const config = TIER_CONFIG[tier] || TIER_CONFIG.bronze;
  const sizeClass = size === 'lg' ? 'text-4xl' : size === 'sm' ? 'text-xs' : 'text-sm';

  return (
    <motion.span
      className={`inline-flex items-center gap-1 rounded-full bg-white/5 px-3 py-1 ${sizeClass} ${config.color} ${config.glow} font-semibold uppercase tracking-wider`}
      whileHover={{ scale: 1.1 }}
    >
      {size !== 'sm' && <span>{config.icon}</span>}
      {tier}
    </motion.span>
  );
}
