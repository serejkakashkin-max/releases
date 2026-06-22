import logging
import os
from datetime import datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, jsonify, render_template, request, send_file

from services.mpr_service import (
    MprError,
    MPR_PACKAGES,
    build_archive_filename,
    build_mpr_package_preview,
    build_mpr_rows,
    build_output_filename,
    generate_mpr_docx,
    list_mpr_templates,
    normalize_mpr_package_codes,
    resolve_mpr_template,
    select_mpr_package_rows,
)


mpr_bp = Blueprint("mpr", __name__)
BASE_PATH = os.getenv("BASE_PATH", "")


@mpr_bp.route("/mpr", methods=["GET"])
def mpr_page():
    return render_template(
        "mpr.html",
        basepath=BASE_PATH,
        templates=list_mpr_templates(),
    )


@mpr_bp.route("/mpr/generate", methods=["POST"])
def mpr_generate():
    template_code = (request.form.get("template_code") or "").strip()
    files = request.files.getlist("files")
    package_codes = request.form.getlist("packages")

    try:
        template_path, template_info = resolve_mpr_template(template_code)
        rows = build_mpr_rows(files)
        selected_codes = normalize_mpr_package_codes(package_codes)
        selected_rows = select_mpr_package_rows(rows, selected_codes)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        documents = []
        for code in selected_codes:
            package = MPR_PACKAGES[code]
            output = generate_mpr_docx(
                template_path,
                selected_rows[code],
                location_label=package["label"],
            )
            filename = build_output_filename(
                template_info,
                package_label=package["label"],
                timestamp=timestamp,
            )
            documents.append((filename, output))

        if len(documents) == 1:
            filename, output = documents[0]
            return send_file(
                output,
                as_attachment=True,
                download_name=filename,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        archive = BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
            for filename, output in documents:
                zip_file.writestr(filename, output.getvalue())
        archive.seek(0)
        return send_file(
            archive,
            as_attachment=True,
            download_name=build_archive_filename(template_info, timestamp=timestamp),
            mimetype="application/zip",
        )
    except MprError as exc:
        payload = {"success": False, "error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        return jsonify(payload), 400
    except Exception as exc:
        logging.exception("MPR generation failed: %s", exc)
        return jsonify({"success": False, "error": "Не удалось сформировать DOCX МПР"}), 500


@mpr_bp.route("/mpr/preview", methods=["POST"])
def mpr_preview():
    template_code = (request.form.get("template_code") or "").strip()
    files = request.files.getlist("files")

    try:
        resolve_mpr_template(template_code)
        rows = build_mpr_rows(files)
        preview = build_mpr_package_preview(rows)
        return jsonify({"success": True, **preview})
    except MprError as exc:
        payload = {"success": False, "error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        return jsonify(payload), 400
    except Exception as exc:
        logging.exception("MPR preview failed: %s", exc)
        return jsonify({"success": False, "error": "Не удалось проверить данные МПР"}), 500
