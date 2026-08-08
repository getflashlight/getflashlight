# MCP tools reference

All tools are read-only. They operate on the currently published GOLD catalog and reject
mutating SQL.

| Tool | Purpose |
| --- | --- |
| `list_metrics()` | List every available group-qualified metric view |
| `describe_metric(name)` | Return title, description, cost metric, dimensions, and measures |
| `query_metric(name, …)` | Query a metric view with filters, selected measures, sort, and limit |
| `list_dimension_values(name, dimension, limit=500)` | Discover valid values for a dimension |
| `list_optimization_rules()` | List deterministic waste-rule guidance and status |
| `list_policy_rules()` | List policy rules, status, sources, and active thresholds |
| `run_sql(sql, limit=200)` | Run a read-only SELECT over the GOLD/SILVER query surface |

## `query_metric`

Arguments:

- `name`: group-qualified metric name, such as `aws.monthly_bill`.
- `filters`: equality map over dimensions/measures. A list matches any supplied value.
- `measures`: optional list limiting returned measures.
- `order_by`: a single column name (a list is accepted; only its first element is used).
- `descending`: whether to reverse the sort.
- `limit`: maximum rows, default `200`.

Use `describe_metric` first. An unknown view or invalid dimension returns an error together
with available choices where possible.

## `run_sql`

Use `run_sql` for bounded analysis that a catalog view cannot express. It is not a write
interface, does not trigger transformation, and should not be treated as an unrestricted
database endpoint. Keep queries explicit, aggregate early, and select only needed columns.
