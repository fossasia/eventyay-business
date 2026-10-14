from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape


def render_invoice_pdf(invoice) -> bytes:
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
    snapshot = invoice.billing_address_snapshot
    address_parts = [
        snapshot.get("organization_name"),
        snapshot.get("contact_name"),
        snapshot.get("address_line_1"),
        snapshot.get("address_line_2"),
        " ".join(
            part for part in (snapshot.get("postal_code"), snapshot.get("city")) if part
        ),
        snapshot.get("country"),
    ]

    story = [
        Paragraph("Eventyay Business Invoice", styles["Title"]),
        Spacer(1, 8 * mm),
        Paragraph(
            f"Invoice number: {escape(invoice.invoice_number)}", styles["Heading2"]
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
    if snapshot.get("tax_id"):
        story.append(Paragraph(f"Tax ID: {escape(snapshot['tax_id'])}"))
    story.append(Spacer(1, 8 * mm))

    rows = [["Description", "Service period", "Qty", "Unit price", "Amount"]]
    for line in invoice.lines.all():
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
    story.extend(
        [
            table,
            Spacer(1, 6 * mm),
            Paragraph(f"Subtotal: {invoice.subtotal} {invoice.currency}"),
            Paragraph(f"Tax: {invoice.tax} {invoice.currency}"),
            Paragraph(
                f"<b>Total: {invoice.total} {invoice.currency}</b>",
                styles["Heading2"],
            ),
            Paragraph(f"Payment status: {invoice.get_payment_state_display()}"),
        ]
    )
    document.build(story)
    return output.getvalue()
