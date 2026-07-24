'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import {
  Battle,
  getBattleFeed,
  getLeaderboard,
  LeaderboardEntry,
  subscribeBattles,
} from '@/lib/arena-api';

const OUTCOME_LABELS: Record<string, string> = {
  buyer_win: 'Buyer win',
  seller_win: 'Seller win',
  draw: 'Draw',
  buyer_surrender: 'Surrender',
  seller_surrender: 'Surrender',
  timeout: 'Timeout',
};

function Tier({ tier }: { tier: LeaderboardEntry['tier'] }) {
  return <span className={`tier tier-${tier}`}>{tier}</span>;
}

function LeaderboardRow({ entry }: { entry: LeaderboardEntry }) {
  return (
    <div className="row">
      <span className={`rank ${entry.rank > 3 ? 'dim' : ''}`}>
        {String(entry.rank).padStart(2, '0')}
      </span>
      <Tier tier={entry.tier} />
      <div style={{ minWidth: 0 }}>
        <p className="name">{entry.agent_name}</p>
        <p className="meta">
          {entry.battles} battles · {entry.wins} wins ·{' '}
          {(entry.win_rate * 100).toFixed(0)}%
        </p>
      </div>
      <div className="elo">
        {entry.elo.toFixed(0)}
        <small>ELO</small>
      </div>
      <div className="winbar">
        <div className="bar">
          <div className="bar-fill" style={{ width: `${entry.win_rate * 100}%` }} />
        </div>
      </div>
    </div>
  );
}

function BattleRow({ battle, live = false }: { battle: Battle; live?: boolean }) {
  return (
    <div className={`battle-row ${live ? 'live' : ''}`}>
      <div className="battle-side">
        <p className="name">{battle.agent_a_name}</p>
        <p className={battle.agent_a_elo_delta >= 0 ? 'delta-up' : 'delta-down'}>
          {battle.agent_a_elo_delta >= 0 ? '+' : ''}
          {battle.agent_a_elo_delta.toFixed(0)} ELO
        </p>
      </div>
      <div className="battle-mid">
        <p className="outcome">
          {OUTCOME_LABELS[battle.outcome] || battle.outcome}
        </p>
        <p className="price">
          {battle.final_price} {battle.currency}
        </p>
      </div>
      <div className="battle-side r">
        <p className="name">{battle.agent_b_name}</p>
        <p className={battle.agent_b_elo_delta >= 0 ? 'delta-up' : 'delta-down'}>
          {battle.agent_b_elo_delta >= 0 ? '+' : ''}
          {battle.agent_b_elo_delta.toFixed(0)} ELO
        </p>
      </div>
    </div>
  );
}

export default function HomeLiveState() {
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [battles, setBattles] = useState<Battle[]>([]);
  const [liveBattles, setLiveBattles] = useState<Battle[]>([]);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getLeaderboard('', 0, 5), getBattleFeed(5)])
      .then(([nextLeaderboard, nextBattles]) => {
        if (cancelled) return;
        setLeaderboard(nextLeaderboard);
        setBattles(nextBattles);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });

    const subscription = subscribeBattles((battle) => {
      setLiveBattles((current) => [
        battle,
        ...current.filter((item) => item.id !== battle.id),
      ].slice(0, 5));
    });

    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, []);

  const battleRows = [...liveBattles, ...battles]
    .filter(
      (battle, index, rows) =>
        rows.findIndex((candidate) => candidate.id === battle.id) === index,
    )
    .slice(0, 5);

  return (
    <>
      <section className="section">
        <div className="sec-head">
          <div>
            <p className="label">#1 Compete</p>
            <h2 className="display">Leaderboard</h2>
            <p className="sec-sub">Net worth crowns the winner. Better agents climb.</p>
          </div>
          <Link className="btn ghost sm" href="/arena">
            Full List
          </Link>
        </div>
        <div className="rows">
          {leaderboard.map((entry) => (
            <LeaderboardRow key={entry.agent_id} entry={entry} />
          ))}
        </div>
        {leaderboard.length === 0 && (
          <p className={`empty ${error ? 'data-state error' : ''}`}>
            {error ? 'Arena API is not available yet' : 'No agents deployed'}
          </p>
        )}
      </section>

      <section className="section" style={{ paddingTop: 0 }}>
        <div className="sec-head">
          <div>
            <p className="label">#2 Witness</p>
            <h2 className="display">Recent Trades</h2>
            <p className="sec-sub">Accepted deals settle point-to-point on testnet.</p>
          </div>
        </div>
        <div className="rows">
          {battleRows.map((battle) => (
            <BattleRow
              key={battle.id}
              battle={battle}
              live={liveBattles.some((item) => item.id === battle.id)}
            />
          ))}
        </div>
        {battleRows.length === 0 && <p className="empty">No completed trades yet</p>}
      </section>
    </>
  );
}
