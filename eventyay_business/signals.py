import logging
from django.db import IntegrityError
from django.db.models import Q, Sum
from django.dispatch import Signal, receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

addon_canceled = Signal()
addon_expired = Signal()
addon_purchased = Signal()
subscription_purchased = Signal()
subscription_downgraded = Signal()
subscription_expired = Signal()

try:
    from eventyay.control.signals import (
        nav_event_settings,
        nav_global,
        nav_organizer,
    )
except ImportError:
    nav_global = None
    nav_organizer = None
    nav_event_settings = None

try:
    from eventyay.base.entitlements import EntitlementDecision
except ImportError:
    EntitlementDecision = None

try:
    from eventyay.base.signals import (
        entitlement_check,
        entitlement_usage_recorded,
        order_paid,
        register_entitlements,
    )
except ImportError:
    register_entitlements = None
    entitlement_check = None
    entitlement_usage_recorded = None
    order_paid = None


if nav_global:

    @receiver(nav_global, dispatch_uid="business_tiers_nav")
    def business_tiers_nav(sender, request, **kwargs):
        url = request.resolver_match
        if not url:
            return []

        user = getattr(request, "user", None)
        if (
            not user
            or not user.is_authenticated
            or not (user.is_staff or user.is_superuser)
        ):
            return []

        path = getattr(request, "path_info", "") or getattr(request, "path", "")
        # Tiers and Subscriptions are global administrative configuration.
        # Only show them in the admin navigation area, not in public-facing or common user dashboards.
        is_admin_area = (
            url.namespace == "eventyay_admin"
            or (
                url.namespace == "plugins:eventyay_business"
                and not url.url_name.startswith("organizer.")
            )
            or url.url_name.startswith("admin.")
            or path.startswith("/admin/")
        )
        if not is_admin_area:
            return []

        return [
            {
                "label": _("Tiers"),
                "url": reverse("plugins:eventyay_business:tiers.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("tiers.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
            {
                "label": _("Subscriptions"),
                "url": reverse("plugins:eventyay_business:subscriptions.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("subscriptions.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
            {
                "label": _("Add-ons"),
                "url": reverse("plugins:eventyay_business:addons.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("addons.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
        ]


if register_entitlements:

    @receiver(register_entitlements, dispatch_uid="business_register_capabilities")
    def register_capabilities_receiver(sender, **kwargs):
        from .capabilities import default_registry

        return default_registry.as_dict()


from django.db.models.signals import post_save
from django.utils.timezone import now
from eventyay.base.models import Organizer


@receiver(post_save, sender=Organizer, dispatch_uid="business_assign_free_tier")
def auto_assign_free_tier(sender, instance, created, **kwargs):
    if not created:
        return

    from .models import (
        Subscription,
        SubscriptionStatus,
        Tier,
        TierStatus,
        TierVersion,
    )
    from .services import seed_standard_entitlements_for_version

    free_tier, _ = Tier.objects.get_or_create(
        slug="free",
        defaults={
            "name": "Free",
            "description": "Default free tier",
            "is_public": True,
            "status": TierStatus.PUBLISHED,
        },
    )
    if free_tier.status == TierStatus.DRAFT:
        free_tier.status = TierStatus.PUBLISHED
        free_tier.save(update_fields=["status"])

    latest_version = (
        free_tier.versions.filter(published_at__isnull=False)
        .order_by("-version")
        .first()
    )
    if not latest_version:
        latest_version, _ = TierVersion.objects.get_or_create(
            tier=free_tier, version=1, defaults={"published_at": now()}
        )
        if latest_version.published_at is None:
            latest_version.published_at = now()
            latest_version.save(update_fields=["published_at"])

    seed_standard_entitlements_for_version(latest_version)

    Subscription.objects.create(
        organizer=instance,
        tier_version=latest_version,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )


if entitlement_check and EntitlementDecision:

    @receiver(entitlement_check, dispatch_uid="business_entitlement_check")
    def enforce_entitlements(
        sender, capability: str, event=None, quantity: int = 1, **kwargs
    ):
        from .capabilities import CapabilityValueType, get_capability
        from .models import Subscription, SubscriptionStatus

        # Platform administrators always retain access
        user = kwargs.get("user")
        if user and (
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        ):
            return EntitlementDecision(allowed=True)

        organizer = sender

        cap_def = get_capability(capability)
        if not cap_def:
            return None

        from django.utils.timezone import now

        current_time = now()
        sub = (
            Subscription.objects.filter(
                organizer=organizer,
                status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE],
                starts_at__lte=current_time,
            )
            .filter(
                Q(ends_at__isnull=True)
                | Q(ends_at__gte=current_time)
                | Q(status=SubscriptionStatus.PAST_DUE)
            )
            .select_related("tier_version")
            .first()
        )

        # If past_due, check if still within grace period
        if (
            sub
            and sub.status == SubscriptionStatus.PAST_DUE
            and not sub.is_in_grace_period()
        ):
            sub = None

        value = None
        if sub and sub.tier_version:
            ent = sub.tier_version.entitlements.filter(capability=capability).first()
            if ent:
                value = ent.get_typed_value()

        if value is None:
            value = cap_def.default_value

        from .models import AddonStatus, EventAddon, OrganizerAddon

        active_org_addons = list(
            OrganizerAddon.objects.filter(
                organizer=organizer,
                addon__active=True,
                status=AddonStatus.ACTIVE,
                starts_at__lte=current_time,
            )
            .filter(
                Q(capability=capability)
                | Q(capability="", addon__capability=capability)
            )
            .exclude(ends_at__lt=current_time)
            .exclude(cancel_at__lte=current_time)
            .select_related("addon")
        )

        active_event_addons = []
        if event:
            active_event_addons = list(
                EventAddon.objects.filter(
                    event=event,
                    addon__active=True,
                    status=AddonStatus.ACTIVE,
                    starts_at__lte=current_time,
                )
                .filter(
                    Q(capability=capability)
                    | Q(capability="", addon__capability=capability)
                )
                .exclude(ends_at__lt=current_time)
                .exclude(cancel_at__lte=current_time)
                .select_related("addon")
            )

        all_active_addons = active_org_addons + active_event_addons

        if cap_def.value_type == CapabilityValueType.BOOLEAN:
            addon_grants = any(bool(a.get_typed_value()) for a in all_active_addons)
            if value or addon_grants:
                return EntitlementDecision(allowed=True)
            else:
                return EntitlementDecision(
                    allowed=False,
                    reason_code="tier_restriction",
                    message="This feature is not available on your current plan.",
                )

        if cap_def.value_type == CapabilityValueType.INTEGER:
            addon_allowance = sum(
                a.quantity
                * int(
                    a.get_typed_value()
                    if a.get_typed_value() is not None
                    else (a.addon.quantity or 1)
                )
                for a in all_active_addons
            )
            if value is not None:
                effective_limit = value + addon_allowance
            elif addon_allowance > 0:
                effective_limit = addon_allowance
            else:
                effective_limit = None

            total_quantity = quantity
            if capability.endswith(".monthly"):
                from .models import UsageRecord

                usage_agg = UsageRecord.objects.filter(
                    organizer=organizer,
                    capability=capability,
                    occurred_at__year=current_time.year,
                    occurred_at__month=current_time.month,
                ).aggregate(total=Sum("quantity"))

                past_usage = usage_agg["total"] or 0
                total_quantity = quantity + int(past_usage)

            if effective_limit is not None and total_quantity > effective_limit:
                return EntitlementDecision(
                    allowed=False,
                    reason_code="tier_limit_exceeded",
                    limit=effective_limit,
                    used=past_usage if capability.endswith(".monthly") else None,
                    message="You have reached the maximum limit for this feature on your current plan.",
                )
            return EntitlementDecision(allowed=True, limit=effective_limit)

        return EntitlementDecision(allowed=True)


if nav_organizer:

    @receiver(nav_organizer, dispatch_uid="business_organizer_plan_nav")
    def business_organizer_plan_nav(sender, request, organizer, **kwargs):
        url = request.resolver_match
        if not url:
            return []

        return [
            {
                "label": _("Plan & Billing"),
                "url": reverse(
                    "plugins:eventyay_business:organizer.plan",
                    kwargs={"organizer": organizer.slug},
                ),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name == "organizer.plan"
                ),
                "icon": "credit-card",
                "position": 100,
            }
        ]


if nav_event_settings:

    @receiver(nav_event_settings, dispatch_uid="business_event_addons_nav")
    def business_event_addons_nav(sender, request, **kwargs):
        url = getattr(request, "resolver_match", None)
        if not url:
            return []

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return []

        organizer = getattr(request, "organizer", None) or getattr(
            sender, "organizer", None
        )
        event = getattr(request, "event", None) or sender
        if not organizer or not event:
            return []

        if not user.has_event_permission(
            organizer, event, "can_change_event_settings", request=request
        ):
            return []

        return [
            {
                "label": _("Add-ons & Modules"),
                "url": reverse(
                    "plugins:eventyay_business:event.addons",
                    kwargs={
                        "organizer": organizer.slug,
                        "event": event.slug,
                    },
                ),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("event.addon")
                ),
                "icon": "puzzle-piece",
            }
        ]


if entitlement_usage_recorded:

    @receiver(
        entitlement_usage_recorded, dispatch_uid="business_entitlement_usage_recorded"
    )
    def handle_usage_recorded(
        sender,
        capability: str,
        quantity: float,
        unit: str,
        source_type: str,
        source_id: str,
        idempotency_key: str,
        event=None,
        metadata=None,
        **kwargs,
    ):
        from .services import record_usage

        organizer = sender

        # We process usage asynchronously if possible, but for now we record it instantly.
        # Idempotency prevents duplicates from multiple identical signal dispatches.
        record_usage(
            organizer=organizer,
            event=event,
            capability=capability,
            quantity=quantity,
            unit=unit,
            source_type=source_type,
            source_id=source_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )


if order_paid:

    @receiver(order_paid, dispatch_uid="business_order_paid_fee")
    def record_platform_fee_on_order_paid(sender, order, **kwargs):
        from decimal import Decimal
        from django.utils.timezone import now

        from .models import Subscription, UsageRecord

        event = sender
        organizer = event.organizer

        import datetime

        payment_date = kwargs.get("payment_date")
        if not isinstance(payment_date, (datetime.date, datetime.datetime)):
            payment_date = None

        if not payment_date:
            payment = kwargs.get("payment")
            cand = getattr(payment, "payment_date", None)
            if isinstance(cand, (datetime.date, datetime.datetime)):
                payment_date = cand

        if not payment_date and hasattr(order, "payments"):
            try:
                confirmed_payment = (
                    order.payments.filter(state="confirmed")
                    .order_by("-payment_date")
                    .first()
                )
                cand = getattr(confirmed_payment, "payment_date", None)
                if isinstance(cand, (datetime.date, datetime.datetime)):
                    payment_date = cand
            except Exception:
                pass

        if not payment_date:
            cand = getattr(order, "datetime", None)
            if isinstance(cand, (datetime.date, datetime.datetime)):
                payment_date = cand
            else:
                payment_date = now()

        sub = (
            Subscription.objects.filter(
                organizer=organizer,
                status="active",
                starts_at__lte=payment_date,
            )
            .exclude(ends_at__lt=payment_date)
            .select_related("tier_version")
            .first()
        )

        if not sub or not sub.tier_version:
            return

        from .capabilities import get_capability

        cap_def = get_capability("commerce.platform_fee_percent")
        if not cap_def:
            return

        ent = sub.tier_version.entitlements.filter(
            capability="commerce.platform_fee_percent"
        ).first()

        if ent:
            fee_percent = ent.get_typed_value()
        else:
            fee_percent = cap_def.default_value

        if not fee_percent or fee_percent <= Decimal("0.0"):
            return

        # Calculate fee base: sum of all active positions (price - tax)
        # Excludes shipping, payment fees (which are OrderFee objects)
        fee_base = Decimal("0.0")
        # In Eventyay, order.positions is the default manager that excludes canceled positions
        for pos in order.positions.all():
            # some old data might have None for tax_value, default to 0
            tax = pos.tax_value or Decimal("0.0")
            fee_base += pos.price - tax

        if fee_base <= Decimal("0.0"):
            return

        fee_amount = (fee_base * fee_percent / Decimal("100.0")).quantize(
            Decimal("0.01")
        )

        if fee_amount <= Decimal("0.0"):
            return

        metadata = {
            "fee_base": str(fee_base),
            "fee_percent": str(fee_percent),
            "order_total": str(order.total),
            "currency": event.currency,
        }

        if sub.currency and sub.currency != event.currency:
            from eventyay.base.settings import GlobalSettingsObject

            gs = GlobalSettingsObject()
            rates_dict = gs.settings.get("ecb_rates_dict", as_type=dict)
            rates_date = gs.settings.get("ecb_rates_date")

            if not (
                rates_dict
                and event.currency in rates_dict
                and sub.currency in rates_dict
            ):
                logger.warning(
                    "Exchange rate unavailable for converting platform fee from %s to %s for order %s. Stopping fee persistence.",
                    event.currency,
                    sub.currency,
                    order.code,
                )
                return

            from decimal import ROUND_HALF_UP

            rate = (
                Decimal(rates_dict[sub.currency]) / Decimal(rates_dict[event.currency])
            ).quantize(Decimal("0.0001"), ROUND_HALF_UP)

            converted_fee = (fee_amount * rate).quantize(Decimal("0.01"))
            metadata["billing_currency"] = sub.currency
            metadata["exchange_rate"] = str(rate)
            metadata["exchange_rate_date"] = str(rates_date)
            metadata["billing_currency_fee_amount"] = str(converted_fee)

        try:
            UsageRecord.objects.get_or_create(
                organizer=organizer,
                idempotency_key=f"order_{order.code}_platform_fee",
                defaults={
                    "event": event,
                    "capability": "commerce.platform_fee_percent",
                    "quantity": fee_amount,
                    "unit": event.currency,
                    "source_type": "order",
                    "source_id": order.code,
                    "occurred_at": payment_date,
                    "metadata": metadata,
                },
            )
        except IntegrityError:
            pass
