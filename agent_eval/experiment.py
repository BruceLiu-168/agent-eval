"""Checkpointed local experiments and evaluation of existing episode records."""

import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from . import __version__
from .adapters import execute_adapter
from .config import adapter_for, decode_json, load_config, read_json, validate_grader
from .contracts import load_dataset, public_case, validate_episode
from .core import release_gate
from .grade_runner import evaluate_bounded
from .reporting import build_report, write_report


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _save(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(_json(data) + "\n")
        file.flush()
        os.fsync(file.fileno())
        temporary = file.name
    os.replace(temporary, path)


@contextlib.contextmanager
def _module_path(directory):
    sys.path.insert(0, directory)
    try:
        yield
    finally:
        sys.path.remove(directory)


def _implementation(config):
    files = {p.name: _sha(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")}
    for label, adapter in [(v, adapter_for(config, v)) for v in config["versions"]] + [("grader", config["grader"])]:
        reference = adapter.get("callable")
        if reference:
            module, _, function = reference.partition(":")
            if not module or not function:
                raise ValueError(f"{label}: callable must use module:function")
            with _module_path(config["_directory"]):
                spec = importlib.util.find_spec(module)
            if spec is None:
                raise ValueError(f"{label}: Python module not found: {module}")
            if spec.origin and Path(spec.origin).is_file():
                files[label + ":" + reference] = _sha(Path(spec.origin).read_bytes())
        for argument in adapter.get("argv", []):
            source = Path(config["_directory"], argument)
            if source.is_file():
                files[label + ":file:" + argument] = _sha(source.read_bytes())
    return files


def _key(record):
    return record["version"], record["case_id"], record["trial"]


def _has_run_files(directory):
    return any(path.name != ".agent-eval.lock" for path in Path(directory).iterdir())


@contextlib.contextmanager
def _run_lock(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".agent-eval.lock"
    token = _json({"pid": os.getpid(), "token": str(uuid.uuid4())})
    try:
        with lock.open("x", encoding="utf-8") as file:
            file.write(token)
    except FileExistsError:
        raise ValueError("run directory is locked; stop the active run first. After a crash, verify the process ended before removing .agent-eval.lock") from None
    try:
        yield
    finally:
        if lock.exists() and lock.read_text(encoding="utf-8") == token:
            lock.unlink()


def _for_gate(records, version):
    grouped = {}
    for record in records:
        if record["version"] == version:
            grouped.setdefault(record["case_id"], []).append(record["grade"])
    results = []
    for grades in grouped.values():
        result = dict(grades[0])
        result["violations"] = sorted({v for g in grades for v in g["violations"]})
        statuses = [g["status"] for g in grades]
        result["status"] = ("fail" if result["violations"] or "fail" in statuses else
                            "infra_error" if "infra_error" in statuses else
                            "unknown" if "unknown" in statuses else "pass")
        results.append(result)
    return results


def make_gate(records, policy):
    if not policy.get("enabled"):
        return None
    result = release_gate(_for_gate(records, policy["baseline"]), _for_gate(records, policy["candidate"]),
                          **{k: policy[k] for k in ("min_families", "margin", "alpha", "min_success_rate")})
    result["policy"]["trial_aggregation"] = "Each case must pass every configured trial; repeats do not increase independent families."
    return result


def run_experiment(config_path, out_dir, resume=False, progress=None):
    with _run_lock(out_dir):
        return _run_experiment(config_path, out_dir, resume, progress)


def _run_experiment(config_path, out_dir, resume=False, progress=None):
    config = load_config(config_path)
    cases = load_dataset(config["dataset"])
    implementation = _implementation(config)
    fingerprint = _sha(_json({"config": config, "dataset": _sha(Path(config["dataset"]).read_bytes()),
                             "implementation": implementation}).encode())
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest_path, checkpoint = out / "manifest.json", out / "records.jsonl"
    if manifest_path.exists():
        if not resume:
            raise ValueError("run directory already exists; use --resume or a new --out directory")
        manifest = read_json(manifest_path)
        if manifest.get("fingerprint") != fingerprint:
            raise ValueError("resume refused: configuration, dataset or implementation changed")
    else:
        if resume:
            raise ValueError("resume requires an existing manifest in --out")
        if _has_run_files(out):
            raise ValueError("output directory must be empty for a new experiment")
        manifest = {"schema_version": 1, "run_id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                    "framework_version": __version__, "fingerprint": fingerprint,
                    "dataset_sha256": _sha(Path(config["dataset"]).read_bytes()),
                    "versions": config["versions"], "trials": config["execution"]["trials"],
                    "seed": config["execution"]["seed"], "case_count": len(cases),
                    "implementation": implementation, "provenance": config["provenance"],
                    "grader": {k: v for k, v in config["grader"].items() if k in ("kind", "model", "version", "callable")},
                    "status": "running"}
        _save(manifest_path, manifest)
    jobs = {(version, case["case_id"], trial): (version, case, trial)
            for version in config["versions"] for case in cases for trial in range(config["execution"]["trials"])}
    records, seen = [], set()
    if checkpoint.exists():
        for number, line in enumerate(checkpoint.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = decode_json(line)
                saved_hash = record.get("record_sha256")
                unsigned = {k: v for k, v in record.items() if k != "record_sha256"}
                if saved_hash != _sha(_json(unsigned).encode()):
                    raise ValueError("record checksum differs")
                key = _key(record)
                if type(key[2]) is not int or key[2] < 0:
                    raise ValueError("invalid trial")
                if key not in jobs or key in seen:
                    raise ValueError("unexpected or duplicate record")
                validate_episode(record["episode"], key[1], key[0])
                if record.get("seed") != config["execution"]["seed"] + key[2]:
                    raise ValueError("trial seed differs")
                if record["grade"].get("status") not in ("pass", "fail", "unknown", "infra_error"):
                    raise ValueError("invalid grade")
                case, grade = jobs[key][1], record["grade"]
                identities = {"case_id": key[1], "agent_version": key[0], "business": case["business"],
                              "family_id": case["family_id"], "episode_id": record["episode"]["episode_id"]}
                if any(grade.get(k) != v for k, v in identities.items()):
                    raise ValueError("grade identity differs")
                if any(not isinstance(grade.get(k), list) or any(not isinstance(x, str) for x in grade[k])
                       for k in ("violations", "evidence")):
                    raise ValueError("grade evidence is malformed")
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"checkpoint line {number} is invalid; inspect it before resuming") from exc
            records.append(record)
            seen.add(key)
    if manifest.get("status") == "completed" and (not checkpoint.exists() or
            len(records) != manifest.get("record_count") or len(records) != len(jobs)):
        raise ValueError("completed run checkpoint is missing or incomplete; restore the original records before resuming")
    manifest["status"] = "running"
    _save(manifest_path, manifest)

    def run(job):
        version, case, trial = job
        seed = config["execution"]["seed"] + trial
        episode = execute_adapter(adapter_for(config, version), public_case(case), version, seed,
                                  timeout=config["execution"]["timeout_seconds"])
        try:
            episode = validate_episode(episode, case["case_id"], version)
        except ValueError:
            episode = {"episode_id": f"{version}:{case['case_id']}:{seed}", "case_id": case["case_id"],
                       "agent_version": version, "status": "infra_error", "events": [],
                       "error_type": "adapter_contract", "cost": None, "latency_ms": None}
        grade = evaluate_bounded(case, episode, config["grader"], config["_directory"],
                                 timeout=config["grader"].get("timeout", 30))
        return {"version": version, "trial": trial, "seed": seed, "case_id": case["case_id"],
                "episode": episode, "grade": grade}

    try:
        with _module_path(config["_directory"]), checkpoint.open("a", encoding="utf-8") as sink:
            with ThreadPoolExecutor(max_workers=config["execution"]["workers"]) as pool:
                futures = [pool.submit(run, job) for key, job in jobs.items() if key not in seen]
                for future in as_completed(futures):
                    record = future.result()
                    record["record_sha256"] = _sha(_json(record).encode())
                    sink.write(_json(record) + "\n")
                    sink.flush()
                    os.fsync(sink.fileno())
                    records.append(record)
                    if progress:
                        progress(len(records), len(jobs))
    except BaseException:
        manifest["status"] = "interrupted"
        _save(manifest_path, manifest)
        raise
    records.sort(key=_key)
    manifest["status"] = "completed"
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["record_count"] = len(records)
    _save(manifest_path, manifest)
    report = build_report(manifest, records, make_gate(records, config["gate"]))
    paths = write_report(report, out)
    return {"run_id": manifest["run_id"], "records": len(records), "gate": report.get("gate"), "reports": paths}


def evaluate_recorded(dataset, episodes_path, out_dir, grader_config=None, working_directory=None):
    with _run_lock(out_dir):
        return _evaluate_recorded(dataset, episodes_path, out_dir, grader_config, working_directory)


def _evaluate_recorded(dataset, episodes_path, out_dir, grader_config=None, working_directory=None):
    out = Path(out_dir)
    if _has_run_files(out):
        raise ValueError("output directory must be empty")
    grader_config = validate_grader(grader_config if grader_config is not None else {"kind": "deterministic"})
    cases = {case["case_id"]: case for case in load_dataset(dataset)}
    records, seen = [], set()
    for number, line in enumerate(Path(episodes_path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = decode_json(line)
            if not isinstance(item, dict):
                raise ValueError("episode record must be an object")
            episode = item.get("episode", item)
            episode = validate_episode(episode)
            case = cases[episode["case_id"]]
            trial = item.get("trial", 0)
            if type(trial) is not int or trial < 0:
                raise ValueError("trial must be a nonnegative integer")
            record = {"version": episode["agent_version"], "case_id": case["case_id"],
                      "trial": trial, "seed": item.get("seed"), "episode": episode,
                      "grade": evaluate_bounded(case, episode, grader_config, working_directory,
                                                 timeout=grader_config.get("timeout", 30))}
            if _key(record) in seen:
                raise ValueError("duplicate case/version/trial")
            records.append(record)
            seen.add(_key(record))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"episode file line {number}: {type(exc).__name__}; check schema, case ID and uniqueness") from exc
    if not records:
        raise ValueError("episode file must not be empty")
    manifest = {"schema_version": 1, "run_id": str(uuid.uuid4()), "mode": "recorded_episodes",
                "created_at": datetime.now(timezone.utc).isoformat(), "framework_version": __version__,
                "dataset_sha256": _sha(Path(dataset).read_bytes()),
                "episodes_sha256": _sha(Path(episodes_path).read_bytes()), "status": "completed",
                "dataset_case_count": len(cases), "observed_case_count": len({r['case_id'] for r in records}),
                "versions": sorted({r["version"] for r in records}),
                "note": "Metrics cover supplied episodes only. Missing dataset cases are not counted as evaluated."}
    out.mkdir(parents=True, exist_ok=True)
    _save(out / "manifest.json", manifest)
    report = build_report(manifest, records)
    return {"run_id": manifest["run_id"], "records": len(records), "gate": None,
            "reports": write_report(report, out)}
