from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from flask import current_app

from services.cross_process_file_lock import CrossProcessFileLock
from services.document_template_runtime_service import atomic_write_bytes, atomic_write_json, read_json
from services.runtime_paths import runtime_path


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_REQUEST_OVERHEAD = 256 * 1024
CANDIDATE_TTL_SECONDS = 24 * 60 * 60
UUID_REPR_LENGTH = 36


class CandidateNotFound(LookupError):
    pass


class CandidateUploadTooLarge(ValueError):
    pass


class CandidateStateConflict(RuntimeError):
    pass


def _runtime_root() -> Path:
    override = current_app.config.get("DOCUMENT_TEMPLATE_CENTER_RUNTIME_ROOT")
    return Path(override) if override else runtime_path()


def cache_root() -> Path:
    return _runtime_root() / "cache" / "document_template_center"


def data_root() -> Path:
    return _runtime_root() / "data" / "document_template_center"


def candidate_directory(candidate_uuid: str) -> Path:
    return cache_root() / "candidates" / validate_uuid(candidate_uuid)


def candidate_lock(candidate_uuid: str, timeout: float = 5) -> CrossProcessFileLock:
    value = validate_uuid(candidate_uuid)
    return CrossProcessFileLock(cache_root() / "locks" / f"candidate-{value}.lock", timeout=timeout)


def document_lock(document_id: str, timeout: float = 5) -> CrossProcessFileLock:
    safe = str(document_id or "")
    if not safe.startswith("dt1_") or len(safe) != 68 or not all(char in "0123456789abcdef" for char in safe[4:]):
        raise ValueError("Invalid document identifier")
    return CrossProcessFileLock(cache_root() / "locks" / f"document-{safe}.lock", timeout=timeout)


def validate_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value or ""))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Invalid identifier") from exc
    if parsed.version != 4 or str(parsed) != str(value).lower():
        raise ValueError("Invalid identifier")
    return str(parsed)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_uploaded_candidate(
    stream: BinaryIO,
    *,
    document_id: str,
    source_filename: str,
    active_filename: str,
    active_sha: str,
    uploaded_by: str,
    comment: str,
) -> dict[str, Any]:
    candidate_uuid = str(uuid.uuid4())
    directory = candidate_directory(candidate_uuid)
    directory.mkdir(parents=True, exist_ok=False)
    temporary = directory / ".candidate.uploading"
    written = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("xb") as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise CandidateUploadTooLarge("File exceeds the 10 MiB limit")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written == 0:
            raise ValueError("Выберите непустой DOCX-файл.")
        os.replace(temporary, directory / "candidate.docx")
        metadata = {
            "version": 1,
            "candidate_uuid": candidate_uuid,
            "document_id": document_id,
            "source_filename": Path(source_filename).name,
            "active_filename": active_filename,
            "active_sha_at_upload": active_sha,
            "uploaded_by": uploaded_by,
            "comment": comment,
            "candidate_sha": digest.hexdigest(),
            "candidate_size": written,
            "state": "uploaded",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "validation": None,
        }
        atomic_write_json(directory / "metadata.json", metadata)
        return metadata
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            raise


def get_candidate(candidate_uuid: str, *, document_id: str | None = None, allow_expired: bool = False) -> dict[str, Any]:
    directory = candidate_directory(candidate_uuid)
    metadata = read_json(directory / "metadata.json", missing=None)
    if not isinstance(metadata, dict) or metadata.get("candidate_uuid") != candidate_uuid:
        raise CandidateNotFound("Candidate not found")
    if document_id is not None and metadata.get("document_id") != document_id:
        raise CandidateNotFound("Candidate not found")
    if not allow_expired and metadata.get("state") not in {"published", "cancelled", "expired"}:
        created = datetime.fromisoformat(str(metadata.get("created_at"))).timestamp()
        if time.time() - created > CANDIDATE_TTL_SECONDS:
            metadata["state"] = "expired"
            metadata["updated_at"] = utc_now()
            atomic_write_json(directory / "metadata.json", metadata)
    return metadata


def update_candidate(candidate_uuid: str, updates: dict[str, Any], *, document_id: str | None = None) -> dict[str, Any]:
    metadata = get_candidate(candidate_uuid, document_id=document_id, allow_expired=True)
    protected = {"candidate_uuid", "document_id", "active_sha_at_upload", "candidate_sha", "uploaded_by", "created_at"}
    if protected.intersection(updates):
        raise ValueError("Protected candidate metadata")
    metadata.update(updates)
    metadata["updated_at"] = utc_now()
    atomic_write_json(candidate_directory(candidate_uuid) / "metadata.json", metadata)
    return metadata


def candidate_file(candidate_uuid: str, name: str = "candidate.docx") -> Path:
    if name not in {"candidate.docx", "test.docx"}:
        raise ValueError("Invalid candidate file")
    path = candidate_directory(candidate_uuid) / name
    if not path.is_file() or path.is_symlink():
        raise CandidateNotFound("Candidate file not found")
    return path


def list_candidates(document_id: str | None = None, *, allow_expired: bool = False) -> list[dict[str, Any]]:
    root = cache_root() / "candidates"
    result = []
    if not root.is_dir():
        return result
    for metadata_path in root.glob("*/metadata.json"):
        try:
            item = get_candidate(metadata_path.parent.name, document_id=document_id, allow_expired=allow_expired)
        except (CandidateNotFound, ValueError, OSError):
            continue
        result.append(item)
    result.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return result


def cancel_candidate(candidate_uuid: str, document_id: str) -> dict[str, Any]:
    with candidate_lock(candidate_uuid):
        metadata = get_candidate(candidate_uuid, document_id=document_id)
        if metadata.get("state") in {"published", "publishing", "cancelled", "expired"}:
            raise CandidateStateConflict("Candidate cannot be cancelled")
        for name in ("candidate.docx", "test.docx"):
            (candidate_directory(candidate_uuid) / name).unlink(missing_ok=True)
        return update_candidate(candidate_uuid, {"state": "cancelled", "validation": None}, document_id=document_id)


def history_directory(document_id: str, version_uuid: str) -> Path:
    validate_uuid(version_uuid)
    return data_root() / "history" / document_id / version_uuid


def create_history_version(document_id: str, payload: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
    version_uuid = str(uuid.uuid4())
    directory = history_directory(document_id, version_uuid)
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write_bytes(directory / "document.docx", payload)
    value = {"version": 1, "version_uuid": version_uuid, "document_id": document_id, **metadata}
    atomic_write_json(directory / "metadata.json", value)
    return value


def get_history_version(document_id: str, version_uuid: str) -> dict[str, Any]:
    directory = history_directory(document_id, version_uuid)
    value = read_json(directory / "metadata.json", missing=None)
    if not isinstance(value, dict) or value.get("document_id") != document_id or value.get("version_uuid") != version_uuid:
        raise CandidateNotFound("History version not found")
    return value


def history_file(document_id: str, version_uuid: str) -> Path:
    path = history_directory(document_id, version_uuid) / "document.docx"
    if not path.is_file() or path.is_symlink():
        raise CandidateNotFound("History version not found")
    return path


def list_history(document_id: str) -> list[dict[str, Any]]:
    root = data_root() / "history" / document_id
    result = []
    if root.is_dir():
        for metadata_path in root.glob("*/metadata.json"):
            try:
                item = get_history_version(document_id, metadata_path.parent.name)
                try:
                    item = dict(item)
                    item["created_at_display"] = datetime.fromisoformat(str(item.get("created_at"))).astimezone().strftime("%d.%m.%Y %H:%M")
                except (ValueError, TypeError):
                    item["created_at_display"] = "Дата недоступна"
                result.append(item)
            except (CandidateNotFound, ValueError, OSError):
                continue
    result.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return result


def prune_history(document_id: str, keep: int = 30) -> None:
    for item in list_history(document_id)[max(1, keep):]:
        shutil.rmtree(history_directory(document_id, item["version_uuid"]), ignore_errors=True)


def cleanup_candidates(*, now: float | None = None) -> int:
    timestamp = time.time() if now is None else now
    changed = 0
    for item in list_candidates(allow_expired=True):
        if item.get("state") in {"published", "cancelled", "expired"}:
            continue
        try:
            created = datetime.fromisoformat(str(item["created_at"])).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if timestamp - created > CANDIDATE_TTL_SECONDS:
            with candidate_lock(item["candidate_uuid"]):
                for name in ("candidate.docx", "test.docx"):
                    (candidate_directory(item["candidate_uuid"]) / name).unlink(missing_ok=True)
                update_candidate(item["candidate_uuid"], {"state": "expired", "validation": None})
                changed += 1
    return changed


def claim_maintenance_window(*, now: float | None = None) -> bool:
    timestamp = time.time() if now is None else now
    state_path = cache_root() / "cleanup_state.json"
    lock_path = cache_root() / "locks" / "cleanup-state.lock"
    with CrossProcessFileLock(lock_path, timeout=1):
        state = read_json(state_path, missing={"version": 1, "last_run": 0})
        if not isinstance(state, dict) or state.get("version") != 1:
            return False
        if timestamp - float(state.get("last_run", 0)) < 5 * 60:
            return False
        atomic_write_json(state_path, {"version": 1, "last_run": timestamp})
        return True
