import Link from 'next/link';
import GameLobby from '@/components/GameLobby';

export default function GameLobbyPage() {
  return (
    <div className="site-main">
      <section className="gm gm-lobby">
        <Link className="back-btn" href="/">
          ← Back
        </Link>
        <header className="gm-head">
          <div>
            <p className="label">Black Market · Lobby</p>
            <h1 className="display gm-title">The Bazaar</h1>
            <p className="sec-sub">
              Open a known Game or watch the deterministic frontend demo.
            </p>
          </div>
        </header>
        <GameLobby />
      </section>
    </div>
  );
}
