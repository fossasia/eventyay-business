---
name: business-change
description: Change or review Eventyay Business tiers, entitlements, fee ledgers, invoices, collection, and billing lifecycle with repository-specific setup and tests.
---

# Business repository changes

Read root [AGENTS.md](../../../AGENTS.md). Use
[architecture](../../context/architecture.md) to locate the owner and
[flows](../../context/flows.md) to trace the affected lifecycle.

For capability or tier work, first read
[capabilities and entitlements](../../context/capabilities-and-entitlements.md).
Distinguish catalogue entries from enforcement callers and billing consumers.

1. Confirm branch, Git root, and imported plugin path. Use
   [development](../../context/development.md) for installation and CI commands.
2. Read the relevant model/service/provider, caller, and test together. Identify
   core contracts before adding a dependency or changing a shared signal.
3. Preserve the invariants in [review prompts](references/review.md) relevant to
   the change. Add focused regressions for changed behavior, using mocked external
   APIs. Documentation-only work needs path/link/frontmatter checks instead.
4. Run the affected tests and repository style checks. State unmet service or
   dependency prerequisites rather than reporting an unexecuted command as passed.
5. Update context if behavior/setup changed. Use
   [the PR template](assets/pr-description.md) for a focused PR against `dev`.
   Related repositories require their own commits and reviewable PRs.
