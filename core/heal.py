"""One model call, one broken step.

The healer's whole job is: given the failing step, the DOM snapshot and the list
of interactive elements that exist *now*, propose selectors that each match
exactly one element. It never rewrites the workflow, never adds steps, and never
decides whether to act — the runner validates every candidate against the live
page before it is accepted.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .selectors import describe

logger = logging.getLogger(__name__)

REGROUND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "One sentence: what changed and why the old selector broke."},
        "candidates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["testid", "css", "role", "label", "text", "xpath"],
                    },
                    "value": {
                        "type": "string",
                        "description": "testid value, CSS selector, label text, exact visible text, or XPath.",
                    },
                    "role": {"type": "string", "description": "Only for kind=role, e.g. button, textbox."},
                    "name": {"type": "string", "description": "Only for kind=role: accessible name."},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "value"],
            },
        },
    },
    "required": ["candidates"],
}

SYSTEM = (
    "You repair exactly one broken step of a recorded browser macro. "
    "You do not rewrite the workflow, add steps, or comment on other steps. "
    "Return selector candidates that each match EXACTLY ONE element in the supplied snapshot, strongest first. "
    "Preference order: data-testid, a stable authored id (#id), accessible role plus accessible name, "
    "form label, exact visible text, a short CSS path, then XPath. "
    "Never guess: if the element is not present on the page, return no candidates."
)

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)

_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SPACE_RE = re.compile(r"\s+")


def sanitize_html(html: str, *, limit: int = 12000) -> str:
    """Strip what the model never needs (scripts, styles, comments) and truncate."""
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _COMMENT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text)
    return text[:limit]


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate)
    try:
        payload = json.loads(candidate)
    except Exception:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return None
    return payload if isinstance(payload, dict) else None


def usage_dict(usage: Any) -> Dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {k: v for k, v in usage.items() if k in USAGE_FIELDS}
    out: Dict[str, Any] = {}
    for field in USAGE_FIELDS:
        value = getattr(usage, field, None)
        if value is not None:
            out[field] = value
    return out


class Healer:
    """Wraps ``ctx.llm.complete_structured`` for a single re-grounding proposal."""

    def __init__(self, ctx: Any, *, enabled: bool = True, max_dom_chars: int = 12000) -> None:
        self.ctx = ctx
        self.enabled = bool(enabled)
        self.max_dom_chars = int(max_dom_chars)
        self.last_prompt_chars = 0

    def available(self) -> bool:
        return bool(self.enabled) and getattr(self.ctx, "llm", None) is not None

    # ------------------------------------------------------------------ prompt
    def _prompt(
        self,
        *,
        macro: Dict[str, Any],
        step: Dict[str, Any],
        html: str,
        elements: List[Dict[str, Any]],
        page_url: str,
        failure: str,
        values: Dict[str, Any],
    ) -> str:
        target = step.get("target") or {}
        intent = f"step {step.get('id')}: {step.get('action')}"
        if step.get("param"):
            intent += (
                f" the field bound to parameter {step['param']!r}"
                f" (this run's value: {values.get(step['param'])!r})"
            )
        elif step.get("key"):
            intent += f" key {step.get('key')!r}"
        elif step.get("action") == "goto":
            intent += f" staying on {step.get('url')!r}"

        lines = [
            f"Page URL: {page_url}",
            f"Macro: {macro.get('name')} — {macro.get('description', '')}",
            f"Broken step: {intent}",
            f"Why it failed: {failure}",
            "Selectors that no longer match:",
        ]
        for item in target.get("strategies") or []:
            lines.append(f"  - {describe(item)}")
        lines.append("Interactive elements currently on the page (JSON):")
        lines.append(json.dumps(elements, indent=1)[:5000])
        lines.append("Page HTML (trimmed):")
        lines.append(sanitize_html(html, limit=self.max_dom_chars))
        lines.append(
            "Return 1-5 selector candidates for the SAME element, strongest first. "
            "No prose outside the JSON."
        )
        return "\n".join(lines)

    # -------------------------------------------------------------------- call
    def propose(
        self,
        *,
        macro: Dict[str, Any],
        step: Dict[str, Any],
        html: str,
        elements: List[Dict[str, Any]],
        page_url: str,
        failure: str,
        values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        llm = getattr(self.ctx, "llm", None)
        if llm is None:
            raise RuntimeError("ctx.llm is unavailable; MIMIC cannot repair this step")

        prompt = self._prompt(
            macro=macro,
            step=step,
            html=html,
            elements=elements,
            page_url=page_url,
            failure=failure,
            values=values or {},
        )
        self.last_prompt_chars = len(prompt)
        logger.info("mimic: re-grounding step %s (%d prompt chars)", step.get("id"), len(prompt))

        result = llm.complete_structured(
            instructions=SYSTEM,
            input=[{"type": "text", "text": prompt}],
            json_schema=REGROUND_SCHEMA,
            schema_name="mimic.reground",
            temperature=0.0,
            max_tokens=700,
            timeout=60,
            purpose="mimic.heal",
        )

        parsed = getattr(result, "parsed", None)
        if parsed is None:
            parsed = _parse_json(getattr(result, "text", "") or "")

        candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
        return {
            "candidates": [item for item in (candidates or []) if isinstance(item, dict)],
            "reason": (parsed or {}).get("reason", "") if isinstance(parsed, dict) else "",
            "llm_calls": 1,
            "usage": usage_dict(getattr(result, "usage", None)),
            "raw": "" if parsed is not None else str(getattr(result, "text", ""))[:1000],
        }
