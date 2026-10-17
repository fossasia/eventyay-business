import logging
import smtplib
from dataclasses import dataclass
from decimal import Decimal

from celery.exceptions import CeleryError
from django.conf import settings
from django.db import transaction
from django.template import TemplateDoesNotExist
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from eventyay.base.i18n import language
from eventyay.base.models import User
from eventyay.base.models.organizer import OrganizerBillingModel
from eventyay.base.services.mail import SendMailException, mail
from eventyay.base.settings import GlobalSettingsObject
from eventyay.multidomain.urlreverse import build_absolute_uri

from .models import (
    BillingNotificationDelivery,
    BillingNotificationEventType,
    BillingNotificationRecipientSource,
    BillingNotificationStatus,
    BusinessInvoice,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BillingNotificationSpec:
    subject: str
    template_name: str


@dataclass(frozen=True)
class BillingNotificationRecipient:
    email: str
    locale: str
    source: BillingNotificationRecipientSource


NOTIFICATION_SPECS = {
    BillingNotificationEventType.INVOICE_ISSUED: BillingNotificationSpec(
        subject=_("Eventyay invoice {invoice_number}"),
        template_name="eventyay_business/email/invoice_issued.txt",
    ),
    BillingNotificationEventType.COLLECTION_PROBLEM: BillingNotificationSpec(
        subject=_("Action required for Eventyay invoice {invoice_number}"),
        template_name="eventyay_business/email/collection_problem.txt",
    ),
    BillingNotificationEventType.COLLECTION_SUCCEEDED: BillingNotificationSpec(
        subject=_("Eventyay invoice {invoice_number} paid"),
        template_name="eventyay_business/email/collection_succeeded.txt",
    ),
    BillingNotificationEventType.ADMIN_TEST: BillingNotificationSpec(
        subject=_("Eventyay Business notification test"),
        template_name="eventyay_business/email/admin_test.txt",
    ),
}

DELIVERY_EXCEPTIONS = (
    CeleryError,
    OSError,
    SendMailException,
    smtplib.SMTPException,
    TemplateDoesNotExist,
    TypeError,
    ValueError,
)


def organizer_admin_recipient(organizer) -> BillingNotificationRecipient | None:
    user = (
        User.objects.filter(
            teams__organizer=organizer,
            teams__can_change_organizer_settings=True,
            is_active=True,
        )
        .exclude(email="")
        .order_by("pk")
        .distinct()
        .first()
    )
    if not user:
        return None
    return BillingNotificationRecipient(
        email=user.email.strip().lower(),
        locale=user.locale or settings.LANGUAGE_CODE,
        source=BillingNotificationRecipientSource.ORGANIZER_ADMIN,
    )


def organizer_billing_recipient(organizer) -> BillingNotificationRecipient | None:
    billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
    if billing and billing.primary_contact_email:
        return BillingNotificationRecipient(
            email=billing.primary_contact_email.strip().lower(),
            locale=billing.preferred_language or settings.LANGUAGE_CODE,
            source=BillingNotificationRecipientSource.BILLING_CONTACT,
        )
    return organizer_admin_recipient(organizer)


def invoice_billing_recipient(
    invoice: BusinessInvoice,
) -> BillingNotificationRecipient | None:
    snapshot_email = invoice.billing_address_snapshot.get("contact_email")
    billing = OrganizerBillingModel.objects.filter(organizer=invoice.organizer).first()
    if snapshot_email:
        return BillingNotificationRecipient(
            email=snapshot_email.strip().lower(),
            locale=(
                billing.preferred_language
                if billing and billing.preferred_language
                else settings.LANGUAGE_CODE
            ),
            source=BillingNotificationRecipientSource.INVOICE_SNAPSHOT,
        )
    return organizer_billing_recipient(invoice.organizer)


def invoice_notification_context(invoice: BusinessInvoice) -> dict:
    support_email = GlobalSettingsObject().settings.get(
        "business_email_support_contact", as_type=str, default=""
    )
    return {
        "organizer_name": invoice.organizer.name,
        "invoice_number": invoice.invoice_number,
        "amount": str(invoice.total),
        "currency": invoice.currency,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else "",
        "action_url": build_absolute_uri(
            invoice.organizer,
            "plugins:eventyay_business:organizer.invoices.detail",
            kwargs={"pk": invoice.pk},
        ),
        "support_email": support_email,
    }


def render_notification_subject(
    spec: BillingNotificationSpec, context: dict, locale: str
) -> str:
    with language(locale):
        return str(spec.subject).format_map(context)


def schedule_billing_notification(delivery_id: int) -> None:
    from .tasks import deliver_billing_notification_task

    def dispatch() -> None:
        delay = getattr(deliver_billing_notification_task, "delay", None)
        if delay:
            delay(delivery_id)
        else:
            deliver_billing_notification_task(delivery_id)

    transaction.on_commit(dispatch, robust=True)


def queue_billing_notification(
    *,
    logical_event_key: str,
    event_type: BillingNotificationEventType | str,
    organizer,
    recipient: BillingNotificationRecipient,
    context: dict,
    related_object_type: str = "",
    related_object_id: str = "",
) -> BillingNotificationDelivery:
    spec = NOTIFICATION_SPECS[BillingNotificationEventType(event_type)]
    defaults = {
        "event_type": event_type,
        "organizer": organizer,
        "related_object_type": related_object_type,
        "related_object_id": str(related_object_id),
        "recipient_source": recipient.source,
        "locale": recipient.locale,
        "template_name": spec.template_name,
        "subject": render_notification_subject(spec, context, recipient.locale),
        "context": context,
    }
    with transaction.atomic():
        delivery, created = BillingNotificationDelivery.objects.get_or_create(
            logical_event_key=logical_event_key,
            recipient=recipient.email.strip().lower(),
            defaults=defaults,
        )
        should_schedule = created or delivery.status in (
            BillingNotificationStatus.PENDING,
            BillingNotificationStatus.FAILED,
        )
        if should_schedule:
            schedule_billing_notification(delivery.pk)
    return delivery


def queue_invoice_notification(
    invoice: BusinessInvoice, event_type: BillingNotificationEventType | str
) -> BillingNotificationDelivery | None:
    recipient = invoice_billing_recipient(invoice)
    if not recipient:
        logger.warning(
            "Business invoice %s has no notification recipient",
            invoice.invoice_number,
        )
        return None
    event_type = BillingNotificationEventType(event_type)
    state_suffix = (
        f":{invoice.payment_state}"
        if event_type == BillingNotificationEventType.COLLECTION_PROBLEM
        else ""
    )
    return queue_billing_notification(
        logical_event_key=f"invoice:{invoice.pk}:{event_type}{state_suffix}",
        event_type=event_type,
        organizer=invoice.organizer,
        recipient=recipient,
        context=invoice_notification_context(invoice),
        related_object_type="business_invoice",
        related_object_id=str(invoice.pk),
    )


def sanitized_delivery_error(error: BaseException) -> dict:
    return {
        "type": type(error).__name__,
        "message": "Email delivery failed. Review the application mail logs.",
    }


def deliver_billing_notification(delivery_id: int) -> BillingNotificationDelivery:
    with transaction.atomic():
        delivery = BillingNotificationDelivery.objects.select_for_update().get(
            pk=delivery_id
        )
        if delivery.status in (
            BillingNotificationStatus.SENDING,
            BillingNotificationStatus.SENT,
        ):
            return delivery
        delivery.status = BillingNotificationStatus.SENDING
        delivery.attempt_count += 1
        delivery.last_attempt_at = now()
        delivery.last_error = {}
        delivery.save(
            update_fields=[
                "status",
                "attempt_count",
                "last_attempt_at",
                "last_error",
                "updated_at",
            ]
        )

    global_settings = GlobalSettingsObject().settings
    sender = global_settings.get("business_email_sender", as_type=str) or None
    reply_to = global_settings.get("business_email_reply_to", as_type=str) or None
    headers = {"Reply-To": reply_to} if reply_to else None
    try:
        mail(
            delivery.recipient,
            delivery.subject,
            delivery.template_name,
            delivery.context,
            event=None,
            locale=delivery.locale,
            sender=sender,
            headers=headers,
            sync_send=True,
        )
    except DELIVERY_EXCEPTIONS as exc:
        BillingNotificationDelivery.objects.filter(
            pk=delivery.pk,
            status=BillingNotificationStatus.SENDING,
        ).update(
            status=BillingNotificationStatus.FAILED,
            last_error=sanitized_delivery_error(exc),
            updated_at=now(),
        )
        logger.warning(
            "Billing notification delivery %s failed with %s",
            delivery.pk,
            type(exc).__name__,
        )
    else:
        BillingNotificationDelivery.objects.filter(
            pk=delivery.pk,
            status=BillingNotificationStatus.SENDING,
        ).update(
            status=BillingNotificationStatus.SENT,
            sent_at=now(),
            last_error={},
            updated_at=now(),
        )
    delivery.refresh_from_db()
    return delivery


def retry_billing_notification(
    delivery: BillingNotificationDelivery,
) -> BillingNotificationDelivery:
    if delivery.status in (
        BillingNotificationStatus.PENDING,
        BillingNotificationStatus.FAILED,
    ):
        schedule_billing_notification(delivery.pk)
    return delivery


def preview_notification_context(_event_type: str) -> dict:
    today = now().date()
    return {
        "organizer_name": "Example Organizer",
        "invoice_number": "INV-EXAMPLE-001",
        "amount": str(Decimal("49.00")),
        "currency": "EUR",
        "issue_date": today.isoformat(),
        "action_url": settings.SITE_URL.rstrip("/") + "/control/",
        "support_email": GlobalSettingsObject().settings.get(
            "business_email_support_contact", as_type=str, default=""
        ),
    }
