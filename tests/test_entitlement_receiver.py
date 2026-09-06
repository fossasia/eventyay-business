import pytest
from django.utils.timezone import now
from unittest.mock import MagicMock

from eventyay.base.models import Organizer
from eventyay.base.entitlements import EntitlementDecision, check_entitlement
from eventyay_business.models import Subscription, Tier, TierVersion, TierEntitlement
from eventyay_business.capabilities import Capability, CapabilityValueType, register_capability

@pytest.fixture
def dummy_organizer():
    return Organizer.objects.create(name="Test Organizer", slug="test-org")

@pytest.fixture
def business_setup(dummy_organizer):
    tier = Tier.objects.create(slug="pro", name="Pro", is_public=True)
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    sub = Subscription.objects.create(
        organizer=dummy_organizer,
        tier_version=version,
        status='active',
        starts_at=now()
    )
    
    # Register a test capability
    cap = Capability(
        name="test.bool",
        label="Test Boolean",
        value_type=CapabilityValueType.BOOLEAN,
        default_value=False
    )
    register_capability(cap, override=True)
    
    cap_int = Capability(
        name="test.int",
        label="Test Integer",
        value_type=CapabilityValueType.INTEGER,
        default_value=10
    )
    register_capability(cap_int, override=True)
    
    return tier, version, sub

@pytest.mark.django_db
def test_receiver_boolean_default_deny(dummy_organizer, business_setup):
    """If no override exists, it should fall back to the False default and deny."""
    decision = check_entitlement(dummy_organizer, capability="test.bool")
    assert decision.allowed is False
    assert decision.reason_code == "tier_restriction"

@pytest.mark.django_db
def test_receiver_boolean_explicit_allow(dummy_organizer, business_setup):
    """If an explicit override exists, it should use it."""
    tier, version, sub = business_setup
    TierEntitlement.objects.create(tier_version=version, capability="test.bool", value="true")
    
    decision = check_entitlement(dummy_organizer, capability="test.bool")
    assert decision.allowed is True

@pytest.mark.django_db
def test_receiver_integer_default_allow(dummy_organizer, business_setup):
    """If quantity <= default_value, it should allow."""
    decision = check_entitlement(dummy_organizer, capability="test.int", quantity=5)
    assert decision.allowed is True
    assert decision.limit == 10

@pytest.mark.django_db
def test_receiver_integer_default_deny(dummy_organizer, business_setup):
    """If quantity > default_value, it should deny."""
    decision = check_entitlement(dummy_organizer, capability="test.int", quantity=15)
    assert decision.allowed is False
    assert decision.reason_code == "tier_limit_exceeded"
    assert decision.limit == 10

@pytest.mark.django_db
def test_receiver_integer_explicit_override(dummy_organizer, business_setup):
    """Explicit override changes the limit."""
    tier, version, sub = business_setup
    TierEntitlement.objects.create(tier_version=version, capability="test.int", value="20")
    
    decision = check_entitlement(dummy_organizer, capability="test.int", quantity=15)
    assert decision.allowed is True
    assert decision.limit == 20
