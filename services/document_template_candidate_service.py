from __future__ import annotations

from pathlib import Path
from typing import Any

from services.document_template_generation_service import generate_test_document
from services.document_template_storage_service import (
    CandidateStateConflict,
    candidate_directory,
    candidate_file,
    candidate_lock,
    get_candidate,
    sha256_file,
    update_candidate,
)
from services.document_template_validation_service import validate_candidate


def validate_staged_candidate(document_id: str, candidate_uuid: str, active_path: Path) -> dict[str, Any]:
    with candidate_lock(candidate_uuid):
        metadata = get_candidate(candidate_uuid, document_id=document_id)
        if sha256_file(active_path) != metadata.get("active_sha_at_upload"):
            return update_candidate(candidate_uuid, {"state": "conflict"}, document_id=document_id)
        if metadata.get("state") not in {"uploaded", "invalid_security", "invalid_contract", "invalid_generation", "validation_interrupted", "valid"}:
            raise CandidateStateConflict("Candidate cannot be validated")
        update_candidate(candidate_uuid, {"state": "validating"}, document_id=document_id)
        candidate_path = candidate_file(candidate_uuid)
        result = validate_candidate(active_path, candidate_path)
        if not result["ok"]:
            groups = {item["group"] for item in result["errors"]}
            if "security" in groups:
                state = "invalid_security"
                candidate_path.unlink(missing_ok=True)
            else:
                state = "invalid_contract"
            return update_candidate(candidate_uuid, {"state": state, "validation": result}, document_id=document_id)
        test_path, generation_errors = generate_test_document(candidate_uuid, candidate_path)
        if generation_errors or test_path is None:
            result["ok"] = False
            result["errors"].extend(generation_errors)
            result["checks"]["generation"] = False
            return update_candidate(candidate_uuid, {"state": "invalid_generation", "validation": result}, document_id=document_id)
        result["checks"]["generation"] = True
        return update_candidate(candidate_uuid, {"state": "valid", "validation": result}, document_id=document_id)
