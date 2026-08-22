"""Deterministic decision engine — ARCHITECTURE.md §4, items 2-9.

Nothing in this package calls an LLM. AGENTS.md rule 1: coverage math,
constraint checks, claim comparison and plan selection are deterministic
Python. The LLM parses inbound text (app/llm/) and narrates a finished
decision — it never owns a number produced here.
"""
