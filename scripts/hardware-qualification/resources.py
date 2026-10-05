#!/usr/bin/env python3
"""Offline simultaneous resource feasibility; never a physical qualification."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys


def number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite number")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(name + " is out of range")
    return value


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


def names(value, name):
    if not isinstance(value, list) or not value or any(
        not isinstance(v, str) or not v for v in value
    ) or len(set(value)) != len(value):
        raise ValueError(name + " must contain unique nonempty strings")
    return value


def objects(plan, key):
    rows = plan.get(key, [])
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError(key + " must be a list of at most 32 objects")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError(key + " entries need nonempty string ids")
        if row["id"] in seen:
            raise ValueError("duplicate " + key + " id: " + row["id"])
        seen.add(row["id"])
    return rows


def validate(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    if not plan.get("radio_demands") and not plan.get("receiver_demands"):
        raise ValueError("at least one demand is required")
    for key in ("phys", "receivers"):
        for row in objects(plan, key):
            if not isinstance(row.get("qualified"), bool) or not isinstance(row.get("available"), bool):
                raise ValueError(key + " require explicit qualified and available booleans")
    for phy in objects(plan, "phys"):
        names(phy.get("bands"), "bands")
        combos = phy.get("combinations")
        if not isinstance(combos, list) or not combos:
            raise ValueError("each PHY requires explicit interface combinations")
        for combo in combos:
            integer(combo["max_interfaces"], "max_interfaces")
            integer(combo["max_channels"], "max_channels")
            limits = combo.get("limits")
            if not isinstance(limits, list) or not limits:
                raise ValueError("combination requires role limits")
            covered = []
            for limit in limits:
                covered.extend(names(limit["roles"], "roles"))
                integer(limit["max"], "role maximum")
            if len(covered) != len(set(covered)):
                raise ValueError("roles may occur only once in a combination's limits")
    for row in objects(plan, "radio_demands"):
        for key in ("band", "role"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError("radio demand requires " + key)
        number(row["center_mhz"], "center_mhz", True)
        number(row["width_mhz"], "width_mhz", True)
    for row in objects(plan, "receivers"):
        names(row["services"], "services")
        integer(row["max_demodulators"], "max_demodulators")
        number(row["low_mhz"], "low_mhz", True)
        number(row["high_mhz"], "high_mhz", True)
        if row["low_mhz"] >= row["high_mhz"]:
            raise ValueError("receiver window must have positive width")
    for row in objects(plan, "receiver_demands"):
        if not isinstance(row.get("service"), str) or not row["service"]:
            raise ValueError("receiver demand requires service")
        number(row["center_mhz"], "center_mhz", True)
        number(row["width_mhz"], "width_mhz", True)


def radio_fits(phy, demands):
    if any(d["band"] not in phy["bands"] for d in demands):
        return False
    channels = {(d["band"], d["center_mhz"], d["width_mhz"]) for d in demands}
    roles = Counter(d["role"] for d in demands)
    for combo in phy["combinations"]:
        allowed = {role for limit in combo["limits"] for role in limit["roles"]}
        if len(demands) > combo["max_interfaces"] or len(channels) > combo["max_channels"]:
            continue
        if not set(roles).issubset(allowed):
            continue
        if all(sum(roles[role] for role in limit["roles"]) <= limit["max"]
               for limit in combo["limits"]):
            return True
    return False


def receiver_fits(receiver, demands):
    return len(demands) <= receiver["max_demodulators"] and all(
        d["service"] in receiver["services"]
        and d["center_mhz"] - d["width_mhz"] / 2 >= receiver["low_mhz"]
        and d["center_mhz"] + d["width_mhz"] / 2 <= receiver["high_mhz"]
        for d in demands
    )


def allocate(resources, demands, fits, allow_candidates):
    resources = [r for r in resources if r["available"] and (r["qualified"] or allow_candidates)]
    assigned = [[] for _ in resources]
    candidates = {d["id"]: [i for i, r in enumerate(resources) if fits(r, [d])] for d in demands}
    impossible = [d["id"] for d in demands if not candidates[d["id"]]]
    if impossible:
        return {"status": "infeasible", "unplaceable_demands": impossible, "assignment": {}}
    demands = sorted(demands, key=lambda d: (len(candidates[d["id"]]), d["id"]))
    steps = 0

    def search(index):
        nonlocal steps
        steps += 1
        if steps > 50000:
            raise RuntimeError("search budget exceeded")
        if index == len(demands):
            return True
        demand = demands[index]
        for i in candidates[demand["id"]]:
            proposed = assigned[i] + [demand]
            if fits(resources[i], proposed):
                assigned[i].append(demand)
                if search(index + 1):
                    return True
                assigned[i].pop()
        return False

    try:
        feasible = search(0)
    except RuntimeError:
        return {"status": "indeterminate", "reason": "search budget exceeded", "assignment": {}}
    result = {"status": "feasible" if feasible else "infeasible", "assignment": {}}
    if feasible:
        result["assignment"] = {r["id"]: [d["id"] for d in group]
                                for r, group in zip(resources, assigned) if group}
        result["uses_candidate_resources"] = any(
            not r["qualified"] for r, group in zip(resources, assigned) if group
        )
    else:
        result["reason"] = "simultaneous demands exceed permitted resource combinations"
    return result


def evaluate(plan, allow_candidates=False):
    validate(plan)
    radios = allocate(plan.get("phys", []), plan.get("radio_demands", []), radio_fits, allow_candidates)
    receivers = allocate(plan.get("receivers", []), plan.get("receiver_demands", []), receiver_fits, allow_candidates)
    statuses = [radios["status"], receivers["status"]]
    return {"schema_version": 1, "physical_qualification": "not_evaluated",
            "mode": "candidate_model" if allow_candidates else "qualified_resources_only",
            "status": "infeasible" if "infeasible" in statuses else
                      "indeterminate" if "indeterminate" in statuses else "feasible",
            "radios": radios, "receivers": receivers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--allow-candidates", action="store_true")
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.plan.read_text()), args.allow_candidates)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "feasible" else 1


if __name__ == "__main__":
    sys.exit(main())
