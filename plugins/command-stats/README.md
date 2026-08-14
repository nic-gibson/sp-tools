# redis-diagnostics

Skills for working with Redis Enterprise debuginfo support packages.

## redis-commandstats-report

Builds a per-database report of `INFO commandstats`, and screens package pairs
for counter resets before any before/after comparison is attempted.

The reason this is a skill rather than a snippet of tar-and-awk is that the data
has traps which change the answer, not just the presentation:

- Counters are cumulative since each shard last started, and shards do not
  restart together. A single capture routinely holds shards covering 8 hours
  beside shards covering 6 days, so summing raw counters adds up windows of
  different lengths. Everything is divided by each shard's own uptime.
- Shards belong to databases. Filtering by role alone pulls in another
  database's shards, so the database must be named.
- Replicas execute the replication stream and count it, so including both roles
  double-counts every write. Masters only by default.
- Percentiles are not additive, so `INFO Latencystats` p99s are reported as a
  spread across shards rather than a fabricated aggregate.
- Counters reset on restart, so cross-capture subtraction can produce negative
  "growth" that reads as a finding. `run_id` per shard settles whether a pair
  can be differenced at all.

See `skills/redis-commandstats-report/references/data_model.md` for the package
layout, the rladmin tables, and the full list of failure modes.
