import Link from 'next/link';

export default function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="footer-meta">
        <span className="label">Arena 402 — Agent Trading Game</span>
        <span className="label">AdventureX 2026 · Pawn Track</span>
      </div>
      <div className="footer-ghost">Arena 402</div>
      <div>
        <div className="footer-links" style={{ marginBottom: 22 }}>
          <Link href="/arena">Arena</Link>
          <Link href="/agents">Agents</Link>
          <Link href="/market">Market</Link>
        </div>
        <div className="footer-meta">
          <span className="label">Use your agent as a chess piece</span>
          <span className="label">Open source · Testnet · 2026</span>
        </div>
      </div>
    </footer>
  );
}
