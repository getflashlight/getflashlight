# GOLD views and metrics

GOLD is Flashlight's published analytics contract. It consists of Parquet-backed views
derived from normalized BRONZE billing and optional telemetry planes. The dashboard and
MCP server both read this layer.

## How to discover views

The available catalog depends on data actually ingested. Use the MCP `list_metrics` tool
or the dashboard to discover names, dimensions, measures, and descriptions at runtime.
This is more reliable than hard-coding a catalog when providers or optional telemetry
are absent.

## Naming and scope

Views are group-qualified, for example `aws.monthly_bill`. A group represents a provider
or cross-provider domain; a view represents a stable question at a defined grain.

| Domain | Examples of questions |
| --- | --- |
| Cost and attribution | Spend by month, account, service, owner, tag, or resource |
| Efficiency | Idle, underutilized, or misplaced capacity and the supporting signal |
| Policy | Compliance status, exceptions, and configured thresholds |
| AI | Endpoint/model/requester token and spend analysis |
| Backing infrastructure | Cloud storage or compute that underlies platform consumption |

## Published-data semantics

Ingest writes to staging and atomically publishes the completed GOLD files. Readers see
the prior published set or the new published set, not a half-written transform. GOLD is
rebuilt after successful source work; a failed connector can therefore leave its previous
data available while other connectors refresh.

## Query safely

Use `describe_metric` before querying a view, select only measures relevant to the
question, and use the narrowest filters possible. When using `run_sql`, keep queries
read-only and bounded. The MCP reader rejects mutations and applies a row limit.
