from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class RuntimeStateError(RuntimeError):
    """A safe, non-sensitive runtime state failure."""


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the adjacent temporary name deliberately short: history paths contain
    # two opaque IDs and can otherwise approach the legacy Windows path limit.
    fd, temporary_name = tempfile.mkstemp(prefix=".t-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def read_json(path: Path, *, missing: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeStateError("Runtime state is unavailable.") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
