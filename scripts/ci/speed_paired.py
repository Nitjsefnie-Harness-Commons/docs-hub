"""Judge a head off the budget's toolchain against a base measured in its job."""
from __future__ import annotations

import speed_budget


def paired_check(document: dict, head: dict, base: dict) -> dict:
    """`check()`'s result plus `base_sha`; each row's `budget` is the base count."""
    # A lock bump is part of the head's delta.
    drift = [field for field in speed_budget.toolchain_diff(head["toolchain"], base["toolchain"])
             if field != "lock_sha256"]
    if drift:
        raise speed_budget.BudgetError(
            f"the base measurement's toolchain differs from the head's on {', '.join(drift)}")
    if not isinstance(base.get("base_sha"), str) or not base["base_sha"]:
        raise speed_budget.BudgetError("the base measurement names no base_sha")
    counts, bases, budgets = head["counts"], base["counts"], document["workloads"]
    tolerance = document["tolerance_pct"]
    rows, failed = [], False
    for name in sorted(set(budgets) | set(counts) | set(bases)):
        count, reference = counts.get(name), bases.get(name)
        delta = (count - reference
                 if count is not None and reference is not None else None)
        if name not in budgets:
            verdict = "unknown"
        elif count is None or reference is None:
            verdict = "missing"
        else:
            verdict = "FAIL" if count > reference * (1 + tolerance / 100) else "ok"
        failed = failed or verdict != "ok"
        rows.append({"name": name, "count": count, "budget": reference,
                     "delta": delta, "verdict": verdict})
    return {"toolchain_diff": speed_budget.toolchain_diff(document["toolchain"],
                                                          head["toolchain"]),
            "rows": rows, "failed": failed, "base_sha": base["base_sha"]}
