"""Bounded adapters for trusted local Python, commands, and HTTP agents.

Every call has a total wall-clock timeout and performs no retries. Processes
are lifecycle isolation, NOT an operating-system security sandbox: local
adapters inherit the caller's filesystem and environment permissions. Only run
trusted code. HTTP requests also run in a worker so slow response streams cannot
evade the overall timeout. Error reports never include stderr or response bodies.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .contracts import public_case, validate_episode


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_WORKER_EXIT_CODES = {64: "adapter_contract", 69: "transport", 75: "timeout"}
_ERROR_MESSAGES = {
    "timeout": "Adapter execution exceeded its timeout; no automatic retry was performed.",
    "transport": "Adapter transport or process execution failed; sensitive diagnostics were omitted.",
    "adapter_contract": "Adapter configuration, request, or response violated the JSON contract.",
}


class AdapterFailure(Exception):
    """A fixed classification, never an arbitrary exception string."""

    def __init__(self, error_type):
        self.error_type = error_type
        super().__init__(error_type)


def _reject_constant(_value):
    raise ValueError("JSON numbers must be finite")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def decode_json(payload):
    return json.loads(payload.decode("utf-8"), parse_constant=_reject_constant,
                      object_pairs_hook=_unique_object)


def encode_request(value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise AdapterFailure("adapter_contract")
    return payload


def _stop_process(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _run_process(argv, payload, timeout, cwd, *, worker=False):
    """Read bounded stdout while discarding diagnostics; never invoke a shell."""
    environment = os.environ.copy()
    paths = [str(cwd), str(Path(__file__).resolve().parents[1])]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    output = bytearray()
    overflow = threading.Event()
    read_error = threading.Event()
    with tempfile.TemporaryFile() as request_file:
        request_file.write(payload)
        request_file.seek(0)
        process = subprocess.Popen(
            argv, stdin=request_file, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=cwd, env=environment,
            shell=False, start_new_session=(os.name == "posix"), bufsize=0,
        )

        def read_output():
            try:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        return
                    remaining = MAX_RESPONSE_BYTES + 1 - len(output)
                    output.extend(chunk[:remaining])
                    if len(output) > MAX_RESPONSE_BYTES:
                        overflow.set()
                        _stop_process(process)
                        return
            except OSError:
                read_error.set()
            finally:
                process.stdout.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            reader.join(timeout=max(0, deadline - time.monotonic()))
            if reader.is_alive():
                timed_out = True
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # End the invocation's process group, including children that might
            # retain pipe descriptors after the direct command has exited.
            _stop_process(process)
            process.wait(timeout=1)
            reader.join(timeout=1)
        if timed_out:
            raise AdapterFailure("timeout")
        if overflow.is_set():
            raise AdapterFailure("adapter_contract")
        if read_error.is_set() or process.returncode != 0:
            classification = (_WORKER_EXIT_CODES.get(process.returncode, "transport")
                              if worker else "transport")
            raise AdapterFailure(classification)
    return bytes(output)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_request(config, payload, timeout):
    """Worker-only HTTP transport; the parent enforces the total deadline."""
    url = config.get("url")
    if not isinstance(url, str):
        raise AdapterFailure("adapter_contract")
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.fragment):
        raise AdapterFailure("adapter_contract")
    headers = config.get("headers", {})
    if not isinstance(headers, dict):
        raise AdapterFailure("adapter_contract")
    safe_headers = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AdapterFailure("adapter_contract")
        lowered = key.lower()
        if (any(word in lowered for word in ("authorization", "cookie", "api-key",
                                             "api_key", "token", "secret"))
                or lowered in {"host", "content-length", "transfer-encoding", "connection"}
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or "\r" in value or "\n" in value):
            raise AdapterFailure("adapter_contract")
        safe_headers[key] = value
    safe_headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    if "api_key_env" in config:
        variable = config["api_key_env"]
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise AdapterFailure("adapter_contract")
        token = os.environ.get(variable)
        if not token or "\r" in token or "\n" in token:
            raise AdapterFailure("adapter_contract")
        safe_headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=payload, headers=safe_headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise AdapterFailure("transport")
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None and not 0 <= int(declared_size) <= MAX_RESPONSE_BYTES:
                raise AdapterFailure("adapter_contract")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AdapterFailure("adapter_contract")
            return body
    except (socket.timeout, TimeoutError):
        raise AdapterFailure("timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AdapterFailure("timeout") from None
        raise AdapterFailure("transport") from None


def _error_episode(case, version, seed, error_type):
    case_id = case.get("case_id")
    case_id = case_id if isinstance(case_id, str) and case_id.strip() else "invalid-case"
    version = version if isinstance(version, str) and version.strip() else "unknown-version"
    business = case.get("business")
    business = business if isinstance(business, str) and business.strip() else "unknown"
    seed = seed if type(seed) is int else 0
    return {
        "episode_id": f"{version}:{case_id}:{seed}",
        "case_id": case_id, "agent_version": version,
        "business": business, "seed": seed,
        "status": "infra_error", "events": [], "final_state": {},
        "cost": None, "latency_ms": None, "error_type": error_type,
        "error_message": _ERROR_MESSAGES[error_type],
    }


def execute_adapter(config: dict, case: dict, version: str, seed: int,
                    timeout: float = 30) -> dict:
    """Execute once; return a validated episode or a sanitized infra_error.

    Supported configs: ``python`` + ``callable=module:function``; ``command`` +
    ``argv``; ``http`` + ``url`` and optional ``api_key_env``/non-secret ``headers``.
    ``working_directory`` selects the local import/process directory. Cost and
    latency reported by the agent are preserved; the runner's observed elapsed
    time is always separately recorded as ``execution_latency_ms``.
    """
    started = time.monotonic()
    try:
        if (not isinstance(config, dict) or not isinstance(case, dict)
                or not isinstance(version, str) or not version
                or type(seed) is not int or type(timeout) not in (float, int)
                or not math.isfinite(timeout) or timeout <= 0):
            raise AdapterFailure("adapter_contract")
        request = {"case": public_case(case), "version": version, "seed": seed}
        payload = encode_request(request)
        cwd = config.get("working_directory", os.getcwd())
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise AdapterFailure("adapter_contract")
        kind = config.get("kind")
        if kind == "python":
            target = config.get("callable")
            if (not isinstance(target, str)
                    or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", target)):
                raise AdapterFailure("adapter_contract")
            raw = _run_process([sys.executable, "-m", "agent_eval.worker", "--callable", target],
                               payload, timeout, cwd, worker=True)
        elif kind == "command":
            argv = config.get("argv")
            if (not isinstance(argv, list) or not argv
                    or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)):
                raise AdapterFailure("adapter_contract")
            raw = _run_process(argv, payload, timeout, cwd)
        elif kind == "http":
            wrapped = encode_request({"config": config, "request": request, "timeout": timeout})
            raw = _run_process([sys.executable, "-m", "agent_eval.worker", "--http"],
                               wrapped, timeout, cwd, worker=True)
        else:
            raise AdapterFailure("adapter_contract")
        episode = validate_episode(decode_json(raw), case_id=case["case_id"], version=version)
        if "business" in episode and episode["business"] != case.get("business"):
            raise AdapterFailure("adapter_contract")
        if "seed" in episode and (type(episode["seed"]) is not int or episode["seed"] != seed):
            raise AdapterFailure("adapter_contract")
        episode.setdefault("business", case.get("business", "unknown"))
        episode.setdefault("seed", seed)
    except AdapterFailure as exc:
        episode = _error_episode(case if isinstance(case, dict) else {}, version, seed, exc.error_type)
    except (ValueError, TypeError, KeyError, RecursionError):
        episode = _error_episode(case if isinstance(case, dict) else {}, version, seed, "adapter_contract")
    except (OSError, subprocess.SubprocessError):
        episode = _error_episode(case if isinstance(case, dict) else {}, version, seed, "transport")
    episode["execution_latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    provenance = episode.get("metric_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    for metric in ("cost", "latency_ms"):
        if episode.get(metric) is None:
            provenance[metric] = "unknown"
        else:
            provenance.setdefault(metric, "self_reported")
    provenance["execution_latency_ms"] = "measured"
    episode["metric_provenance"] = provenance
    return episode
