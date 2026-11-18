
"""Shape B — one bounded ReAct agent."""

from __future__ import annotations

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    """Run one claim with a single bounded ReAct agent."""

    conn = connect(store_path)

    try:
        return _run(
            conn,
            claim_id,
            seed=seed,
            crash_at=crash_at,
        )
    finally:
        conn.close()


def _run(
    conn,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
) -> RunResult:

    budget = Budget()
    run_id = make_run_id("single", claim_id, seed)
    trace = Trace(run_id)

    # Keep tool results between ReAct iterations.
    state = {
        "claim": None,
        "order_result": None,
        "policy": None,
        "history": None,
        "quote": None,
    }

    def decide(observation):
        # Record one model call for each ReAct decision.
        # Do not put ticket contents or private data in the prompt.
        complete(
            budget,
            seed=seed,
            purpose="plan",
            prompt="Choose the next read-only action for a refund claim.",
        )

        # Collect the previous tool result.
        last_tool = observation.get("last_tool")
        last_result = observation.get("last_result")

        if last_tool == "get_claim_view":
            state["claim"] = last_result

        elif last_tool == "lookup_order":
            state["order_result"] = last_result

        elif last_tool == "lookup_policy":
            state["policy"] = last_result

        elif last_tool == "lookup_refund_history":
            state["history"] = last_result

        elif last_tool == "compute_compensation":
            state["quote"] = last_result

        # Step 1: Read the claim.
        if state["claim"] is None:
            trace.add("tool_selected", {"name": "get_claim_view"})
            return {
                "tool": "get_claim_view",
                "args": {
                    "claim_id": claim_id,
                },
            }

        claim = state["claim"]

        # Claim does not exist.
        if not claim.get("found"):
            return {
                "final": {
                    "status": "failed",
                    "error_reason": "unknown_claim",
                }
            }

        # Step 2: Require order_id from the claim row.
        # Never extract an order ID from ticket text.
        order_id = claim.get("order_id")

        if not order_id:
            return {
                "final": {
                    "status": "needs_info",
                    "error_reason": "missing_order_id",
                }
            }

        # Step 3: Read the order and check ownership.
        if state["order_result"] is None:
            trace.add("tool_selected", {"name": "lookup_order"})
            return {
                "tool": "lookup_order",
                "args": {
                    "order_id": order_id,
                    "principal_id": claim["principal_id"],
                },
            }

        order_result = state["order_result"]

        if not order_result.get("visible"):
            return {
                "final": {
                    "status": "escalated",
                    "error_reason": order_result.get("reason"),
                }
            }

        order = order_result["order"]

        # Step 4: Read the current policy.
        if state["policy"] is None:
            trace.add("tool_selected", {"name": "lookup_policy"})
            return {
                "tool": "lookup_policy",
                "args": {},
            }

        # Step 5: Read previous refunds for this order.
        if state["history"] is None:
            trace.add("tool_selected", {"name": "lookup_refund_history"})
            return {
                "tool": "lookup_refund_history",
                "args": {
                    "order_id": order_id,
                    "principal_id": claim["principal_id"],
                },
            }

        history = state["history"]

        if not history.get("visible"):
            return {
                "final": {
                    "status": "escalated",
                    "error_reason": history.get("reason"),
                }
            }

        # Step 6: Calculate compensation using code.
        # The model never decides the refund amount.
        if state["quote"] is None:
            trace.add("tool_selected", {"name": "compute_compensation"})
            return {
                "tool": "compute_compensation",
                "args": {
                    "order": order,
                },
            }

        # All required information has been collected.
        return {
            "final": {
                "status": "ready",
            }
        }

    # Run the bounded ReAct loop.
    result = run_react(
        conn,
        budget,
        observation={
            "claim_id": claim_id,
        },
        decide=decide,
        max_steps=8,
    )

    # Step limit reached.
    if result["stopped"] == "step_cap":
        trace.add(
            "decision",
            {
                "status": "failed",
                "reason": "step_cap",
            },
        )

        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="step_cap",
        )

    final = result["final"]
    final_status = final.get("status")

    # Handle early exits.
    if final_status in ("failed", "needs_info", "escalated"):
        trace.add(
            "decision",
            {
                "status": final_status,
            },
        )

        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status=final_status,
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=final.get("error_reason"),
        )

    # Make sure a quote was calculated.
    if state["quote"] is None:
        trace.add(
            "decision",
            {
                "status": "failed",
                "reason": "missing_quote",
            },
        )

        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="missing_quote",
        )

    quote = state["quote"]
    proposal = int(quote["amount_vnd"])

    # Only safe information is written to the trace.
    trace.add(
        "quote",
        {
            "amount_vnd": proposal,
            "reason": quote.get("reason"),
            "clause_ids": quote.get("clause_ids", []),
        },
    )

    # No entitlement means no refund call.
    if proposal <= 0:
        trace.add(
            "decision",
            {
                "status": "denied",
            },
        )

        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status="denied",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=quote.get("reason"),
        )

    # The host is the only component allowed to write a refund.
    # Pass the FULL quote. Do not subtract previous refunds here.
    try:
        outcome = issue_refund(
            conn,
            claim_id,
            proposal,
            crash_at=crash_at,
        )

    except InjectedCrash:
        # Flush the trace, then let the injected crash propagate.
        trace.add(
            "crash",
            {
                "at": crash_at,
            },
        )
        trace.write()
        raise

    # Host rejected the refund or detected a conflict.
    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"

        trace.add(
            "decision",
            {
                "status": status,
                "reason": outcome["reason"],
            },
        )

        return finish(
            conn,
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=outcome["reason"],
        )

    # Refund successfully committed.
    trace.add(
        "decision",
        {
            "status": "resolved",
        },
    )

    return finish(
        conn,
        shape="single",
        claim_id=claim_id,
        seed=seed,
        status="resolved",
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=None,
    )
