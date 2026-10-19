import datetime
import logging
from django.db import DatabaseError, transaction
from django.db.models import Q, Sum
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from eventyay.base.models import OrderRefund, Organizer
from eventyay.base.models.organizer import OrganizerBillingModel

from .models import (
    BillingCollectionAuthorization,
    BillingNotificationEventType,
)

logger = logging.getLogger(__name__)

addon_canceled = Signal()
addon_expired = Signal()
addon_purchased = Signal()
subscription_purchased = Signal()
subscription_downgraded = Signal()
subscription_expired = Signal()
subscription_change_scheduled = Signal()
subscription_change_canceled = Signal()
subscription_past_due = Signal()
subscription_recovered = Signal()
subscription_canceled = Signal()
addon_cancellation_scheduled = Signal()

AUTOMATIC_COLLECTION_TERMS_VERSION = "2"


def _resolve_payment_date(*, order, payment=None, payment_date=None):
    if not isinstance(payment_date, (datetime.date, datetime.datetime)):
        payment_date = None

    if not payment_date:
        candidate = getattr(payment, "payment_date", None)
        if isinstance(candidate, (datetime.date, datetime.datetime)):
            payment_date = candidate

    if not payment_date and hasattr(order, "payments"):
        try:
            confirmed_payment = (
                order.payments.filter(state="confirmed")
                .order_by("-payment_date")
                .first()
            )
            candidate = getattr(confirmed_payment, "payment_date", None)
            if isinstance(candidate, (datetime.date, datetime.datetime)):
                payment_date = candidate
        except (AttributeError, DatabaseError):
            pass

    if not payment_date:
        candidate = getattr(order, "datetime", None)
        if isinstance(candidate, (datetime.date, datetime.datetime)):
            payment_date = candidate
        else:
            payment_date = now()

    return payment_date


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
    from eventyay.control.signals import platform_mail_templates
except ImportError:
    platform_mail_templates = None

try:
    from eventyay.base.entitlements import EntitlementDecision
except ImportError:
    EntitlementDecision = None

try:
    from eventyay.base.platform_fees import PlatformFeePolicy
    from eventyay.base.signals import platform_fee_policy
except ImportError:
    PlatformFeePolicy = None
    platform_fee_policy = None

try:
    from eventyay.base.signals import legacy_billing_collection_disabled
except ImportError:
    legacy_billing_collection_disabled = None

try:
    from eventyay.base.signals import organizer_billing_payment_method_saved
except ImportError:
    organizer_billing_payment_method_saved = None

try:
    from eventyay.base.signals import (
        entitlement_check,
        entitlement_usage_recorded,
        order_canceled,
        order_paid,
        register_entitlements,
    )
except ImportError:
    register_entitlements = None
    entitlement_check = None
    entitlement_usage_recorded = None
    order_canceled = None
    order_paid = None


if platform_fee_policy and PlatformFeePolicy:

    @receiver(platform_fee_policy, dispatch_uid="business_platform_fee_policy")
    def provide_platform_fee_policy(sender, order, payment, **kwargs):
        from .models import Subscription
        from .services import resolve_fee_settings

        payment_date = _resolve_payment_date(
            order=order,
            payment=payment,
            payment_date=kwargs.get("payment_date"),
        )
        subscription = (
            Subscription.objects.filter(
                organizer=sender.organizer,
                status="active",
                starts_at__lte=payment_date,
            )
            .exclude(ends_at__lt=payment_date)
            .select_related("tier_version")
            .first()
        )
        fee_percent, maximum_fee, is_override = resolve_fee_settings(
            event=sender,
            order=order,
            tier_version=subscription.tier_version if subscription else None,
        )
        return PlatformFeePolicy(
            percentage=fee_percent,
            maximum_fee=maximum_fee,
            is_override=is_override,
            source="eventyay-business",
        )


def business_notification_template_loader(event_type):
    def load():
        from .notifications import NOTIFICATION_PLACEHOLDERS, NOTIFICATION_SPECS

        spec = NOTIFICATION_SPECS[event_type]
        context = {name: "{" + name + "}" for name in NOTIFICATION_PLACEHOLDERS}
        context["event_type"] = event_type
        return str(spec.subject), render_to_string(spec.template_name, context)

    return load


def business_notification_preview_context():
    return {
        "organizer_name": "Example Organizer",
        "object_name": "Professional",
        "event_name": "Example Conference",
        "invoice_number": "INV-EXAMPLE-001",
        "amount": "49.00",
        "currency": "EUR",
        "issue_date": "2026-10-19",
        "effective_date": "2026-10-19",
        "deadline": "2026-10-22",
        "next_retry_at": "2026-10-20",
        "days_remaining": "3",
        "current_state": "Active",
        "failure_code": "stripe_unavailable",
        "action_url": "https://eventyay.example/control/",
        "support_email": "support@eventyay.example",
    }


def register_business_mail_templates(sender, **kwargs):
    return [
        {
            "key": f"business-{event_type}",
            "name": event_type.label,
            "category": _("Business"),
            "trigger": event_type.label,
            "recipient_type": (
                _("Platform administrator")
                if event_type == BillingNotificationEventType.PLATFORM_ALERT
                else _("Organizer billing contact")
            ),
            "load": business_notification_template_loader(event_type),
            "preview": business_notification_preview_context(),
            "url": reverse(
                "plugins:eventyay_business:notifications.template",
                kwargs={"event_type": event_type},
            ),
        }
        for event_type in BillingNotificationEventType
    ]


if platform_mail_templates:
    receiver(
        platform_mail_templates,
        dispatch_uid="business_platform_mail_templates",
    )(register_business_mail_templates)


if legacy_billing_collection_disabled:

    @receiver(
        legacy_billing_collection_disabled,
        dispatch_uid="business_disable_legacy_billing_collection",
    )
    def disable_legacy_billing_collection(sender, organizer, **kwargs):
        from .platform_fee_ledger import organizer_consolidated_billing_enabled

        return organizer_consolidated_billing_enabled(organizer)


def record_automatic_collection_authorization(
    organizer, user, stripe_customer_id: str
) -> BillingCollectionAuthorization | None:
    billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
    if (
        not billing
        or not stripe_customer_id
        or billing.stripe_customer_id != stripe_customer_id
    ):
        return None

    authorized_by = user if user and user.is_authenticated else None
    with transaction.atomic():
        locked_organizer = Organizer.objects.select_for_update().get(pk=organizer.pk)
        authorization, _ = BillingCollectionAuthorization.objects.update_or_create(
            organizer=locked_organizer,
            defaults={
                "stripe_customer_id": stripe_customer_id,
                "authorized_at": now(),
                "authorized_by": authorized_by,
                "revoked_at": None,
                "terms_version": AUTOMATIC_COLLECTION_TERMS_VERSION,
            },
        )
        from .tasks import retry_outstanding_business_invoices_task

        def retry_outstanding_invoices(organizer_id=organizer.pk):
            delay = getattr(retry_outstanding_business_invoices_task, "delay", None)
            if delay:
                return delay(organizer_id)
            return retry_outstanding_business_invoices_task(organizer_id)

        transaction.on_commit(
            retry_outstanding_invoices,
            robust=True,
        )
    return authorization


if organizer_billing_payment_method_saved:

    @receiver(
        organizer_billing_payment_method_saved,
        dispatch_uid="business_enable_automatic_collection_on_payment_method_save",
    )
    def enable_automatic_collection_on_payment_method_save(
        sender, organizer, user, stripe_customer_id, **kwargs
    ):
        authorization = record_automatic_collection_authorization(
            organizer=organizer,
            user=user,
            stripe_customer_id=stripe_customer_id,
        )
        if authorization:
            from .notifications import queue_organizer_notification

            billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
            payment_method_id = (
                billing.stripe_payment_method_id if billing else "unknown"
            )
            transaction.on_commit(
                lambda: queue_organizer_notification(
                    organizer=organizer,
                    event_type=BillingNotificationEventType.PAYMENT_METHOD_UPDATED,
                    logical_event_key=(
                        f"organizer:{organizer.pk}:payment-method-updated:"
                        f"{payment_method_id}"
                    ),
                ),
                robust=True,
            )
        return authorization


def _queue_subscription_lifecycle(
    instance, event_type, key_suffix="", **context_values
):
    from .notifications import queue_subscription_notification

    transaction.on_commit(
        lambda: queue_subscription_notification(
            instance,
            event_type,
            key_suffix=key_suffix,
            **context_values,
        ),
        robust=True,
    )


def _queue_addon_lifecycle(instance, event_type, key_suffix=""):
    from .notifications import queue_addon_notification

    transaction.on_commit(
        lambda: queue_addon_notification(
            instance,
            event_type,
            key_suffix=key_suffix,
        ),
        robust=True,
    )


@receiver(subscription_purchased, dispatch_uid="business_notify_subscription_purchased")
def notify_subscription_purchased(sender, instance, **kwargs):
    _queue_subscription_lifecycle(
        instance,
        BillingNotificationEventType.PLAN_ACTIVATED,
        key_suffix=f"{instance.tier_version_id}:{instance.starts_at.isoformat()}",
    )


@receiver(subscription_downgraded, dispatch_uid="business_notify_subscription_changed")
def notify_subscription_changed(sender, instance, cancellation=False, **kwargs):
    event_type = (
        BillingNotificationEventType.PLAN_CANCELED
        if cancellation
        else BillingNotificationEventType.PLAN_CHANGED
    )
    _queue_subscription_lifecycle(
        instance,
        event_type,
        key_suffix=f"{instance.tier_version_id}:{instance.updated_at.isoformat()}",
    )


@receiver(subscription_expired, dispatch_uid="business_notify_subscription_expired")
def notify_subscription_expired(sender, instance, grace_deadline=None, **kwargs):
    _queue_subscription_lifecycle(
        instance,
        BillingNotificationEventType.ACCESS_EXPIRED,
        key_suffix=(
            grace_deadline.isoformat()
            if grace_deadline
            else instance.updated_at.isoformat()
        ),
        deadline=grace_deadline,
    )


@receiver(subscription_past_due, dispatch_uid="business_notify_subscription_past_due")
def notify_subscription_past_due(sender, instance, **kwargs):
    _queue_subscription_lifecycle(
        instance,
        BillingNotificationEventType.GRACE_PERIOD_STARTED,
        key_suffix=(
            instance.past_due_since.isoformat()
            if instance.past_due_since
            else instance.updated_at.isoformat()
        ),
    )


@receiver(subscription_recovered, dispatch_uid="business_notify_subscription_recovered")
def notify_subscription_recovered(sender, instance, recovery_marker="", **kwargs):
    _queue_subscription_lifecycle(
        instance,
        BillingNotificationEventType.ACCESS_RESTORED,
        key_suffix=recovery_marker or instance.updated_at.isoformat(),
    )


@receiver(subscription_canceled, dispatch_uid="business_notify_subscription_canceled")
def notify_subscription_canceled(sender, instance, **kwargs):
    _queue_subscription_lifecycle(
        instance,
        BillingNotificationEventType.PLAN_CANCELED,
        key_suffix=(
            instance.cancel_at.isoformat()
            if instance.cancel_at
            else instance.updated_at.isoformat()
        ),
    )


@receiver(
    subscription_change_scheduled,
    dispatch_uid="business_notify_subscription_change_scheduled",
)
def notify_subscription_change_scheduled(
    sender, instance, cancellation=False, **kwargs
):
    event_type = (
        BillingNotificationEventType.PLAN_CANCELLATION_SCHEDULED
        if cancellation
        else BillingNotificationEventType.PLAN_CHANGE_SCHEDULED
    )
    context_values = {}
    if cancellation:
        context_values["deadline"] = instance.pending_change_at
    _queue_subscription_lifecycle(
        instance,
        event_type,
        key_suffix=(
            instance.pending_change_at.isoformat()
            if instance.pending_change_at
            else instance.updated_at.isoformat()
        ),
        **context_values,
    )


@receiver(
    subscription_change_canceled,
    dispatch_uid="business_notify_subscription_change_canceled",
)
def notify_subscription_change_canceled(
    sender, instance, cancellation=False, canceled_marker="", **kwargs
):
    event_type = (
        BillingNotificationEventType.PLAN_CANCELLATION_CANCELED
        if cancellation
        else BillingNotificationEventType.PLAN_CHANGE_CANCELED
    )
    _queue_subscription_lifecycle(
        instance,
        event_type,
        key_suffix=canceled_marker or instance.updated_at.isoformat(),
    )


@receiver(addon_purchased, dispatch_uid="business_notify_addon_purchased")
def notify_addon_purchased(sender, instance, **kwargs):
    _queue_addon_lifecycle(
        instance,
        BillingNotificationEventType.ADDON_ACTIVATED,
        key_suffix=instance.starts_at.isoformat(),
    )


@receiver(
    addon_cancellation_scheduled,
    dispatch_uid="business_notify_addon_cancellation_scheduled",
)
def notify_addon_cancellation_scheduled(sender, instance, **kwargs):
    _queue_addon_lifecycle(
        instance,
        BillingNotificationEventType.ADDON_CANCELLATION_SCHEDULED,
        key_suffix=(
            instance.cancel_at.isoformat()
            if instance.cancel_at
            else instance.updated_at.isoformat()
        ),
    )


@receiver(addon_canceled, dispatch_uid="business_notify_addon_canceled")
def notify_addon_canceled(sender, instance, **kwargs):
    _queue_addon_lifecycle(
        instance,
        BillingNotificationEventType.ADDON_CANCELED,
        key_suffix=(
            instance.cancel_at.isoformat()
            if instance.cancel_at
            else instance.updated_at.isoformat()
        ),
    )


@receiver(addon_expired, dispatch_uid="business_notify_addon_expired")
def notify_addon_expired(sender, instance, **kwargs):
    _queue_addon_lifecycle(
        instance,
        BillingNotificationEventType.ADDON_EXPIRED,
        key_suffix=(
            instance.ends_at.isoformat()
            if instance.ends_at
            else instance.updated_at.isoformat()
        ),
    )


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
                "label": _("Business Settings"),
                "url": reverse("plugins:eventyay_business:settings"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name == "settings"
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
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
            {
                "label": _("Current charges"),
                "url": reverse("plugins:eventyay_business:current_charges.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("current_charges.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
            {
                "label": _("Invoices"),
                "url": reverse("plugins:eventyay_business:invoices.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("invoices.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
            {
                "label": _("Notification delivery"),
                "url": reverse("plugins:eventyay_business:notifications.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("notifications.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
            {
                "label": _("Billing reconciliation"),
                "url": reverse("plugins:eventyay_business:reconciliation.list"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("reconciliation.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
            {
                "label": _("Billing activation"),
                "url": reverse("plugins:eventyay_business:billing_activation"),
                "active": (
                    url.namespace == "plugins:eventyay_business"
                    and url.url_name.startswith("billing_activation")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            },
        ]


if register_entitlements:

    @receiver(register_entitlements, dispatch_uid="business_register_capabilities")
    def register_capabilities_receiver(sender, **kwargs):
        from .capabilities import default_registry

        return default_registry.as_dict()


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

        addon_statuses = [AddonStatus.ACTIVE]
        if sub and sub.status == SubscriptionStatus.PAST_DUE:
            addon_statuses.append(AddonStatus.PAST_DUE)

        active_org_addons = list(
            OrganizerAddon.objects.filter(
                organizer=organizer,
                addon__active=True,
                status__in=addon_statuses,
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
                    status__in=addon_statuses,
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
                    and (
                        url.url_name.startswith("organizer.plan")
                        or url.url_name.startswith("organizer.invoices")
                        or url.url_name.startswith("organizer.current_charges")
                    )
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
        from .platform_fee_ledger import record_platform_fees_for_paid_order

        return record_platform_fees_for_paid_order(sender, order)


if order_canceled:

    @receiver(order_canceled, dispatch_uid="business_order_canceled_fee")
    def record_platform_fee_on_order_canceled(sender, order, **kwargs):
        from .platform_fee_ledger import record_platform_fee_cancellation

        return record_platform_fee_cancellation(sender, order)


@receiver(post_save, sender=OrderRefund, dispatch_uid="business_refund_platform_fee")
def record_platform_fee_on_refund(sender, instance, **kwargs):
    if instance.state != OrderRefund.REFUND_STATE_DONE:
        return []

    from .platform_fee_ledger import record_platform_fee_refund

    return record_platform_fee_refund(instance)
