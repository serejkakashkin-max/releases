from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List


MANIFEST_RELATIVE_PATH = Path("vendor") / "manifest.json"
_CACHE: "OrderedDict[str, tuple[float, tuple, Dict[str, object]]]" = OrderedDict()
_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ROOTS = 8


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_with_checkout_normalization(path: Path) -> set[str]:
    payload = path.read_bytes()
    hashes = {hashlib.sha256(payload).hexdigest()}
    # Git for Windows may materialize text bundles with CRLF even though the
    # pinned upstream artifact and manifest use LF. Only that exact reversible
    # newline transformation is accepted; all other byte changes still fail.
    if b"\r\n" in payload:
        hashes.add(hashlib.sha256(payload.replace(b"\r\n", b"\n")).hexdigest())
    return hashes


def _signature(static_root: Path) -> tuple:
    manifest = static_root / MANIFEST_RELATIVE_PATH
    try:
        manifest_stat = manifest.stat()
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        values = [(str(MANIFEST_RELATIVE_PATH), manifest_stat.st_size, manifest_stat.st_mtime_ns)]
        for asset in payload.get("assets", []):
            relative = str(asset.get("path") or "")
            path = static_root / relative
            try:
                item = path.stat(); values.append((relative, item.st_size, item.st_mtime_ns))
            except OSError:
                values.append((relative, -1, -1))
        return tuple(values)
    except Exception:
        return (("manifest", -1, -1),)


def verify_vendor_assets(static_root: Path) -> Dict[str, object]:
    static_root = Path(static_root).resolve()
    cache_key = str(static_root)
    signature = _signature(static_root)
    cached = _CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] <= _CACHE_TTL_SECONDS and cached[1] == signature:
        _CACHE.move_to_end(cache_key)
        return {"ok": cached[2]["ok"], "problems": list(cached[2]["problems"])}
    manifest_path = static_root / MANIFEST_RELATIVE_PATH
    problems: List[str] = []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ok": False, "problems": ["vendor manifest"]}

    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        return {"ok": False, "problems": ["vendor manifest"]}

    for asset in assets:
        if not isinstance(asset, dict):
            problems.append("unknown vendor asset")
            continue
        name = str(asset.get("name") or "vendor asset")
        relative_value = str(asset.get("path") or "")
        expected_hash = str(asset.get("sha256") or "").lower()
        if asset.get("status") != "ready" or not relative_value or len(expected_hash) != 64:
            problems.append(name)
            continue
        candidate = (static_root / relative_value).resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError:
            problems.append(name)
            continue
        try:
            if not candidate.is_file() or expected_hash not in _sha256_with_checkout_normalization(candidate):
                problems.append(name)
        except OSError:
            problems.append(name)

    result = {"ok": not problems, "problems": list(dict.fromkeys(problems))}
    _CACHE[cache_key] = (time.monotonic(), signature, result)
    _CACHE.move_to_end(cache_key)
    while len(_CACHE) > _CACHE_MAX_ROOTS:
        _CACHE.popitem(last=False)
    return {"ok": result["ok"], "problems": list(result["problems"])}
