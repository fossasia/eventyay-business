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
            }
        ]


if register_entitlements:

    @receiver(register_entitlements, dispatch_uid="business_register_capabilities")
    def register_capabilities_receiver(sender, **kwargs):
        from .capabilities import default_registry

        return default_registry.as_dict()
