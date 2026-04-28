Preferred deterministic tool:

```bash
workspace/tools/firefly-bridge health
workspace/tools/firefly-bridge accounts list
workspace/tools/firefly-bridge transactions search --days 7 --query groceries
workspace/tools/firefly-bridge expense dry-run --amount 12.34 --description "Lunch" --merchant cafe
```

The bridge prints JSON to stdout and logs diagnostics to stderr. Use it before any ad hoc shell behavior.
