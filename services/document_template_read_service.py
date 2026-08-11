from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import sys
import threading
import unicodedata
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional

from services.release_template_catalog_service import build_runtime_template_catalog


DOCUMENT_ID_RE = re.compile(r"^dt1_[0-9a-f]{64}$")
DEFAULT_PAGE_SIZE = 12
RECENT_DOCUMENT_DAYS = 3
CATALOG_CHANGE_WINDOWS = (3, 7, 30)
_READ_CACHE: "OrderedDict[str, tuple[float, tuple, Dict[str, ResolvedDocument]]]" = OrderedDict()
_PATH_CACHE: "OrderedDict[str, tuple[float, Dict[str, str]]]" = OrderedDict()
_CACHE_LOCK = threading.RLock()
_READ_CACHE_TTL_SECONDS = 60
_READ_CACHE_MAX_ROOTS = 4


def clear_document_template_read_cache(root: Path | None = None) -> None:
    """Public invalidation hook used after an atomic template mutation.

    The current whitelist is intentionally rebuilt for every access so a deleted or
    replaced file can never be served from a stale path. The hook is kept explicit
    for callers and for a future metadata-only snapshot cache.
    """
    if root is None:
        with _CACHE_LOCK:
            _READ_CACHE.clear()
            _PATH_CACHE.clear()
        return
    try:
        key = str(Path(root).absolute())
    except (OSError, RuntimeError):
        return
    with _CACHE_LOCK:
        _READ_CACHE.pop(key, None)
        _PATH_CACHE.pop(key, None)


class DocumentTemplateRootUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedDocument:
    document_id: str
    path: Path
    relative_path: str
    relative_directory: str
    filename: str
    size: int
    modified_ns: int
    sha256: str
    template_root: Path

    def as_view_model(self) -> Dict[str, object]:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "size": self.size,
            "size_display": _format_size(self.size),
            "modified_at": _format_timestamp(self.modified_ns),
            "sha256_short": self.sha256[:12],
            "sha256": self.sha256,
        }


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _contains_symlink(root: Path, candidate: Path, *, lstat=os.lstat) -> bool:
    try:
        if stat.S_ISLNK(lstat(root).st_mode):
            return True
    except OSError:
        return True
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        try:
            is_link = stat.S_ISLNK(lstat(current).st_mode)
        except OSError:
            return True
        if is_link:
            return True
    return False


def _normalize_relative_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or "").replace("\\", "/"))
    if re.match(r"^[a-zA-Z]:", normalized) or normalized.startswith("//"):
        raise ValueError("Unsafe relative path")
    parsed = PurePosixPath(normalized)
    if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("Unsafe relative path")
    normalized = parsed.as_posix()
    if sys.platform.startswith("win"):
        normalized = normalized.casefold()
    return normalized


def document_id_for_relative_path(relative_path: str) -> str:
    normalized = _normalize_relative_path(relative_path)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"dt1_{digest}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _root_paths(root: Path) -> tuple[Path, Path]:
    lexical_root = Path(root).absolute()
    if lexical_root.is_symlink():
        raise DocumentTemplateRootUnavailable("Template root must not be a symlink")
    try:
        resolved_root = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DocumentTemplateRootUnavailable("Template root is unavailable") from exc
    if not resolved_root.is_dir():
        raise DocumentTemplateRootUnavailable("Template root is unavailable")
    return lexical_root, resolved_root


def _root_signature(root: Path) -> tuple:
    values = []
    try:
        for candidate in root.rglob("*"):
            if candidate.suffix.casefold() != ".docx":
                continue
            try:
                item = candidate.stat(follow_symlinks=False)
                values.append((candidate.relative_to(root).as_posix(), item.st_size, item.st_mtime_ns, item.st_mode))
            except (OSError, ValueError):
                values.append((str(candidate), -1, -1, -1))
    except OSError:
        return (("unavailable",),)
    return tuple(sorted(values))


def _build_path_mapping(root: Path, *, fresh: bool = False) -> Dict[str, str]:
    lexical_root, resolved_root = _root_paths(root)
    cache_key = str(lexical_root)
    with _CACHE_LOCK:
        cached = _PATH_CACHE.get(cache_key)
        if not fresh and cached and time.monotonic() - cached[0] <= _READ_CACHE_TTL_SECONDS:
            _PATH_CACHE.move_to_end(cache_key)
            return dict(cached[1])
    result: Dict[str, str] = {}
    try:
        for candidate in lexical_root.rglob("*"):
            if candidate.suffix.casefold() != ".docx" or _contains_symlink(lexical_root, candidate):
                continue
            try:
                item_stat = candidate.stat(follow_symlinks=False)
                resolved = candidate.resolve(strict=True)
                relative = candidate.relative_to(lexical_root).as_posix()
                document_id = document_id_for_relative_path(relative)
            except (OSError, RuntimeError, ValueError):
                continue
            if stat.S_ISREG(item_stat.st_mode) and _path_is_within(resolved, resolved_root) and resolved.suffix.casefold() == ".docx":
                result.setdefault(document_id, relative)
    except OSError as exc:
        raise DocumentTemplateRootUnavailable("Template root cannot be read") from exc
    with _CACHE_LOCK:
        _PATH_CACHE[cache_key] = (time.monotonic(), dict(result))
        _PATH_CACHE.move_to_end(cache_key)
        while len(_PATH_CACHE) > _READ_CACHE_MAX_ROOTS:
            _PATH_CACHE.popitem(last=False)
    return result


def _resolve_relative_target(root: Path, relative: str) -> tuple[Path, Path]:
    lexical_root, resolved_root = _root_paths(root)
    normalized = _normalize_relative_path(relative)
    candidate = lexical_root.joinpath(*PurePosixPath(relative).parts)
    if document_id_for_relative_path(relative) != document_id_for_relative_path(normalized):
        raise ValueError("Unsafe relative path")
    if _contains_symlink(lexical_root, candidate):
        raise OSError("Template target is unsafe")
    item_stat = candidate.stat(follow_symlinks=False)
    resolved = candidate.resolve(strict=True)
    if not stat.S_ISREG(item_stat.st_mode) or not _path_is_within(resolved, resolved_root) or resolved.suffix.casefold() != ".docx":
        raise OSError("Template target is unavailable")
    return lexical_root, resolved


def read_document_payload(document_id: str, root: Path) -> tuple[ResolvedDocument, bytes] | None:
    if not DOCUMENT_ID_RE.fullmatch(str(document_id or "")):
        return None
    mapping = _build_path_mapping(root)
    relative = mapping.get(document_id)
    if relative is None:
        relative = _build_path_mapping(root, fresh=True).get(document_id)
    if relative is None:
        return None
    try:
        lexical_root, target = _resolve_relative_target(root, relative)
        digest = hashlib.sha256()
        payload = bytearray()
        with target.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                payload.extend(chunk)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        current = target.stat(follow_symlinks=False)
        if _stable_stat_key(before) != _stable_stat_key(after) or _stable_stat_key(after) != _stable_stat_key(current):
            return None
        if _contains_symlink(lexical_root, lexical_root.joinpath(*PurePosixPath(relative).parts)):
            return None
        if target.resolve(strict=True) != target:
            return None
        normalized_directory = _normalize_relative_path(str(PurePosixPath(relative).parent))
        document = ResolvedDocument(
            document_id=document_id,
            path=target,
            relative_path=relative,
            relative_directory=normalized_directory,
            filename=PurePosixPath(relative).name,
            size=after.st_size,
            modified_ns=after.st_mtime_ns,
            sha256=digest.hexdigest(),
            template_root=lexical_root,
        )
        return document, bytes(payload)
    except (OSError, RuntimeError, ValueError):
        return None


def build_document_whitelist(root: Path, *, fresh: bool = False) -> Dict[str, ResolvedDocument]:
    lexical_root, resolved_root = _root_paths(root)
    cache_key = str(lexical_root)
    signature = _root_signature(lexical_root)
    with _CACHE_LOCK:
        cached = _READ_CACHE.get(cache_key)
        if not fresh and cached and time.monotonic() - cached[0] <= _READ_CACHE_TTL_SECONDS and cached[1] == signature:
            _READ_CACHE.move_to_end(cache_key)
            return dict(cached[2])
    result: Dict[str, ResolvedDocument] = {}

    try:
        candidates: Iterable[Path] = lexical_root.rglob("*")
        for candidate in candidates:
            if candidate.suffix.casefold() != ".docx":
                continue
            if _contains_symlink(lexical_root, candidate):
                continue
            try:
                item_stat = candidate.stat(follow_symlinks=False)
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not stat.S_ISREG(item_stat.st_mode) or not _path_is_within(resolved, resolved_root):
                continue
            if resolved.suffix.casefold() != ".docx":
                continue

            relative = candidate.relative_to(lexical_root).as_posix()
            try:
                normalized_relative = _normalize_relative_path(relative)
                normalized_directory = _normalize_relative_path(
                    str(PurePosixPath(normalized_relative).parent)
                )
                document_id = document_id_for_relative_path(relative)
                file_hash = _sha256(resolved)
            except (OSError, ValueError):
                continue
            if document_id in result:
                continue
            result[document_id] = ResolvedDocument(
                document_id=document_id,
                path=resolved,
                relative_path=relative,
                relative_directory=normalized_directory,
                filename=candidate.name,
                size=item_stat.st_size,
                modified_ns=item_stat.st_mtime_ns,
                sha256=file_hash,
                template_root=lexical_root,
            )
    except OSError as exc:
        raise DocumentTemplateRootUnavailable("Template root cannot be read") from exc
    if not fresh:
        with _CACHE_LOCK:
            _READ_CACHE[cache_key] = (time.monotonic(), signature, dict(result))
            _READ_CACHE.move_to_end(cache_key)
            while len(_READ_CACHE) > _READ_CACHE_MAX_ROOTS:
                _READ_CACHE.popitem(last=False)
    return result


def resolve_document(document_id: str, root: Path) -> Optional[ResolvedDocument]:
    resolved = read_document_payload(document_id, root)
    return resolved[0] if resolved is not None else None


def _format_timestamp(modified_ns: int) -> str:
    value = datetime.fromtimestamp(modified_ns / 1_000_000_000, tz=timezone.utc).astimezone()
    return value.strftime("%d.%m.%Y %H:%M")


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def _sort_key(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).casefold()


def _matches_query(kit: Dict[str, object], query: str) -> bool:
    if not query:
        return True
    haystack = [
        kit.get("category"),
        kit.get("release_clean"),
        kit.get("release_full"),
        kit.get("ke"),
        kit.get("variant"),
    ]
    haystack.extend(document.get("filename") for document in kit.get("documents", []))
    return any(query in _sort_key(value) for value in haystack)


def build_catalog_page(
    root: Path,
    *,
    query: str = "",
    category: str = "",
    variant: str = "",
    changed_within: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    now_ns: int | None = None,
) -> Dict[str, object]:
    current_ns = int(now_ns if now_ns is not None else time.time_ns())
    try:
        requested_change_window = int(changed_within) if str(changed_within).strip() else 0
    except (TypeError, ValueError):
        requested_change_window = 0
    selected_change_window = (
        requested_change_window
        if requested_change_window in CATALOG_CHANGE_WINDOWS
        else 0
    )
    recent_threshold_ns = current_ns - RECENT_DOCUMENT_DAYS * 24 * 60 * 60 * 1_000_000_000
    filter_threshold_ns = (
        current_ns - selected_change_window * 24 * 60 * 60 * 1_000_000_000
        if selected_change_window
        else 0
    )
    whitelist = build_document_whitelist(root)
    documents_by_directory: Dict[str, List[ResolvedDocument]] = {}
    for document in whitelist.values():
        documents_by_directory.setdefault(document.relative_directory, []).append(document)

    kits: List[Dict[str, object]] = []
    for entry in build_runtime_template_catalog(root=Path(root)):
        relative_dir = str(entry.get("relative_dir") or "")
        try:
            directory_key = _normalize_relative_path(relative_dir)
        except ValueError:
            continue
        documents = sorted(
            documents_by_directory.get(directory_key, []),
            key=lambda item: _sort_key(item.filename),
        )
        if not documents:
            continue
        latest_modified_ns = max(item.modified_ns for item in documents)
        document_models = []
        for document in documents:
            model = document.as_view_model()
            model["is_recently_modified"] = document.modified_ns > recent_threshold_ns
            document_models.append(model)
        recent_document_count = sum(
            1 for document in document_models if document["is_recently_modified"]
        )
        kits.append({
            "category": str(entry.get("category") or ""),
            "release_clean": str(entry.get("release_clean") or ""),
            "release_full": str(entry.get("release_full") or ""),
            "ke": str(entry.get("ke") or ""),
            "variant": str(entry.get("variant") or ""),
            "doc_count": len(documents),
            "modified_at": _format_timestamp(latest_modified_ns),
            "latest_modified_ns": latest_modified_ns,
            "recent_document_count": recent_document_count,
            "has_recent_changes": recent_document_count > 0,
            "documents": document_models,
        })

    kits.sort(key=lambda item: (
        _sort_key(item["category"]),
        _sort_key(item["release_clean"]),
        _sort_key(item["release_full"]),
    ))

    categories = sorted({str(item["category"]) for item in kits if item["category"]}, key=_sort_key)
    # The UI keeps the historical ``variant`` query key, but its choices are
    # the actual template-kit directories.  Parsed variant metadata is empty
    # for many supported categories (including AI_AGENTS), while release_full
    # is the exact catalog folder name and therefore the reliable child value.
    variants = sorted({str(item["release_full"]) for item in kits if item["release_full"]}, key=_sort_key)
    variants_by_category = {
        category_name: sorted(
            {
                str(item["release_full"])
                for item in kits
                if item["category"] == category_name and item["release_full"]
            },
            key=_sort_key,
        )
        for category_name in categories
    }

    # Category/variant is one catalog contract. Normalize untrusted query
    # values against exact kit names so a hand-crafted URL cannot select a kit
    # belonging to a different category.
    selected_category = category if category in categories else ""
    available_variants = (
        variants_by_category.get(selected_category, [])
        if selected_category
        else variants
    )
    selected_variant = variant if variant in available_variants else ""
    normalized_query = _sort_key(query).strip()
    filtered = [
        item for item in kits
        if (not selected_category or item["category"] == selected_category)
        and (not selected_variant or item["release_full"] == selected_variant)
        and (not filter_threshold_ns or item["latest_modified_ns"] > filter_threshold_ns)
        and _matches_query(item, normalized_query)
    ]

    page_size = max(1, int(page_size))
    total_pages = max(1, math.ceil(len(filtered) / page_size))
    current_page = min(max(1, int(page)), total_pages)
    start = (current_page - 1) * page_size
    page_items = filtered[start:start + page_size]

    return {
        "summary": {
            "kits": len(kits),
            "documents": sum(int(item["doc_count"]) for item in kits),
            "categories": len(categories),
            "filtered_kits": len(filtered),
        },
        "filters": {
            "q": str(query or "").strip(),
            "category": selected_category,
            "variant": selected_variant,
            "changed_within": str(selected_change_window) if selected_change_window else "",
        },
        "filter_options": {
            "categories": categories,
            "variants": available_variants,
            "all_variants": variants,
            "variants_by_category": variants_by_category,
            "change_windows": CATALOG_CHANGE_WINDOWS,
        },
        "kits": page_items,
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_items": len(filtered),
            "has_previous": current_page > 1,
            "has_next": current_page < total_pages,
        },
    }
