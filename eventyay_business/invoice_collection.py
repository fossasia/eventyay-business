import logging
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils.timezone import now
from eventyay.base.models.organizer import OrganizerBillingModel
from eventyay.helpers.stripe_utils import get_stripe_secret_key

from .models import (
    AddonStatus,
    BillingCollectionAuthorization,
    BusinessInvoice,
    BusinessInvoiceStatus,
    EventAddon,
    InvoicePaymentState,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
)
from .platform_fee_ledger import organizer_consolidated_billing_enabled
from .services import invalidate_entitlement_cache

logger = logging.getLogger(__name__)

try:
    from django_scopes import scopes_disabled
except ModuleNotFoundError as err:
    if err.name != "django_scopes":
        raise
    from contextlib import nullcontext as scopes_disabled

try:
    import stripe
except ImportError:
    stripe = None


COLLECTION_METADATA_TYPE = "eventyay_business_invoice"
COLLECTION_RETRY_DELAY = timedelta(days=1)


class InvoiceCollectionError(ValidationError):
    pass


def invoice_amount_in_minor_units(invoice: BusinessInvoice) -> int:
    places = settings.CURRENCY_PLACES.get(invoice.currency, 2)
    return int(invoice.total * (Decimal("10") ** places))


def payment_intent_value(payment_intent, key, default=None):
    if isinstance(payment_intent, dict):
        return payment_intent.get(key, default)
    value = getattr(payment_intent, key, default)
    return default if value is None else value


def payment_intent_metadata(payment_intent) -> dict:
    metadata = payment_intent_value(payment_intent, "metadata", {})
    return dict(metadata) if metadata else {}


def stripe_reference(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None)


def collection_error_details(error=None, payment_intent=None) -> dict:
    details = {}
    if error is not None:
        code = getattr(error, "code", None)
        decline_code = getattr(error, "decline_code", None)
        json_body = getattr(error, "json_body", None) or {}
        error_data = json_body.get("error", {}) if isinstance(json_body, dict) else {}
        code = code or error_data.get("code")
        decline_code = decline_code or error_data.get("decline_code")
        if code:
            details["code"] = code
        if decline_code:
            details["decline_code"] = decline_code
    last_payment_error = payment_intent_value(payment_intent, "last_payment_error", {})
    if last_payment_error:
        code = payment_intent_value(last_payment_error, "code")
        decline_code = payment_intent_value(last_payment_error, "decline_code")
        if code:
            details["code"] = code
        if decline_code:
            details["decline_code"] = decline_code
    return details


def payment_intent_from_error(error):
    payment_intent = getattr(error, "payment_intent", None)
    if payment_intent:
        return payment_intent
    json_body = getattr(error, "json_body", None) or {}
    if not isinstance(json_body, dict):
        return None
    error_data = json_body.get("error", {})
    if not isinstance(error_data, dict):
        return None
    return error_data.get("payment_intent")


def billing_contact_email(invoice: BusinessInvoice) -> str | None:
    recipient = invoice.billing_address_snapshot.get("contact_email")
    if not recipient:
        billing = OrganizerBillingModel.objects.filter(
            organizer=invoice.organizer
        ).first()
        recipient = billing.primary_contact_email if billing else None
    return recipient


def notify_collection_problem(invoice: BusinessInvoice) -> None:
    recipient = billing_contact_email(invoice)
    if not recipient:
        return
    send_mail(
        f"Action required for Eventyay invoice {invoice.invoice_number}",
        (
            f"We could not automatically collect invoice {invoice.invoice_number}. "
            "Sign in to Eventyay, review your billing settings, and update your "
            "saved payment method if needed."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
        fail_silently=True,
    )


def notify_collection_succeeded(invoice: BusinessInvoice) -> None:
    recipient = billing_contact_email(invoice)
    if not recipient:
        return
    send_mail(
        f"Eventyay invoice {invoice.invoice_number} paid",
        (
            f"Payment for invoice {invoice.invoice_number} was collected "
            "successfully. The paid invoice remains available in Eventyay."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
        fail_silently=True,
    )


def update_organizer_collection_health(organizer, succeeded: bool) -> None:
    subscription = (
        Subscription.objects.select_for_update()
        .filter(organizer=organizer)
        .filter(
            Q(status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE])
            | Q(
                status=SubscriptionStatus.EXPIRED,
                past_due_since__isnull=False,
            )
        )
        .order_by("-starts_at")
        .first()
    )
    if subscription:
        if succeeded and subscription.status in (
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.EXPIRED,
        ):
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.past_due_since = None
            subscription.save(update_fields=["status", "past_due_since", "updated_at"])
        elif not succeeded and subscription.status == SubscriptionStatus.ACTIVE:
            subscription.status = SubscriptionStatus.PAST_DUE
            subscription.save(update_fields=["status", "past_due_since", "updated_at"])

    if succeeded:
        OrganizerAddon.objects.filter(
            organizer=organizer,
            status=AddonStatus.PAST_DUE,
        ).update(status=AddonStatus.ACTIVE, updated_at=now())
        EventAddon.objects.filter(
            event__organizer=organizer,
            status=AddonStatus.PAST_DUE,
        ).update(status=AddonStatus.ACTIVE, updated_at=now())
    else:
        OrganizerAddon.objects.filter(
            organizer=organizer,
            status=AddonStatus.ACTIVE,
        ).update(status=AddonStatus.PAST_DUE, updated_at=now())
        EventAddon.objects.filter(
            event__organizer=organizer,
            status=AddonStatus.ACTIVE,
        ).update(status=AddonStatus.PAST_DUE, updated_at=now())
    invalidate_entitlement_cache(organizer=organizer)


def update_invoice_from_payment_intent(
    invoice: BusinessInvoice,
    payment_intent,
    error=None,
    force_requires_action: bool = False,
) -> BusinessInvoice:
    previous_state = invoice.payment_state
    intent_id = payment_intent_value(payment_intent, "id")
    status = payment_intent_value(payment_intent, "status", "")
    if previous_state == InvoicePaymentState.SUCCEEDED and status != "succeeded":
        return invoice
    latest_charge = stripe_reference(
        payment_intent_value(payment_intent, "latest_charge")
    )
    metadata = dict(invoice.collection_metadata or {})
    metadata["stripe_status"] = status
    if latest_charge:
        metadata["stripe_charge_id"] = latest_charge
    error_details = collection_error_details(error, payment_intent)
    if error_details:
        metadata["last_error"] = error_details
    elif status == "succeeded":
        metadata.pop("last_error", None)

    if intent_id:
        invoice.stripe_payment_intent_id = intent_id
    if status == "succeeded":
        invoice.status = BusinessInvoiceStatus.PAID
        invoice.payment_state = InvoicePaymentState.SUCCEEDED
        invoice.paid_at = invoice.paid_at or now()
        update_organizer_collection_health(invoice.organizer, succeeded=True)
    elif force_requires_action or status == "requires_action":
        invoice.status = BusinessInvoiceStatus.OPEN
        invoice.payment_state = InvoicePaymentState.REQUIRES_ACTION
        update_organizer_collection_health(invoice.organizer, succeeded=False)
    elif status == "processing":
        invoice.status = BusinessInvoiceStatus.OPEN
        invoice.payment_state = InvoicePaymentState.PROCESSING
    else:
        invoice.status = BusinessInvoiceStatus.OPEN
        invoice.payment_state = InvoicePaymentState.FAILED
        update_organizer_collection_health(invoice.organizer, succeeded=False)

    invoice.collection_metadata = metadata
    invoice.save(
        update_fields=[
            "status",
            "payment_state",
            "paid_at",
            "stripe_payment_intent_id",
            "collection_metadata",
            "updated_at",
        ]
    )
    if (
        invoice.payment_state
        in (InvoicePaymentState.FAILED, InvoicePaymentState.REQUIRES_ACTION)
        and previous_state != invoice.payment_state
    ):
        transaction.on_commit(lambda: notify_collection_problem(invoice))
    elif (
        invoice.payment_state == InvoicePaymentState.SUCCEEDED
        and previous_state != InvoicePaymentState.SUCCEEDED
    ):
        transaction.on_commit(lambda: notify_collection_succeeded(invoice))
    return invoice


def fail_invoice_collection(invoice: BusinessInvoice, code: str) -> BusinessInvoice:
    metadata = dict(invoice.collection_metadata or {})
    metadata["last_error"] = {"code": code}
    previous_state = invoice.payment_state
    invoice.status = BusinessInvoiceStatus.OPEN
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.last_collection_attempt_at = now()
    invoice.collection_metadata = metadata
    invoice.save(
        update_fields=[
            "status",
            "payment_state",
            "last_collection_attempt_at",
            "collection_metadata",
            "updated_at",
        ]
    )
    update_organizer_collection_health(invoice.organizer, succeeded=False)
    if previous_state != InvoicePaymentState.FAILED:
        transaction.on_commit(lambda: notify_collection_problem(invoice))
    return invoice


def mark_collection_reconciliation(
    invoice: BusinessInvoice, code: str
) -> BusinessInvoice:
    metadata = dict(invoice.collection_metadata or {})
    metadata["reconciliation_required"] = True
    metadata["last_error"] = {"code": code}
    invoice.status = BusinessInvoiceStatus.OPEN
    invoice.payment_state = InvoicePaymentState.PROCESSING
    invoice.last_collection_attempt_at = now()
    invoice.collection_metadata = metadata
    invoice.save(
        update_fields=[
            "status",
            "payment_state",
            "last_collection_attempt_at",
            "collection_metadata",
            "updated_at",
        ]
    )
    return invoice


def validate_collection_configuration(invoice: BusinessInvoice):
    if stripe is None:
        raise InvoiceCollectionError("Stripe is unavailable.")
    try:
        secret_key = get_stripe_secret_key()
    except ValidationError as exc:
        raise InvoiceCollectionError("Stripe billing is not configured.") from exc

    billing = OrganizerBillingModel.objects.filter(organizer=invoice.organizer).first()
    authorization = BillingCollectionAuthorization.objects.filter(
        organizer=invoice.organizer,
        revoked_at__isnull=True,
    ).first()
    if not billing or not billing.stripe_customer_id:
        raise InvoiceCollectionError("Organizer billing customer is missing.")
    if not billing.stripe_payment_method_id:
        raise InvoiceCollectionError("Organizer payment method is missing.")
    if (
        not authorization
        or authorization.stripe_customer_id != billing.stripe_customer_id
    ):
        raise InvoiceCollectionError("Automatic collection is not authorized.")
    return secret_key, billing


def begin_collection_attempt(invoice: BusinessInvoice, idempotency_key: str) -> None:
    invoice.collection_attempt_count += 1
    invoice.last_collection_attempt_at = now()
    invoice.payment_state = InvoicePaymentState.PROCESSING
    metadata = dict(invoice.collection_metadata or {})
    metadata["idempotency_key"] = idempotency_key
    invoice.collection_metadata = metadata
    invoice.save(
        update_fields=[
            "collection_attempt_count",
            "last_collection_attempt_at",
            "payment_state",
            "collection_metadata",
            "updated_at",
        ]
    )


def find_existing_payment_intent(invoice: BusinessInvoice):
    result = stripe.PaymentIntent.search(
        query=(
            "metadata['eventyay_business_invoice_id']:"
            f"'{invoice.pk}' AND metadata['type']:'{COLLECTION_METADATA_TYPE}'"
        ),
        limit=100,
    )
    for intent in payment_intent_value(result, "data", []) or []:
        metadata = payment_intent_metadata(intent)
        if metadata.get(
            "eventyay_business_invoice_number"
        ) == invoice.invoice_number and metadata.get("organizer_id") == str(
            invoice.organizer_id
        ):
            return intent
    return None


def collect_business_invoice(invoice_or_id) -> BusinessInvoice:
    invoice_id = (
        invoice_or_id.pk
        if isinstance(invoice_or_id, BusinessInvoice)
        else invoice_or_id
    )
    with scopes_disabled(), transaction.atomic():
        invoice = (
            BusinessInvoice.objects.select_for_update()
            .select_related("organizer")
            .get(pk=invoice_id)
        )
        if not invoice.finalized_at:
            raise InvoiceCollectionError("Only finalized invoices can be collected.")
        if (
            invoice.status == BusinessInvoiceStatus.PAID
            or invoice.payment_state == InvoicePaymentState.SUCCEEDED
        ):
            return invoice
        if invoice.status in (
            BusinessInvoiceStatus.CANCELED,
            BusinessInvoiceStatus.UNCOLLECTIBLE,
        ):
            return invoice
        if not organizer_consolidated_billing_enabled(invoice.organizer):
            logger.info(
                "Business invoice %s is awaiting organizer billing cutover",
                invoice.invoice_number,
            )
            return invoice
        if invoice.total <= Decimal("0.00"):
            invoice.status = BusinessInvoiceStatus.PAID
            invoice.payment_state = InvoicePaymentState.SUCCEEDED
            invoice.paid_at = invoice.paid_at or now()
            invoice.save(
                update_fields=["status", "payment_state", "paid_at", "updated_at"]
            )
            return invoice

        try:
            secret_key, billing = validate_collection_configuration(invoice)
        except InvoiceCollectionError as exc:
            logger.warning(
                "Business invoice %s is not ready for collection: %s",
                invoice.invoice_number,
                exc,
            )
            return fail_invoice_collection(invoice, "billing_not_ready")

        stripe.api_key = secret_key
        intent = None
        recreate_intent = False
        try:
            if invoice.stripe_payment_intent_id:
                intent = stripe.PaymentIntent.retrieve(invoice.stripe_payment_intent_id)
            else:
                intent = find_existing_payment_intent(invoice)
                if intent:
                    invoice.stripe_payment_intent_id = payment_intent_value(
                        intent, "id"
                    )
                    invoice.save(
                        update_fields=["stripe_payment_intent_id", "updated_at"]
                    )

            intent_status = payment_intent_value(intent, "status") if intent else None
            if intent_status in ("succeeded", "processing"):
                return update_invoice_from_payment_intent(invoice, intent)
            if intent_status == "requires_action":
                return update_invoice_from_payment_intent(
                    invoice, intent, force_requires_action=True
                )
            if intent_status == "canceled":
                invoice.stripe_payment_intent_id = None
                invoice.save(update_fields=["stripe_payment_intent_id", "updated_at"])
                intent = None
                recreate_intent = True
            if intent is not None and intent_status not in (
                "requires_payment_method",
                "requires_confirmation",
            ):
                return mark_collection_reconciliation(invoice, "unknown_payment_state")

            if intent is not None:
                attempt_number = invoice.collection_attempt_count + 1
                idempotency_key = (
                    f"eventyay-business-invoice-{invoice.pk}-retry-{attempt_number}"
                )
                begin_collection_attempt(invoice, idempotency_key)
                intent = stripe.PaymentIntent.confirm(
                    invoice.stripe_payment_intent_id,
                    payment_method=billing.stripe_payment_method_id,
                    off_session=True,
                    idempotency_key=idempotency_key,
                )
            else:
                idempotency_key = (
                    f"eventyay-business-invoice-{invoice.pk}-recreate-"
                    f"{invoice.collection_attempt_count + 1}"
                    if recreate_intent
                    else f"eventyay-business-invoice-{invoice.pk}-v1"
                )
                begin_collection_attempt(invoice, idempotency_key)
                intent = stripe.PaymentIntent.create(
                    amount=invoice_amount_in_minor_units(invoice),
                    currency=invoice.currency.lower(),
                    customer=billing.stripe_customer_id,
                    payment_method=billing.stripe_payment_method_id,
                    off_session=True,
                    confirm=True,
                    description=f"Eventyay invoice {invoice.invoice_number}",
                    metadata={
                        "type": COLLECTION_METADATA_TYPE,
                        "eventyay_business_invoice_id": str(invoice.pk),
                        "eventyay_business_invoice_number": invoice.invoice_number,
                        "organizer_id": str(invoice.organizer_id),
                    },
                    idempotency_key=idempotency_key,
                )
        except (stripe.error.APIConnectionError, TimeoutError) as exc:
            logger.warning(
                "Stripe collection needs reconciliation for Business invoice %s: %s",
                invoice.invoice_number,
                exc,
            )
            return mark_collection_reconciliation(invoice, "stripe_connection_error")
        except stripe.error.CardError as exc:
            intent = payment_intent_from_error(exc) or intent
            force_requires_action = (
                collection_error_details(exc, intent).get("code")
                == "authentication_required"
            )
            return update_invoice_from_payment_intent(
                invoice,
                intent or {},
                error=exc,
                force_requires_action=force_requires_action,
            )
        except stripe.error.StripeError as exc:
            logger.warning(
                "Stripe collection failed for Business invoice %s: %s",
                invoice.invoice_number,
                exc,
            )
            return fail_invoice_collection(invoice, "stripe_error")

        return update_invoice_from_payment_intent(invoice, intent)


def business_invoice_from_payment_intent(payment_intent):
    metadata = payment_intent_metadata(payment_intent)
    if metadata.get("type") != COLLECTION_METADATA_TYPE:
        return None
    invoice_id = metadata.get("eventyay_business_invoice_id")
    invoice_number = metadata.get("eventyay_business_invoice_number")
    if not invoice_id or not invoice_number:
        return None
    try:
        invoice_id = int(invoice_id)
    except (TypeError, ValueError):
        return None
    invoice = (
        BusinessInvoice.objects.select_for_update()
        .select_related("organizer")
        .filter(pk=invoice_id, invoice_number=invoice_number)
        .first()
    )
    if not invoice:
        return None
    if metadata.get("organizer_id") != str(invoice.organizer_id):
        return None
    amount = payment_intent_value(payment_intent, "amount")
    if amount is not None and amount != invoice_amount_in_minor_units(invoice):
        return None
    currency = payment_intent_value(payment_intent, "currency")
    if currency is not None and currency.lower() != invoice.currency.lower():
        return None
    intent_id = payment_intent_value(payment_intent, "id")
    if (
        invoice.stripe_payment_intent_id
        and intent_id != invoice.stripe_payment_intent_id
    ):
        return None
    return invoice


def process_business_payment_intent(payment_intent):
    with scopes_disabled(), transaction.atomic():
        invoice = business_invoice_from_payment_intent(payment_intent)
        if not invoice:
            return None
        return update_invoice_from_payment_intent(invoice, payment_intent)


def collect_ready_business_invoices(invoice_ids=None) -> dict:
    retry_before = now() - COLLECTION_RETRY_DELAY
    with scopes_disabled():
        invoices = BusinessInvoice.objects.filter(
            finalized_at__isnull=False,
            total__gt=Decimal("0.00"),
            status=BusinessInvoiceStatus.OPEN,
        ).filter(
            Q(payment_state=InvoicePaymentState.NOT_STARTED)
            | Q(
                payment_state__in=(
                    InvoicePaymentState.FAILED,
                    InvoicePaymentState.REQUIRES_ACTION,
                    InvoicePaymentState.PROCESSING,
                ),
                last_collection_attempt_at__isnull=True,
            )
            | Q(
                payment_state__in=(
                    InvoicePaymentState.FAILED,
                    InvoicePaymentState.REQUIRES_ACTION,
                    InvoicePaymentState.PROCESSING,
                ),
                last_collection_attempt_at__lte=retry_before,
            )
        )
        if invoice_ids is not None:
            invoices = invoices.filter(pk__in=invoice_ids)
        invoice_ids_to_collect = [
            invoice.pk
            for invoice in invoices.select_related("organizer")
            if organizer_consolidated_billing_enabled(invoice.organizer)
        ]

    result = {
        "processed": 0,
        "succeeded": 0,
        "processing": 0,
        "requires_action": 0,
        "failed": 0,
    }
    for invoice_id in invoice_ids_to_collect:
        try:
            invoice = collect_business_invoice(invoice_id)
        except (DatabaseError, InvoiceCollectionError, stripe.error.StripeError) as exc:
            logger.warning(
                "Business invoice %s collection failed; continuing with remaining invoices: %s",
                invoice_id,
                exc,
            )
            result["processed"] += 1
            result["failed"] += 1
            continue
        result["processed"] += 1
        if invoice.payment_state == InvoicePaymentState.SUCCEEDED:
            result["succeeded"] += 1
        elif invoice.payment_state == InvoicePaymentState.PROCESSING:
            result["processing"] += 1
        elif invoice.payment_state == InvoicePaymentState.REQUIRES_ACTION:
            result["requires_action"] += 1
        elif invoice.payment_state == InvoicePaymentState.FAILED:
            result["failed"] += 1
    return result
