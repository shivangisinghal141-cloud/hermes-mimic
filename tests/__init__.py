"""Test package marker.

The repository root is the Hermes plugin package. Without this marker pytest walks
up from ``tests/`` and tries to import the root as a package, which is not
importable under the checkout's directory name.
"""
