import { createRemoteJWKSet, jwtVerify } from "jose";

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  TEAM_DOMAIN: string;
  POLICY_AUD: string;
  SYNC_TOKEN: string;
  DISCORD_PUBLIC_KEY?: string;
  DISCORD_BOT_TOKEN?: string;
  DISCORD_ALLOWED_USER_IDS?: string;
  DISCORD_CHANNEL_ID?: string;
  PUBLIC_REPORT_BASE_URL?: string;
  PUBLIC_EDGE?: string;
}

type Data = Record<string, unknown>;
type CommandKind = "draft" | "approve" | "checkpoint" | "resume" | "auto_checkpoint";
interface JobRow {
  id: string;
  question: string;
  status: string;
  snapshot: string;
  command: string | null;
  created_at: string;
  updated_at: string;
  lease_token: string | null;
  lease_until: number | null;
  report: string | null;
  source: string | null;
  discord_user_id: string | null;
  discord_channel_id: string | null;
  discord_last_status: string | null;
}

export const LEASE_SECONDS = 180;
export const AUTO_CHECKPOINT_SECONDS = 5 * 60;
const MAX_PENDING = 20;
const BROWSER_BODY_BYTES = 32 * 1024;
const SYNC_BODY_BYTES = 1024 * 1024;
const REPORT_BYTES = 900 * 1024;
const SNAPSHOT_BYTES = 512 * 1024;
const ID_RE = /^[a-f0-9]{32}$/;
const DISCORD_ID_RE = /^\d{5,25}$/;
const STATUSES = new Set(["drafting", "searching", "running", "pico_review", "checkpoint", "done", "error", "interrupted"]);
const WAITING = new Set(["pico_review", "checkpoint", "done", "error", "interrupted"]);
const DISCORD_NOTIFY_STATUSES = new Set(["pico_review", "done", "error"]);
const encoder = new TextEncoder();
const accessKeys = new Map<string, ReturnType<typeof createRemoteJWKSet>>();

export class HttpError extends Error {
  constructor(message: string, readonly status = 422) {
    super(message);
  }
}

function object(value: unknown, label = "內容"): Data {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new HttpError(`${label}格式不正確。`);
  }
  return value as Data;
}

function string(value: unknown, maximum: number, label: string, minimum = 0): string {
  if (typeof value !== "string" || value.trim().length < minimum || value.length > maximum) {
    throw new HttpError(`${label}長度或格式不正確。`);
  }
  return value.trim();
}

function list(value: unknown, maximum: number, label: string, minimum = 0): unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    throw new HttpError(`${label}數量或格式不正確。`);
  }
  return value;
}

function id(value: unknown): string {
  if (typeof value !== "string" || !ID_RE.test(value)) throw new HttpError("無效的查詢識別碼。", 404);
  return value;
}

function now(): number { return Math.floor(Date.now() / 1000); }
function timestamp(): string { return new Date().toISOString(); }
function uuid(): string { return crypto.randomUUID().replaceAll("-", ""); }

function publicReportPath(path: string): boolean {
  return /^\/briefs\/[a-f0-9]{32}\/report$/.test(path);
}

function hexBytes(value: string, label: string): Uint8Array {
  if (!/^[0-9a-f]+$/i.test(value) || value.length % 2 !== 0) {
    throw new HttpError(`${label}格式不正確。`, 401);
  }
  const bytes = new Uint8Array(value.length / 2);
  for (let index = 0; index < bytes.length; index++) bytes[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
  return bytes;
}

function discordUserId(data: Data): string {
  const member = data.member;
  const user = member && typeof member === "object" && !Array.isArray(member)
    ? (member as Data).user : data.user;
  return string(user && typeof user === "object" && !Array.isArray(user) ? (user as Data).id : "", 25, "Discord 使用者", 5);
}

function discordChannelId(data: Data): string {
  return string(data.channel_id, 25, "Discord 頻道", 5);
}

function discordAllowed(env: Env, userId: string, channelId: string): boolean {
  const allowed = (env.DISCORD_ALLOWED_USER_IDS || "").split(",").map(value => value.trim()).filter(Boolean);
  return DISCORD_ID_RE.test(userId) && allowed.includes(userId)
    && (!env.DISCORD_CHANNEL_ID || env.DISCORD_CHANNEL_ID === channelId);
}

function interactionReply(content: string, ephemeral = true): Response {
  return Response.json({ type: 4, data: { content: content.slice(0, 2000), ...(ephemeral ? { flags: 64 } : {}) } });
}

async function discordInteractionData(request: Request, env: Env): Promise<Data> {
  const publicKey = (env.DISCORD_PUBLIC_KEY || "").trim();
  const signature = request.headers.get("X-Signature-Ed25519") || "";
  const signedAt = request.headers.get("X-Signature-Timestamp") || "";
  if (!/^[0-9a-f]{128}$/i.test(signature) || !/^\d+$/.test(signedAt)
      || Math.abs(now() - Number(signedAt)) > 300 || !/^[0-9a-f]{64}$/i.test(publicKey)) {
    throw new HttpError("Discord 互動簽章無效。", 401);
  }
  const body = new Uint8Array(await request.arrayBuffer());
  const prefix = encoder.encode(signedAt);
  const message = new Uint8Array(prefix.length + body.length);
  message.set(prefix);
  message.set(body, prefix.length);
  const key = await crypto.subtle.importKey("raw", hexBytes(publicKey, "Discord 公開金鑰"), { name: "Ed25519" }, false, ["verify"]);
  const valid = await crypto.subtle.verify("Ed25519", key, hexBytes(signature, "Discord 簽章"), message);
  if (!valid) throw new HttpError("Discord 互動簽章無效。", 401);
  let parsed: unknown;
  try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(body)); }
  catch { throw new HttpError("Discord 互動內容格式不正確。", 400); }
  return object(parsed, "Discord 互動內容");
}

async function discordApi(env: Env, path: string, body: Data): Promise<Data> {
  const token = (env.DISCORD_BOT_TOKEN || "").trim();
  if (!token) throw new Error("DISCORD_BOT_TOKEN is not configured");
  const response = await fetch(`https://discord.com/api/v10${path}`, {
    method: "POST",
    headers: { Authorization: `Bot ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Discord API HTTP ${response.status}`);
  const text = await response.text();
  return text ? object(JSON.parse(text), "Discord 回應") : {};
}

export async function secretsEqual(actual: string, expected: string): Promise<boolean> {
  const [left, right] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(actual)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const a = new Uint8Array(left), b = new Uint8Array(right);
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a[index] ^ b[index];
  return difference === 0;
}

export async function requireAccess(request: Request, env: Env): Promise<void> {
  const team = env.TEAM_DOMAIN?.trim() || "";
  const audience = env.POLICY_AUD?.trim() || "";
  if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/i.test(team) || !audience || /REPLACE|CHANGE_ME/.test(audience)) {
    throw new HttpError("Cloudflare Access 尚未設定完成。", 503);
  }
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) throw new HttpError("請先透過 Cloudflare Access 登入。", 401);
  const issuer = `https://${team}`;
  let keys = accessKeys.get(issuer);
  if (!keys) {
    keys = createRemoteJWKSet(new URL(`${issuer}/cdn-cgi/access/certs`));
    accessKeys.set(issuer, keys);
  }
  try {
    await jwtVerify(token, keys, { issuer, audience, algorithms: ["RS256"] });
  } catch {
    throw new HttpError("Cloudflare Access 登入驗證失敗。", 401);
  }
}

async function requireSync(request: Request, env: Env): Promise<void> {
  if (!env.SYNC_TOKEN || env.SYNC_TOKEN.length < 32 || /REPLACE|CHANGE_ME/.test(env.SYNC_TOKEN)) {
    throw new HttpError("同步金鑰尚未設定完成。", 503);
  }
  if (!await secretsEqual(request.headers.get("X-Evidence-Sync-Token") || "", env.SYNC_TOKEN)) {
    throw new HttpError("同步驗證失敗。", 401);
  }
}

async function payload(request: Request, limit: number): Promise<Data> {
  if (request.headers.get("content-type")?.split(";")[0].trim() !== "application/json") {
    throw new HttpError("請使用 JSON 格式送出。", 415);
  }
  const size = request.headers.get("content-length");
  if (size !== null && (!/^\d+$/.test(size) || Number(size) > limit)) {
    throw new HttpError("輸入內容過長。", 413);
  }
  const reader = request.body?.getReader();
  if (!reader) throw new HttpError("請提供 JSON 內容。", 400);
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > limit) {
      await reader.cancel();
      throw new HttpError("輸入內容過長。", 413);
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  let data: unknown;
  try { data = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes)); }
  catch { throw new HttpError("JSON 格式不正確。", 400); }
  return object(data);
}

function verifyOrigin(request: Request): void {
  const origin = request.headers.get("origin");
  if ((origin && origin !== new URL(request.url).origin) || request.headers.get("sec-fetch-site") === "cross-site") {
    throw new HttpError("請從查詢頁面送出請求。", 403);
  }
}

function term(value: unknown): string {
  const valueString = string(value, 150, "搜尋詞", 1);
  if (valueString.split(/\s+/).length > 5) throw new HttpError("搜尋詞每組最多 5 個英文單字。");
  return valueString;
}

export function validatePicos(value: unknown): Data[] {
  return list(value, 3, "PICO", 1).map((item, index) => {
    const pico = object(item, "PICO");
    const direction = pico.claim_direction ?? "benefit";
    if (direction !== "benefit" && direction !== "harm") throw new HttpError("claim_direction 必須是 benefit 或 harm。");
    const priority = pico.priority ?? 1;
    if (!Number.isInteger(priority) || Number(priority) < 1 || Number(priority) > 3) throw new HttpError("PICO 優先順序需為 1 至 3。");
    return {
      pico_id: `pico_0${index + 1}`,
      population: string(pico.population, 2000, "族群", 1),
      intervention: string(pico.intervention, 2000, "介入", 1),
      comparator: string(pico.comparator ?? "", 2000, "比較"),
      outcome: term(pico.outcome),
      outcome_domain: string(pico.outcome_domain ?? "", 150, "結果分類"),
      question_text: string(pico.question_text, 2000, "子問題", 1),
      rationale: string(pico.rationale ?? "", 2000, "理由"),
      primary_terms: list(pico.primary_terms, 12, "介入搜尋詞", 1).map(term),
      secondary_terms: list(pico.secondary_terms, 12, "結果搜尋詞", 1).map(term),
      mesh_terms: list(pico.mesh_terms ?? [], 12, "MeSH 搜尋詞").map(term),
      priority,
      claim_direction: direction,
    };
  });
}

function checkpoint(data: Data, job: Data): Data {
  const picos = list(job.picos, 3, "PICO").map(p => object(p).pico_id);
  const seen = new Set<string>();
  const additions = list(data.additions ?? [], 3, "補充文獻").map(item => {
    const addition = object(item);
    const picoId = string(addition.pico_id, 7, "PICO ID", 7);
    if (!/^pico_0[1-3]$/.test(picoId) || !picos.includes(picoId) || seen.has(picoId)) {
      throw new HttpError("請選擇有效且不重複的 PICO。");
    }
    seen.add(picoId);
    return { pico_id: picoId, identifiers: list(addition.identifiers ?? [], 10, "文獻編號")
      .map(value => string(value, 250, "PMID 或 DOI", 1)) };
  });
  return { note: string(data.note ?? "已確認納入研究。", 2000, "確認備註"), additions };
}

function publicReportUrl(env: Env, jobId: string): string {
  const base = (env.PUBLIC_REPORT_BASE_URL || "https://evidence-brief-public.rivafann.workers.dev").trim().replace(/\/$/, "");
  return `${base}/briefs/${jobId}/report`;
}

function snapshot(row: JobRow, env: Env): Data {
  return { ...object(JSON.parse(row.snapshot)), id: row.id, question: row.question,
    status: row.status, updated_at: row.updated_at,
    report_url: row.status === "done" && row.report !== null ? publicReportUrl(env, row.id) : null };
}

function cleanSnapshot(value: unknown, row: JobRow): Data {
  const incoming = object(value, "進度");
  if (!STATUSES.has(String(incoming.status))) throw new HttpError("無效的處理狀態。");
  const result: Data = {
    id: row.id, question: row.question, status: incoming.status,
    message: string(incoming.message ?? "", 2000, "進度訊息"), updated_at: timestamp(),
    picos: list(incoming.picos ?? [], 3, "PICO"),
    preview: list(incoming.preview ?? [], 3, "搜尋预覽"),
    states: list(incoming.states ?? [], 3, "處理狀態"),
    studies: object(incoming.studies ?? {}, "文獻"),
    gaps: object(incoming.gaps ?? {}, "遺漏文獻"),
    events: list(incoming.events ?? [], 60, "處理紀錄"),
    report_url: null,
  };
  if (incoming.additions_result !== undefined) result.additions_result = object(incoming.additions_result, "補充結果");
  if (incoming.fulltext !== undefined) {
    if (typeof incoming.fulltext !== "boolean") throw new HttpError("全文設定格式不正確。");
    result.fulltext = incoming.fulltext;
  }
  if (encoder.encode(JSON.stringify(result)).byteLength > SNAPSHOT_BYTES) throw new HttpError("進度內容過長。", 413);
  return result;
}

async function getJob(env: Env, jobId: string): Promise<JobRow> {
  const row = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id(jobId)).first<JobRow>();
  if (!row) throw new HttpError("找不到這筆查詢。", 404);
  return row;
}

interface DiscordContext {
  userId: string;
  channelId: string;
}

async function notifyDiscord(env: Env, row: JobRow, brief: Data): Promise<void> {
  if (row.source !== "discord" || !row.discord_channel_id) return;
  const status = String(brief.status);
  const mention = row.discord_user_id && DISCORD_ID_RE.test(row.discord_user_id)
    ? `<@${row.discord_user_id}> ` : "";
  let content = "";
  let components: Data[] = [];
  if (status === "pico_review") {
    const picos = Array.isArray(brief.picos) ? brief.picos : [];
    const lines = picos.map((item, index) => {
      const pico = item && typeof item === "object" && !Array.isArray(item) ? item as Data : {};
      return `${index + 1}. ${String(pico.question_text || pico.outcome || "未提供子問題")}`;
    });
    content = `${mention}Evidence Brief\n${row.question}\n\nPICO 已整理完成：\n${lines.join("\n")}\n\n確認後開始搜尋文獻：`;
    components = [{ type: 1, components: [{ type: 2, style: 1, label: "確認 PICO，開始搜尋", custom_id: `evidence:approve:${row.id}` }] }];
  } else if (status === "done") {
    content = `${mention}Evidence Brief 已完成\n${row.question}\n\n公開證據摘要：${publicReportUrl(env, row.id)}`;
  } else if (status === "error") {
    content = `${mention}Evidence Brief 需要接續處理\n${row.question}\n\n${String(brief.message || "請回到查詢頁面接續執行。")}`;
  } else return;
  await discordApi(env, `/channels/${row.discord_channel_id}/messages`, {
    content: content.slice(0, 2000),
    components,
    allowed_mentions: row.discord_user_id && DISCORD_ID_RE.test(row.discord_user_id)
      ? { parse: [], users: [row.discord_user_id] } : { parse: [] },
  });
}

async function discordInteraction(request: Request, env: Env): Promise<Response> {
  const data = await discordInteractionData(request, env);
  const type = Number(data.type);
  if (type === 1) return Response.json({ type: 1 });
  if (type !== 2 && type !== 3) return interactionReply("目前只支援 Evidence Brief 查詢與 PICO 確認。", true);

  const userId = discordUserId(data), channelId = discordChannelId(data);
  if (!discordAllowed(env, userId, channelId)) return interactionReply("此 Discord 使用者或頻道沒有使用權限。", true);

  if (type === 2) {
    const command = object(data.data, "Discord 指令");
    const name = string(command.name, 32, "Discord 指令名稱", 1);
    if (name !== "evidence") return interactionReply("未知的 Evidence Brief 指令。", true);
    const options = list(command.options ?? [], 25, "Discord 指令選項");
    const option = options.find(item => item && typeof item === "object" && !Array.isArray(item)
      && (item as Data).name === "question");
    const question = string(option && typeof option === "object" && !Array.isArray(option) ? (option as Data).value : "", 2000, "臨床問題", 5);
    const created = await createBrief(env, { question }, { userId, channelId });
    const job = await created.clone().json() as Data;
    return interactionReply(`已收到臨床問題，查詢編號 ${String(job.id)}。PICO 整理完成後會在此頻道通知你。`, true);
  }

  const component = object(data.data, "Discord 按鈕");
  const customId = string(component.custom_id, 100, "Discord 按鈕", 1);
  const match = customId.match(/^evidence:approve:([a-f0-9]{32})$/);
  if (!match) return interactionReply("此按鈕已失效，請重新送出查詢。", true);
  const row = await getJob(env, match[1]);
  if (row.source !== "discord" || row.discord_user_id !== userId || row.discord_channel_id !== channelId) {
    return interactionReply("這不是你的 Evidence Brief 查詢。", true);
  }
  if (row.status !== "pico_review" || row.command !== null || row.lease_token !== null) {
    return interactionReply("這筆查詢已經開始處理或已完成，請稍候查看最新通知。", true);
  }
  const job = snapshot(row, env);
  const approved = await enqueue(env, row.id, "approve", { picos: job.picos });
  if (approved.status !== 202) return interactionReply("查詢目前無法接續，請稍後再試。", true);
  return interactionReply("已確認 PICO，開始搜尋文獻。完成後會在此頻道通知你。", true);
}

async function createBrief(env: Env, data: Data, discord?: DiscordContext): Promise<Response> {
  const question = string(data.question, 2000, "問題", 5), jobId = uuid(), at = timestamp();
  const job: Data = { id: jobId, question, status: "drafting", message: "已排入佇列，等待工作電腦處理…",
    updated_at: at, picos: [], preview: [], studies: {}, gaps: {}, states: [], events: [], report_url: null };
  const row = await env.DB.prepare(`INSERT INTO jobs
    (id, question, status, snapshot, command, created_at, updated_at, source, discord_user_id, discord_channel_id)
    SELECT ?, ?, 'drafting', ?, ?, ?, ?, ?, ?, ?
    WHERE (SELECT COUNT(*) FROM jobs WHERE command IS NOT NULL) < ? RETURNING *`)
    .bind(jobId, question, JSON.stringify(job), JSON.stringify({ id: uuid(), kind: "draft", payload: { question } }), at, at,
      discord ? "discord" : "web", discord?.userId || null, discord?.channelId || null, MAX_PENDING)
    .first<JobRow>();
  if (!row) throw new HttpError("等待處理的查詢已達上限，請稍後再試。", 429);
  return Response.json(snapshot(row, env), { status: 202 });
}

async function enqueue(env: Env, jobId: string, kind: Exclude<CommandKind, "draft">, data: Data): Promise<Response> {
  const row = await getJob(env, jobId), job = snapshot(row, env);
  const allowed = kind === "approve" ? ["pico_review"] : kind === "checkpoint" ? ["checkpoint"] : ["error", "interrupted"];
  if (!allowed.includes(row.status) || row.command !== null || row.lease_token !== null) {
    throw new HttpError("查詢狀態已變更或正在處理，請重新整理。", 409);
  }
  const commandPayload = kind === "approve" ? { picos: validatePicos(data.picos) }
    : kind === "checkpoint" ? checkpoint(data, job) : {};
  const status = kind === "approve" ? "searching" : kind === "resume" && !(job.picos as unknown[]).length ? "drafting" : "running";
  const at = timestamp();
  const updated = { ...job, status, updated_at: at, message: "已排入佇列，等待工作電腦接續處理…", report_url: null };
  if (kind === "approve") Object.assign(updated, { ...commandPayload, preview: [] });
  const result = await env.DB.prepare(`UPDATE jobs SET snapshot = ?, command = ?, status = ?, updated_at = ?
    WHERE id = ? AND status = ? AND command IS NULL AND lease_token IS NULL
    AND (SELECT COUNT(*) FROM jobs WHERE command IS NOT NULL) < ? RETURNING *`)
    .bind(JSON.stringify(updated), JSON.stringify({ id: uuid(), kind, payload: commandPayload }), status, at, row.id, row.status, MAX_PENDING)
    .first<JobRow>();
  if (!result) throw new HttpError("查詢已在處理中或佇列已滿，請稍後重新整理。", 409);
  return Response.json(snapshot(result, env), { status: 202 });
}

async function claim(env: Env, data: Data): Promise<Response> {
  const workerId = string(data.worker_id, 100, "工作電腦名稱", 1), seconds = now();
  const cutoff = new Date((seconds - AUTO_CHECKPOINT_SECONDS) * 1000).toISOString();
  const expired = await env.DB.prepare(`SELECT * FROM jobs
    WHERE status = 'checkpoint' AND command IS NULL AND lease_token IS NULL AND updated_at <= ?
    ORDER BY updated_at, id LIMIT 1`).bind(cutoff).first<JobRow>();
  if (expired) {
    const lease = uuid(), at = timestamp();
    const autoJob = snapshot(expired, env);
    autoJob.status = "running";
    autoJob.message = "專家核對已等待 5 分鐘，Opus 正在自動核對…";
    autoJob.updated_at = at;
    autoJob.report_url = null;
    const command = { id: uuid(), kind: "auto_checkpoint", payload: {} };
    const autoRow = await env.DB.prepare(`UPDATE jobs SET snapshot = ?, command = ?, status = 'running',
      updated_at = ?, lease_token = ?, lease_until = ?, worker_id = ?
      WHERE id = ? AND status = 'checkpoint' AND command IS NULL AND lease_token IS NULL AND updated_at <= ?
      RETURNING *`)
      .bind(JSON.stringify(autoJob), JSON.stringify(command), at, lease, seconds + LEASE_SECONDS, workerId,
        expired.id, cutoff).first<JobRow>();
    if (autoRow) return Response.json({ job: snapshot(autoRow, env), command, lease_token: lease });
  }
  const lease = uuid();
  const row = await env.DB.prepare(`UPDATE jobs SET lease_token = ?, lease_until = ?, worker_id = ?
    WHERE id = (SELECT id FROM jobs WHERE command IS NOT NULL
      AND (lease_until IS NULL OR lease_until <= ?) ORDER BY created_at, id LIMIT 1)
    AND command IS NOT NULL AND (lease_until IS NULL OR lease_until <= ?) RETURNING *`)
    .bind(lease, seconds + LEASE_SECONDS, workerId, seconds, seconds).first<JobRow>();
  return Response.json(row ? { job: snapshot(row, env), command: JSON.parse(row.command!), lease_token: lease } : { job: null });
}

async function updateJob(env: Env, data: Data): Promise<Response> {
  const row = await getJob(env, id(data.id));
  const previous = object(JSON.parse(row.snapshot), "既有進度");
  const lease = string(data.lease_token, 32, "租約", 32);
  if (row.lease_token !== lease || (row.lease_until ?? 0) <= now()) throw new HttpError("工作租約已過期。", 409);
  if (data.finished !== undefined && typeof data.finished !== "boolean") throw new HttpError("finished 格式不正確。");
  const finished = data.finished === true, brief = cleanSnapshot(data.brief, row);
  if (finished && !WAITING.has(String(brief.status))) throw new HttpError("工作尚未抵達可停止的狀態。");
  let report: string | null = null;
  if (data.report !== undefined) {
    if (typeof data.report !== "string" || !data.report.trim()) throw new HttpError("報告格式不正確。");
    if (encoder.encode(data.report).byteLength > REPORT_BYTES) throw new HttpError("報告內容過長。", 413);
    report = data.report;
  }
  if (finished && brief.status === "done" && report === null && row.report === null) throw new HttpError("完成的查詢必須包含報告。");
  const seconds = now();
  const result = await env.DB.prepare(`UPDATE jobs SET snapshot = ?, status = ?, updated_at = ?,
    report = COALESCE(?, report), command = CASE WHEN ? THEN NULL ELSE command END,
    lease_token = CASE WHEN ? THEN NULL ELSE lease_token END,
    lease_until = CASE WHEN ? THEN NULL ELSE ? END
    WHERE id = ? AND lease_token = ? AND lease_until > ? AND command IS NOT NULL RETURNING *`)
    .bind(JSON.stringify(brief), String(brief.status), String(brief.updated_at), report,
      Number(finished), Number(finished), Number(finished), seconds + LEASE_SECONDS, row.id, lease, seconds)
    .first<JobRow>();
  if (!result) throw new HttpError("工作租約已過期或查詢已更新。", 409);
  // A long-running local pipeline sends progress updates every few seconds;
  // use those updates to keep the worker presence indicator fresh as well.
  await env.DB.prepare("UPDATE worker_heartbeat SET last_seen = ? WHERE singleton = 1")
    .bind(seconds).run();
  if (row.source === "discord" && row.discord_channel_id
      && DISCORD_NOTIFY_STATUSES.has(String(brief.status))
      && row.discord_last_status !== String(brief.status)
      && String(previous.status) !== String(brief.status)) {
    try {
      await notifyDiscord(env, row, brief);
      await env.DB.prepare("UPDATE jobs SET discord_last_status = ? WHERE id = ?")
        .bind(String(brief.status), row.id).run();
    } catch (error) {
      console.error("Evidence Brief Discord notification failed", error instanceof Error ? error.name : "UnknownError");
    }
  }
  return Response.json(snapshot(result, env));
}

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url), path = url.pathname;
  const sync = path.startsWith("/api/worker/");
  if (sync) await requireSync(request, env);
  if (request.method === "POST") verifyOrigin(request);
  const data = request.method === "POST" ? await payload(request, sync ? SYNC_BODY_BYTES : BROWSER_BODY_BYTES) : {};

  if (request.method === "POST" && path === "/api/worker/claim") return claim(env, data);
  if (request.method === "POST" && path === "/api/worker/update") return updateJob(env, data);
  if (request.method === "POST" && path === "/api/worker/heartbeat") {
    if (typeof data.fulltext !== "boolean") throw new HttpError("全文設定格式不正確。");
    await env.DB.prepare(`INSERT INTO worker_heartbeat (singleton, last_seen, fulltext) VALUES (1, ?, ?)
      ON CONFLICT(singleton) DO UPDATE SET last_seen = excluded.last_seen, fulltext = excluded.fulltext`)
      .bind(now(), Number(data.fulltext)).run();
    return Response.json({ ok: true });
  }
  if (request.method === "GET" && path === "/api/config") {
    const heartbeat = await env.DB.prepare("SELECT last_seen, fulltext FROM worker_heartbeat WHERE singleton = 1")
      .first<{ last_seen: number; fulltext: number }>();
    return Response.json({ authenticated: true, configured: true, missing: [],
      fulltext: Boolean(heartbeat?.fulltext), worker_online: heartbeat !== null && heartbeat.last_seen > now() - LEASE_SECONDS });
  }
  if (request.method === "POST" && path === "/api/logout") {
    return Response.json({ authenticated: false, logout_url: "/cdn-cgi/access/logout" });
  }
  if (request.method === "GET" && path === "/api/briefs") {
    const rows = await env.DB.prepare("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 200").all<JobRow>();
    return Response.json({ briefs: rows.results.map(row => {
      const job = snapshot(row, env);
      return Object.fromEntries(["id", "question", "status", "message", "updated_at", "report_url"].map(key => [key, job[key]]));
    }) });
  }
  if (request.method === "POST" && path === "/api/briefs") return createBrief(env, data);
  const detail = path.match(/^\/api\/briefs\/([a-f0-9]{32})$/);
  if (request.method === "GET" && detail) return Response.json(snapshot(await getJob(env, detail[1]), env));
  const action = path.match(/^\/api\/briefs\/([a-f0-9]{32})\/(approve|checkpoint|resume)$/);
  if (request.method === "POST" && action) return enqueue(env, action[1], action[2] as Exclude<CommandKind, "draft">, data);
  const report = path.match(/^\/briefs\/([a-f0-9]{32})\/report$/);
  if (request.method === "GET" && report) {
    const row = await getJob(env, report[1]);
    if (row.status !== "done" || row.report === null) throw new HttpError("摘要尚未完成。", 404);
    return new Response(row.report, { headers: { "Content-Type": "text/html; charset=utf-8" } });
  }
  if (request.method === "GET" || request.method === "HEAD") {
    const assets: Record<string, string> = { "/": "/index.html", "/evidence-brief": "/index.html",
      "/evidence-brief/": "/index.html", "/static/app.js": "/app.js", "/static/styles.css": "/styles.css" };
    if (Object.hasOwn(assets, path)) {
      url.pathname = assets[path];
      const asset = await env.ASSETS.fetch(new Request(url, { method: request.method }));
      // The asset service may canonicalize /index.html to /. Follow internally,
      // since redirecting the client would loop through the authenticated route.
      if (asset.status >= 300 && asset.status < 400 && path !== "/static/app.js" && path !== "/static/styles.css") {
        url.pathname = "/";
        return env.ASSETS.fetch(new Request(url, { method: request.method }));
      }
      return asset;
    }
  }
  throw new HttpError("找不到此頁面。", 404);
}

function secure(response: Response, path: string): Response {
  const result = new Response(response.body, response);
  result.headers.set("Cache-Control", "no-store");
  result.headers.set("X-Content-Type-Options", "nosniff");
  result.headers.set("Referrer-Policy", "same-origin");
  result.headers.set("X-Frame-Options", "DENY");
  result.headers.set("Content-Security-Policy", /^\/briefs\/[a-f0-9]{32}\/report$/.test(path)
    ? "sandbox allow-scripts allow-popups allow-modals; default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
    : "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
  return result;
}

/** Dependency injection is for tests; production always uses requireAccess. */
export function createHandler(verify: typeof requireAccess = requireAccess) {
  return async (request: Request, env: Env): Promise<Response> => {
    let response: Response;
    try {
      const path = new URL(request.url).pathname;
      if (env.PUBLIC_EDGE === "true" && path !== "/discord/interactions" && !publicReportPath(path)) {
        throw new HttpError("找不到此頁面。", 404);
      }
      if (path === "/discord/interactions") {
        response = await discordInteraction(request, env);
      } else {
        if (env.PUBLIC_EDGE !== "true" && !publicReportPath(path)) await verify(request, env);
        response = await route(request, env);
      }
    } catch (error) {
      if (error instanceof HttpError) response = Response.json({ detail: error.message }, { status: error.status });
      else {
        console.error("Evidence Brief relay request failed", error instanceof Error ? error.name : "UnknownError");
        response = Response.json({ detail: "服務暫時無法完成請求，請稍後再試。" }, { status: 500 });
      }
    }
    return secure(response, new URL(request.url).pathname);
  };
}

export default { fetch: createHandler() } satisfies ExportedHandler<Env>;
