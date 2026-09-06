from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

try:
    from eventyay.control.signals import nav_global
except ImportError:
    nav_global = None


try:
    from eventyay.base.signals import register_entitlements
except ImportError:
    register_entitlements = None


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

    from .models import Subscription, SubscriptionStatus, Tier

    free_tier = Tier.objects.filter(slug="free").first()
    if not free_tier:
        free_tier = Tier.objects.create(
            slug="free",
            name="Free",
            description="Default free tier",
            is_public=True,
        )
    
    latest_version = free_tier.versions.order_by("-version").first()
    if not latest_version:
        from .models import TierVersion
        latest_version = TierVersion.objects.create(
            tier=free_tier,
            version=1,
            published_at=now()
        )
    
    Subscription.objects.create(
        organizer=instance,
        tier_version=latest_version,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )
