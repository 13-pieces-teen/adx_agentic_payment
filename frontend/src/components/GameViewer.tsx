'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import {
  getPawnhouseGame,
  getPawnhouseTimeline,
  PawnhouseGameState,
  PawnhouseTimelineEvent,
} from '@/lib/game-api';

const PHASES = ['registration', 'decide', 'pairing', 'negotiating', 'settling'];

const DEMO_EVENTS: PawnhouseTimelineEvent[] = [
  {
    sequence: 1,
    type: 'round.decision_submitted',
    data: { agent: 'Cassius', action: 'buy', good: 'grain' },
  },
  {
    sequence: 2,
    type: 'pairing.created',
    data: { buyer: 'Cassius', seller: 'Livia', good: 'grain' },
  },
  {
    sequence: 3,
    type: 'negotiation.proposed',
    data: { price: '2.4', message: 'Two sacks before the northern gate closes.' },
  },
  {
    sequence: 4,
    type: 'negotiation.accepted',
    data: { price: '2.4', quantity: '2' },
  },
  {
    sequence: 5,
    type: 'settlement.confirmed',
    data: { network: 'Injective EVM testnet' },
  },
];

function eventSummary(data: Record<string, unknown>): string {
  return Object.entries(data)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(' · ');
}

export default function GameViewer({ gameId }: { gameId: string }) {
  const demo = gameId === 'demo';
  const [state, setState] = useState<PawnhouseGameState | null>(
    demo
      ? {
          gameId: 'demo',
          phase: 'negotiating',
          currentRound: 3,
          totalRounds: 8,
          schemaVersion: 'arena.pawnhouse-game-state.v1',
        }
      : null,
  );
  const [events, setEvents] = useState<PawnhouseTimelineEvent[]>(
    demo ? DEMO_EVENTS : [],
  );
  const [error, setError] = useState('');

  useEffect(() => {
    if (demo) return;
    let after = 0;
    let stopped = false;

    async function refresh(signal?: AbortSignal) {
      try {
        const [nextState, timeline] = await Promise.all([
          getPawnhouseGame(gameId, signal),
          getPawnhouseTimeline(gameId, after, signal),
        ]);
        if (stopped) return;
        setState(nextState);
        if (timeline.events.length > 0) {
          after = timeline.nextAfter;
          setEvents((current) => {
            const merged = [...current, ...timeline.events];
            return merged
              .filter(
                (event, index, rows) =>
                  rows.findIndex((candidate) => candidate.sequence === event.sequence) ===
                  index,
              )
              .slice(-80);
          });
        }
        setError('');
      } catch (cause) {
        if (!stopped && !(cause instanceof DOMException && cause.name === 'AbortError')) {
          setError('This game is not available from the public Arena API yet.');
        }
      }
    }

    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = window.setInterval(() => void refresh(), 3_000);
    return () => {
      stopped = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [demo, gameId]);

  const activePhase = String(state?.phase || 'registration').toLowerCase();

  return (
    <section className="gm">
      <Link className="back-btn" href="/game">
        ← Lobby
      </Link>
      <header className="gm-head">
        <div>
          <p className="label">
            King&apos;s Pawnhouse · Game {gameId}
            {demo ? ' · Demo feed' : ''}
          </p>
          <h1 className="display gm-title">
            Round {String(state?.currentRound || 0).padStart(2, '0')}
            <span className="gm-title-total">
              / {String(state?.totalRounds || 0).padStart(2, '0')}
            </span>
          </h1>
        </div>
        <div className="gm-machine" aria-label={`Current phase: ${activePhase}`}>
          {PHASES.map((phase, index) => (
            <span key={phase}>
              <span className={`gm-phase ${activePhase === phase ? 'active' : ''}`}>
                {phase}
              </span>
              {index < PHASES.length - 1 && (
                <span className="gm-phase-arrow" aria-hidden="true">
                  →
                </span>
              )}
            </span>
          ))}
        </div>
      </header>

      {error && <p className="data-state error">{error}</p>}

      <div className="game-demo-board">
        <article className="game-demo-column">
          <h3>Agents</h3>
          <p>
            Each participant view is frozen when the Agent joins the Game.
            Runtime configuration cannot change mid-game.
          </p>
          <p className="label">Cassius · deciding</p>
          <p className="label">Livia · negotiating</p>
          <p className="label">Marius · pooled</p>
        </article>
        <article className="game-demo-column">
          <h3>Market</h3>
          <p>GRAIN · 2.0 reference</p>
          <p>IRON · 5.5 reference</p>
          <p>WARHORSE · 8.0 reference</p>
          <p>GEMS · 4.2 reference</p>
        </article>
        <article className="game-demo-column">
          <h3>Negotiation</h3>
          <p>
            Public negotiation messages are untrusted, sanitized, and limited to
            the versioned propose / accept / reject action contract.
          </p>
          <div className="term">
            <span className="prompt">$</span> arena402 tail --game {gameId}
            <br />
            <span className="cursor">▌</span>
          </div>
        </article>
      </div>

      <section style={{ marginTop: 40 }}>
        <p className="label gm-col-head">Immutable event timeline</p>
        <div className="timeline-list">
          {events.map((event) => (
            <div className="timeline-event" key={event.sequence}>
              <code>#{String(event.sequence).padStart(3, '0')}</code>
              <div>
                <strong>{event.type}</strong>
                <span>{eventSummary(event.data) || 'Recorded by Arena'}</span>
              </div>
            </div>
          ))}
          {events.length === 0 && <p className="empty">Waiting for Arena events</p>}
        </div>
      </section>
    </section>
  );
}
