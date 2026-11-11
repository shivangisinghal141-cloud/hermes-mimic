"""Deterministic execution of a compiled macro.

The runner is the part that makes the pitch true: after compilation, a run makes
zero model calls. It resolves each step against the selector ladder, acts, and
verifies the effect. Model calls happen only when a step breaks:

    resolve → (miss or drift) → heal (one ctx.llm call) → patch macro → retry once
            → still broken? stop, save the checkpoint and the DOM snapshot

Every step is a checkpoint, so a failed run can be resumed at the step that broke
instead of replaying the whole workflow.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from .heal import sanitize_html
from .macro import now_iso, params_with_defaults, render, tool_name
from .selectors import describe, normalize_candidate, resolve

logger = logging.getLogger(__name__)

ELEMENT_SCRIPT = """() => Array.from(document.querySelectorAll(
  'a,button,input,select,textarea,[role],[data-testid],[data-test],[data-qa]'
)).slice(0, 150).map((el, index) => ({
  index,
  tag: el.tagName.toLowerCase(),
  id: el.id || undefined,
  name: el.getAttribute('name') || undefined,
  type: el.getAttribute('type') || undefined,
  role: el.getAttribute('role') || undefined,
  text: ((el.innerText || el.value || '') + '').trim().slice(0, 80) || undefined,
  label: (el.labels && el.labels[0] ? (el.labels[0].innerText || '').trim() : '') || undefined,
  aria: el.getAttribute('aria-label') || undefined,
  testid: el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || undefined,
  disabled: el.disabled || undefined
}))"""


class Runner:
    def __init__(
        self,
        session,
        *,
        store=None,
        healer=None,
        strict: bool = True,
        step_timeout_ms: int = 4000,
        on_macro_updated=None,
    ) -> None:
        self.session = session
        self.store = store
        self.healer = healer
        self.strict = bool(strict)
        self.step_timeout_ms = int(step_timeout_ms or 4000)
        self.on_macro_updated = on_macro_updated
        self.llm_calls = 0
        self.heals = 0
        self.usage: Dict[str, float] = {}

    # ------------------------------------------------------------------ public
    def run(
        self,
        macro: Dict[str, Any],
        params: Optional[Dict[str, Any]] = None,
        *,
        resume: bool = False,
        heal: Optional[bool] = None,
        force_heal_step: Optional[int] = None,
    ) -> Dict[str, Any]:
        heal_enabled = True if heal is None else bool(heal)
        self.llm_calls = 0
        self.heals = 0
        self.usage = {}
        return self.session.call(
            self._run(macro, dict(params or {}), resume=bool(resume), heal=heal_enabled, force_heal_step=force_heal_step),
            timeout=1800,
        )

    def run_rows(
        self,
        macro: Dict[str, Any],
        params: Optional[Dict[str, Any]] = None,
        rows: Optional[List[Dict[str, Any]]] = None,
        *,
        heal: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Replay the same macro once per record. One browser, one compile, N rows."""
        base = dict(params or {})
        clean_rows = [row for row in (rows or []) if isinstance(row, dict)]
        heal_enabled = True if heal is None else bool(heal)
        self.llm_calls = 0
        self.heals = 0
        self.usage = {}
        started = time.monotonic()
        results: List[Dict[str, Any]] = []

        for index, row in enumerate(clean_rows):
            merged = {**base, **row}
            try:
                result = self.session.call(
                    self._run(macro, merged, resume=False, heal=heal_enabled, force_heal_step=None),
                    timeout=1800,
                )
            except Exception as exc:  # keep the batch reportable
                result = {"ok": False, "status": "failed", "error": str(exc), "steps": [], "llm_calls": 0, "heals": 0}
            result["row"] = row
            result["row_index"] = index
            results.append(result)
            self.llm_calls += int(result.get("llm_calls") or 0)
            self.heals += int(result.get("heals") or 0)
            for key, value in (result.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    self.usage[key] = self.usage.get(key, 0) + value
            if not result.get("ok"):
                break

        ok = bool(results) and all(result.get("ok") for result in results)
        return {
            "ok": ok,
            "status": "ok" if ok else "failed",
            "mode": "rows",
            "macro": macro.get("name"),
            "tool": tool_name(macro.get("name", "")),
            "rows": results,
            "rows_requested": len(clean_rows),
            "rows_run": len(results),
            "llm_calls": self.llm_calls,
            "heals": self.heals,
            "usage": dict(self.usage),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # ------------------------------------------------------------------ engine
    async def _run(
        self,
        macro: Dict[str, Any],
        params: Dict[str, Any],
        *,
        resume: bool,
        heal: bool,
        force_heal_step: Optional[int],
    ) -> Dict[str, Any]:
        started = time.monotonic()
        page = await self.session._ensure_page()
        steps = list(macro.get("steps") or [])
        values = params_with_defaults(macro, params)
        run_id = f"run-{uuid.uuid4().hex[:10]}"

        start_index = 0
        resumed_from = None
        if resume and self.store is not None:
            previous = self.store.last_run(macro.get("name"))
            if previous and previous.get("status") != "ok" and previous.get("failed_step_id") is not None:
                failed_id = previous["failed_step_id"]
                start_index = next(
                    (index for index, step in enumerate(steps) if step.get("id") == failed_id),
                    0,
                )
                resumed_from = previous.get("run_id")

        results: List[Dict[str, Any]] = []
        failed: Optional[Dict[str, Any]] = None

        for index, step in enumerate(steps):
            if index < start_index:
                continue
            outcome = await self._run_step(
                page,
                macro,
                step,
                values,
                run_id=run_id,
                heal=heal,
                force_heal=step.get("id") == force_heal_step,
            )
            results.append(outcome)

            if self.store is not None:
                self.store.save_run(
                    {
                        "run_id": run_id,
                        "macro": macro.get("name"),
                        "status": "running",
                        "values": values,
                        "completed_step_ids": [item["id"] for item in results if item["status"] == "ok"],
                        "failed_step_id": outcome.get("id") if outcome["status"] == "failed" else None,
                        "updated_at": now_iso(),
                    }
                )

            if outcome["status"] == "failed":
                failed = outcome
                break

        status = "failed" if failed else ("healed" if any(item.get("healed") for item in results) else "ok")
        payload: Dict[str, Any] = {
            "ok": failed is None,
            "status": status,
            "macro": macro.get("name"),
            "tool": tool_name(macro.get("name", "")),
            "run_id": run_id,
            "steps": results,
            "steps_ok": len([item for item in results if item["status"] == "ok"]),
            "steps_total": len(steps),
            "llm_calls": self.llm_calls,
            "heals": self.heals,
            "usage": dict(self.usage),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "values": values,
            "resume_from": resumed_from,
        }
        if failed:
            payload.update(
                {
                    "error": failed.get("error") or "step failed",
                    "failed_step_id": failed.get("id"),
                    "snapshot": failed.get("snapshot"),
                }
            )
        if self.store is not None:
            self.store.save_run({**payload, "updated_at": now_iso(), "failed_step_id": payload.get("failed_step_id")})
        return payload

    # -------------------------------------------------------------------- step
    async def _run_step(
        self,
        page,
        macro: Dict[str, Any],
        step: Dict[str, Any],
        values: Dict[str, Any],
        *,
        run_id: str,
        heal: bool,
        force_heal: bool,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        action = step.get("action")
        outcome: Dict[str, Any] = {
            "id": step.get("id"),
            "action": action,
            "status": "ok",
            "strategy": None,
            "degraded": False,
            "healed": False,
            "duration_ms": 0,
            "error": "",
        }

        if action == "goto":
            url = render(step.get("url"), values) or ""
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=max(self.step_timeout_ms * 4, 15000))
            except Exception as exc:
                outcome.update(status="failed", error=f"navigation failed: {exc}")
                return await self._fail(page, outcome, run_id, macro, step, values)
            verified, detail = await self._verify(page, step, values, None)
            if not verified:
                outcome.update(status="failed", error=f"navigation check failed: {detail}")
                return await self._fail(page, outcome, run_id, macro, step, values)
            outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
            return outcome

        if action == "wait":
            await asyncio.sleep(float(step.get("seconds") or 0.5))
            outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
            return outcome

        target = step.setdefault("target", {})
        strategies = list(target.get("strategies") or [])
        resolution = await resolve(page, strategies, timeout_ms=self.step_timeout_ms)

        drift = resolution.ok and resolution.degraded and self.strict
        if not resolution.ok or drift or force_heal:
            if heal:
                heal_info = await self._heal(page, macro, step, resolution, values)
                outcome["heal"] = {k: v for k, v in heal_info.items() if k != "html"}
                if heal_info.get("ok"):
                    resolution = await resolve(
                        page, step["target"].get("strategies") or [], timeout_ms=self.step_timeout_ms
                    )
                    outcome["healed"] = True
                else:
                    logger.info("mimic: healing did not produce a usable selector: %s", heal_info.get("reason"))

        if not resolution.ok:
            outcome.update(status="failed", error=resolution.summary(), tried=resolution.tried)
            return await self._fail(page, outcome, run_id, macro, step, values)

        # Strict mode does not silently lean on a fuzzy fallback: if the primary
        # selector is gone and healing could not replace it, that is a failure with
        # a precise reason, not a green step hiding drift.
        if resolution.degraded and self.strict:
            outcome.update(
                status="failed",
                error=(
                    "selector drift: the primary selector broke and self-healing did not repair it "
                    f"(fallback that still matches: {describe(resolution.strategy or {})})"
                ),
                tried=resolution.tried,
            )
            return await self._fail(page, outcome, run_id, macro, step, values)

        outcome["strategy"] = describe(resolution.strategy or {})
        outcome["degraded"] = resolution.degraded

        try:
            await self._apply(page, step, values, resolution)
        except Exception as exc:
            outcome.update(status="failed", error=f"{action} failed: {exc}")
            return await self._fail(page, outcome, run_id, macro, step, values)

        verified, detail = await self._verify(page, step, values, resolution)
        if not verified:
            outcome.update(status="failed", error=f"verification failed: {detail}")
            return await self._fail(page, outcome, run_id, macro, step, values)

        outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
        return outcome

    async def _fail(
        self,
        page,
        outcome: Dict[str, Any],
        run_id: str,
        macro: Dict[str, Any],
        step: Dict[str, Any],
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Capture what the model needs to repair this step, then return the failure."""
        outcome["duration_ms"] = int(outcome.get("duration_ms") or 0)
        try:
            html = sanitize_html(await page.content(), limit=200000)
        except Exception:
            html = ""
        if self.store is not None and html:
            try:
                outcome["snapshot"] = self.store.save_snapshot(run_id, step.get("id"), html)
            except Exception:
                logger.debug("mimic: could not save snapshot", exc_info=True)
        outcome["values"] = values
        return outcome

    # ------------------------------------------------------------------- apply
    async def _value_for(self, step: Dict[str, Any], values: Dict[str, Any]) -> Any:
        if step.get("param"):
            return values.get(step["param"])
        return render(step.get("value"), values)

    async def _apply(self, page, step: Dict[str, Any], values: Dict[str, Any], resolution) -> None:
        action = step.get("action")
        locator = resolution.locator
        timeout = self.step_timeout_ms

        if action == "click":
            await locator.click(timeout=timeout)
            return
        if action == "fill":
            value = await self._value_for(step, values)
            await locator.fill("" if value is None else str(value), timeout=timeout)
            return
        if action == "select":
            value = await self._value_for(step, values)
            text = "" if value is None else str(value)
            try:
                await locator.select_option(label=text, timeout=timeout)
            except Exception:
                await locator.select_option(value=text, timeout=timeout)
            return
        if action == "check":
            await locator.check(timeout=timeout)
            return
        if action == "uncheck":
            await locator.uncheck(timeout=timeout)
            return
        if action == "press":
            key = step.get("key") or "Enter"
            try:
                await locator.press(key, timeout=timeout)
            except Exception:
                await page.keyboard.press(key)
            return
        raise ValueError(f"unknown action {action!r}")

    # ------------------------------------------------------------------ verify
    async def _verify(self, page, step: Dict[str, Any], values: Dict[str, Any], resolution) -> tuple:
        verify = step.get("verify") or {}
        kind = verify.get("kind")

        if kind == "value":
            expected = "" if verify.get("param") is None else values.get(verify["param"])
            if verify.get("param") is None:
                expected = render(verify.get("value"), values)
            expected = "" if expected is None else str(expected)
            try:
                actual = await resolution.locator.input_value(timeout=2500)
            except Exception:
                try:
                    actual = (await resolution.locator.inner_text(timeout=2500)).strip()
                except Exception as exc:
                    return False, f"could not read back the value ({exc})"
            return actual == expected, f"{actual!r} != {expected!r}"

        if kind == "checked":
            try:
                actual = await resolution.locator.is_checked()
            except Exception as exc:
                return False, f"could not read checked state ({exc})"
            return actual == bool(verify.get("value")), f"checked={actual}"

        if kind == "url_contains":
            needle = str(verify.get("value") or "")
            return (needle in page.url), f"{page.url!r} does not contain {needle!r}"

        return True, ""

    # -------------------------------------------------------------------- heal
    async def _heal(
        self,
        page,
        macro: Dict[str, Any],
        step: Dict[str, Any],
        resolution,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self.healer is None or not self.healer.available():
            return {"ok": False, "reason": "healing unavailable (no ctx.llm, or self-healing is disabled)"}

        try:
            html = await page.content()
        except Exception:
            html = ""
        try:
            elements = await page.evaluate(ELEMENT_SCRIPT)
        except Exception:
            elements = []
        failure = resolution.summary() if resolution is not None else "step was forced into repair"

        try:
            proposal = await asyncio.to_thread(
                self.healer.propose,
                macro=macro,
                step=step,
                html=html,
                elements=elements,
                page_url=page.url,
                failure=failure,
                values=values,
            )
        except Exception as exc:
            logger.warning("mimic: healing call failed: %s", exc)
            return {"ok": False, "reason": f"model call failed: {exc}"}

        self.llm_calls += int(proposal.get("llm_calls") or 1)
        for key, value in (proposal.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                self.usage[key] = self.usage.get(key, 0) + value

        candidates = proposal.get("candidates") or []
        rejected: List[Dict[str, Any]] = []

        for candidate in candidates:
            item = normalize_candidate(candidate)
            if item is None:
                rejected.append({"candidate": candidate, "why": "not a usable selector"})
                continue
            probe = await resolve(page, [item], timeout_ms=1500)
            if not probe.ok:
                rejected.append({"candidate": describe(item), "why": probe.summary()})
                continue

            target = step.setdefault("target", {})
            old = list(target.get("strategies") or [])
            target["strategies"] = [item] + [
                existing for existing in old if describe(existing) != describe(item)
            ]
            step["heal_count"] = int(step.get("heal_count") or 0) + 1
            step["healed_at"] = now_iso()

            history = macro.setdefault("history", [])
            history.append(
                {
                    "at": step["healed_at"],
                    "step": step.get("id"),
                    "failure": failure,
                    "replaced": [describe(existing) for existing in old[:2]],
                    "with": describe(item),
                    "reason": proposal.get("reason", ""),
                }
            )
            del history[:-50]
            macro["updated_at"] = now_iso()

            if self.store is not None:
                self.store.save(macro)
            if self.on_macro_updated is not None:
                try:
                    self.on_macro_updated(macro)
                except Exception:
                    logger.debug("mimic: macro update callback failed", exc_info=True)
            self.heals += 1
            logger.info("mimic: healed step %s with %s", step.get("id"), describe(item))
            return {
                "ok": True,
                "strategy": item,
                "reason": proposal.get("reason", ""),
                "rejected": rejected,
                "history": history[-1],
            }

        return {
            "ok": False,
            "reason": "no proposed selector matched exactly one element",
            "rejected": rejected,
            "raw": proposal.get("raw", ""),
        }
