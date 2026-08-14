---
name: redis-commandstats-report
description: Analyse Redis command statistics from a Redis Enterprise debuginfo support package and produce a Redis-branded HTML report for one named database. Use this skill whenever the user points at a debuginfo or support package (a debuginfo tar.gz), mentions rladmin, per-shard redis_NN.txt files, INFO commandstats, cmdstat_ lines, latencystats or errorstats, or asks what a Redis database is doing, which commands cost the most, where CPU time goes, whether load is skewed across shards, or wants command statistics summarised, aggregated or charted. Also use it when the user asks to compare two support packages, because the counter-reset check that decides whether a comparison is even valid lives here. Prefer this over ad-hoc tar, grep or awk work on a support package, even if the user only asks for "a quick look" at a single file.
---

# Redis commandstats report

`INFO commandstats` from a Redis Enterprise support package looks like a simple
table of counters. It isn't, and the traps are the reason this skill exists.
Aggregating it naively produces numbers that are confidently wrong, and nothing
in the output signals that anything went astray.

Build the report with the bundled script rather than by hand:

```bash
python3 scripts/commandstats_report.py \
    --package /path/to/debuginfo.XXXX.tar.gz \
    --database DBNAME \
    --outdir OUTPUT_DIR
```

The script handles extraction, database scoping, rate normalisation, chart
rendering and verification. It needs `pandas`, `numpy` and `matplotlib`.

## Getting the two inputs

**The package.** A `debuginfo.*.tar.gz`, or a directory it has already been
extracted to. Only the files needed get extracted, into a temp directory — these
packages run to hundreds of megabytes and unpacking all of it wastes minutes.

**The database name.** Required, and never guessed. A package can hold several
databases and each one's shards are separately identified, so a report scoped to
the wrong database is worse than no report at all. If the user hasn't said which
database, find out rather than assuming:

```bash
python3 scripts/commandstats_report.py --package PACKAGE --list
```

That prints each database with its id, shard count, memory limit and type in a
couple of seconds. If exactly one database comes back, say which one you're
using and carry on — no need to ask. If several come back, ask which they want,
or build a report per database if they want all of them. The selector accepts a
name (`pers-3950`), a bare id (`4`) or `db:4`, case-insensitively, and refuses
anything matching more or less than one database.

## Why the script and not tar plus awk

Four things about this data change the answer, not just the presentation:

**Counters are cumulative since each shard last started, and shards don't
restart together.** A single capture routinely contains shards whose counters
cover 8 hours next to shards covering 6 days. Summing raw `calls` across such a
set adds up windows of different lengths and yields a number that means nothing.
Everything is divided by each shard's own `uptime_in_seconds`, which makes
shards comparable and produces real rates — something the counters alone can't
give you.

**Shards belong to databases.** The `DATABASES` and `SHARDS` tables in
`node_*.rladmin` are keyed by database, so every shard is attributable to
exactly one. Filtering by `role = master` alone would silently pull in another
database's shards.

**Master and replica both count the commands they run.** Replicas execute the
replication stream, so including them double-counts every write. The default is
masters only; `--role slave` or `--role all` are there for when comparing roles
is the actual question.

**Percentiles are not additive.** `INFO Latencystats` gives real p50/p99/p99.9
per shard, which fixes the biggest weakness of commandstats — a mean cannot
distinguish uniformly-slow from usually-fast-with-a-bad-tail. But averaging p99
across shards is meaningless, so the report shows the median across shards with
the min and max alongside, and says so.

## Reading the result

The script prints a summary to stdout. Relay what matters, and lead with
whichever of these the data actually supports rather than walking the sections in
order:

- **Measurement windows.** If it reports more than one cohort, the capture is a
  blend of different time windows and §3 of the report is the most interesting
  part. A large write-workload gap between the longest and shortest cohort means
  writes have stopped: uniform across shards within a cohort but absent from
  recent windows is the signature of traffic that ceased, not of key skew.
- **Where CPU goes.** The most-called command is rarely the most expensive.
  Where administrative and monitoring commands dominate CPU time, that is worth
  saying plainly — on a lightly loaded database, observability overhead can
  outweigh the workload it observes.
- **Lifetime versus instantaneous.** A big gap between the rate-normalised mean
  and summed `instantaneous_ops_per_sec` means the workload isn't steady, and the
  lifetime mean is smoothing over something.
- **Total CPU.** Command execution in milli-cores is the scale check on
  everything else; it tells you whether throughput is anywhere near being the
  binding constraint.

Then show the user the file. Don't paste the whole table set into chat — the
report has table twins for every figure, and the CSVs are there for anyone who
wants to work with the numbers.

## Comparing two packages

Users often want growth or drift, which needs two captures. Screen the pair
first — the shard files carry `run_id` and `uptime_in_seconds` in their
`# Server` section, and those settle it definitively:

```bash
python3 scripts/check_pair.py --before PACKAGE_A --after PACKAGE_B --database DBNAME
```

A shard whose `run_id` changed between captures restarted, so its counters reset
and any difference is meaningless. The useful rule the check reports: a capture
at time B can only be differenced against one at time A if every shard's uptime
at B exceeds B − A, which makes the **minimum shard uptime** in the later
package the maximum usable look-back.

When the pair fails, say so and explain what would work, rather than producing
deltas with a warning attached. Negative "growth" is not a finding, it's an
artefact, and a reader who skims will take it at face value. Two single-snapshot
reports side by side are the honest fallback, and share-based figures remain
comparable across them because they're uptime-invariant.

## Reference

`references/data_model.md` covers the package layout, the rladmin tables, which
INFO sections carry what, the fields worth reading beyond commandstats, and the
failure modes worth knowing about. Read it when the data doesn't look like the
script expects, when you need a field the report doesn't surface, or when a
package is laid out unusually.
