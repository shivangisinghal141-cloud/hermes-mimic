"""Durable storage for macros, run checkpoints and DOM snapshots.

Everything lives under ``plugin_data_dir()/``:

    macros/<name>.json      the learned macros (data, never code)
    runs/<macro>.json       latest run checkpoint, used for resume
    runs/<run_id>.json      full run record
    snapshots/<run>.html    DOM captured at a failed step
    skill/SKILL.md          generated companion skill (lists the macros)
    browser/                the automation browser's profile

Writes are atomic (temp file + rename) so a crash mid-heal cannot leave a macro
half-written and unusable.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .macro import macro_summary, slug


class MacroStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.macros_dir = self.root / "macros"
        self.runs_dir = self.root / "runs"
        self.snapshots_dir = self.root / "snapshots"
        self.skill_dir = self.root / "skill"
        self.browser_dir = self.root / "browser"
        for directory in (self.macros_dir, self.runs_dir, self.snapshots_dir, self.skill_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ macros
    def path_for(self, name: str) -> Path:
        return self.macros_dir / f"{slug(name)}.json"

    def list(self) -> List[Dict[str, Any]]:
        macros: List[Dict[str, Any]] = []
        for path in sorted(self.macros_dir.glob("*.json")):
            data = self._read(path)
            if isinstance(data, dict) and data.get("steps"):
                macros.append(data)
        return macros

    def summaries(self) -> List[Dict[str, Any]]:
        return [macro_summary(macro) for macro in self.list()]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """Load a macro by macro name or by tool name (``mimic_x``)."""
        if not name:
            return None
        key = slug(name)
        path = self.path_for(key)
        if not path.exists() and key.startswith("mimic_"):
            path = self.path_for(key[len("mimic_") :])
        if not path.exists():
            return None
        data = self._read(path)
        return data if isinstance(data, dict) else None

    def save(self, macro: Dict[str, Any]) -> Path:
        macro["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path = self.path_for(macro.get("name", "macro"))
        self._write(path, macro)
        return path

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        if path.exists():
            path.unlink()
            return True
        if slug(name).startswith("mimic_"):
            return self.delete(slug(name)[len("mimic_") :])
        return False

    # ------------------------------------------------------------------- runs
    def save_run(self, run: Dict[str, Any]) -> Path:
        run_id = run.get("run_id") or f"run-{int(time.time())}"
        run["run_id"] = run_id
        path = self.runs_dir / f"{slug(run.get('macro', 'macro'))}.latest.json"
        self._write(path, run)
        self._write(self.runs_dir / f"{run_id}.json", run)
        return path

    def last_run(self, macro_name: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / f"{slug(macro_name)}.latest.json"
        data = self._read(path)
        return data if isinstance(data, dict) else None

    def save_snapshot(self, run_id: str, step_id: Any, html: str) -> str:
        path = self.snapshots_dir / f"{slug(run_id)}-step{step_id}.html"
        path.write_text(html or "", encoding="utf-8")
        return str(path)

    # ------------------------------------------------------------------ stats
    def bump_stats(self, macro: Dict[str, Any], *, ok: bool, heals: int = 0, llm_calls: int = 0) -> None:
        stats = macro.setdefault("stats", {})
        stats["runs"] = int(stats.get("runs", 0)) + 1
        stats["ok"] = int(stats.get("ok", 0)) + (1 if ok else 0)
        stats["heals"] = int(stats.get("heals", 0)) + int(heals)
        stats["llm_calls"] = int(stats.get("llm_calls", 0)) + int(llm_calls)
        stats["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        stats["last_status"] = "ok" if ok else "failed"
        self.save(macro)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _write(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
