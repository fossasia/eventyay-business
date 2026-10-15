from dataclasses import dataclass
from eventyay.base.models.organizer import OrganizerBillingModel

from .models import (
    AddonStatus,
    BillingCollectionAuthorization,
    EventAddon,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
)


@dataclass(frozen=True)
class ConsolidatedBillingPreflight:
    billing_customer_ready: bool
    payment_method_ready: bool
    collection_authorized: bool
    active_stripe_subscription_ids: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return (
            self.billing_customer_ready
            and self.payment_method_ready
            and self.collection_authorized
            and not self.active_stripe_subscription_ids
        )


def consolidated_billing_preflight(organizer) -> ConsolidatedBillingPreflight:
    billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
    authorization_exists = BillingCollectionAuthorization.objects.filter(
        organizer=organizer,
        revoked_at__isnull=True,
    ).exists()
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
    return ConsolidatedBillingPreflight(
        billing_customer_ready=bool(billing and billing.stripe_customer_id),
        payment_method_ready=bool(billing and billing.stripe_payment_method_id),
        collection_authorized=authorization_exists,
        active_stripe_subscription_ids=tuple(sorted(subscription_ids)),
    )
