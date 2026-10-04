"""Small, explicit JSON experiment configurations; paths follow the config file."""

import copy
import json
import math
from pathlib import Path


def decode_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non-finite JSON number: " + value)
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=invalid)


def read_json(path):
    return decode_json(Path(path).read_text(encoding="utf-8"))


def validate_grader(grader):
    if not isinstance(grader, dict) or grader.get("kind", "deterministic") not in ("deterministic", "llm", "python", "callable"):
        raise ValueError("grader: unsupported kind")
    if any(key in grader for key in ("api_key", "token", "password")):
        raise ValueError("grader: use api_key_env for credentials")
    if grader.get("kind") == "llm" and any(not isinstance(grader.get(k), str) or not grader[k].strip() for k in ("base_url", "model", "version")):
        raise ValueError("grader: LLM judge requires explicit base_url, model and version")
    if grader.get("kind") in ("python", "callable") and (not isinstance(grader.get("callable"), str) or ":" not in grader["callable"]):
        raise ValueError("grader: custom grader requires module:function callable")
    _numeric(grader.get("timeout", 30), "grader.timeout", 0)
    if grader.get("timeout", 30) <= 0:
        raise ValueError("grader.timeout: must be positive")
    return grader


def _numeric(value, name, minimum, maximum=None, integer=False):
    try:
        valid = (type(value) in ((int,) if integer else (int, float))
                 and math.isfinite(value) and value >= minimum
                 and (maximum is None or value <= maximum))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name}: invalid {'integer' if integer else 'number'}")


def load_config(path):
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError("config: must be an object")
    unknown = set(config) - {"schema_version", "dataset", "adapter", "version_adapters", "versions",
                             "execution", "grader", "gate", "provenance"}
    if unknown:
        raise ValueError("config: unknown fields " + ", ".join(sorted(unknown)))
    if type(config.get("schema_version", 1)) is not int or config.get("schema_version", 1) != 1:
        raise ValueError("schema_version: must be 1")
    if not isinstance(config.get("dataset"), str) or not config["dataset"]:
        raise ValueError("dataset: requires a JSONL file path")
    config["dataset"] = str((path.parent / config["dataset"]).resolve())
    versions = config.get("versions")
    if (not isinstance(versions, list) or not versions or
            any(not isinstance(v, str) or not v.strip() for v in versions) or
            len(set(versions)) != len(versions)):
        raise ValueError("versions: requires unique nonempty version names")
    execution = config.setdefault("execution", {})
    if not isinstance(execution, dict) or set(execution) - {"trials", "seed", "timeout_seconds", "workers"}:
        raise ValueError("execution: unsupported fields")
    for key, default in {"trials": 1, "seed": 0, "timeout_seconds": 30, "workers": 1}.items():
        execution.setdefault(key, default)
        _numeric(execution[key], "execution." + key, 0 if key == "seed" else 1,
                 64 if key == "workers" else None, integer=key != "timeout_seconds")
    config.setdefault("adapter", {})
    overrides = config.setdefault("version_adapters", {})
    if not isinstance(overrides, dict) or set(overrides) - set(versions):
        raise ValueError("version_adapters: keys must appear in versions")
    for version in versions:
        adapter = overrides.get(version, config["adapter"])
        if not isinstance(adapter, dict) or adapter.get("kind") not in ("python", "http", "command"):
            raise ValueError(f"adapter for {version}: kind must be python, http, or command")
        required = {"python": "callable", "http": "url", "command": "argv"}[adapter["kind"]]
        if not adapter.get(required):
            raise ValueError(f"adapter for {version}: missing {required}")
        if adapter["kind"] == "command":
            if not isinstance(adapter["argv"], list) or any(not isinstance(a, str) or not a for a in adapter["argv"]):
                raise ValueError(f"adapter for {version}: argv must be an array of strings")
        elif not isinstance(adapter[required], str):
            raise ValueError(f"adapter for {version}: {required} must be a string")
        if any(key in adapter for key in ("api_key", "token", "password")):
            raise ValueError("adapter: use api_key_env for credentials")
    grader = config.setdefault("grader", {"kind": "deterministic"})
    validate_grader(grader)
    gate = config.setdefault("gate", {"enabled": False})
    allowed = {"enabled", "baseline", "candidate", "min_families", "margin", "alpha", "min_success_rate"}
    if not isinstance(gate, dict) or set(gate) - allowed or type(gate.get("enabled", False)) is not bool:
        raise ValueError("gate: invalid fields")
    if gate.get("enabled"):
        if gate.get("baseline") not in versions or gate.get("candidate") not in versions or gate["baseline"] == gate["candidate"]:
            raise ValueError("gate: requires two distinct configured versions")
        defaults = {"min_families": 30, "margin": 0.05, "alpha": 0.05, "min_success_rate": 0.8}
        for key, value in defaults.items():
            gate.setdefault(key, value)
            _numeric(gate[key], "gate." + key, 1 if key == "min_families" else 0,
                     None if key == "min_families" else 1, integer=key == "min_families")
        if not 0 < gate["alpha"] < 1:
            raise ValueError("gate.alpha: must be between 0 and 1")
    if not isinstance(config.setdefault("provenance", {}), dict):
        raise ValueError("provenance: must be an object")
    config["_directory"] = str(path.parent)
    config["_path"] = str(path)
    return config


def adapter_for(config, version):
    adapter = copy.deepcopy(config["version_adapters"].get(version, config["adapter"]))
    adapter["working_directory"] = config["_directory"]
    return adapter
