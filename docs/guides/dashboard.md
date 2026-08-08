# Explore the dashboard

Start the local dashboard:

```bash
flashlight dashboard serve
```

By default it listens at `http://127.0.0.1:8501`. The dashboard is a read-only consumer
of published GOLD data, except for its explicitly interactive connection and assistant
configuration surfaces.

## What the dashboard answers

| Area | Use it to answer |
| --- | --- |
| Overview and provider views | What did we spend, and where did it change? |
| Attribution | Which account, project, owner, tag, or resource drives spend? |
| Efficiency / waste | Which billed capacity is likely recoverable, and why? |
| Policy | Which platform settings meet or miss policy thresholds? |
| AI costs | Which endpoints, models, and requesters drive AI spend and token use? |
| Backing compute and storage | What infrastructure cost sits behind a managed platform? |
| Driver health | Which drivers or workloads show operational risk signals? |

## Read numbers responsibly

Every cost chart uses the GOLD view's declared cost measure. Flashlight uses
`EffectiveCost` as the canonical cost metric and preserves credits/refunds as negative
values. Waste is a measured or rule-based recoverable-spend estimate, not an automatic
remediation recommendation. Read [FOCUS and cost integrity](../concepts/focus-and-integrity.md)
before reconciling a chart to a provider invoice.

## Dashboard assistant

The optional assistant translates questions into the same MCP metric operations used by
external agents. It is bring-your-own-key: configure the provider/model in the dashboard
or `assistant.yml`, and store the API key outside that file. It does not mutate billing
data or cloud resources.
