from __future__ import annotations

import base64
import os
import threading
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
from flask import current_app
from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, Field

from ... import AppError


class RecognitionResult(BaseModel):
    merchant: str | None
    expense_date: str | None
    amount: float | None
    currency: str | None
    converted_amount: float | None
    purpose: str | None
    document_type: Literal["invoice", "foreign_invoice", "receipt", "purchase_list", "payment_record", "unknown"]
    uncertainties: list[str]
    confidence: float = Field(ge=0, le=1)


SYSTEM_PROMPT = """你是高校财务报销票据识别助手。请只根据文件中可见、可读的信息抽取字段，不要猜测。
识别商户/收款方、消费日期（YYYY-MM-DD）、总金额、币种 ISO 代码、购买内容/用途摘要。
对于 Invoice 或 Receipt，保留票面原币金额；不得根据汇率自行推算人民币金额。
对于支付记录，如果文件明确显示银行卡、支付宝、微信等账户实际扣除的人民币金额，把该实际扣款填入 converted_amount；它不是估算汇率换算值。若未明确显示人民币实际扣款，converted_amount 必须返回 null。
文件类型必须是：invoice=中国增值税发票，foreign_invoice=国外 Invoice，receipt=国外 Receipt，purchase_list=购入清单，payment_record=支付记录，unknown=无法判断。
若字段缺失或存在歧义，将具体说明写入 uncertainties；缺失字段返回 null。confidence 表示整份识别结果置信度。"""

PDF_RENDER_SCALE = 2.0
PDF_MAX_RENDER_DIMENSION = 4096
MAX_RENDERED_PDF_BYTES = 34 * 1024 * 1024

_status_lock = threading.Lock()
_last_attempt: dict[str, object | None] = {
    "attempted_at": None,
    "succeeded": None,
    "error": None,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_attempt(succeeded: bool, error: str | None) -> None:
    with _status_lock:
        _last_attempt.update({"attempted_at": _utc_now(), "succeeded": succeeded, "error": error})


def _reset_status_for_testing() -> None:
    with _status_lock:
        _last_attempt.update({"attempted_at": None, "succeeded": None, "error": None})


def recognition_status() -> dict:
    configured = bool(current_app.config.get("RECOGNIZER") or os.environ.get("DEEPSEEK_API_KEY", "").strip())
    with _status_lock:
        latest = dict(_last_attempt)
    if not configured:
        availability = "unconfigured"
    elif latest["attempted_at"] is None:
        availability = "unknown"
    else:
        availability = "available" if latest["succeeded"] else "unavailable"
    return {
        "provider": "deepseek",
        "provider_label": "DeepSeek",
        "configured": configured,
        "availability": availability,
        "model": current_app.config["DEEPSEEK_MODEL"],
        "last_attempt_at": latest["attempted_at"],
        "last_succeeded": latest["succeeded"],
        "last_error": latest["error"],
        "key_exposed_to_browser": False,
    }


def explain_recognition_failure(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return exc.message
    if isinstance(exc, RateLimitError):
        return "DeepSeek API 当前触发速率限制；文件副本已保留，可稍后重试或先手工补录。"
    if isinstance(exc, AuthenticationError):
        return "DeepSeek API 密钥无效或已失效；文件副本已保留，请检查后端密钥配置。"
    if isinstance(exc, PermissionDeniedError):
        return "当前 DeepSeek 密钥无权使用所选模型；文件副本已保留，请检查模型权限。"
    if isinstance(exc, APIConnectionError):
        return "无法连接 DeepSeek API；文件副本已保留，请检查网络后重试或先手工补录。"
    if isinstance(exc, BadRequestError):
        return "DeepSeek API 拒绝了当前图片或模型请求；文件副本已保留，请手工补录并检查模型配置。"
    if isinstance(exc, APIStatusError):
        if exc.status_code == 402:
            return "DeepSeek API 账户余额不足；文件副本已保留，可充值后重试或先手工补录。"
        if exc.status_code in {500, 503}:
            return "DeepSeek API 服务暂时不可用；文件副本已保留，可稍后重试或先手工补录。"
        return f"DeepSeek API 返回请求错误（HTTP {exc.status_code}）；文件副本已保留，请检查模型配置或先手工补录。"
    return f"识别服务暂不可用（{type(exc).__name__}）；文件副本已保留，请手工补录。"


def _image_part(image_bytes: bytes, mime_type: str) -> dict:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{mime_type};base64,{encoded}",
        "detail": "high",
    }


def _render_pdf_pages(path: Path, max_pages: int) -> list[bytes]:
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise AppError("PDF 无法打开或已损坏，请检查文件后重试。", 422, "recognition_pdf_invalid") from exc
    try:
        page_count = len(document)
        if page_count == 0:
            raise AppError("PDF 没有可识别页面，请检查文件后重试。", 422, "recognition_pdf_empty")
        if page_count > max_pages:
            raise AppError(
                f"PDF 共 {page_count} 页，超过单次识别上限 {max_pages} 页；请拆分后重试。",
                422,
                "recognition_pdf_too_many_pages",
            )
        rendered: list[bytes] = []
        rendered_bytes = 0
        for page_index in range(page_count):
            page = document[page_index]
            bitmap = None
            image = None
            rgb_image = None
            try:
                width, height = page.get_size()
                longest_side = max(float(width), float(height), 1.0)
                scale = min(PDF_RENDER_SCALE, PDF_MAX_RENDER_DIMENSION / longest_side)
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                rgb_image = image.convert("RGB")
                buffer = BytesIO()
                rgb_image.save(buffer, format="JPEG", quality=92, optimize=True)
                payload = buffer.getvalue()
                rendered_bytes += len(payload)
                if rendered_bytes > MAX_RENDERED_PDF_BYTES:
                    raise AppError(
                        "PDF 转换后的图片总量过大，无法在一次请求中识别；请拆分或压缩 PDF 后重试。",
                        422,
                        "recognition_pdf_too_large",
                    )
                rendered.append(payload)
            except AppError:
                raise
            except Exception as exc:
                raise AppError(
                    f"PDF 第 {page_index + 1} 页无法转换为图片，请检查文件后重试。",
                    422,
                    "recognition_pdf_render_failed",
                ) from exc
            finally:
                if rgb_image is not None:
                    rgb_image.close()
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
        return rendered
    finally:
        document.close()


def _input_parts(path: Path, mime_type: str) -> list[dict]:
    if mime_type == "application/pdf":
        max_pages = int(current_app.config["MAX_RECOGNITION_PDF_PAGES"])
        return [_image_part(page_bytes, "image/jpeg") for page_bytes in _render_pdf_pages(path, max_pages)]
    return [_image_part(path.read_bytes(), mime_type)]


def _recognize_with_deepseek(path: Path, mime_type: str, original_name: str) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise AppError("DeepSeek API 密钥尚未配置，可保留导入副本并手工补录。", 503, "deepseek_not_configured")
    client = OpenAI(
        api_key=api_key,
        base_url=current_app.config["DEEPSEEK_BASE_URL"],
        timeout=90.0,
        max_retries=2,
    )
    response = client.responses.parse(
        model=current_app.config["DEEPSEEK_MODEL"],
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    *_input_parts(path, mime_type),
                    {"type": "input_text", "text": f"识别这份报销材料。原始文件名仅供参考：{original_name}"},
                ],
            },
        ],
        text_format=RecognitionResult,
    )
    if response.output_parsed is None:
        raise AppError("DeepSeek 未返回可解析的识别结果，请手工补录。", 502, "recognition_unparseable")
    return response.output_parsed.model_dump()


def recognize_file(path_value: str, mime_type: str, original_name: str) -> dict:
    try:
        override = current_app.config.get("RECOGNIZER")
        if override:
            result = override(path_value, mime_type, original_name)
        else:
            result = _recognize_with_deepseek(Path(path_value), mime_type, original_name)
    except Exception as exc:
        _record_attempt(False, explain_recognition_failure(exc))
        raise
    _record_attempt(True, None)
    return result
