from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

try:
    from eventyay.control.signals import nav_global
except ImportError:
    nav_global = None


if nav_global:

    @receiver(nav_global, dispatch_uid="business_tiers_nav")
    def business_tiers_nav(sender, request, **kwargs):
        url = request.resolver_match
        if not url:
            return []

        return [
            {
                "label": _("Tiers"),
                "url": reverse("eventyay_business:tiers.list"),
                "active": (
                    url.namespace == "eventyay_business"
                    and url.url_name.startswith("tiers.")
                ),
                "parent": reverse("eventyay_admin:admin.global.business"),
            }
        ]
