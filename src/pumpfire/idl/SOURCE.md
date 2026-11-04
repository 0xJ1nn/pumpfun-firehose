# Vendored Anchor IDLs

These JSON files are the public Anchor interface descriptions for the pump.fun
bonding-curve program and the PumpSwap AMM program.

| File | Source | Fetched | sha256 of the original |
|---|---|---|---|
| `pump.json` | https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json | 2026-11-05 | `7616681ea557975f9f623ac211080f22a93060781eaf4765b0a488d980997207` |
| `pump_amm.json` | https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json | 2026-11-05 | `b7d8c57a4d9c4dd0109a9ab893052352333252d4eedced10ac091d4d66cab89b` |

The files were trimmed to `address`, `metadata`, `events`, and `types`. No
field, type, discriminator, or address was edited.

To refresh:

```bash
curl -sSL -o /tmp/pump.json https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json
curl -sSL -o /tmp/pump_amm.json https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json
python - <<'PY'
import json
for name in ("pump", "pump_amm"):
    doc = json.load(open(f"/tmp/{name}.json"))
    kept = {k: doc[k] for k in ("address", "metadata", "events", "types") if k in doc}
    with open(f"src/pumpfire/idl/{name}.json", "w") as fh:
        json.dump(kept, fh, indent=2, sort_keys=True)
        fh.write("\n")
PY
```

Then update the sha256 values above and run the tests.
