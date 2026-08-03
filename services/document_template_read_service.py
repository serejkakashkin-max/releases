from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional

from services.release_template_catalog_service import build_runtime_template_catalog


DOCUMENT_ID_RE = re.compile(r"^dt1_[0-9a-f]{64}$")
DEFAULT_PAGE_SIZE = 12


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

    def as_view_model(self) -> Dict[str, object]:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "size": self.size,
            "size_display": _format_size(self.size),
            "modified_at": _format_timestamp(self.modified_ns),
            "sha256_short": self.sha256[:12],
        }


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _contains_symlink(root: Path, candidate: Path) -> bool:
    if root.is_symlink():
        return True
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
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


def build_document_whitelist(root: Path) -> Dict[str, ResolvedDocument]:
    lexical_root, resolved_root = _root_paths(root)
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
            )
    except OSError as exc:
        raise DocumentTemplateRootUnavailable("Template root cannot be read") from exc
    return result


def resolve_document(document_id: str, root: Path) -> Optional[ResolvedDocument]:
    if not DOCUMENT_ID_RE.fullmatch(str(document_id or "")):
        return None
    return build_document_whitelist(root).get(document_id)


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
    ke: str = "",
    variant: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, object]:
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
        kits.append({
            "category": str(entry.get("category") or ""),
            "release_clean": str(entry.get("release_clean") or ""),
            "release_full": str(entry.get("release_full") or ""),
            "ke": str(entry.get("ke") or ""),
            "variant": str(entry.get("variant") or ""),
            "doc_count": len(documents),
            "modified_at": _format_timestamp(max(item.modified_ns for item in documents)),
            "documents": [item.as_view_model() for item in documents],
        })

    kits.sort(key=lambda item: (
        _sort_key(item["category"]),
        _sort_key(item["release_clean"]),
        _sort_key(item["release_full"]),
    ))

    categories = sorted({str(item["category"]) for item in kits if item["category"]}, key=_sort_key)
    ke_values = sorted({str(item["ke"]) for item in kits if item["ke"]}, key=_sort_key)
    variants = sorted({str(item["variant"]) for item in kits if item["variant"]}, key=_sort_key)
    normalized_query = _sort_key(query).strip()
    filtered = [
        item for item in kits
        if (not category or item["category"] == category)
        and (not ke or item["ke"] == ke)
        and (not variant or item["variant"] == variant)
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
            "category": category,
            "ke": ke,
            "variant": variant,
        },
        "filter_options": {
            "categories": categories,
            "ke_values": ke_values,
            "variants": variants,
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
