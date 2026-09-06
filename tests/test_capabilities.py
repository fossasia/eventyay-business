import pytest

from eventyay_business.capabilities import (
    BUILTIN_CAPABILITIES,
    Capability,
    CapabilityAlreadyRegistered,
    CapabilityRegistry,
    CapabilityType,
    UnknownCapability,
    get_capabilities,
    get_capability,
    is_registered,
    register_builtin_capabilities,
    register_capability,
    registry,
)

REQUIRED_CAPABILITIES = {
    "video.youtube": CapabilityType.BOOLEAN,
    "video.jitsi": CapabilityType.BOOLEAN,
    "video.jitsi.concurrent_rooms": CapabilityType.INTEGER,
    "video.loungemesh": CapabilityType.BOOLEAN,
    "email.bulk.monthly": CapabilityType.INTEGER,
    "organizer.full_admins": CapabilityType.INTEGER,
    "api.read": CapabilityType.BOOLEAN,
    "api.write": CapabilityType.BOOLEAN,
    "api.webhooks": CapabilityType.BOOLEAN,
    "commerce.platform_fee_percent": CapabilityType.DECIMAL,
    "registration.free_allowance_per_event": CapabilityType.INTEGER,
    "registration.free_overage_price": CapabilityType.MONEY,
    "support.priority": CapabilityType.BOOLEAN,
}


def test_register_and_get_capability():
    capabilities = CapabilityRegistry()
    registered = capabilities.register("video.custom", CapabilityType.BOOLEAN)

    assert registered == Capability("video.custom", CapabilityType.BOOLEAN)
    assert capabilities.get("video.custom") is registered
    assert capabilities.is_registered("video.custom")
    assert not capabilities.is_registered("video.missing")


def test_register_is_idempotent_for_the_same_definition():
    capabilities = CapabilityRegistry()
    first = capabilities.register("api.write", CapabilityType.BOOLEAN)
    second = capabilities.register("api.write", CapabilityType.BOOLEAN)

    assert first is second


def test_conflicting_registration_is_rejected():
    capabilities = CapabilityRegistry()
    capabilities.register("email.bulk.monthly", CapabilityType.INTEGER, unit="emails")

    with pytest.raises(CapabilityAlreadyRegistered, match="email.bulk.monthly"):
        capabilities.register("email.bulk.monthly", CapabilityType.BOOLEAN)


def test_unknown_capability_raises():
    capabilities = CapabilityRegistry()

    with pytest.raises(UnknownCapability, match="missing.capability"):
        capabilities.get("missing.capability")


def test_empty_capability_name_is_rejected():
    capabilities = CapabilityRegistry()

    with pytest.raises(ValueError, match="non-empty"):
        capabilities.register("", CapabilityType.BOOLEAN)
    with pytest.raises(ValueError, match="non-empty"):
        capabilities.register("  padded  ", CapabilityType.BOOLEAN)


def test_plugin_hook_registers_on_an_isolated_registry():
    capabilities = CapabilityRegistry()

    register_capability(
        "plugins.exhibition",
        CapabilityType.BOOLEAN,
        target=capabilities,
    )

    assert capabilities.is_registered("plugins.exhibition")
    assert not is_registered("plugins.exhibition", target=CapabilityRegistry())


def test_builtin_catalogue_includes_required_capabilities():
    capabilities = CapabilityRegistry()
    register_builtin_capabilities(capabilities)

    for name, value_type in REQUIRED_CAPABILITIES.items():
        capability = capabilities.get(name)
        assert capability.value_type is value_type

    assert {item[0] for item in BUILTIN_CAPABILITIES} >= set(REQUIRED_CAPABILITIES)


def test_builtin_registration_is_idempotent():
    capabilities = CapabilityRegistry()
    register_builtin_capabilities(capabilities)
    register_builtin_capabilities(capabilities)

    assert len(capabilities.all()) == len(BUILTIN_CAPABILITIES)


def test_module_registry_exposes_helpers():
    register_builtin_capabilities()

    assert is_registered("api.write")
    assert get_capability("api.write").value_type is CapabilityType.BOOLEAN
    names = {capability.name for capability in get_capabilities()}
    assert names >= set(REQUIRED_CAPABILITIES)
    assert registry.is_registered("support.priority")
