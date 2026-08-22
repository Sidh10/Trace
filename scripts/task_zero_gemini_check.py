"""Task Zero — ARCHITECTURE.md §2.

Prove ONE thing before anything else in this repo gets built: a Gemini
function-calling round trip actually works. One script, one tool, one
successful structured response.

This script does not touch app/ — it exists to answer pass/fail within the
first few minutes, per AGENTS.md rule 2 (LLM-optional mode is mandatory
regardless of the answer).

Usage:
    python scripts/task_zero_gemini_check.py

Exit code 0 = pass (Gemini returned a structured function call we could
parse). Exit code 1 = fail. Either way, the reason is printed plainly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# --- load .env before anything else needs GEMINI_API_KEY ------------------
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # fall through to os.environ; a missing key fails loudly below

import os


def fail(reason: str) -> None:
    print(f"TASK ZERO: FAIL\nReason: {reason}")
    print(
        "\nThis is expected to be survivable — AGENTS.md rule 2 requires the "
        "deterministic path (TRACE_LLM_ENABLED=false) to work end to end "
        "regardless of this result. Proceed to build-order item 1."
    )
    sys.exit(1)


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        fail("GEMINI_API_KEY is unset or empty (checked .env and shell env).")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        fail(f"google-genai SDK not importable: {exc}")
        return

    client = genai.Client(api_key=api_key)

    # One tool: the exact kind of structured field-extraction the real
    # pipeline needs the LLM for (AGENTS.md rule 1) — parsing an unstructured
    # supplier message into structured fields. Nothing else is exercised
    # here; coverage math / decisions are not the LLM's job even in this
    # smoke test.
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
                    description="Purchase order ID referenced in the message.",
                ),
                "claim_status": types.Schema(
                    type=types.Type.STRING,
                    description="Supplier's claimed status of the shipment.",
                    enum=["dispatched", "delayed", "cancelled", "confirmed", "unclear"],
                ),
                "claimed_delay_days": types.Schema(
                    type=types.Type.INTEGER,
                    description="Delay in days if the supplier stated one, else 0.",
                ),
            },
            required=["po_id", "claim_status"],
        ),
    )

    tool = types.Tool(function_declarations=[report_supplier_claim])

    supplier_message = (
        "Hi team, re PO-7712 — apologies, the COMP-104 batch is running "
        "about 3 days behind. We've dispatched it as of this morning "
        "though, tracking should update shortly. — SUP-21"
    )

    prompt = (
        "Extract the structured claim from this supplier message by "
        f"calling report_supplier_claim. Message:\n\n{supplier_message}"
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[tool],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="ANY")
                ),
            ),
        )
    except Exception as exc:  # network, auth, quota — any of these is a real answer
        fail(f"Gemini API call raised: {type(exc).__name__}: {exc}")
        return

    candidates = response.candidates or []
    if not candidates:
        fail("Gemini returned zero candidates.")
        return

    parts = candidates[0].content.parts if candidates[0].content else []
    function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

    if not function_calls:
        fail(
            "Gemini responded but did not return a function call. "
            f"Raw text (if any): {getattr(response, 'text', None)!r}"
        )
        return

    call = function_calls[0]
    if call.name != "report_supplier_claim":
        fail(f"Gemini called the wrong tool: {call.name!r}")
        return

    args = dict(call.args)
    required = {"po_id", "claim_status"}
    missing = required - args.keys()
    if missing:
        fail(f"Function call args missing required fields: {missing}. Got: {args}")
        return

    print("TASK ZERO: PASS")
    print(f"Model called: {call.name}")
    print(f"Structured args: {json.dumps(args, indent=2)}")
    print(
        "\nGemini function-calling round trip confirmed. Proceeding to "
        "build-order item 1 (ARCHITECTURE.md §4) with TRACE_LLM_ENABLED=true "
        "available — the LLM-off path per AGENTS.md rule 2 still gets built "
        "and rehearsed regardless."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
