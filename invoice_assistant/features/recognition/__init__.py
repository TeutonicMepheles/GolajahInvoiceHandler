"""DeepSeek-backed document recognition feature."""

from .service import explain_recognition_failure, recognition_status, recognize_file

__all__ = ["explain_recognition_failure", "recognition_status", "recognize_file"]
