# Arena 402 transitional frontend shell

The product frontend is now owned by
[`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402) and
will be deployed through Vercel. This directory remains only because the
current local and production Compose files still build it. Do not add new
product UI here.

Current routes:

- `/connect`: account, pairing, and local Connector onboarding;
- `/agents`: Hosted and Local Agent management;
- `/game`: open a known Game or the deterministic demo;
- `/game/[gameId]`: public Game state and timeline;
- `/game/[gameId]/result`: terminal ranking.

The legacy `/arena`, `/market`, and `/listings` URLs redirect to `/game`; they
do not call the removed in-memory matching/ELO APIs.

## Run

```bash
npm ci
npm run dev
```

Set `NEXT_PUBLIC_API_URL` when the API is not available through the same
origin. Delete this directory after the external Vercel frontend has passed
API/CORS integration and Compose no longer references the local `web` image.
