from __future__ import annotations

import os
import stat
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from services.document_template_audit_service import append_audit_event
from services.document_template_generation_service import generate_synthetic_document, generate_test_document
from services.document_template_read_service import clear_document_template_read_cache
from services.document_template_runtime_service import atomic_write_json
from services.document_template_storage_service import (
    CandidateNotFound,
    CandidateStateConflict,
    candidate_directory,
    candidate_file,
    candidate_lock,
    create_history_version,
    data_root,
    document_lock,
    get_candidate,
    get_history_version,
    history_directory,
    history_file,
    list_candidates,
    list_history,
    prune_history,
    sha256_bytes,
    sha256_file,
    update_candidate,
    utc_now,
)
from services.document_template_validation_service import build_contract, inspect_docx, validate_candidate
from services.release_template_catalog_service import clear_template_catalog_cache


class DocumentConflict(RuntimeError):
    pass


class DocumentMutationBlocked(RuntimeError):
    pass


def _read_active_bytes(document) -> bytes:
    target = Path(document.path)
    try:
        if target.is_symlink() or target.resolve(strict=True) != target:
            raise DocumentMutationBlocked("Active path is unsafe")
        with target.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise DocumentMutationBlocked("Active target is not a regular file")
            payload = handle.read()
        if target.is_symlink() or target.resolve(strict=True) != target:
            raise DocumentMutationBlocked("Active path changed while reading")
        return payload
    except OSError as exc:
        raise DocumentMutationBlocked("Active document is unavailable") from exc


def _write_adjacent_and_replace(target: Path, payload: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".op-", dir=str(target.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        errors, _ = inspect_docx(temporary)
        if errors:
            raise DocumentMutationBlocked("Replacement failed validation")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _finalize_history(item: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    item.update(updates)
    item["updated_at"] = utc_now()
    atomic_write_json(history_directory(item["document_id"], item["version_uuid"]) / "metadata.json", item)
    return item


def _blocked_by_recovery(document_id: str) -> bool:
    return (
        any(item.get("state") == "publish_failed" for item in list_candidates(document_id))
        or any(item.get("state") == "publish_failed" for item in list_history(document_id))
    )


def _failure_point(injector, name: str) -> None:
    if callable(injector):
        injector(name)


def publish_candidate(document, candidate_uuid: str, actor: str, *, failure_injector=None) -> dict[str, Any]:
    with candidate_lock(candidate_uuid):
        with document_lock(document.document_id):
            if _blocked_by_recovery(document.document_id):
                raise DocumentMutationBlocked("Document requires controlled recovery")
            metadata = get_candidate(candidate_uuid, document_id=document.document_id)
            if metadata.get("state") != "valid":
                raise CandidateStateConflict("Only a valid candidate can be published")
            active_bytes = _read_active_bytes(document)
            active_sha = sha256_bytes(active_bytes)
            if active_sha != metadata.get("active_sha_at_upload"):
                update_candidate(candidate_uuid, {"state": "conflict"}, document_id=document.document_id)
                raise DocumentConflict("Active document has changed")
            candidate_path = candidate_file(candidate_uuid)
            result = validate_candidate(document.path, candidate_path)
            if not result["ok"]:
                update_candidate(candidate_uuid, {"state": "invalid_contract", "validation": result}, document_id=document.document_id)
                raise CandidateStateConflict("Candidate validation changed")
            test_path, generation_errors = generate_test_document(candidate_uuid, candidate_path)
            if generation_errors or test_path is None:
                result["ok"] = False; result["errors"].extend(generation_errors)
                update_candidate(candidate_uuid, {"state": "invalid_generation", "validation": result}, document_id=document.document_id)
                raise CandidateStateConflict("Synthetic generation failed")
            candidate_bytes = candidate_path.read_bytes()
            candidate_sha = sha256_bytes(candidate_bytes)
            _failure_point(failure_injector, "before_history")
            history = create_history_version(document.document_id, active_bytes, {
                "created_at": utc_now(), "updated_at": utc_now(), "state": "prepared",
                "source_filename": document.filename, "sha256": active_sha,
                "actor": actor, "action": "publish_previous", "comment": metadata.get("comment", ""),
                "contract": build_contract(document.path), "candidate_uuid": candidate_uuid,
            })
            update_candidate(candidate_uuid, {"prepared_history_version_uuid": history["version_uuid"]}, document_id=document.document_id)
            _failure_point(failure_injector, "after_prepared_history")
            operation_uuid = str(uuid.uuid4())
            update_candidate(candidate_uuid, {
                "state": "publishing", "operation_uuid": operation_uuid,
                "expected_old_sha": active_sha, "expected_new_sha": candidate_sha,
                "history_version_uuid": history["version_uuid"],
            }, document_id=document.document_id)
            _failure_point(failure_injector, "after_publishing_metadata")
            try:
                _failure_point(failure_injector, "before_replace")
                _write_adjacent_and_replace(document.path, candidate_bytes)
                _failure_point(failure_injector, "after_replace")
                if sha256_file(document.path) != candidate_sha:
                    raise DocumentMutationBlocked("Active SHA verification failed")
                _failure_point(failure_injector, "after_active_verification")
            except Exception:
                try:
                    _write_adjacent_and_replace(document.path, active_bytes)
                except Exception:
                    update_candidate(candidate_uuid, {"state": "publish_failed", "error_code": "restore_failed"}, document_id=document.document_id)
                    raise DocumentMutationBlocked("Publish recovery is required")
                update_candidate(candidate_uuid, {"state": "recovered", "error_code": "publish_recovered", "prepared_history_version_uuid": ""}, document_id=document.document_id)
                _finalize_history(history, {"state": "recovered"})
                append_audit_event(actor=actor, action="publish", document_id=document.document_id, relative_target=document.relative_path, candidate_uuid=candidate_uuid, old_sha=active_sha, new_sha=candidate_sha, result="recovered", error_code="publish_recovered")
                raise
            _finalize_history(history, {"state": "committed"})
            for sibling in list_candidates(document.document_id):
                if sibling["candidate_uuid"] != candidate_uuid and sibling.get("active_sha_at_upload") == active_sha and sibling.get("state") not in {"published", "cancelled", "expired"}:
                    _failure_point(failure_injector, "during_sibling_invalidation")
                    with candidate_lock(sibling["candidate_uuid"]):
                        update_candidate(sibling["candidate_uuid"], {"state": "conflict"}, document_id=document.document_id)
            prune_history(document.document_id)
            clear_document_template_read_cache(document.path.parent.parent.parent)
            clear_template_catalog_cache()
            _failure_point(failure_injector, "before_audit")
            append_audit_event(actor=actor, action="publish", document_id=document.document_id, relative_target=document.relative_path, candidate_uuid=candidate_uuid, version_uuid=history["version_uuid"], comment=metadata.get("comment"), old_sha=active_sha, new_sha=candidate_sha, result="published")
            _failure_point(failure_injector, "after_audit")
            published = update_candidate(candidate_uuid, {"state": "published", "published_at": utc_now(), "prepared_history_version_uuid": ""}, document_id=document.document_id)
            return published


def rollback_version(document, version_uuid: str, *, actor: str, reason: str, expected_active_sha: str, failure_injector=None) -> dict[str, Any]:
    with document_lock(document.document_id):
        if _blocked_by_recovery(document.document_id):
            raise DocumentMutationBlocked("Document requires controlled recovery")
        active_bytes = _read_active_bytes(document)
        active_sha = sha256_bytes(active_bytes)
        if active_sha != expected_active_sha:
            raise DocumentConflict("Active document has changed")
        historical = get_history_version(document.document_id, version_uuid)
        historical_path = history_file(document.document_id, version_uuid)
        errors, _ = inspect_docx(historical_path)
        if errors:
            raise DocumentMutationBlocked("Historical document is unsafe")
        try:
            if historical.get("contract") != build_contract(historical_path):
                raise DocumentMutationBlocked("Historical contract has changed")
            with tempfile.TemporaryDirectory(prefix="oplot-rollback-check-") as temporary_directory:
                generated, generation_errors = generate_synthetic_document(historical_path, Path(temporary_directory) / "test.docx")
                if generated is None or generation_errors:
                    raise DocumentMutationBlocked("Historical generation failed")
        except DocumentMutationBlocked:
            raise
        except Exception as exc:
            raise DocumentMutationBlocked("Historical contract is unavailable") from exc
        historical_bytes = historical_path.read_bytes()
        historical_sha = sha256_bytes(historical_bytes)
        _failure_point(failure_injector, "before_history")
        operation_uuid = str(uuid.uuid4())
        current_history = create_history_version(document.document_id, active_bytes, {
            "created_at": utc_now(), "updated_at": utc_now(), "state": "prepared",
            "source_filename": document.filename, "sha256": active_sha, "actor": actor,
            "action": "rollback_previous", "comment": reason, "contract": build_contract(document.path),
            "operation_uuid": operation_uuid, "expected_old_sha": active_sha,
            "expected_new_sha": historical_sha, "target_version_uuid": version_uuid,
        })
        _failure_point(failure_injector, "after_prepared_history")
        _finalize_history(current_history, {"state": "publishing"})
        _failure_point(failure_injector, "after_publishing_metadata")
        try:
            _failure_point(failure_injector, "before_replace")
            _write_adjacent_and_replace(document.path, historical_bytes)
            _failure_point(failure_injector, "after_replace")
            if sha256_file(document.path) != historical_sha:
                raise DocumentMutationBlocked("Rollback SHA verification failed")
            _failure_point(failure_injector, "after_active_verification")
        except Exception:
            try:
                _write_adjacent_and_replace(document.path, active_bytes)
            except Exception as exc:
                raise DocumentMutationBlocked("Rollback recovery is required") from exc
            _finalize_history(current_history, {"state": "recovered"})
            raise
        prune_history(document.document_id)
        clear_template_catalog_cache(); clear_document_template_read_cache()
        _failure_point(failure_injector, "before_audit")
        append_audit_event(actor=actor, action="rollback", document_id=document.document_id, relative_target=document.relative_path, version_uuid=version_uuid, comment=reason, old_sha=active_sha, new_sha=historical_sha, result="published")
        _failure_point(failure_injector, "after_audit")
        _finalize_history(current_history, {"state": "committed"})
        return {"state": "published", "sha256": historical_sha, "previous_version_uuid": current_history["version_uuid"]}


def recover_stale_operations(*, document_resolver=None, now: float | None = None) -> dict[str, int]:
    timestamp = time.time() if now is None else now
    outcome = {"validation_interrupted": 0, "published": 0, "recovered": 0, "publish_failed": 0}
    for item in list_candidates():
        try:
            age = timestamp - datetime.fromisoformat(str(item["updated_at"])).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if item.get("state") == "validating" and age > 15 * 60:
            with candidate_lock(item["candidate_uuid"]):
                update_candidate(item["candidate_uuid"], {"state": "validation_interrupted", "error_code": "validation_interrupted"})
            outcome["validation_interrupted"] += 1
        elif item.get("state") == "valid" and item.get("prepared_history_version_uuid") and age > 2 * 60:
            candidate_uuid = item["candidate_uuid"]; document_id = item["document_id"]
            with candidate_lock(candidate_uuid):
                with document_lock(document_id):
                    document = document_resolver(document_id) if callable(document_resolver) else None
                    try:
                        history = get_history_version(document_id, item["prepared_history_version_uuid"])
                        if document is not None and sha256_file(document.path) == item.get("active_sha_at_upload") and history.get("state") == "prepared":
                            _finalize_history(history, {"state": "recovered"})
                            update_candidate(candidate_uuid, {"prepared_history_version_uuid": "", "error_code": "recovery_prepared_history"})
                            outcome["recovered"] += 1
                        else:
                            update_candidate(candidate_uuid, {"state": "publish_failed", "error_code": "recovery_prepared_history_inconsistent"})
                            outcome["publish_failed"] += 1
                    except (CandidateNotFound, ValueError, OSError):
                        update_candidate(candidate_uuid, {"state": "publish_failed", "error_code": "recovery_prepared_history_missing"})
                        outcome["publish_failed"] += 1
        elif item.get("state") == "publishing" and age > 2 * 60:
            candidate_uuid = item["candidate_uuid"]
            document_id = item["document_id"]
            with candidate_lock(candidate_uuid):
                with document_lock(document_id):
                    # Recovery deliberately never replaces files. The active path is stored only as
                    # an opaque relative target in audit, so callers must supply a resolver hook.
                    document = document_resolver(document_id) if callable(document_resolver) else None
                    if document is None:
                        state = "publish_failed"
                    else:
                        actual_sha = sha256_file(document.path)
                        try:
                            history = get_history_version(document_id, item.get("history_version_uuid", ""))
                            history_ok = history.get("sha256") == item.get("expected_old_sha") and history_file(document_id, history["version_uuid"]).is_file()
                        except (CandidateNotFound, ValueError, OSError):
                            history = None; history_ok = False
                        if actual_sha == item.get("expected_new_sha") and history_ok:
                            state = "published"
                            _finalize_history(history, {"state": "committed"})
                            for sibling in list_candidates(document_id):
                                if sibling["candidate_uuid"] != candidate_uuid and sibling.get("active_sha_at_upload") == item.get("expected_old_sha") and sibling.get("state") not in {"published", "cancelled", "expired"}:
                                    update_candidate(sibling["candidate_uuid"], {"state": "conflict"})
                        elif actual_sha == item.get("expected_old_sha") and history_ok:
                            state = "recovered"
                            _finalize_history(history, {"state": "recovered"})
                        else:
                            state = "publish_failed"
                    update_candidate(candidate_uuid, {"state": state, "error_code": f"recovery_{state}", "prepared_history_version_uuid": ""})
                    append_audit_event(actor=item.get("uploaded_by", ""), action="publish_recovery", document_id=document_id, relative_target=getattr(document, "relative_path", ""), candidate_uuid=candidate_uuid, old_sha=item.get("expected_old_sha"), new_sha=item.get("expected_new_sha"), result=state, error_code=f"recovery_{state}")
                    outcome[state] += 1
    history_root = data_root() / "history"
    if history_root.is_dir():
        for metadata_path in history_root.glob("*/*/metadata.json"):
            try:
                document_id = metadata_path.parent.parent.name
                item = get_history_version(document_id, metadata_path.parent.name)
                age = timestamp - datetime.fromisoformat(str(item["updated_at"])).timestamp()
            except (CandidateNotFound, ValueError, TypeError, KeyError, OSError):
                continue
            if item.get("action") != "rollback_previous" or item.get("state") not in {"prepared", "publishing"} or age <= 2 * 60:
                continue
            with document_lock(document_id):
                document = document_resolver(document_id) if callable(document_resolver) else None
                if document is None:
                    state = "publish_failed"
                else:
                    actual_sha = sha256_file(document.path)
                    if actual_sha == item.get("expected_new_sha"):
                        state = "published"
                    elif actual_sha == item.get("expected_old_sha"):
                        state = "recovered"
                    else:
                        state = "publish_failed"
                _finalize_history(item, {"state": "committed" if state == "published" else state, "error_code": f"rollback_recovery_{state}"})
                append_audit_event(actor=item.get("actor", ""), action="rollback_recovery", document_id=document_id, relative_target=getattr(document, "relative_path", ""), version_uuid=item.get("target_version_uuid"), old_sha=item.get("expected_old_sha"), new_sha=item.get("expected_new_sha"), result=state, error_code=f"rollback_recovery_{state}")
                outcome[state] += 1
    return outcome
