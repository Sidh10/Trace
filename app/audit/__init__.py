"""Audit — ARCHITECTURE.md §9. Item 7's provenance graph: the audit trail
and the assumption ledger as ONE object, not two (ARCHITECTURE.md §4).

Deterministic construction only. Nothing in this package decides anything or
calls an LLM to build the graph; it records what the engine already decided,
citing the same objects rather than recomputing their numbers.
"""
