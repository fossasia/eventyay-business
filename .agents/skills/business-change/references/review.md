# Business review prompts

Select the relevant cases rather than rerunning every billing scenario:

- Tier/add-on changes: historical version references, effective dates, typed
  capabilities, organizer/event scope, cache invalidation, pending transitions.
- Usage/fees: stable source IDs, duplicate delivery, currency/country fallback,
  paid date, cutover, historical checkout collection, refunds/cancellations.
- Invoices: source claiming, immutable issued snapshots, currency separation,
  proration, retries, existing PaymentIntents, matching collection authorization.
- Cutover: accepted reconciliation evidence, late old webhooks, legacy collector
  disabled, core billing identity reused.

Follow `.agents/context/flows.md` for owning files and test modules. Changes to
core signal contracts need corresponding core tests and a separate PR.
