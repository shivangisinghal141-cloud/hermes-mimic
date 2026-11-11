"""MIMIC internals: macro data model, selector engine, recorder, compiler, runner, healer.

Nothing in here imports Hermes. The plugin layer (``__init__.py``, ``tools.py``)
is the only place that touches ``ctx``, which keeps the deterministic core
testable without an agent runtime.
"""
