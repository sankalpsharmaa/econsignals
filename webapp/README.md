# EconSignals webapp

React + Vite + TypeScript dashboard for the EconSignals research feed.

## Dev

```bash
cd webapp
npm install
npm run dev        # http://localhost:5173/econsignals/
```

## Build

```bash
npm run build      # outputs to webapp/dist/
```

Asset paths are prefixed `/econsignals/` (set via `base` in `vite.config.ts`) for GitHub Pages deployment at `https://sankalpsharmaa.github.io/econsignals/`.

## Feed data

The app reads `webapp/public/feed.json` at runtime. Regenerate it with:

```bash
python -m econsignals.lib.snapshot
```

This writes a fresh snapshot from `data/econsignals.db` to `webapp/public/feed.json`.

To use a live backend instead, set `VITE_API_BASE=https://your-api.example.com` before building. The app will fetch `${VITE_API_BASE}/api/feed` (same JSON shape).

## Deploy to GitHub Pages

1. Run `npm run build`
2. Push `webapp/dist/` contents to the `gh-pages` branch, or configure GitHub Actions to do so.
3. Ensure a `.nojekyll` file is present at the root of the `gh-pages` branch.
