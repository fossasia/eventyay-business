import logging
import smtplib
from celery.exceptions import CeleryError
from dataclasses import dataclass
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.formats import date_format
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
    EventAddon,
    OrganizerAddon,
    Subscription,
)

logger = logging.getLogger(__name__)

CUSTOM_NOTIFICATION_TEMPLATE = "eventyay_business/email/custom_notification.txt"
MAX_NOTIFICATION_SUBJECT_LENGTH = 255
NOTIFICATION_PLACEHOLDERS = {
    "organizer_name": _("Organizer name"),
    "object_name": _("Plan or add-on name"),
    "event_name": _("Event name"),
    "invoice_number": _("Invoice number"),
    "amount": _("Amount"),
    "currency": _("Currency"),
    "issue_date": _("Invoice issue date"),
    "effective_date": _("Effective or renewal date"),
    "deadline": _("Payment or cancellation deadline"),
    "next_retry_at": _("Next payment retry date"),
    "days_remaining": _("Days remaining"),
    "current_state": _("Current billing state"),
    "failure_code": _("Platform failure code"),
    "action_url": _("Relevant Eventyay page"),
    "support_email": _("Configured billing support address"),
}


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
    BillingNotificationEventType.PAYMENT_METHOD_MISSING: BillingNotificationSpec(
        subject=_("Payment method needed for Eventyay invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.AUTHORIZATION_MISSING: BillingNotificationSpec(
        subject=_("Automatic payment is off for invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PAYMENT_DECLINED: BillingNotificationSpec(
        subject=_("Payment declined for Eventyay invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.AUTHENTICATION_REQUIRED: BillingNotificationSpec(
        subject=_("Confirm payment for Eventyay invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.RETRY_SCHEDULED: BillingNotificationSpec(
        subject=_("Payment retry scheduled for invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.GRACE_PERIOD_STARTED: BillingNotificationSpec(
        subject=_("Eventyay billing grace period started"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.GRACE_PERIOD_REMINDER: BillingNotificationSpec(
        subject=_("{days_remaining} days remain in your Eventyay billing grace period"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.ACCESS_EXPIRED: BillingNotificationSpec(
        subject=_("Eventyay paid access expired for nonpayment"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PAYMENT_METHOD_UPDATED: BillingNotificationSpec(
        subject=_("Eventyay payment method updated"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.ACCESS_RESTORED: BillingNotificationSpec(
        subject=_("Eventyay paid access restored"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.SUPPORT_REQUIRED: BillingNotificationSpec(
        subject=_("Support is needed for invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLATFORM_ALERT: BillingNotificationSpec(
        subject=_("Eventyay platform billing alert for {organizer_name}"),
        template_name="eventyay_business/email/platform_alert.txt",
    ),
    BillingNotificationEventType.PLAN_ACTIVATED: BillingNotificationSpec(
        subject=_("Your Eventyay {object_name} plan is active"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLAN_CHANGE_SCHEDULED: BillingNotificationSpec(
        subject=_("Your Eventyay plan change is scheduled"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLAN_CHANGE_CANCELED: BillingNotificationSpec(
        subject=_("Your scheduled Eventyay plan change was canceled"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLAN_CHANGED: BillingNotificationSpec(
        subject=_("Your Eventyay plan changed to {object_name}"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLAN_CANCELLATION_SCHEDULED: BillingNotificationSpec(
        subject=_("Your Eventyay plan cancellation is scheduled"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLAN_CANCELLATION_CANCELED: BillingNotificationSpec(
        subject=_("Your Eventyay plan cancellation was canceled"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.PLAN_CANCELED: BillingNotificationSpec(
        subject=_("Your Eventyay paid plan ended"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.ADDON_ACTIVATED: BillingNotificationSpec(
        subject=_("Eventyay add-on {object_name} is active"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.ADDON_CANCELLATION_SCHEDULED: BillingNotificationSpec(
        subject=_("Eventyay add-on {object_name} cancellation is scheduled"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.ADDON_CANCELED: BillingNotificationSpec(
        subject=_("Eventyay add-on {object_name} ended"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.ADDON_EXPIRED: BillingNotificationSpec(
        subject=_("Eventyay add-on {object_name} expired"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.RENEWAL_APPROACHING: BillingNotificationSpec(
        subject=_("Eventyay {object_name} renews soon"),
        template_name="eventyay_business/email/lifecycle.txt",
    ),
    BillingNotificationEventType.CREDIT_ISSUED: BillingNotificationSpec(
        subject=_("Credit or adjustment issued for invoice {invoice_number}"),
        template_name="eventyay_business/email/lifecycle.txt",
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


def platform_admin_recipient() -> BillingNotificationRecipient | None:
    user = (
        User.objects.filter(is_active=True)
        .filter(Q(is_staff=True) | Q(is_superuser=True))
        .exclude(email="")
        .order_by("pk")
        .first()
    )
    if not user:
        return None
    return BillingNotificationRecipient(
        email=user.email.strip().lower(),
        locale=user.locale or settings.LANGUAGE_CODE,
        source=BillingNotificationRecipientSource.PLATFORM_ADMIN,
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


class NotificationFormatContext(dict):
    def __missing__(self, key):
        return ""


def format_notification_template(template: str, context: dict) -> str:
    return template.format_map(NotificationFormatContext(context))


def notification_template_setting_key(
    event_type: BillingNotificationEventType | str, locale: str, field: str
) -> str:
    return f"business_notification_template_{event_type}_{locale}_{field}"


def notification_template_sources(
    event_type: BillingNotificationEventType | str, locale: str
) -> tuple[str, str, bool]:
    event_type = BillingNotificationEventType(event_type)
    spec = NOTIFICATION_SPECS[event_type]
    custom_subject, custom_body = notification_template_overrides(event_type, locale)
    placeholder_context = {name: "{" + name + "}" for name in NOTIFICATION_PLACEHOLDERS}
    placeholder_context["event_type"] = event_type
    with language(locale):
        default_subject = str(spec.subject)
        default_body = render_to_string(spec.template_name, placeholder_context)
    return (
        custom_subject or default_subject,
        custom_body or default_body,
        bool(custom_subject or custom_body),
    )


def notification_template_overrides(
    event_type: BillingNotificationEventType | str, locale: str
) -> tuple[str, str]:
    global_settings = GlobalSettingsObject().settings
    return tuple(
        global_settings.get(
            notification_template_setting_key(event_type, locale, field),
            as_type=str,
            default="",
        )
        for field in ("subject", "body")
    )


def render_notification_content(
    event_type: BillingNotificationEventType | str, context: dict, locale: str
) -> tuple[str, str | None, bool]:
    event_type = BillingNotificationEventType(event_type)
    spec = NOTIFICATION_SPECS[event_type]
    global_settings = GlobalSettingsObject().settings
    custom_subject = global_settings.get(
        notification_template_setting_key(event_type, locale, "subject"),
        as_type=str,
        default="",
    )
    custom_body = global_settings.get(
        notification_template_setting_key(event_type, locale, "body"),
        as_type=str,
        default="",
    )
    with language(locale):
        subject = format_notification_template(
            custom_subject or str(spec.subject), context
        )
        body = (
            format_notification_template(custom_body, context) if custom_body else None
        )
    return subject, body, bool(custom_subject or custom_body)


def save_notification_template(
    event_type: BillingNotificationEventType | str,
    locale: str,
    subject: str,
    body: str,
) -> None:
    global_settings = GlobalSettingsObject().settings
    for field, value in (("subject", subject), ("body", body)):
        key = notification_template_setting_key(event_type, locale, field)
        if value:
            global_settings.set(key, value)
        elif global_settings.get(key, as_type=str, default=""):
            del global_settings[key]


def reset_notification_template(
    event_type: BillingNotificationEventType | str, locale: str
) -> None:
    global_settings = GlobalSettingsObject().settings
    for field in ("subject", "body"):
        key = notification_template_setting_key(event_type, locale, field)
        if global_settings.get(key, as_type=str, default=""):
            del global_settings[key]


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
    event_type = BillingNotificationEventType(event_type)
    spec = NOTIFICATION_SPECS[event_type]
    subject, custom_body, _is_custom = render_notification_content(
        event_type, context, recipient.locale
    )
    subject = subject[:MAX_NOTIFICATION_SUBJECT_LENGTH]
    delivery_context = context
    template_name = spec.template_name
    if custom_body is not None:
        delivery_context = {**context, "notification_body": custom_body}
        template_name = CUSTOM_NOTIFICATION_TEMPLATE
    defaults = {
        "event_type": event_type,
        "organizer": organizer,
        "related_object_type": related_object_type,
        "related_object_id": str(related_object_id),
        "recipient_source": recipient.source,
        "locale": recipient.locale,
        "template_name": template_name,
        "subject": subject,
        "context": delivery_context,
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


def format_notification_datetime(value, locale: str) -> str:
    if not value:
        return ""
    with language(locale):
        return date_format(value, "DATETIME_FORMAT")


def organizer_notification_context(
    organizer,
    event_type: BillingNotificationEventType | str,
    recipient: BillingNotificationRecipient,
    **values,
) -> dict:
    context = {
        "event_type": BillingNotificationEventType(event_type),
        "organizer_name": organizer.name,
        "object_name": "",
        "event_name": "",
        "invoice_number": "",
        "amount": "",
        "currency": "",
        "effective_date": "",
        "deadline": "",
        "next_retry_at": "",
        "days_remaining": "",
        "current_state": "",
        "action_url": build_absolute_uri(
            organizer,
            "plugins:eventyay_business:organizer.plan",
        ),
        "support_email": GlobalSettingsObject().settings.get(
            "business_email_support_contact", as_type=str, default=""
        ),
    }
    for key, value in values.items():
        if key.endswith("_at") or key in {"effective_date", "deadline"}:
            value = format_notification_datetime(value, recipient.locale)
        elif isinstance(value, Decimal):
            value = str(value)
        context[key] = value if value is not None else ""
    return context


def queue_organizer_notification(
    *,
    organizer,
    event_type: BillingNotificationEventType | str,
    logical_event_key: str,
    related_object_type: str = "organizer",
    related_object_id: str = "",
    recipient: BillingNotificationRecipient | None = None,
    **context_values,
) -> BillingNotificationDelivery | None:
    recipient = recipient or organizer_billing_recipient(organizer)
    if not recipient:
        logger.warning(
            "Organizer %s has no recipient for billing event %s",
            organizer.slug,
            event_type,
        )
        return None
    context = organizer_notification_context(
        organizer,
        event_type,
        recipient,
        **context_values,
    )
    return queue_billing_notification(
        logical_event_key=logical_event_key,
        event_type=event_type,
        organizer=organizer,
        recipient=recipient,
        context=context,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
    )


def subscription_price_context(subscription: Subscription) -> tuple[str, str]:
    if not subscription.tier_version_id:
        return "", subscription.currency or ""
    prices = subscription.tier_version.prices.filter(active=True)
    if subscription.billing_interval:
        prices = prices.filter(billing_interval=subscription.billing_interval)
    if subscription.currency:
        prices = prices.filter(currency=subscription.currency)
    price = prices.order_by("pk").first()
    return (
        (str(price.amount), price.currency)
        if price
        else ("", subscription.currency or "")
    )


def queue_subscription_notification(
    subscription: Subscription,
    event_type: BillingNotificationEventType | str,
    *,
    key_suffix: str = "",
    **context_values,
) -> BillingNotificationDelivery | None:
    recipient = organizer_billing_recipient(subscription.organizer)
    if not recipient:
        return None
    amount, currency = subscription_price_context(subscription)
    with language(recipient.locale):
        current_state = str(subscription.get_status_display())
    marker = key_suffix or subscription.updated_at.isoformat()
    object_name = context_values.pop(
        "object_name",
        (
            subscription.pending_tier_version.tier.name
            if subscription.pending_tier_version_id
            else (
                subscription.tier_version.tier.name
                if subscription.tier_version_id
                else ""
            )
        ),
    )
    effective_date = context_values.pop(
        "effective_date", subscription.pending_change_at or subscription.starts_at
    )
    deadline = context_values.pop(
        "deadline", subscription.grace_period_ends_at() or subscription.ends_at
    )
    return queue_organizer_notification(
        organizer=subscription.organizer,
        event_type=event_type,
        logical_event_key=f"subscription:{subscription.pk}:{event_type}:{marker}",
        related_object_type="subscription",
        related_object_id=str(subscription.pk),
        recipient=recipient,
        object_name=object_name,
        amount=amount,
        currency=currency,
        effective_date=effective_date,
        deadline=deadline,
        current_state=current_state,
        **context_values,
    )


def queue_addon_notification(
    assignment: OrganizerAddon | EventAddon,
    event_type: BillingNotificationEventType | str,
    *,
    key_suffix: str = "",
    **context_values,
) -> BillingNotificationDelivery | None:
    is_event_addon = isinstance(assignment, EventAddon)
    organizer = assignment.event.organizer if is_event_addon else assignment.organizer
    recipient = organizer_billing_recipient(organizer)
    if not recipient:
        return None
    with language(recipient.locale):
        current_state = str(assignment.get_status_display())
    marker = key_suffix or assignment.updated_at.isoformat()
    return queue_organizer_notification(
        organizer=organizer,
        event_type=event_type,
        logical_event_key=(
            f"{'event' if is_event_addon else 'organizer'}-addon:"
            f"{assignment.pk}:{event_type}:{marker}"
        ),
        related_object_type=("event_addon" if is_event_addon else "organizer_addon"),
        related_object_id=str(assignment.pk),
        recipient=recipient,
        object_name=assignment.addon.name,
        event_name=assignment.event.name if is_event_addon else "",
        amount=(assignment.price or Decimal("0.00")) * assignment.quantity,
        currency=assignment.currency,
        effective_date=assignment.starts_at,
        deadline=assignment.cancel_at or assignment.ends_at,
        current_state=current_state,
        **context_values,
    )


def queue_platform_alert(
    invoice: BusinessInvoice, failure_code: str
) -> BillingNotificationDelivery | None:
    recipient = platform_admin_recipient()
    if not recipient:
        logger.error(
            "No platform administrator recipient for invoice %s billing alert",
            invoice.invoice_number,
        )
        return None
    return queue_organizer_notification(
        organizer=invoice.organizer,
        event_type=BillingNotificationEventType.PLATFORM_ALERT,
        logical_event_key=f"invoice:{invoice.pk}:platform-alert:{failure_code}",
        related_object_type="business_invoice",
        related_object_id=str(invoice.pk),
        recipient=recipient,
        invoice_number=invoice.invoice_number,
        amount=invoice.total,
        currency=invoice.currency,
        current_state=invoice.get_payment_state_display(),
        failure_code=failure_code,
        action_url=(
            settings.SITE_URL.rstrip("/")
            + reverse(
                "plugins:eventyay_business:invoices.detail",
                kwargs={"pk": invoice.pk},
            )
        ),
    )


def queue_invoice_notification(
    invoice: BusinessInvoice,
    event_type: BillingNotificationEventType | str,
    *,
    key_suffix: str = "",
    **context_values,
) -> BillingNotificationDelivery | None:
    recipient = invoice_billing_recipient(invoice)
    if not recipient:
        logger.warning(
            "Business invoice %s has no notification recipient",
            invoice.invoice_number,
        )
        return None
    event_type = BillingNotificationEventType(event_type)
    marker = key_suffix or (
        invoice.payment_state
        if event_type == BillingNotificationEventType.COLLECTION_PROBLEM
        else ""
    )
    state_suffix = f":{marker}" if marker else ""
    with language(recipient.locale):
        current_state = str(invoice.get_payment_state_display())
    context = organizer_notification_context(
        invoice.organizer,
        event_type,
        recipient,
        invoice_number=invoice.invoice_number,
        amount=invoice.total,
        currency=invoice.currency,
        current_state=current_state,
        **context_values,
    )
    context.update(invoice_notification_context(invoice))
    return queue_billing_notification(
        logical_event_key=f"invoice:{invoice.pk}:{event_type}{state_suffix}",
        event_type=event_type,
        organizer=invoice.organizer,
        recipient=recipient,
        context=context,
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
        "event_type": BillingNotificationEventType(_event_type),
        "organizer_name": "Example Organizer",
        "object_name": "Professional",
        "event_name": "Example Conference",
        "invoice_number": "INV-EXAMPLE-001",
        "amount": str(Decimal("49.00")),
        "currency": "EUR",
        "issue_date": today.isoformat(),
        "effective_date": today.isoformat(),
        "deadline": today.isoformat(),
        "next_retry_at": today.isoformat(),
        "days_remaining": "3",
        "current_state": "Active",
        "failure_code": "stripe_unavailable",
        "action_url": settings.SITE_URL.rstrip("/") + "/control/",
        "support_email": GlobalSettingsObject().settings.get(
            "business_email_support_contact", as_type=str, default=""
        ),
    }
