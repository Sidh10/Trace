"""LLM boundary — ARCHITECTURE.md §9. AGENTS.md rule 1: everything in this
package parses unstructured text into structured fields, or turns a finished
deterministic decision into plain language. Nothing here compares, decides,
scores, or is ever imported by a module that isn't gated on
`config.TRACE_LLM_ENABLED` (AGENTS.md rule 2).
"""
