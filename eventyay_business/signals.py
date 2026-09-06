from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

try:
    from eventyay.control.signals import nav_global, nav_organizer
except ImportError:
    nav_global = None
    nav_organizer = None

try:
    from eventyay.base.entitlements import EntitlementDecision
except ImportError:
    EntitlementDecision = None

try:
    from eventyay.base.signals import register_entitlements, entitlement_check
except ImportError:
    register_entitlements = None
    entitlement_check = None


if nav_global:

    @receiver(nav_global, dispatch_uid="business_tiers_nav")
    def business_tiers_nav(sender, request, **kwargs):
        url = request.resolver_match
        if not url:
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

    from .models import Subscription, SubscriptionStatus, Tier, TierVersion

    free_tier, _ = Tier.objects.get_or_create(
        slug="free",
        defaults={
            "name": "Free",
            "description": "Default free tier",
            "is_public": True,
        }
    )
    
    latest_version = free_tier.versions.filter(published_at__isnull=False).order_by("-version").first()
    if not latest_version:
        latest_version, _ = TierVersion.objects.get_or_create(
            tier=free_tier,
            version=1,
            defaults={"published_at": now()}
        )
    
    Subscription.objects.create(
        organizer=instance,
        tier_version=latest_version,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )


if entitlement_check and EntitlementDecision:

    @receiver(entitlement_check, dispatch_uid="business_entitlement_check")
    def enforce_entitlements(sender, capability: str, event=None, quantity: int = 1, **kwargs):
        from .capabilities import get_capability, CapabilityValueType
        from .models import Subscription
        
        organizer = sender
        
        cap_def = get_capability(capability)
        if not cap_def:
            return None
            
        sub = Subscription.objects.filter(
            organizer=organizer, 
            status__in=["active", "pending"]
        ).select_related("tier_version").first()
        
        value = None
        if sub and sub.tier_version:
            ent = sub.tier_version.entitlements.filter(capability=capability).first()
            if ent:
                value = ent.get_typed_value()
                
        if value is None:
            value = cap_def.default_value
            
        if cap_def.value_type == CapabilityValueType.BOOLEAN:
            if value:
                return EntitlementDecision(allowed=True)
            else:
                return EntitlementDecision(allowed=False, reason_code="tier_restriction", message="This feature is not available on your current plan.")
                
        if cap_def.value_type == CapabilityValueType.INTEGER:
            if value is not None and quantity > value:
                return EntitlementDecision(allowed=False, reason_code="tier_limit_exceeded", limit=value, message="You have reached the maximum limit for this feature on your current plan.")
            return EntitlementDecision(allowed=True, limit=value)
            
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
