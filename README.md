# MIMIC

[![tests](https://github.com/shivangisinghal141-cloud/hermes-mimic/actions/workflows/tests.yml/badge.svg)](https://github.com/shivangisinghal141-cloud/hermes-mimic/actions/workflows/tests.yml)

**Show it once, then it never needs the model again.**

MIMIC is a [Hermes](https://hermes-agent.nousresearch.com) plugin. You perform a browser
task once; MIMIC records it, compiles it into a deterministic, parameterized tool, and
registers that tool live. Afterwards it runs with **zero LLM calls**. When the site changes
and a step breaks, it hands the failing step plus a DOM snapshot to Hermes, which
re-grounds that one step, patches the macro, and resumes from the checkpoint.

A skill is prose the model re-reads and re-improvisates on every run — slow, expensive,
flaky. MIMIC compiles to code, runs it deterministically, and only spends tokens on repair.
That is the whole difference.

```
record once ──► compile ──► macro JSON ──► live tool `mimic_invoice_entry`
                                 │                    │
                                 │                    ├── run for 1 record:  0 model calls
                                 │                    ├── run for 200 rows:  0 model calls
                                 │                    └── step breaks ──► 1 model call ──► patched ──► resumes
                                 └── plugin_data_dir()/macros/*.json  (data, never plugin code)
```

## What a run actually costs

| | A skill / pure prompting | MIMIC |
|---|---|---|
| Reads the page again | yes, every run | no |
| Re-decides selectors | yes, every run | no — recorded ladder |
| Model calls per run | many | **0** |
| Model calls when the site changes | redo everything | **1** for the one broken step |
| Batch of 200 records | 200 improvised workflows | 1 compile, 200 deterministic runs |

## Install

```bash
hermes plugins install shivangisinghal141-cloud/hermes-mimic   # or copy this directory
hermes plugins enable mimic
pip install playwright && python -m playwright install chromium
```

Playwright is declared as a plugin dependency; the browser binary is a one-time download.
Configuration lives on the plugin page (or `plugins.entries.mimic.settings` in
`config.yaml`): `headless`, `cdp_url` (attach to a Chrome you already have open), `strict`,
`heal`, `step_timeout_ms`.

## Quickstart

```
you:  "Every Monday I type the same five invoices into our AP portal."

agent → mimic_record_start(url="https://ap.example.com/invoices/new", name="invoice_entry")
        ... the workflow is performed once (by hand or by the agent's browser tool) ...
agent → mimic_record_stop(name="invoice_entry", description="Create one AP invoice from a row")
        → tool `mimic_invoice_entry` is registered live, with params:
          customer, invoice_number, amount, terms, notes

agent → mimic_invoice_entry(rows=[
          {"customer": "Acme", "invoice_number": "INV-1", "amount": "1200"},
          {"customer": "Globex", "invoice_number": "INV-2", "amount": "980"},
          ...
        ])
        → 8 rows done in seconds, llm_calls: 0
```

From then on the agent sees a tool whose description says exactly when to use it, and the
companion skill (`/skills → mimic`) explains the same to the model.

## How recording works

1. `recorder.js` is injected into the browser **context** (so every new document — including
   the page after a form submit — is instrumented).
2. It observes real DOM interactions: `click`, debounced `fill`, `select`, `check`, `Enter`.
   Clicks that are merely the first half of a `select`/`check` are dropped at compile time,
   and Enter-then-submit-click is de-duplicated.
3. For every target it records a **descriptor**: testid, id, accessible role + accessible
   name, label, visible text, a uniqueness-checked CSS path, absolute XPath.
4. The compiler turns values into **parameters** (named after the field) and builds a
   **selector ladder** per step, ordered by stability.
5. Every step is a **checkpoint**; data steps verify their own effect (`input.value` really
   holds this row's value).

## The selector engine

Only a strategy that matches **exactly one** element counts. Zero matches is a miss; three
matches is ambiguous and therefore also a miss — a macro that clicks "the first of three
buttons" is a bug waiting to happen.

| Score | Strategy | Why |
|---|---|---|
| 100 | `testid` — `data-testid`/`data-test`/`data-qa`/`data-cy` | authored to be stable |
| 98 | `css` — `#id`, only when the id looks authored | tightest identity; generated hashes are rejected |
| 95 | `role` — accessible role + accessible name | survives restyling and re-ordering |
| 90 | `label` — form label | how a human identifies a field |
| 80 | `text` — exact visible text | readable, brittle to copy changes |
| 70 | `css` — structural path | works when nothing else does |
| 50 | `xpath` — absolute path | last resort |

When the primary strategy misses but a fallback matches, the step is reported **degraded**
— the page drifted. In strict mode that is treated as drift and repaired, rather than
silently leaning on a fuzzy match. Generated ids (`react-select-7-input`, `:r1a:`,
`field-a1b2c3d4e5`) are never treated as identity, so ordinary React re-renders do not
trigger healing.

## Self-healing

```
resolve step → miss or drift
   → snapshot DOM + interactive-element list
   → ONE ctx.llm.complete_structured call (JSON schema, temperature 0)
   → validate every proposed selector against the live page (must be unique)
   → patch the macro JSON in plugin_data_dir(), retry the step once
   → resume from that checkpoint
   → still broken? stop, save snapshot + checkpoint, report exactly what failed
```

Repair is a one-step patch. The recorded intent is never rewritten, no steps are added, and
if no proposed selector validates, MIMIC stops instead of guessing. The model call is
counted in the run report (`llm_calls`, `heals`, `usage`).

## Tools

| Tool | Purpose |
|---|---|
| `mimic_record_start` | open/attach a browser and inject the recorder |
| `mimic_record_stop` | compile, save, **register `mimic_<name>` live** |
| `mimic_run` | run a macro (or `rows` batch), with checkpoint resume |
| `mimic_list` | learned macros, params, steps, run stats |
| `mimic_show` | full macro, selector ladders, repair history, file path |
| `mimic_heal` | re-check / force re-ground one step against the live site |
| `mimic_delete` | remove a macro's data file (requires `confirm=true`) |

Every learned macro also becomes its own tool with a generated schema, so the model can call
`mimic_invoice_entry(customer=..., rows=[...])` directly.

## Data layout (rule 03 compliant)

```
<HERMES_HOME>/plugin-data/mimic/
├── macros/<name>.json     learned macros — data, never plugin code
├── runs/<name>.latest.json checkpoints used for resume
├── snapshots/<run>-stepN.html DOM captured where a step broke
├── skill/SKILL.md          generated companion skill (lists the macros)
└── browser/                the automation browser profile
```

MIMIC never edits its own `__init__.py` or any other plugin file. Healing writes JSON under
the data directory, which is exactly the line the catalog draws: **learned macros are data,
not code**.

## 90-second demo

| Time | Beat |
|---|---|
| 0–20s | `mimic_record_start`, fill the invoice form by hand, `mimic_record_stop` |
| 20–35s | "do this for these 8 rows" → 8 rows complete in seconds, `llm_calls: 0` |
| 35–60s | rename the submit button's id in devtools → run again → step goes red → one model call → self-heals → green |
| 60–90s | `mimic_show` → the generated tool schema, the selector ladder, the repair history, and the macro JSON on disk |

The end-to-end test below performs exactly this sequence against a real Chromium, including
the rename and the repair.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install playwright pytest
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest
```

`tests/test_e2e.py` records the demo invoice form in a real browser, replays it at zero model
calls, runs a 3-row batch, then renames the submit button, asserts the step is repaired with
exactly one model call, asserts the repair is persisted into the macro JSON, and asserts the
next run is free again.

## Use cases

- **High-volume data entry** — 50+ invoices/expenses/onboarding forms from one recording.
- **Recurring back-office work** — weekly vendor payments, monthly compliance uploads,
  product listing updates, tickets into a clunky CRM.
- **Research pipelines** — search → extract → save to CRM/Airtable, deterministic and cheap.
- **The ugly intranet** — PTO approvals, timesheets, inventory checks with no API.
- **Multi-step checkout / procurement** — same basket, same coupon, every month.
- **Lightweight QA** — record the critical journey, re-run after each deploy; a renamed
  button heals instead of failing the suite.
- **Personal rituals** — statements → accounting software, client portal status checks.

## Troubleshooting

**`git@github.com: Permission denied (publickey)`** — the deploy key is not attached to that
repository. Repo → *Settings* → *Deploy keys* → *Add deploy key*, tick **Allow write access**
for pushes, and check the saved row's fingerprint. Verify with `ssh -T git@github.com`: a
working key greets you with `Hi <owner>/<repo>!`, even when the repo is private.

**`src refspec main does not match any`** — the bundle's branch is named
`freebuff/<chat-id>`, not `main`. Rename it before pushing:

```
git clone mimic.bundle hermes-mimic && cd hermes-mimic
git branch -M main
git remote set-url origin git@github.com:<owner>/hermes-mimic.git
git push -u origin main
```

**`could not read Username for 'https://github.com'`** — no credential helper is configured
for HTTPS. Use the SSH URL above, or let your OS keychain hold a personal access token; never
put a token in a tracked file or in the remote URL.

**`! [rejected] main -> main (non-fast-forward)`** — the remote already has commits this
branch does not. Run `git pull --rebase origin main` and push again, or
`git push --force-with-lease` only if you intend to discard the remote history.

**The tests badge shows "no status"** — the `tests` workflow has never run on that repo. It
appears after the first push, and a private repo shows no badge to anyone who is not signed in.

## Limitations (honest ones)

- Browser only. No desktop/OS recording.
- Clicks, fills, selects, checks and Enter — not drag-and-drop, canvas, file pickers, or
  iframes.
- Healing repairs **one** step at a time; a wholesale redesign is a re-record.
- Macro parameters are strings passed to the field; no type coercion beyond what the site does.
- `resume` assumes the browser still sits where the failing step left it (the session is kept
  alive between calls for exactly this reason).

## License

MIT.
