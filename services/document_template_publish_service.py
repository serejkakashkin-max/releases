from __future__ import annotations

import os
import re
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
    candidate_recovery_sensitive,
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
        any(candidate_recovery_sensitive(item) for item in list_candidates(document_id, allow_expired=True))
        or any(item.get("state") in {"prepared", "publishing", "publish_failed"} or item.get("audit_pending") or item.get("recovery_blocking") for item in list_history(document_id))
    )


def _failure_point(injector, name: str) -> None:
    if callable(injector):
        injector(name)


def _audit_values(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _require_attempt_audit(**values: Any) -> None:
    try:
        append_audit_event(**values)
    except Exception as exc:
        raise DocumentMutationBlocked("Operation audit is unavailable") from exc


def _pending_candidate_audit(candidate_uuid: str, document_id: str, values: dict[str, Any]) -> None:
    update_candidate(candidate_uuid, {"audit_pending": values}, document_id=document_id)
    try:
        append_audit_event(**values)
    except Exception:
        return
    update_candidate(candidate_uuid, {"audit_pending": None}, document_id=document_id)


def _pending_history_audit(history: dict[str, Any], values: dict[str, Any]) -> None:
    _finalize_history(history, {"audit_pending": values})
    try:
        append_audit_event(**values)
    except Exception:
        return
    _finalize_history(history, {"audit_pending": None})


def _invalidate_siblings(document_id: str, candidate_uuid: str, old_sha: str, *, failure_injector=None) -> None:
    for sibling in list_candidates(document_id, allow_expired=True):
        if sibling["candidate_uuid"] == candidate_uuid:
            continue
        with candidate_lock(sibling["candidate_uuid"]):
            current = get_candidate(sibling["candidate_uuid"], document_id=document_id)
            if current.get("active_sha_at_upload") != old_sha or current.get("state") in {"published", "cancelled", "expired"}:
                continue
            _failure_point(failure_injector, "during_sibling_invalidation")
            update_candidate(sibling["candidate_uuid"], {"state": "conflict"}, document_id=document_id)


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
            if candidate_sha != metadata.get("candidate_sha"):
                update_candidate(candidate_uuid, {"state": "invalid_security", "error_code": "candidate_sha_mismatch"}, document_id=document.document_id)
                raise CandidateStateConflict("Candidate bytes changed after validation")
            _failure_point(failure_injector, "before_history")
            operation_uuid = str(uuid.uuid4())
            _require_attempt_audit(
                actor=actor, action="publish_attempt", document_id=document.document_id,
                relative_target=document.relative_path, candidate_uuid=candidate_uuid,
                comment=metadata.get("comment"), old_sha=active_sha, new_sha=candidate_sha,
                result="attempt", operation_uuid=operation_uuid,
            )
            history = create_history_version(document.document_id, active_bytes, {
                "created_at": utc_now(), "updated_at": utc_now(), "state": "prepared",
                "source_filename": document.filename, "sha256": active_sha,
                "actor": actor, "action": "publish_previous", "comment": metadata.get("comment", ""),
                "contract": build_contract(document.path), "candidate_uuid": candidate_uuid,
                "operation_uuid": operation_uuid,
            })
            update_candidate(candidate_uuid, {"prepared_history_version_uuid": history["version_uuid"], "operation_uuid": operation_uuid}, document_id=document.document_id)
            _failure_point(failure_injector, "after_prepared_history")
            update_candidate(candidate_uuid, {
                "state": "publishing", "operation_uuid": operation_uuid,
                "expected_old_sha": active_sha, "expected_new_sha": candidate_sha,
                "history_version_uuid": history["version_uuid"], "publishing_by": actor,
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
                _pending_candidate_audit(candidate_uuid, document.document_id, _audit_values(actor=actor, action="publish", document_id=document.document_id, relative_target=document.relative_path, candidate_uuid=candidate_uuid, old_sha=active_sha, new_sha=candidate_sha, result="recovered", error_code="publish_recovered", operation_uuid=operation_uuid))
                raise
            _finalize_history(history, {"state": "committed"})
            terminal_event = _audit_values(
                actor=actor, action="publish", document_id=document.document_id,
                relative_target=document.relative_path, candidate_uuid=candidate_uuid,
                version_uuid=history["version_uuid"], comment=metadata.get("comment"),
                old_sha=active_sha, new_sha=candidate_sha, result="published",
                operation_uuid=operation_uuid,
            )
            published = update_candidate(candidate_uuid, {
                "state": "published", "published_at": utc_now(), "published_by": actor,
                "prepared_history_version_uuid": "", "audit_pending": terminal_event,
            }, document_id=document.document_id)
            _invalidate_siblings(document.document_id, candidate_uuid, active_sha, failure_injector=failure_injector)
            prune_history(document.document_id)
            clear_document_template_read_cache(document.template_root)
            clear_template_catalog_cache()
            _failure_point(failure_injector, "before_audit")
            _pending_candidate_audit(candidate_uuid, document.document_id, terminal_event)
            _failure_point(failure_injector, "after_audit")
            return get_candidate(candidate_uuid, document_id=document.document_id)


def rollback_version(document, version_uuid: str, *, actor: str, reason: str, expected_active_sha: str, failure_injector=None) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", str(expected_active_sha or "")) is None:
        raise ValueError("Invalid expected SHA")
    with document_lock(document.document_id):
        if _blocked_by_recovery(document.document_id):
            raise DocumentMutationBlocked("Document requires controlled recovery")
        active_bytes = _read_active_bytes(document)
        active_sha = sha256_bytes(active_bytes)
        if active_sha != expected_active_sha:
            raise DocumentConflict("Active document has changed")
        historical = get_history_version(document.document_id, version_uuid)
        historical_path = history_file(document.document_id, version_uuid)
        if historical.get("state") != "committed":
            raise DocumentMutationBlocked("Historical version is not committed")
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
        if historical_sha != historical.get("sha256"):
            raise DocumentMutationBlocked("Historical SHA verification failed")
        _failure_point(failure_injector, "before_history")
        operation_uuid = str(uuid.uuid4())
        _require_attempt_audit(
            actor=actor, action="rollback_attempt", document_id=document.document_id,
            relative_target=document.relative_path, version_uuid=version_uuid,
            comment=reason, old_sha=active_sha, new_sha=historical_sha,
            result="attempt", operation_uuid=operation_uuid,
        )
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
                _finalize_history(current_history, {"state": "publish_failed", "error_code": "rollback_restore_failed"})
                raise DocumentMutationBlocked("Rollback recovery is required") from exc
            _finalize_history(current_history, {"state": "recovered"})
            raise
        terminal_event = _audit_values(
            actor=actor, action="rollback", document_id=document.document_id,
            relative_target=document.relative_path, version_uuid=version_uuid,
            comment=reason, old_sha=active_sha, new_sha=historical_sha,
            result="published", operation_uuid=operation_uuid,
        )
        _finalize_history(current_history, {"state": "committed", "audit_pending": terminal_event})
        prune_history(document.document_id)
        clear_template_catalog_cache(); clear_document_template_read_cache(document.template_root)
        _failure_point(failure_injector, "before_audit")
        _pending_history_audit(current_history, terminal_event)
        _failure_point(failure_injector, "after_audit")
        return {"state": "published", "sha256": historical_sha, "previous_version_uuid": current_history["version_uuid"]}


def _actual_file_sha(path_getter) -> str | None:
    try:
        return sha256_file(path_getter())
    except (CandidateNotFound, ValueError, OSError):
        return None


def recover_stale_operations(*, document_resolver=None, now: float | None = None) -> dict[str, int]:
    timestamp = time.time() if now is None else now
    outcome = {"validation_interrupted": 0, "published": 0, "recovered": 0, "publish_failed": 0, "audit_completed": 0}
    for item in list_candidates(allow_expired=True):
        candidate_uuid = item["candidate_uuid"]
        document_id = item["document_id"]
        if isinstance(item.get("audit_pending"), dict):
            with candidate_lock(candidate_uuid), document_lock(document_id):
                item = get_candidate(candidate_uuid, document_id=document_id)
                if item.get("state") == "published":
                    document = document_resolver(document_id) if callable(document_resolver) else None
                    try:
                        history = get_history_version(document_id, item.get("history_version_uuid", ""))
                    except (CandidateNotFound, ValueError, OSError):
                        history = None
                    evidence_ok = bool(
                        document and history and item.get("operation_uuid")
                        and item.get("operation_uuid") == history.get("operation_uuid")
                        and history.get("state") == "committed"
                        and sha256_file(document.path) == item.get("expected_new_sha") == item.get("candidate_sha")
                        and _actual_file_sha(lambda: candidate_file(candidate_uuid)) == item.get("expected_new_sha")
                        and _actual_file_sha(lambda: history_file(document_id, history["version_uuid"])) == history.get("sha256") == item.get("expected_old_sha")
                    )
                    if not evidence_ok:
                        update_candidate(candidate_uuid, {"state": "publish_failed", "recovery_blocking": True, "error_code": "pending_audit_evidence_inconsistent"}, document_id=document_id)
                        if history:
                            _finalize_history(history, {"state": "publish_failed", "recovery_blocking": True})
                        outcome["publish_failed"] += 1
                        continue
                    _invalidate_siblings(document_id, candidate_uuid, item.get("expected_old_sha", ""))
                try:
                    append_audit_event(**item["audit_pending"])
                except Exception:
                    pass
                else:
                    update_candidate(candidate_uuid, {"audit_pending": None}, document_id=document_id)
                    item["audit_pending"] = None
                    outcome["audit_completed"] += 1
        try:
            age = timestamp - datetime.fromisoformat(str(item["updated_at"])).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if item.get("state") == "validating" and age > 15 * 60:
            with candidate_lock(candidate_uuid):
                updated = update_candidate(candidate_uuid, {"state": "validation_interrupted", "error_code": "validation_interrupted"})
                _pending_candidate_audit(candidate_uuid, document_id, _audit_values(actor=updated.get("uploaded_by"), action="validation_recovery", document_id=document_id, candidate_uuid=candidate_uuid, result="validation_interrupted", error_code="validation_interrupted"))
            outcome["validation_interrupted"] += 1
            continue
        prepared_uuid = item.get("prepared_history_version_uuid")
        if item.get("state") == "valid" and prepared_uuid and age > 2 * 60:
            with candidate_lock(candidate_uuid), document_lock(document_id):
                document = document_resolver(document_id) if callable(document_resolver) else None
                history = None
                try:
                    history = get_history_version(document_id, prepared_uuid)
                except (CandidateNotFound, ValueError, OSError):
                    pass
                active_actual = sha256_file(document.path) if document is not None else None
                candidate_actual = _actual_file_sha(lambda: candidate_file(candidate_uuid))
                history_actual = _actual_file_sha(lambda: history_file(document_id, prepared_uuid)) if history else None
                consistent = bool(
                    document and history
                    and active_actual == item.get("active_sha_at_upload")
                    and candidate_actual == item.get("candidate_sha")
                    and history_actual == item.get("active_sha_at_upload") == history.get("sha256")
                    and history.get("state") == "prepared"
                    and item.get("operation_uuid")
                    and item.get("operation_uuid") == history.get("operation_uuid")
                )
                state = "recovered" if consistent else "publish_failed"
                if history:
                    _finalize_history(history, {"state": "recovered" if consistent else "publish_failed", "recovery_blocking": not consistent})
                updates = {"prepared_history_version_uuid": "", "error_code": f"recovery_prepared_{state}"}
                if not consistent:
                    updates.update({"state": "publish_failed", "recovery_blocking": True})
                update_candidate(candidate_uuid, updates, document_id=document_id)
                _pending_candidate_audit(candidate_uuid, document_id, _audit_values(actor=item.get("uploaded_by"), action="publish_recovery", document_id=document_id, relative_target=getattr(document, "relative_path", ""), candidate_uuid=candidate_uuid, old_sha=item.get("active_sha_at_upload"), new_sha=item.get("candidate_sha"), result=state, error_code=f"recovery_prepared_{state}", operation_uuid=item.get("operation_uuid")))
                outcome[state] += 1
            continue
        if item.get("state") != "publishing" or age <= 2 * 60:
            continue
        with candidate_lock(candidate_uuid), document_lock(document_id):
            document = document_resolver(document_id) if callable(document_resolver) else None
            try:
                history = get_history_version(document_id, item.get("history_version_uuid", ""))
            except (CandidateNotFound, ValueError, OSError):
                history = None
            active_actual = sha256_file(document.path) if document is not None else None
            candidate_actual = _actual_file_sha(lambda: candidate_file(candidate_uuid))
            history_actual = _actual_file_sha(lambda: history_file(document_id, history["version_uuid"])) if history else None
            evidence_ok = bool(
                document and history and item.get("operation_uuid")
                and item.get("operation_uuid") == history.get("operation_uuid")
                and history.get("state") in {"prepared", "publishing"}
                and candidate_actual == item.get("candidate_sha") == item.get("expected_new_sha")
                and history_actual == history.get("sha256") == item.get("expected_old_sha")
            )
            if evidence_ok and active_actual == item.get("expected_new_sha"):
                state = "published"
                _finalize_history(history, {"state": "committed", "recovery_blocking": False})
                for sibling in list_candidates(document_id, allow_expired=True):
                    if sibling["candidate_uuid"] == candidate_uuid or sibling.get("active_sha_at_upload") != item.get("expected_old_sha") or sibling.get("state") in {"published", "cancelled", "expired"}:
                        continue
                    with candidate_lock(sibling["candidate_uuid"]):
                        update_candidate(sibling["candidate_uuid"], {"state": "conflict"}, document_id=document_id)
            elif evidence_ok and active_actual == item.get("expected_old_sha"):
                state = "recovered"
                _finalize_history(history, {"state": "recovered", "recovery_blocking": False})
            else:
                state = "publish_failed"
                if history:
                    _finalize_history(history, {"state": "publish_failed", "recovery_blocking": True})
            updates = {"state": state, "error_code": f"recovery_{state}", "prepared_history_version_uuid": "", "recovery_blocking": state == "publish_failed"}
            if state == "published":
                updates.update({"published_at": utc_now(), "published_by": item.get("publishing_by") or item.get("uploaded_by")})
            update_candidate(candidate_uuid, updates, document_id=document_id)
            _pending_candidate_audit(candidate_uuid, document_id, _audit_values(actor=item.get("publishing_by") or item.get("uploaded_by"), action="publish_recovery", document_id=document_id, relative_target=getattr(document, "relative_path", ""), candidate_uuid=candidate_uuid, old_sha=item.get("expected_old_sha"), new_sha=item.get("expected_new_sha"), result=state, error_code=f"recovery_{state}", operation_uuid=item.get("operation_uuid")))
            outcome[state] += 1
            if document is not None and state == "published":
                clear_document_template_read_cache(document.template_root); clear_template_catalog_cache()

    history_root = data_root() / "history"
    if not history_root.is_dir():
        return outcome
    for metadata_path in history_root.glob("*/*/metadata.json"):
        try:
            document_id = metadata_path.parent.parent.name
            item = get_history_version(document_id, metadata_path.parent.name)
        except (CandidateNotFound, ValueError, OSError):
            continue
        if isinstance(item.get("audit_pending"), dict):
            with document_lock(document_id):
                if item.get("action") == "rollback_previous" and item.get("state") == "committed":
                    document = document_resolver(document_id) if callable(document_resolver) else None
                    try:
                        target = get_history_version(document_id, item.get("target_version_uuid", ""))
                    except (CandidateNotFound, ValueError, OSError):
                        target = None
                    evidence_ok = bool(
                        document and target and item.get("operation_uuid")
                        and sha256_file(document.path) == item.get("expected_new_sha")
                        and _actual_file_sha(lambda: history_file(document_id, item["version_uuid"])) == item.get("sha256") == item.get("expected_old_sha")
                        and _actual_file_sha(lambda: history_file(document_id, target["version_uuid"])) == target.get("sha256") == item.get("expected_new_sha")
                        and target.get("state") == "committed"
                    )
                    if not evidence_ok:
                        _finalize_history(item, {"state": "publish_failed", "recovery_blocking": True, "error_code": "pending_rollback_audit_evidence_inconsistent"})
                        outcome["publish_failed"] += 1
                        continue
                try:
                    append_audit_event(**item["audit_pending"])
                except Exception:
                    pass
                else:
                    _finalize_history(item, {"audit_pending": None})
                    item["audit_pending"] = None
                    outcome["audit_completed"] += 1
        try:
            age = timestamp - datetime.fromisoformat(str(item["updated_at"])).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if item.get("action") != "rollback_previous" or item.get("state") not in {"prepared", "publishing"} or age <= 2 * 60:
            continue
        with document_lock(document_id):
            document = document_resolver(document_id) if callable(document_resolver) else None
            try:
                target = get_history_version(document_id, item.get("target_version_uuid", ""))
            except (CandidateNotFound, ValueError, OSError):
                target = None
            active_actual = sha256_file(document.path) if document is not None else None
            current_history_actual = _actual_file_sha(lambda: history_file(document_id, item["version_uuid"]))
            target_actual = _actual_file_sha(lambda: history_file(document_id, target["version_uuid"])) if target else None
            evidence_ok = bool(
                document and target and item.get("operation_uuid")
                and item.get("state") in {"prepared", "publishing"}
                and current_history_actual == item.get("sha256") == item.get("expected_old_sha")
                and target_actual == target.get("sha256") == item.get("expected_new_sha")
                and target.get("state") == "committed"
            )
            if evidence_ok and active_actual == item.get("expected_new_sha"):
                state = "published"
            elif evidence_ok and active_actual == item.get("expected_old_sha"):
                state = "recovered"
            else:
                state = "publish_failed"
            history_state = "committed" if state == "published" else state
            _finalize_history(item, {"state": history_state, "error_code": f"rollback_recovery_{state}", "recovery_blocking": state == "publish_failed"})
            event = _audit_values(actor=item.get("actor"), action="rollback_recovery", document_id=document_id, relative_target=getattr(document, "relative_path", ""), version_uuid=item.get("target_version_uuid"), old_sha=item.get("expected_old_sha"), new_sha=item.get("expected_new_sha"), result=state, error_code=f"rollback_recovery_{state}", operation_uuid=item.get("operation_uuid"))
            _pending_history_audit(item, event)
            outcome[state] += 1
            if document is not None and state == "published":
                clear_document_template_read_cache(document.template_root); clear_template_catalog_cache()
    return outcome
