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


