import pytest

from eventyay_business.capabilities import (
    STANDARD_CAPABILITIES,
    Capability,
    CapabilityRegistry,
    CapabilityValueType,
    get_all_capabilities,
    get_capability,
    get_capability_choices,
    register_capability,
)
from eventyay_business.forms import TierEntitlementForm
from eventyay_business.models import TierEntitlement


def test_standard_capabilities_catalogue():
    """Verify all 13 standard capabilities required by Issue #6 are present in default_registry."""
    expected_capabilities = {
        "video.youtube": CapabilityValueType.BOOLEAN,
        "video.jitsi": CapabilityValueType.BOOLEAN,
        "video.jitsi.concurrent_rooms": CapabilityValueType.INTEGER,
        "video.loungemesh": CapabilityValueType.BOOLEAN,
        "email.bulk.monthly": CapabilityValueType.INTEGER,
        "organizer.full_admins": CapabilityValueType.INTEGER,
        "api.read": CapabilityValueType.BOOLEAN,
        "api.write": CapabilityValueType.BOOLEAN,
        "api.webhooks": CapabilityValueType.BOOLEAN,
        "commerce.platform_fee_percent": CapabilityValueType.DECIMAL,
        "registration.free_allowance_per_event": CapabilityValueType.INTEGER,
        "registration.free_overage_price": CapabilityValueType.MONEY,
        "support.priority": CapabilityValueType.BOOLEAN,
    }

    assert len(STANDARD_CAPABILITIES) == 13
    for name, expected_type in expected_capabilities.items():
        cap = get_capability(name)
        assert cap is not None, f"Capability {name} should be in default_registry"
        assert cap.value_type == expected_type


def test_capability_to_dict():
    cap = Capability(
        name="video.test",
        label="Test Video",
        description="A test capability",
        value_type=CapabilityValueType.INTEGER,
        category="Testing",
        unit="streams",
        default_value=5,
        metadata={"internal": True},
    )
    serialized = cap.to_dict()
    assert serialized == {
        "name": "video.test",
        "label": "Test Video",
        "description": "A test capability",
        "value_type": "integer",
        "category": "Testing",
        "unit": "streams",
        "default_value": 5,
        "metadata": {"internal": True},
    }


def test_custom_registry_operations():
    registry = CapabilityRegistry()
    cap1 = Capability(
        name="feature.alpha",
        label="Alpha",
        category="Features",
        value_type=CapabilityValueType.BOOLEAN,
    )
    cap2 = Capability(
        name="feature.beta",
        label="Beta",
        category="Features",
        value_type=CapabilityValueType.INTEGER,
    )

    registry.register(cap1)
    registry.register(cap2)

    assert registry.get("feature.alpha") == cap1
    assert registry.get("feature.beta") == cap2
    assert len(registry.all()) == 2

    # Category grouping
    grouped = registry.by_category()
    assert "Features" in grouped
    assert len(grouped["Features"]) == 2

    # Duplicate registration raises ValueError unless override=True
    with pytest.raises(ValueError):
        registry.register(cap1)

    cap1_updated = Capability(name="feature.alpha", label="Alpha Updated")
    registry.register(cap1_updated, override=True)
    assert registry.get("feature.alpha").label == "Alpha Updated"

    # Unregister
    registry.unregister("feature.beta")
    assert registry.get("feature.beta") is None

    # Choices
    choices = registry.choices()
    assert choices == [("feature.alpha", "feature.alpha (Alpha Updated)")]


def test_global_register_capability_hook():
    custom_cap = Capability(
        name="custom.hubspot_sync",
        label="HubSpot Sync",
        category="Integrations",
        value_type=CapabilityValueType.BOOLEAN,
    )
    register_capability(custom_cap, override=True)

    retrieved = get_capability("custom.hubspot_sync")
    assert retrieved is not None
    assert retrieved.name == "custom.hubspot_sync"

    all_caps = get_all_capabilities()
    assert custom_cap in all_caps

    choices = dict(get_capability_choices())
    assert "custom.hubspot_sync" in choices


def test_register_entitlements_signal_receiver():
    from eventyay_business.signals import register_capabilities_receiver

    result = register_capabilities_receiver(sender=None)
    assert isinstance(result, dict)
    assert "video.youtube" in result
    assert "email.bulk.monthly" in result
    assert result["video.youtube"]["value_type"] == "boolean"


def test_tier_entitlement_form_choices():
    form = TierEntitlementForm()
    choices = dict(form.fields["capability"].widget.choices)
    assert "video.youtube" in choices
    assert "organizer.full_admins" in choices

    # Test preserving non-standard capability on existing instance
    existing = TierEntitlement(capability="legacy.unregistered_feature")
    edit_form = TierEntitlementForm(instance=existing)
    edit_choices = dict(edit_form.fields["capability"].widget.choices)
    assert "legacy.unregistered_feature" in edit_choices
