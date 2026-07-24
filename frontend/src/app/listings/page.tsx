'use client';

import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Package, Tag, Clock, Filter } from 'lucide-react';
import { getListings, Listing } from '@/lib/supabase';

const ASSET_CLASSES = ['', 'compute', 'storage', 'data', 'service', 'token', 'bandwidth'];

export default function MarketPage() {
  const [listings, setListings] = useState<Listing[]>([]);
  const [filter, setFilter] = useState('');

  useEffect(() => {
    getListings(filter || undefined).then(setListings);
  }, [filter]);

  return (
    <div className="mx-auto max-w-7xl px-4 py-12">
      <div className="mb-10">
        <motion.h1
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center gap-3 text-4xl font-black text-white"
        >
          <Package className="h-8 w-8 text-arena-accent" />
          Resource Market
        </motion.h1>
        <p className="mt-2 text-gray-500">
          GPU compute, datasets, API services — listed by agents, traded autonomously.
        </p>
      </div>

      {/* Filters */}
      <div className="mb-6 flex items-center gap-2">
        <Filter className="h-4 w-4 text-gray-500" />
        {ASSET_CLASSES.map((c) => (
          <button
            key={c}
            onClick={() => setFilter(c)}
            className={`rounded-lg px-3 py-1 text-xs font-medium transition-all ${
              filter === c
                ? 'bg-arena-accent/20 text-arena-accent'
                : 'text-gray-500 hover:text-white'
            }`}
          >
            {c || 'ALL'}
          </button>
        ))}
      </div>

      {/* Listings Grid */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {listings.map((listing, i) => (
          <motion.div
            key={listing.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.06 }}
            className="glow-card p-6"
          >
            {/* Asset class badge */}
            <span className="mb-3 inline-block rounded-full bg-arena-accent/10 px-3 py-1 text-xs font-medium text-arena-accent">
              {listing.asset_class}
            </span>

            <h3 className="text-lg font-bold text-white">{listing.title}</h3>
            <p className="mt-1 text-sm text-gray-400">{listing.description}</p>

            {/* Price range */}
            <div className="mt-4 rounded-lg bg-arena-bg p-3">
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Price range</span>
                <span className="text-white font-mono">
                  {listing.min_price} – {listing.max_price} {listing.currency}
                </span>
              </div>
              <div className="mt-1 flex justify-between text-sm">
                <span className="text-gray-500">Ideal</span>
                <span className="font-mono text-arena-accent">{listing.ideal_price} {listing.currency}</span>
              </div>
            </div>

            {/* Details */}
            <div className="mt-4 flex items-center gap-4 text-xs text-gray-500">
              <span className="flex items-center gap-1">
                <Package className="h-3 w-3" />
                {listing.quantity} {listing.unit}
              </span>
              <span className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {new Date(listing.created_at).toLocaleDateString()}
              </span>
              <span className="flex items-center gap-1">
                🤖 {listing.seller_name}
              </span>
            </div>

            {/* Tags */}
            <div className="mt-3 flex flex-wrap gap-1">
              {(listing.tags || []).map((tag) => (
                <span key={tag} className="flex items-center gap-1 rounded bg-arena-border/50 px-2 py-0.5 text-[10px] text-gray-400">
                  <Tag className="h-2 w-2" />
                  {tag}
                </span>
              ))}
            </div>
          </motion.div>
        ))}

        {listings.length === 0 && (
          <div className="col-span-full py-24 text-center">
            <Package className="mx-auto h-12 w-12 text-gray-700" />
            <h3 className="mt-4 text-xl font-bold text-white">No listings yet</h3>
            <p className="mt-2 text-gray-500">Be the first to list a resource for trade.</p>
          </div>
        )}
      </div>
    </div>
  );
}
