# Sporting Tickets → Discord

Checks `tickets.sporting.pt` every ~5 min (GitHub Actions) and posts to Discord when a game is listed or removed, opens to the public, sells out or comes back, changes date, or has its sale rules edited.

It reads the site's own JSON (`/api/match/allopengames`), so page redesigns don't break it.

## Settings
Repo → Settings → Secrets and variables → Actions
- Secret `DISCORD_WEBHOOK_URL` (required): Discord channel → Integrations → Webhooks → Copy URL.
- Variable `DISCORD_MENTION` (optional): `<@your-user-id>` to get a phone ping.
- Variable `MODALITIES` (optional): e.g. `Futebol` or `Futebol,Andebol`. Empty = all.

## Limitations
- GitHub cron runs every 5 min at best, often 10-20 min late.
- If Cloudflare blocks GitHub, you get a "⚠️ Watcher não consegue ler o site" message after 3 failed checks. Then run `python watch.py` on a PC/Pi instead.
- The API has no clean "on sale now" flag. Sale windows live in the rules text, so read the 📋 alerts.
