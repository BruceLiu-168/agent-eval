"""Internal one-invocation worker. It is not an OS security sandbox."""

import argparse
import contextlib
import importlib
import json
import os
import sys

from .adapters import (AdapterFailure, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
                       _http_request, decode_json, encode_request)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--callable", dest="target")
    modes.add_argument("--http", action="store_true")
    args = parser.parse_args(argv)
    protocol_fd = os.dup(sys.stdout.fileno())
    # Capture both Python print() and native writes to stdout. A Python-level
    # redirect alone does not catch os.write(1, ...) or library subprocess logs.
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        with contextlib.redirect_stdout(sys.stderr):
            payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(payload) > MAX_REQUEST_BYTES:
                raise AdapterFailure("adapter_contract")
            request = decode_json(payload)
            if args.http:
                result = _http_request(request["config"], encode_request(request["request"]),
                                       request["timeout"])
            else:
                module, name = args.target.split(":", 1)
                function = importlib.import_module(module)
                for component in name.split("."):
                    function = getattr(function, component)
                result = function(request["case"], request["version"], request["seed"])
                try:
                    result = json.dumps(result, ensure_ascii=False, allow_nan=False,
                                        separators=(",", ":")).encode("utf-8")
                except (TypeError, ValueError, RecursionError):
                    raise AdapterFailure("adapter_contract") from None
            if len(result) > MAX_RESPONSE_BYTES:
                raise AdapterFailure("adapter_contract")
        offset = 0
        while offset < len(result):
            offset += os.write(protocol_fd, result[offset:])
        return 0
    except AdapterFailure as exc:
        return {"adapter_contract": 64, "transport": 69, "timeout": 75}[exc.error_type]
    except (ValueError, TypeError, KeyError):
        return 64
    except Exception:
        # Exception text can contain request data or credentials; parent receives
        # only a fixed exit classification.
        return 69
    finally:
        os.close(protocol_fd)


if __name__ == "__main__":
    raise SystemExit(main())
