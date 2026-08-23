from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from openai import APIStatusError
from PIL import Image
from reportlab.pdfgen import canvas

from invoice_assistant import AppError
from invoice_assistant.features.recognition import service


def recognition_result() -> service.RecognitionResult:
    return service.RecognitionResult(
        merchant="示例商户",
        expense_date="2026-08-23",
        amount=79.6,
        currency="CNY",
        converted_amount=None,
        purpose="数据线",
        document_type="invoice",
        uncertainties=[],
        confidence=0.97,
    )


class FakeResponses:
    def __init__(self, captured: dict, output=None, error: Exception | None = None):
        self.captured = captured
        self.output = output or recognition_result()
        self.error = error

    def parse(self, **kwargs):
        self.captured["request"] = kwargs
        if self.error:
            raise self.error
        return type("Response", (), {"output_parsed": self.output})()


def install_fake_client(monkeypatch, captured: dict, error: Exception | None = None) -> None:
    def factory(**kwargs):
        captured["client"] = kwargs
        return type("Client", (), {"responses": FakeResponses(captured, error=error)})()

    monkeypatch.setattr(service, "OpenAI", factory)


def write_image(path: Path) -> None:
    image = Image.new("RGB", (900, 1200), "white")
    image.save(path, format="PNG")
    image.close()


def write_pdf(path: Path, pages: int) -> None:
    pdf = canvas.Canvas(str(path), pagesize=(420, 595))
    for page_number in range(pages):
        pdf.drawString(50, 520, f"Invoice page {page_number + 1}")
        pdf.showPage()
    pdf.save()


@pytest.fixture(autouse=True)
def reset_recognition_status():
    service._reset_status_for_testing()
    yield
    service._reset_status_for_testing()


def test_image_request_uses_deepseek_responses_and_records_success(app, tmp_path, monkeypatch):
    path = tmp_path / "invoice.png"
    write_image(path)
    captured = {}
    install_fake_client(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-secret")
    app.config["RECOGNIZER"] = None

    with app.app_context():
        result = service.recognize_file(str(path), "image/png", path.name)
        status = service.recognition_status()

    assert result["merchant"] == "示例商户"
    assert captured["client"]["base_url"] == "https://api.deepseek.com"
    assert captured["request"]["model"] == "deepseek-v4-flash-vision-exp"
    content = captured["request"]["input"][1]["content"]
    image_parts = [part for part in content if part["type"] == "input_image"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"].startswith("data:image/png;base64,")
    assert not any(part["type"] == "input_file" for part in content)
    assert captured["request"]["text_format"] is service.RecognitionResult
    assert status["availability"] == "available"
    assert status["last_succeeded"] is True
    assert status["last_error"] is None


def test_pdf_pages_are_rendered_as_jpeg_images(app, tmp_path, monkeypatch):
    path = tmp_path / "two-pages.pdf"
    write_pdf(path, pages=2)
    captured = {}
    install_fake_client(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-secret")
    app.config["RECOGNIZER"] = None

    with app.app_context():
        service.recognize_file(str(path), "application/pdf", path.name)

    content = captured["request"]["input"][1]["content"]
    image_parts = [part for part in content if part["type"] == "input_image"]
    assert len(image_parts) == 2
    assert all(part["image_url"].startswith("data:image/jpeg;base64,") for part in image_parts)
    assert not any(part["type"] == "input_file" for part in content)


def test_pdf_page_limit_fails_safely_and_updates_status(app, tmp_path, monkeypatch):
    path = tmp_path / "too-many-pages.pdf"
    write_pdf(path, pages=2)
    captured = {}
    install_fake_client(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-secret")
    app.config.update(RECOGNIZER=None, MAX_RECOGNITION_PDF_PAGES=1)

    with app.app_context():
        with pytest.raises(AppError) as error:
            service.recognize_file(str(path), "application/pdf", path.name)
        status = service.recognition_status()

    assert error.value.code == "recognition_pdf_too_many_pages"
    assert status["availability"] == "unavailable"
    assert "超过单次识别上限" in status["last_error"]
    assert "request" not in captured


def test_missing_key_is_reported_as_unconfigured(app, tmp_path, monkeypatch):
    path = tmp_path / "invoice.png"
    write_image(path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app.config["RECOGNIZER"] = None

    with app.app_context():
        with pytest.raises(AppError) as error:
            service.recognize_file(str(path), "image/png", path.name)
        status = service.recognition_status()

    assert error.value.code == "deepseek_not_configured"
    assert status["configured"] is False
    assert status["availability"] == "unconfigured"
    assert "密钥尚未配置" in status["last_error"]


def test_insufficient_balance_has_deepseek_specific_message():
    response = httpx.Response(402, request=httpx.Request("POST", "https://api.deepseek.com/responses"))
    error = APIStatusError("Insufficient Balance", response=response, body={"error": {"message": "balance"}})

    message = service.explain_recognition_failure(error)

    assert "DeepSeek API 账户余额不足" in message
    assert "OpenAI" not in message
