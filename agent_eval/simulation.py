"""Deterministic demo agents and a stateful, entirely local business sandbox.

These are scripted policies, not LLM calls or a production integration. Costs and
latencies are synthetic. Mutation tools deliberately model a legacy service
account with broader authority than an end user: the agent must check identity,
policy, and approval before invoking them. Production tools should additionally
enforce those checks at the capability boundary.

The sandbox never reads a case's expected_state or trajectory assertions. Its
state transitions derive only from input, initial state, and tool semantics.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any


VERSIONS = {"baseline", "candidate", "regressed"}
MUTATIONS = {"commit_refund", "grant_access"}


def load_demo_cases() -> list[dict[str, Any]]:
    """Load the checked-in examples without altering them."""
    from .scaffold import bundled_dataset
    source = bundled_dataset()
    return [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class _Sandbox:
    def __init__(self, case: dict[str, Any], seed: int):
        self.business = case["business"]
        self.request = copy.deepcopy(case["input"])
        self.state = copy.deepcopy(case["initial_state"])
        self.state.setdefault("operations", {})
        self.events: list[dict[str, Any]] = []
        self.agent_id = "orchestrator"
        self.rng = random.Random(seed)
        self.cost = 0.0
        self.latency_ms = 0
        self.fault_used = False

    def emit(self, action: str, args: dict[str, Any], result: dict[str, Any]):
        latency = (160 if action in MUTATIONS else 35) + self.rng.randint(0, 12)
        if result.get("error") == "timeout":
            latency += 650
        cost = 0.002 if action in MUTATIONS else 0.0004
        self.cost = round(self.cost + cost, 6)
        self.latency_ms += latency
        self.events.append({"action": action, "args": copy.deepcopy(args),
                            "result": copy.deepcopy(result), "agent_id": self.agent_id,
                            "cost": cost, "latency_ms": latency})
        return result

    def handoff(self, agent_id: str, reason: str):
        self.emit("handoff", {"to_agent": agent_id, "reason": reason}, {"ok": True})
        self.agent_id = agent_id

    def call(self, action: str, **args: Any):
        handler = getattr(self, f"_tool_{action}", None)
        if handler is None:
            raise ValueError(f"Unknown demo tool: {action}")
        if action in MUTATIONS:
            fault = self.request.get("failure_mode", "none")
            if fault == "tool_error":
                return self.emit(action, args, {"ok": False, "error": "unavailable"})
            if not self.fault_used and fault == "timeout_before_commit":
                self.fault_used = True
                return self.emit(action, args, {"ok": False, "error": "timeout",
                                               "commit_status": "unknown"})
            result = handler(**args)
            if not self.fault_used and fault == "timeout_after_commit" and result.get("ok"):
                self.fault_used = True
                # State is already committed, but the caller never sees the response.
                return self.emit(action, args, {"ok": False, "error": "timeout",
                                               "commit_status": "unknown"})
        else:
            result = handler(**args)
        return self.emit(action, args, result)

    def _tool_verify_identity(self, actor_id: str):
        identity = self.state.get("identities", {}).get(actor_id)
        if not identity or not identity.get("active", False):
            return {"ok": False, "reason": "unknown_or_inactive_identity"}
        return {"ok": True, "actor_id": actor_id, "role": identity.get("role", "user")}

    def _tool_check_refund_policy(self, actor_id: str, order_id: str, amount: float):
        order = self.state["order"]
        if order["id"] != order_id or order["owner_id"] != actor_id:
            return {"ok": False, "reason": "not_order_owner"}
        existing = self.state["operations"].get(self.request["request_id"])
        payload = {"business": "refund", "order_id": order_id, "amount": amount}
        if existing and existing.get("payload") == payload:
            return {"ok": True, "idempotent_replay": True}
        if amount <= 0 or amount > order["paid"] - order["refunded"]:
            return {"ok": False, "reason": "amount_outside_refundable_balance"}
        return {"ok": True}

    def _tool_check_access_policy(self, actor_id: str, user_id: str, resource_id: str):
        identity = self.state.get("identities", {}).get(actor_id, {})
        policy = self.state["policy"]
        access = self.state["access"]
        allowed = (actor_id == user_id and identity.get("active", False)
                   and identity.get("role") in policy["allowed_roles"]
                   and resource_id not in policy.get("denied_resources", [])
                   and user_id == access["user_id"] and resource_id == access["resource_id"])
        return {"ok": allowed, "reason": "eligible" if allowed else "policy_denied",
                "requires_approval": policy.get("requires_approval", True)}

    def _tool_check_approval(self, request_id: str):
        status = self.state.get("approvals", {}).get(request_id, "pending")
        return {"ok": status == "approved", "approval_status": status}

    def _tool_request_approval(self, request_id: str):
        # A trusted stub service makes this decision, never the requester's input.
        decision = self.state.get("approval_service", {}).get("decision", "pending")
        if decision not in {"approved", "pending", "denied"}:
            decision = "pending"
        self.state.setdefault("approvals", {})[request_id] = decision
        return {"ok": True, "approval_status": decision}

    def _tool_lookup_operation(self, idempotency_key: str):
        operation = self.state["operations"].get(idempotency_key)
        return {"ok": True, "found": operation is not None,
                "operation": copy.deepcopy(operation)}

    def _existing_operation(self, key: str, payload: dict[str, Any]):
        existing = self.state["operations"].get(key)
        if existing is None:
            return None
        if existing.get("payload") != payload:
            return {"ok": False, "error": "idempotency_conflict"}
        return {"ok": True, "deduplicated": True, "operation": copy.deepcopy(existing)}

    def _tool_commit_refund(self, order_id: str, amount: float, idempotency_key: str):
        payload = {"business": "refund", "order_id": order_id, "amount": amount}
        existing = self._existing_operation(idempotency_key, payload)
        if existing is not None:
            return existing
        order = self.state["order"]
        if order["id"] != order_id or amount <= 0 or amount > order["paid"] - order["refunded"]:
            return {"ok": False, "error": "invalid_refund"}
        order["refunded"] = round(order["refunded"] + amount, 2)
        order["refund_count"] += 1
        operation = {"status": "committed", "payload": payload}
        self.state["operations"][idempotency_key] = operation
        return {"ok": True, "deduplicated": False, "operation": copy.deepcopy(operation)}

    def _tool_grant_access(self, user_id: str, resource_id: str, idempotency_key: str):
        payload = {"business": "access_request", "user_id": user_id, "resource_id": resource_id}
        existing = self._existing_operation(idempotency_key, payload)
        if existing is not None:
            return existing
        access = self.state["access"]
        if access["user_id"] != user_id or access["resource_id"] != resource_id:
            return {"ok": False, "error": "unknown_grant_target"}
        # Permission membership is naturally idempotent in addition to request keys.
        if not access["granted"]:
            access["granted"] = True
            access["grant_count"] += 1
        operation = {"status": "committed", "payload": payload}
        self.state["operations"][idempotency_key] = operation
        return {"ok": True, "deduplicated": False, "operation": copy.deepcopy(operation)}

    def finish(self, outcome: str):
        self.state["outcome"] = outcome
        self.emit("respond", {"outcome": outcome}, {"ok": outcome != "infra_error"})
        return "infra_error" if outcome == "infra_error" else "completed"


def _execute_mutation(sandbox: _Sandbox, version: str, action: str, args: dict[str, Any]):
    request_id = sandbox.request["request_id"]
    for attempt in range(3):
        # baseline's accidental per-attempt key loses exactly-once semantics.
        key = request_id if version == "candidate" else f"{request_id}:attempt:{attempt}"
        result = sandbox.call(action, **args, idempotency_key=key)
        if result.get("ok"):
            if version == "regressed":
                sandbox.call(action, **args, idempotency_key=f"{request_id}:unsafe_duplicate")
            return sandbox.finish("succeeded")
        if result.get("error") == "timeout" and version == "candidate":
            lookup = sandbox.call("lookup_operation", idempotency_key=key)
            if lookup["found"]:
                return sandbox.finish("succeeded")
        elif result.get("error") not in {"timeout", "unavailable"}:
            return sandbox.finish("rejected")
    sandbox.handoff("human_support", "mutation_service_unavailable_after_bounded_retries")
    return sandbox.finish("infra_error")


def _run_scripted_policy(sandbox: _Sandbox, version: str):
    request = sandbox.request
    sandbox.handoff("refund_agent" if sandbox.business == "refund" else "access_agent",
                    "route_business_request")
    if version != "regressed":
        identity = sandbox.call("verify_identity", actor_id=request["actor_id"])
        if not identity["ok"]:
            return sandbox.finish("rejected")
    if sandbox.business == "refund":
        args = {"order_id": request["order_id"], "amount": request["amount"]}
        if version != "regressed":
            policy = sandbox.call("check_refund_policy", actor_id=request["actor_id"], **args)
            if not policy["ok"]:
                return sandbox.finish("rejected")
        return _execute_mutation(sandbox, version, "commit_refund", args)
    args = {"user_id": request["user_id"], "resource_id": request["resource_id"]}
    if version != "regressed":
        policy = sandbox.call("check_access_policy", actor_id=request["actor_id"], **args)
        if not policy["ok"]:
            return sandbox.finish("rejected")
        if policy["requires_approval"] and version == "candidate":
            approval = sandbox.call("check_approval", request_id=request["request_id"])
            if not approval["ok"] and approval["approval_status"] == "pending":
                if request.get("allow_approval_request", False):
                    sandbox.handoff("approval_agent", "approval_required")
                    sandbox.call("request_approval", request_id=request["request_id"])
                    approval = sandbox.call("check_approval", request_id=request["request_id"])
                    sandbox.handoff("access_agent", "approval_service_responded")
            if not approval["ok"]:
                return sandbox.finish("awaiting_approval" if approval["approval_status"] == "pending"
                                      else "rejected")
    return _execute_mutation(sandbox, version, "grant_access", args)


def run_case(case: dict[str, Any], version: str, seed: int = 0) -> dict[str, Any]:
    """Run one isolated episode; repeated (case, version, seed) is reproducible.

    ``baseline`` mishandles idempotency and approval. ``candidate`` repairs both.
    ``regressed`` additionally skips identity/policy checks and duplicates writes.
    Expected answers and trajectory rules are deliberately not used here.
    """
    if version not in VERSIONS:
        raise ValueError(f"version must be one of {sorted(VERSIONS)}")
    if case.get("business") not in {"refund", "access_request"}:
        raise ValueError("business must be refund or access_request")
    sandbox = _Sandbox(case, seed)
    status = _run_scripted_policy(sandbox, version)
    return {"episode_id": f"{version}:{case['case_id']}:{seed}", "case_id": case["case_id"],
            "agent_version": version, "seed": seed, "business": case["business"],
            "final_state": sandbox.state, "events": sandbox.events,
            "cost": sandbox.cost, "latency_ms": sandbox.latency_ms, "status": status}
