from __future__ import annotations

from flask import Blueprint, jsonify

from ...db import get_db
from ...idempotency import get_operation


agent_operations_api = Blueprint("agent_operations_api", __name__)


@agent_operations_api.get("/agent-operations/<operation_id>")
def read_agent_operation(operation_id: str):
    """Return only the durable, redacted operation status snapshot."""
    return jsonify({"operation": get_operation(get_db(), operation_id)})
