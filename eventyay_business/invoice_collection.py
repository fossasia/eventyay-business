import logging
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils.timezone import now
from enum import StrEnum
from eventyay.base.models.organizer import OrganizerBillingModel
from eventyay.helpers.stripe_utils import get_stripe_secret_key

from .models import (
    AddonStatus,
    BillingCollectionAuthorization,
    BillingNotificationEventType,
    BusinessInvoice,
    BusinessInvoiceStatus,
    EventAddon,
    InvoicePaymentState,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
)
from .notifications import queue_invoice_notification
from .platform_fee_ledger import organizer_consolidated_billing_enabled
from .services import get_collection_retry_days, invalidate_entitlement_cache

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


class CollectionFailureCode(StrEnum):
    UNKNOWN = "collection_failed"
    STRIPE_UNAVAILABLE = "stripe_unavailable"
    STRIPE_NOT_CONFIGURED = "stripe_not_configured"
    STRIPE_CONNECTION_ERROR = "stripe_connection_error"
    STRIPE_ERROR = "stripe_error"
    UNKNOWN_PAYMENT_STATE = "unknown_payment_state"
    BILLING_RECORD_MISSING = "billing_record_missing"
    BILLING_CUSTOMER_MISSING = "billing_customer_missing"
    PAYMENT_METHOD_MISSING = "payment_method_missing"
    AUTHORIZATION_MISSING = "authorization_missing"
    AUTHENTICATION_REQUIRED = "authentication_required"


COLLECTION_RETRY_BLOCKED_CODES = frozenset(
    {
        CollectionFailureCode.BILLING_RECORD_MISSING,
        CollectionFailureCode.BILLING_CUSTOMER_MISSING,
        CollectionFailureCode.PAYMENT_METHOD_MISSING,
        CollectionFailureCode.AUTHORIZATION_MISSING,
        CollectionFailureCode.AUTHENTICATION_REQUIRED,
    }
)


class InvoiceCollectionError(ValidationError):
    def __init__(
        self,
        message: str,
        *,
        code: CollectionFailureCode = CollectionFailureCode.UNKNOWN,
        organizer_actionable: bool = False,
    ):
        super().__init__(message)
        self.collection_code = code
        self.organizer_actionable = organizer_actionable


def invoice_collection_failure_code(
    invoice: BusinessInvoice,
) -> CollectionFailureCode | str | None:
    metadata = invoice.collection_metadata
    if not isinstance(metadata, dict):
        return None
    last_error = metadata.get("last_error")
    if not isinstance(last_error, dict):
        return None
    code = last_error.get("code")
    return code if isinstance(code, str) else None


def collection_retry_delays() -> tuple[timedelta, ...]:
    return tuple(timedelta(days=day) for day in get_collection_retry_days())


def collection_retry_exhausted(
    attempt_count: int, retry_delays: tuple[timedelta, ...] | None = None
) -> bool:
    return attempt_count > len(retry_delays or collection_retry_delays())


def collection_retry_attempt_count(
    invoice: BusinessInvoice, metadata: dict | None = None
) -> int:
    metadata = metadata or (
        invoice.collection_metadata
        if isinstance(invoice.collection_metadata, dict)
        else {}
    )
    cycle_start = int(metadata.get("retry_cycle_start_attempt", 0))
    return max(invoice.collection_attempt_count - cycle_start, 0)


def collection_retry_due(invoice: BusinessInvoice, current_time=None) -> bool:
    retry_delays = collection_retry_delays()
    if invoice.payment_state == InvoicePaymentState.NOT_STARTED:
        return True
    if invoice.payment_state == InvoicePaymentState.REQUIRES_ACTION:
        return False
    failure_code = invoice_collection_failure_code(invoice)
    if failure_code in COLLECTION_RETRY_BLOCKED_CODES:
        return False
    metadata = (
        invoice.collection_metadata
        if isinstance(invoice.collection_metadata, dict)
        else {}
    )
    is_reconciliation = invoice.payment_state == InvoicePaymentState.PROCESSING
    if metadata.get("retry_exhausted") and not is_reconciliation:
        return False
    attempt_count = collection_retry_attempt_count(invoice, metadata)
    if is_reconciliation:
        attempt_count = int(metadata.get("reconciliation_attempt_count", 1))
    if (
        collection_retry_exhausted(attempt_count, retry_delays)
        and not is_reconciliation
    ):
        return False
    if not invoice.last_collection_attempt_at:
        return True
    retry_index = max(attempt_count - 1, 0)
    retry_index = min(retry_index, len(retry_delays) - 1)
    previous_offset = retry_delays[retry_index - 1] if retry_index else timedelta(0)
    retry_delay = retry_delays[retry_index] - previous_offset
    return invoice.last_collection_attempt_at + retry_delay <= (current_time or now())


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


def organizer_actionable_stripe_failure(error) -> CollectionFailureCode | None:
    json_body = getattr(error, "json_body", None) or {}
    error_data = json_body.get("error", {}) if isinstance(json_body, dict) else {}
    param = getattr(error, "param", None) or error_data.get("param")
    code = getattr(error, "code", None) or error_data.get("code")
    if param == "customer":
        return CollectionFailureCode.BILLING_CUSTOMER_MISSING
    if param == "payment_method" or code in {
        "payment_method_missing",
        "payment_method_unexpected_state",
    }:
        return CollectionFailureCode.PAYMENT_METHOD_MISSING
    return None


def notify_collection_problem(invoice: BusinessInvoice) -> None:
    queue_invoice_notification(
        invoice,
        BillingNotificationEventType.COLLECTION_PROBLEM,
    )


def notify_collection_succeeded(invoice: BusinessInvoice) -> None:
    queue_invoice_notification(
        invoice,
        BillingNotificationEventType.COLLECTION_SUCCEEDED,
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
        metadata.pop("retry_exhausted", None)
        metadata.pop("retry_after_payment_method_update", None)
        metadata.pop("retry_cycle_start_attempt", None)
        metadata.pop("organizer_actionable", None)
        metadata.pop("reconciliation_required", None)
        metadata.pop("reconciliation_attempt_count", None)

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
        metadata["last_error"] = {"code": CollectionFailureCode.AUTHENTICATION_REQUIRED}
        metadata["retry_after_payment_method_update"] = True
        metadata["organizer_actionable"] = True
        update_organizer_collection_health(invoice.organizer, succeeded=False)
    elif status == "processing":
        invoice.status = BusinessInvoiceStatus.OPEN
        invoice.payment_state = InvoicePaymentState.PROCESSING
    else:
        if not error_details:
            metadata["last_error"] = {"code": CollectionFailureCode.UNKNOWN}
        if collection_retry_exhausted(
            collection_retry_attempt_count(invoice, metadata)
        ):
            invoice.status = BusinessInvoiceStatus.UNCOLLECTIBLE
            metadata["retry_exhausted"] = True
        else:
            invoice.status = BusinessInvoiceStatus.OPEN
        invoice.payment_state = InvoicePaymentState.FAILED
        metadata["organizer_actionable"] = True
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


def fail_invoice_collection(
    invoice: BusinessInvoice,
    code: CollectionFailureCode | str,
) -> BusinessInvoice:
    metadata = dict(invoice.collection_metadata or {})
    metadata["last_error"] = {"code": code}
    metadata["retry_after_payment_method_update"] = (
        code in COLLECTION_RETRY_BLOCKED_CODES
    )
    metadata["organizer_actionable"] = True
    metadata.pop("retry_exhausted", None)
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
    invoice: BusinessInvoice, code: CollectionFailureCode | str
) -> BusinessInvoice:
    metadata = dict(invoice.collection_metadata or {})
    metadata["reconciliation_required"] = True
    metadata["last_error"] = {"code": code}
    reconciliation_attempt_count = (
        int(metadata.get("reconciliation_attempt_count", 0)) + 1
    )
    metadata["reconciliation_attempt_count"] = reconciliation_attempt_count
    if collection_retry_exhausted(reconciliation_attempt_count):
        metadata["retry_exhausted"] = True
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
        raise InvoiceCollectionError(
            "Stripe is unavailable.",
            code=CollectionFailureCode.STRIPE_UNAVAILABLE,
        )
    try:
        secret_key = get_stripe_secret_key()
    except ValidationError as exc:
        raise InvoiceCollectionError(
            "Stripe billing is not configured.",
            code=CollectionFailureCode.STRIPE_NOT_CONFIGURED,
        ) from exc

    billing = OrganizerBillingModel.objects.filter(organizer=invoice.organizer).first()
    authorization = BillingCollectionAuthorization.objects.filter(
        organizer=invoice.organizer,
        revoked_at__isnull=True,
    ).first()
    if not billing:
        raise InvoiceCollectionError(
            "Organizer billing details are missing.",
            code=CollectionFailureCode.BILLING_RECORD_MISSING,
            organizer_actionable=True,
        )
    if not billing.stripe_customer_id:
        raise InvoiceCollectionError(
            "Organizer billing customer is missing.",
            code=CollectionFailureCode.BILLING_CUSTOMER_MISSING,
            organizer_actionable=True,
        )
    if not billing.stripe_payment_method_id:
        raise InvoiceCollectionError(
            "Organizer payment method is missing.",
            code=CollectionFailureCode.PAYMENT_METHOD_MISSING,
            organizer_actionable=True,
        )
    if (
        not authorization
        or authorization.stripe_customer_id != billing.stripe_customer_id
    ):
        raise InvoiceCollectionError(
            "Automatic collection is not authorized.",
            code=CollectionFailureCode.AUTHORIZATION_MISSING,
            organizer_actionable=True,
        )
    return secret_key, billing


def begin_collection_attempt(invoice: BusinessInvoice, idempotency_key: str) -> None:
    invoice.collection_attempt_count += 1
    invoice.last_collection_attempt_at = now()
    invoice.payment_state = InvoicePaymentState.PROCESSING
    metadata = dict(invoice.collection_metadata or {})
    metadata["idempotency_key"] = idempotency_key
    metadata.pop("retry_after_payment_method_update", None)
    metadata.pop("retry_exhausted", None)
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
            if exc.organizer_actionable:
                return fail_invoice_collection(invoice, exc.collection_code)
            return mark_collection_reconciliation(invoice, exc.collection_code)

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
            retry_after_payment_method_update = bool(
                invoice.collection_metadata.get("retry_after_payment_method_update")
            )
            if (
                intent_status == "requires_action"
                and not retry_after_payment_method_update
            ):
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
                "requires_action",
            ):
                return mark_collection_reconciliation(
                    invoice, CollectionFailureCode.UNKNOWN_PAYMENT_STATE
                )

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
            return mark_collection_reconciliation(
                invoice, CollectionFailureCode.STRIPE_CONNECTION_ERROR
            )
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
            failure_code = organizer_actionable_stripe_failure(exc)
            if failure_code:
                logger.warning(
                    "Business invoice %s needs organizer billing details repaired: %s",
                    invoice.invoice_number,
                    exc,
                )
                return fail_invoice_collection(invoice, failure_code)
            logger.warning(
                "Stripe collection failed for Business invoice %s: %s",
                invoice.invoice_number,
                exc,
            )
            return mark_collection_reconciliation(
                invoice, CollectionFailureCode.STRIPE_ERROR
            )

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


def retry_outstanding_business_invoices_after_payment_method_update(
    organizer_id: int,
) -> dict:
    invoice_ids = []
    with scopes_disabled(), transaction.atomic():
        invoices = list(
            BusinessInvoice.objects.select_for_update().filter(
                organizer_id=organizer_id,
                finalized_at__isnull=False,
                total__gt=Decimal("0.00"),
                status__in=(
                    BusinessInvoiceStatus.OPEN,
                    BusinessInvoiceStatus.UNCOLLECTIBLE,
                ),
                payment_state__in=(
                    InvoicePaymentState.FAILED,
                    InvoicePaymentState.REQUIRES_ACTION,
                ),
            )
        )
        for invoice in invoices:
            metadata = dict(invoice.collection_metadata or {})
            if not metadata.get("organizer_actionable"):
                continue
            metadata["retry_after_payment_method_update"] = True
            metadata["retry_cycle_start_attempt"] = invoice.collection_attempt_count
            previous_error = metadata.pop("last_error", None)
            if previous_error:
                metadata["previous_error"] = previous_error
            metadata.pop("retry_exhausted", None)
            metadata.pop("reconciliation_required", None)
            metadata.pop("reconciliation_attempt_count", None)
            invoice.status = BusinessInvoiceStatus.OPEN
            invoice.payment_state = InvoicePaymentState.FAILED
            invoice.last_collection_attempt_at = None
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
            invoice_ids.append(invoice.pk)
    return collect_ready_business_invoices(invoice_ids=invoice_ids)


def collect_ready_business_invoices(invoice_ids=None) -> dict:
    current_time = now()
    with scopes_disabled():
        invoices = BusinessInvoice.objects.filter(
            finalized_at__isnull=False,
            total__gt=Decimal("0.00"),
            status=BusinessInvoiceStatus.OPEN,
        ).filter(
            payment_state__in=(
                InvoicePaymentState.NOT_STARTED,
                InvoicePaymentState.FAILED,
                InvoicePaymentState.REQUIRES_ACTION,
                InvoicePaymentState.PROCESSING,
            )
        )
        if invoice_ids is not None:
            invoices = invoices.filter(pk__in=invoice_ids)
        invoice_ids_to_collect = [
            invoice.pk
            for invoice in invoices.select_related("organizer")
            if organizer_consolidated_billing_enabled(invoice.organizer)
            and collection_retry_due(invoice, current_time=current_time)
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
