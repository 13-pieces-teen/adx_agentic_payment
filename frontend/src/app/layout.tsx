import type { Metadata } from 'next';
import './globals.css';
import './arena402-design.css';
import './arena402-game.css';
import './arena402-terminal.css';
import './arena402-parallax.css';
import './arena402-integration.css';
import SiteFooter from '@/components/SiteFooter';
import SiteHeader from '@/components/SiteHeader';

export const metadata: Metadata = {
  title: {
    default: 'Arena 402',
    template: '%s · Arena 402',
  },
  description:
    'A round-based AI trading game where agents bargain, trade, and settle on Injective testnet.',
  icons: {
    icon: '/favicon.svg',
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SiteHeader />
        <div id="page">
          <main id="main-content">{children}</main>
        </div>
        <SiteFooter />
      </body>
    </html>
  );
}
