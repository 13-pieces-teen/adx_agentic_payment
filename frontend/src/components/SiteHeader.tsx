'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import {
  ConnectorAuthSession,
  getConnectorAuthSession,
  logoutConnectorUser,
} from '@/lib/connector-api';

const PRIMARY_LINKS = [
  { href: '/arena', label: 'Arena' },
  { href: '/agents', label: 'Agents' },
  { href: '/game', label: 'Game' },
] as const;

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export default function SiteHeader() {
  const pathname = usePathname();
  const menuRef = useRef<HTMLDivElement>(null);
  const [session, setSession] = useState<ConnectorAuthSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getConnectorAuthSession()
      .then((value) => {
        if (!cancelled) setSession(value);
      })
      .catch(() => {
        if (!cancelled) setSession(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    function closeMenu(event: MouseEvent) {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    }
    document.addEventListener('click', closeMenu);
    return () => document.removeEventListener('click', closeMenu);
  }, [menuOpen]);

  async function signOut() {
    if (!session) return;
    try {
      await logoutConnectorUser(session.csrf_token);
    } finally {
      setSession(null);
      setMenuOpen(false);
    }
  }

  return (
    <nav className="nav" aria-label="Primary navigation">
      <div className="nav-links left">
        {PRIMARY_LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className={`nav-link ${isActive(pathname, link.href) ? 'active' : ''}`}
          >
            {link.label}
          </Link>
        ))}
      </div>

      <Link href="/" className="nav-brand" aria-label="Arena 402 home">
        Arena 402
      </Link>

      <div className="nav-links right">
        <Link
          href="/market"
          className={`nav-link ${isActive(pathname, '/market') ? 'active' : ''}`}
        >
          Market
        </Link>
        {loading ? (
          <span className="nav-session-loading" aria-label="Checking session" />
        ) : session ? (
          <div className="nav-session" ref={menuRef}>
            <button
              type="button"
              className="nav-user-trigger"
              aria-expanded={menuOpen}
              aria-haspopup="menu"
              onClick={() => setMenuOpen((value) => !value)}
            >
              <span className="nav-avatar nav-avatar-fallback" aria-hidden="true">
                {session.user.username.slice(0, 1)}
              </span>
              <span className="nav-user-name">{session.user.username}</span>
              <span className="nav-user-caret" aria-hidden="true">
                ▾
              </span>
            </button>
            {menuOpen && (
              <div className="nav-session-menu" role="menu">
                <p>{session.user.username}</p>
                <Link href="/agents" role="menuitem">
                  Manage agents
                </Link>
                <Link href="/connect" role="menuitem">
                  Connect computer
                </Link>
                <button type="button" role="menuitem" onClick={() => void signOut()}>
                  Sign out
                </button>
              </div>
            )}
          </div>
        ) : (
          <Link href="/connect" className="nav-signin">
            <span className="nav-signin-label">Try Now</span>
            <span className="nav-signin-rule" aria-hidden="true" />
          </Link>
        )}
      </div>
    </nav>
  );
}
