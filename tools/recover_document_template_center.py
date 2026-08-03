from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DOC_TEMPLATES_ROOT
from extensions import app
from services.document_template_publish_service import recover_stale_operations
from services.document_template_read_service import resolve_document


def main() -> int:
    with app.app_context():
        root = Path(app.config.get("DOCUMENT_TEMPLATE_CENTER_ROOT", DOC_TEMPLATES_ROOT))
        outcome = recover_stale_operations(document_resolver=lambda document_id: resolve_document(document_id, root))
    print("Document Template Center recovery: " + ", ".join(f"{key}={value}" for key, value in outcome.items()))
    return 2 if outcome.get("publish_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
