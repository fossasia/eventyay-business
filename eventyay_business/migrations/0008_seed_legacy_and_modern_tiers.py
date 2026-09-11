from django.db import migrations
from django.utils.timezone import now


def create_tiers_and_migrate_to_legacy(apps, schema_editor):
    Tier = apps.get_model("eventyay_business", "Tier")
    TierVersion = apps.get_model("eventyay_business", "TierVersion")
    TierEntitlement = apps.get_model("eventyay_business", "TierEntitlement")
    Subscription = apps.get_model("eventyay_business", "Subscription")
    Organizer = apps.get_model("base", "Organizer")

    tiers_to_ensure = [
        {"slug": "free", "name": "Free", "is_public": True},
        {"slug": "plus", "name": "Plus", "is_public": True},
        {"slug": "enterprise", "name": "Enterprise", "is_public": True},
        {"slug": "legacy", "name": "Legacy", "is_public": False},
    ]

    standard_defaults = [
        ("video.youtube", "true", "", False),
        ("video.jitsi", "true", "", False),
        ("video.jitsi.concurrent_rooms", "1", "rooms", False),
        ("video.loungemesh", "false", "", False),
        ("email.bulk.monthly", "1000", "emails", False),
        ("organizer.full_admins", "2", "admins", False),
        ("api.read", "true", "", False),
        ("api.write", "false", "", False),
        ("api.webhooks", "false", "", False),
        ("commerce.platform_fee_percent", "0.0", "%", False),
        ("registration.free_allowance_per_event", "100", "registrations", False),
        ("registration.free_overage_price", "0.0", "per registration", False),
        ("support.priority", "false", "", False),
    ]

    tier_objects = {}
    current_time = now()

    for tier_data in tiers_to_ensure:
        tier, created = Tier.objects.get_or_create(
            slug=tier_data["slug"],
            defaults={
                "name": tier_data["name"],
                "status": "published",
                "is_public": tier_data["is_public"],
            },
        )
        latest_version = (
            tier.versions.filter(published_at__isnull=False)
            .order_by("-version")
            .first()
        )
        if not latest_version:
            latest_version = TierVersion.objects.create(
                tier=tier, version=1, published_at=current_time
            )
        tier_objects[tier_data["slug"]] = latest_version

        # Seed standard entitlements for this version
        existing_caps = set(
            TierEntitlement.objects.filter(tier_version=latest_version).values_list(
                "capability", flat=True
            )
        )
        to_create = []
        for cap_name, val, unit, overage in standard_defaults:
            if cap_name not in existing_caps:
                to_create.append(
                    TierEntitlement(
                        tier_version=latest_version,
                        capability=cap_name,
                        value=val,
                        unit=unit,
                        overage_allowed=overage,
                    )
                )
        if to_create:
            TierEntitlement.objects.bulk_create(to_create)

    legacy_version = tier_objects["legacy"]

    # Assign all existing active/pending subscriptions to the Legacy tier
    existing_subs = Subscription.objects.filter(status__in=["active", "pending"])
    existing_subs.update(tier_version=legacy_version)

    # For any organizer that somehow missed getting a subscription
    subscribed_org_ids = Subscription.objects.filter(
        status__in=["active", "pending"]
    ).values_list("organizer_id", flat=True)
    organizers_without_subs = Organizer.objects.exclude(pk__in=subscribed_org_ids)

    subs_to_create = []
    for org in organizers_without_subs:
        subs_to_create.append(
            Subscription(
                organizer=org,
                tier_version=legacy_version,
                status="active",
                starts_at=current_time,
            )
        )

    if subs_to_create:
        Subscription.objects.bulk_create(subs_to_create)


class Migration(migrations.Migration):

    dependencies = [
        ("eventyay_business", "0007_seed_free_tier_entitlements"),
        ("base", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_tiers_and_migrate_to_legacy, migrations.RunPython.noop
        ),
    ]
