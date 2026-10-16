import base64
import binascii
from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape

from .invoice_presentation import (
    invoice_section_for_line_type,
    normalized_invoice_presentation_snapshot,
)


def invoice_logo(snapshot: dict):
    logo = snapshot.get("logo")
    if not logo:
        return None
    try:
        image = Image(BytesIO(base64.b64decode(logo["content_base64"])))
    except (binascii.Error, KeyError, TypeError, ValueError):
        return None
    scale = min(
        (48 * mm) / image.imageWidth,
        (22 * mm) / image.imageHeight,
        1,
    )
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    return image


def invoice_line_table(lines, invoice, styles) -> Table:
    rows = [["Description", "Service period", "Qty", "Unit price", "Amount"]]
    for line in lines:
        service_period = ""
        if line.service_period_start and line.service_period_end:
            service_period = (
                f"{line.service_period_start:%Y-%m-%d} – "
                f"{line.service_period_end:%Y-%m-%d}"
            )
        rows.append(
            [
                Paragraph(escape(line.description), styles["BodyText"]),
                service_period,
                f"{line.quantity}",
                f"{line.unit_price} {invoice.currency}",
                f"{line.amount} {invoice.currency}",
            ]
        )
    table = Table(rows, colWidths=[62 * mm, 42 * mm, 16 * mm, 27 * mm, 27 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ]
        )
    )
    return table


def render_invoice_pdf(invoice, lines=None) -> bytes:
    if not invoice.finalized_at:
        raise ValueError("Only finalized invoices can be downloaded.")

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Eventyay invoice {invoice.invoice_number}",
    )
    styles = getSampleStyleSheet()
    billing_snapshot = invoice.billing_address_snapshot or {}
    presentation = normalized_invoice_presentation_snapshot(
        invoice.presentation_snapshot
    )
    issuer = presentation["issuer"]
    wording = presentation["wording"]
    address_parts = [
        billing_snapshot.get("organization_name"),
        billing_snapshot.get("contact_name"),
        billing_snapshot.get("address_line_1"),
        billing_snapshot.get("address_line_2"),
        " ".join(
            part
            for part in (
                billing_snapshot.get("postal_code"),
                billing_snapshot.get("city"),
            )
            if part
        ),
        billing_snapshot.get("country"),
    ]

    story = []
    logo = invoice_logo(presentation)
    if logo:
        story.extend([logo, Spacer(1, 3 * mm)])
    story.extend(
        [
            Paragraph(f"{escape(issuer['legal_name'])} Invoice", styles["Title"]),
            Spacer(1, 4 * mm),
        ]
    )
    if issuer["address"]:
        story.append(
            Paragraph(
                "<br/>".join(
                    escape(part.strip())
                    for part in issuer["address"].splitlines()
                    if part.strip()
                )
            )
        )
    if issuer["tax_registration"]:
        story.append(
            Paragraph(f"Tax registration: {escape(issuer['tax_registration'])}")
        )
    if issuer["support_email"]:
        story.append(Paragraph(f"Support: {escape(issuer['support_email'])}"))
    story.extend(
        [
            Spacer(1, 6 * mm),
            Paragraph(
                f"Invoice number: {escape(invoice.invoice_number)}",
                styles["Heading2"],
            ),
            Paragraph(f"Issue date: {invoice.issue_date}"),
            Paragraph(f"Due date: {invoice.due_date}"),
            Paragraph(
                f"Billing period: {invoice.billing_period_start:%Y-%m-%d} to "
                f"{invoice.billing_period_end:%Y-%m-%d}"
            ),
            Spacer(1, 6 * mm),
            Paragraph("Bill to", styles["Heading2"]),
            Paragraph("<br/>".join(escape(part) for part in address_parts if part)),
        ]
    )
    if billing_snapshot.get("tax_id"):
        story.append(Paragraph(f"Tax ID: {escape(billing_snapshot['tax_id'])}"))
    story.append(Spacer(1, 6 * mm))

    grouped_lines = defaultdict(list)
    invoice_lines = list(lines if lines is not None else invoice.lines.all())
    for line in invoice_lines:
        grouped_lines[invoice_section_for_line_type(line.line_type)].append(line)

    for section in presentation["sections"]:
        section_lines = grouped_lines[section["key"]]
        section_total = sum((line.amount for line in section_lines), Decimal("0.00"))
        if not section_lines and not presentation["show_zero_value_sections"]:
            continue
        if (
            section_total == Decimal("0.00")
            and not presentation["show_zero_value_sections"]
        ):
            continue
        story.append(Paragraph(escape(section["label"]), styles["Heading2"]))
        if section_lines:
            story.append(invoice_line_table(section_lines, invoice, styles))
        else:
            story.append(Paragraph("No charges in this section."))
        story.append(Spacer(1, 4 * mm))

    story.extend(
        [
            Paragraph(f"Subtotal: {invoice.subtotal} {invoice.currency}"),
            Paragraph(f"Tax: {invoice.tax} {invoice.currency}"),
            Paragraph(
                f"<b>Total: {invoice.total} {invoice.currency}</b>",
                styles["Heading2"],
            ),
            Paragraph(f"Payment status: {invoice.get_payment_state_display()}"),
        ]
    )
    if wording["due_date_text"]:
        story.append(Paragraph(escape(wording["due_date_text"])))
    if wording["payment_instructions"]:
        story.extend(
            [
                Spacer(1, 4 * mm),
                Paragraph("Payment instructions", styles["Heading2"]),
                Paragraph(escape(wording["payment_instructions"])),
            ]
        )
    if wording["terms"]:
        story.extend(
            [
                Spacer(1, 4 * mm),
                Paragraph("Terms", styles["Heading2"]),
                Paragraph(escape(wording["terms"])),
            ]
        )
    if wording["footer"]:
        story.extend([Spacer(1, 6 * mm), Paragraph(escape(wording["footer"]))])

    document.build(story)
    return output.getvalue()
