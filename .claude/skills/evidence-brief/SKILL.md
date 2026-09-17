---
name: evidence-brief
description: Turn one clinical question or idea into a single easy-to-read evidence page — PubMed-only PICO search (Q1, ≥2016, DOI + CrossRef verified), GRADE certainty, verdict, plain-language + professional write-up. The token-lean sibling of /claim-appraise.
user_invocable: true
---

# Evidence Brief (`/evidence-brief "<question or idea>"`)

One question in, one page out. Python does everything deterministic (search,
filters, CrossRef gate, verdict math, citation whitelist, rendering) and
`lit-review brief next` decides what happens when — you never sequence the
stages yourself. You dispatch only the subagents `next` asks for: batched
screening agents, a GRADE agent, a verify agent and a writer agent per PICO. Nothing else. Keep your own
context small: never paste study JSON or task prompts into it — pass file paths.

What this deliberately does NOT do (use `/claim-appraise` if you need them):
Scopus/Embase, PRISMA 27-item audit loop, adversarial 3-lens verify,
OpenEvidence cross-check, argdown, bilingual dual-audience site, Cloudflare.

## Setup
```bash
cd <repo> && source .venv/bin/activate   # or .venv/bin/lit-review directly
```
`.env` needs `PUBMED_EMAIL` (and ideally `UNPAYWALL_EMAIL`; same address is fine).
`SCOPUS_API_KEY` is optional: with it, a most-cited pass is added per PICO.

## Year window
`--min-year` defaults to **2000**, not 2016. The 2016 cutoff belongs to
`/claim-appraise` (is this current claim true?); a settled question — exercise
physiology, diagnostics, surgical technique — has its landmark trials and
meta-analyses well before it, and a 2016 gate silently removes exactly the
studies that answer it. Raise it only when the question is about something
recent. Unranked journals are admitted and screened rather than dropped, and
the page reports how many there were.

## Step 1 — Decompose (you, inline; no subagent)

Read the question. Write 1–3 PICO sub-questions to `output/<slug>/picos.json`
(pick a short kebab-case slug). Set `claim_direction` per sub-question:
`"benefit"` when the idea is "X helps" (a beneficial effect confirms it),
`"harm"` when the idea is "X increases risk" (a harmful effect confirms it).

```json
{
  "question": "<the user's question, verbatim>",
  "picos": [
    {
      "pico_id": "pico_01",
      "population": "...", "intervention": "...", "comparator": "...", "outcome": "...",
      "outcome_domain": "snake_case_tag",
      "claim_direction": "benefit | harm",
      "question_text": "One natural-language sentence shown on the page (繁體中文 OK)",
      "primary_terms": ["4-8 English search terms for the intervention/population"],
      "secondary_terms": ["English outcome terms"],
      "mesh_terms": [],
      "priority": 1
    }
  ]
}
```

Search terms go into PubMed as `(term1 OR term2 ...) AND (outcome1 OR ...)`,
so use terms that appear in titles/abstracts — short noun phrases, not
sentences. `outcome` is used as a search term too, so keep it short. Show the
PICOs to the user with `AskUserQuestion` (approve / edit) before searching — a
wrong PICO wastes the run.

## Step 2 — Pre-flight (Python, 0 tokens)
```bash
lit-review brief doctor <slug>     # picos.json contract, credentials, whatever is on disk
lit-review brief preview <slug>    # PubMed hit counts: whole query, design sweep,每個 term
```
`doctor` exits 1 on a broken contract (missing `claim_direction`, no
`question_text`, phrase-length search terms) — fix `picos.json` and re-run it.
Its warnings (no Scopus key, no JournalFetcher) are informational: say what the
brief will lack and continue.

`preview` prices the search before it runs. **A term with 0 hits must be fixed
before continuing** — replace it with the wording PubMed actually indexes and
re-run `preview`. Terms over 50 000 hits are flagged yellow: fine inside an OR
group, but if the whole query is in the tens of thousands the PICO is too broad
to narrow into a page. If the whole-query count is under ~20, widen the terms
or say so now rather than delivering an empty brief.

## Step 3 — The loop (`next`, until it says rendered)
```bash
lit-review brief next <slug>
```
`next` runs every deterministic stage that is ready, prints what it did, and
then stops in exactly one of four states. Read the last block it printed:

**checkpoint** — the included studies and top gate near-misses (`brief gaps`)
are ready for automatic Opus expert review. Ask Opus to review the evidence
set for each PICO. It may nominate an identifier only when copied exactly from
the candidate gap list; pass those identifiers through `lit-review brief add`
and record the review with `lit-review brief checkpoint`. Then run `next` again.
Do not ask the user to review or confirm this stage.

**dispatch** — it printed exactly which agents to launch, e.g.
```
→ dispatch 2 agent(s) in ONE message:
   Screen pico_01  model=haiku   Read /abs/…/tasks/screen_pico_01_01.md and do exactly what it says.
   Write pico_02   model=opus  Read /abs/…/tasks/write_pico_02.md and do exactly what it says.
```
Launch **exactly those**, all in one message (parallel), using the model printed
on each line, with the printed prompt verbatim as the agent prompt — do not add, drop or reorder
agents, and do not read the task files yourself. Wait for all of them, then run
`next` again. If an agent's output is unusable, `next` will ask for it again;
re-dispatch only what it names.

**rendered** — `output/<slug>/brief.html` is written. Go to Step 4.

**done** — nothing left to do; the page is already current.

If `next` exits 1 it prints one line saying what is wrong (a writer cited a DOI
outside the included set, a missing artefact). Fix that and re-run; never edit
the JSON by hand to make a gate pass.

Use `lit-review brief status <slug>` any time to see where each PICO stands
without running anything.

## Step 4 — Deliver
Publish `output/<slug>/brief.html` as an Artifact (it is self-contained, no
external assets) and give the user the link plus the local path. Summarise in
3–5 lines: overall verdict, per-PICO verdict + certainty, number of references,
and whether the brief is full-text verified or abstract-level.

## What `next` runs under the hood
Each of these is still a command, for when one stage has to be redone or
inspected on its own. In normal use you do not call them.

| stage | command | writes |
|---|---|---|
| search | `lit-review brief search <slug>` | `pico_NN.json` (3 passes + CrossRef snowballing) |
| automatic expert review | `lit-review brief studies <slug>` / `add` | `expert_added` in `pico_NN.json` |
| gate near-misses | `lit-review brief gaps <slug>` | reads `pico_NN.excluded.json` |
| screen task | `lit-review brief screen-tasks <slug>` / `collect-screen` | `tasks/screen_NN_BB.md` → Haiku writes batch JSON; Python writes `pico_NN.screen.json` |
| GRADE task | `lit-review brief grade-tasks <slug>` | `tasks/grade_NN.md` → agent writes `grade_pico_NN.json` |
| full text | `lit-review brief fulltext <slug>` | `pico_NN.fulltext.json` (body of evidence only, via JournalFetcher) |
| verify task | `lit-review brief verify-tasks <slug>` | `tasks/verify_NN.md` → agent overwrites `grade_pico_NN.json` (abstract copy kept as `*.abstract.json`) |
| writer task | `lit-review brief write-tasks <slug>` | `tasks/write_NN.md` → agent writes `write_pico_NN.json` |
| citation gate | `lit-review brief check <slug>` | exit 1 on any DOI outside the included set |
| render | `lit-review brief render <slug>` | `brief.html` |

Python recomputes the final certainty from the domain downgrades; the GRADE
agent's own label is advisory. Screening is authoritative: GRADE may narrow
the screened-in set but cannot add a screened-out study. Only the studies the GRADE agent named as
`body_of_evidence` are fetched in full text (typically 2–6), never the whole
included set; the page shows "全文複核 n／m". Without `JOURNALFETCHER_DIR`
(default `~/JournalFetcher`) `next` skips the fulltext and verify stages and
the brief is abstract-level — say so when you deliver it.

When a full text fetch fails, `brief doctor` prints the exact manual drop-in
path. Put the publisher PDF at
`output/<slug>/fulltext/pdf/<doi_with_slashes_replaced_by_underscores>.pdf` and
run `next` again; it notices the file and reconverts it automatically.

State is only the files on disk, so any stage can be redone by deleting its
output, and `next` notices staleness by itself: adding a study re-runs GRADE
for that PICO, and a new GRADE re-runs the writer.

## Budget
≈3 agents plus one Haiku agent per screening batch per PICO. If
you find yourself dispatching agents that `next` did not name, something
is wrong — stop and ask.
