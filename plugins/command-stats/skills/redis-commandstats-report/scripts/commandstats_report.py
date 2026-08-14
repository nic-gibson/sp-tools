#!/usr/bin/env python3
"""Build a single-database commandstats report from a Redis Enterprise support package.

    # what databases are in here?
    python3 commandstats_report.py --package debuginfo.XXX.tar.gz --list

    # the report
    python3 commandstats_report.py --package debuginfo.XXX.tar.gz \
        --database pers-3950 --outdir ~/Downloads

The database is required and never inferred: a report silently scoped to the
wrong database is worse than no report. `--list` exists so you can find the name
without guessing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parse          # noqa: E402
import metrics        # noqa: E402
import render         # noqa: E402


def _slug(s):
    return ''.join(ch if (ch.isalnum() or ch in '-_.') else '-' for ch in str(s))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Single-database INFO commandstats report from a Redis Enterprise '
                    'debuginfo support package.')
    ap.add_argument('--package', required=True,
                    help='path to debuginfo.*.tar.gz, or an already-extracted directory')
    ap.add_argument('--database',
                    help="database name, id, or 'db:<id>' — required unless --list")
    ap.add_argument('--outdir', default='.', help='where to write the report and CSVs')
    ap.add_argument('--role', default='master', choices=['master', 'slave', 'all'],
                    help='which shards to include (default: master, so replicated '
                         'writes are not double-counted)')
    ap.add_argument('--list', action='store_true',
                    help='list the databases in the package and exit')
    ap.add_argument('--no-csv', action='store_true', help='skip the CSV exports')
    ap.add_argument('--json', action='store_true',
                    help='print a machine-readable summary instead of prose')
    a = ap.parse_args(argv)

    if a.list:
        b = parse.open_bundle(a.package, tempfile.mkdtemp(prefix='debuginfo-'))
        if a.json:
            print(json.dumps({
                'package': os.path.basename(a.package),
                'generated_at': b.generated_at,
                'databases': [
                    dict(db_id=int(i), **{k: (int(v) if k == 'shards' else v)
                                          for k, v in r.items() if k != 'db_id'})
                    for i, r in b.databases.iterrows()],
            }, indent=2, default=str))
        else:
            print(f'package : {os.path.basename(a.package)}')
            print(f'captured: {b.generated_at}')
            print(f'databases ({len(b.databases)}):')
            for i, r in b.databases.iterrows():
                sh = b.shards[b.shards.db_id == i]
                print(f"  db:{i}  {r['db_name']}   {r['shards']} shards "
                      f"({(sh.role == 'master').sum()} master / "
                      f"{(sh.role == 'slave').sum()} replica)   {r['memory_size']}   "
                      f"{r['type']}   persistence={r['persistence']}")
            print('\nRun again with --database <name> to build the report.')
        return 0

    if not a.database:
        b = parse.open_bundle(a.package, tempfile.mkdtemp(prefix='debuginfo-'))
        names = ', '.join(f"db:{i} ({r['db_name']})" for i, r in b.databases.iterrows())
        print(f'error: --database is required. This package contains: {names}', file=sys.stderr)
        return 2

    workdir = tempfile.mkdtemp(prefix='debuginfo-')
    info, cmd, lat, err, meta = parse.load(a.package, a.database, a.role, workdir)
    if cmd.empty:
        print(f"error: no Commandstats data in the {a.role} shard files for "
              f"db:{info['db_id']}", file=sys.stderr)
        return 3

    D = metrics.build(info, cmd, lat, err, meta)
    os.makedirs(a.outdir, exist_ok=True)

    pkg = os.path.basename(a.package)
    tag = pkg
    for suffix in ('.tar.gz', '.tgz'):
        if tag.endswith(suffix):
            tag = tag[:-len(suffix)]
    tag = tag.replace('debuginfo.', '')
    stem = f"{_slug(info['db_name'])}_{_slug(tag)}"

    html_path = os.path.join(a.outdir, f'commandstats_{stem}.html')
    with open(html_path, 'w', encoding='utf-8') as fh:
        fh.write(render.build_html(D, package_label=pkg))

    written = [html_path]
    if not a.no_csv:
        exports = {
            f'commands_{stem}.csv': D['per_cmd'].sort_values('rate', ascending=False),
            f'shards_{stem}.csv': D['per_shard'].sort_values('rate', ascending=False),
            f'cohorts_{stem}.csv': D['cohort'],
            f'latencystats_{stem}.csv': D['lat'],
            f'shard_index_{stem}.csv': info['shard_index'],
        }
        for name, df in exports.items():
            p = os.path.join(a.outdir, name)
            df.to_csv(p, index=(name.startswith(('commands_', 'cohorts_'))))
            written.append(p)

    t, c = D['totals'], D['conc']
    top_cpu = D['per_cmd']['cpu_share'].idxmax()
    gap = metrics.cohort_write_gap(D)
    ok_err, err_msg = metrics.reconcile_errors(D)
    summary = dict(
        database=info['db_name'], db_id=info['db_id'], package=pkg,
        generated_at=info['generated_at'], role=a.role,
        shards_used=len(info['shards_used']), shards_in_db=info['shards_in_db'],
        memory_size=info['memory_size'], db_type=info['db_type'],
        commands=t['n_commands'], cumulative_calls=t['calls'],
        rate_per_sec=round(t['rate'], 4), instantaneous_ops_per_sec=t['iops'],
        cpu_millicores=round(t['cores'] * 1000, 3),
        uptime_min_h=round(t['uptime_min'] / 3600, 2),
        uptime_max_h=round(t['uptime_max'] / 3600, 2),
        uptime_spread=round(t['uptime_spread'], 2),
        measurement_windows=t['n_cohorts'],
        top_command_by_rate=D['per_cmd']['rate'].idxmax(),
        top_command_by_cpu=top_cpu,
        top_cpu_share_pct=round(float(D['per_cmd'].loc[top_cpu, 'cpu_share']) * 100, 2),
        commands_for_90pct=c['n_for_90'],
        rejected_calls=t['rejected'], failed_calls=t['failed'],
        errorstats_reconciles=ok_err, errorstats_detail=err_msg,
        write_workload_gap=(round(gap['ratio'], 1) if gap else None),
        files=written,
    )
    if a.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"report  : {html_path}")
        for p in written[1:]:
            print(f"csv     : {p}")
        print(f"\ndatabase: {info['db_name']} (db:{info['db_id']}) · "
              f"{len(info['shards_used'])} {a.role} shards · captured {info['generated_at']}")
        print(f"windows : {t['n_cohorts']} cohort(s), uptimes "
              f"{t['uptime_min']/3600:,.1f}h–{t['uptime_max']/3600:,.1f}h "
              f"({t['uptime_spread']:,.1f}x spread)")
        print(f"traffic : {t['rate']:,.0f} calls/sec lifetime mean, "
              f"{t['iops']:,.0f}/sec instantaneous, {t['cores']*1000:,.1f} milli-cores CPU")
        print(f"top cost: {top_cpu} at {summary['top_cpu_share_pct']:,.1f}% of CPU time")
        print(f"errors  : rejected={t['rejected']:,} failed={t['failed']:,} — {err_msg}")
        if gap and gap['ratio'] >= 20:
            print(f"note    : write commands {gap['ratio']:,.0f}x more intense in the "
                  f"{gap['long_cohort']} cohort than {gap['short_cohort']} — writes look historical")
    return 0


if __name__ == '__main__':
    sys.exit(main())
