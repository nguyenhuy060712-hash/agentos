# Agent OS showcase site (show-agentos.mozaai.one)

Static product site for **Agent OS Chat** and **Agent OS** (MozaAI). Vietnamese at `/`, English at `/en/`.
Not deployed anywhere yet; build output is `dist/` (pure static files, no server code, no trackers, no external requests, self-hosted fonts).

## Build

Requires Node >= 22.22 (Astro 7). On the box: `export PATH=$HOME/.local/node24/bin:$PATH`.

```
npm ci
npm run build        # -> dist/
npm run og           # regenerate public/og-vi.png, public/og-en.png (headless Chrome)
npm run shots        # serve dist on 127.0.0.1:4321 first; writes shots/*.png
```

## Stack (versions checked on npm, 21/10/2026, ICT)

| Package | Version | Published (ICT) |
|---|---|---|
| astro | 7.4.1 | 19/10/2026 21:13 |
| tailwindcss / @tailwindcss/vite | 4.3.3 | 16/07/2026 19:03 |
| @astrojs/sitemap | 3.7.4 | 01/09/2026 02:39 |
| sharp (image optimisation) | 0.35.5 | 27/09/2026 20:46 |
| @fontsource-variable/inter, @fontsource/be-vietnam-pro | 5.3.0 | 19/07/2026 |
| playwright-core (dev, screenshots/OG) | 1.64.1 | 20/10/2026 12:24 |
| Node.js LTS (build) | 24.22.0 | 14/10/2026 (nodejs.org) |

## Content sources

All facts and numbers come from the internal handoff docs (KNOWLEDGE.md, agentos_ui_roadmap_status.md,
phase0_report.md, agentos_gap_vs_chatgpt_langchain.md, 19–21/10/2026). Roadmap data: `src/data/roadmap.ts`.
Copy: `src/data/content.ts`. Screenshots: `src/assets/shots/` (chat UI from the VPS and local test runs;
Agent OS admin from the local test instance with test data, username footer masked).

## Deployment on VPS #83 (21/10/2026, no domain yet)

- Files: `/opt/agentos-show` (previous copies `/opt/agentos-show.prev_*`).
- Web server: a **separate** Caddy instance `caddy-show.service`, config `/etc/caddy-show/Caddyfile`, `127.0.0.1:8093`
  (static, zstd/gzip, security headers incl. CSP, `/_astro/*` cached 1 year immutable, HTML 5 min).
  The main Caddy runs with `admin off`, so `caddy reload` is impossible there; a separate instance avoids reloading or
  restarting the shared Caddy (Hermes/HA/deepagent). Backup of `/etc/caddy` taken before: `/etc/caddy.bak_show_20261021_100836`.
- Public URL: `cloudflared-show.service` (quick tunnel -> 8093). The URL changes when the tunnel restarts; get it with
  `journalctl -u cloudflared-show | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1`, then rebuild:

```
SITE_URL=<show tunnel URL> PUBLIC_AGENTOS_URL=<agent os URL>/ PUBLIC_CHAT_URL=<agent os URL>/chat/ npm run build
tar -C dist -czf /tmp/show_dist.tgz .   # upload, extract to /opt/agentos-show.new, swap directories
```

- Quick tunnels add `x-robots-tag: none` and their own robots.txt, so search engines will not index the site until a real domain is used.
- Health: `/workspace/agentos-local/ops/health_vps83.sh` checks `caddy-show`, `cloudflared-show`, `/` and `/en/` = 200, canonical = current URL.
- Rollback / removal: `systemctl disable --now cloudflared-show caddy-show`; remove the two unit files, `/etc/caddy-show`, `/opt/agentos-show*`.

## Pages (25/10/2026)

| Path | What |
|---|---|
| `/`, `/en/` | Short home page that routes to the pages below |
| `/tinh-nang/`, `/en/features/` | Feature catalogue (docs style: left TOC, filter, readiness labels, real screenshots) + LangSmith comparison. Data: `src/data/features.ts`, `src/data/parity.ts` |
| `/hoc/` + 7 lessons | Course "Hiểu và xây dựng hệ thống AI Agent với Agent OS" (Vietnamese). Lessons: `src/pages/hoc/*.astro`, metadata `src/data/lessons.ts`, static SVG diagrams `src/diagrams/` |

Sources of truth for features: `agentos-local/docs/HUONG_DAN_SU_DUNG.md`, `GD6..GD9_PLAN.md`, `BAO_CAO_TU_DONG.md`, `GD9_PARITY.md`.

```
node scripts/import-shots.mjs     # acceptance screenshots -> src/assets/f/*.webp (1600 px, q80)
node scripts/check-pages.mjs      # with `astro preview` running: every page × desktop/phone × light/dark;
                                  # fails on console errors, external requests, overflow, broken links/images
node scripts/check-pages.mjs --shots   # also writes shots/site/*.png (git-ignored)
```
On Windows run Astro with the portable Node in `../tools/` (`node node_modules/astro/bin/astro.mjs build`).
