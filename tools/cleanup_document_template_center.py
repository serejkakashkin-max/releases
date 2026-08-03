from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from extensions import app
from services.document_template_storage_service import cleanup_candidates


def main() -> int:
    with app.app_context():
        count = cleanup_candidates()
    print(f"Document Template Center cleanup: {count} candidate(s) expired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
