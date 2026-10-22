"""Public pricing page listing the published tiers and add-ons."""

from django.db import models
from django.http import Http404, HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from eventyay.base.models.global_plugin_config import GlobalPluginConfig
from eventyay.base.models.page import Page
from eventyay.base.settings import GlobalSettingsObject
from eventyay.common.permissions import is_admin_mode_active
from eventyay.eventyay_common.navigation import get_global_navigation

from .models import AddonDefinition, Tier, TierStatus

PRICING_PAGE_VISIBILITY_SETTING = "business_pricing_page_visibility"
PRICING_SETTING_PREFIX = "business_pricing_"
# Sections of the pricing page administrators can switch on and off, with defaults.
PRICING_PAGE_SWITCHES = {
    "business_pricing_show_tiers": True,
    "business_pricing_show_features": True,
    "business_pricing_show_developer_features": True,
    "business_pricing_show_addons": True,
    "business_pricing_show_signup_button": True,
}
# Texts administrators can edit; when blank the page shows its translated default.
PRICING_PAGE_TEXTS = (
    "business_pricing_title",
    "business_pricing_intro",
    "business_pricing_signup_label",
    "business_pricing_signup_url",
    "business_pricing_footer_note",
)


class PricingPageVisibility(models.TextChoices):
    EVERYONE = "everyone", _("Everyone")
    SIGNED_IN = "signed_in", _("Signed-in users")
    ADMINS = "admins", _("Platform administrators only")


def pricing_page_visibility() -> str:
    visibility = GlobalSettingsObject().settings.get(
        PRICING_PAGE_VISIBILITY_SETTING,
        as_type=str,
        default=PricingPageVisibility.ADMINS,
    )
    if visibility not in PricingPageVisibility.values:
        return PricingPageVisibility.ADMINS
    return visibility


def pricing_page_settings() -> dict[str, bool | str]:
    """Return the pricing page switches and texts, keyed without the setting prefix."""
    global_settings = GlobalSettingsObject().settings
    values = {
        key: global_settings.get(key, as_type=bool, default=default)
        for key, default in PRICING_PAGE_SWITCHES.items()
    }
    values.update(
        {
            key: (global_settings.get(key, as_type=str, default="") or "").strip()
            for key in PRICING_PAGE_TEXTS
        }
    )
    return {
        key.removeprefix(PRICING_SETTING_PREFIX): value for key, value in values.items()
    }


def public_tier_offers() -> list[dict]:
    """Return the public, published tiers with their latest version, prices and features."""
    tiers = (
        Tier.objects.filter(status=TierStatus.PUBLISHED, is_public=True)
        .order_by("display_order", "id")
        .prefetch_related("versions__prices", "versions__entitlements")
    )
    offers = []
    for tier in tiers:
        latest_version = (
            tier.versions.filter(published_at__isnull=False)
            .order_by("-version")
            .first()
        )
        if not latest_version:
            continue
        prices = list(
            latest_version.prices.filter(active=True).order_by(
                "billing_interval", "amount"
            )
        )
        entitlements = list(latest_version.entitlements.all())
        offers.append(
            {
                "tier": tier,
                "version": latest_version,
                "prices": prices,
                "entitlements": entitlements,
                "organizer_features": [
                    ent
                    for ent in entitlements
                    if ent.capability_audience != "developer"
                ],
                "developer_features": [
                    ent
                    for ent in entitlements
                    if ent.capability_audience == "developer"
                ],
            }
        )
    return offers


def is_platform_admin(request: HttpRequest) -> bool:
    user = request.user
    return user.is_authenticated and (user.is_staff or user.is_administrator)


def can_view_pricing_page(request: HttpRequest) -> bool:
    """Platform administrators can always view the page, to review it before release."""
    visibility = pricing_page_visibility()
    if visibility == PricingPageVisibility.EVERYONE or is_platform_admin(request):
        return True
    return (
        visibility == PricingPageVisibility.SIGNED_IN and request.user.is_authenticated
    )


def plan_purchase_links(request: HttpRequest) -> list[dict]:
    """Return the plan upgrade pages of the organizers the signed-in user can buy for."""
    if not request.user.is_authenticated:
        return []
    # Without the request, admin mode does not widen this to every organizer.
    organizers = request.user.get_organizers_with_permission(
        "can_change_organizer_settings"
    ).order_by("name")
    return [
        {
            "organizer": organizer,
            "url": reverse(
                "plugins:eventyay_business:organizer.plan.upgrade",
                kwargs={"organizer": organizer.slug},
            ),
        }
        for organizer in organizers
    ]


class BusinessPricingView(TemplateView):
    template_name = "eventyay_business/pricing/pricing.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page = pricing_page_settings()
        context["page"] = page
        tier_list = public_tier_offers() if page["show_tiers"] else []
        for item in tier_list:
            item["paid_prices"] = [
                price for price in item["prices"] if price.amount > 0
            ]
            if not page["show_developer_features"]:
                item["developer_features"] = []
        context["tier_list"] = tier_list
        context["addons"] = (
            AddonDefinition.objects.filter(active=True, public=True).order_by(
                "assignment_scope", "name"
            )
            if page["show_addons"]
            else []
        )
        context["visibility"] = pricing_page_visibility()
        # Only in admin mode, so administrators browsing normally see the page as
        # other signed-in users do.
        context["show_visibility_notice"] = context[
            "visibility"
        ] != PricingPageVisibility.EVERYONE and is_admin_mode_active(self.request)
        context["purchase_links"] = plan_purchase_links(self.request)
        # Same page chrome as core's system pages.
        context["staff_session"] = is_admin_mode_active(self.request)
        context["nav_items"] = (
            get_global_navigation(self.request)
            if self.request.user.is_authenticated
            else []
        )
        context["show_link_in_header_for_all_pages"] = Page.objects.filter(
            link_in_system=True, link_in_header=True
        )
        context["show_link_in_footer_for_all_pages"] = Page.objects.filter(
            link_in_system=True, link_in_footer=True
        )
        return context


class PricingPageView(View):
    """Serve /pricing/ to the audience chosen in Business settings.

    Visitors outside that audience get a 404, as does everyone while the plugin is
    globally disabled.
    """

    def dispatch(self, request, *args, **kwargs) -> HttpResponse:
        plugin_enabled = __package__ not in GlobalPluginConfig.get_disabled_modules()
        if plugin_enabled and can_view_pricing_page(request):
            return BusinessPricingView.as_view()(request, *args, **kwargs)
        raise Http404()
