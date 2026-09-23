"""Strict, bounded JSON-lines RPC. No eval, inference, keys, or browser connection."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import BinaryIO, TextIO

from .guarded import GuardedController, GuardedError, fields, integer

MAX_LINE_BYTES = 128 * 1024  # Includes terminating newline, in both directions.


def _pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str):
    raise ValueError("Non-finite JSON number")


def dispatch(controller: GuardedController, request: dict) -> dict:
    fields(request, {"id", "method", "args"})
    integer(request["id"], -(2**53 - 1), 2**53 - 1)
    method = request["method"]
    if not isinstance(method, str) or method not in {"observe", "act", "waitForChange"}:
        raise GuardedError("unknown_method", "Unknown RPC method")
    required, optional = {
        "observe": ({"scope"}, {"maxElements"}),
        "act": ({"scope", "observationId", "action"}, set()),
        "waitForChange": ({"scope", "revision"}, {"timeoutMs"}),
    }[method]
    args = fields(request["args"], required, optional)
    return getattr(controller, method)(**args)


def serve(
    controller: GuardedController,
    stdin: BinaryIO,
    stdout: BinaryIO,
    stderr: TextIO | None = None,
) -> None:
    """Run sequentially until EOF. Invalid/unrecoverable IDs are reported as null.

    Oversized lines are drained in bounded chunks before reading the next frame.
    Do not share the controller/backend with concurrent raw users.
    """
    stderr = stderr or sys.stderr
    while True:
        line = stdin.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        request_id = None
        try:
            if len(line) > MAX_LINE_BYTES:
                while not line.endswith(b"\n"):
                    line = stdin.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                raise GuardedError("line_too_large", "JSON line exceeds 128 KiB")
            if not line.endswith(b"\n"):
                raise GuardedError(
                    "invalid_request", "JSON lines must end with newline"
                )
            try:
                request = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=_pairs,
                    parse_constant=_constant,
                )
            except (ValueError, UnicodeError, RecursionError):
                raise GuardedError("invalid_json", "Invalid JSON frame") from None
            if (
                isinstance(request, dict)
                and type(request.get("id")) is int
                and abs(request["id"]) < 2**53
            ):
                request_id = request["id"]
            with contextlib.redirect_stdout(stderr):
                result = dispatch(controller, request)
            response = {"id": request_id, "result": result}
        except GuardedError as exc:
            response = {
                "id": request_id,
                "error": {"code": exc.code, "message": str(exc)},
            }
        except Exception:  # noqa: BLE001 - fail closed at the native/RPC trust boundary
            # Never echo native exception strings: they may contain private UI text.
            print("macos-harness: guarded request failed", file=stderr)
            response = {
                "id": request_id,
                "error": {
                    "code": "backend_error",
                    "message": "Unable to complete guarded request",
                },
            }
        encoded = (
            json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_LINE_BYTES:
            encoded = (
                json.dumps(
                    {
                        "id": request_id,
                        "error": {
                            "code": "response_too_large",
                            "message": "Bounded response exceeded frame limit",
                        },
                    }
                )
                + "\n"
            ).encode()
        stdout.write(encoded)
        stdout.flush()


def serve_cli(allowed_apps: list[str]) -> int:
    """Reserve stdout's fd for framing, including against native library prints."""
    from .guarded_ax import NativeAXBackend
    from .macos import MacOS

    sys.stdout.flush()
    wire_fd = os.dup(sys.stdout.fileno())
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        with os.fdopen(os.dup(wire_fd), "wb") as wire:
            controller = GuardedController(NativeAXBackend(MacOS()), allowed_apps)
            serve(controller, sys.stdin.buffer, wire)
        return 0
    except BrokenPipeError:
        return 0
    except Exception:  # noqa: BLE001 - fail closed at the native/RPC trust boundary
        print("macos-harness: unable to start guarded server", file=sys.stderr)
        return 1
    finally:
        sys.stdout.flush()
        os.dup2(wire_fd, sys.stdout.fileno())
        os.close(wire_fd)
