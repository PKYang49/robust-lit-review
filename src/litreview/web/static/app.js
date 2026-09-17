// @ts-check
"use strict";

/** @typedef {{[key:string]: unknown, pico_id:string, population?:string, intervention?:string, comparator?:string, outcome?:string, question_text?:string, primary_terms?:string[], secondary_terms?:string[], claim_direction?:string}} Pico */
/** @typedef {{title?:string, doi?:string, pmid?:string, year?:number, pub_type?:string|string[], cited_by_count?:number}} Study */
/** @typedef {{pico_id:string, query?:number, design_sweep?:number, terms?:{group?:string, term:string, hits:number}[]}} Preview */
/** @typedef {{id:string, question:string, status:string, message?:string, updated_at?:string, picos?:Pico[], preview?:Preview[], studies?:Record<string,Study[]>, gaps?:Record<string,unknown[]>, report_url?:string, events?:unknown[], error?:string, additions_result?:{added?:string[], rejected?:Record<string,string>}}} Brief */
/** @typedef {{authenticated:boolean, configured:boolean, missing:string[], fulltext:boolean, worker_online?:boolean}} Configuration */

/** @template {HTMLElement} T @param {string} id @returns {T} */
function byId(id) {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing UI element: ${id}`);
  return /** @type {T} */ (element);
}

/** @template {keyof HTMLElementTagNameMap} K @param {K} tag @param {string} [className] @param {unknown} [content] @returns {HTMLElementTagNameMap[K]} */
function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined && content !== null) node.textContent = String(content);
  return node;
}

const ui = {
  auth: byId("auth-screen"),
  authStatus: byId("auth-status"),
  login: /** @type {HTMLFormElement} */ (byId("login-form")),
  password: /** @type {HTMLInputElement} */ (byId("password")),
  loginError: byId("login-error"),
  retry: /** @type {HTMLButtonElement} */ (byId("connection-retry")),
  workspace: byId("workspace"),
  welcome: byId("welcome"),
  view: byId("brief-view"),
  history: byId("history-list"),
  historySearch: /** @type {HTMLInputElement} */ (byId("history-search")),
  alert: byId("global-alert"),
  configWarning: byId("config-warning"),
  queryForm: /** @type {HTMLFormElement} */ (byId("query-form")),
  question: /** @type {HTMLTextAreaElement} */ (byId("question")),
  querySubmit: /** @type {HTMLButtonElement} */ (byId("query-submit")),
  refresh: /** @type {HTMLButtonElement} */ (byId("refresh")),
};

const statuses = /** @type {Record<string, string>} */ ({
  drafting: "正在拆解問題", pico_review: "待確認 PICO", searching: "正在搜尋文獻",
  checkpoint: "Opus 專家核對中", running: "正在評讀與彙整", done: "摘要已完成",
  error: "需要處理", interrupted: "工作已中斷",
});
const activePhases = new Set(["drafting", "searching", "checkpoint", "running"]);
const stepNames = ["確認 PICO", "文獻搜尋", "Opus 專家核對", "證據摘要"];
const stepIndexes = /** @type {Record<string, number>} */ ({drafting:0, pico_review:0, searching:1, checkpoint:2, running:3, done:4});

/** @type {Configuration|null} */
let configuration = null;
/** @type {Brief[]} */
let briefs = [];
/** @type {Brief|null} */
let currentBrief = null;
let selectedId = "";
let pending = false;
let loginPending = false;
let historyLoaded = false;
let historyVersion = 0;
let detailVersion = 0;
let selectionVersion = 0;
/** @type {number|undefined} */
let pollTimer;
/** @type {AbortController|null} */
let detailController = null;

class ApiError extends Error {
  /** @param {string} message @param {number} status */
  constructor(message, status) { super(message); this.status = status; }
}

/** @param {string} path @param {RequestInit} [options] @returns {Promise<any>} */
async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  let response;
  try {
    response = await fetch(path, { ...options, headers, credentials:"same-origin", cache:"no-store" });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError("暫時無法連線到工作站，請檢查網路後重試。", 0);
  }
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin("登入已逾時，請重新輸入密碼。");
    const detail = typeof payload?.detail === "string" ? payload.detail : "工作站無法完成此操作，請稍後重試。";
    throw new ApiError(detail, response.status);
  }
  return payload;
}

/** @param {unknown} error @returns {string} */
function errorMessage(error) { return error instanceof Error ? error.message : "發生未預期的錯誤，請稍後重試。"; }
/** @param {string} message */
function announce(message) { byId("announcement").textContent = message; }
/** @param {string} [message] */
function showError(message = "") { ui.alert.textContent = message; ui.alert.hidden = !message; }
/** @param {string} [message] */
function showLogin(message = "") {
  clearTimeout(pollTimer);
  detailController?.abort();
  detailVersion++;
  historyVersion++;
  configuration = null;
  briefs = [];
  currentBrief = null;
  historyLoaded = false;
  ui.history.replaceChildren();
  ui.view.replaceChildren();
  ui.workspace.hidden = true;
  ui.auth.hidden = false;
  ui.authStatus.hidden = true;
  ui.login.hidden = false;
  ui.retry.hidden = true;
  ui.loginError.textContent = message;
  ui.password.value = "";
}

/** @param {string|undefined} value @returns {string} */
function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-TW", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}).format(date);
}

function renderConfiguration() {
  const missing = configuration?.missing || [];
  const configured = configuration?.configured === true;
  ui.querySubmit.disabled = !configured || pending;
  ui.configWarning.hidden = configured;
  ui.configWarning.textContent = configured ? "" : `工作站尚未完成設定，暫時無法建立查詢。${missing.length ? `尚缺：${missing.join("、")}` : "請檢查伺服器設定。"}`;
  const workerWarning = byId("worker-warning");
  workerWarning.hidden = configuration?.worker_online !== false;
  workerWarning.textContent = configuration?.worker_online === false ? "Mac 目前離線，送出後會排隊，待主機上線處理。" : "";
}

async function bootstrap() {
  ui.retry.hidden = true;
  ui.authStatus.hidden = false;
  ui.authStatus.textContent = "正在連線…";
  try {
    const config = /** @type {Configuration} */ (await api("/api/config"));
    if (!config.authenticated) { showLogin(); return; }
    configuration = config;
    ui.auth.hidden = true;
    ui.workspace.hidden = false;
    renderConfiguration();
    await Promise.all([loadHistory(), navigateFromHash()]);
  } catch (error) {
    ui.authStatus.textContent = errorMessage(error);
    ui.retry.hidden = false;
  }
}

function renderHistory() {
  const query = ui.historySearch.value.trim().toLocaleLowerCase();
  const filtered = briefs.filter(brief => brief.question.toLocaleLowerCase().includes(query));
  byId("history-count").textContent = String(briefs.length);
  ui.history.replaceChildren();
  if (!filtered.length) {
    ui.history.append(el("p", "history-empty", historyLoaded ? (query ? "找不到符合的查詢。" : "你的第一份證據摘要，從這裡開始。") : "正在載入紀錄…"));
    return;
  }
  for (const brief of filtered) {
    const button = el("button", `history-item${brief.id === selectedId ? " active" : ""}`);
    button.type = "button";
    if (brief.id === selectedId) button.setAttribute("aria-current", "page");
    button.append(el("span", "history-title", brief.question));
    const meta = el("span", "history-meta");
    meta.append(el("span", `history-status ${knownStatus(brief.status)}`, statusLabel(brief.status)), el("span", "", formatDate(brief.updated_at)));
    button.append(meta);
    button.addEventListener("click", () => navigate(brief.id));
    ui.history.append(button);
  }
}

async function loadHistory() {
  const version = ++historyVersion;
  try {
    const data = await api("/api/briefs");
    if (version !== historyVersion || !configuration) return;
    briefs = Array.isArray(data?.briefs) ? data.briefs : [];
    historyLoaded = true;
    renderHistory();
  } catch (error) {
    if (version !== historyVersion) return;
    if (!historyLoaded) ui.history.replaceChildren(el("p", "history-empty", "紀錄載入失敗，請按重新整理。"));
    if (configuration) showError(errorMessage(error));
  }
}

/** @param {string} status */
function knownStatus(status) { return Object.prototype.hasOwnProperty.call(statuses, status) ? status : "unknown"; }
/** @param {string} status */
function statusLabel(status) { return statuses[knownStatus(status)] || "狀態更新中"; }
/** @param {string} id */
function navigate(id) {
  const hash = id ? `#brief=${encodeURIComponent(id)}` : "";
  if (window.location.hash === hash) void navigateFromHash();
  else if (!hash) { history.pushState(null, "", window.location.pathname + window.location.search); void navigateFromHash(); }
  else window.location.hash = hash;
}

async function navigateFromHash() {
  clearTimeout(pollTimer);
  detailController?.abort();
  detailVersion++;
  selectionVersion++;
  selectedId = new URLSearchParams(window.location.hash.slice(1)).get("brief") || "";
  currentBrief = null;
  showError();
  ui.welcome.hidden = Boolean(selectedId);
  ui.view.hidden = !selectedId;
  document.title = "Evidence Brief · 臨床證據查詢";
  renderHistory();
  if (!configuration) return;
  if (selectedId) {
    const loading = el("div", "loading-state");
    loading.setAttribute("role", "status");
    loading.append(el("span", "spinner"), el("p", "", "正在載入證據查詢…"));
    ui.view.replaceChildren(loading);
    await loadBrief();
  } else {
    renderConfiguration();
  }
}

/** @param {boolean} [quiet] */
async function loadBrief(quiet = false) {
  const id = selectedId;
  if (!id || !configuration) return;
  const version = ++detailVersion;
  detailController?.abort();
  detailController = new AbortController();
  try {
    const brief = /** @type {Brief} */ (await api(`/api/briefs/${encodeURIComponent(id)}`, {signal:detailController.signal}));
    if (version !== detailVersion || selectedId !== id || !configuration) return;
    if (pending) { schedulePoll(); return; }
    const draft = currentBrief?.status === brief.status ? captureReviewDraft() : null;
    currentBrief = brief;
    renderBrief(brief);
    if (draft) restoreReviewDraft(draft);
    document.title = `${brief.question.slice(0, 55)} · Evidence Brief`;
    schedulePoll();
  } catch (error) {
    if (version !== detailVersion || !configuration || (error instanceof Error && error.name === "AbortError")) return;
    showError(errorMessage(error));
    if (!quiet && !currentBrief) {
      const panel = el("section", "panel error-panel");
      panel.append(el("h2", "", "目前無法載入這份查詢"), el("p", "panel-intro", "可使用上方的重新整理再試一次，或從查詢紀錄選擇其他項目。"));
      ui.view.replaceChildren(panel);
    }
    if (currentBrief && activePhases.has(currentBrief.status)) schedulePoll(7000);
  }
}

/** @param {number} [delay] */
function schedulePoll(delay = 3000) {
  clearTimeout(pollTimer);
  if (!configuration || !currentBrief || !activePhases.has(currentBrief.status) || document.hidden) return;
  pollTimer = window.setTimeout(async () => {
    if (pending) { schedulePoll(); return; }
    await Promise.all([loadBrief(true), loadHistory()]);
  }, delay);
}

/** @param {Brief} brief */
function renderBrief(brief) {
  ui.view.replaceChildren();
  const header = el("header", "brief-header");
  header.append(el("p", "eyebrow", "YOUR EVIDENCE WORKSPACE"), el("h1", "", brief.question));
  const meta = el("div", "brief-meta");
  meta.append(el("span", `badge ${knownStatus(brief.status)}`, statusLabel(brief.status)));
  if (brief.updated_at) meta.append(el("span", "", `更新於 ${formatDate(brief.updated_at)}`));
  header.append(meta);
  ui.view.append(header, renderSteps(brief.status));
  if (activePhases.has(brief.status)) {
    const progress = el("section", "progress-panel");
    progress.setAttribute("role", "status");
    const title = el("h2", "progress-title");
    title.append(el("span", "spinner"), document.createTextNode(statusLabel(brief.status)));
    progress.append(title, el("p", "", brief.message || "工作站正在處理，你可以稍後回來查看。"), el("p", "", "關閉此頁不會停止工作；重新登入後可接續查看。"));
    ui.view.append(progress);
  }
  if (brief.status === "pico_review") ui.view.append(renderPicoReview(brief));
  else if (brief.status === "checkpoint") ui.view.append(renderAutoCheckpoint(brief));
  else if (brief.status === "done") ui.view.append(renderResult(brief));
  else if (brief.status === "error" || brief.status === "interrupted") ui.view.append(renderFailure(brief));
  if (brief.status !== "pico_review" && brief.status !== "checkpoint" && brief.picos?.length) ui.view.append(renderPicoSummary(brief));
  if (brief.events?.length) ui.view.append(renderEvents(brief.events));
  const footer = el("footer", "brief-footer");
  footer.append(el("span", "", "Evidence Brief · 可追溯的臨床證據"), el("span", "", "在其他裝置完成操作後，按重新整理同步。"));
  ui.view.append(footer);
}

/** @param {string} status */
function renderSteps(status) {
  const list = el("ol", "steps");
  list.setAttribute("aria-label", "查詢進度");
  const stage = stepIndexes[status] ?? -1;
  stepNames.forEach((name, index) => {
    const item = el("li", `step${stage > index ? " complete" : stage === index ? " current" : ""}`);
    if (stage === index) item.setAttribute("aria-current", "step");
    const number = el("span", "step-number", stage > index ? "✓" : String(index + 1));
    number.setAttribute("aria-hidden", "true");
    item.append(number, el("span", "", name));
    list.append(item);
  });
  return list;
}

/** @returns {Map<string,string>|null} */
function captureReviewDraft() {
  const form = ui.view.querySelector("form[data-dirty='true']");
  if (!form) return null;
  const values = new Map();
  for (const input of form.querySelectorAll("input,textarea,select")) {
    if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement || input instanceof HTMLSelectElement) values.set(input.id, input.value);
  }
  return values;
}

/** @param {Map<string,string>} values */
function restoreReviewDraft(values) {
  const form = ui.view.querySelector("form");
  if (!form) return;
  for (const input of form.querySelectorAll("input,textarea,select")) {
    if ((input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement || input instanceof HTMLSelectElement) && values.has(input.id)) input.value = values.get(input.id) || "";
  }
  form.dataset.dirty = "true";
}

/** @param {string} id @param {string} label @param {string} value @param {{wide?:boolean, multiline?:boolean, hint?:string, required?:boolean}} [options] */
function field(id, label, value, options = {}) {
  const wrap = el("div", `field${options.wide ? " wide" : ""}`);
  const caption = el("label", "", label);
  caption.htmlFor = id;
  const input = options.multiline ? el("textarea") : el("input");
  input.id = id;
  input.name = id;
  input.value = value;
  input.required = options.required || false;
  if (input instanceof HTMLTextAreaElement) input.rows = 2;
  if (options.hint) {
    input.setAttribute("aria-describedby", `${id}-hint`);
    const hint = el("p", "field-hint", options.hint);
    hint.id = `${id}-hint`;
    wrap.append(caption, input, hint);
  } else wrap.append(caption, input);
  return {wrap, input};
}

/** @param {Brief} brief */
function renderPicoReview(brief) {
  const panel = el("section", "panel");
  panel.append(el("span", "section-kicker", "01 / DEFINE YOUR QUESTION"), el("h2", "", "確認你的 PICO"), el("p", "panel-intro", "確認拆解是否符合你的臨床問題。你可以直接修改族群、介入與搜尋詞，確認後才會開始搜尋。"));
  const form = el("form");
  form.addEventListener("input", () => { form.dataset.dirty = "true"; });
  /** @type {{original:Pico, inputs:Record<string,HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement>}[]} */
  const editors = [];
  for (const [index, pico] of (brief.picos || []).entries()) {
    const card = el("fieldset", "pico-card");
    const legend = el("legend", "pico-heading");
    legend.append(el("span", "count", `PICO ${index + 1}`), el("span", "", pico.pico_id));
    card.append(legend);
    const grid = el("div", "field-grid");
    /** @type {Record<string,HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement>} */
    const inputs = {};
    const specs = [
      {key:"population", label:"P · 研究族群", required:true},
      {key:"intervention", label:"I · 介入措施／暴露", required:true},
      {key:"comparator", label:"C · 比較對象", required:false},
      {key:"outcome", label:"O · 關注結果", required:true},
      {key:"question_text", label:"完整研究問題", wide:true, multiline:true, required:true},
      {key:"primary_terms", label:"主要搜尋詞", multiline:true, hint:"介入／暴露相關詞；每行一詞，可用逗號分隔。"},
      {key:"secondary_terms", label:"次要搜尋詞", multiline:true, hint:"結果相關詞；每行一詞，可用逗號分隔。"},
    ];
    for (const spec of specs) {
      const raw = pico[spec.key];
      const value = Array.isArray(raw) ? raw.join("\n") : typeof raw === "string" ? raw : "";
      const control = field(`pico-${index}-${spec.key}`, spec.label, value, spec);
      inputs[spec.key] = control.input;
      grid.append(control.wrap);
    }
    const direction = el("div", "field wide");
    const label = el("label", "", "要檢驗的主張方向");
    label.htmlFor = `pico-${index}-claim-direction`;
    const select = el("select");
    select.id = label.htmlFor;
    select.name = "claim_direction";
    for (const [value, text] of [["benefit", "效益：介入改善結果"], ["harm", "傷害：暴露增加風險"]]) {
      const option = el("option", "", text);
      option.value = value;
      select.append(option);
    }
    select.value = pico.claim_direction || "benefit";
    inputs.claim_direction = select;
    direction.append(label, select);
    grid.append(direction);
    card.append(grid);
    const preview = brief.preview?.find(item => item.pico_id === pico.pico_id);
    if (preview) card.append(renderPreview(preview));
    form.append(card);
    editors.push({original:pico, inputs});
  }
  const actions = el("div", "form-actions");
  actions.append(el("p", "", "這一步保留你的判斷。送出後將依確認的 PICO 搜尋文獻。"));
  // A settled question's landmark trials often predate 2016, so the year window
  // belongs to the asker rather than being fixed for every question.
  const yearRow = el("label", "field");
  yearRow.append(el("span", "", "收錄年份起自"));
  const yearInput = el("input");
  Object.assign(yearInput, {type: "number", min: "1960", max: String(new Date().getFullYear()),
                            step: "1", value: String(brief.min_year || 2000), name: "min_year"});
  yearRow.append(yearInput, el("small", "", "成熟題目（如運動生理學）的關鍵證據常早於 2016 年。"));
  actions.append(yearRow);
  const submit = el("button", "button primary", "確認 PICO，開始搜尋 →");
  submit.type = "submit";
  submit.disabled = !editors.length;
  actions.append(submit);
  form.append(actions);
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (pending || !form.reportValidity()) return;
    const picos = editors.map(({original, inputs}) => {
      const updated = {...original};
      for (const [key, input] of Object.entries(inputs)) updated[key] = key.endsWith("_terms") ? splitTerms(input.value) : input.value.trim();
      return updated;
    });
    void mutate(brief.id, "approve", {picos, min_year: Number(yearInput.value)}, form,
                "PICO 已確認，開始搜尋文獻。");
  });
  panel.append(form);
  return panel;
}

/** @param {string} value */
function splitTerms(value) { return [...new Set(value.split(/[\n,，;；]+/).map(term => term.trim()).filter(Boolean))]; }

/** @param {Preview} preview */
function renderPreview(preview) {
  const detail = el("details", "preview");
  detail.append(el("summary", "", "查看初始搜尋預覽"));
  const counts = el("div", "preview-counts");
  for (const [label, value] of [["整體查詢", preview.query], ["研究設計搜尋", preview.design_sweep]]) {
    const item = el("span", "", `${label} 命中數`);
    item.prepend(el("strong", "", typeof value === "number" ? value.toLocaleString("zh-TW") : "—"));
    counts.append(item);
  }
  detail.append(counts, el("p", "field-hint", "命中數是搜尋規模預覽，並非最終納入研究數。修改 PICO 後會在正式搜尋時套用。"));
  const terms = el("div", "preview-terms");
  for (const term of preview.terms || []) {
    const chip = el("span", "term-chip", term.term);
    chip.append(el("b", "", typeof term.hits === "number" ? term.hits.toLocaleString("zh-TW") : "—"));
    terms.append(chip);
  }
  detail.append(terms);
  return detail;
}

/** @param {Brief} brief */
function renderAutoCheckpoint(brief) {
  const panel = el("section", "panel");
  panel.append(el("span", "section-kicker", "03 / EXPERT CHECKPOINT"), el("h2", "", "Opus 正在核對納入研究"), el("p", "panel-intro", "系統會自動檢視搜尋結果與候選文獻，完成後直接繼續評讀與產生摘要。這一步不需要你操作。"));
  if (brief.additions_result) {
    const result = brief.additions_result;
    if (result.added?.length) panel.append(el("p", "panel-intro", `已補入 ${result.added.length} 篇研究：${result.added.join("；")}`));
    if (result.rejected && Object.keys(result.rejected).length) {
      const rejected = el("section", "progress-panel");
      rejected.append(el("h3", "progress-title", "部分候選文獻未能納入"), el("p", "", "Opus 已完成核對，系統會繼續處理其餘證據。"));
      const list = el("ul", "gap-list");
      for (const [identifier, reason] of Object.entries(result.rejected)) list.append(el("li", "", `${identifier}：${reason}`));
      rejected.append(list);
      panel.append(rejected);
    }
  }
  for (const [index, pico] of (brief.picos || []).entries()) {
    const card = el("section", "pico-card");
    const heading = el("div", "pico-heading");
    const studies = brief.studies?.[pico.pico_id] || [];
    heading.append(el("h3", "", `PICO ${index + 1} · ${pico.pico_id}`), el("span", "count", `${studies.length} 篇`));
    card.append(heading, el("p", "pico-summary", pico.question_text || ""));
    if (studies.length) {
      const list = el("ol", "study-list");
      for (const study of studies) list.append(renderStudy(study));
      card.append(list);
    } else card.append(el("p", "empty-studies", "目前沒有納入研究，系統會保留此證據缺口並繼續處理。"));
    const gaps = brief.gaps?.[pico.pico_id] || [];
    if (gaps.length) {
      const list = el("ul", "gap-list");
      for (const gap of gaps) list.append(el("li", "", readableValue(gap)));
      card.append(list);
    }
    panel.append(card);
  }
  return panel;
}

/** @param {Study} study */
function renderStudy(study) {
  const item = el("li", "study");
  item.append(el("h4", "study-title", study.title || "未提供研究標題"));
  const meta = el("div", "study-meta");
  if (study.year) meta.append(el("span", "", study.year));
  if (study.pub_type) meta.append(el("span", "", Array.isArray(study.pub_type) ? study.pub_type.join(" · ") : study.pub_type));
  if (typeof study.cited_by_count === "number") meta.append(el("span", "", `引用 ${study.cited_by_count.toLocaleString("zh-TW")}`));
  item.append(meta);
  const links = el("div", "study-links");
  const pmid = String(study.pmid || "").trim();
  const doi = String(study.doi || "").trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "").replace(/^doi:\s*/i, "");
  if (/^\d+$/.test(pmid)) links.append(externalLink(`https://pubmed.ncbi.nlm.nih.gov/${pmid}/`, `PubMed · ${pmid} ↗`));
  if (/^10\.\d{4,9}\/\S+$/i.test(doi)) links.append(externalLink(`https://doi.org/${doi.split("/").map(encodeURIComponent).join("/")}`, "DOI 原始文獻 ↗"));
  item.append(links);
  return item;
}

/** @param {string} url @param {string} label */
function externalLink(url, label) {
  const link = el("a", "", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

/** @param {Brief} brief */
function renderResult(brief) {
  const panel = el("section", "panel result-panel");
  panel.append(el("div", "result-icon", "✓"), el("span", "section-kicker", "YOUR BRIEF IS READY"), el("h2", "", "證據摘要已完成"), el("p", "panel-intro", "查看完整報告中的證據結論、品質評估與研究來源。報告可在這個工作站的其他裝置開啟。"));
  const link = externalLink(brief.report_url || `/briefs/${encodeURIComponent(brief.id)}/report`, "閱讀完整證據摘要 ↗");
  link.className = "button primary";
  panel.append(link);
  return panel;
}

/** @param {Brief} brief */
function renderFailure(brief) {
  const panel = el("section", "panel error-panel");
  panel.append(el("h2", "", brief.status === "interrupted" ? "工作暫時中斷" : "此查詢需要處理"), el("p", "panel-intro", "已完成的進度會保留。確認工作站連線與設定後，可以嘗試接續執行。"));
  if (brief.error || brief.message) panel.append(el("p", "error-detail", brief.error || brief.message));
  const button = el("button", "button primary", "接續執行 →");
  button.type = "button";
  button.addEventListener("click", () => { void mutate(brief.id, "resume", {}, panel, "正在接續執行。"); });
  panel.append(button);
  return panel;
}

/** @param {Brief} brief */
function renderPicoSummary(brief) {
  const panel = el("section", "panel");
  panel.append(el("h2", "", "本次研究問題"));
  for (const [index, pico] of (brief.picos || []).entries()) {
    const card = el("section", "pico-card");
    card.append(el("h3", "pico-heading", `PICO ${index + 1} · ${pico.pico_id}`), el("p", "pico-summary", pico.question_text || ""));
    const preview = brief.preview?.find(item => item.pico_id === pico.pico_id);
    if (preview) card.append(renderPreview(preview));
    panel.append(card);
  }
  return panel;
}

/** @param {unknown} value @returns {string} */
function readableValue(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const item = /** @type {Record<string,unknown>} */ (value);
    const message = item.message || item.detail || item.reason || item.text || item.event;
    if (typeof message === "string") return `${item.timestamp ? `${formatDate(String(item.timestamp))} · ` : ""}${message}`;
    return JSON.stringify(value);
  }
  return String(value ?? "");
}

/** @param {unknown[]} events */
function renderEvents(events) {
  const detail = el("details", "event-log");
  detail.append(el("summary", "", `執行紀錄 · ${events.length}`));
  const list = el("ol");
  for (const event of events.slice(-50)) list.append(el("li", "", readableValue(event)));
  detail.append(list);
  return detail;
}

/** @param {HTMLElement} container @param {boolean} disabled */
function setControlsDisabled(container, disabled) {
  for (const control of container.querySelectorAll("button,input,textarea,select")) {
    if (control instanceof HTMLButtonElement || control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement || control instanceof HTMLSelectElement) control.disabled = disabled;
  }
  container.setAttribute("aria-busy", String(disabled));
}

/** @param {string} id @param {string} action @param {unknown} payload @param {HTMLElement} container @param {string} success */
async function mutate(id, action, payload, container, success) {
  if (pending) return;
  pending = true;
  clearTimeout(pollTimer);
  detailController?.abort();
  detailVersion++;
  const selection = selectionVersion;
  setControlsDisabled(container, true);
  ui.refresh.disabled = true;
  showError();
  try {
    await api(`/api/briefs/${encodeURIComponent(id)}/${action}`, {method:"POST", body:JSON.stringify(payload)});
    announce(success);
    pending = false;
    if (selectedId === id && selectionVersion === selection) await loadBrief();
    await loadHistory();
  } catch (error) {
    if (configuration) showError(errorMessage(error));
  } finally {
    pending = false;
    setControlsDisabled(container, false);
    ui.refresh.disabled = false;
    renderConfiguration();
    if (configuration && selectedId && !currentBrief) void loadBrief();
    else schedulePoll();
  }
}

ui.queryForm.addEventListener("submit", async event => {
  event.preventDefault();
  const question = ui.question.value.trim();
  ui.question.setCustomValidity(question.length < 5 ? "請至少輸入 5 個字元，清楚描述臨床問題。" : "");
  if (pending || !configuration?.configured || !ui.queryForm.reportValidity()) return;
  pending = true;
  const selection = selectionVersion;
  setControlsDisabled(ui.queryForm, true);
  ui.refresh.disabled = true;
  ui.querySubmit.textContent = "正在建立查詢…";
  showError();
  try {
    const brief = /** @type {Brief} */ (await api("/api/briefs", {method:"POST", body:JSON.stringify({question})}));
    ui.question.value = "";
    updateQuestionCount();
    announce("已建立證據查詢。");
    pending = false;
    if (selectionVersion === selection) navigate(brief.id);
    await loadHistory();
  } catch (error) {
    if (configuration) showError(errorMessage(error));
  } finally {
    pending = false;
    setControlsDisabled(ui.queryForm, false);
    ui.refresh.disabled = false;
    ui.querySubmit.replaceChildren(document.createTextNode("建立證據查詢 "), el("span", "", "↗"));
    renderConfiguration();
    if (configuration && selectedId && !currentBrief) void loadBrief();
  }
});

function updateQuestionCount() {
  ui.question.setCustomValidity("");
  byId("question-count").textContent = `${ui.question.value.length.toLocaleString("zh-TW")} / 2,000`;
}

ui.login.addEventListener("submit", async event => {
  event.preventDefault();
  if (loginPending || !ui.login.reportValidity()) return;
  loginPending = true;
  setControlsDisabled(ui.login, true);
  ui.loginError.textContent = "";
  try {
    await api("/api/login", {method:"POST", body:JSON.stringify({password:ui.password.value})});
    ui.password.value = "";
    await bootstrap();
  } catch (error) { ui.loginError.textContent = errorMessage(error); }
  finally { loginPending = false; setControlsDisabled(ui.login, false); }
});

byId("logout").addEventListener("click", async () => {
  const button = /** @type {HTMLButtonElement} */ (byId("logout"));
  if (button.disabled) return;
  button.disabled = true;
  try {
    const result = await api("/api/logout", {method:"POST"});
    showLogin();
    if (typeof result?.logout_url === "string") {
      const destination = new URL(result.logout_url, window.location.origin);
      if (destination.origin === window.location.origin) window.location.assign(destination.href);
    }
  }
  catch (error) { showError(errorMessage(error)); }
  finally { button.disabled = false; }
});

ui.refresh.addEventListener("click", async () => {
  if (pending || ui.refresh.disabled) return;
  ui.refresh.disabled = true;
  showError();
  try {
    const config = /** @type {Configuration} */ (await api("/api/config"));
    if (!config.authenticated) { showLogin(); return; }
    configuration = config;
    renderConfiguration();
    await Promise.all([loadHistory(), selectedId ? loadBrief() : Promise.resolve()]);
    announce("已重新整理工作站。 ");
  } catch (error) { if (configuration) showError(errorMessage(error)); }
  finally { ui.refresh.disabled = false; }
});

ui.question.addEventListener("input", updateQuestionCount);
ui.historySearch.addEventListener("input", renderHistory);
ui.retry.addEventListener("click", () => { void bootstrap(); });
byId("new-query").addEventListener("click", () => { navigate(""); ui.question.focus(); });
for (const chip of document.querySelectorAll("[data-question]")) {
  chip.addEventListener("click", () => {
    if (pending) return;
    ui.question.value = chip.getAttribute("data-question") || "";
    updateQuestionCount();
    ui.question.focus();
  });
}
window.addEventListener("hashchange", () => { void navigateFromHash(); });
window.addEventListener("popstate", () => { if (configuration && !window.location.hash) void navigateFromHash(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(pollTimer);
  else if (!pending && configuration && currentBrief && activePhases.has(currentBrief.status)) void loadBrief(true);
});
void bootstrap();
