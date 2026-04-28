# Firefly Companion Operations

Use this local operational skill whenever the task involves Firefly III actions from this companion.

Operating rules:
- Prefer `workspace/tools/firefly-bridge` over browser automation or ad hoc shell commands.
- Normalize dates to ISO 8601 and amounts to two decimals before sending anything to Firefly III.
- Resolve account, category, and merchant aliases using `workspace/config/mappings.yml`.
- Perform a dry-run before any write operation unless the user explicitly wants the final commit.
- Treat ambiguity conservatively; do not invent account names, category names, or merchants.
- Require explicit confirmation for transactions at or above the configured high-value threshold.
- Never delete or bulk-edit without explicit confirmation.
- Avoid destructive operations and do not expose disabled internal capabilities.

Decision sequence:
1. Check API availability with `workspace/tools/firefly-bridge health`.
2. Inspect current state using list or search commands.
3. Build a draft transaction with `expense dry-run`, `income dry-run`, or `transfer dry-run`.
4. Review dedupe and threshold results.
5. Only run `create` when the user intent is unambiguous and confirmed.

Mapping policy:
- Use alias maps first.
- Then apply merchant shortcuts.
- If no safe mapping exists, stop and ask for a concrete account or category name instead of guessing.
