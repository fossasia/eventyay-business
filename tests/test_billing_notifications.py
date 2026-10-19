import pytest
from datetime import datetime
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.test import override_settings
from django.urls import reverse
from django.utils.timezone import make_aware, now
from eventyay.base.models import Organizer, User
from eventyay.base.models.auth import StaffSession
from eventyay.base.models.organizer import OrganizerBillingModel
from eventyay.base.services.mail import SendMailException
from eventyay.control.views.admin_messages import get_platform_mail_templates
from unittest.mock import patch

from eventyay_business.forms import BillingNotificationTemplateForm
from eventyay_business.models import (
    BillingNotificationDelivery,
    BillingNotificationEventType,
    BillingNotificationRecipientSource,
    BillingNotificationStatus,
    BusinessInvoice,
    BusinessInvoiceStatus,
    InvoicePaymentState,
    Subscription,
)
from eventyay_business.notifications import (
    CUSTOM_NOTIFICATION_TEMPLATE,
    MAX_NOTIFICATION_SUBJECT_LENGTH,
    NOTIFICATION_SPECS,
    BillingNotificationRecipient,
    deliver_billing_notification,
    invoice_billing_recipient,
    notification_template_overrides,
    notification_template_sources,
    preview_notification_context,
    queue_billing_notification,
    queue_invoice_notification,
    render_notification_content,
    save_notification_template,
)
from eventyay_business.signals import (
    platform_mail_templates,
    register_business_mail_templates,
)


def notification_template_form_data(subject, message, locale="en"):
    locale_index = [code for code, _label in settings.LANGUAGES].index(locale)
    return {
        "active_locales": locale,
        f"subject_{locale_index}": subject,
        f"message_{locale_index}": message,
    }


@pytest.fixture
def notification_setup(db):
    organizer = Organizer.objects.create(
        name="Notification Organizer", slug="notification-organizer"
    )
    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        primary_contact_name="Billing Contact",
        primary_contact_email="billing@example.com",
        company_or_organization_name="Notification Organizer",
        address_line_1="Example Street 1",
        address_line_2="",
        city="Berlin",
        zip_code="10115",
        country="DE",
        preferred_language="de",
        tax_id="",
    )
    invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number="INV-202610-0001-01",
        billing_period_start=make_aware(datetime(2026, 9, 1)),
        billing_period_end=make_aware(datetime(2026, 9, 30, 23, 59, 59)),
        currency="EUR",
        status=BusinessInvoiceStatus.OPEN,
        payment_state=InvoicePaymentState.NOT_STARTED,
        subtotal=Decimal("49.00"),
        total=Decimal("49.00"),
        issue_date=datetime(2026, 10, 1).date(),
        finalized_at=now(),
        billing_address_snapshot={"contact_email": "snapshot@example.com"},
    )
    return organizer, billing, invoice


@pytest.fixture
def business_admin_client(admin_client, admin_user):
    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save(update_fields=["is_staff"])
    return admin_client


@pytest.mark.django_db
def test_invoice_recipient_prefers_snapshot_then_billing_then_admin(
    notification_setup,
):
    organizer, billing, invoice = notification_setup

    recipient = invoice_billing_recipient(invoice)
    assert recipient.email == "snapshot@example.com"
    assert recipient.source == BillingNotificationRecipientSource.INVOICE_SNAPSHOT
    assert recipient.locale == "de"

    invoice.billing_address_snapshot = {}
    recipient = invoice_billing_recipient(invoice)
    assert recipient.email == "billing@example.com"
    assert recipient.source == BillingNotificationRecipientSource.BILLING_CONTACT

    billing.delete()
    admin = User.objects.create_user("organizer-admin@example.com", "password")
    team = organizer.teams.create(
        name="Administrators",
        all_events=True,
        can_change_organizer_settings=True,
    )
    team.members.add(admin)

    recipient = invoice_billing_recipient(invoice)
    assert recipient.email == "organizer-admin@example.com"
    assert recipient.source == BillingNotificationRecipientSource.ORGANIZER_ADMIN


@pytest.mark.django_db
def test_duplicate_logical_event_is_sent_once(notification_setup):
    organizer, _, invoice = notification_setup
    recipient = BillingNotificationRecipient(
        email="billing@example.com",
        locale="en",
        source=BillingNotificationRecipientSource.BILLING_CONTACT,
    )
    context = {
        "organizer_name": organizer.name,
        "invoice_number": invoice.invoice_number,
    }

    with patch("eventyay_business.notifications.schedule_billing_notification"):
        first = queue_billing_notification(
            logical_event_key="invoice:1:issued",
            event_type=BillingNotificationEventType.INVOICE_ISSUED,
            organizer=organizer,
            recipient=recipient,
            context=context,
        )
        second = queue_billing_notification(
            logical_event_key="invoice:1:issued",
            event_type=BillingNotificationEventType.INVOICE_ISSUED,
            organizer=organizer,
            recipient=recipient,
            context=context,
        )

    with patch("eventyay_business.notifications.mail") as send:
        deliver_billing_notification(first.pk)
        deliver_billing_notification(second.pk)

    assert first.pk == second.pk
    assert BillingNotificationDelivery.objects.count() == 1
    assert send.call_count == 1
    first.refresh_from_db()
    assert first.status == BillingNotificationStatus.SENT
    assert first.attempt_count == 1


@pytest.mark.django_db
def test_rolled_back_billing_change_neither_records_nor_dispatches(
    notification_setup,
):
    organizer, _, invoice = notification_setup
    recipient = BillingNotificationRecipient(
        email="billing@example.com",
        locale="en",
        source=BillingNotificationRecipientSource.BILLING_CONTACT,
    )

    with patch(
        "eventyay_business.tasks.deliver_billing_notification_task.delay"
    ) as dispatch, pytest.raises(RuntimeError, match="rollback"), transaction.atomic():
        queue_billing_notification(
            logical_event_key="invoice:rollback:issued",
            event_type=BillingNotificationEventType.INVOICE_ISSUED,
            organizer=organizer,
            recipient=recipient,
            context={"invoice_number": invoice.invoice_number},
        )
        raise RuntimeError("rollback")

    assert not BillingNotificationDelivery.objects.filter(
        logical_event_key="invoice:rollback:issued"
    ).exists()
    dispatch.assert_not_called()


@pytest.mark.django_db
def test_failed_delivery_is_sanitized_and_can_be_retried(notification_setup):
    _, _, invoice = notification_setup
    with patch("eventyay_business.notifications.schedule_billing_notification"):
        delivery = queue_invoice_notification(
            invoice, BillingNotificationEventType.COLLECTION_PROBLEM
        )

    with patch(
        "eventyay_business.notifications.mail",
        side_effect=SendMailException("secret SMTP response"),
    ):
        deliver_billing_notification(delivery.pk)

    delivery.refresh_from_db()
    assert delivery.status == BillingNotificationStatus.FAILED
    assert delivery.attempt_count == 1
    assert delivery.last_error == {
        "type": "SendMailException",
        "message": "Email delivery failed. Review the application mail logs.",
    }
    assert "secret" not in str(delivery.last_error)

    with patch("eventyay_business.notifications.mail") as send:
        deliver_billing_notification(delivery.pk)

    delivery.refresh_from_db()
    assert send.call_count == 1
    assert delivery.status == BillingNotificationStatus.SENT
    assert delivery.attempt_count == 2
    assert delivery.sent_at is not None
    assert delivery.last_error == {}


@pytest.mark.django_db
@override_settings(SITE_URL="https://example.test")
def test_invoice_notification_audit_contains_only_delivery_context(
    notification_setup,
):
    _, _, invoice = notification_setup
    with patch("eventyay_business.notifications.schedule_billing_notification"):
        delivery = queue_invoice_notification(
            invoice, BillingNotificationEventType.INVOICE_ISSUED
        )

    assert delivery.recipient == "snapshot@example.com"
    assert delivery.locale == "de"
    assert delivery.related_object_type == "business_invoice"
    assert delivery.related_object_id == str(invoice.pk)
    assert delivery.context["invoice_number"] == invoice.invoice_number
    assert "stripe" not in str(delivery.context).lower()
    assert "secret" not in str(delivery.context).lower()


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="billing@example.test",
)
def test_admin_test_send_records_delivery_without_changing_billing_state(
    business_admin_client,
    notification_setup,
    django_capture_on_commit_callbacks,
):
    organizer, _, invoice = notification_setup
    subscription = Subscription.objects.get(organizer=organizer)
    invoice_state = (
        invoice.status,
        invoice.payment_state,
        invoice.collection_attempt_count,
    )
    subscription_state = (subscription.status, subscription.ends_at)

    with patch(
        "eventyay_business.tasks.deliver_billing_notification_task.delay"
    ) as dispatch, django_capture_on_commit_callbacks(execute=True):
        response = business_admin_client.post(
            reverse("plugins:eventyay_business:notifications.preview"),
            {
                "organizer": organizer.pk,
                "event_type": BillingNotificationEventType.ADMIN_TEST,
                "recipient": "test-recipient@example.com",
                "locale": "en",
            },
        )

    assert response.status_code == 302
    delivery = BillingNotificationDelivery.objects.get(
        event_type=BillingNotificationEventType.ADMIN_TEST
    )
    dispatch.assert_called_once_with(delivery.pk)
    assert delivery.recipient == "test-recipient@example.com"
    deliver_billing_notification(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.status == BillingNotificationStatus.SENT
    invoice.refresh_from_db()
    subscription.refresh_from_db()
    assert (
        invoice.status,
        invoice.payment_state,
        invoice.collection_attempt_count,
    ) == invoice_state
    assert (subscription.status, subscription.ends_at) == subscription_state


@pytest.mark.django_db
def test_delivery_history_uses_trimmed_organizer_filter(
    business_admin_client, notification_setup
):
    organizer, _, invoice = notification_setup
    with patch("eventyay_business.notifications.schedule_billing_notification"):
        queue_invoice_notification(invoice, BillingNotificationEventType.INVOICE_ISSUED)

    response = business_admin_client.get(
        reverse("plugins:eventyay_business:notifications.list"),
        {"organizer": f"  {organizer.slug}  "},
    )

    assert response.status_code == 200
    assert response.context["current_organizer"] == organizer.slug
    assert invoice.invoice_number.encode() not in response.content
    assert b"snapshot@example.com" in response.content


@pytest.mark.django_db
def test_invoice_list_links_to_notification_template_editor(business_admin_client):
    response = business_admin_client.get(
        reverse("plugins:eventyay_business:invoices.list")
    )

    assert response.status_code == 200
    assert (
        reverse("plugins:eventyay_business:notifications.preview").encode()
        in response.content
    )
    assert b"Customize email templates" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("event_type", NOTIFICATION_SPECS)
def test_all_billing_notification_templates_render(event_type):
    spec = NOTIFICATION_SPECS[event_type]
    body = render_to_string(
        spec.template_name,
        preview_notification_context(event_type),
    )

    assert body.strip()


def test_notification_template_form_rejects_unknown_placeholders():
    form = BillingNotificationTemplateForm(
        data=notification_template_form_data(
            "Invoice {invoice_number}", "Hello {unknown_value}"
        )
    )

    assert not form.is_valid()
    assert "Invalid placeholder" in str(form.errors["message"])


def test_notification_template_form_uses_shared_email_editor():
    form = BillingNotificationTemplateForm()

    message_widget = form.fields["message"].widget.widgets[0]
    assert message_widget.attrs["data-tiptap-profile"] == "email"
    assert "invoice_number" in message_widget.attrs["data-tiptap-placeholders"]


@pytest.mark.django_db
def test_business_notifications_are_registered_in_message_center():
    templates = register_business_mail_templates(sender=None)

    assert len(templates) == len(BillingNotificationEventType.choices)
    assert {str(template["category"]) for template in templates} == {"Business"}
    invoice_template = next(
        template
        for template in templates
        if template["key"] == f"business-{BillingNotificationEventType.INVOICE_ISSUED}"
    )
    assert invoice_template["url"] == reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": BillingNotificationEventType.INVOICE_ISSUED},
    )


@pytest.mark.django_db
@pytest.mark.skipif(
    platform_mail_templates is None,
    reason="The installed core version does not expose the platform template registry",
)
def test_message_center_lists_business_notification_templates(business_admin_client):
    templates = get_platform_mail_templates()
    invoice_template = next(
        template
        for template in templates
        if template["key"] == f"business-{BillingNotificationEventType.INVOICE_ISSUED}"
    )

    assert str(invoice_template["category"]) == "Business"
    edit_url = reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": BillingNotificationEventType.INVOICE_ISSUED},
    )
    assert invoice_template["url"] == edit_url

    response = business_admin_client.get(
        reverse("eventyay_admin:admin.messages.templates")
    )
    assert response.status_code == 200
    assert edit_url.encode() in response.content
    assert b"Business" in response.content


@pytest.mark.django_db
def test_custom_notification_template_is_previewed_and_snapshotted(
    business_admin_client, notification_setup
):
    organizer, _, _invoice = notification_setup
    event_type = BillingNotificationEventType.INVOICE_ISSUED
    edit_url = reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": event_type},
    )

    response = business_admin_client.post(
        edit_url,
        notification_template_form_data(
            "Invoice {invoice_number} is ready",
            "Hello {organizer_name}, view {invoice_number} at {action_url}",
        ),
    )

    assert response.status_code == 302
    preview = business_admin_client.get(
        reverse("plugins:eventyay_business:notifications.preview"), {"locale": "en"}
    )
    assert b"Invoice INV-EXAMPLE-001 is ready" in preview.content
    assert b"Hello Example Organizer" in preview.content

    recipient = BillingNotificationRecipient(
        email="billing@example.com",
        locale="en",
        source=BillingNotificationRecipientSource.BILLING_CONTACT,
    )
    with patch("eventyay_business.notifications.schedule_billing_notification"):
        delivery = queue_billing_notification(
            logical_event_key="invoice:custom-template",
            event_type=event_type,
            organizer=organizer,
            recipient=recipient,
            context=preview_notification_context(event_type),
        )

    assert delivery.subject == "Invoice INV-EXAMPLE-001 is ready"
    assert delivery.template_name == CUSTOM_NOTIFICATION_TEMPLATE
    assert delivery.context["notification_body"].startswith("Hello Example Organizer")


@pytest.mark.django_db
def test_notification_template_reset_restores_default(
    business_admin_client, notification_setup
):
    event_type = BillingNotificationEventType.ADMIN_TEST
    edit_url = reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": event_type},
    )
    business_admin_client.post(
        edit_url,
        notification_template_form_data("Custom subject", "Custom body"),
    )

    response = business_admin_client.post(
        edit_url,
        {
            "action": "reset",
        },
    )

    assert response.status_code == 302
    edit_page = business_admin_client.get(edit_url, {"locale": "en"})
    assert b"Custom subject" not in edit_page.content
    assert b"Eventyay Business notification test" in edit_page.content


@pytest.mark.django_db
def test_notification_template_uses_core_multilingual_editor(
    business_admin_client, notification_setup
):
    event_type = BillingNotificationEventType.ADMIN_TEST
    edit_url = reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": event_type},
    )
    form_data = notification_template_form_data("English custom", "English body")
    form_data.update(
        notification_template_form_data("Deutscher Betreff", "Deutscher Text", "de")
    )
    form_data["active_locales"] = "en,de"
    business_admin_client.post(edit_url, form_data)

    response = business_admin_client.get(edit_url)

    assert response.status_code == 200
    assert response.context["form"].initial["subject"]["en"] == "English custom"
    assert response.context["form"].initial["subject"]["de"] == "Deutscher Betreff"
    assert b'id="mail-language-tabs"' in response.content
    assert b'id="add-language-btn"' in response.content
    assert b'lang="en"' in response.content
    assert b'lang="de"' in response.content
    assert b"pretixcontrol/js/admin-mail-language-manager.js" in response.content
    assert b'data-tiptap-profile="email"' in response.content
    subject, body, is_custom = render_notification_content(event_type, {}, "de")
    assert subject == "Deutscher Betreff"
    assert body == "Deutscher Text"
    assert is_custom is True


@pytest.mark.django_db
def test_notification_template_prefills_inactive_language_defaults(
    business_admin_client, notification_setup
):
    event_type = BillingNotificationEventType.ADDON_ACTIVATED
    edit_url = reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": event_type},
    )

    response = business_admin_client.get(edit_url)
    default_subject, default_body, _is_custom = notification_template_sources(
        event_type, "de"
    )

    assert response.status_code == 200
    assert response.context["form"].initial["active_locales"] == "en"
    assert response.context["form"].initial["subject"]["de"] == default_subject
    assert response.context["form"].initial["message"]["de"] == default_body
    assert default_subject.strip()
    assert default_body.strip()


@pytest.mark.django_db
def test_notification_template_does_not_store_unchanged_defaults(
    business_admin_client, notification_setup
):
    event_type = BillingNotificationEventType.ADDON_ACTIVATED
    edit_url = reverse(
        "plugins:eventyay_business:notifications.template",
        kwargs={"event_type": event_type},
    )
    english_subject, english_body, _is_custom = notification_template_sources(
        event_type, "en"
    )
    _german_subject, german_body, _is_custom = notification_template_sources(
        event_type, "de"
    )
    form_data = notification_template_form_data(english_subject, english_body)
    form_data.update(
        notification_template_form_data("Deutscher Betreff", german_body, "de")
    )
    form_data["active_locales"] = "en,de"

    for _attempt in range(2):
        response = business_admin_client.post(edit_url, form_data)

        assert response.status_code == 302
        assert notification_template_overrides(event_type, "en") == ("", "")
        assert notification_template_overrides(event_type, "de") == (
            "Deutscher Betreff",
            "",
        )


@pytest.mark.django_db
def test_test_notification_preserves_selected_language(
    business_admin_client, notification_setup
):
    organizer, _, _invoice = notification_setup

    with patch("eventyay_business.notifications.schedule_billing_notification"):
        response = business_admin_client.post(
            reverse("plugins:eventyay_business:notifications.preview"),
            {
                "organizer": organizer.pk,
                "event_type": BillingNotificationEventType.ADMIN_TEST,
                "recipient": "test@example.com",
                "locale": "de",
            },
        )

    assert response.status_code == 302
    assert response.url.endswith("?locale=de")

    preview = business_admin_client.get(response.url)
    assert preview.context["preview_locale"] == "de"
    assert preview.context["form"].initial["locale"] == "de"


@pytest.mark.django_db
def test_rendered_notification_subject_is_truncated_to_model_limit(
    notification_setup,
):
    organizer, _, _invoice = notification_setup
    event_type = BillingNotificationEventType.ADMIN_TEST
    save_notification_template(
        event_type,
        "en",
        "Notice for {organizer_name}",
        "Notification body",
    )
    context = preview_notification_context(event_type)
    context["organizer_name"] = "A" * 300
    recipient = BillingNotificationRecipient(
        email="billing@example.com",
        locale="en",
        source=BillingNotificationRecipientSource.BILLING_CONTACT,
    )

    with patch("eventyay_business.notifications.schedule_billing_notification"):
        delivery = queue_billing_notification(
            logical_event_key="admin-test:long-subject",
            event_type=event_type,
            organizer=organizer,
            recipient=recipient,
            context=context,
        )

    assert len(delivery.subject) == MAX_NOTIFICATION_SUBJECT_LENGTH
    assert (
        delivery.subject
        == ("Notice for " + "A" * 300)[:MAX_NOTIFICATION_SUBJECT_LENGTH]
    )
