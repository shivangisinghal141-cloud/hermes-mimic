"""Which tool exists in the model's tool list.

The recorder and compiler are the content; this file is the surface. Descriptions
are written for the decision the model actually has to make: "should I click
through this site again, or call the tool that already knows how?"
"""

from __future__ import annotations

from typing import Any, Dict, List

RECORD_START: Dict[str, Any] = {
    "name": "mimic_record_start",
    "description": (
        "Start learning a browser workflow. Opens (or attaches to) a browser at `url` and injects a "
        "recorder that observes real DOM interactions. Perform the task once — by hand or through your "
        "browser tool — then call mimic_record_stop. Use this whenever a browser task will be repeated: "
        "filling forms, entering batches of records, checking the same dashboard, submitting the same "
        "internal tool. Recording once produces a deterministic tool that costs no model calls to run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Page to open before recording starts."},
            "name": {
                "type": "string",
                "description": "Working name for the macro, e.g. 'invoice_entry' or 'vendor_payment'.",
            },
        },
        "required": ["url"],
    },
}

RECORD_STOP: Dict[str, Any] = {
    "name": "mimic_record_stop",
    "description": (
        "Stop recording, compile the captured interactions into a parameterized macro, save it as data, "
        "and register it live as a new tool named mimic_<name>. Field values you typed become typed "
        "parameters, so the same workflow can be replayed for other records. Returns the compiled steps, "
        "parameters and the name of the new tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name for the macro and its tool (mimic_<name>)."},
            "description": {
                "type": "string",
                "description": "One line the future model will read to decide when to use this tool.",
            },
            "discard": {
                "type": "boolean",
                "description": "Throw the recording away instead of compiling it.",
                "default": False,
            },
        },
        "required": ["name"],
    },
}

RUN: Dict[str, Any] = {
    "name": "mimic_run",
    "description": (
        "Run a learned macro deterministically — no model calls, no re-deciding of selectors. Use it for "
        "a learned workflow whose own tool is not visible in your tool list, or when you want the run "
        "report/checkpoint control. Pass `rows` to replay the same workflow over many records in one call. "
        "If a step breaks, MIMIC re-grounds that one step with a single model call, patches the saved macro "
        "and resumes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Macro name (or its tool name, mimic_<name>)."},
            "params": {
                "type": "object",
                "description": "Parameter values for this run; omitted parameters use their recorded defaults.",
            },
            "rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Batch mode: one object per record, each overriding parameters.",
            },
            "resume": {
                "type": "boolean",
                "description": "Resume the last failed run at the step that broke instead of starting over.",
                "default": False,
            },
            "heal": {
                "type": "boolean",
                "description": "Allow one repair call if a step breaks (default comes from plugin settings).",
            },
        },
        "required": ["name"],
    },
}

LIST: Dict[str, Any] = {
    "name": "mimic_list",
    "description": (
        "List the browser macros MIMIC has learned: tool name, steps, parameters, source URL and run "
        "statistics. Check this before re-recording a workflow that may already be a tool."
    ),
    "parameters": {"type": "object", "properties": {}},
}

SHOW: Dict[str, Any] = {
    "name": "mimic_show",
    "description": (
        "Show one learned macro in full: every step with its selector ladder, its parameters, the exact "
        "tool schema the model sees, repair history, and where the macro JSON lives on disk."
    ),
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Macro name or tool name."}},
        "required": ["name"],
    },
}

HEAL: Dict[str, Any] = {
    "name": "mimic_heal",
    "description": (
        "Repair or re-check a macro against the live site without changing the recorded intent. Runs the "
        "workflow (or up to `step_id`) and, when a selector no longer matches or a requested step needs "
        "re-grounding, makes exactly one model call to propose new selectors, validates them against the "
        "page, and patches the saved macro."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Macro name or tool name."},
            "step_id": {"type": "integer", "description": "Force re-grounding of this step."},
            "params": {"type": "object", "description": "Parameter values to use while probing."},
        },
        "required": ["name"],
    },
}

DELETE: Dict[str, Any] = {
    "name": "mimic_delete",
    "description": "Delete a learned macro (its data file). Requires confirm=true.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Macro name or tool name."},
            "confirm": {"type": "boolean", "description": "Must be true."},
        },
        "required": ["name", "confirm"],
    },
}

CORE_SCHEMAS: List[Dict[str, Any]] = [RECORD_START, RECORD_STOP, RUN, LIST, SHOW, HEAL, DELETE]
