from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._support import PROJECT_ROOT
from services.document_template_vendor_service import verify_vendor_assets


class DocumentTemplateAssetTests(unittest.TestCase):
    def test_committed_vendor_assets_match_manifest(self):
        result = verify_vendor_assets(PROJECT_ROOT / "static")
        self.assertTrue(result["ok"], result["problems"])

    def test_corrupt_asset_is_reported_by_logical_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            static_root = Path(temporary)
            vendor = static_root / "vendor"
            vendor.mkdir()
            asset = vendor / "asset.js"
            asset.write_text("corrupt", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "assets": [{
                    "name": "Pinned asset",
                    "path": "vendor/asset.js",
                    "sha256": "0" * 64,
                    "status": "ready",
                }],
            }
            (vendor / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = verify_vendor_assets(static_root)
            self.assertFalse(result["ok"])
            self.assertEqual(["Pinned asset"], result["problems"])


if __name__ == "__main__":
    unittest.main()
