import base64
import mimetypes
from eventyay.base.settings import GlobalSettingsObject
from io import BytesIO
from PIL import Image as PillowImage

from .models import InvoiceLineType

DEFAULT_SECTION_ORDER = (
    "plans",
    "addons",
    "ticket_fees",
    "adjustments",
    "taxes",
)
DEFAULT_SECTION_LABELS = {
    "plans": "Plans",
    "addons": "Add-ons",
    "ticket_fees": "Ticket fees and usage",
    "adjustments": "Credits and adjustments",
    "taxes": "Taxes",
}
LINE_TYPE_SECTIONS = {
    InvoiceLineType.SUBSCRIPTION: "plans",
    InvoiceLineType.ADDON: "addons",
    InvoiceLineType.PLATFORM_FEE: "ticket_fees",
    InvoiceLineType.REGISTRATION_OVERAGE: "ticket_fees",
    InvoiceLineType.MANUAL_ADJUSTMENT: "adjustments",
    InvoiceLineType.CREDIT: "adjustments",
    InvoiceLineType.OTHER: "adjustments",
    InvoiceLineType.TAX: "taxes",
}


def parse_section_order(value: str | None) -> tuple[str, ...]:
    keys = tuple(part.strip() for part in (value or "").split(",") if part.strip())
    if len(keys) == len(DEFAULT_SECTION_ORDER) and set(keys) == set(
        DEFAULT_SECTION_ORDER
    ):
        return keys
    return DEFAULT_SECTION_ORDER


def invoice_section_for_line_type(line_type: str) -> str:
    return LINE_TYPE_SECTIONS.get(line_type, "adjustments")


def snapshot_invoice_logo(settings_proxy) -> dict | None:
    try:
        logo_file = settings_proxy.get("business_invoice_logo", binary_file=True)
    except (OSError, ValueError):
        return None
    if not logo_file:
        return None

    try:
        logo_data = logo_file.read()
        PillowImage.open(BytesIO(logo_data)).verify()
    except (OSError, ValueError):
        return None
    content_type = mimetypes.guess_type(getattr(logo_file, "name", ""))[0]
    return {
        "content_base64": base64.b64encode(logo_data).decode("ascii"),
        "content_type": content_type or "image/png",
    }


def current_invoice_presentation_snapshot() -> dict:
    settings_proxy = GlobalSettingsObject().settings
    section_order = parse_section_order(
        settings_proxy.get(
            "business_invoice_section_order",
            as_type=str,
            default=",".join(DEFAULT_SECTION_ORDER),
        )
    )
    sections = [
        {
            "key": key,
            "label": settings_proxy.get(
                f"business_invoice_section_label_{key}",
                as_type=str,
                default=DEFAULT_SECTION_LABELS[key],
            )
            or DEFAULT_SECTION_LABELS[key],
        }
        for key in section_order
    ]
    return {
        "version": 1,
        "issuer": {
            "legal_name": settings_proxy.get(
                "business_invoice_issuer_legal_name",
                as_type=str,
                default="Eventyay",
            )
            or "Eventyay",
            "address": settings_proxy.get(
                "business_invoice_issuer_address", as_type=str, default=""
            )
            or "",
            "tax_registration": settings_proxy.get(
                "business_invoice_issuer_tax_registration",
                as_type=str,
                default="",
            )
            or "",
            "support_email": settings_proxy.get(
                "business_invoice_support_email", as_type=str, default=""
            )
            or "",
        },
        "wording": {
            "payment_instructions": settings_proxy.get(
                "business_invoice_payment_instructions", as_type=str, default=""
            )
            or "",
            "due_date_text": settings_proxy.get(
                "business_invoice_due_date_text",
                as_type=str,
                default="Payment is collected automatically on the due date.",
            )
            or "",
            "footer": settings_proxy.get(
                "business_invoice_footer", as_type=str, default=""
            )
            or "",
            "terms": settings_proxy.get(
                "business_invoice_terms", as_type=str, default=""
            )
            or "",
        },
        "sections": sections,
        "show_zero_value_sections": settings_proxy.get(
            "business_invoice_show_zero_value_sections",
            as_type=bool,
            default=False,
        ),
        "logo": snapshot_invoice_logo(settings_proxy),
    }


def normalized_invoice_presentation_snapshot(snapshot: dict | None) -> dict:
    current = snapshot or {}
    issuer = current.get("issuer") if isinstance(current.get("issuer"), dict) else {}
    wording = current.get("wording") if isinstance(current.get("wording"), dict) else {}
    configured_sections = current.get("sections")
    labels = dict(DEFAULT_SECTION_LABELS)
    order = []
    if isinstance(configured_sections, list):
        for section in configured_sections:
            if not isinstance(section, dict):
                continue
            key = section.get("key")
            if key not in DEFAULT_SECTION_ORDER or key in order:
                continue
            order.append(key)
            label = section.get("label")
            if isinstance(label, str) and label.strip():
                labels[key] = label.strip()
    if set(order) != set(DEFAULT_SECTION_ORDER):
        order = list(DEFAULT_SECTION_ORDER)

    logo = current.get("logo")
    if not isinstance(logo, dict) or not isinstance(logo.get("content_base64"), str):
        logo = None
    return {
        "version": 1,
        "issuer": {
            "legal_name": issuer.get("legal_name") or "Eventyay",
            "address": issuer.get("address") or "",
            "tax_registration": issuer.get("tax_registration") or "",
            "support_email": issuer.get("support_email") or "",
        },
        "wording": {
            "payment_instructions": wording.get("payment_instructions") or "",
            "due_date_text": wording.get("due_date_text")
            or "Payment is collected automatically on the due date.",
            "footer": wording.get("footer") or "",
            "terms": wording.get("terms") or "",
        },
        "sections": [{"key": key, "label": labels[key]} for key in order],
        "show_zero_value_sections": bool(
            current.get("show_zero_value_sections", False)
        ),
        "logo": logo,
    }
