"""MIMIC — show it once, then it never needs the model again.

Registering this plugin does three things:

1. every stored macro is re-registered as its own live tool (``mimic_<name>``),
   so a workflow learned yesterday is a first-class tool today;
2. the core tools are registered (record, run, list, show, heal, delete);
3. a companion skill is published so the agent knows *when* to reach for a
   recorded macro instead of driving the site by hand again.

Learned macros are data under ``plugin_data_dir()``. This module never rewrites
plugin files, which keeps MIMIC on the right side of catalog rule 03
(no self-updating code).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
from typing import Any, Dict, Optional

# Some tooling imports this file without a parent package (pytest collecting the
# repository root, a bare ``python __init__.py``). Give it one so the relative
# imports below behave exactly as they do when Hermes loads the plugin.
if __package__ in (None, ""):  # pragma: no cover - tooling import path
    __package__ = "mimic"
    __path__ = [os.path.dirname(os.path.abspath(__file__))]  # type: ignore[assignment]
    sys.modules.setdefault("mimic", sys.modules[__name__])

from . import schemas, tools
from .core.browser import BrowserSession
from .core.compiler import compile_recording, interactions
from .core.heal import Healer
from .core.macro import macro_name_from_tool, slug, tool_name, tool_schema, validate
from .core.paths import data_dir
from .core.recorder import Recorder
from .core.runner import Runner
from .core.store import MacroStore

logger = logging.getLogger("mimic")

CORE_TOOLS = {schema["name"]: schema for schema in schemas.CORE_SCHEMAS}

DEFAULTS: Dict[str, Any] = {
    "headless": True,
    "cdp_url": "",
    "strict": True,
    "heal": True,
    "step_timeout_ms": 4000,
    "native_browser": False,
}

ENV_KEYS = {
    "MIMIC_HEADLESS": "headless",
    "MIMIC_CDP_URL": "cdp_url",
    "MIMIC_STRICT": "strict",
    "MIMIC_HEAL": "heal",
    "MIMIC_STEP_TIMEOUT_MS": "step_timeout_ms",
    "MIMIC_NATIVE_BROWSER": "native_browser",
}


def _coerce(key: str, value: Any) -> Any:
    if key in ("headless", "strict", "heal", "native_browser"):
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)
    if key == "step_timeout_ms":
        try:
            return int(value)
        except Exception:
            return DEFAULTS[key]
    return "" if value is None else str(value)


def settings_from_context(ctx: Any) -> Dict[str, Any]:
    """Read plugin settings without assuming one config shape."""
    values = dict(DEFAULTS)
    raw: Optional[Dict[str, Any]] = None
    for attribute in ("get_config", "config"):
        candidate = getattr(ctx, attribute, None)
        if candidate is None:
            continue
        try:
            raw = candidate() if callable(candidate) else candidate
        except Exception:
            raw = None
        if isinstance(raw, dict):
            break
        raw = None

    if isinstance(raw, dict):
        scopes = [raw]
        if isinstance(raw.get("settings"), dict):
            scopes.append(raw["settings"])
        for scope in scopes:
            for key in values:
                if key in scope:
                    values[key] = _coerce(key, scope[key])

    for env_key, key in ENV_KEYS.items():
        value = os.environ.get(env_key)
        if value not in (None, ""):
            values[key] = _coerce(key, value)
    return values


class MimicPlugin:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.cfg = settings_from_context(ctx)
        self.store = MacroStore(data_dir())
        self.lock = threading.RLock()
        self.session: Optional[BrowserSession] = None
        self.recorder: Optional[Recorder] = None
        self.healer = Healer(ctx, enabled=bool(self.cfg.get("heal", True)))
        self.macros: Dict[str, Dict[str, Any]] = {}
        self._registered: Dict[str, Dict[str, Any]] = {}
        self._session_pid = os.getpid()

    # ------------------------------------------------------------- registration
    def register(self) -> "MimicPlugin":
        for macro in self.store.list():
            self.macros[slug(macro.get("name"))] = macro

        handlers = tools.build_handlers(self)
        for name, schema in CORE_TOOLS.items():
            self._register_tool(name, schema, handlers[name])

        for macro in list(self.macros.values()):
            self.register_macro_tool(macro)

        self.publish_skill()

        try:
            self.ctx.register_hook("on_skill_lifecycle", self.on_skill_lifecycle)
        except Exception:
            logger.warning("mimic: could not register the on_skill_lifecycle hook", exc_info=True)

        logger.info(
            "mimic: ready — %d macro tool(s), data at %s", len(self.macros), self.store.root
        )
        return self

    def _register_tool(self, name: str, schema: Dict[str, Any], handler, *, force: bool = False) -> bool:
        if name in self._registered and not force:
            return True
        try:
            self.ctx.register_tool(name=name, toolset="mimic", schema=schema, handler=handler)
            self._registered[name] = schema
            return True
        except Exception:
            pass
        try:
            self.ctx.register_tool(name=name, toolset="mimic", schema=schema, handler=handler, override=True)
            self._registered[name] = schema
            return True
        except Exception:
            logger.debug("mimic: live registration for %s is unavailable", name, exc_info=True)
            return False

    def register_macro_tool(self, macro: Dict[str, Any]) -> bool:
        """Make a macro callable as its own tool. Returns whether that worked live."""
        name = tool_name(macro.get("name", ""))
        self.macros[slug(macro.get("name"))] = macro

        def handler(args, _tool=name, **kwargs):  # noqa: ARG001 - Hermes context kwargs
            return tools.handle_macro_tool(self, _tool, args or {})

        return self._register_tool(name, tool_schema(macro), handler)

    def tool_schema_for(self, macro: Dict[str, Any]) -> Dict[str, Any]:
        return tool_schema(macro)

    # ------------------------------------------------------------------- browser
    def browser(self) -> BrowserSession:
        with self.lock:
            if os.getpid() != self._session_pid:  # a fork must not share a socket
                self.session = None
                self.recorder = None
                self._registered.clear()
                self._session_pid = os.getpid()
            if self.session is None:
                self.session = BrowserSession(
                    headless=bool(self.cfg.get("headless", True)),
                    cdp_url=str(self.cfg.get("cdp_url") or ""),
                    profile_dir=self.store.browser_dir,
                    native_browser=bool(self.cfg.get("native_browser", False)),
                )
                atexit.register(self.shutdown)
            return self.session

    def shutdown(self) -> None:
        with self.lock:
            if self.session is not None:
                try:
                    self.session.close()
                except Exception:
                    logger.debug("mimic: browser shutdown failed", exc_info=True)
                self.session = None
                self.recorder = None

    # ----------------------------------------------------------------- recording
    def start_recording(self, url: str, name: str = "") -> Dict[str, Any]:
        self.browser()
        if self.recorder is None:
            self.recorder = Recorder(self.session)
        info = self.recorder.start(url, name=name)
        return info

    def stop_recording(self, *, name: str, description: str = "", discard: bool = False) -> Dict[str, Any]:
        if self.recorder is None or not self.recorder.active:
            raise RuntimeError("no recording in progress; call mimic_record_start first")

        raw = self.recorder.stop()
        if discard:
            return {"discarded": True, "events": raw.get("event_count", 0), "llm_calls": 0}

        macro = compile_recording(
            name=name,
            events=raw.get("events") or [],
            start_url=raw.get("start_url") or raw.get("url") or "",
            final_url=raw.get("url") or "",
            title=raw.get("title") or "",
            description=description,
            strict=bool(self.cfg.get("strict", True)),
        )

        if interactions(macro) == 0:
            raise ValueError(
                "the recording captured no interactions — fill a field or click something first"
            )
        problems = validate(macro)
        if problems:
            raise ValueError("the recorded macro is not runnable: " + "; ".join(problems))

        self.store.save(macro)
        registered = self.register_macro_tool(macro)
        self.publish_skill()
        return {
            "macro": macro.get("name"),
            "tool": tool_name(macro.get("name", "")),
            "tool_registered": registered,
            "steps": len(macro.get("steps") or []),
            "params": [param["name"] for param in macro.get("params") or []],
            "source_url": macro.get("source_url"),
            "macro_file": str(self.store.path_for(macro.get("name", ""))),
            "recorded_events": raw.get("event_count", 0),
            "llm_calls": 0,
            "hint": (
                f"call {tool_name(macro.get('name', ''))} with your values, "
                "or pass rows=[{...}, {...}] to replay it for a whole batch"
            ),
        }

    # --------------------------------------------------------------------- data
    def get_macro(self, name: Any) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        key = slug(name)
        if key in self.macros:
            return self.macros[key]
        alias = macro_name_from_tool(key)
        if alias in self.macros:
            return self.macros[alias]
        macro = self.store.get(key)
        if macro is not None:
            self.macros[slug(macro.get("name"))] = macro
        return macro

    def _remember(self, macro: Dict[str, Any]) -> None:
        self.macros[slug(macro.get("name"))] = macro

    def delete_macro(self, macro: Dict[str, Any]) -> None:
        name = macro.get("name", "")
        self.store.delete(name)
        self.macros.pop(slug(name), None)
        self.publish_skill()

    # ---------------------------------------------------------------------- run
    def _runner(self) -> Runner:
        return Runner(
            self.browser(),
            store=self.store,
            healer=self.healer,
            strict=bool(self.cfg.get("strict", True)),
            step_timeout_ms=int(self.cfg.get("step_timeout_ms", 4000)),
            on_macro_updated=self._remember,
        )

    def run_macro(
        self,
        macro: Dict[str, Any],
        params: Optional[Dict[str, Any]] = None,
        rows: Optional[list] = None,
        *,
        resume: bool = False,
        heal: Any = None,
    ) -> Dict[str, Any]:
        runner = self._runner()
        if rows:
            result = runner.run_rows(macro, params or {}, rows, heal=heal)
        else:
            result = runner.run(macro, params or {}, resume=resume, heal=heal)
        try:
            self.store.bump_stats(
                macro,
                ok=bool(result.get("ok")),
                heals=int(result.get("heals") or 0),
                llm_calls=int(result.get("llm_calls") or 0),
            )
        except Exception:
            logger.debug("mimic: could not record run stats", exc_info=True)
        return result

    def heal_macro(self, macro: Dict[str, Any], step_id: Any = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        runner = self._runner()
        result = runner.run(
            macro,
            params or {},
            force_heal_step=int(step_id) if step_id is not None else None,
        )
        try:
            self.store.bump_stats(
                macro,
                ok=bool(result.get("ok")),
                heals=int(result.get("heals") or 0),
                llm_calls=int(result.get("llm_calls") or 0),
            )
        except Exception:
            pass
        return result

    # -------------------------------------------------------------------- skill
    def publish_skill(self) -> str:
        """Write the companion skill (macro list included) and register it."""
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "mimic", "SKILL.md")
        try:
            with open(template_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except Exception:
            logger.debug("mimic: skill template missing", exc_info=True)
            text = "---\nname: mimic\ndescription: Replay recorded browser workflows.\n---\n\n# MIMIC\n\n{{MACROS}}\n"

        rows = []
        for summary in self.store.summaries():
            params = ", ".join(summary["params"]) or "none"
            rows.append(
                f"- `{summary['tool']}` — {summary['description'] or 'replays ' + str(summary['source_url'])} "
                f"({summary['steps']} steps; params: {params})"
            )
        listing = "\n".join(rows) if rows else "- (none yet — record one with `mimic_record_start`)"
        text = text.replace("{{MACROS}}", listing).replace("{{COUNT}}", str(len(rows)))

        path = self.store.skill_dir / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        try:
            self.ctx.register_skill("mimic", path)
        except Exception:
            logger.debug("mimic: register_skill is unavailable on this Hermes build", exc_info=True)
        return str(path)

    def on_skill_lifecycle(self, **kwargs: Any) -> None:
        """Observe skill usage; republish the companion skill if it disappears."""
        skill = slug(kwargs.get("skill_name") or "")
        if skill != "mimic":
            return
        action = str(kwargs.get("action") or "")
        try:
            with (self.store.skill_dir / "usage.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": _now(),
                            "action": action,
                            "use_count": kwargs.get("use_count"),
                            "reused": kwargs.get("reused"),
                            "src": "mimic.on_skill_lifecycle",
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass
        if action in ("removed", "deleted", "unregistered", "unpublished", "disabled"):
            try:
                self.publish_skill()
            except Exception:
                logger.debug("mimic: skill republish failed", exc_info=True)


def _now() -> str:
    from .core.macro import now_iso

    return now_iso()


def register(ctx: Any) -> None:
    """Hermes entry point."""
    MimicPlugin(ctx).register()
