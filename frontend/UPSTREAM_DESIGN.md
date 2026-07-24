# Arena 402 frontend integration

The visual system in this directory is based on
[`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402) at
commit `f72dcc9239dd323e1723e2d1103c48b960a07e0a`.

The upstream design remains authoritative for:

- the `ink`, `paper`, gray, serif, and mono design tokens;
- the Instrument Serif and IBM Plex Mono type hierarchy;
- the mix-blend navigation, engraving artwork, marquee, rows, cards, terminal,
  and game-board visual patterns;
- the dark-first "luxury chess + CLI hacker" direction.

The application runtime is Next.js. Upstream browser-side Supabase access and
global JavaScript state are intentionally not carried forward. Their roles map
to the Arena 402 backend as follows:

| Upstream concern | Integrated implementation |
| --- | --- |
| `js/supabase.js` | `src/lib/arena-api.ts` and `src/lib/game-api.ts` |
| `js/auth.js` | `src/lib/connector-api.ts` and `/connect` |
| `js/game-state.js` | public game snapshot and timeline API polling |
| `js/render.js` | App Router pages and React components |
| `css/style.css` | `src/app/arena402-design.css` |
| `css/game.css` | `src/app/arena402-game.css` |
| `css/terminal.css` | `src/app/arena402-terminal.css` |

Do not add a second frontend runtime or reintroduce direct database access from
the browser. New UI should reuse the upstream design tokens and component
patterns, while all business state continues to flow through Arena-owned API
boundaries.

## API routing

The browser calls same-origin `/api/*` paths. During local development,
Next.js proxies them to `http://127.0.0.1:8000`; on Vercel the default target is
`https://api.arena402.com`. Override either target with `API_PROXY_TARGET`.
Keep `NEXT_PUBLIC_API_URL` blank unless deliberately testing direct cross-origin
requests.

The backend mounts public game reads only when
`ADX_ARENA_CORE_ENABLED=true`. This exposes `/api/v1/pawnhouse/*` reads without
enabling the development mutation surface under `/api/dev/*`.
