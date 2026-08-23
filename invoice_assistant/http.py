from __future__ import annotations

import json

from flask import request

from . import AppError


def json_body() -> dict:
    class DuplicateField(ValueError):
        pass

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateField(key)
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    if not request.is_json:
        raise AppError("请求体必须是 JSON 对象。", 400, "invalid_json")
    try:
        payload = json.loads(
            request.get_data(cache=True),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except DuplicateField:
        raise AppError("JSON 对象不能包含重复字段。", 400, "duplicate_json_field")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        raise AppError("请求体必须是有效 JSON 对象。", 400, "invalid_json")
    if not isinstance(payload, dict):
        raise AppError("请求体必须是 JSON 对象。", 400, "invalid_json")
    return payload
