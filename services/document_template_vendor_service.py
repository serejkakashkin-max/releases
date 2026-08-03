from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List


MANIFEST_RELATIVE_PATH = Path("vendor") / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_vendor_assets(static_root: Path) -> Dict[str, object]:
    static_root = Path(static_root).resolve()
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
            if not candidate.is_file() or _sha256(candidate) != expected_hash:
                problems.append(name)
        except OSError:
            problems.append(name)

    return {"ok": not problems, "problems": list(dict.fromkeys(problems))}
