# AI costs and usage

AI cost answers need two different kinds of evidence: the provider bill tells us what was charged, while serving telemetry tells us who used a model and how many tokens they used. Flashlight keeps those facts separate until it can join them honestly in GOLD.

This is a concept page. For source setup and permissions, see the [Databricks connector guide](https://getflashlight.app/connectors/databricks/index.md).

## The important distinction: how the endpoint is billed

Tokens are not always the billing meter. A cost-per-token number is meaningful only when the provider charges per token.

| Serving mode                      | What is billed                                             | Can cost be allocated by token share?                                              |
| --------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Pay per token                     | Input/output tokens                                        | Yes. Token share reflects the metered charge.                                      |
| Provisioned throughput or compute | Reserved capacity over time                                | No. An idle endpoint can cost money with zero tokens.                              |
| External model                    | Gateway/service activity; model vendor may bill separately | No. Flashlight does not claim the gateway charge is the model vendor's token cost. |
| Unknown                           | Insufficient evidence to classify the billing shape        | No.                                                                                |

`NULL` allocation is intentional: it means “not honestly allocatable,” never “free.”

flowchart LR bill["FOCUS cost records\\nwhat was billed"] --> spend["AI spend GOLD views"] usage["Serving telemetry\\nendpoint, model, requester, tokens"] --> token["AI usage metrics"] spend --> join["One cost-to-usage join\\nwith allocation basis"] token --> join join --> readers["Dashboard and MCP"]

## What the views answer

The published AI views make the following questions answerable without mixing unlike numbers:

| Question                                                       | Evidence used                                             |
| -------------------------------------------------------------- | --------------------------------------------------------- |
| How much of the bill is AI-related?                            | FOCUS cost records, grouped by AI product/service.        |
| Which endpoint, model, project, or requester used the service? | Serving telemetry, when the source exposes it.            |
| How many input and output tokens were used?                    | Serving telemetry.                                        |
| What did a requester’s tokens cost?                            | Only pay-per-token records with a valid allocation basis. |
| Is provisioned capacity idle?                                  | Separate efficiency evidence—not a token allocation.      |

The dashboard labels the basis used for each dollar figure. This keeps a headline AI spend total useful while preventing a false precision per token or per user.

## Guarantees and limits

- **The bill remains canonical.** AI spend is a slice of the provider bill, not a second price calculation from telemetry.
- **Tokens and dollars can have different coverage.** Billing can be present when serving telemetry is unavailable, and vice versa.
- **No fabricated allocation.** Shared, provisioned, external, or unknown billing is shown as unallocated rather than divided by token share.
- **No automatic remediation.** Idle or failed endpoint signals are efficiency findings for review, not commands to change production serving capacity.

## Use the results well

1. Start with the AI spend total and product family for the selected period.
1. Check the allocation basis before comparing requesters or quoting a cost per million tokens.
1. Use token volumes for usage analysis even when dollar allocation is unavailable.
1. Investigate provisioned endpoints with the associated efficiency evidence rather than attributing idle capacity to a requester.

See [GOLD views and metrics](https://getflashlight.app/concepts/gold-views/index.md) for shared metric semantics and [MCP tools](https://getflashlight.app/reference/mcp-tools/index.md) for discovery/query workflow.
