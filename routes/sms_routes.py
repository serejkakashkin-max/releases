import re
import tempfile
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from io import BytesIO

from flask import Blueprint, request, send_file, jsonify

from config import sms_logger
from services.sms_service import (
    normalize_sms_profile,
    process_csv,
    resolve_sms_profile_template,
)
from services.counter_service import increment_counter


sms_bp = Blueprint("sms", __name__)


def _safe_release_sms_filename(item, index):
    parts = [
        str(item.get("release_key") or "").strip(),
        str(item.get("rov_key") or "").strip(),
        str(item.get("result") or "").strip(),
    ]
    stem = "_".join(part for part in parts if part)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    if not stem:
        stem = f"sms_{index + 1}"
    return f"SMS_{stem}.csv"


@sms_bp.route("/sms/release-monitor/generate", methods=["POST"])
def generate_release_monitor_sms():
    """Generate grouped CSV files from already prepared release-monitor SMS texts."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "Ожидается JSON-объект."}), 400

    forbidden_keys = {
        "filename",
        "file_name",
        "path",
        "csv",
        "csv_path",
        "template",
        "template_path",
    }
    if forbidden_keys.intersection(payload):
        return jsonify({
            "success": False,
            "error": "Путь или имя CSV-шаблона нельзя передавать с frontend.",
        }), 400

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"success": False, "error": "Нет включенных SMS для выгрузки."}), 400
    if len(items) > 1000:
        return jsonify({"success": False, "error": "За один раз можно сформировать не более 1000 SMS."}), 400

    validated = []
    errors = []
    templates = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"Строка {index + 1}: ожидается объект.")
            continue
        if forbidden_keys.intersection(item):
            errors.append(f"Строка {index + 1}: путь или имя CSV-шаблона запрещены.")
            continue

        release_key = str(item.get("release_key") or "").strip()
        rov_key = str(item.get("rov_key") or "").strip()
        row_key = str(item.get("row_key") or "").strip()
        text = str(item.get("text") or "").strip()
        result = str(item.get("result") or "").strip().lower()

        if not row_key:
            errors.append(f"Строка {index + 1}: не указан row_key.")
        if not release_key:
            errors.append(f"Строка {index + 1}: не указан ключ релиза.")
        if not text:
            errors.append(f"{release_key or f'Строка {index + 1}'}: текст SMS пуст.")
        if result not in {"success", "failure"}:
            errors.append(f"{release_key or f'Строка {index + 1}'}: неизвестный результат внедрения.")

        try:
            profile = normalize_sms_profile(item.get("profile"))
            if profile not in templates:
                templates[profile] = resolve_sms_profile_template(profile)
        except (ValueError, FileNotFoundError) as exc:
            errors.append(f"{release_key or f'Строка {index + 1}'}: {exc}")
            continue

        if row_key and release_key and text and result in {"success", "failure"}:
            validated.append({
                "profile": profile,
                "template": templates[profile],
                "release_key": release_key,
                "rov_key": rov_key,
                "row_key": row_key,
                "result": result,
                "text": text,
                "filename": _safe_release_sms_filename(item, index),
            })

    if errors:
        return jsonify({"success": False, "error": "Не удалось сформировать SMS.", "errors": errors}), 400
    if not validated:
        return jsonify({"success": False, "error": "Нет проверенных SMS для выгрузки."}), 400

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zip_buffer = BytesIO()
            used_names = set()

            with ZipFile(zip_buffer, "w", compression=ZIP_DEFLATED) as zip_file:
                for index, item in enumerate(validated):
                    filename = item["filename"]
                    original_filename = filename
                    suffix = 2
                    archive_key = (item["profile"], filename.lower())
                    while archive_key in used_names:
                        filename = f"{Path(original_filename).stem}_{suffix}.csv"
                        archive_key = (item["profile"], filename.lower())
                        suffix += 1
                    used_names.add(archive_key)

                    output_path = temp_path / f"{index:03d}_{filename}"
                    process_csv(item["template"], item["text"], output_path)
                    zip_file.write(output_path, f"{item['profile']}/{filename}")

            zip_buffer.seek(0)
            increment_counter("sms")
            download_name = f"sms_release_monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            return send_file(
                zip_buffer,
                as_attachment=True,
                download_name=download_name,
                mimetype="application/zip",
            )
    except UnicodeEncodeError:
        return jsonify({
            "success": False,
            "error": "Текст SMS содержит символы, которые нельзя записать в CSV-кодировке cp1251.",
        }), 400
    except Exception as exc:
        sms_logger.exception("release-monitor SMS generation failed: %s", type(exc).__name__)
        return jsonify({
            "success": False,
            "error": "Не удалось сформировать ZIP с SMS. Проверьте CSV-шаблоны.",
        }), 500
