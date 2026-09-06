import pytest
from django.utils.timezone import now
from eventyay.base.models import Organizer
from eventyay_business.models import Subscription, Tier, TierVersion

@pytest.mark.django_db
def test_organizer_auto_assign_free_tier():
    # Creating an organizer should trigger the post_save signal
    org = Organizer.objects.create(name="Test Organizer", slug="test-org")
    
    # Check if a subscription was created
    sub = Subscription.objects.filter(organizer=org).first()
    assert sub is not None
    assert sub.status == "active"
    assert sub.tier_version.tier.slug == "free"

@pytest.mark.django_db
def test_management_command_assign_free_tier():
    from django.core.management import call_command
    
    # Create organizer bypassing signals if possible, or just delete its sub
    org = Organizer.objects.create(name="Legacy Organizer", slug="legacy-org")
    Subscription.objects.filter(organizer=org).delete()
    
    assert Subscription.objects.count() == 0
    
    # Run command
    call_command("assign_free_tiers")
    
    # Verify
    assert Subscription.objects.count() == 1
    sub = Subscription.objects.first()
    assert sub.organizer == org
    assert sub.tier_version.tier.slug == "free"
