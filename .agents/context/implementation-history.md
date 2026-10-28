# Parent epic and implementation evidence

Reviewed on 2026-10-28 against upstream `dev` and the open
[Eventyay epic #5890](https://github.com/fossasia/eventyay/issues/5890), including
its comments and the descriptions/status of its 23 directly linked PRs.
All PRs in the table were merged when inspected. This is a historical record,
not a claim that any particular deployment runs them or has passed reconciliation.

The epic remains open for deployment configuration, accepted reconciliation on
deployment data, controlled organizer activation, and first-cycle monitoring.
Implementation completion is not permission to activate or charge organizers.

## Evidence index

| PR | Merged change |
|---|---|
| [eventyay #5985](https://github.com/fossasia/eventyay/pull/5985) | feat: Phase 1 (Core Cleanup) - Abolish commercial settings, fallback fees, and business UI from core |
| [eventyay #6049](https://github.com/fossasia/eventyay/pull/6049) | Move Stripe ticketing settings ownership to plugin |
| [eventyay #6415](https://github.com/fossasia/eventyay/pull/6415) | Add neutral platform fee policy signal |
| [eventyay #6499](https://github.com/fossasia/eventyay/pull/6499) | Allow external collectors to disable legacy billing |
| [eventyay-business #103](https://github.com/fossasia/eventyay-business/pull/103) | Accrue ticket fees for monthly invoicing |
| [eventyay-business #106](https://github.com/fossasia/eventyay-business/pull/106) | Finalize auditable Eventyay business invoices |
| [eventyay-business #107](https://github.com/fossasia/eventyay-business/pull/107) | Make consolidated invoice calculation reproducible |
| [eventyay-business #108](https://github.com/fossasia/eventyay-business/pull/108) | Collect consolidated invoices through Stripe PaymentIntents |
| [eventyay-business #109](https://github.com/fossasia/eventyay-business/pull/109) | Gate consolidated billing at organizer cutover |
| [eventyay-business #110](https://github.com/fossasia/eventyay-business/pull/110) | Add consolidated billing migration preflight |
| [eventyay-business #112](https://github.com/fossasia/eventyay-business/pull/112) | Complete guarded consolidated billing activation |
| [eventyay-business #114](https://github.com/fossasia/eventyay-business/pull/114) | Add configurable immutable invoice presentation |
| [eventyay-business #121](https://github.com/fossasia/eventyay-business/pull/121) | Add idempotent billing notification delivery and audit |
| [eventyay-business #123](https://github.com/fossasia/eventyay-business/pull/123) | Add organizer billing lifecycle and dunning emails |
| [eventyay-business #127](https://github.com/fossasia/eventyay-business/pull/127) | Add non-collecting billing reconciliation engine |
| [eventyay-business #128](https://github.com/fossasia/eventyay-business/pull/128) | Add billing reconciliation review and acceptance |
| [eventyay-business #130](https://github.com/fossasia/eventyay-business/pull/130) | Gate controlled billing rollout on accepted reconciliation |
| [eventyay-business #95](https://github.com/fossasia/eventyay-business/pull/95) | feat: Phase 1B - abolish global fallback fees and take ownership of billing settings |
| [eventyay-business #97](https://github.com/fossasia/eventyay-business/pull/97) | Phase 3: exclude Stripe-collected platform fees from monthly debt |
| [eventyay-business #99](https://github.com/fossasia/eventyay-business/pull/99) | Provide Business platform fee policy through Core |
| [eventyay-stripe #49](https://github.com/fossasia/eventyay-stripe/pull/49) | Phase 2B: collect smart platform fees in Stripe Connect |
| [eventyay-stripe #54](https://github.com/fossasia/eventyay-stripe/pull/54) | Refund Stripe Connect application fees |
| [eventyay-stripe #55](https://github.com/fossasia/eventyay-stripe/pull/55) | Stop collecting platform fees during ticket checkout |

## Read history in order

Early epic comments and Stripe #49 describe splitting platform fees during
checkout. Stripe #55 supersedes that design: attendee checkout no longer adds
commercial application fees. Stripe #54 remains relevant for historical refunds.
Core #6415's older PR description says Stripe consumes its resolver; current
Stripe source does not. The neutral fee-policy contract remains available to
Business and other consumers. Follow current code and the updated epic.

Business #103 establishes ledger accrual and cutover-aware historical offsets;
#106–#108 establish canonical Eventyay invoices, reproducible calculation, and
PaymentIntent collection. Core #6499 plus Business #109 separate collector
ownership. Business #110/#112 add preflight and guarded activation; #130 later
adds accepted-reconciliation requirements. Old #112 rollout instructions alone
are therefore insufficient for current activation.

Business #121/#123 provide durable notification delivery and lifecycle messages.
#127 computes reconciliation in rollback-only transactions; #128 accepts exact
results; #130 gates activation on current accepted evidence and monitors rollout.
See `billing_migration.py`, `reconciliation.py`, and their tests for the current
checks. Historical PR test counts are evidence from their authors, not tests run
for this documentation change.

## Scope and known gaps

The epic describes billing architecture, not proof that every catalogue feature
has an enforcement caller. Use [the capability audit](capabilities-and-entitlements.md)
for that distinction. In particular, generic overage metadata does not establish
a generic metered billing engine, and invoice line types for tax/manual adjustments
do not establish automatic tax calculation. Inspect actual invoice producers.

No live deployment inspection, organizer activation, invoice generation, or money
movement was performed to write this context. Refresh issue/PR status and inspect
source when planning future work; do not reuse old temporary stack branches from
merged PR descriptions as a required development setup.
