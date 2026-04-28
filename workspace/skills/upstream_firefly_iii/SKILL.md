---
title: Firefly III
source: https://llmbase.ai/openclaw/firefly-iii/
adapted_for: firefly-openclaw-companion
retrieved: 2026-04-15
---

# Firefly III

This vendored skill is a local adaptation of the Firefly III OpenClaw skill listing published at LLMBase. It is kept in-repo so the companion stays offline-friendly and deterministic at runtime.

Use this skill when the task involves:
- inspecting Firefly III accounts, budgets, categories, and recent transactions
- preparing transaction drafts for expenses, income, or transfers
- summarizing recent or monthly financial activity

Preferred execution path:
1. Use `workspace/tools/firefly-bridge health` to confirm API access.
2. Inspect concrete entities with `accounts list`, `categories list`, `budgets list`, or `transactions search`.
3. For writes, draft through `expense dry-run`, `income dry-run`, or `transfer dry-run` first.
4. Only switch to `create` after the payload is concrete and confirmed.

Bridge command examples:

```bash
workspace/tools/firefly-bridge accounts list --type asset
workspace/tools/firefly-bridge categories list
workspace/tools/firefly-bridge budgets list
workspace/tools/firefly-bridge transactions search --days 14 --query groceries
workspace/tools/firefly-bridge summary month --month 2026-04
workspace/tools/firefly-bridge expense dry-run --amount 43.20 --description "Groceries" --merchant coop
```

Constraints:
- Work only through the Firefly III REST API.
- Do not assume account or category names; inspect them first when uncertain.
- Keep outputs deterministic and machine-readable.
