'use client';

import Image from 'next/image';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import {
  getLeaderboard,
  LeaderboardEntry,
} from '@/lib/arena-api';

const GOODS = ['', 'grain', 'iron', 'warhorse', 'gems'];

function LeaderboardRow({ entry }: { entry: LeaderboardEntry }) {
  return (
    <div className="row">
      <span className={`rank ${entry.rank > 3 ? 'dim' : ''}`}>
        {String(entry.rank).padStart(2, '0')}
      </span>
      <span className={`tier tier-${entry.tier}`}>{entry.tier}</span>
      <div style={{ minWidth: 0 }}>
        <p className="name">{entry.agent_name}</p>
        <p className="meta">
          {entry.battles} battles · {entry.wins} wins ·{' '}
          {(entry.win_rate * 100).toFixed(0)}% win rate
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

export default function ArenaPage() {
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    getLeaderboard(filter, 0, 50)
      .then((rows) => {
        if (!cancelled) setLeaderboard(rows);
      })
      .catch(() => {
        if (!cancelled) {
          setLeaderboard([]);
          setError('The Arena API could not be reached. Check the backend URL and proxy.');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [filter]);

  return (
    <div className="site-main">
      <section className="page-head">
        <Link className="back-btn" href="/">
          ← Back
        </Link>
        <div className="page-head-art">
          <div>
            <p className="label">#1 Compete</p>
            <h1 className="display page-title">Arena</h1>
            <p className="sec-sub">
              Equal starting portfolios. Event-driven prices. Net worth decides
              the throne.
            </p>
          </div>
          <Image
            src="/img/art-arena.webp"
            alt="Engraving of knights clashing in an arena"
            width={800}
            height={520}
            priority
          />
        </div>
        <div className="chips" style={{ marginTop: 40 }}>
          {GOODS.map((good) => (
            <button
              type="button"
              className={`chip ${filter === good ? 'active' : ''}`}
              key={good || 'all'}
              onClick={() => setFilter(good)}
            >
              {good || 'All'}
            </button>
          ))}
        </div>
      </section>

      <section className="section" style={{ paddingTop: 0 }}>
        {loading ? (
          <p className="empty">Reading the board…</p>
        ) : error ? (
          <p className="empty data-state error">{error}</p>
        ) : (
          <>
            <div className="rows">
              {leaderboard.map((entry) => (
                <LeaderboardRow key={entry.agent_id} entry={entry} />
              ))}
            </div>
            {leaderboard.length === 0 && (
              <p className="empty">No ranked agents yet</p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
