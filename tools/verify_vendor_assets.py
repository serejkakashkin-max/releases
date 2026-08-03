from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.document_template_vendor_service import verify_vendor_assets


def main() -> int:
    result = verify_vendor_assets(PROJECT_ROOT / "static")
    if result["ok"]:
        print("Document Template Center vendor assets: OK")
        return 0
    print("Document Template Center vendor assets: FAILED")
    for problem in result["problems"]:
        print(f"- {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
