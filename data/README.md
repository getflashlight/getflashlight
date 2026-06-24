# Sample data

`auralake sample` downloads this FOCUS sample on demand and seeds it — the
zero-config way to get demo data (nothing is bundled in the wheel). To use your
own files, drop FOCUS CSV/Parquet anywhere and point a `focus_file` connector at
the path in `~/.auralake/config/connections.yml`.

## Fetch the official FinOps FOCUS sample dataset

```bash
curl -sL -o data/focus_sample.csv \
  https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample.csv
# 1,000 anonymized, FOCUS-1.0-conformant rows (AWS / Microsoft / Oracle).
# A 10,000-row version (focus_sample_10000.csv) is in the same folder.
```

Then point a `focus_file` connector at it in `~/.auralake/config/connections.yml`
and run:

```bash
auralake ingest   # load → BRONZE, rebuild GOLD
```

Source: https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data

> Note: real FOCUS files are not committed to this repo (see `.gitignore`).
