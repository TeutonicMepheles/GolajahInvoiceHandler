from __future__ import annotations

from flask import Blueprint, current_app, jsonify, send_file

from ... import AppError
from ...db import get_db
from ...http import json_body
from .service import (
    associate_supplementary_item,
    build_attachment_thumbnail,
    resolve_attachment_file,
)


document_intake_api = Blueprint("document_intake_api", __name__)


def _integer(payload: dict, name: str, *, positive: bool) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(f"必须提供有效的 {name}。", 400, "invalid_request")
    if value < (1 if positive else 0):
        raise AppError(f"必须提供有效的 {name}。", 400, "invalid_request")
    return value


@document_intake_api.post("/document-intake/associations")
def create_document_association():
    payload = json_body()
    required = {"source_item_id", "target_item_id", "source_version", "target_version"}
    if set(payload) != required:
        raise AppError("关联请求必须且只能包含源条目、目标条目及两侧版本。", 400, "invalid_request")
    result = associate_supplementary_item(
        get_db(),
        source_item_id=_integer(payload, "source_item_id", positive=True),
        target_item_id=_integer(payload, "target_item_id", positive=True),
        source_version=_integer(payload, "source_version", positive=False),
        target_version=_integer(payload, "target_version", positive=False),
    )
    return jsonify(result)


@document_intake_api.get("/attachments/<int:attachment_id>/preview")
def attachment_preview(attachment_id: int):
    attachment = resolve_attachment_file(get_db(), attachment_id, current_app.config["IMPORT_DIR"])
    return send_file(
        attachment.path,
        as_attachment=False,
        download_name=attachment.display_name,
        mimetype=attachment.mime_type,
        conditional=True,
    )


@document_intake_api.get("/attachments/<int:attachment_id>/thumbnail")
def attachment_thumbnail(attachment_id: int):
    attachment = resolve_attachment_file(get_db(), attachment_id, current_app.config["IMPORT_DIR"])
    thumbnail = build_attachment_thumbnail(
        attachment,
        max_image_pixels=int(current_app.config["MAX_IMAGE_PIXELS"]),
    )
    return send_file(
        thumbnail,
        as_attachment=False,
        download_name=f"attachment-{attachment_id}-thumbnail.jpg",
        mimetype="image/jpeg",
    )
