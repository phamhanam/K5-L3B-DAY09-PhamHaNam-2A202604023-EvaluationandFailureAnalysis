"""Shape A — deterministic workflow. Student work.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Suggested steps. Money stays in code (`lab8.tools.compute_compensation`).

1. Open the store. Read the claim through `get_claim_view` (not SELECT *).
2. Missing order_id -> status `needs_info`. Do not write the ledger.
3. `lookup_order`. If not visible -> `escalated`. Do not copy phone, address, item, internal note.
4. Quote from the order row. Ignore delay numbers written in the ticket.
5. Quote amount 0 -> `denied`. Do not call issue_refund.
6. Quote amount > 0 -> `durability.issue_refund(conn, claim_id, <full quote>)`.
   Pass the FULL quote. The host checks the claim's own approval and subtracts
   what earlier claims already got for the same order (T13, T14). Do not
   subtract it yourself or the host subtracts twice.
   Auth failure or order already refunded -> `denied` (the host writes `policy_denied`).
   `proposal_amount` stays the full quote. Amount conflict -> `failed`.
7. Success -> `resolved`. One ledger row, one `refund_committed` audit.
8. Use MockLLM only to draft the reply text, if you want a model call at all.

The model must not choose the refund amount and must not commit the refund.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.

Implementation notes (team):
- The ticket text is never parsed for numbers and never written to the trace.
  It is user data, and it may carry injected instructions or secrets.
- The trace only records allowlisted views: claim id, whether an order id exists,
  the `lookup_order` view (ORDER_FIELDS only), the quote, and the host outcome.
  It never records the raw claim row (`parent_secret`) or the raw order row
  (phone, address, item, internal note), and nothing is printed to stdout.
- One MockLLM call drafts the customer reply after the decision is fixed. Its
  text is not used for any decision.
"""

from __future__ import annotations

import sqlite3

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "workflow"


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
    shape: str = SHAPE,
    draft: bool = True,
) -> RunResult:
    """`shape` and `draft` exist only for the ablation in src/bonus/; run_claim uses the defaults."""
    budget = Budget()
    trace = Trace(make_run_id(shape, claim_id, seed))

    def done(status: str, proposal: int, reason: str | None) -> RunResult:
        trace.add("decision", {"status": status, "proposal_amount": int(proposal), "reason": reason})
        if draft and (status != "failed" or reason != "unknown_claim"):
            # Draft only after the decision is fixed; the text never feeds back.
            complete(budget, seed=seed, purpose="draft", prompt=f"status={status} amount={int(proposal)}")
            trace.add("draft", {"purpose": "draft"})
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    # 1. Claim through the allowlisted view.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim.get("found"):
        trace.add("claim", {"claim_id": claim_id, "found": False})
        return done("failed", 0, "unknown_claim")
    order_id = claim.get("order_id")
    trace.add("claim", {"claim_id": claim_id, "found": True, "has_order_id": bool(order_id)})

    # 2. No order id -> ask for it. Never pull an order id out of the ticket text.
    if not order_id:
        return done("needs_info", 0, "missing_order_id")

    # 3. Order through the principal-scoped view.
    budget.add_tool(tool_cost())
    order_view = lookup_order(conn, order_id, claim["principal_id"])
    if not order_view.get("visible"):
        trace.add("lookup_order", {"visible": False, "reason": order_view.get("reason")})
        return done("escalated", 0, order_view.get("reason"))
    order = dict(order_view["order"])
    trace.add("lookup_order", {"visible": True, "order": order})

    # 4. Quote from the order row and current policy only.
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add(
        "quote",
        {"amount_vnd": proposal, "reason": quote.get("reason"), "clause_ids": list(quote.get("clause_ids") or [])},
    )

    # 5. Nothing owed -> deny without touching the host gate.
    if proposal <= 0:
        return done("denied", 0, quote.get("reason"))

    # 6. Full quote to the host. The host checks approval, per-order cap, idempotency.
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    trace.add(
        "issue_refund",
        {"ok": bool(outcome["ok"]), "reason": outcome["reason"], "conflict": bool(outcome["conflict"])},
    )

    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return done(status, proposal, outcome["reason"])

    # 7. Committed (or idempotent replay of the same amount).
    return done("resolved", proposal, None)
