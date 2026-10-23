# Sewa Setu — Old Age Pension Portal

Handover copy. Vendor maintenance contract ended 31/01/2026.

## Running

```
docker compose up --build
```

- Portal: http://localhost:8000
- The simulated SMS gateway is internal-only and is not exposed through the
  portal. It has no host port; application requests stay on the Compose network.
- Database: Postgres, loaded from `seed/seed.sql` on first boot

For VM/VPS deployment, see [`DEPLOYMENT.md`](DEPLOYMENT.md). Do not expose
PostgreSQL, Redis, or the SMS gateway directly to the internet.

Production server logs (Jan–Jun 2026) are provided in `logs/`.

Listen to `dc-briefing.mp3` first (the DC's briefing), then read `BRIEF.md`.
