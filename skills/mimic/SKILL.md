---
name: mimic
description: Replay a browser workflow that was recorded once — deterministic, zero model calls, self-healing when the site changes.
---

# MIMIC — recorded browser tools

A learned macro is a real tool with typed parameters. Running it costs **no model
calls**: no re-reading the page, no re-deciding selectors, no re-improvisation.

There are {{COUNT}} learned macro(s) right now:

{{MACROS}}

## When to reach for a macro

Reach for the macro tool instead of driving the browser when:

- the task repeats (another invoice, another timesheet, another vendor payment);
- the site is the same and only the **data** changes;
- the workflow is long enough that re-clicking it by hand is where mistakes live;
- the user says "do it again", "for these rows", "same as last time", "every week".

Do **not** re-record something that already exists. Call `mimic_list` first. Each
macro is also its own tool (`mimic_<name>`); if it is not in your tool list, run it
through `mimic_run`.

## Recording a new one

1. `mimic_record_start` with the URL and a short name.
2. Perform the workflow **once**, exactly as it should always run: fill the fields,
   choose the dropdown values, press the final button.
3. `mimic_record_stop` with the name. MIMIC compiles the interactions into a
   parameterized macro, saves it as data, and registers `mimic_<name>` live.

Field values you entered become parameter defaults. That is how one recording turns
into "do this for these 8 rows": pass `rows=[{...}, {...}]` to a single call.

## When a step breaks

Sites change. A renamed button or a moved form breaks one step, not the workflow:

- The run reports the failing step, why it failed, and where the DOM snapshot is.
- With self-healing enabled, MIMIC makes **one** model call to re-ground that step,
  validates the proposed selector against the live page, patches the macro JSON and
  resumes from the checkpoint.
- If it cannot repair it, the run stops with `needs_human`-style detail instead of
  guessing. Ask the user only then.

Use `mimic_heal` to re-check a macro against the live site without changing what it
was recorded to do, and `mimic_show` to inspect a macro's steps, selectors and repair
history.

## Rules of thumb

- Macros are data (`plugin_data_dir()/macros/*.json`), never plugin code.
- Repair is a one-step patch, not a re-record: the intent stays the recorded intent.
- Prefer editing parameters over recording a second near-identical macro.
- Deleting a macro (`mimic_delete`, `confirm=true`) removes its data file only.
