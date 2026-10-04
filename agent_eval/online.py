"""Local online-evaluation feedback store (standard library only).

Sampling uses a stable SHA-256 assignment, never a risk-triggered admission rule.
Only random-stream, mature, labelled outcomes estimate production success. The
risk stream is for discovery and may overlap the random stream.

Redaction covers sensitive dictionary keys and email text, recursively. Email
identities use stable per-store HMAC pseudonyms, preserving equality and object
key distinctness. This is not complete PII/DLP, nor a guarantee that email-domain
or parsing-dependent business logic is preserved. The local prototype keeps its
random HMAC key in the protected database; production should use managed keys.
Use one store to own a given regression JSONL destination.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate_case, validate_episode


_EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
_SECRET_KEY = re.compile(r"password|passwd|(?:^|_)pwd(?:_|$)|token|secret|credential|apikey|api_key|authorization|cookie|privatekey|private_key", re.I)
_REDACTED = "[REDACTED]"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone; naive timestamps are rejected")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: str, field: str) -> str:
    return _time(value, field).isoformat().replace("+00:00", "Z")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return value


def redact(value: Any, *, pseudonym_key: bytes | None = None) -> Any:
    """Copy and redact, retaining equality of distinct email identities.

    With a key, email pseudonyms use HMAC-SHA256. The pure API's keyless SHA-256
    fallback is only a deterministic example: it is susceptible to dictionary
    guesses and is unsuitable for production. OnlineStore always supplies its
    persistent random key. Secret-key values remain opaque redaction markers.
    """
    if pseudonym_key is not None and (not isinstance(pseudonym_key, bytes) or not pseudonym_key):
        raise ValueError("pseudonym_key must be nonempty bytes")
    # Reserved markers avoid re-pseudonymizing already sanitized records and
    # prevent a crafted literal marker from colliding with a generated value.
    reserved = re.compile(r"\[EMAIL:[0-9a-f]{64}\]")

    def email_text(text: str) -> str:
        if reserved.search(text):
            raise ValueError("raw input contains a reserved email pseudonym marker")

        def pseudonym(match: re.Match[str]) -> str:
            message = b"agent-eval/email/v1\0" + match.group(0).encode()
            digest = (hmac.new(pseudonym_key, message, hashlib.sha256).hexdigest()
                      if pseudonym_key is not None else hashlib.sha256(message).hexdigest())
            return f"[EMAIL:{digest}]"

        return _EMAIL.sub(pseudonym, text)

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            result = {}
            for key, child in item.items():
                safe_key = email_text(key)
                if safe_key in result:
                    raise ValueError("redaction would merge distinct object keys")
                # An email identity used as a map key is not a secret field,
                # even if its local part contains words such as "token".
                secret_field = _EMAIL.fullmatch(key) is None and _SECRET_KEY.search(key.replace("-", "_"))
                result[safe_key] = _REDACTED if secret_field else visit(child)
            return result
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str):
            return email_text(item)
        return item

    return visit(value)


def _outcome(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("outcome must be an object")
    success = value.get("success")
    if success is not None and not isinstance(success, bool):
        raise ValueError("outcome.success must be true, false, or null")
    return {
        "mature_at": _timestamp(value.get("mature_at"), "outcome.mature_at"),
        "success": success,
        "source": _text(value.get("source"), "outcome.source"),
    }


def _review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("verified"), bool):
        raise ValueError("review.verified must be a boolean")
    reviewer = value.get("reviewer", "")
    if not isinstance(reviewer, str) or (value["verified"] and not reviewer.strip()):
        raise ValueError("a verified review requires a named reviewer")
    return {"verified": value["verified"], "reviewer": reviewer}


def _case_error(case: Any) -> str | None:
    """Return a promotion refusal while using the shared framework contract."""
    if not isinstance(case, dict):
        return "missing replay_case"
    if not (isinstance(case.get("expected_state"), dict) and case["expected_state"]
            or "expected_output" in case or case.get("checks") or case.get("rubric")):
        return "missing oracle: replay_case requires expected_state, expected_output, checks, or rubric"
    try:
        validate_case(case)
    except ValueError as exc:
        message = str(exc).replace("case.", "replay_case.", 1)
        # Keep the existing diagnostic recognizable for users migrating datasets.
        if "required_order" in message or "required_success_before" in message:
            message += "; expected action pairs [before, after]"
        return message
    return None


def _fingerprint(case: dict[str, Any]) -> str:
    # Include all grading semantics, including explicit null output. Renamed
    # copies deduplicate, while altered checks/rubrics remain distinct scenarios.
    normalized = validate_case(case)
    meaningful = {key: normalized[key] for key in (
        "business", "input", "initial_state", "expected_state", "expected_output",
    ) if key in normalized}
    meaningful["family_id"] = (None if normalized["family_id"] == normalized["case_id"]
                               else normalized["family_id"])
    for key in ("forbidden_actions", "required_actions", "required_order", "required_success_before",
                "checks", "rubric"):
        meaningful[key] = normalized.get(key, [])
    meaningful["limits"] = normalized.get("limits", {})
    return hashlib.sha256(_json(meaningful).encode()).hexdigest()


def _episode_key(episode_id: str) -> str:
    return hashlib.sha256(episode_id.encode()).hexdigest()


class OnlineStore:
    """SQLite-backed, idempotent episode intake and reviewed failure promotion."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                intake_payload TEXT NOT NULL,
                random_rate REAL NOT NULL,
                is_random INTEGER NOT NULL,
                is_risk INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promotions (
                destination TEXT NOT NULL,
                episode_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                PRIMARY KEY (destination, episode_key)
            );
            CREATE TABLE IF NOT EXISTS store_metadata (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            );
        """)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO store_metadata VALUES (?, ?)",
                                    ("email_hmac_key_v1", secrets.token_bytes(32)))
            self._pseudonym_key = bytes(self.connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?", ("email_hmac_key_v1",)
            ).fetchone()["value"])

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "OnlineStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def ingest(self, record: dict[str, Any], random_rate: float = 1.0) -> dict[str, Any]:
        """Persist a redacted event once; conflicting duplicate IDs are rejected.

        Resubmission of the original intake remains idempotent after annotation.
        Sampling is assigned only at the first intake and never changed by retry.
        """
        _number(random_rate, "random_rate")
        if random_rate > 1:
            raise ValueError("random_rate must be between 0 and 1")
        if not isinstance(record, dict) or not isinstance(record.get("episode"), dict):
            raise ValueError("record.episode must be an object")
        episode = validate_episode(record["episode"])
        episode_id = episode["episode_id"]
        if not isinstance(episode.get("metadata", {}), dict):
            raise ValueError("episode.metadata must be an object")
        risk_flags = record.get("risk_flags", [])
        if not isinstance(risk_flags, list) or not all(isinstance(flag, str) and flag for flag in risk_flags):
            raise ValueError("risk_flags must be a list of nonempty strings")
        normalized = dict(record)
        normalized["episode"] = episode
        normalized["observed_at"] = _timestamp(record.get("observed_at"), "observed_at")
        normalized["outcome"] = _outcome(record.get("outcome"))
        normalized["review"] = _review(record.get("review", {"verified": False, "reviewer": ""}))
        normalized["risk_flags"] = sorted(set(risk_flags))
        sanitized = redact(normalized, pseudonym_key=self._pseudonym_key)
        payload = _json(sanitized)
        key = _episode_key(episode_id)
        draw = int(hashlib.sha256(("agent-eval/random/v1\0" + episode_id).encode()).hexdigest(), 16)
        selected = draw < random_rate * (1 << 256)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO episodes VALUES (?, ?, ?, ?, ?, ?)",
                (key, payload, payload, random_rate, int(selected), int(bool(risk_flags))),
            )
            row = self.connection.execute("SELECT * FROM episodes WHERE episode_key = ?", (key,)).fetchone()
            if row["intake_payload"] != payload and row["payload"] != payload:
                raise ValueError("conflicting episode_id; use annotate() for delayed outcomes or review")
        return {
            "episode_id": sanitized["episode"]["episode_id"],
            "inserted": cursor.rowcount == 1,
            "random_selected": bool(row["is_random"]),
            "risk_selected": bool(row["is_risk"]),
            "sample_rate": row["random_rate"],
        }

    def annotate(
        self, episode_id: str, *, outcome: dict[str, Any] | None = None,
        review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Explicitly backfill a business label/review without resampling an episode.

        Reports are maturity-filtered current-state views, not historical label
        snapshots; annotate does not implement a full audit/event-sourcing system.
        """
        key = _episode_key(_text(episode_id, "episode_id"))
        with self.connection:
            row = self.connection.execute("SELECT payload FROM episodes WHERE episode_key = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(episode_id)
            record = json.loads(row["payload"])
            if outcome is not None:
                record["outcome"] = redact(_outcome(outcome), pseudonym_key=self._pseudonym_key)
            if review is not None:
                record["review"] = redact(_review(review), pseudonym_key=self._pseudonym_key)
            self.connection.execute("UPDATE episodes SET payload = ? WHERE episode_key = ?", (_json(record), key))
        return {"episode_id": record["episode"]["episode_id"], "updated": outcome is not None or review is not None}

    def report(self, as_of: str) -> dict[str, Any]:
        """Estimate success only from randomly admitted, mature labelled episodes."""
        cutoff = _time(as_of, "as_of")
        def counts() -> dict[str, Any]:
            return {"selected": 0, "pending": 0, "unknown": 0, "mature_labelled": 0,
                    "successes": 0, "failures": 0, "infra_errors": 0}
        random = counts()
        risk = counts()
        observed = future = unselected = overlap = 0
        rate_counts: dict[float, int] = {}
        weighted_success = weighted_labelled = 0.0
        for row in self.connection.execute("SELECT * FROM episodes ORDER BY episode_key"):
            record = json.loads(row["payload"])
            if _time(record["observed_at"], "observed_at") > cutoff:
                future += 1
                continue
            observed += 1
            rate = row["random_rate"]
            rate_counts[rate] = rate_counts.get(rate, 0) + 1
            overlap += bool(row["is_random"] and row["is_risk"])
            unselected += not row["is_random"] and not row["is_risk"]
            outcome = record["outcome"]
            mature = _time(outcome["mature_at"], "mature_at") <= cutoff
            labelled = isinstance(outcome["success"], bool)
            for selected, group in ((row["is_random"], random), (row["is_risk"], risk)):
                if not selected:
                    continue
                group["selected"] += 1
                group["infra_errors"] += record["episode"]["status"] == "infra_error"
                if not mature:
                    group["pending"] += 1
                elif not labelled:
                    group["unknown"] += 1
                else:
                    group["mature_labelled"] += 1
                    group["successes" if outcome["success"] else "failures"] += 1
            if row["is_random"] and mature and labelled:
                weighted_labelled += 1.0 / rate
                weighted_success += int(outcome["success"]) / rate
        random["success_rate"] = (
            weighted_success / weighted_labelled if weighted_labelled and 0.0 not in rate_counts else None
        )
        random["unweighted_sample_success_rate"] = (
            random["successes"] / random["mature_labelled"] if random["mature_labelled"] else None
        )
        random["estimator"] = "inverse-probability weighted ratio over mature labelled random episodes"
        random["denominator_warning"] = (
            "Conditional on labels observed; missing/late labels can bias this estimate. "
            "A zero-rate intake stratum prevents estimating all observed traffic."
        )
        return {
            "as_of": cutoff.isoformat().replace("+00:00", "Z"),
            "observed": observed, "future_excluded": future, "unselected": unselected,
            "sample_rate": next(iter(rate_counts)) if len(rate_counts) == 1 else None,
            "sample_rates": [{"rate": rate, "observed": count} for rate, count in sorted(rate_counts.items())],
            "random": random, "risk_discovery": risk, "overlap": overlap,
            "streams_are_disjoint": False,
            "risk_note": "Risk discovery counts overlap the random stream and are not a population success estimate.",
        }

    def promote(self, output_path: str | Path, as_of: str) -> dict[str, Any]:
        """Atomically append reviewed, mature failures with explicit replay oracles.

        Deduplicate both source episodes and semantic cases, including cases
        already present in the destination. Refusals are returned with reasons.
        No oracle is inferred from the failed episode's final state.
        """
        cutoff = _time(as_of, "as_of")
        destination = Path(output_path).resolve()
        if destination == self.path.resolve():
            raise ValueError("regression destination must differ from the SQLite database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        promoted: list[str] = []
        rejected: list[dict[str, str]] = []
        duplicates = skipped = 0
        temporary: str | None = None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
            fingerprints: set[str] = set()
            case_id_fingerprints: dict[str, str] = {}
            for line_number, line in enumerate(existing.splitlines(), 1):
                if not line.strip():
                    continue
                case = json.loads(line)
                error = _case_error(case)
                if error:
                    raise ValueError(f"invalid regression file line {line_number}: {error}")
                case = validate_case(case)
                fingerprint = _fingerprint(case)
                if case["case_id"] in case_id_fingerprints:
                    raise ValueError(f"invalid regression file line {line_number}: duplicate case_id")
                fingerprints.add(fingerprint)
                case_id_fingerprints[case["case_id"]] = fingerprint
            additions: list[str] = []
            for row in self.connection.execute("SELECT * FROM episodes ORDER BY episode_key"):
                record = json.loads(row["payload"])
                outcome = record["outcome"]
                if (_time(record["observed_at"], "observed_at") > cutoff
                    or _time(outcome["mature_at"], "mature_at") > cutoff
                    or outcome["success"] is not False
                    or not record["review"]["verified"]
                    or not (row["is_random"] or row["is_risk"])):
                    skipped += 1
                    continue
                episode_id = record["episode"]["episode_id"]
                case = record.get("replay_case")
                error = _case_error(case)
                if error:
                    rejected.append({"episode_id": episode_id, "reason": error})
                    continue
                case = validate_case(case)
                fingerprint = _fingerprint(case)
                existing_fingerprint = case_id_fingerprints.get(case["case_id"])
                if existing_fingerprint is not None and existing_fingerprint != fingerprint:
                    rejected.append({"episode_id": episode_id,
                                     "reason": f"case_id conflict: {case['case_id']} identifies a different scenario"})
                    continue
                # The file is the materialized artifact. A historical ledger row
                # cannot suppress restoration after deletion or partial loss.
                if fingerprint in fingerprints:
                    duplicates += 1
                else:
                    additions.append(_json(case))
                    fingerprints.add(fingerprint)
                    case_id_fingerprints[case["case_id"]] = fingerprint
                    promoted.append(case["case_id"])
                self.connection.execute("INSERT OR IGNORE INTO promotions VALUES (?, ?, ?)",
                                        (str(destination), row["episode_key"], fingerprint))
            if additions:
                content = existing + ("\n" if existing and not existing.endswith("\n") else "")
                content += "\n".join(additions) + "\n"
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                                 prefix=".regression-", delete=False) as output:
                    temporary = output.name
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                temporary = None
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            raise
        return {"output_path": str(destination), "promoted": len(promoted), "case_ids": promoted,
                "duplicates": duplicates, "skipped": skipped, "rejected": rejected}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("input", help="JSONL records")
    ingest_parser.add_argument("--random-rate", type=float, default=1.0)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--as-of", required=True)
    promote_parser = sub.add_parser("promote")
    promote_parser.add_argument("output")
    promote_parser.add_argument("--as-of", required=True)
    annotate_parser = sub.add_parser("annotate", help="backfill delayed outcome labels and review")
    annotate_parser.add_argument("--episode-id", required=True)
    annotate_parser.add_argument("--outcome", help="JSON file containing mature_at, success, and source")
    annotate_parser.add_argument("--review", help="JSON file containing verified and reviewer")
    args = parser.parse_args(argv)
    if args.command == "annotate" and not (args.outcome or args.review):
        parser.error("annotate requires at least one of --outcome or --review")
    try:
        with OnlineStore(args.db) as store:
            if args.command == "ingest":
                with open(args.input, encoding="utf-8") as source:
                    result = [store.ingest(json.loads(line), args.random_rate) for line in source if line.strip()]
            elif args.command == "report":
                result = store.report(args.as_of)
            elif args.command == "annotate":
                updates = {}
                for name in ("outcome", "review"):
                    source_path = getattr(args, name)
                    if source_path:
                        with open(source_path, encoding="utf-8") as source:
                            value = json.load(source)
                        if not isinstance(value, dict):
                            raise ValueError("annotation file must contain a JSON object")
                        updates[name] = value
                result = store.annotate(args.episode_id, **updates)
            else:
                result = store.promote(args.output, args.as_of)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyError:
        parser.exit(2, "error: episode was not found\n")
    except (ValueError, OSError, sqlite3.Error) as exc:
        # File names, malformed JSON keys, or driver messages may contain secret
        # user data. Expose only the error type and a stable corrective hint.
        parser.exit(2, f"error: {type(exc).__name__}: operation failed; check input format and local paths\n")


if __name__ == "__main__":
    raise SystemExit(main())
