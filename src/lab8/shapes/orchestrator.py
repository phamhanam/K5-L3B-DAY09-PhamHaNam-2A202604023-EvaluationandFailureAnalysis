"""Shape C — orchestrator plus read-only investigators.

1. Build a handoff with `lab8.handoff.make_handoff`. Allowed tools are read-only.
   Never put parent_secret, raw headers, or the full claim row into facts.
2. `lab8.runtime.run_readonly_investigators` runs those tools.
3. The orchestrator quotes from the investigator's order view and calls
   `durability.issue_refund`. Investigators have no write registry.
   Pass the full quote; the host subtracts earlier refunds on the same order.
   `lookup_refund_history` is a read-only tool you may hand to an investigator.
4. Trace the sanitized handoff, not the parent context.
"""

from __future__ import annotations

import sqlite3

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import make_handoff
from lab8.mock_llm import complete
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import compute_compensation, tool_cost
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
) -> RunResult:
    budget = Budget()
    run_id = make_run_id("orchestrator", claim_id, seed)
    trace = Trace(run_id)

    budget.add_tool(tool_cost())
    claim_row = conn.execute(
        "SELECT claim_id, principal_id, order_id FROM claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if claim_row is None:
        trace.add("error", {"reason": "unknown_claim"})
        return finish(
            conn,
            shape="orchestrator",
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="unknown_claim",
        )

    if not claim_row["order_id"]:
        complete(
            budget,
            seed=seed,
            purpose="draft",
            prompt=f"claim_id={claim_id} status=needs_info",
        )
        trace.add("decision", {"status": "needs_info", "reason": "missing_order_id"})
        return finish(
            conn,
            shape="orchestrator",
            claim_id=claim_id,
            seed=seed,
            status="needs_info",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="missing_order_id",
        )

    handoff = make_handoff(
        request_id=f"REQ-{claim_id}",
        claim_id=claim_id,
        task="investigate_order_policy_and_refund_history",
        facts={},
        allowed_tools=[
            "get_claim_view",
            "lookup_order",
            "lookup_policy",
            "lookup_refund_history",
        ],
    )
    trace.add(
        "handoff_sanitized",
        {
            "sanitized": True,
            "contract_version": handoff["contract_version"],
            "allowed_tools": list(handoff["allowed_tools"]),
        },
    )
    trace.add("handoff", safe_dump(handoff))

    investigated = run_readonly_investigators(
        conn,
        budget,
        handoff,
        principal_id=claim_row["principal_id"],
    )
    order_view = investigated["tools"].get("lookup_order") or {}
    policy_view = investigated["tools"].get("lookup_policy") or {}
    trace.add(
        "investigator",
        {
            "lookup_order": safe_dump(order_view),
            "policy_count": len(policy_view.get("policies") or []),
            "refund_history_checked": "lookup_refund_history" in investigated["tools"],
        },
    )

    if not order_view.get("visible"):
        complete(
            budget,
            seed=seed,
            purpose="draft",
            prompt=f"claim_id={claim_id} status=escalated",
        )
        trace.add(
            "decision",
            {"status": "escalated", "reason": order_view.get("reason")},
        )
        return finish(
            conn,
            shape="orchestrator",
            claim_id=claim_id,
            seed=seed,
            status="escalated",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=order_view.get("reason"),
        )

    order = dict(order_view["order"])
    budget.add_tool(tool_cost())
    quote_payload = compute_compensation(conn, order)
    proposal = int(quote_payload["amount_vnd"])
    trace.add("quote", safe_dump(quote_payload))

    if proposal <= 0:
        complete(
            budget,
            seed=seed,
            purpose="draft",
            prompt=f"claim_id={claim_id} status=denied proposal=0",
        )
        trace.add(
            "decision",
            {
                "status": "denied",
                "proposal_amount": 0,
                "reason": quote_payload.get("reason"),
            },
        )
        return finish(
            conn,
            shape="orchestrator",
            claim_id=claim_id,
            seed=seed,
            status="denied",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=quote_payload.get("reason"),
        )

    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise

    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        complete(
            budget,
            seed=seed,
            purpose="draft",
            prompt=f"claim_id={claim_id} status={status} proposal={proposal}",
        )
        trace.add(
            "decision",
            {
                "status": status,
                "proposal_amount": proposal,
                "reason": outcome["reason"],
                "conflict": bool(outcome["conflict"]),
            },
        )
        return finish(
            conn,
            shape="orchestrator",
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=outcome["reason"],
        )

    complete(
        budget,
        seed=seed,
        purpose="draft",
        prompt=f"claim_id={claim_id} status=resolved proposal={proposal}",
    )
    trace.add(
        "decision",
        {
            "status": "resolved",
            "proposal_amount": proposal,
            "reason": outcome.get("reason"),
        },
    )
    return finish(
        conn,
        shape="orchestrator",
        claim_id=claim_id,
        seed=seed,
        status="resolved",
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=None,
    )
