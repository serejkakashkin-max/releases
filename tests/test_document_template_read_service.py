from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests._support import create_docx, prepare_config_import

prepare_config_import()

from services.document_template_read_service import (
    _contains_symlink,
    build_catalog_page,
    build_document_whitelist,
    document_id_for_relative_path,
    resolve_document,
)
from services.release_template_catalog_service import clear_template_catalog_cache


class DocumentTemplateReadServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "doc_templates"
        self.root.mkdir()
        clear_template_catalog_cache()

    def tearDown(self):
        clear_template_catalog_cache()
        self.temporary.cleanup()

    def make_kit(self, category="CAT", name="Комплект PL (12345)", documents=1):
        directory = self.root / category / name
        for index in range(documents):
            create_docx(directory / f"Документ {index + 1}.docx", f"Документ {index + 1}")
        clear_template_catalog_cache()
        return directory

    def test_opaque_id_is_deterministic_and_contains_no_path(self):
        self.make_kit()
        first = build_document_whitelist(self.root)
        second = build_document_whitelist(self.root)
        self.assertEqual(set(first), set(second))
        document_id = next(iter(first))
        self.assertRegex(document_id, r"^dt1_[0-9a-f]{64}$")
        self.assertNotIn("Документ", document_id)
        self.assertNotIn("/", document_id)
        self.assertNotIn("\\", document_id)

    def test_same_filename_in_different_directories_has_distinct_id(self):
        create_docx(self.root / "A" / "Первый (12345)" / "same.docx")
        create_docx(self.root / "B" / "Второй (54321)" / "same.docx")
        whitelist = build_document_whitelist(self.root)
        self.assertEqual(2, len(whitelist))
        self.assertEqual(2, len(set(whitelist)))

    def test_unsafe_relative_paths_are_rejected(self):
        for value in ("../secret.docx", "C:\\secret.docx", "C:/secret.docx", "\\\\server\\share\\secret.docx", "/secret.docx"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    document_id_for_relative_path(value)

    def test_malformed_ids_never_resolve(self):
        self.make_kit()
        for value in ("../secret.docx", "C:\\secret.docx", "\\\\server\\share\\secret.docx", "dt1_not-a-hash"):
            with self.subTest(value=value):
                self.assertIsNone(resolve_document(value, self.root))

    def test_symlink_escape_is_not_whitelisted(self):
        outside = Path(self.temporary.name) / "outside.docx"
        create_docx(outside)
        link = self.root / "CAT" / "Комплект (12345)" / "link.docx"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks are not available: {exc}")
        self.assertEqual({}, build_document_whitelist(self.root))

    def test_symlink_component_check_is_always_testable_without_os_privilege(self):
        directory = self.make_kit()
        candidate = directory / "Документ 1.docx"
        linked_component = directory

        def injected_lstat(path):
            result = os.lstat(path)
            if Path(path) == linked_component:
                values = list(result)
                values[0] = stat.S_IFLNK | 0o777
                return os.stat_result(values)
            return result

        self.assertTrue(_contains_symlink(self.root, candidate, lstat=injected_lstat))

    def test_catalog_metadata_search_and_filters(self):
        self.make_kit(category="CAT_A", name="Комплект PL (12345)", documents=2)
        self.make_kit(category="CAT_B", name="Другой BH (54321)", documents=1)
        catalog = build_catalog_page(self.root)
        self.assertEqual(2, catalog["summary"]["kits"])
        self.assertEqual(3, catalog["summary"]["documents"])
        self.assertEqual(
            ["Другой BH (54321)", "Комплект PL (12345)"],
            catalog["filter_options"]["variants"],
        )
        self.assertEqual(
            {
                "CAT_A": ["Комплект PL (12345)"],
                "CAT_B": ["Другой BH (54321)"],
            },
            catalog["filter_options"]["variants_by_category"],
        )
        self.assertNotIn(str(self.root), repr(catalog))
        for document in catalog["kits"][0]["documents"]:
            self.assertEqual(12, len(document["sha256_short"]))

        by_query = build_catalog_page(self.root, query="Документ 2")
        self.assertEqual(1, by_query["summary"]["filtered_kits"])
        by_category = build_catalog_page(self.root, category="CAT_B")
        self.assertEqual(1, by_category["summary"]["filtered_kits"])
        self.assertEqual(["Другой BH (54321)"], by_category["filter_options"]["variants"])
        invalid_pair = build_catalog_page(
            self.root,
            category="CAT_B",
            variant="Комплект PL (12345)",
        )
        self.assertEqual("", invalid_pair["filters"]["variant"])
        self.assertEqual(1, invalid_pair["summary"]["filtered_kits"])
        all_categories = build_catalog_page(
            self.root,
            category="",
            variant="Комплект PL (12345)",
        )
        self.assertEqual(
            ["Другой BH (54321)", "Комплект PL (12345)"],
            all_categories["filter_options"]["variants"],
        )
        by_ke = build_catalog_page(
            self.root,
            ke="12345",
            variant="Комплект PL (12345)",
        )
        self.assertEqual(1, by_ke["summary"]["filtered_kits"])

    def test_category_variant_options_follow_actual_kit_directories(self):
        names = [
            "AEF Containers AI-Агент оркестратор рабочих мест ММБ(14290659)",
            "AEF Containers AI-Планировщик E2E(14061745)",
            "AEF Containers AI-Цифровой клиентский менеджер ММБ(13204700)",
            "AI-Помощник по развитию бизнеса(13300754)",
        ]
        for name in names:
            self.make_kit(category="AI_AGENTS", name=name)

        catalog = build_catalog_page(self.root, category="AI_AGENTS")

        self.assertEqual(names, catalog["filter_options"]["variants"])
        self.assertEqual(
            names,
            catalog["filter_options"]["variants_by_category"]["AI_AGENTS"],
        )
        selected = build_catalog_page(
            self.root,
            category="AI_AGENTS",
            variant=names[2],
        )
        self.assertEqual(1, selected["summary"]["filtered_kits"])
        self.assertEqual(names[2], selected["kits"][0]["release_full"])

    def test_pagination_is_clamped_and_deterministic(self):
        for index in range(13):
            self.make_kit(category="CAT", name=f"Комплект {index:02d} ({10000 + index})")
        first = build_catalog_page(self.root, page=1)
        second = build_catalog_page(self.root, page=2)
        beyond = build_catalog_page(self.root, page=999)
        self.assertEqual(12, len(first["kits"]))
        self.assertEqual(1, len(second["kits"]))
        self.assertEqual(2, beyond["pagination"]["page"])
        self.assertEqual(second["kits"], beyond["kits"])


if __name__ == "__main__":
    unittest.main()
