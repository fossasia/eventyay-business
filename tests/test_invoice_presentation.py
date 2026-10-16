import pytest
from datetime import datetime
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.timezone import make_aware, now
from eventyay.base.models import Organizer, User
from io import BytesIO
from pypdf import PdfReader

from eventyay_business.forms import InvoicePresentationSettingsForm
from eventyay_business.invoice_document import render_invoice_pdf
from eventyay_business.invoicing_service import finalize_business_invoice
from eventyay_business.models import (
    BusinessInvoice,
    BusinessInvoiceLine,
    InvoiceLineType,
)


def presentation_form_data(**overrides):
    data = {
        "business_invoice_issuer_legal_name": "Eventyay Billing GmbH",
        "business_invoice_issuer_address": "Example Street 1\nBerlin",
        "business_invoice_issuer_tax_registration": "DE123456789",
        "business_invoice_support_email": "billing@example.com",
        "business_invoice_payment_instructions": "Payment is collected by card.",
        "business_invoice_due_date_text": "Due within 14 days.",
        "business_invoice_footer": "Thank you for organizing with us.",
        "business_invoice_terms": "Standard billing terms apply.",
        "business_invoice_section_order": (
            "plans,addons,ticket_fees,adjustments,taxes"
        ),
        "business_invoice_section_label_plans": "Membership",
        "business_invoice_section_label_addons": "Optional services",
        "business_invoice_section_label_ticket_fees": "Ticket charges",
        "business_invoice_section_label_adjustments": "Adjustments",
        "business_invoice_section_label_taxes": "Taxes",
    }
    data.update(overrides)
    return data


@pytest.fixture
def invoice_admin_client(client, db):
    user = User.objects.create_user(
        "invoice-admin@example.com", "password", is_staff=True
    )
    client.force_login(user)
    user.staffsession_set.create(
        date_start=now(), session_key=client.session.session_key
    )
    return client


@pytest.mark.django_db
def test_invoice_settings_page_saves_and_previews_pdf(invoice_admin_client):
    settings_url = reverse("plugins:eventyay_business:settings")
    legacy_settings_url = reverse("plugins:eventyay_business:invoice_settings")

    page = invoice_admin_client.get(settings_url)
    assert page.status_code == 200
    assert b"Invoice presentation" in page.content
    assert b"eventyay_business/admin.css" in page.content
    assert b"48 \xc3\x97 22 mm" in page.content

    legacy_page = invoice_admin_client.get(legacy_settings_url)
    assert legacy_page.status_code == 302
    assert legacy_page.url == f"{settings_url}#tab-invoice_presentation"

    response = invoice_admin_client.post(settings_url, presentation_form_data())
    assert response.status_code == 302
    assert response.url == settings_url

    preview = invoice_admin_client.get(
        reverse("plugins:eventyay_business:invoice_settings.preview")
    )
    assert preview.status_code == 200
    assert preview["Content-Type"] == "application/pdf"
    assert preview.content.startswith(b"%PDF")
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(preview.content)).pages
    )
    assert "Eventyay Billing GmbH Invoice" in text
    assert "Membership" in text
    assert "Ticket charges" in text


@pytest.mark.django_db
def test_invoice_settings_reject_invalid_section_order():
    form = InvoicePresentationSettingsForm(
        data=presentation_form_data(business_invoice_section_order="plans,addons,taxes")
    )
    assert not form.is_valid()
    assert "business_invoice_section_order" in form.errors


@pytest.mark.django_db
def test_finalized_invoice_keeps_immutable_presentation_snapshot():
    form = InvoicePresentationSettingsForm(data=presentation_form_data())
    assert form.is_valid(), form.errors
    form.save()

    organizer = Organizer.objects.create(name="Example Organizer", slug="example-org")
    period_start = make_aware(datetime(2026, 9, 1))
    period_end = make_aware(datetime(2026, 9, 30, 23, 59, 59))
    invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number="INV-PRESENTATION-1",
        billing_period_start=period_start,
        billing_period_end=period_end,
        currency="EUR",
    )
    BusinessInvoiceLine.objects.create(
        invoice=invoice,
        line_type=InvoiceLineType.SUBSCRIPTION,
        description="Professional plan",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        amount=Decimal("100.00"),
        service_period_start=period_start,
        service_period_end=period_end,
    )
    invoice = finalize_business_invoice(invoice)
    assert invoice.presentation_snapshot["issuer"]["legal_name"] == (
        "Eventyay Billing GmbH"
    )

    changed_form = InvoicePresentationSettingsForm(
        data=presentation_form_data(
            business_invoice_issuer_legal_name="Changed Future Issuer",
            business_invoice_section_label_plans="Changed Future Label",
        )
    )
    assert changed_form.is_valid(), changed_form.errors
    changed_form.save()

    invoice.refresh_from_db()
    pdf_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(render_invoice_pdf(invoice))).pages
    )
    assert "Eventyay Billing GmbH Invoice" in pdf_text
    assert "Membership" in pdf_text
    assert "Changed Future Issuer" not in pdf_text
    assert "Changed Future Label" not in pdf_text

    invoice.presentation_snapshot = {
        **invoice.presentation_snapshot,
        "issuer": {"legal_name": "Mutated issuer"},
    }
    with pytest.raises(ValidationError, match="cannot be changed"):
        invoice.save()
