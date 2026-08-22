"""Gemini client — ARCHITECTURE.md §9, item 3's LLM half.

Thin wrapper, one call shape. AGENTS.md rule 1: the LLM's only job anywhere in
this repo is (a) parsing unstructured text into structured fields, or (b)
turning a finished deterministic decision into plain language. This module
implements (a), for exactly the claim shape `app/engine/verify.py` needs —
the same `report_supplier_claim` function-calling tool
`scripts/task_zero_gemini_check.py` already proved works end to end (ran
2026-08-22, exit 0 — see OPEN_ITEMS.md). Nothing here compares a claim to
tracking, scores a supplier, or makes any decision; `verify.py` owns every
comparison, on the deterministic side of AGENTS.md rule 1's line.

Only ever called when `config.TRACE_LLM_ENABLED` is True, and only from
`verify.py`'s LLM branch. AGENTS.md rule 2 requires the pipeline to run end to
end without this module doing anything for real — the deterministic
regex/keyword parser in `verify.py` is the required path; this is bolted on
top of it, never underneath it. Consistent with that, every failure mode here
raises rather than substitutes a guessed answer: a silently wrong parse would
be a worse failure than a loud one, and `verify.py`'s caller decides whether
to fall back to the deterministic parser, not this module.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app import config

# Same enum Task Zero already validated end to end. verify.py's own
# ClaimStatus type must stay in lockstep with this — both parsers (LLM and
# deterministic) must emit into the identical value set so a caller can
# treat their outputs interchangeably.
CLAIM_STATUSES: tuple[str, ...] = ("dispatched", "delayed", "cancelled", "confirmed", "unclear")

# Pinned to what Task Zero validated. Changing this is a re-validation, not a
# one-line edit — rerun scripts/task_zero_gemini_check.py after touching it.
#
# Public (not _-prefixed): this is the exact string ARCHITECTURE.md §7's
# ProvenanceEdge.model_version field wants on a successful call
# ("gemini-<version> | deterministic"). verify.py imports it rather than
# hardcoding a second copy of the model name, so the two can't drift out of
# sync if this is ever repinned.
MODEL_VERSION = "gemini-3.6-flash"


class LLMParsedClaim(BaseModel):
    """Raw structured output of one Gemini call. `verify.py` wraps this into
    its own `ParsedClaim` (which also carries `parsed_by` / `raw_message` for
    audit) — this model is the LLM boundary only, nothing more."""

    po_id: Optional[str] = None
    claim_status: str
    claimed_delay_days: int = 0


def parse_supplier_claim(message_body: str) -> LLMParsedClaim:
    """One Gemini function-calling round trip over a supplier's free-text
    message. Raises on any failure: missing API key, SDK import failure,
    network error, zero candidates, no function call, wrong tool called, or a
    `claim_status` outside `CLAIM_STATUSES`. Never returns a guessed or
    partial answer — every field on the returned object came from Gemini's
    structured response, not from this function inferring around a gap.
    """
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is unset — cannot call Gemini.")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)

    report_supplier_claim = types.FunctionDeclaration(
        name="report_supplier_claim",
        description=(
            "Report the structured claim extracted from a supplier's "
            "free-text message about a purchase order."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "po_id": types.Schema(
                    type=types.Type.STRING,
                    description="Purchase order ID referenced in the message, if any.",
                ),
                "claim_status": types.Schema(
                    type=types.Type.STRING,
                    description="Supplier's claimed status of the shipment.",
                    enum=list(CLAIM_STATUSES),
                ),
                "claimed_delay_days": types.Schema(
                    type=types.Type.INTEGER,
                    description="Delay in days if the supplier stated one, else 0.",
                ),
            },
            required=["claim_status"],
        ),
    )
    tool = types.Tool(function_declarations=[report_supplier_claim])

    prompt = (
        "Extract the structured claim from this supplier message by calling "
        f"report_supplier_claim. Message:\n\n{message_body}"
    )

    response = client.models.generate_content(
        model=MODEL_VERSION,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[tool],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        ),
    )

    candidates = response.candidates or []
    if not candidates:
        raise RuntimeError("Gemini returned zero candidates.")

    parts = candidates[0].content.parts if candidates[0].content else []
    calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
    if not calls:
        raise RuntimeError(
            "Gemini responded but did not return a function call. "
            f"Raw text (if any): {getattr(response, 'text', None)!r}"
        )

    call = calls[0]
    if call.name != "report_supplier_claim":
        raise RuntimeError(f"Gemini called the wrong tool: {call.name!r}")

    args = dict(call.args)
    claim_status = args.get("claim_status")
    if claim_status not in CLAIM_STATUSES:
        raise RuntimeError(f"Gemini returned an out-of-enum claim_status: {claim_status!r}")

    return LLMParsedClaim(
        po_id=args.get("po_id"),
        claim_status=claim_status,
        claimed_delay_days=int(args.get("claimed_delay_days") or 0),
    )
