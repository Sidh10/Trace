@AGENTS.md

# Claude Code — session notes

Shared rules live in `AGENTS.md`, imported above. Everything below is
Claude-Code-specific and doesn't apply to other tools.

## Accounts & sessions

- Each teammate authenticates with their **own** Pro account. Claude chat and
  Claude Code share one usage pool per account — one shared login becomes one
  shared bottleneck for the whole team.
- Two clocks apply: a 5-hour rolling session limit and a weekly cap. Expect to
  hit the session wall two or three times over a 16–20 hour build. Stagger who
  is driving so the whole team is never blocked at once.
- Verify `ANTHROPIC_API_KEY` is **unset** in this shell before running Claude
  Code. If it's set, usage bills against API credits instead of the
  subscription — credits the team doesn't have.

## Model

Default to Sonnet. Reserve Opus for genuinely hard reasoning (e.g. debugging
the Pareto solver's edge cases or the staleness/re-entry logic), not
scaffolding or boilerplate.

## Before starting any task

Read `PROJECT.md` for why, `ARCHITECTURE.md` for how, `BRAND.md` before writing
user-facing copy. Don't ask what the rubric weights are or what stack to use —
it's already decided, in `AGENTS.md` and `ARCHITECTURE.md`.
