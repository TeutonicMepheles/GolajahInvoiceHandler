from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import AppError
from .domain import CATEGORY_META


FONT_NAME = "InvoiceAssistantCN"
FONT_BOLD = "InvoiceAssistantCN-Bold"
try:
    regular_path = Path("C:/Windows/Fonts/Deng.ttf")
    bold_path = Path("C:/Windows/Fonts/Dengb.ttf")
    if regular_path.is_file():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(regular_path)))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold_path if bold_path.is_file() else regular_path)))
    else:
        raise FileNotFoundError
except Exception:
    FONT_NAME = "STSong-Light"
    FONT_BOLD = FONT_NAME
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    except Exception:
        pass


def _paragraph(text, style):
    safe = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe, style)


def _cover_pdf(batch: dict, items: list[dict]) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=15 * mm,
        bottomMargin=14 * mm,
        title="报销材料目录",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Title"],
        fontName=FONT_BOLD,
        fontSize=21,
        leading=28,
        textColor=colors.HexColor("#12344D"),
        alignment=TA_LEFT,
        spaceAfter=7 * mm,
    )
    meta = ParagraphStyle(
        "Meta",
        parent=styles["BodyText"],
        fontName=FONT_NAME,
        fontSize=9.5,
        leading=15,
        textColor=colors.HexColor("#496475"),
    )
    cell = ParagraphStyle(
        "Cell",
        parent=styles["BodyText"],
        fontName=FONT_NAME,
        fontSize=7.4,
        leading=10,
        wordWrap="CJK",
    )
    head = ParagraphStyle(
        "Head",
        parent=cell,
        textColor=colors.white,
        alignment=TA_CENTER,
        fontSize=7.2,
    )
    story = [_paragraph("报销材料目录", title)]
    project_name = batch.get("project_name") or "未分项目"
    total_text = f"{batch['total_amount']:.2f} CNY"
    story.extend(
        [
            _paragraph(f"报销包：{batch['name']}　　项目：{project_name}", meta),
            _paragraph(f"用途说明：{batch.get('purpose') or '未填写'}", meta),
            _paragraph(f"备注：{batch.get('notes') or '无'}", meta),
            _paragraph(f"合计：{total_text}　　条目数：{len(items)}", meta),
            Spacer(1, 5 * mm),
        ]
    )
    data = [[_paragraph(value, head) for value in ["序号", "日期", "商户", "用途", "金额", "材料", "状态"]]]
    for index, item in enumerate(items, 1):
        material_names = "、".join(a["category_label"] for a in item["attachments"]) or "无"
        data.append(
            [
                _paragraph(f"{index:03d}", cell),
                _paragraph(item["expense_date"], cell),
                _paragraph(item["merchant"], cell),
                _paragraph(item["purpose"], cell),
                _paragraph(f"{item['reimbursement_amount']:.2f} CNY", cell),
                _paragraph(material_names, cell),
                _paragraph("齐全" if item["material"]["complete"] else "缺失", cell),
            ]
        )
    table = Table(data, colWidths=[11 * mm, 22 * mm, 31 * mm, 41 * mm, 25 * mm, 34 * mm, 17 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#164E63")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7D5DC")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F8F9")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)
    doc.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    buffer.seek(0)
    return buffer


def _page_number(canv: canvas.Canvas, doc):
    canv.saveState()
    canv.setFont(FONT_NAME, 8)
    canv.setFillColor(colors.HexColor("#6B7F8C"))
    canv.drawRightString(A4[0] - 14 * mm, 8 * mm, f"第 {doc.page} 页")
    canv.restoreState()


def _separator_pdf(item: dict, index: int) -> BytesIO:
    buffer = BytesIO()
    canv = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    canv.setFillColor(colors.HexColor("#12344D"))
    canv.rect(0, height - 70 * mm, width, 70 * mm, fill=1, stroke=0)
    canv.setFillColor(colors.HexColor("#35B6A3"))
    canv.rect(18 * mm, height - 27 * mm, 16 * mm, 2.5 * mm, fill=1, stroke=0)
    canv.setFont(FONT_NAME, 13)
    canv.setFillColor(colors.HexColor("#CFE9E5"))
    canv.drawString(18 * mm, height - 20 * mm, f"条目 {index:03d}")
    canv.setFont(FONT_BOLD, 24)
    canv.setFillColor(colors.white)
    canv.drawString(18 * mm, height - 43 * mm, str(item["merchant"])[:30])
    canv.setFont(FONT_NAME, 11)
    canv.setFillColor(colors.HexColor("#D7E5EA"))
    canv.drawString(18 * mm, height - 55 * mm, f"{item['expense_date']}　{item['reimbursement_amount']:.2f} CNY")
    y = height - 93 * mm
    labels = [
        ("报销项目", item.get("project_name") or "未分项目"),
        ("用途说明", item.get("purpose") or "未填写"),
        ("所附材料", "、".join(a["category_label"] for a in item["attachments"]) or "无"),
        ("材料状态", "齐全" if item["material"]["complete"] else "缺失：" + "、".join(m["label"] for m in item["material"]["missing"])),
    ]
    for label, value in labels:
        canv.setFont(FONT_NAME, 9)
        canv.setFillColor(colors.HexColor("#6B7F8C"))
        canv.drawString(20 * mm, y, label)
        canv.setFont(FONT_NAME, 12)
        canv.setFillColor(colors.HexColor("#173A4F"))
        canv.drawString(20 * mm, y - 8 * mm, str(value)[:70])
        y -= 25 * mm
    canv.setFont(FONT_NAME, 8)
    canv.setFillColor(colors.HexColor("#7B8E99"))
    canv.drawRightString(width - 18 * mm, 10 * mm, "以下为本条目对应的原始报销材料")
    canv.showPage()
    canv.save()
    buffer.seek(0)
    return buffer


def _normalized_pdf_pages(path: Path):
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise AppError(f"PDF 已加密，无法合并：{path.name}", 409, "encrypted_pdf")
        for source in reader.pages:
            if getattr(source, "rotation", 0) and hasattr(source, "transfer_rotation_to_content"):
                rotation_writer = PdfWriter()
                rotation_writer.add_page(source)
                source = rotation_writer.pages[0]
                source.transfer_rotation_to_content()
            source_w = float(source.mediabox.width)
            source_h = float(source.mediabox.height)
            if source_w <= 0 or source_h <= 0:
                raise AppError(f"PDF 页面尺寸无效：{path.name}", 409, "invalid_pdf_page")
            page_size = landscape(A4) if source_w > source_h else A4
            margin = 5 * mm
            scale = min((page_size[0] - 2 * margin) / source_w, (page_size[1] - 2 * margin) / source_h)
            tx = (page_size[0] - source_w * scale) / 2
            ty = (page_size[1] - source_h * scale) / 2
            writer = PdfWriter()
            target = writer.add_blank_page(width=page_size[0], height=page_size[1])
            target.merge_transformed_page(source, Transformation().scale(scale).translate(tx, ty))
            yield target
    except AppError:
        raise
    except Exception as exc:
        raise AppError(f"无法读取 PDF {path.name}：{exc}", 409, "invalid_pdf")


def _image_pdf_page(path: Path):
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.load()
            buffer = BytesIO()
            width, height = image.size
            page_size = landscape(A4) if width > height else A4
            canv = canvas.Canvas(buffer, pagesize=page_size)
            margin = 5 * mm
            scale = min((page_size[0] - 2 * margin) / width, (page_size[1] - 2 * margin) / height)
            draw_w, draw_h = width * scale, height * scale
            x, y = (page_size[0] - draw_w) / 2, (page_size[1] - draw_h) / 2
            from reportlab.lib.utils import ImageReader

            canv.drawImage(ImageReader(image), x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
            canv.showPage()
            canv.save()
            buffer.seek(0)
            return PdfReader(buffer).pages[0]
    except Exception as exc:
        raise AppError(f"无法读取图片 {path.name}：{exc}", 409, "invalid_image")


def generate_material_package(batch: dict, items: list[dict], archive_files: dict[int, Path], output_path: Path) -> dict:
    writer = PdfWriter()
    for page in PdfReader(_cover_pdf(batch, items)).pages:
        writer.add_page(page)
    attachment_pages = 0
    for index, item in enumerate(items, 1):
        for page in PdfReader(_separator_pdf(item, index)).pages:
            writer.add_page(page)
        for attachment in item["attachments"]:
            file_path = archive_files[attachment["id"]]
            if file_path.suffix.lower() == ".pdf":
                for page in _normalized_pdf_pages(file_path):
                    writer.add_page(page)
                    attachment_pages += 1
            else:
                writer.add_page(_image_pdf_page(file_path))
                attachment_pages += 1
    writer.add_metadata({"/Title": f"报销材料包 - {batch['name']}", "/Author": "发票报销管理助手"})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as stream:
        writer.write(stream)
    verification = PdfReader(str(output_path), strict=False)
    expected_min_pages = 1 + len(items) + attachment_pages
    if len(verification.pages) < expected_min_pages:
        raise AppError("生成的 PDF 页数校验失败。", 500, "pdf_verification_failed")
    portrait_pages = 0
    landscape_pages = 0
    for page in verification.pages:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        is_portrait = abs(width - A4[0]) <= 1.5 and abs(height - A4[1]) <= 1.5
        is_landscape = abs(width - A4[1]) <= 1.5 and abs(height - A4[0]) <= 1.5
        if not is_portrait and not is_landscape:
            raise AppError("生成的 PDF 存在非 A4 页面。", 500, "pdf_not_a4")
        portrait_pages += int(is_portrait)
        landscape_pages += int(is_landscape)
    return {
        "page_count": len(verification.pages),
        "attachment_pages": attachment_pages,
        "file_count": len(archive_files),
        "portrait_pages": portrait_pages,
        "landscape_pages": landscape_pages,
    }
