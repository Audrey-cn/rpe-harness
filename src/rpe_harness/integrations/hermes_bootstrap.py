"""Launch installed Hermes with an in-process, accounting-neutral usage observer.

This file is executable directly by Hermes's Python runtime and uses only the
standard library until the installed Hermes bootstrap activates its dependencies.
It never serializes prompts, response content, credentials, or unknown usage fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any


_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)
_DETAIL_FIELDS = {
    "prompt_tokens_details": ("cached_tokens", "audio_tokens", "text_tokens"),
    "input_tokens_details": ("cached_tokens", "audio_tokens", "text_tokens"),
    "completion_tokens_details": (
        "reasoning_tokens",
        "audio_tokens",
        "text_tokens",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
    ),
    "output_tokens_details": ("reasoning_tokens", "audio_tokens", "text_tokens"),
    "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
}
_MISSING = object()
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}\Z")


def _present_value(value: Any, name: str) -> Any:
    """Read only fields the SDK says were supplied, rather than its default values."""
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    if value is None:
        return _MISSING
    try:
        fields_set = getattr(value, "model_fields_set", _MISSING)
        if fields_set is _MISSING:
            fields_set = getattr(value, "__fields_set__", _MISSING)
        if fields_set is not _MISSING and name not in fields_set:
            return _MISSING
        return getattr(value, name, _MISSING)
    except Exception:
        return _MISSING


def _is_counter(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and (not isinstance(value, float) or math.isfinite(value))
    )


def extract_provider_usage(usage: Any) -> tuple[dict[str, Any], list[str]]:
    """Copy allowlisted numeric usage leaves, recording their original dotted paths.

    Explicit numeric zero is retained. Missing fields, None, SDK defaults excluded
    by ``model_fields_set``, and values requiring coercion are never filled with zero.
    """
    raw: dict[str, Any] = {}
    present: list[str] = []
    for name in _TOKEN_FIELDS:
        value = _present_value(usage, name)
        if _is_counter(value):
            raw[name] = value
            present.append(name)
    for container, names in _DETAIL_FIELDS.items():
        nested = _present_value(usage, container)
        details: dict[str, Any] = {}
        for name in names:
            value = _present_value(nested, name)
            if _is_counter(value):
                details[name] = value
                present.append(f"{container}.{name}")
        if details:
            raw[container] = details
    return raw, sorted(present)


def augment_usage_summary(
    summary: dict[str, Any] | None, response: Any
) -> dict[str, Any] | None:
    """Append telemetry without replacing or mutating Hermes accounting fields."""
    if response is None:
        return summary
    raw, present = extract_provider_usage(_present_value(response, "usage"))
    identity = {}
    for name in ("id", "model", "provider"):
        value = _present_value(response, name)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            identity[name] = value
    return {
        **(summary or {}),
        "raw_provider_usage": raw,
        "present_fields": present,
        "response_identity": identity,
    }


def install_usage_telemetry(mixin_type: type | None = None) -> None:
    """Observe hook summaries only; do not patch Hermes usage normalization or files."""
    if mixin_type is None:
        mixin_type = importlib.import_module("agent.api_request_hooks").ApiRequestHooksMixin
    original = mixin_type._usage_summary_for_api_request_hook
    if getattr(original, "_rpe_usage_telemetry", False):
        return

    @wraps(original)
    def observed(self: Any, response: Any) -> dict[str, Any] | None:
        summary = original(self, response)
        try:
            return augment_usage_summary(summary, response)
        except Exception:
            # A telemetry failure must not alter model execution or original accounting.
            return summary

    observed._rpe_usage_telemetry = True
    mixin_type._usage_summary_for_api_request_hook = observed


def install_process_telemetry() -> None:
    """Synchronously journal direct children of this isolated Python process.

    The patch stays in this process; shell forks and externally executed Python
    programs still require the launcher's descendant tracking. No argv or
    environment contents are persisted, and Popen remains the original class.
    """
    path = os.environ.get("RPE_HERMES_PROCESS_PATH")
    if not path:
        return
    if not Path(path).is_absolute():
        raise RuntimeError("isolated child process registry must be an absolute path")
    original = subprocess.Popen.__init__
    if getattr(original, "_rpe_process_telemetry", False):
        return

    def start_identity(pid: int) -> dict[str, Any] | None:
        if sys.platform == "darwin":
            import ctypes

            # Apple's SDK sys/proc_info.h, struct proc_bsdinfo (136 bytes on
            # supported 64-bit macOS). libproc is callable inside the sandbox;
            # executing the setuid /bin/ps binary is not.
            class BsdInfo(ctypes.Structure):
                _fields_ = (
                    [(name, ctypes.c_uint32) for name in
                     ("flags", "status", "xstatus", "pid", "ppid", "uid", "gid",
                      "ruid", "rgid", "svuid", "svgid", "reserved")]
                    + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
                    + [(name, ctypes.c_uint32) for name in
                       ("nfiles", "pgid", "pjobc", "tdev", "tpgid")]
                    + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                       ("start_usec", ctypes.c_uint64)]
                )

            query = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            info = BsdInfo()
            size = ctypes.sizeof(info)
            if query(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid:
                return None
            if not info.start_sec or info.start_usec >= 1_000_000:
                return None
            local = time.localtime(info.start_sec)
            weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.tm_wday]
            month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                     "Oct", "Nov", "Dec")[local.tm_mon - 1]
            identity = (f"{weekday} {month} {local.tm_mday} "
                        f"{local.tm_hour:02}:{local.tm_min:02}:{local.tm_sec:02} {local.tm_year}")
            return {"lstart": identity, "start_sec": info.start_sec, "start_usec": info.start_usec}

        # Keep the portable unsandboxed probe nonrecursive as well.
        probe = subprocess.Popen.__new__(subprocess.Popen)
        original(probe, ["/bin/ps", "-p", str(pid), "-o", "lstart="],
                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
        try:
            output, _ = probe.communicate(timeout=3)
        finally:
            if probe.poll() is None:
                probe.kill()
            probe.wait()
        identity = " ".join(output.split())
        return {"lstart": identity} if not probe.returncode and identity else None

    @wraps(original)
    def observed(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if self.poll() is not None:
            return
        try:
            identity = start_identity(self.pid)
            if identity is None:
                if self.poll() is not None:
                    return
                raise OSError("process start identity is unavailable")
            record = {"pid": self.pid, "parent_pid": os.getpid(), **identity}
            data = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("process registry is not a regular file")
                os.fchmod(fd, 0o600)
                if os.write(fd, data) != len(data):
                    raise OSError("incomplete process registry append")
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            # Popen owns this exact child, so it can safely reap it without
            # guessing a process group or acting on an unrelated recycled PID.
            try:
                self.kill()
                self.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise RuntimeError("cannot persist isolated child process identity") from None

    observed._rpe_process_telemetry = True
    subprocess.Popen.__init__ = observed


def install_producer_toolset() -> None:
    """Use installed Hermes's runtime toolset API only in an opted-in producer process."""
    if os.environ.get('RPE_HERMES_ENTRY_MODE') == 'producer':
        importlib.import_module('toolsets').create_custom_toolset(
            'rpe_producer_only', 'Bounded RPE candidate generation', tools=['write_file'], includes=[])


def main(argv: list[str] | None = None) -> Any:
    """Dispatch unchanged CLI arguments after activating and observing installed Hermes."""
    install_root = os.environ.get("RPE_HERMES_INSTALL_ROOT", "")
    root = Path(install_root).resolve() if install_root else None
    if root is None or not (root / "hermes_cli/main.py").is_file():
        raise SystemExit("RPE_HERMES_INSTALL_ROOT must name an installed Hermes source directory")
    sys.path.insert(0, str(root))
    if argv is not None:
        sys.argv = [sys.argv[0], *argv]
    install_process_telemetry()
    importlib.import_module("hermes_bootstrap")
    install_producer_toolset()
    install_usage_telemetry()
    return importlib.import_module("hermes_cli.main").main()


if __name__ == "__main__":
    raise SystemExit(main())
