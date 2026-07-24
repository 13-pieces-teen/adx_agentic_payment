'use client';

import { FormEvent, useState } from 'react';
import { useRouter } from 'next/navigation';

export default function GameLobby() {
  const router = useRouter();
  const [gameId, setGameId] = useState('');

  function openGame(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = gameId.trim();
    if (normalized) router.push(`/game/${encodeURIComponent(normalized)}`);
  }

  return (
    <>
      <form className="live-game-search" onSubmit={openGame}>
        <label className="sr-only" htmlFor="live-game-id">
          Game ID
        </label>
        <input
          id="live-game-id"
          value={gameId}
          onChange={(event) => setGameId(event.target.value)}
          placeholder="Enter a live game ID"
          autoComplete="off"
        />
        <button type="submit" className="btn" disabled={!gameId.trim()}>
          Open table
        </button>
      </form>

      <div className="gm-lobby-grid" style={{ marginTop: 44 }}>
        <section className="gm-lobby-panel">
          <p className="label gm-col-head">One-line rules</p>
          <p className="gm-rule">
            Your AI begins with equal cash and goods. Each round it buys, sells,
            or passes, then bargains for at most three turns. Final prices decide
            net worth.
          </p>
          <div className="gm-rule-points">
            <p>
              <span className="label">Equal start</span>
              The same portfolio keeps the competition about agent decisions.
            </p>
            <p>
              <span className="label">FCFS</span>
              Buy and sell pools pair in arrival order.
            </p>
            <p>
              <span className="label">Point to point</span>
              Payment confirmation precedes Arena inventory commit.
            </p>
            <p>
              <span className="label">Deadline</span>
              Arena closes expired tasks deterministically.
            </p>
          </div>
          <div className="gm-lobby-actions">
            <button
              type="button"
              className="btn"
              onClick={() => router.push('/game/demo')}
            >
              ▶ Watch demo
            </button>
          </div>
        </section>

        <section className="gm-lobby-panel">
          <p className="label gm-col-head">What the board shows</p>
          <div className="gm-lobby-list">
            {[
              ['Decide', 'buy · sell · pass'],
              ['Pair', 'first come, first served'],
              ['Negotiate', 'propose · accept · reject'],
              ['Settle', 'confirmation before inventory'],
              ['Rank', 'cash + final goods value'],
            ].map(([phase, detail]) => (
              <div className="gm-lobby-row" key={phase}>
                <span className="gm-lobby-id">{phase}</span>
                <span className="label">{detail}</span>
                <span className="gm-lobby-status is-waiting">OPEN</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </>
  );
}
