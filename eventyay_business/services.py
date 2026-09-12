import logging
from django.db import IntegrityError, transaction
from django.utils.timezone import now

logger = logging.getLogger(__name__)


def record_usage(
    organizer,
    capability,
    quantity,
    unit,
    source_type,
    source_id,
    idempotency_key,
    event=None,
    metadata=None,
):
    """
    Atomically records a usage event.
    Relies on database unique constraints for idempotency.
    Returns the created UsageRecord or None if it was already processed.
    """
    from .models import UsageRecord

    try:
        with transaction.atomic():
            record = UsageRecord.objects.create(
                organizer=organizer,
                event=event,
                capability=capability,
                quantity=quantity,
                unit=unit,
                source_type=source_type,
                source_id=source_id,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
                occurred_at=now(),
            )
            return record
    except IntegrityError as exc:
        cause = getattr(exc, "__cause__", None)
        diag = getattr(cause, "diag", None) if cause else None
        constraint_name = getattr(diag, "constraint_name", None) if diag else None

        if constraint_name == "unique_usage_idempotency_per_organizer":
            logger.info(
                f"Usage record with idempotency key {idempotency_key} already exists for organizer {organizer.slug}."
            )
            return None

        # Re-raise all other integrity errors
        raise


def seed_standard_entitlements_for_version(tier_version, TierEntitlementModel=None):
    """
    Populates standard catalogue capabilities as TierEntitlement database records
    for the given tier_version if they do not already exist.
    """
    if TierEntitlementModel is None:
        from .models import TierEntitlement as TierEntitlementModel

    from .capabilities import STANDARD_CAPABILITIES, CapabilityValueType

    existing_caps = set(
        TierEntitlementModel.objects.filter(tier_version=tier_version).values_list(
            "capability", flat=True
        )
    )

    entitlements_to_create = []
    for cap in STANDARD_CAPABILITIES:
        if cap.name in existing_caps:
            continue

        raw_val = cap.default_value
        if cap.value_type == CapabilityValueType.BOOLEAN:
            str_val = "true" if raw_val else "false"
        elif raw_val is not None:
            str_val = str(raw_val)
        else:
            str_val = ""

        entitlements_to_create.append(
            TierEntitlementModel(
                tier_version=tier_version,
                capability=cap.name,
                value=str_val,
                unit=cap.unit or "",
                overage_allowed=False,
            )
        )

    if entitlements_to_create:
        try:
            TierEntitlementModel.objects.bulk_create(
                entitlements_to_create, ignore_conflicts=True
            )
        except IntegrityError:
            pass


def migrate_tier_subscribers(tier, target_version, from_version=None):
    """
    Migrates active and pending subscriptions belonging to a tier (or specific from_version)
    to target_version.
    Returns the number of subscriptions updated.
    """
    from .models import Subscription, SubscriptionStatus

    if target_version.tier_id != tier.pk:
        raise ValueError("Target version does not belong to the specified tier.")
    if from_version and from_version.tier_id != tier.pk:
        raise ValueError("From version does not belong to the specified tier.")

    qs = Subscription.objects.filter(
        tier_version__tier=tier,
        status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING],
    ).exclude(tier_version=target_version)

    if from_version:
        qs = qs.filter(tier_version=from_version)

    count = qs.update(tier_version=target_version, updated_at=now())
    return count


def migrate_addon_assignments(addon_definition):
    """
    Synchronizes all active assignments of an AddonDefinition to match its current
    snapshot fields (capability, entitlement_value, price, currency).
    Returns the total number of assignments updated.
    """
    from .models import AddonStatus, EventAddon, OrganizerAddon

    org_count = OrganizerAddon.objects.filter(
        addon=addon_definition,
        status=AddonStatus.ACTIVE,
    ).update(
        capability=addon_definition.capability,
        entitlement_value=addon_definition.entitlement_value,
        price=addon_definition.price,
        currency=addon_definition.currency,
        updated_at=now(),
    )

    event_count = EventAddon.objects.filter(
        addon=addon_definition,
        status=AddonStatus.ACTIVE,
    ).update(
        capability=addon_definition.capability,
        entitlement_value=addon_definition.entitlement_value,
        price=addon_definition.price,
        currency=addon_definition.currency,
        updated_at=now(),
    )

    return org_count + event_count


def invalidate_entitlement_cache(organizer=None, event=None):
    """
    Invalidates any cached entitlement decisions for an organizer and/or event.
    """
    try:
        from django.core.cache import cache

        keys = []
        if organizer:
            org_id = getattr(organizer, "pk", organizer)
            keys.extend(
                [
                    f"entitlements:org:{org_id}",
                    f"business:entitlements:org:{org_id}",
                ]
            )
        if event:
            event_id = getattr(event, "pk", event)
            keys.extend(
                [
                    f"entitlements:event:{event_id}",
                    f"business:entitlements:event:{event_id}",
                ]
            )
        if keys:
            cache.delete_many(keys)
    except Exception:
        logger.debug("Failed to invalidate entitlement cache", exc_info=True)


def log_addon_lifecycle_action(instance, action: str, user=None, data=None):
    """
    Records an audit log entry on the target Organizer or Event for an add-on lifecycle change.
    """
    payload = data or {}
    payload.setdefault("addon_id", instance.addon_id)
    payload.setdefault("addon_name", instance.addon.name if instance.addon else "")
    payload.setdefault("status", instance.status)

    target = getattr(instance, "event", None) or getattr(instance, "organizer", None)
    if target and hasattr(target, "log_action"):
        try:
            target.log_action(
                f"eventyay_business.addon.{action}",
                user=user,
                data=payload,
            )
        except Exception:
            logger.debug(
                "Failed to create LogEntry for %s on %s", action, target, exc_info=True
            )


def get_grace_period_days(subscription=None) -> int:
    """
    Returns the configured grace period in days for past-due subscriptions.
    Hierarchy:
    1. subscription.configuration_snapshot["grace_period_days"]
    2. subscription.tier_version.configuration_snapshot["grace_period_days"]
    3. settings.EVENTYAY_BUSINESS_GRACE_PERIOD_DAYS
    4. GlobalSettingsObject().settings.get("business_grace_period_days")
    5. Default fallback: 7 days
    """
    if subscription:
        sub_snapshot = getattr(subscription, "configuration_snapshot", None)
        if isinstance(sub_snapshot, dict) and "grace_period_days" in sub_snapshot:
            try:
                return int(sub_snapshot["grace_period_days"])
            except (ValueError, TypeError):
                pass

        tier_ver = getattr(subscription, "tier_version", None)
        ver_snapshot = (
            getattr(tier_ver, "configuration_snapshot", None) if tier_ver else None
        )
        if isinstance(ver_snapshot, dict) and "grace_period_days" in ver_snapshot:
            try:
                return int(ver_snapshot["grace_period_days"])
            except (ValueError, TypeError):
                pass

    from django.conf import settings

    val = getattr(settings, "EVENTYAY_BUSINESS_GRACE_PERIOD_DAYS", None)
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass

    try:
        from eventyay.base.settings import GlobalSettingsObject

        gs = GlobalSettingsObject()
        gs_val = gs.settings.get("business_grace_period_days", as_type=int)
        if gs_val is not None:
            return int(gs_val)
    except Exception:
        pass

    return 7
