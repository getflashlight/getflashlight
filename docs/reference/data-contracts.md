# Data contracts

Flashlight persists canonical, typed records before deriving analytical GOLD views. These
contracts define what each plane means and prevent a provider-specific implementation from
silently changing shared metrics.

## Cost: `FocusRecord`

One normalized billing charge at charge-period grain.

| Field group | Representative fields | Semantics |
| --- | --- | --- |
| Provenance | `provider_name`, `billing_account_id`, `x_source_connector` | Who supplied the charge and which configured source produced it |
| Periods | `billing_period_*`, `charge_period_*` | Billing context and additive charge interval |
| Cost | `billed_cost`, `effective_cost`, `list_cost`, `contracted_cost`, `billing_currency` | Separate cost bases; views declare which one they use |
| Classification | `charge_category`, `service_*`, `sku_id`, `pricing_category` | Source-normalized billing dimensions |
| Resource and usage | `resource_*`, `consumed_quantity`, `consumed_unit` | Resource-level analysis where the source supplies it |
| Allocation | `tags`, commitment and invoice details | Allocation, discount, and invoice context |
| Extensions | `x_compute_class`, `x_effective_is_list`, `x_record_id` | Explicit non-universal source context |

The internal target FOCUS version is 1.1. Fields use Python `snake_case`; comments in the
model identify their corresponding FOCUS names.

## Efficiency: `EfficiencyRecord`

One actionable entity per provider and month. It includes billed cost, optional native
quantity, utilization/activity signals, ownership, and structured `cause_detail` facts.
GOLD rules—not the source record—classify categories and calculate recoverable spend.

`utilization_pct` is either `0–100` or absent. An absent value means Flashlight cannot
honestly claim utilization-based waste for that entity.

## AI usage: `AiUsageRecord`

One endpoint × served entity × requester × request-project × month token measurement.
It contains request counts, input/output/error token counts, serving mode, and endpoint
configuration facts. It deliberately has no cost column: canonical endpoint spend remains
in the FOCUS plane, and GOLD makes the documented cost/token relationship once.

## Supporting telemetry

| Contract | Grain | Purpose |
| --- | --- | --- |
| `DriverHealthRecord` | Driver × application × user × month | Client-driver fleet health and usage volume |
| `StorageLocationRecord` | Current platform location snapshot | Maps platform storage metadata to cloud object storage; not a cost fact |
| `ComputeInstanceRecord` | Instance membership × month | Maps platform cluster membership to cloud compute; not an efficiency signal |

## Persistence and publication

Cost and telemetry planes are partitioned by provider and month where appropriate. The
ingest window is replaced atomically at its partition scope. GOLD is materialized to a
staging directory and atomically published for readers. See [Data architecture](../architecture.md).
