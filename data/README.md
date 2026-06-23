# Sample data

Drop FOCUS files here (mounted into the containers at `/data`) and point a
`focus_file` connector at them in `config/connections.yml`.

## Fetch the official FinOps FOCUS sample dataset

```bash
curl -sL -o data/focus_sample.csv \
  https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample.csv
# 1,000 anonymized, FOCUS-1.0-conformant rows (AWS / Microsoft / Oracle).
# A 10,000-row version (focus_sample_10000.csv) is in the same folder.
```

Then enable the `focus_file` connector in `config/connections.yml` and run:

```bash
docker compose --profile ingest run --rm ingest   # ingest + build views
# or, against a local server:  auralake ingest
```

Source: https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data

> Note: real FOCUS files are not committed to this repo (see `.gitignore`).
