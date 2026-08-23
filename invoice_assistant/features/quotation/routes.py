from __future__ import annotations

from flask import Blueprint, current_app, jsonify, send_file

from ...http import json_body
from ...quotation import calculate_quotation
from ...quotation_export import build_quotation_docx


quotation_api = Blueprint("quotation_api", __name__)


@quotation_api.post("/quotations/calculate")
def quotation_calculate():
    return jsonify(calculate_quotation(json_body()))


@quotation_api.post("/quotations/export")
def quotation_export():
    document, filename = build_quotation_docx(json_body(), current_app.config["QUOTATION_TEMPLATE"])
    return send_file(
        document,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
