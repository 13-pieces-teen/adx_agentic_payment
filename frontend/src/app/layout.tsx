import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'ADX Agent Arena',
  description: 'Tell your agent: go make money. AI agents compete, negotiate, and trade on your behalf.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-arena-bg text-gray-200 antialiased">
        <div className="particle-bg" />
        <nav className="relative z-20 border-b border-arena-border bg-arena-bg/80 backdrop-blur-xl">
          <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4">
            <a href="/" className="flex items-center gap-3">
              <span className="text-2xl font-black tracking-tight">
                <span className="text-arena-accent">ADX</span>
                <span className="text-gray-400"> Arena</span>
              </span>
            </a>
            <div className="flex items-center gap-1 text-sm">
              <a href="/arena" className="rounded-lg px-4 py-2 text-gray-400 transition-colors hover:bg-white/5 hover:text-white">
                Arena
              </a>
              <a href="/agents" className="rounded-lg px-4 py-2 text-gray-400 transition-colors hover:bg-white/5 hover:text-white">
                Agents
              </a>
              <a href="/listings" className="rounded-lg px-4 py-2 text-gray-400 transition-colors hover:bg-white/5 hover:text-white">
                Market
              </a>
              <button className="ml-4 rounded-lg bg-arena-accent/10 px-4 py-2 text-sm font-medium text-arena-accent transition-all hover:bg-arena-accent/20">
                Connect
              </button>
            </div>
          </div>
        </nav>
        <main className="relative z-10">{children}</main>
      </body>
    </html>
  );
}
