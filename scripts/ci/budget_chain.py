"""Numeric chain validation and exact declaration accounting for budget windows."""
from __future__ import annotations

import json
import math
from decimal import Decimal

ABSENT = object()
DOCUMENT = ".github/speed-budget.json"


def key_path(parts: list[str]) -> str:
    if not parts:
        return "(document)"
    rendered = []
    for index, part in enumerate(parts):
        if part.replace("_", "a").isalnum() and not part[0].isdigit():
            rendered.append(part if index == 0 else f".{part}")
        else:
            rendered.append(f"[{json.dumps(part)}]")
    return "".join(rendered)


def is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, int):
        return True
    # A JSON NaN/Infinity literal arrives as a float; it must fail closed.
    return isinstance(value, float) and math.isfinite(value)


def show(value: object) -> str:
    if value is None or value is ABSENT:
        return "absent"
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value, default=str)


def under(key: str, parent: str) -> bool:
    """Whether the rendered key names a leaf under parent."""
    return (key.startswith(f"{parent}.")
            or key.startswith(f"{parent}["))


def finding(document: str, key: str, base: object, head: object,
            why: str) -> str:
    return (f"{document}: {key}: reference {show(base)}, "
            f"head {show(head)} — {why}")


def consume_declaration(declarations: list, key: str, was: object,
                        now: object) -> bool:
    """Pop the one declaration naming this raise exactly."""
    for index, (_document, decl_key, decl_was, decl_to) in enumerate(
            declarations):
        if decl_key == key and decl_was == was and decl_to == now:
            del declarations[index]
            return True
    return False


def declaration_mismatch(document: str, key: str, was: object,
                         to: object) -> str:
    """The finding for a declaration the diff does not carry exactly."""
    return (f"{document}: {key}: declaration {was} -> {to} does not match "
            "a raise in this diff — a declaration must name the exact from "
            "and to the diff carries")


def numeric_key(key: str) -> bool:
    return under(key, "workloads") or key == "tolerance_pct"


def normalized(values: dict) -> dict:
    return {key: value for key, value in values.items()
            if not (numeric_key(key) and value is None)}


def same(left: object, right: object) -> bool:
    return is_number(left) == is_number(right) and left == right


def numeric_values(document: dict | None) -> dict:
    if not document:
        return {}
    values = {key_path(["workloads", key]): value
              for key, value in document.get("workloads", {}).items()}
    if "tolerance_pct" in document:
        values["tolerance_pct"] = document["tolerance_pct"]
    return normalized(values)


class ValueChains:
    """Keep each contributed numeric value, including invalid intermediates."""

    def __init__(self, base: dict) -> None:
        self.base = numeric_values(base)
        self.entries = {key: [("base", value)] for key, value in self.base.items()}

    def record(self, sha: str, parents: list, document: dict | None) -> None:
        before = [numeric_values(parent) for parent in parents] or [{}]
        after = numeric_values(document)
        for key in set(after).union(*before):
            values = [parent.get(key, ABSENT) for parent in before]
            new = after.get(key, ABSENT)
            chain = self.entries.setdefault(key, [("base", self.base.get(key, ABSENT))])
            # Parent values preserve invalid branch inputs even at a merge resolution.
            chain.extend((sha, value) for value in values
                         if value is not ABSENT and not is_number(value))
            if all(not same(value, new) for value in values):
                chain.append((sha, new))

    def finish(self, document: dict | None, pools: dict, originals: dict,
               eligible: set[str]) -> list[str]:
        findings = []
        for key, entries in self.entries.items():
            for sha, value in entries:
                if value is not ABSENT and not is_number(value):
                    findings.append(finding(DOCUMENT, key, self.base.get(key, ABSENT), value,
                                            f"not a finite number in value chain at {sha[:12]}"))
        after = numeric_values(document)
        for key, was in self.base.items():
            now = after.get(key, ABSENT)
            if not (is_number(was) and is_number(now) and now > was):
                continue
            exact = (DOCUMENT, key, was, now)
            # One line proves the same exact step and net raise, with one consumption.
            consumed = any(originals.get(sha, []).count(exact) > pools.get(sha, []).count(exact)
                           for sha in eligible)
            if not consumed and not any(consume_declaration(pools.get(sha, []), key, was, now)
                                        for sha in eligible):
                findings.append(finding(DOCUMENT, key, was, now,
                                        "net raised; it may only fall without a declaration"))
        return findings
