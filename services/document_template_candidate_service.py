from __future__ import annotations

from pathlib import Path
from typing import Any

from services.document_template_audit_service import append_audit_event
from services.document_template_generation_service import generate_test_document
from services.document_template_storage_service import (
    CandidateNotFound,
    CandidateStateConflict,
    candidate_directory,
    candidate_file,
    candidate_lock,
    get_candidate,
    sha256_bytes,
    sha256_file,
    update_candidate,
)
from services.document_template_validation_service import validate_candidate


class CandidatePreviewDenied(RuntimeError):
    pass


def _validation_audit(metadata: dict[str, Any], state: str, result: dict[str, Any]) -> None:
    first_error = next(iter(result.get("errors") or []), {})
    event = dict(
        actor=metadata.get("uploaded_by", ""),
        action="validation_result",
        document_id=metadata.get("document_id", ""),
        candidate_uuid=metadata.get("candidate_uuid", ""),
        result=state,
        error_code=first_error.get("code", ""),
    )
    try:
        append_audit_event(**event)
    except Exception:
        update_candidate(metadata["candidate_uuid"], {"audit_pending": event}, document_id=metadata["document_id"])


def candidate_preview_payload(document_id: str, candidate_uuid: str, *, test_document: bool = False) -> tuple[bytes, dict[str, Any]]:
    with candidate_lock(candidate_uuid):
        metadata = get_candidate(candidate_uuid, document_id=document_id)
        validation = metadata.get("validation")
        checks = validation.get("checks") if isinstance(validation, dict) else None
        if not isinstance(checks, dict):
            raise CandidatePreviewDenied("Candidate has no completed validation")
        if test_document:
            if checks.get("generation") is not True:
                raise CandidatePreviewDenied("Synthetic generation is not confirmed")
            path = candidate_file(candidate_uuid, "test.docx")
            expected_sha = metadata.get("test_sha")
        else:
            if metadata.get("state") in {"uploaded", "validating", "validation_interrupted", "invalid_security"}:
                raise CandidatePreviewDenied("Candidate preview is not permitted")
            if checks.get("security") is not True or checks.get("structure") is not True:
                raise CandidatePreviewDenied("Basic DOCX validation did not pass")
            path = candidate_file(candidate_uuid, "candidate.docx")
            expected_sha = metadata.get("candidate_sha")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CandidateNotFound("Candidate file not found") from exc
        if not isinstance(expected_sha, str) or sha256_bytes(payload) != expected_sha:
            raise CandidatePreviewDenied("Candidate file does not match validation metadata")
        return payload, metadata


def validate_staged_candidate(document_id: str, candidate_uuid: str, active_path: Path) -> dict[str, Any]:
    with candidate_lock(candidate_uuid):
        metadata = get_candidate(candidate_uuid, document_id=document_id)
        test_path = candidate_directory(candidate_uuid) / "test.docx"
        test_path.unlink(missing_ok=True)
        update_candidate(candidate_uuid, {"test_sha": "", "test_size": 0}, document_id=document_id)
        if sha256_file(active_path) != metadata.get("active_sha_at_upload"):
            result = {
                "ok": False,
                "errors": [{"code": "active_sha_conflict", "message": "Действующий шаблон изменился после загрузки кандидата.", "group": "structure"}],
                "warnings": [], "contract": {},
                "checks": {"security": False, "structure": False, "placeholders": False, "jira": False, "generation": False},
            }
            updated = update_candidate(candidate_uuid, {"state": "conflict", "validation": result}, document_id=document_id)
            _validation_audit(metadata, "conflict", result)
            return updated
        if metadata.get("state") not in {"uploaded", "invalid_security", "invalid_contract", "invalid_generation", "validation_interrupted", "valid"}:
            raise CandidateStateConflict("Candidate cannot be validated")
        update_candidate(candidate_uuid, {"state": "validating", "test_sha": "", "test_size": 0}, document_id=document_id)
        candidate_path = candidate_file(candidate_uuid)
        if sha256_file(candidate_path) != metadata.get("candidate_sha"):
            result = {
                "ok": False,
                "errors": [{"code": "candidate_sha_mismatch", "message": "Загруженный файл изменился после приёма.", "group": "security"}],
                "warnings": [],
                "contract": {},
                "checks": {"security": False, "structure": False, "placeholders": False, "jira": False, "generation": False},
            }
            candidate_path.unlink(missing_ok=True)
            updated = update_candidate(candidate_uuid, {"state": "invalid_security", "validation": result}, document_id=document_id)
            _validation_audit(metadata, "invalid_security", result)
            return updated
        result = validate_candidate(active_path, candidate_path)
        if not result["ok"]:
            groups = {item["group"] for item in result["errors"]}
            if "security" in groups:
                state = "invalid_security"
                candidate_path.unlink(missing_ok=True)
            else:
                state = "invalid_contract"
            result["checks"]["generation"] = False
            updated = update_candidate(candidate_uuid, {"state": state, "validation": result}, document_id=document_id)
            _validation_audit(metadata, state, result)
            return updated
        test_path, generation_errors = generate_test_document(candidate_uuid, candidate_path)
        if generation_errors or test_path is None:
            result["ok"] = False
            result["errors"].extend(generation_errors)
            result["checks"]["generation"] = False
            updated = update_candidate(candidate_uuid, {"state": "invalid_generation", "validation": result}, document_id=document_id)
            _validation_audit(metadata, "invalid_generation", result)
            return updated
        result["checks"]["generation"] = True
        updated = update_candidate(candidate_uuid, {
            "state": "valid",
            "validation": result,
            "test_sha": sha256_file(test_path),
            "test_size": test_path.stat().st_size,
        }, document_id=document_id)
        _validation_audit(metadata, "valid", result)
        return updated
