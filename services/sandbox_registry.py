from __future__ import annotations

import logging
import threading
from copy import deepcopy
from typing import Any, Dict, List

from services.public_url_service import public_url_for


logger = logging.getLogger(__name__)

_lock = threading.RLock()
_modules: Dict[str, Dict[str, Any]] = {}


def register_sandbox_module(descriptor: Dict[str, Any]) -> None:
    module_id = str(descriptor.get("id") or "").strip()
    if not module_id:
        raise ValueError("Sandbox module descriptor must contain id")
    with _lock:
        _modules[module_id] = deepcopy(descriptor)


def clear_sandbox_modules() -> None:
    with _lock:
        _modules.clear()


def get_sandbox_modules(*, loaded_only: bool = False) -> List[Dict[str, Any]]:
    with _lock:
        modules = [deepcopy(module) for module in _modules.values()]

    result = []
    for module in modules:
        if loaded_only and not module.get("loaded"):
            continue
        endpoint = str(module.get("endpoint") or "").strip()
        module["url"] = None
        if module.get("loaded") and endpoint:
            try:
                module["url"] = public_url_for(endpoint)
            except Exception as exc:
                logger.warning(
                    "Sandbox module URL is unavailable for %s: %s",
                    module.get("id"),
                    type(exc).__name__,
                )
        result.append(module)

    return sorted(
        result,
        key=lambda item: (
            str(item.get("owner_code") or ""),
            str(item.get("title") or ""),
        ),
    )


def has_loaded_sandbox_modules() -> bool:
    return any(module.get("loaded") for module in get_sandbox_modules(loaded_only=True))
