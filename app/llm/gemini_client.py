"""Gemini client — ARCHITECTURE.md §9, item 3 AND item 6's LLM half.

Thin wrapper, two call shapes — AGENTS.md rule 1's own two jobs, both now
implemented here:
  (a) parsing unstructured text into structured fields —
      `parse_supplier_claim`, for `app/engine/verify.py` (item 3). The same
      `report_supplier_claim` function-calling tool
      `scripts/task_zero_gemini_check.py` already proved works end to end
      (ran 2026-08-22, exit 0 — see OPEN_ITEMS.md).
  (b) turning a finished deterministic decision into plain language —
      `narrate_decision`, for `app/engine/ratchet.py` (item 6). Takes the
      ALREADY-COMPLETE deterministic brief text as input and asks for a
      polished rephrasing — nothing about what facts to include, what
      decision was made, or what any number is comes from this call; the
      caller decides all of that before this function is ever invoked.

Nothing here compares a claim to tracking, checks a threshold, or decides
execute-vs-escalate — `verify.py` and `ratchet.py` own every comparison and
every decision, on the deterministic side of AGENTS.md rule 1's line. This
module's two functions only ever move information ACROSS that line in the
two directions rule 1 permits: text-in-structure-out, or
structure-in-text-out. Never structure-in-decision-out.

Only ever called when `config.TRACE_LLM_ENABLED` is True, and only from
`verify.py`'s / `ratchet.py`'s own LLM branches. AGENTS.md rule 2 requires
the pipeline to run end to end without this module doing anything for real —
the deterministic regex/keyword parser in `verify.py` and the deterministic
template in `ratchet.py` are the required paths; this is bolted on top of
them, never underneath. Consistent with that, every failure mode here raises
rather than substitutes a guessed answer: a silently wrong parse or
narration would be a worse failure than a loud one, and the caller decides
whether to fall back to the deterministic path, not this module.
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


def narrate_decision(deterministic_brief_text: str) -> str:
    """One Gemini call, job (b): rephrase an ALREADY-COMPLETE, already-
    correct deterministic decision brief into more natural prose. The
    caller (`ratchet.py`) computes every number, every trigger, and the
    execute-vs-escalate decision itself BEFORE this is ever called — this
    function receives the finished brief as its ONLY input and returns
    text; nothing it returns is parsed back into any decision (AGENTS.md
    rule 3: the LLM cannot argue past a hard trigger, structurally, because
    nothing here is ever read as anything other than a display string).

    Raises on any failure — missing API key, SDK import failure, network
    error, empty response — never substitutes a guessed rephrasing.
    """
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is unset — cannot call Gemini.")

    from google import genai

    client = genai.Client(api_key=config.GEMINI_API_KEY)

    prompt = (
        "Rephrase the following supply-chain decision brief into clear, "
        "plain-language prose for an operations manager. Do not add, "
        "remove, or change any number, decision, or fact — only improve "
        "readability and flow.\n\n" + deterministic_brief_text
    )

    response = client.models.generate_content(model=MODEL_VERSION, contents=prompt)

    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise RuntimeError("Gemini returned no narration text.")
    return text.strip()
