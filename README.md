# LCL Freight Picking Tool

A mobile checklist for picking customer freight / warehouse receipts against a consolidation list, with photo documentation posted straight to Slack. Sibling app to the Warehouse Inventory app — same Node/Express + Render + Slack pattern.

## What it does
1. **Load the list** — upload your consolidation list (Excel, CSV, or text-based PDF) and it auto-builds the pick checklist. (Or tap *Load sample list* to try the flow.)
2. **Pick** — for each line: check it off, enter the picked quantity (shown against the expected qty), and confirm the location. Short picks flag automatically. A progress bar tracks lines picked.
3. **Photos** — Take Photo or Upload, add as many as you need (thumbnails with remove).
4. **Post to Slack** — choose the channel and post a clean summary (WR# header, per-line qty/location, short-pick flags) with all the photos attached.

## Deploy (same as the inventory app)
1. Put these files in a **new GitHub repo** (with `package.json` at the repo root).
2. Create a **new Render Web Service** from that repo. Instance type **Starter** (always-on, no cold start).
3. Set one environment variable: `SLACK_BOT_TOKEN` — the bot token from your Slack workspace.
   Required bot scopes: `channels:read`, `groups:read`, `chat:write`, `files:write`.
   The bot must be **invited to any channel** you want to post in (`/invite @yourbot`).
4. Deploy. Add a tile linking to the new URL on `lcl-hub.onrender.com`.

## Local run
```
npm install
SLACK_BOT_TOKEN=xoxb-... npm start
# open http://localhost:3000
```

## Notes
- The channel picker shows every channel the bot can see (or restrict it with `SLACK_ALLOWED_CHANNELS`).
- PDF parsing is best-effort and works on text-based PDFs, not scanned images. Excel/CSV is the most reliable list format.
- Drop `logo.png` / `logo-wordmark.png` into `public/` if you want the LCL logo in place of the text wordmark.
