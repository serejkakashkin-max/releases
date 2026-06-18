import logging
import os

from flask import Blueprint, jsonify, render_template, request, send_file

from services.mpr_service import (
    MprError,
    build_mpr_rows,
    build_output_filename,
    generate_mpr_docx,
    list_mpr_templates,
    resolve_mpr_template,
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

    try:
        template_path, template_info = resolve_mpr_template(template_code)
        rows = build_mpr_rows(files)
        output = generate_mpr_docx(template_path, rows)
        filename = build_output_filename(template_info)
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except MprError as exc:
        payload = {"success": False, "error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        return jsonify(payload), 400
    except Exception as exc:
        logging.exception("MPR generation failed: %s", exc)
        return jsonify({"success": False, "error": "Не удалось сформировать DOCX МПР"}), 500
