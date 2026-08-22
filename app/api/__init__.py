"""Orchestrator — ARCHITECTURE.md §9. The assembled pipeline: one callable
chain wiring items 1-7 together in the order §3's control-flow diagram gives
them, plus the single ERP-write boundary (AGENTS.md rule 5).

Holds no decision logic of its own. Every judgement — what is thin, what to
poll, what contradicts, what is eligible, what to plan, execute-or-escalate —
belongs to the engine module that already owns it. This package sequences
those calls and guards the one irreversible action.
"""
