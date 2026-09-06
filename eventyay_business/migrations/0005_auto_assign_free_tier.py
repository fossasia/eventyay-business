from django.db import migrations
from django.utils.timezone import now

def auto_assign_free_tier(apps, schema_editor):
    Organizer = apps.get_model('base', 'Organizer')
    Tier = apps.get_model('eventyay_business', 'Tier')
    TierVersion = apps.get_model('eventyay_business', 'TierVersion')
    Subscription = apps.get_model('eventyay_business', 'Subscription')

    # Fetch or create free tier
    free_tier, _ = Tier.objects.get_or_create(
        slug="free",
        defaults={
            "name": "Free",
            "description": "Default free tier",
            "is_public": True,
        }
    )

    latest_version = free_tier.versions.filter(published_at__isnull=False).order_by("-version").first()
    if not latest_version:
        latest_version, _ = TierVersion.objects.get_or_create(
            tier=free_tier,
            version=1,
            defaults={"published_at": now()}
        )
        if latest_version.published_at is None:
            latest_version.published_at = now()
            latest_version.save(update_fields=["published_at"])

    # Cross-app relations might not be available on historical models, so query IDs
    subscribed_org_ids = Subscription.objects.filter(status__in=['active', 'pending']).values_list('organizer_id', flat=True)
    organizers_without_subs = Organizer.objects.exclude(pk__in=subscribed_org_ids)
    
    subscriptions_to_create = []
    for org in organizers_without_subs:
        subscriptions_to_create.append(
            Subscription(
                organizer=org,
                tier_version=latest_version,
                status='active',
                starts_at=now(),
            )
        )
    
    if subscriptions_to_create:
        Subscription.objects.bulk_create(subscriptions_to_create)

class Migration(migrations.Migration):
    dependencies = [
        ('eventyay_business', '0004_subscription'),
        ('base', '0001_initial'),  # Ensure base app is loaded
    ]

    operations = [
        migrations.RunPython(auto_assign_free_tier, reverse_code=migrations.RunPython.noop),
    ]
