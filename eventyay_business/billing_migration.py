import datetime
from dataclasses import dataclass
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.timezone import now
from eventyay.base.models import Organizer
from eventyay.base.models.organizer import OrganizerBillingModel

from .models import (
    AddonStatus,
    BillingCollectionAuthorization,
    BillingReconciliationResult,
    BillingReconciliationRunStatus,
    EventAddon,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
)
from .platform_fee_ledger import BILLING_CUTOVER_SETTING, organizer_billing_cutover_at


@dataclass(frozen=True)
class ConsolidatedBillingPreflight:
    billing_customer_ready: bool
    payment_method_ready: bool
    collection_authorized: bool
    active_stripe_subscription_ids: tuple[str, ...]
    reconciliation_ready: bool
    reconciliation_result_id: int | None
    activated_at: datetime.datetime | None

    @property
    def ready(self) -> bool:
        return (
            self.billing_customer_ready
            and self.payment_method_ready
            and self.collection_authorized
            and not self.active_stripe_subscription_ids
            and self.reconciliation_ready
        )


def consolidated_billing_preflight(organizer) -> ConsolidatedBillingPreflight:
    billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
    authorization_exists = bool(
        billing
        and billing.stripe_customer_id
        and BillingCollectionAuthorization.objects.filter(
            organizer=organizer,
            stripe_customer_id=billing.stripe_customer_id,
            revoked_at__isnull=True,
        ).exists()
    )
    subscription_ids = set(
        Subscription.objects.filter(
            organizer=organizer,
            status__in=(SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE),
        )
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .values_list("stripe_subscription_id", flat=True)
    )
    subscription_ids.update(
        OrganizerAddon.objects.filter(
            organizer=organizer,
            status__in=(AddonStatus.ACTIVE, AddonStatus.PAST_DUE),
        )
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .values_list("stripe_subscription_id", flat=True)
    )
    subscription_ids.update(
        EventAddon.objects.filter(
            event__organizer=organizer,
            status__in=(AddonStatus.ACTIVE, AddonStatus.PAST_DUE),
        )
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .values_list("stripe_subscription_id", flat=True)
    )
    for snapshot in Subscription.objects.filter(organizer=organizer).values_list(
        "configuration_snapshot", flat=True
    ):
        subscription_ids.update(
            (snapshot or {}).get("uncanceled_stripe_subscription_ids", [])
        )
    reconciliation_result = (
        BillingReconciliationResult.objects.filter(
            organizer=organizer,
            run__status=BillingReconciliationRunStatus.COMPLETED,
        )
        .select_related("run")
        .order_by("-run__period_end", "-pk")
        .first()
    )
    reconciliation_ready = bool(
        reconciliation_result and reconciliation_result.is_currently_accepted
    )
    return ConsolidatedBillingPreflight(
        billing_customer_ready=bool(billing and billing.stripe_customer_id),
        payment_method_ready=bool(billing and billing.stripe_payment_method_id),
        collection_authorized=authorization_exists,
        active_stripe_subscription_ids=tuple(sorted(subscription_ids)),
        reconciliation_ready=reconciliation_ready,
        reconciliation_result_id=(
            reconciliation_result.pk if reconciliation_result else None
        ),
        activated_at=organizer_billing_cutover_at(organizer),
    )


@transaction.atomic
def enable_consolidated_billing(organizer):
    """Record cutover only after rechecking readiness under a row lock."""
    organizer = Organizer.objects.select_for_update().get(pk=organizer.pk)
    if organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str):
        return organizer
    preflight = consolidated_billing_preflight(organizer)
    if not preflight.ready:
        raise ValidationError("Organizer is not ready for consolidated billing.")
    organizer.settings.set(BILLING_CUTOVER_SETTING, now().isoformat())
    return organizer
