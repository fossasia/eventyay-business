import logging
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.dispatch import receiver
from django.utils.timezone import now

from .invoice_collection import (
    collect_ready_business_invoices,
    retry_outstanding_business_invoices_after_payment_method_update,
)
from .notifications import deliver_billing_notification
from .reconciliation import run_billing_reconciliation

logger = logging.getLogger(__name__)

try:
    from django_scopes import scopes_disabled
except ModuleNotFoundError as err:
    if err.name != "django_scopes":
        raise
    from contextlib import nullcontext as scopes_disabled

try:
    from eventyay.celery_app import app
except ImportError:

    class _App:
        def task(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

    app = _App()

try:
    from eventyay.base.signals import periodic_task
except ImportError:
    periodic_task = None


def expire_addon_assignments():
    """
    Scans active add-on assignments (both OrganizerAddon and EventAddon) and:
    1. Cancels assignments where cancel_at <= now().
    2. Expires assignments where ends_at <= now().
    Invalidates entitlement cache and logs audit actions.
    """
    from .models import AddonStatus, EventAddon, OrganizerAddon
    from .services import invalidate_entitlement_cache, log_addon_lifecycle_action
    from .signals import addon_canceled, addon_expired

    current_time = now()
    canceled_count = 0
    expired_count = 0

    with scopes_disabled():
        # 1. Process scheduled cancellations for OrganizerAddon
        org_canceled = list(
            OrganizerAddon.objects.filter(
                status=AddonStatus.ACTIVE,
                cancel_at__isnull=False,
                cancel_at__lte=current_time,
            ).select_related("organizer", "addon")
        )
        for item in org_canceled:
            with transaction.atomic():
                rows_updated = OrganizerAddon.objects.filter(
                    pk=item.pk,
                    status=AddonStatus.ACTIVE,
                ).update(
                    status=AddonStatus.CANCELED,
                    canceled_at=current_time,
                    updated_at=current_time,
                )
                if not rows_updated:
                    continue
                item.status = AddonStatus.CANCELED
                item.canceled_at = current_time
                canceled_count += 1
                log_addon_lifecycle_action(item, "canceled")
                invalidate_entitlement_cache(organizer=item.organizer)
                addon_canceled.send(
                    sender=OrganizerAddon, instance=item, immediate=False
                )

        # 2. Process expiration for OrganizerAddon
        org_expired = list(
            OrganizerAddon.objects.filter(
                status=AddonStatus.ACTIVE,
                ends_at__isnull=False,
                ends_at__lte=current_time,
            ).select_related("organizer", "addon")
        )
        for item in org_expired:
            with transaction.atomic():
                rows_updated = OrganizerAddon.objects.filter(
                    pk=item.pk,
                    status=AddonStatus.ACTIVE,
                ).update(
                    status=AddonStatus.EXPIRED,
                    updated_at=current_time,
                )
                if not rows_updated:
                    continue
                item.status = AddonStatus.EXPIRED
                expired_count += 1
                log_addon_lifecycle_action(item, "expired")
                invalidate_entitlement_cache(organizer=item.organizer)
                addon_expired.send(sender=OrganizerAddon, instance=item)

        # 3. Process scheduled cancellations for EventAddon
        event_canceled = list(
            EventAddon.objects.filter(
                status=AddonStatus.ACTIVE,
                cancel_at__isnull=False,
                cancel_at__lte=current_time,
            ).select_related("event", "event__organizer", "addon")
        )
        for item in event_canceled:
            with transaction.atomic():
                rows_updated = EventAddon.objects.filter(
                    pk=item.pk,
                    status=AddonStatus.ACTIVE,
                ).update(
                    status=AddonStatus.CANCELED,
                    canceled_at=current_time,
                    updated_at=current_time,
                )
                if not rows_updated:
                    continue
                item.status = AddonStatus.CANCELED
                item.canceled_at = current_time
                canceled_count += 1
                log_addon_lifecycle_action(item, "canceled")
                invalidate_entitlement_cache(
                    organizer=item.event.organizer, event=item.event
                )
                addon_canceled.send(sender=EventAddon, instance=item, immediate=False)

        # 4. Process expiration for EventAddon
        event_expired = list(
            EventAddon.objects.filter(
                status=AddonStatus.ACTIVE,
                ends_at__isnull=False,
                ends_at__lte=current_time,
            ).select_related("event", "event__organizer", "addon")
        )
        for item in event_expired:
            with transaction.atomic():
                rows_updated = EventAddon.objects.filter(
                    pk=item.pk,
                    status=AddonStatus.ACTIVE,
                ).update(
                    status=AddonStatus.EXPIRED,
                    updated_at=current_time,
                )
                if not rows_updated:
                    continue
                item.status = AddonStatus.EXPIRED
                expired_count += 1
                log_addon_lifecycle_action(item, "expired")
                invalidate_entitlement_cache(
                    organizer=item.event.organizer, event=item.event
                )
                addon_expired.send(sender=EventAddon, instance=item)

    logger.info(
        "expire_addon_assignments completed: %d canceled, %d expired",
        canceled_count,
        expired_count,
    )
    return {"canceled": canceled_count, "expired": expired_count}


@app.task(name="eventyay_business.expire_addon_assignments")
def expire_addon_assignments_task():
    return expire_addon_assignments()


if periodic_task:

    @receiver(periodic_task, dispatch_uid="business_expire_addon_assignments")
    def periodic_expire_addon_assignments(sender, **kwargs):
        return expire_addon_assignments()


def manage_subscription_lifecycles():
    """
    Scans subscriptions to:
    1. Apply scheduled downgrades where pending_tier_version is set and pending_change_at <= now().
    2. Expire past-due subscriptions where the configured grace period has ended.
    Invalidates entitlement cache and emits lifecycle signals.
    """
    from .models import (
        AddonPricingMode,
        AddonStatus,
        BillingNotificationEventType,
        EventAddon,
        OrganizerAddon,
        Subscription,
        SubscriptionStatus,
    )
    from .notifications import (
        queue_addon_notification,
        queue_subscription_notification,
    )
    from .services import (
        get_grace_reminder_days,
        get_renewal_reminder_days,
        invalidate_entitlement_cache,
    )
    from .signals import subscription_downgraded, subscription_expired

    current_time = now()
    downgraded_count = 0
    expired_count = 0
    reminder_count = 0
    renewal_reminder_count = 0

    with scopes_disabled():
        # 1. Apply scheduled downgrades
        scheduled = list(
            Subscription.objects.filter(
                status=SubscriptionStatus.ACTIVE,
                pending_tier_version__isnull=False,
            )
            .filter(
                Q(pending_change_at__isnull=True)
                | Q(pending_change_at__lte=current_time)
            )
            .select_related("organizer", "pending_tier_version")
        )
        for item in scheduled:
            with transaction.atomic():
                locked_sub = (
                    Subscription.objects.select_for_update()
                    .filter(pk=item.pk, status=SubscriptionStatus.ACTIVE)
                    .first()
                )
                if not locked_sub or not locked_sub.pending_tier_version:
                    continue
                if (
                    locked_sub.pending_change_at
                    and locked_sub.pending_change_at > current_time
                ):
                    continue

                locked_sub.tier_version = locked_sub.pending_tier_version
                if locked_sub.pending_billing_interval:
                    locked_sub.billing_interval = locked_sub.pending_billing_interval
                locked_sub.pending_tier_version = None
                locked_sub.pending_billing_interval = None
                locked_sub.pending_change_at = None
                locked_sub.save()

                downgraded_count += 1
                organizer = locked_sub.organizer
                sub_instance = locked_sub
                transaction.on_commit(
                    lambda org=organizer, inst=sub_instance: (
                        invalidate_entitlement_cache(organizer=org),
                        subscription_downgraded.send(
                            sender=Subscription, instance=inst
                        ),
                    )
                )

        # 2. Expire past-due subscriptions whose grace period has ended
        past_due_subs = list(
            Subscription.objects.filter(
                status=SubscriptionStatus.PAST_DUE,
            ).select_related("organizer", "tier_version")
        )
        for item in past_due_subs:
            grace_deadline = item.grace_period_ends_at()
            if item.is_in_grace_period():
                days_remaining = (grace_deadline.date() - current_time.date()).days
                if days_remaining in get_grace_reminder_days():
                    delivery = queue_subscription_notification(
                        item,
                        BillingNotificationEventType.GRACE_PERIOD_REMINDER,
                        key_suffix=f"{grace_deadline.isoformat()}:{days_remaining}",
                        days_remaining=days_remaining,
                        deadline=grace_deadline,
                    )
                    reminder_count += int(delivery is not None)
                continue
            with transaction.atomic():
                locked_sub = (
                    Subscription.objects.select_for_update()
                    .filter(pk=item.pk, status=SubscriptionStatus.PAST_DUE)
                    .first()
                )
                if not locked_sub or locked_sub.is_in_grace_period():
                    continue

                locked_sub.status = SubscriptionStatus.EXPIRED
                locked_sub.save()

                expired_count += 1
                organizer = locked_sub.organizer
                sub_instance = locked_sub
                transaction.on_commit(
                    lambda org=organizer, inst=sub_instance, deadline=grace_deadline: (
                        invalidate_entitlement_cache(organizer=org),
                        subscription_expired.send(
                            sender=Subscription,
                            instance=inst,
                            grace_deadline=deadline,
                        ),
                    ),
                    robust=True,
                )

        renewal_cutoff = current_time + timedelta(days=get_renewal_reminder_days())
        renewing_subscriptions = Subscription.objects.filter(
            status=SubscriptionStatus.ACTIVE,
            ends_at__gt=current_time,
            ends_at__lte=renewal_cutoff,
            cancel_at__isnull=True,
        ).select_related("organizer", "tier_version", "tier_version__tier")
        for item in renewing_subscriptions:
            delivery = queue_subscription_notification(
                item,
                BillingNotificationEventType.RENEWAL_APPROACHING,
                key_suffix=item.ends_at.isoformat(),
                effective_date=item.ends_at,
            )
            renewal_reminder_count += int(delivery is not None)

        addon_filters = {
            "status": AddonStatus.ACTIVE,
            "addon__pricing_mode": AddonPricingMode.RECURRING,
            "ends_at__gt": current_time,
            "ends_at__lte": renewal_cutoff,
            "cancel_at__isnull": True,
        }
        organizer_addons = OrganizerAddon.objects.filter(
            **addon_filters
        ).select_related("organizer", "addon")
        event_addons = EventAddon.objects.filter(**addon_filters).select_related(
            "event", "event__organizer", "addon"
        )
        for item in (*organizer_addons, *event_addons):
            delivery = queue_addon_notification(
                item,
                BillingNotificationEventType.RENEWAL_APPROACHING,
                key_suffix=item.ends_at.isoformat(),
                effective_date=item.ends_at,
            )
            renewal_reminder_count += int(delivery is not None)

    logger.info(
        "manage_subscription_lifecycles completed: %d downgraded, %d expired, "
        "%d grace reminders, %d renewal reminders",
        downgraded_count,
        expired_count,
        reminder_count,
        renewal_reminder_count,
    )
    return {
        "downgraded": downgraded_count,
        "expired": expired_count,
        "grace_reminders": reminder_count,
        "renewal_reminders": renewal_reminder_count,
    }


@app.task(name="eventyay_business.manage_subscription_lifecycles")
def manage_subscription_lifecycles_task():
    return manage_subscription_lifecycles()


def generate_monthly_business_invoices(start_date=None, end_date=None):
    """
    Executes the monthly business invoice generation engine across all organizers.
    """
    from eventyay.base.models import Organizer

    from .invoicing_service import generate_all_business_invoices
    from .platform_fee_ledger import organizer_consolidated_billing_enabled

    organizers = [
        organizer
        for organizer in Organizer.objects.all()
        if organizer_consolidated_billing_enabled(organizer)
    ]
    invoices = generate_all_business_invoices(
        start_date=start_date,
        end_date=end_date,
        organizers=organizers,
    )
    collection_result = collect_ready_business_invoices()
    logger.info(
        "generate_monthly_business_invoices completed: %d invoices generated",
        len(invoices),
    )
    return {
        "generated_invoices_count": len(invoices),
        "collection": collection_result,
    }


@app.task(name="eventyay_business.collect_ready_business_invoices")
def collect_ready_business_invoices_task(invoice_ids=None):
    return collect_ready_business_invoices(invoice_ids=invoice_ids)


@app.task(name="eventyay_business.retry_outstanding_business_invoices")
def retry_outstanding_business_invoices_task(organizer_id: int):
    return retry_outstanding_business_invoices_after_payment_method_update(organizer_id)


@app.task(name="eventyay_business.deliver_billing_notification")
def deliver_billing_notification_task(delivery_id: int):
    delivery = deliver_billing_notification(delivery_id)
    return {
        "delivery_id": delivery.pk,
        "status": delivery.status,
        "attempt_count": delivery.attempt_count,
    }


@app.task(name="eventyay_business.generate_monthly_business_invoices")
def generate_monthly_business_invoices_task():
    return generate_monthly_business_invoices()


@app.task(name="eventyay_business.run_billing_reconciliation")
def run_billing_reconciliation_task():
    run = run_billing_reconciliation()
    return {"run_id": run.pk, **run.summary}


if periodic_task:

    @receiver(periodic_task, dispatch_uid="business_manage_subscription_lifecycles")
    def periodic_manage_subscription_lifecycles(sender, **kwargs):
        return manage_subscription_lifecycles()

    @receiver(periodic_task, dispatch_uid="business_generate_monthly_invoices")
    def periodic_generate_business_invoices(sender, **kwargs):
        return generate_monthly_business_invoices()

    @receiver(periodic_task, dispatch_uid="business_billing_reconciliation")
    def periodic_billing_reconciliation(sender, **kwargs):
        run = run_billing_reconciliation()
        return {"run_id": run.pk, **run.summary}
