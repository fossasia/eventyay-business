import pytest
from datetime import datetime
from decimal import Decimal
from django.utils.timezone import make_aware, now
from eventyay.base.models import Organizer

from eventyay_business.billing_overview import build_organizer_billing_overview
from eventyay_business.capabilities import CapabilityValueType, get_capability
from eventyay_business.forms import AddonDefinitionForm, TierEntitlementForm
from eventyay_business.models import (
    AddonAssignmentScope,
    AddonDefinition,
    AddonPricingMode,
    Subscription,
    Tier,
    TierEntitlement,
    TierVersion,
    UsageRecord,
)

pytestmark = pytest.mark.django_db


def choice_keys(field):
    return {
        key
        for _category, items in field.widget.choices
        if isinstance(items, (list, tuple))
        for key, _label in items
    }


def tier_form(**data):
    return TierEntitlementForm(data=data)


def addon_data(**overrides):
    data = {
        "name": "Extra",
        "slug": "extra",
        "assignment_scope": AddonAssignmentScope.ORGANIZER,
        "pricing_mode": AddonPricingMode.RECURRING,
        "currency": "EUR",
        "price": "10.00",
        "capability": "email.bulk.monthly",
        "entitlement_value": "500",
        "quantity": "1",
        "active": "on",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "capability,value",
    [
        ("email.bulk.monthly", "100"),
        ("commerce.platform_fee_percent", "2.5"),
        ("registration.free_overage_price", "0.20"),
    ],
)
def test_tier_form_ignores_generic_overage_for_standard_capabilities(capability, value):
    form = tier_form(
        capability=capability,
        value=value,
        overage_allowed="on",
        overage_price="1.00",
        currency="EUR",
        overage_block_size="10",
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["overage_allowed"] is False
    assert form.cleaned_data["overage_price"] is None
    assert form.cleaned_data["overage_block_size"] is None


def test_tier_form_keeps_overage_for_custom_capabilities():
    existing = TierEntitlement(capability="legacy.custom_feature")
    form = TierEntitlementForm(
        instance=existing,
        data={
            "capability": "legacy.custom_feature",
            "value": "5",
            "overage_allowed": "on",
            "overage_price": "1.00",
            "currency": "EUR",
            "overage_block_size": "10",
        },
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["overage_allowed"] is True
    assert form.cleaned_data["overage_price"] == Decimal("1.00")
    assert form.cleaned_data["overage_block_size"] == 10


def test_hidden_overage_values_do_not_block_standard_capabilities():
    form = tier_form(
        capability="email.bulk.monthly",
        value="100",
        overage_allowed="on",
        overage_price="1.00",
        currency="EU",
        overage_block_size="10",
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["currency"] == ""


@pytest.mark.parametrize(
    "capability,value",
    [
        ("email.bulk.monthly", "-1"),
        ("video.jitsi.concurrent_rooms", "-3"),
        ("commerce.platform_fee_percent", "-0.5"),
        ("registration.free_overage_price", "-0.20"),
    ],
)
def test_tier_form_rejects_negative_values(capability, value):
    form = tier_form(capability=capability, value=value)

    assert not form.is_valid()
    assert "Value cannot be negative." in form.errors["value"]


def test_tier_form_allows_zero():
    assert tier_form(capability="email.bulk.monthly", value="0").is_valid()


def test_addon_form_offers_only_features_and_limits():
    keys = choice_keys(AddonDefinitionForm().fields["capability"])

    assert "video.youtube" in keys
    assert "email.bulk.monthly" in keys
    assert "commerce.platform_fee_percent" not in keys
    assert "registration.free_overage_price" not in keys
    for key in keys:
        assert get_capability(key).value_type in (
            CapabilityValueType.BOOLEAN,
            CapabilityValueType.INTEGER,
        )


@pytest.mark.parametrize(
    "capability", ["commerce.platform_fee_percent", "registration.free_overage_price"]
)
def test_addon_form_rejects_capabilities_add_ons_cannot_change(capability):
    form = AddonDefinitionForm(
        data=addon_data(capability=capability, entitlement_value="1")
    )

    assert not form.is_valid()
    assert "capability" in form.errors


def test_existing_addon_with_an_unsupported_capability_can_still_be_saved():
    addon = AddonDefinition.objects.create(
        slug="extra",
        name="Extra",
        price=Decimal("10.00"),
        currency="EUR",
        capability="commerce.platform_fee_percent",
        entitlement_value="1",
    )
    form = AddonDefinitionForm(
        instance=addon,
        data=addon_data(
            capability="commerce.platform_fee_percent",
            entitlement_value="1",
            name="Renamed",
        ),
    )

    assert "commerce.platform_fee_percent" in choice_keys(form.fields["capability"])
    assert form.is_valid(), form.errors


def test_yes_no_addon_always_uses_one_unit():
    form = AddonDefinitionForm(
        data=addon_data(
            capability="video.loungemesh", entitlement_value="true", quantity="5"
        )
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["quantity"] == 1


def test_limit_addon_keeps_its_units():
    form = AddonDefinitionForm(data=addon_data(quantity="3"))

    assert form.is_valid(), form.errors
    assert form.cleaned_data["quantity"] == 3


def test_addon_form_rejects_negative_allowance():
    form = AddonDefinitionForm(data=addon_data(entitlement_value="-100"))

    assert not form.is_valid()
    assert "Value cannot be negative." in form.errors["entitlement_value"]


def test_current_charges_do_not_estimate_unbilled_overage():
    organizer = Organizer.objects.create(name="Org", slug="org")
    tier = Tier.objects.create(slug="pro", name="Pro")
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    TierEntitlement.objects.create(
        tier_version=version,
        capability="email.bulk.monthly",
        value="100",
        overage_allowed=True,
        overage_price=Decimal("0.05"),
        currency="EUR",
    )
    subscription = Subscription.objects.get(organizer=organizer)
    subscription.tier_version = version
    subscription.currency = "EUR"
    subscription.starts_at = make_aware(datetime(2026, 1, 1))
    subscription.save()
    UsageRecord.objects.create(
        organizer=organizer,
        capability="email.bulk.monthly",
        quantity=Decimal("150"),
        unit="emails",
        source_type="bulk_email",
        source_id="1",
        idempotency_key="mail-1",
        occurred_at=make_aware(datetime(2026, 8, 10, 12, 0)),
    )

    overview = build_organizer_billing_overview(
        organizer, reference=make_aware(datetime(2026, 8, 20, 12, 0))
    )
    (item,) = [
        row for row in overview["usage"] if row["capability"] == "email.bulk.monthly"
    ]

    assert item["quantity"] == Decimal("150")
    assert item["overage"] is None
    assert item["amount"] == Decimal("0.00")
