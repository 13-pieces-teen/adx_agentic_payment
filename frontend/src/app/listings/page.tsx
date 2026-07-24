'use client';

import Image from 'next/image';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import { getListings, Listing } from '@/lib/arena-api';

const ASSET_CLASSES = ['', 'grain', 'iron', 'warhorse', 'gems'];

export default function MarketPage() {
  const [listings, setListings] = useState<Listing[]>([]);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    getListings(filter || undefined)
      .then((rows) => {
        if (!cancelled) setListings(rows);
      })
      .catch(() => {
        if (!cancelled) {
          setListings([]);
          setError('The market API could not be reached. Check the backend URL and proxy.');
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
            <p className="label">#3 Trade</p>
            <h1 className="display page-title">Market</h1>
            <p className="sec-sub">
              Goods enter buy and sell pools before first-come-first-served
              pairing.
            </p>
          </div>
          <Image
            src="/img/art-market.webp"
            alt="Engraving of Hermes presiding over a marketplace"
            width={800}
            height={520}
            priority
          />
        </div>
        <div className="chips" style={{ marginTop: 40 }}>
          {ASSET_CLASSES.map((asset) => (
            <button
              type="button"
              className={`chip ${filter === asset ? 'active' : ''}`}
              key={asset || 'all'}
              onClick={() => setFilter(asset)}
            >
              {asset || 'All'}
            </button>
          ))}
        </div>
      </section>

      <section className="section" style={{ paddingTop: 0 }}>
        {loading ? (
          <p className="empty">Opening the market…</p>
        ) : error ? (
          <p className="empty data-state error">{error}</p>
        ) : (
          <>
            <div className="grid-2">
              {listings.map((listing) => (
                <article className="card" key={listing.id}>
                  <p className="label" style={{ fontSize: 8, marginBottom: 10 }}>
                    {listing.asset_class} · {listing.seller_name}
                  </p>
                  <h3 style={{ marginBottom: 10 }}>{listing.title}</h3>
                  <p
                    style={{
                      color: 'var(--grey)',
                      fontSize: 12,
                      lineHeight: 1.7,
                      marginBottom: 18,
                    }}
                  >
                    {listing.description}
                  </p>
                  <div
                    style={{
                      borderTop: '1px solid var(--line)',
                      marginBottom: 14,
                      paddingTop: 12,
                    }}
                  >
                    <div className="kv">
                      <span>Price range</span>
                      <b>
                        {listing.min_price}–{listing.max_price}{' '}
                        {listing.currency}
                      </b>
                    </div>
                    <div className="kv">
                      <span>Ideal</span>
                      <b>
                        {listing.ideal_price} {listing.currency}
                      </b>
                    </div>
                    <div className="kv">
                      <span>Quantity</span>
                      <b>
                        {listing.quantity} {listing.unit}
                      </b>
                    </div>
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {(listing.tags || []).slice(0, 4).map((tag) => (
                      <span className="tag" key={tag}>
                        #{tag}
                      </span>
                    ))}
                  </div>
                  <div className="market-card-meta">
                    <span>{new Date(listing.created_at).toLocaleDateString()}</span>
                    <span>{listing.is_active ? 'Open' : 'Closed'}</span>
                  </div>
                </article>
              ))}
            </div>
            {listings.length === 0 && <p className="empty">No open listings</p>}
          </>
        )}
      </section>
    </div>
  );
}
