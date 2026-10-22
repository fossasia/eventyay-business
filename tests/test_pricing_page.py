import pytest
from decimal import Decimal
from django.test import override_settings
from django.urls import reverse
from django.utils.timezone import now
from eventyay.base.models import Organizer, Team, User
from eventyay.base.models.auth import StaffSession
from eventyay.base.models.global_plugin_config import GlobalPluginConfig
from eventyay.base.settings import GlobalSettingsObject

from eventyay_business.forms import GlobalBusinessSettingsForm
from eventyay_business.models import (
    AddonAssignmentScope,
    AddonDefinition,
    AddonPricingMode,
    BillingInterval,
    Tier,
    TierPrice,
    TierStatus,
    TierVersion,
)
from eventyay_business.pricing import (
    PRICING_PAGE_VISIBILITY_SETTING,
    PricingPageVisibility,
    pricing_page_settings,
    pricing_page_visibility,
)

PRICING_TEMPLATE = "eventyay_business/pricing/pricing.html"


@pytest.fixture
def pricing_url():
    return reverse("plugins:eventyay_business:pricing")


@pytest.fixture
def published_tiers(db):
    plus = Tier.objects.create(
        slug="plus", name="Plus Plan", status=TierStatus.PUBLISHED, is_public=True
    )
    version = TierVersion.objects.create(tier=plus, version=1, published_at=now())
    TierPrice.objects.create(
        tier_version=version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("69.00"),
        currency="EUR",
    )
    hidden = Tier.objects.create(
        slug="internal", name="Internal Plan", status=TierStatus.PUBLISHED
    )
    TierVersion.objects.create(tier=hidden, version=1, published_at=now())
    draft = Tier.objects.create(
        slug="draft", name="Draft Plan", status=TierStatus.DRAFT, is_public=True
    )
    TierVersion.objects.create(tier=draft, version=1)
    AddonDefinition.objects.create(
        name="Exhibition Module",
        slug="exhibition-module",
        capability="plugin.exhibition",
        entitlement_value="true",
        assignment_scope=AddonAssignmentScope.EVENT,
        price=Decimal("99.00"),
        currency="EUR",
        pricing_mode=AddonPricingMode.ONE_TIME,
        active=True,
        public=True,
    )


@pytest.fixture
def admin_user_client(client, db):
    admin = User.objects.create_adminuser(
        email="pricing-admin@example.com", password="x"
    )
    client.force_login(admin)
    return client


def set_visibility(value: str):
    GlobalSettingsObject().settings.set(PRICING_PAGE_VISIBILITY_SETTING, value)


@pytest.fixture
def user_client(client, db):
    user = User.objects.create_user("organiser@example.com", "secret")
    client.force_login(user)
    client.user = user
    return client


def add_organizer(user, name, slug):
    organizer = Organizer.objects.create(name=name, slug=slug)
    team = Team.objects.create(organizer=organizer, can_change_organizer_settings=True)
    team.members.add(user)
    return organizer


def used_templates(response) -> set[str]:
    return {template.name for template in response.templates}


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_public_pricing_page_lists_published_public_tiers_and_addons(
    client, published_tiers, pricing_url
):
    set_visibility(PricingPageVisibility.EVERYONE)

    response = client.get(pricing_url)

    assert response.status_code == 200
    assert PRICING_TEMPLATE in used_templates(response)
    content = response.content.decode()
    assert "Plus Plan" in content
    assert "69.00 EUR" in content
    assert "Exhibition Module" in content
    assert "Internal Plan" not in content
    assert "Draft Plan" not in content
    assert "Only platform administrators can see this page" not in content
    assert "Get started" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_hidden_pricing_page_is_not_found_for_visitors(
    client, published_tiers, pricing_url
):
    response = client.get(pricing_url)

    assert response.status_code == 404
    assert b"Plus Plan" not in response.content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_admins_always_see_pricing_page(
    admin_user_client, published_tiers, pricing_url
):
    response = admin_user_client.get(pricing_url)

    assert response.status_code == 200
    assert PRICING_TEMPLATE in used_templates(response)
    assert b"Plus Plan" in response.content
    # Outside admin mode, administrators see the page as other users do.
    assert "can see this page" not in response.content.decode()

    session = admin_user_client.session
    session.save()
    StaffSession.objects.create(
        user=User.objects.get(email="pricing-admin@example.com"),
        session_key=session.session_key,
    )
    content = admin_user_client.get(pricing_url).content.decode()
    assert "Only platform administrators can see this page" in content

    set_visibility(PricingPageVisibility.SIGNED_IN)
    content = admin_user_client.get(pricing_url).content.decode()
    assert "Only signed-in users can see this page" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_globally_disabled_plugin_hides_pricing_page(
    admin_user_client, published_tiers, pricing_url
):
    set_visibility(PricingPageVisibility.EVERYONE)
    GlobalPluginConfig.objects.create(module="eventyay_business", is_active=False)

    response = admin_user_client.get(pricing_url)

    assert response.status_code == 404


@pytest.mark.django_db
def test_settings_form_saves_pricing_page_options():
    form = GlobalBusinessSettingsForm(
        data={
            PRICING_PAGE_VISIBILITY_SETTING: PricingPageVisibility.SIGNED_IN,
            "business_pricing_title": "Plans for every event",
            "business_pricing_intro": "Simple, transparent pricing.",
            "business_pricing_show_tiers": "on",
            "business_pricing_show_signup_button": "on",
            "business_pricing_signup_label": "Start free",
            "business_pricing_signup_url": "/common/",
            "business_pricing_footer_note": "Prices exclude VAT.",
        }
    )
    assert form.is_valid(), form.errors

    form.save()

    page = pricing_page_settings()
    assert pricing_page_visibility() == PricingPageVisibility.SIGNED_IN
    assert page["title"] == "Plans for every event"
    assert page["show_tiers"] is True
    assert page["show_addons"] is False
    assert page["show_features"] is False
    assert page["signup_url"] == "/common/"
    assert page["footer_note"] == "Prices exclude VAT."


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "//evil.example", "example.com"]
)
def test_settings_form_rejects_unsafe_signup_links(url):
    form = GlobalBusinessSettingsForm(data={"business_pricing_signup_url": url})

    assert not form.is_valid()
    assert "business_pricing_signup_url" in form.errors


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_pricing_page_uses_admin_texts_and_switches(
    client, published_tiers, pricing_url
):
    set_visibility(PricingPageVisibility.EVERYONE)
    gs = GlobalSettingsObject().settings
    gs.set("business_pricing_title", "Plans for every event")
    gs.set("business_pricing_intro", "Simple, transparent pricing.")
    gs.set("business_pricing_signup_label", "Start free")
    gs.set("business_pricing_signup_url", "/common/")
    gs.set("business_pricing_footer_note", "Prices exclude VAT.")
    gs.set("business_pricing_show_addons", False)

    content = client.get(pricing_url).content.decode()

    assert "Plans for every event" in content
    assert "Simple, transparent pricing." in content
    assert "Start free" in content
    assert 'href="/common/"' in content
    assert "Prices exclude VAT." in content
    assert "Plus Plan" in content
    assert "Exhibition Module" not in content

    gs.set("business_pricing_show_tiers", False)
    gs.set("business_pricing_show_addons", True)
    content = client.get(pricing_url).content.decode()

    assert "Plus Plan" not in content
    assert "Exhibition Module" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_signed_in_visibility_shows_page_to_signed_in_users_only(
    client, user_client, published_tiers, pricing_url
):
    set_visibility(PricingPageVisibility.SIGNED_IN)

    assert user_client.get(pricing_url).status_code == 200
    user_client.logout()
    assert client.get(pricing_url).status_code == 404


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_signed_in_organiser_goes_to_plan_purchase(
    user_client, published_tiers, pricing_url
):
    set_visibility(PricingPageVisibility.SIGNED_IN)
    acme = add_organizer(user_client.user, "Acme", "acme")
    acme_upgrade = reverse(
        "plugins:eventyay_business:organizer.plan.upgrade",
        kwargs={"organizer": acme.slug},
    )

    content = user_client.get(pricing_url).content.decode()

    assert f'href="{acme_upgrade}"' in content
    assert "Choose this plan" in content
    assert "Get started" not in content

    globex = add_organizer(user_client.user, "Globex", "globex")
    content = user_client.get(pricing_url).content.decode()

    assert "pricing-organizer-choice" in content
    assert f'href="{acme_upgrade}"' in content
    assert (
        reverse(
            "plugins:eventyay_business:organizer.plan.upgrade",
            kwargs={"organizer": globex.slug},
        )
        in content
    )
