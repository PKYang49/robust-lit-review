import assert from "node:assert/strict";
import { Buffer as NodeBuffer } from "node:buffer";
import { generateKeyPairSync, sign } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import test from "node:test";
import worker, { createHandler, HttpError, type Env, minYear, validatePicos } from "../src/index.ts";

type Data = Record<string, any>;
const SYNC_TOKEN = "test-sync-token-which-is-at-least-32-characters";
const PICO = {
  pico_id: "../../escape", population: "Adults", intervention: "Intervention", comparator: "Placebo",
  outcome: "Mortality", question_text: "Does intervention help?", primary_terms: ["Intervention"],
  secondary_terms: ["Mortality"], claim_direction: "benefit",
};

/** This adapter runs the production SQL against SQLite, without imitating queries. */
function fixture(t: { after: (callback: () => void) => void }) {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../migrations/0001_jobs.sql", import.meta.url).pathname, "utf8"));
  db.exec(readFileSync(new URL("../migrations/0002_discord_source.sql", import.meta.url).pathname, "utf8"));
  t.after(() => db.close());
  const assets: string[] = [];
  const env = {
    DB: { prepare(sql: string) {
      let values: SQLInputValue[] = [];
      const statement = {
        bind(...parameters: SQLInputValue[]) { values = parameters; return statement; },
        async first() { return db.prepare(sql).get(...values) ?? null; },
        async all() { return { results: db.prepare(sql).all(...values), success: true }; },
        async run() { return db.prepare(sql).run(...values); },
      };
      return statement;
    } },
    ASSETS: { async fetch(request: Request) {
      assets.push(new URL(request.url).pathname);
      return new Response("asset content");
    } },
    TEAM_DOMAIN: "test.cloudflareaccess.com", POLICY_AUD: "test-audience", SYNC_TOKEN,
  } as unknown as Env;
  const handler = createHandler(async () => {});
  const send = async (path: string, body?: unknown, headers: Record<string, string> = {}, fetcher = handler) => {
    return fetcher(new Request(`https://brief.example${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    }), env);
  };
  const sync = (path: string, data: unknown) => send(`/api/worker/${path}`, data, { "X-Evidence-Sync-Token": SYNC_TOKEN });
  const create = async () => {
    const response = await send("/api/briefs", { question: "A sufficiently long clinical question?" });
    assert.equal(response.status, 202);
    return await response.json() as Data;
  };
  const claim = async () => {
    const response = await sync("claim", { worker_id: "test-mac" });
    assert.equal(response.status, 200);
    return await response.json() as Data;
  };
  return { db, env, assets, send, sync, create, claim, handler };
}

test("production fails closed for missing Access configuration and missing JWT on every route", async t => {
  const f = fixture(t);
  const paths = ["/", "/evidence-brief", "/static/app.js", "/static/styles.css", "/api/config"];
  for (const path of paths) {
    assert.equal((await f.send(path, undefined, {}, worker.fetch)).status, 401);
  }
  f.env.POLICY_AUD = "REPLACE_WITH_AUDIENCE";
  assert.equal((await f.send("/static/app.js", undefined, {}, worker.fetch)).status, 503);
  assert.deepEqual(f.assets, []);
});

test("failed JWT verification cannot reach static assets, reports, or sync endpoints", async t => {
  const f = fixture(t);
  const reject = createHandler(async () => { throw new HttpError("bad assertion", 401); });
  for (const path of ["/", "/static/app.js", "/api/worker/claim"]) {
    const response = await f.send(path, path.endsWith("claim") ? {} : undefined, { "X-Evidence-Sync-Token": SYNC_TOKEN }, reject);
    assert.equal(response.status, 401);
    assert.match(response.headers.get("Cache-Control")!, /no-store/);
  }
  assert.deepEqual(f.assets, []);
});

test("completed reports are public while the workspace remains behind Access", async t => {
  const f = fixture(t);
  const reject = async () => { throw new HttpError("Access required", 401); };
  const report = `/briefs/${"a".repeat(32)}/report`;
  assert.equal((await f.send(report, undefined, {}, createHandler(reject))).status, 404);
  assert.equal((await f.send("/evidence-brief", undefined, {}, createHandler(reject))).status, 401);
});

test("public edge answers Discord PING with a valid signature", async t => {
  const f = fixture(t);
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const der = NodeBuffer.from(publicKey.export({ format: "der", type: "spki" }));
  f.env.PUBLIC_EDGE = "true";
  f.env.DISCORD_PUBLIC_KEY = der.subarray(-32).toString("hex");
  const timestamp = String(Math.floor(Date.now() / 1000));
  const body = JSON.stringify({ type: 1 });
  const signature = NodeBuffer.from(sign(null, NodeBuffer.from(timestamp + body), privateKey)).toString("hex");
  const response = await f.send("/discord/interactions", { type: 1 }, {
    "X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp,
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { type: 1 });
});

test("sync endpoints require a second secret after Access succeeds", async t => {
  const f = fixture(t);
  assert.equal((await f.send("/api/worker/claim", { worker_id: "mac" })).status, 401);
  assert.equal((await f.send("/api/worker/heartbeat", { fulltext: true }, { "X-Evidence-Sync-Token": "wrong" })).status, 401);
  assert.equal((await f.sync("claim", { worker_id: "mac" })).status, 200);
  f.env.SYNC_TOKEN = "";
  assert.equal((await f.sync("claim", { worker_id: "mac" })).status, 503);
});

test("authenticated static paths map to existing assets and unknown paths stay private", async t => {
  const f = fixture(t);
  for (const path of ["/", "/evidence-brief", "/static/app.js", "/static/styles.css"]) {
    assert.equal((await f.send(path)).status, 200);
  }
  assert.deepEqual(f.assets, ["/index.html", "/index.html", "/app.js", "/styles.css"]);
  assert.equal((await f.send("/index.ts")).status, 404);
  assert.equal((await f.send("/api/briefs/../../private")).status, 404);
});

test("browser writes reject cross-site, oversized, non-JSON and invalid payloads", async t => {
  const f = fixture(t);
  assert.equal((await f.send("/api/briefs", { question: "Clinical question" }, { Origin: "https://evil.example" })).status, 403);
  assert.equal((await f.send("/api/briefs", { question: "Clinical question" }, { "Sec-Fetch-Site": "cross-site" })).status, 403);
  assert.equal((await f.send("/api/briefs", { question: "x".repeat(32769) })).status, 413);
  assert.equal((await f.send("/api/briefs", { question: "Clinical question" }, { "Content-Type": "text/plain" })).status, 415);
  for (const question of ["short".slice(0, 4), "     ", "x".repeat(2001), null, 123]) {
    assert.equal((await f.send("/api/briefs", { question })).status, 422);
  }
});

test("PICO validation canonicalizes paths and bounds terms", () => {
  assert.equal(validatePicos([PICO])[0].pico_id, "pico_01");
  const withLongMeshHeading = validatePicos([{ ...PICO,
    mesh_terms: ["Drug-Related Side Effects and Adverse Reactions"],
  }]);
  assert.deepEqual(withLongMeshHeading[0].mesh_terms,
    ["Drug-Related Side Effects and Adverse Reactions"]);
  for (const value of [[], [PICO, PICO, PICO, PICO], [{ ...PICO, primary_terms: [] }],
    [{ ...PICO, secondary_terms: Array(13).fill("term") }], [{ ...PICO, outcome: "one two three four five six" }],
    [{ ...PICO, claim_direction: "unknown" }], [{ ...PICO, population: "x".repeat(2001) }]]) {
    assert.throws(() => validatePicos(value), HttpError);
  }
});

test("queue capacity is enforced atomically and jobs are not claimed twice", async t => {
  const f = fixture(t);
  for (let index = 0; index < 20; index++) await f.create();
  assert.equal((await f.send("/api/briefs", { question: "Another clinical question" })).status, 429);
  const first = await f.claim(), second = await f.claim();
  assert.notEqual(first.job.id, second.job.id);
  assert.match(first.job.id, /^[a-f0-9]{32}$/);
  assert.equal(first.command.kind, "draft");
  assert.match(first.command.id, /^[a-f0-9]{32}$/);
  assert.equal(first.command.payload.question, first.job.question);
});

test("lease retry preserves command identity and rejects expired or superseded updates", async t => {
  const f = fixture(t);
  await f.create();
  const first = await f.claim();
  assert.deepEqual(await f.claim(), { job: null });
  f.db.prepare("UPDATE jobs SET lease_until = 1 WHERE id = ?").run(first.job.id);
  assert.equal((await f.sync("update", { id: first.job.id, lease_token: first.lease_token, brief: first.job })).status, 409);
  const retry = await f.claim();
  assert.equal(retry.command.id, first.command.id);
  assert.notEqual(retry.lease_token, first.lease_token);
  assert.equal((await f.sync("update", { id: first.job.id, lease_token: first.lease_token, brief: first.job })).status, 409);
  f.db.prepare("UPDATE jobs SET lease_until = ? WHERE id = ?").run(Math.floor(Date.now() / 1000) + 15, retry.job.id);
  const response = await f.sync("update", { id: retry.job.id, lease_token: retry.lease_token, brief: retry.job });
  assert.equal(response.status, 200);
  const stored = f.db.prepare("SELECT lease_until, command FROM jobs WHERE id = ?").get(retry.job.id)!;
  assert.ok(Number(stored.lease_until) >= Math.floor(Date.now() / 1000) + 179);
  assert.ok(stored.command);
});

test("draft, approval, checkpoint and report complete through separate durable commands", async t => {
  const f = fixture(t), initial = await f.create();
  const draft = await f.claim();
  let response = await f.sync("update", { id: initial.id, lease_token: draft.lease_token, finished: true,
    brief: { ...draft.job, status: "pico_review", picos: validatePicos([PICO]), question: "overwrite", phase: "leak" } });
  assert.equal(response.status, 200);
  let job = await response.json() as Data;
  assert.equal(job.question, initial.question);
  assert.equal(job.phase, undefined);
  const approval = { picos: [PICO] };
  response = await f.send(`/api/briefs/${job.id}/approve`, approval);
  assert.equal(response.status, 202);
  assert.equal((await f.send(`/api/briefs/${job.id}/approve`, approval)).status, 409);
  const approve = await f.claim();
  assert.equal(approve.command.kind, "approve");
  assert.notEqual(approve.command.id, draft.command.id);
  assert.equal(approve.command.payload.picos[0].pico_id, "pico_01");
  response = await f.sync("update", { id: job.id, lease_token: approve.lease_token, finished: true,
    brief: { ...approve.job, status: "checkpoint" } });
  assert.equal(response.status, 200);
  assert.equal((await f.send(`/api/briefs/${job.id}/checkpoint`, { additions: [{ pico_id: "../evil", identifiers: ["123"] }] })).status, 422);
  assert.equal((await f.send(`/api/briefs/${job.id}/checkpoint`, { additions: [{ pico_id: "pico_02", identifiers: ["123"] }] })).status, 422);
  assert.equal((await f.send(`/api/briefs/${job.id}/checkpoint`, { note: "Checked", additions: [{ pico_id: "pico_01", identifiers: ["12345678"] }] })).status, 202);
  const confirm = await f.claim();
  assert.equal(confirm.command.kind, "checkpoint");
  response = await f.sync("update", { id: job.id, lease_token: confirm.lease_token, finished: true,
    brief: { ...confirm.job, status: "done" }, report: "<!doctype html><h1>Verified report</h1>" });
  assert.equal(response.status, 200);
  job = await response.json() as Data;
  assert.equal(job.report_url, `https://evidence-brief-public.rivafann.workers.dev/briefs/${job.id}/report`);
  assert.deepEqual(await f.claim(), { job: null });
  response = await f.send(`/briefs/${job.id}/report`);
  assert.equal(response.status, 200);
  assert.match(await response.text(), /Verified report/);
  const csp = response.headers.get("Content-Security-Policy")!;
  assert.match(csp, /sandbox allow-scripts allow-popups allow-modals/);
  assert.doesNotMatch(csp, /allow-same-origin/);
  assert.equal((await f.send(`/api/briefs/${job.id}/resume`, {})).status, 409);
});

test("expired expert checkpoints are claimed for automatic Opus review", async t => {
  const f = fixture(t), initial = await f.create();
  const draft = await f.claim();
  let response = await f.sync("update", { id: initial.id, lease_token: draft.lease_token, finished: true,
    brief: { ...draft.job, status: "pico_review", picos: validatePicos([PICO]) } });
  const draftJob = await response.json() as Data;
  response = await f.send(`/api/briefs/${draftJob.id}/approve`, { picos: [PICO] });
  assert.equal(response.status, 202);
  const approval = await f.claim();
  response = await f.sync("update", { id: draftJob.id, lease_token: approval.lease_token, finished: true,
    brief: { ...approval.job, status: "checkpoint" } });
  assert.equal(response.status, 200);
  assert.deepEqual(await f.claim(), { job: null });

  const stale = new Date(Date.now() - 301_000).toISOString();
  f.db.prepare("UPDATE jobs SET updated_at = ?, snapshot = json_set(snapshot, '$.updated_at', ?) WHERE id = ?")
    .run(stale, stale, draftJob.id);
  const automatic = await f.claim();
  assert.equal(automatic.command.kind, "auto_checkpoint");
  assert.equal(automatic.job.status, "running");
  assert.match(automatic.job.message, /Opus/);
});

test("error and interrupted states can resume but unfinished work cannot release a lease", async t => {
  const f = fixture(t);
  await f.create();
  const claimed = await f.claim();
  assert.equal((await f.sync("update", { id: claimed.job.id, lease_token: claimed.lease_token, brief: claimed.job, finished: true })).status, 422);
  assert.equal((await f.sync("update", { id: claimed.job.id, lease_token: claimed.lease_token,
    brief: { ...claimed.job, status: "error" }, finished: true })).status, 200);
  assert.equal((await f.send(`/api/briefs/${claimed.job.id}/resume`, {})).status, 202);
  const resumed = await f.claim();
  assert.equal(resumed.command.kind, "resume");
  assert.equal(resumed.job.status, "drafting");
  assert.equal((await f.send(`/api/briefs/${claimed.job.id}/resume`, {})).status, 409);
});

test("Discord error notifications include a retry button", async t => {
  const f = fixture(t);
  await f.create();
  const claimed = await f.claim();
  f.db.prepare("UPDATE jobs SET source = 'discord', discord_user_id = ?, discord_channel_id = ? WHERE id = ?")
    .run("123456789", "987654321", claimed.job.id);
  f.env.DISCORD_BOT_TOKEN = "bot-token";
  const originalFetch = globalThis.fetch;
  const requests: { url: string; body: Data }[] = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), body: JSON.parse(String(init?.body)) });
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  const response = await f.sync("update", {
    id: claimed.job.id,
    lease_token: claimed.lease_token,
    brief: { ...claimed.job, status: "error", message: "PICO 格式需要重試。" },
    finished: true,
  });

  assert.equal(response.status, 200);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "https://discord.com/api/v10/channels/987654321/messages");
  assert.equal(requests[0].body.components[0].components[0].custom_id,
    `evidence:resume:${claimed.job.id}`);
});

test("Discord can open a PICO edit form and queue the revision", async t => {
  const f = fixture(t);
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const der = NodeBuffer.from(publicKey.export({ format: "der", type: "spki" }));
  const userId = "123456789", channelId = "987654321";
  f.env.PUBLIC_EDGE = "true";
  f.env.DISCORD_PUBLIC_KEY = der.subarray(-32).toString("hex");
  f.env.DISCORD_ALLOWED_USER_IDS = userId;
  f.env.DISCORD_CHANNEL_ID = channelId;

  const signed = async (body: Data) => {
    const timestamp = String(Math.floor(Date.now() / 1000));
    const raw = JSON.stringify(body);
    const signature = NodeBuffer.from(sign(null, NodeBuffer.from(timestamp + raw), privateKey)).toString("hex");
    return f.send("/discord/interactions", body, {
      "X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp,
    });
  };
  const base = { member: { user: { id: userId } }, channel_id: channelId };
  const created = await signed({ ...base, type: 2,
    data: { name: "evidence", options: [{ name: "question", value: "A sufficiently long clinical question?" }] } });
  const content = (await created.json() as Data).data.content as string;
  const jobId = content.match(/查詢編號 ([a-f0-9]{32})/)?.[1];
  assert.ok(jobId);
  const row = f.db.prepare("SELECT snapshot FROM jobs WHERE id = ?").get(jobId) as { snapshot: string };
  const snapshot = JSON.parse(row.snapshot);
  snapshot.status = "pico_review";
  snapshot.picos = [PICO];
  f.db.prepare("UPDATE jobs SET status = 'pico_review', snapshot = ?, command = NULL WHERE id = ?")
    .run(JSON.stringify(snapshot), jobId);

  const modal = await signed({ ...base, type: 3, data: { custom_id: `evidence:edit:${jobId}` } });
  const modalBody = await modal.json() as Data;
  assert.equal(modalBody.type, 9);
  assert.equal(modalBody.data.custom_id, `evidence:edit:${jobId}`);

  const submitted = await signed({ ...base, type: 5, data: {
    custom_id: `evidence:edit:${jobId}`,
    components: [{ type: 1, components: [{ type: 4, custom_id: "instruction",
      value: "把主要結果改成心血管死亡，並保留 LVEF 正常族群" }] }],
  } });
  assert.equal(submitted.status, 200);
  const commandRow = f.db.prepare("SELECT command, status FROM jobs WHERE id = ?").get(jobId) as { command: string; status: string };
  assert.equal(commandRow.status, "drafting");
  const command = JSON.parse(commandRow.command);
  assert.equal(command.kind, "edit_pico");
  assert.equal(command.payload.instruction, "把主要結果改成心血管死亡，並保留 LVEF 正常族群");
});

test("worker updates bound reports and reject invalid states or completed jobs without reports", async t => {
  const f = fixture(t);
  await f.create();
  const { job, lease_token } = await f.claim();
  assert.equal((await f.sync("update", { id: job.id, lease_token, brief: { ...job, status: "injected" } })).status, 422);
  assert.equal((await f.sync("update", { id: job.id, lease_token, brief: { ...job, status: "done" }, finished: true })).status, 422);
  assert.equal((await f.sync("update", { id: job.id, lease_token, brief: job, report: "x".repeat(900 * 1024 + 1) })).status, 413);
  assert.equal((await f.sync("update", { id: job.id, lease_token, brief: job, report: "x".repeat(1024 * 1024) })).status, 413);
  assert.equal((await f.send(`/briefs/${job.id}/report`)).status, 404);
});

test("worker snapshots preserve the selected year window", async t => {
  const f = fixture(t);
  await f.create();
  const { job, lease_token } = await f.claim();
  const response = await f.sync("update", {
    id: job.id,
    lease_token,
    brief: { ...job, min_year: 2010 },
  });
  assert.equal(response.status, 200);
  const row = f.db.prepare("SELECT snapshot FROM jobs WHERE id = ?").get(job.id) as { snapshot: string };
  assert.equal(JSON.parse(row.snapshot).min_year, 2010);
});

test("heartbeat exposes capabilities and offline status without blocking new jobs", async t => {
  const f = fixture(t);
  let config = await (await f.send("/api/config")).json() as Data;
  assert.equal(config.configured, true);
  assert.equal(config.worker_online, false);
  await f.create();
  assert.equal((await f.sync("heartbeat", { fulltext: true })).status, 200);
  config = await (await f.send("/api/config")).json() as Data;
  assert.equal(config.fulltext, true);
  assert.equal(config.worker_online, true);
  f.db.prepare("UPDATE worker_heartbeat SET last_seen = 1").run();
  config = await (await f.send("/api/config")).json() as Data;
  assert.equal(config.worker_online, false);
  const logout = await (await f.send("/api/logout", {})).json() as Data;
  assert.equal(logout.logout_url, "/cdn-cgi/access/logout");
});

test("the year window is per question, bounded, and defaults to 2000", () => {
  assert.equal(minYear(undefined), 2000);
  assert.equal(minYear(""), 2000);
  assert.equal(minYear(2016), 2016);
  assert.equal(minYear("1995"), 1995);
  for (const bad of [1959, 1800, new Date().getUTCFullYear() + 1, 2016.5, "soon"]) {
    assert.throws(() => minYear(bad), HttpError, `expected ${String(bad)} to be rejected`);
  }
});
