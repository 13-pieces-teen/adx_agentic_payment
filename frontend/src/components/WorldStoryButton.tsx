'use client';

import { useEffect, useState } from 'react';

export default function WorldStoryButton() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [open]);

  return (
    <>
      <button type="button" className="btn ghost sm" onClick={() => setOpen(true)}>
        ⚜ The World
      </button>
      {open && (
        <div
          className="world-modal-overlay"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
        >
          <section
            className="world-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="world-story-title"
          >
            <button
              type="button"
              className="world-modal-close"
              aria-label="Close world story"
              onClick={() => setOpen(false)}
            >
              ×
            </button>
            <h2 id="world-story-title">The King&apos;s Pawnhouse</h2>
            <p className="world-quote">
              “In chaos, the best business is done. Enter the Pawnhouse.”
            </p>
            <h3>The World</h3>
            <p>
              402 AD. Aurelia, the Golden Kingdom, is falling. Grain prices triple
              by the hour, soldiers&apos; pay turns worthless, and nobles pawn their
              ancestral jewels. One market still asks no questions: the
              King&apos;s Pawnhouse.
            </p>
            <h3>You Are A Pawn</h3>
            <p>
              Your AI is the merchant you send into the market. It reads the
              events, chooses what to buy or sell, bargains with another pawn, and
              survives the final repricing.
            </p>
            <p>
              A pawn looks expendable until it reaches the far end of the board.
              Then it becomes a king.
            </p>
          </section>
        </div>
      )}
    </>
  );
}
