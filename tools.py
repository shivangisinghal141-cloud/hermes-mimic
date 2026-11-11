"""Tool handlers.

Hermes contract: a handler takes ``(args: dict, **kwargs)``, returns a JSON
string, and never raises. Everything the model needs to act on — including a
failed step with its DOM snapshot — comes back inside that JSON.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


def ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, default=str)


def err(message: Any, **payload: Any) -> str:
    return json.dumps({"ok": False, "error": str(message), **payload}, default=str)


def _guard(fn: Callable[..., str], plugin: Any, args: Dict[str, Any]) -> str:
    try:
        return fn(plugin, args or {})
    except Exception as exc:  # a tool must never raise into the agent loop
        logger.warning("mimic: tool failed: %s", exc, exc_info=True)
        return err(exc)


# ---------------------------------------------------------------- core handlers
def _record_start(plugin: Any, args: Dict[str, Any]) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return err("url is required — open the page whose workflow MIMIC should learn")
    info = plugin.start_recording(url, name=(args.get("name") or ""))
    return ok(**info, next_step="Do the task once, then call mimic_record_stop with a name.")


def _record_stop(plugin: Any, args: Dict[str, Any]) -> str:
    name = (args.get("name") or "").strip()
    discard = bool(args.get("discard"))
    if not name and not discard:
        return err("name is required (a short identifier such as 'invoice_entry')")
    result = plugin.stop_recording(
        name=name or "discarded",
        description=(args.get("description") or "").strip(),
        discard=discard,
    )
    return ok(**result)


def _run(plugin: Any, args: Dict[str, Any]) -> str:
    name = args.get("name") or args.get("macro")
    macro = plugin.get_macro(name)
    if macro is None:
        return err(
            f"no macro named {name!r}",
            macros=[item["tool"] for item in plugin.store.summaries()],
            hint="call mimic_list, or record this workflow with mimic_record_start",
        )
    params = dict(args.get("params") or {})
    for param in macro.get("params") or []:
        key = param.get("name")
        if key in args and args[key] is not None:
            params[key] = args[key]
    result = plugin.run_macro(
        macro,
        params,
        rows=args.get("rows") or None,
        resume=bool(args.get("resume")),
        heal=args.get("heal"),
    )
    return ok(**result)


def _list(plugin: Any, args: Dict[str, Any]) -> str:  # noqa: ARG001
    summaries = plugin.store.summaries()
    return ok(
        count=len(summaries),
        macros=summaries,
        data_dir=str(plugin.store.root),
        hint=(
            "Macros are data files (macros/*.json); each one is also registered as its own tool. "
            "Nothing here needs a model call to run."
        ),
    )


def _show(plugin: Any, args: Dict[str, Any]) -> str:
    name = args.get("name")
    macro = plugin.get_macro(name)
    if macro is None:
        return err(f"no macro named {name!r}")
    path = plugin.store.path_for(macro.get("name", ""))
    return ok(
        macro=macro,
        tool_schema=plugin.tool_schema_for(macro),
        macro_file=str(path),
        exists=path.exists(),
    )


def _heal(plugin: Any, args: Dict[str, Any]) -> str:
    name = args.get("name")
    macro = plugin.get_macro(name)
    if macro is None:
        return err(f"no macro named {name!r}")
    step_id = args.get("step_id")
    params = dict(args.get("params") or {})
    result = plugin.heal_macro(macro, step_id=step_id, params=params)
    return ok(**result)


def _delete(plugin: Any, args: Dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return err("refusing to delete without confirm=true")
    name = args.get("name")
    macro = plugin.get_macro(name)
    if macro is None:
        return err(f"no macro named {name!r}")
    plugin.delete_macro(macro)
    return ok(deleted=macro.get("name"), note="The data file is gone; its tool stays registered until restart.")


# --------------------------------------------------------------- dynamic tools
def handle_macro_tool(plugin: Any, tool: str, args: Dict[str, Any]) -> str:
    """Handler for a live ``mimic_<name>`` tool generated from one macro."""
    macro = plugin.get_macro(tool)
    if macro is None:
        return err(f"macro for tool {tool!r} is no longer stored")
    params: Dict[str, Any] = {}
    for param in macro.get("params") or []:
        key = param.get("name")
        if key in args and args[key] is not None:
            params[key] = args[key]
    if isinstance(args.get("params"), dict):
        params.update(args["params"])
    result = plugin.run_macro(
        macro,
        params,
        rows=args.get("rows") or None,
        resume=bool(args.get("resume")),
        heal=args.get("heal"),
    )
    return ok(**result)


def build_handlers(plugin: Any) -> Dict[str, Callable[..., str]]:
    """Map core tool names to bound, exception-safe handlers."""

    def wrap(fn: Callable[[Any, Dict[str, Any]], str]) -> Callable[..., str]:
        def handler(args: Dict[str, Any], **kwargs: Any) -> str:
            return _guard(fn, plugin, args)

        return handler

    return {
        "mimic_record_start": wrap(_record_start),
        "mimic_record_stop": wrap(_record_stop),
        "mimic_run": wrap(_run),
        "mimic_list": wrap(_list),
        "mimic_show": wrap(_show),
        "mimic_heal": wrap(_heal),
        "mimic_delete": wrap(_delete),
    }
