# Business architecture and repository map

Business is a Django plugin installed into [Eventyay core](https://github.com/fossasia/eventyay).
It is not a standalone server. [Eventyay Stripe](https://github.com/fossasia/eventyay-stripe)
is a separate payment plugin; a common local layout places both under core's
`plugins/` directory. All paths below are local to Business unless marked core.

## Source map

| Area | Files in `eventyay_business/` |
|---|---|
| Registration | `apps.py`, `__init__.py`, `signals.py`, `urls.py` |
| Data and schema | `models.py`, `migrations/` |
| Capability definitions and evaluation | `capabilities.py`, `signals.py`, `services.py` |
| Admin/organizer/event UI | `views.py`, `forms.py`, `pricing.py`, `templates/`, `static/` |
| Fee policy and history | `services.py`, `platform_fee_ledger.py` |
| Invoice calculation and presentation | `invoicing_service.py`, `invoice_presentation.py`, `invoice_document.py`, `billing_overview.py` |
| Payment collection and old Stripe Checkout flows | `invoice_collection.py`, `stripe_service.py`, `views_stripe.py` |
| Cutover and audit | `billing_migration.py`, `reconciliation.py` |
| Scheduled work and communication | `tasks.py`, `notifications.py` |
| Discounts and reservations | `vouchers.py` |

`pyproject.toml` registers `eventyay_business:PluginApp` under `pretix.plugin`.
Core discovers the module from installed package metadata. `PluginApp.ready()`
loads signals and tasks; its metadata is hidden/restricted, so installation is
not equivalent to a user-selectable event plugin checkbox.

## Domain model

- `Tier`, `TierVersion`, `TierPrice`, and `TierEntitlement` describe versioned
  commercial offers. `Subscription` selects a version for an organizer, including
  lifecycle dates and pending changes. Preserve historical version references.
- `AddonDefinition`, `OrganizerAddon`, and `EventAddon` describe optional features
  and assignments with different ownership scopes. Capabilities have typed values;
  `capabilities.py` is the registry, not a collection of arbitrary setting names.
- `UsageRecord` stores attributed usage; `PlatformFeeLedgerEntry` preserves fee
  history and adjustments. Duplicate signals must not create duplicate charges.
- `BusinessInvoice` and `BusinessInvoiceLine` preserve calculated charges and
  presentation snapshots. `BillingCollectionAuthorization` represents permission
  to collect from the organizer's customer; a saved card alone is insufficient.
- `BillingReconciliationRun`/`Result` and `BillingNotificationDelivery` track
  rollout evidence and communication. `VoucherBusinessTargets` and
  `VoucherRedemption` extend core voucher behavior.

Read model constraints and corresponding migrations before changing lifecycle
or uniqueness rules. Avoid recalculating issued invoice history from current
catalog prices or settings.

## Contracts with core and Stripe

Core owns organizers/events/orders, `OrganizerBillingModel`, and the shared
signals in `app/eventyay/base/signals.py`. Business receives entitlement checks,
usage, fee policy requests, and order lifecycle notifications. Core's
`app/eventyay/base/entitlements.py` is the caller API for other features.

Business's `stripe_service.get_or_create_stripe_customer` reuses the canonical
core billing customer despite the historical function name. Billing customer
creation belongs to core. `invoice_collection.py` uses core Stripe helpers. Business owns the billing
settings UI/validation; the generic core settings store and helpers still supply
the stored credentials.
Business does not route its subscription/invoice webhooks through the attendee
payment handler in the Stripe plugin.

Core's Git dependency sources and `app/uv.lock` select the packaged Business
revision. Plugin CI instead installs this checkout over core `dev`. Check both
when local behavior and deployment behavior disagree.
