# Evidence Brief Cloudflare Relay

This Worker hosts the responsive Evidence Brief page, stores query snapshots in
Cloudflare D1, and queues work for the local Mac. The Mac runs the existing
PubMed/GRADE/full-text pipeline and uploads progress and the final report.

Private workspace: <https://evidence-brief-relay.rivafann.workers.dev/evidence-brief>
Public report host: <https://evidence-brief-public.rivafann.workers.dev>

## One-time Cloudflare setup

From this directory:

```bash
npm install
npx wrangler d1 create evidence-brief
```

Put the returned database id in `wrangler.toml`, then apply the schema:

```bash
npx wrangler d1 migrations apply evidence-brief --remote
npx wrangler secret put SYNC_TOKEN
npx wrangler deploy
```

Create a Cloudflare Access self-hosted application for the deployed
`evidence-brief.<account>.workers.dev` hostname. Allow only the intended email
address. Copy its audience tag into `wrangler.toml` as `POLICY_AUD`, then deploy
again. Access must cover the hostname (or the Worker itself), including static
assets and `/briefs/*/report`. The Worker also verifies the Access JWT and fails
closed while the audience is a placeholder.

The browser uses Access login. The local relay uses the Access service-token
headers plus `X-Evidence-Sync-Token`; those credentials are never included in
the page or repository. Completed reports are served from the separate public
report host and do not require Access.

## Discord mobile entrypoint

The existing `MacJournal` Discord bot accepts the `/evidence` slash command in
the configured channel. Send `/evidence` with a clinical question; after Opus
prepares the PICO, the bot posts a confirmation button. Pressing it starts the
search and the bot posts the public report link when the evidence brief is
complete. Discord interactions are signature-verified and restricted to the
configured user and channel.

The public report Worker is deployed with `wrangler.public.toml`; it exposes
only `/briefs/<id>/report` and `/discord/interactions`, while the workspace and
all progress APIs remain behind Cloudflare Access.

## LLM routing and automatic expert review

Cloudflare does not call an LLM. After the Mac relay claims a query, Claude Code
runs the LLM stages locally: Opus handles PICO drafting, GRADE, full-text
verification, and writing; Haiku remains reserved for batched abstract
screening. After the search, Opus automatically reviews the included studies
and candidate gaps, then the pipeline continues without a user confirmation
step. Any added study must come from the candidate list already returned by the
search, so the review cannot invent a citation. Existing jobs left at the old
manual checkpoint are also assigned to Opus after five minutes by the next
online Mac relay poll; if the Mac is offline, the query waits until it
reconnects.

## Mac worker

Create `.evidence-brief/cloud.env` with file mode `600`:

```env
EVIDENCE_BRIEF_RELAY=https://evidence-brief-relay.rivafann.workers.dev
EVIDENCE_BRIEF_SYNC_TOKEN=<the same value entered with wrangler secret put>
CF_ACCESS_CLIENT_ID=<Access service token client id>
CF_ACCESS_CLIENT_SECRET=<Access service token client secret>
```

If this Mac already runs Weekly Journal, the relay also reads
`/Users/pokai/JournalFetcher/.env` (or the path in `JOURNALFETCHER_ENV`) and
reuses its `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, and
`FEEDBACK_SYNC_TOKEN`. Only `EVIDENCE_BRIEF_RELAY` needs to be added to the
Evidence Brief `cloud.env` in that setup.

Start the worker from the repository root:

```bash
evidence-brief-worker
```

This Mac is configured to keep the relay running at login with
`~/Library/LaunchAgents/com.pokai.evidence-brief-worker.plist`.

Use `evidence-brief-worker --once` for a smoke test. It sends a heartbeat,
claims one queued command, resumes the local pipeline, and uploads durable
snapshots. If the Mac is offline, queries remain queued in D1 and the page
shows that they will start when the worker returns.

## Local-only mode

For development without Cloudflare, run:

```bash
evidence-brief-web --host 127.0.0.1 --port 8765
```

The local page uses a generated access key in `.evidence-brief/access-key.txt`.
It is separate from Cloudflare Access and is intended for the same Mac only.

## Checks

```bash
npm run typecheck
npm test
npm run dry-run
```
