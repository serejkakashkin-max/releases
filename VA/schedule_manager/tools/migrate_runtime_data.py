from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from VA.schedule_manager.config import (
    DATA_DIR,
    MIGRATION_REPORT_DIR,
    STATE_DIR,
    UPLOAD_DIR,
    ensure_runtime_dirs,
)


DATA_FILES = (
    "schedule_data.json",
    "employees.json",
    "competencies.json",
    "shifts.json",
    "integration_settings.json",
    "schedule_edits.json",
)
UPLOAD_EXTENSIONS = {".xlsx", ".xls"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Schedule Manager runtime data.")
    parser.add_argument("--source", required=True, help="Source Schedule Manager directory.")
    parser.add_argument("--dry-run", action="store_true", help="Only show planned actions.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing runtime files.")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    report = migrate(source, dry_run=args.dry_run, overwrite=args.overwrite)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def migrate(source: Path, *, dry_run: bool, overwrite: bool) -> Dict[str, object]:
    ensure_runtime_dirs()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report: Dict[str, object] = {
        "ok": True,
        "source_exists": source.exists(),
        "dry_run": dry_run,
        "overwrite": overwrite,
        "copied": [],
        "skipped": [],
        "missing": [],
        "errors": [],
    }
    if not source.exists() or not source.is_dir():
        report["ok"] = False
        report["errors"].append("source_not_found")
        _write_report(report, timestamp, dry_run=dry_run)
        return report

    for name in DATA_FILES:
        _copy_file(
            source / "data" / name,
            DATA_DIR / name,
            report=report,
            dry_run=dry_run,
            overwrite=overwrite,
            backup_root=STATE_DIR / "migration_backups" / timestamp,
        )

    uploads_source = source / "uploads"
    if uploads_source.exists():
        for file in uploads_source.iterdir():
            if not file.is_file() or file.suffix.lower() not in UPLOAD_EXTENSIONS:
                continue
            _copy_file(
                file,
                UPLOAD_DIR / file.name,
                report=report,
                dry_run=dry_run,
                overwrite=overwrite,
                backup_root=STATE_DIR / "migration_backups" / timestamp,
            )
    else:
        report["missing"].append(_safe_name(uploads_source))

    _write_report(report, timestamp, dry_run=dry_run)
    return report


def _copy_file(
    source: Path,
    target: Path,
    *,
    report: Dict[str, object],
    dry_run: bool,
    overwrite: bool,
    backup_root: Path,
) -> None:
    if not source.exists() or not source.is_file():
        report["missing"].append(_safe_name(source))
        return
    if target.exists() and not overwrite:
        report["skipped"].append({"file": _safe_name(target), "reason": "exists"})
        return
    if dry_run:
        action = "overwrite" if target.exists() else "copy"
        report["skipped"].append({"file": _safe_name(target), "reason": f"dry_run_{action}"})
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_target = backup_root / target.parent.name / target.name
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_target)
    shutil.copy2(source, target)
    report["copied"].append(_safe_name(target))


def _write_report(report: Dict[str, object], timestamp: str, *, dry_run: bool) -> None:
    MIGRATION_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "dry_run" if dry_run else "run"
    report_path = MIGRATION_REPORT_DIR / f"{timestamp}_{suffix}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_name(path: Path) -> str:
    parts = path.parts[-3:]
    return "/".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
