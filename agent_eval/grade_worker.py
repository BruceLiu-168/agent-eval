"""Internal JSON grader worker; stdout is reserved for one complete grade."""

import contextlib
import json
import os
import sys

from .adapters import MAX_RESPONSE_BYTES, decode_json
from .grade_runner import MAX_GRADE_REQUEST_BYTES
from .grading import evaluate


def main():
    protocol_fd = os.dup(sys.stdout.fileno())
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        with contextlib.redirect_stdout(sys.stderr):
            payload = sys.stdin.buffer.read(MAX_GRADE_REQUEST_BYTES + 1)
            if len(payload) > MAX_GRADE_REQUEST_BYTES:
                return 64
            request = decode_json(payload)
            result = evaluate(request["case"], request["episode"], request.get("grader_config"))
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_RESPONSE_BYTES:
                return 64
        offset = 0
        while offset < len(encoded):
            offset += os.write(protocol_fd, encoded[offset:])
        return 0
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return 64
    except Exception:
        return 69
    finally:
        os.close(protocol_fd)


if __name__ == "__main__":
    raise SystemExit(main())
