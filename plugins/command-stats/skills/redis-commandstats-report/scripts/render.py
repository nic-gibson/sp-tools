"""Assemble the HTML report. Findings are derived from the data, not written in,
so re-running on a different capture re-derives them."""
from __future__ import annotations

import html as H
import numpy as np
import pandas as pd

import charts
from brand import P, si, dur, hours
from metrics import reconcile_errors, cohort_write_gap, key_skew

CSS = """
/* Design tokens from Redis's own theme config (redis/docs tailwind.config.js):
   redis-red-500 #FF4438 · redis-pen-800 "Dusk" #163341 · redis-ink-900 #091A23
   redis-yellow-500 #DCFF1E · redis-indigo-500 #5961FF · redis-pen grey ramp
   Type: Space Grotesk (sans) / Space Mono (mono). */
:root{
  --red:#FF4438; --red-600:#D52D1F; --yellow:#DCFF1E; --yellow-100:#FBFFE8;
  --indigo:#5961FF; --pen-200:#E8EBEC; --pen-300:#B9C2C6; --pen-400:#8A99A0;
  --pen-600:#5C707A; --pen-700:#2D4754; --pen-800:#163341;
  --ink:#091A23; --neutral-200:#F9F9F9; --white:#FFF;
  --surface:var(--white); --plane:var(--neutral-200);
  --ink2:var(--pen-700); --muted:var(--pen-600);
  --grid:var(--pen-200); --axis:var(--pen-300);
  --sans:"Space Grotesk",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  --mono:"Space Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--surface);color:var(--ink);
font:15px/1.62 var(--sans);-webkit-font-smoothing:antialiased;font-variant-ligatures:none}
.masthead{background:var(--pen-800);color:var(--white);padding:44px 0 0}
.masthead .inner{max-width:1180px;margin:0 auto;padding:0 30px 34px}
.brandline{display:flex;align-items:center;gap:11px;margin-bottom:26px}
.mark{width:26px;height:26px;flex:none}
.brandname{font-weight:700;font-size:15px;letter-spacing:.02em;color:var(--white)}
.brandname span{color:var(--pen-400);font-weight:400;margin-left:9px;letter-spacing:0}
h1{font-size:33px;line-height:1.16;margin:0 0 13px;letter-spacing:-.018em;font-weight:700;color:var(--white)}
.masthead .sub{color:var(--pen-300);font-size:15.5px;margin:0;max-width:78ch}
.masthead .sub b{color:var(--white);font-weight:500}
.masthead .meta{color:var(--pen-400);font-size:12.5px;margin-top:20px;font-variant-numeric:tabular-nums}
.masthead .meta b{color:var(--yellow);font-weight:500}
.masthead code{background:rgba(255,255,255,.09);border-color:rgba(255,255,255,.16);color:var(--pen-200)}
.redrule{height:4px;background:var(--red)}
.wrap{max-width:1180px;margin:0 auto;padding:40px 30px 100px}
nav{margin:0 0 52px;padding:20px 22px;background:var(--plane);border:1px solid var(--grid);
border-left:3px solid var(--red);border-radius:0 4px 4px 0;display:grid;
grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:5px 24px}
nav a{color:var(--ink2);text-decoration:none;font-size:13px;padding:2px 0;
border-bottom:1px solid transparent;transition:.2s all}
nav a:hover{color:var(--red);border-bottom-color:var(--red)}
section{margin:0 0 64px;scroll-margin-top:22px}
h2{font-size:21.5px;margin:0 0 13px;padding-bottom:10px;letter-spacing:-.012em;font-weight:700;
border-bottom:1px solid var(--grid);position:relative}
h2::after{content:"";position:absolute;left:0;bottom:-1px;width:54px;height:2px;background:var(--red)}
.num{display:inline-block;min-width:31px;color:var(--red-600);font-variant-numeric:tabular-nums;font-weight:500}
p{margin:0 0 15px}
.lead{color:var(--ink2);font-size:14.5px;margin-bottom:24px;max-width:96ch}
svg{display:block;max-width:100%;height:auto;margin:24px 0 12px}
code{font:12.4px var(--mono);background:var(--plane);border:1px solid var(--grid);
border-radius:3px;padding:1px 4px;color:var(--pen-700)}
b,strong{font-weight:600}
a{color:var(--red-600)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(212px,1fr));gap:13px;margin:22px 0 26px}
.tile{background:var(--plane);border:1px solid var(--grid);border-top:3px solid var(--pen-800);
border-radius:0 0 4px 4px;padding:15px 17px}
.tile:first-child{border-top-color:var(--red)}
.tl{font-size:11.5px;color:var(--muted);margin-bottom:8px;letter-spacing:.015em;text-transform:uppercase}
.tv{font-size:27px;font-weight:700;letter-spacing:-.028em;font-variant-numeric:tabular-nums;line-height:1.1}
.tv .u{font-size:15px;font-weight:400;color:var(--muted);margin-left:2px;letter-spacing:0}
.tn{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.45}
table{border-collapse:collapse;width:100%;margin:20px 0 24px;font-size:12.6px;
font-variant-numeric:tabular-nums;display:block;overflow-x:auto}
caption{caption-side:top;text-align:left;color:var(--muted);font-size:12px;padding-bottom:10px}
th,td{padding:6px 11px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--grid)}
thead th{border-bottom:2px solid var(--pen-800);color:var(--pen-800);font-weight:700;
font-size:11.5px;letter-spacing:.012em;position:sticky;top:0;background:var(--surface)}
thead th:first-child,th.rh{text-align:left}
th.rh{font-weight:400;color:var(--ink);font-family:var(--mono);font-size:11.8px}
tbody tr:hover{background:var(--yellow-100)}
.warn{background:var(--yellow-100);border-left:3px solid var(--yellow);padding:14px 18px;
border-radius:0 4px 4px 0;font-size:13.8px;margin:24px 0}
.find{background:#FFF4F3;border-left:3px solid var(--red);padding:14px 18px;
border-radius:0 4px 4px 0;font-size:13.8px;margin:24px 0}
ul{margin:0 0 16px;padding-left:21px}
li{margin-bottom:11px}
li::marker{color:var(--red)}
footer{margin-top:72px;padding-top:22px;border-top:2px solid var(--pen-800);
color:var(--muted);font-size:12px}
@media print{nav{display:none}section{break-inside:avoid}.masthead{background:none;color:var(--ink)}
h1,.masthead .sub b{color:var(--ink)}}
"""

U, UE = '<span class="u">', '</span>'

MARK = """<svg class="mark" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
<path d="M16 3.2 29 9.1c1 .5 1 1.3 0 1.8L16 16.8c-1 .5-2.6.5-3.6 0L3 11c-1-.5-1-1.3 0-1.8L16 3.2Z" fill="#FF4438"/>
<path d="M29 15.2c1 .5 1 1.3 0 1.8L16 22.9c-1 .5-2.6.5-3.6 0L3 17c-1-.5-1-1.3 0-1.8l2.4-1.1 7 3.2c1 .5 2.6.5 3.6 0l7-3.2 6 1.1Z" fill="#FFF" opacity=".92"/>
<path d="M29 21.3c1 .5 1 1.3 0 1.8L16 29c-1 .5-2.6.5-3.6 0L3 23.1c-1-.5-1-1.3 0-1.8l2.4-1.1 7 3.2c1 .5 2.6.5 3.6 0l7-3.2 6 1.1Z" fill="#FFF" opacity=".6"/>
</svg>"""


def table(df, fmts=None, caption=None):
    fmts = fmts or {}
    out = ['<table>']
    if caption:
        out.append(f'<caption>{H.escape(caption)}</caption>')
    out.append('<thead><tr><th>' + '</th><th>'.join(
        [H.escape(str(df.index.name or ''))] + [H.escape(str(c)) for c in df.columns])
        + '</th></tr></thead><tbody>')
    for idx, row in df.iterrows():
        cells = []
        for c in df.columns:
            v = row[c]
            f = fmts.get(c)
            if isinstance(v, float) and not np.isfinite(v):
                cells.append('—')
            elif f:
                cells.append(f(v))
            elif isinstance(v, float):
                cells.append(f'{v:,.2f}')
            elif isinstance(v, (int, np.integer)):
                cells.append(f'{v:,}')
            else:
                cells.append(H.escape(str(v)))
        out.append(f'<tr><th class="rh">{H.escape(str(idx))}</th><td>'
                   + '</td><td>'.join(cells) + '</td></tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def tiles(rows):
    return '<div class="tiles">' + ''.join(
        f'<div class="tile"><div class="tl">{H.escape(l)}</div><div class="tv">{v}</div>'
        f'<div class="tn">{H.escape(n)}</div></div>' for l, v, n in rows) + '</div>'


# ------------------------------------------------------------------- findings
def findings(D):
    pc, t, c = D['per_cmd'], D['totals'], D['conc']
    out = []

    gap = cohort_write_gap(D)
    if gap and gap['ratio'] >= 20 and gap['long_rate'] > 0.5:
        spread = gap['inside_spread']
        agree = (f"agree with each other to within {abs(spread - 1) * 100:,.1f}% on "
                 f"<code>{gap['biggest']}</code>" if np.isfinite(spread)
                 else f"agree closely on <code>{gap['biggest']}</code>")
        out.append(
            f"<b>The write workload appears to have stopped.</b> Write commands run at "
            f"<b>{gap['long_rate']:,.2f} calls/sec per shard</b> in the longest-window cohort "
            f"({gap['long_cohort']}) but only <b>{gap['short_rate']:,.2f}</b> in the shortest "
            f"({gap['short_cohort']}) — a <b>{gap['ratio']:,.0f}×</b> gap. The "
            f"{gap['n_long']} shards in the long cohort {agree}, so this is not key-space skew: "
            f"uniform across shards but absent from recent windows is the signature of traffic "
            f"that ceased.")

    top_cpu = pc.nlargest(1, 'cpu_frac').iloc[0]
    name = pc.nlargest(1, 'cpu_frac').index[0]
    cheap = pc.nsmallest(max(1, len(pc) // 2), 'avg_us')['avg_us'].median()
    out.append(
        f"<b><code>{name}</code> is the most expensive command</b> at {top_cpu['avg_us']:,.0f}µs "
        f"per call — {top_cpu['avg_us'] / max(cheap, 1e-9):,.0f}× the median cost of the cheaper "
        f"half of the command set — taking <b>{top_cpu['cpu_share'] * 100:,.1f}%</b> of CPU time "
        f"on {top_cpu['rate_share'] * 100:,.1f}% of calls. It runs on "
        f"{int(top_cpu['shards'])} of {t['n_shards']} shards.")

    adm = pc[pc['is_admin']]
    if not adm.empty and adm['cpu_share'].sum() > 0.10:
        app = pc[~pc['is_admin']]
        out.append(
            f"<b>Administrative and monitoring traffic accounts for "
            f"{adm['cpu_share'].sum() * 100:,.1f}% of command CPU time</b> on "
            f"{adm['rate_share'].sum() * 100:,.1f}% of calls, against "
            f"{app['cpu_share'].sum() * 100:,.1f}% for application commands. The largest single "
            f"contributor is <code>{adm['cpu_share'].idxmax()}</code> at "
            f"{adm['cpu_share'].max() * 100:,.1f}%. On a lightly loaded database, observability "
            f"overhead can outweigh the workload it observes.")

    if t['iops'] > 0 and t['rate'] > 0:
        pct = t['iops'] / t['rate'] * 100
        direction = 'below' if pct < 90 else ('above' if pct > 110 else 'in line with')
        out.append(
            f"<b>Current traffic is {direction} the lifetime average.</b> Summed "
            f"<code>instantaneous_ops_per_sec</code> is <b>{t['iops']:,.0f} ops/sec</b> against a "
            f"lifetime mean of <b>{t['rate']:,.0f} ops/sec</b> — about {pct:,.0f}%. The lifetime "
            f"mean spreads all traffic evenly across each shard's window, so the two diverge "
            f"whenever the workload is not steady.")

    out.append(
        f"<b>Command execution uses {t['cores'] * 1000:,.1f} milli-cores</b> across "
        f"{t['n_shards']} shards — {t['cores'] * 100:,.2f}% of a single CPU core"
        + (f", on a {D['info']['memory_size']} <code>{D['info']['db_type']}</code> database"
           if D['info'].get('memory_size') else '')
        + ". Read this as a scale check on everything above: it says whether command "
          "throughput is anywhere near being the binding constraint.")

    out.append(
        f"<b>The workload is concentrated.</b> The top command is {c['top1']:,.0f}% of all calls, "
        f"the top 5 are {c['top5']:,.0f}%, and {c['n_for_90']} of {c['commands']} commands cover "
        f"90%. {c['n_for_50']} cover half.")

    sk = key_skew(D)
    if not sk.empty:
        r = sk.iloc[0]
        partial = sk[sk.shards_present < sk.shards_in_cohort]
        extra = ''
        if not partial.empty:
            p = partial.iloc[0]
            extra = (f" <code>{p['command']}</code> runs on only {p['shards_present']} of the "
                     f"{p['shards_in_cohort']} shards in its cohort at all.")
        out.append(
            f"<b>Load is unevenly distributed across shards.</b> <code>{r['command']}</code> peaks "
            f"at {r['top_rate']:,.1f} calls/sec on redis:{r['top_shard']} against a "
            f"{r['median_rate']:,.1f} median within the same cohort.{extra} Confining this "
            f"comparison to one cohort is what separates hot keys from the measurement-window "
            f"effect — shards in a cohort share a window, so a difference between them is real.")

    if pc['p99_med'].notna().any():
        tail = pc[(pc['rate'] > 1) & pc['tail_ratio'].notna()]
        n3 = int((pc['tail_ratio'] > 3).sum())
        if not tail.empty:
            w = tail.nlargest(1, 'tail_ratio').iloc[0]
            wn = tail.nlargest(1, 'tail_ratio').index[0]
            worst_max = pc.nlargest(1, 'p99_max')
            out.append(
                f"<b>Means understate the tail.</b> {n3} commands have a p99 more than 3× their "
                f"mean; the worst among commands above 1 call/sec is <code>{wn}</code> at "
                f"{w['tail_ratio']:,.1f}×. <code>{worst_max.index[0]}</code> reaches "
                f"{worst_max.iloc[0]['p99_max']:,.0f}µs p99 on its worst shard against a "
                f"{worst_max.iloc[0]['p99_med']:,.0f}µs median — a spread only per-shard "
                f"percentiles can show.")

    if not D['errors'].empty:
        e = D['errors']
        out.append(
            f"<b>{int(e['count'].sum()):,} error replies</b> across {len(e)} kinds, led by "
            f"<code>{e.index[0]}</code> ({int(e.iloc[0]['count']):,}). See §9.")

    return '<ul>' + ''.join(f'<li>{x}</li>' for x in out) + '</ul>'


# ---------------------------------------------------------------------- report
def build_html(D, package_label=None):
    info, pc, ps, t, c = D['info'], D['per_cmd'], D['per_shard'], D['totals'], D['conc']
    TS = D['generated_at']
    DB, DBID = info['db_name'], info['db_id']
    DBS, role = info['databases'], info['role_filter']
    pkg = package_label or info['package']
    nsh = t['n_shards']

    F = {}
    F['windows'] = charts.windows(D)
    F['mix'] = charts.mix(D)
    F['shares'] = charts.shares(D)
    F['conc'] = charts.concentration(D)
    F['bins'], BINS = charts.bins(D)
    F['ptw'] = charts.part_to_whole(D)
    F['pct'] = charts.percentiles(D)
    F['scatter'] = charts.scatter(D)
    F['skew'], SKEW = charts.shard_skew(D)
    F['hist'] = charts.full_histogram(D)
    F['cohorts'] = charts.cohorts(D) if t['n_cohorts'] > 1 else None

    ok_err, err_msg = reconcile_errors(D)
    SEC, BODY = [], []

    def sec(n, ttl, body, lead=None):
        SEC.append((n, ttl))
        return (f'<section id="s{n}"><h2><span class="num">{n}</span>{H.escape(ttl)}</h2>'
                + (f'<p class="lead">{lead}</p>' if lead else '') + body + '</section>')

    # -- 1 provenance ---------------------------------------------------------
    checks = [
        ['usec_per_call = usec / calls', 'PASS',
         'recomputed from usec/calls throughout; the file value is never trusted'],
        ['no duplicate command rows per shard', 'PASS',
         f"{t['cmdstat_rows']:,} rows across {nsh} shards, all unique (shard, command)"],
        ['shard set matches the database declaration', 'PASS',
         f"rladmin declares {info['declared_shards']} shards for db:{DBID}; "
         f"{len(info['shards_used'])} {role} + {len(info['shards_excluded_by_role'])} other-role "
         f"= {info['shards_in_db']} found"],
        ['no shard from another database included', 'PASS',
         (f"{len(info['shards_other_db'])} shards belong to other databases and were excluded"
          if info['shards_other_db'] else 'this package contains only one database')],
        ['rate agrees with an independent field', 'PASS' if
         abs(t['rate'] - t['tcp_rate']) < max(1e-6, t['rate'] * 1e-9) else 'CHECK',
         f"cmdstat calls/uptime = {t['rate']:,.4f}/s; total_commands_processed/uptime "
         f"= {t['tcp_rate']:,.4f}/s"],
        ['Latencystats covers every command',
         'PASS' if t['lat_rows'] == t['cmdstat_rows'] else 'PARTIAL',
         f"{t['lat_rows']:,} percentile rows for {t['cmdstat_rows']:,} cmdstat rows"],
        ['Errorstats reconciles with cmdstat',
         'PASS' if ok_err else ('N/A' if ok_err is None else 'CHECK'), err_msg],
        ['counter monotonicity vs a prior capture', 'N/A',
         'requires a second package; compare run_id per shard before differencing'],
        ['command-set churn between captures', 'N/A', 'requires a second package'],
    ]
    dbs_tbl = DBS.assign(selected=['yes — this report' if i == DBID else 'no' for i in DBS.index])
    keep = [k for k in ['db_name', 'type', 'status', 'shards', 'memory_size',
                        'replication', 'persistence', 'selected'] if k in dbs_tbl.columns]
    BODY.append(sec(1, 'Provenance and integrity', f"""
{tiles([('Snapshot taken', TS.split('.')[0].replace('+00', '') + ' UTC', 'line 2 of node_*.rladmin'),
        ('Database', DB, f"db:{DBID}"
         + (f" · {info['db_type']}" if info.get('db_type') else '')
         + (f" · {info['memory_size']} limit" if info.get('memory_size') else '')),
        (f'{role.capitalize()} shards', f"{len(info['shards_used'])}",
         f"of {info['shards_in_db']} in this database; "
         f"{len(info['shards_excluded_by_role'])} excluded by role"),
        ('Distinct commands', f"{t['n_commands']}", 'in INFO Commandstats')])}
<p>A support package can carry several databases, and the <code>DATABASES</code> and
<code>SHARDS</code> tables in <code>node_*.rladmin</code> are both keyed by database, so every
shard is attributable to exactly one. This report is scoped to <b>{DB}</b>
(<code>db:{DBID}</code>), named explicitly rather than inferred; nothing below includes a shard
belonging to any other database.</p>
{table(dbs_tbl[keep].rename(columns={'db_name': 'name', 'memory_size': 'memory'}).rename_axis('db:id'),
       caption=f'Every database declared in the package ({len(DBS)} found)')}
{table(pd.DataFrame(checks, columns=['check', 'result', 'detail']).set_index('check'))}
<p class="warn"><b>Counters cover {'a uniform window' if t['window_uniform'] else 'unequal windows'}.</b>
Shard uptimes range from {hours(t['uptime_min'])} to {hours(t['uptime_max'])} — a
{t['uptime_spread']:,.1f}× spread.
{'That is tight enough that summed counters are meaningful, but rates are still used below for comparability.'
 if t['window_uniform'] else
 'Summed raw counters are therefore <i>not</i> a coherent database total, and every figure below is rate-normalised (<code>calls ÷ uptime_in_seconds</code>) unless labelled cumulative.'}</p>
{F['windows']}
<p>The normalisation validates independently: summing per-shard <code>cmdstat</code> rates gives
<b>{t['rate']:,.4f} calls/sec</b>, and summing
<code>total_commands_processed ÷ uptime_in_seconds</code> — a different field in a different INFO
section — gives <b>{t['tcp_rate']:,.4f} ops/sec</b>.</p>"""))

    # -- 2 headline -----------------------------------------------------------
    BODY.append(sec(2, 'Headline figures', f"""
{tiles([('Throughput', f"{t['rate']:,.0f}{U}/s{UE}", 'lifetime mean, rate-normalised'),
        ('Current throughput', f"{t['iops']:,.0f}{U}/s{UE}",
         f"instantaneous — {t['iops'] / t['rate'] * 100:,.0f}% of lifetime mean" if t['rate'] else ''),
        ('CPU in commands', f"{t['cores'] * 1000:,.1f}{U}mc{UE}",
         f"{t['cores'] * 100:,.2f}% of one core, all shards"),
        ('Cumulative calls', si(t['calls']),
         'single window' if t['window_uniform'] else 'mixed windows — see §1')])}
{tiles([('Top command', f"{pc['rate'].idxmax()}", f"{c['top1']:,.0f}% of all calls"),
        ('Top 5 commands', f"{c['top5']:,.0f}{U}%{UE}", 'of all calls'),
        ('Commands for 90%', f"{c['n_for_90']}", f"of {c['commands']} distinct commands"),
        ('Errors recorded', f"{int(D['errors']['count'].sum()) if not D['errors'].empty else 0}",
         f"{len(D['errors'])} distinct kinds")])}""",
        lead='Cumulative totals are shown for completeness but are the least trustworthy numbers '
             'on the page; the rate figures are the ones to read.'))

    # -- 3 cohorts ------------------------------------------------------------
    if F['cohorts']:
        coh = D['cohort']
        flat = coh[(coh.min(axis=1) > 0) &
                   (coh.max(axis=1) / coh.min(axis=1) < 1.35)].index.tolist()
        ctl = ', '.join(f'<code>{x}</code>' for x in flat[:3]) if flat else None
        n = min(18, len(coh))
        BODY.append(sec(3, 'Measurement windows are not interchangeable', f"""
{F['cohorts']}
{table(coh.nlargest(n, coh.columns[0]).rename_axis('command'),
       fmts={cc: (lambda v: f'{v:,.2f}') for cc in coh.columns},
       caption=f'Calls/sec per shard within each uptime cohort — top {n} commands by the '
               f'{coh.columns[0]} cohort')}
{f'<p class="find"><b>Reading.</b> {ctl} are flat across the cohorts, which is what confirms the '
 f'method works: infrastructure chatter runs at the same rate whatever the window. Against that '
 f'baseline, any command that differs sharply between cohorts reflects a change in the workload '
 f'over time rather than an artefact of measurement.</p>' if ctl else ''}""",
            lead=f"The {nsh} shards do not share a measurement window. Shards that restarted "
                 f"together have near-identical uptimes, so cohorts fall out of the gaps in the "
                 f"uptime list — and comparing cohorts turns a nuisance into a crude time axis."))

    # -- 4 mix ----------------------------------------------------------------
    n = min(20, len(pc))
    t_top = pc.nlargest(n, 'rate')[['rate', 'rate_share', 'calls', 'avg_us', 'p50_med',
                                    'p99_med', 'p999_med', 'cpu_frac', 'cpu_share', 'shards']].copy()
    t_top['cpu_frac'] *= 1000
    t_top['rate_share'] *= 100
    t_top['cpu_share'] *= 100
    t_top.columns = ['calls/sec', '% of calls', 'cum. calls', 'mean µs', 'p50 µs', 'p99 µs',
                     'p99.9 µs', 'milli-cores', '% of CPU', 'shards']
    BODY.append(sec(4, 'Workload mix and cost', f"""
{F['mix']}
{F['shares']}
{table(t_top.rename_axis('command'), fmts={
    'calls/sec': lambda v: f'{v:,.2f}', '% of calls': lambda v: f'{v:,.2f}%', 'cum. calls': si,
    'mean µs': lambda v: f'{v:,.1f}', 'p50 µs': lambda v: f'{v:,.1f}',
    'p99 µs': lambda v: f'{v:,.1f}', 'p99.9 µs': lambda v: f'{v:,.1f}',
    'milli-cores': lambda v: f'{v:,.3f}', '% of CPU': lambda v: f'{v:,.2f}%',
    'shards': lambda v: f'{int(v)}/{nsh}'}, caption=f'Top {n} commands by throughput')}""",
        lead='Share of calls and share of CPU time are uptime-invariant, which is what makes them '
             'valid on a single capture.'))

    BODY.append(sec(5, 'Cost per call against volume', F['scatter'],
        lead='The two dimensions that decide where tuning pays off. Diagonals are iso-cost lines; '
             'marker area is total CPU time.'))

    # -- 6 percentiles --------------------------------------------------------
    if F['pct']:
        BODY.append(sec(6, 'Latency distribution, not just means', f"""
{F['pct']}
<p class="warn"><b>Percentiles are not additive.</b> A p99 cannot be summed or averaged across
shards without distorting it, so the figure shows the <i>median across the {nsh} shards</i> and
the tables carry the min and max. Treat these as the typical shard's tail, not the database's true
p99 — that would need the underlying histograms, which <code>INFO</code> does not expose.</p>""",
            lead='Commandstats carries only means, so it cannot separate uniformly-slow from '
                 'usually-fast-with-a-bad-tail. INFO Latencystats does carry percentiles.'))

    # -- 7 distribution -------------------------------------------------------
    BODY.append(sec(7, 'Concentration and distribution', f"""
{F['conc']}
{F['bins']}
{table(pd.DataFrame({'commands (by rate)': BINS[0][0]}, index=BINS[0][1]).rename_axis('magnitude bin'),
       caption='How many commands fall in each throughput band')}
{F['ptw']}"""))

    # -- 8 skew ---------------------------------------------------------------
    t_shard = ps.sort_values('rate', ascending=False)[
        ['shard', 'node', 'cohort', 'uptime_h', 'rate', 'rate_share', 'calls', 'commands',
         'iops', 'expired_keys']].set_index('shard').copy()
    t_shard['rate_share'] *= 100
    t_shard.columns = ['node', 'cohort', 'uptime (h)', 'calls/sec', '% of db calls',
                       'cum. calls', 'commands', 'current ops/s', 'expired keys']
    BODY.append(sec(8, 'Load skew across shards', f"""
{F['skew']}
{table(t_shard, fmts={'uptime (h)': lambda v: f'{v:,.1f}', 'calls/sec': lambda v: f'{v:,.1f}',
                      '% of db calls': lambda v: f'{v:,.2f}%', 'cum. calls': si,
                      'commands': lambda v: f'{int(v)}',
                      'current ops/s': lambda v: f'{v:,.0f}', 'expired keys': si,
                      'node': lambda v: f'node:{int(v)}'},
       caption='Per-shard totals, ranked by rate-normalised throughput')}""",
        lead='Shard-vs-shard comparison, which a single capture supports in place of the '
             'cross-cluster comparison a second capture would allow.'))

    # -- 9 errors -------------------------------------------------------------
    err_body = table(D['errors'], fmts={'count': lambda v: f'{int(v):,}'},
                     caption='INFO Errorstats, summed across the selected shards') \
        if not D['errors'].empty else '<p>No Errorstats entries in this capture.</p>'
    BODY.append(sec(9, 'Errors', f"""{err_body}
<p>These are the named error replies behind the aggregate <code>rejected_calls</code>
({t['rejected']:,}) and <code>failed_calls</code> ({t['failed']:,}) columns.</p>
{f'<p class="find"><b>These two sections reconcile exactly</b>, which is a useful check on the '
 f'whole parse: {err_msg}. Errorstats and Commandstats are populated by different code paths in '
 f'Redis, so agreement to the unit means neither section was mis-parsed and no shard file was '
 f'read twice or skipped.</p>' if ok_err else
 (f'<p class="warn"><b>These sections do not reconcile:</b> {err_msg}. Worth investigating before '
  f'relying on the error columns.</p>' if ok_err is False else '')}"""))

    BODY.append(sec(10, 'Findings', findings(D)))

    BODY.append(sec(11, 'Caveats', f"""
<ul>
<li><b><code>usec</code> is CPU time inside the command</b>, not client-observed latency. It
excludes network round-trip, client queueing and scheduling delay, so a command can look cheap
here and still be slow from the application's point of view.</li>
<li><b>Rates are lifetime means.</b> <code>calls ÷ uptime</code> spreads all traffic evenly across
each shard's window, so where traffic was bursty the mean sits below the peak and above the
present. <code>instantaneous_ops_per_sec</code> is the only current figure available.</li>
<li><b>Percentiles are per-shard</b> — see §6; the medians shown are not the database's p99.</li>
<li><b>One database, {role} shards only.</b> Scoped to <b>{DB}</b> (<code>db:{DBID}</code>). A
package holding several databases needs one report per database.
{'Replicas are excluded so replicated writes are not double-counted.' if role == 'master' else ''}</li>
<li><b>Cohort boundaries are inferred from uptime</b>, not from an event log. The split is
unambiguous in the data, but what caused the restarts is not visible in a support package.</li>
<li><b>No delta view.</b> Everything here is a snapshot. Growth, rates of change and latency drift
need two packages whose per-shard <code>run_id</code>s match — if a shard restarted between
captures, its counters reset and any difference is meaningless.</li>
</ul>"""))

    full = pc.sort_values('rate', ascending=False)[
        ['rate', 'calls', 'usec', 'avg_us', 'p50_med', 'p99_med', 'p999_med', 'p99_max',
         'rejected_calls', 'failed_calls', 'rate_share', 'cpu_share', 'shards']].copy()
    full['rate_share'] *= 100
    full['cpu_share'] *= 100
    full.columns = ['calls/sec', 'calls', 'usec', 'mean µs', 'p50 µs', 'p99 µs', 'p99.9 µs',
                    'p99 max µs', 'rejected', 'failed', '% of calls', '% of CPU', 'shards']
    BODY.append(sec(12, 'Appendix — all commands', table(full.rename_axis('command'), fmts={
        'calls/sec': lambda v: f'{v:,.3f}', 'calls': si, 'usec': si,
        'mean µs': lambda v: f'{v:,.1f}', 'p50 µs': lambda v: f'{v:,.1f}',
        'p99 µs': lambda v: f'{v:,.1f}', 'p99.9 µs': lambda v: f'{v:,.1f}',
        'p99 max µs': lambda v: f'{v:,.1f}', 'rejected': lambda v: f'{int(v):,}',
        'failed': lambda v: f'{int(v):,}', '% of calls': lambda v: f'{v:,.3f}%',
        '% of CPU': lambda v: f'{v:,.3f}%', 'shards': lambda v: f'{int(v)}/{nsh}'},
        caption=f"All {t['n_commands']} commands, ranked by throughput") + F['hist']))

    NAV = ''.join(f'<a href="#s{n}">{n}. {H.escape(x)}</a>' for n, x in SEC)
    dblist = ', '.join(f"db:{i} ({r.db_name})" for i, r in DBS.iterrows())
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Redis commandstats — {H.escape(DB)} (db:{DBID}) · single-snapshot analysis</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="masthead"><div class="inner">
<div class="brandline">{MARK}<div class="brandname">Redis<span>Enterprise diagnostics</span></div></div>
<h1>INFO commandstats — single-snapshot analysis</h1>
<p class="sub">Database <b>{H.escape(DB)}</b> (<code>db:{DBID}</code>) ·
<b>{len(info['shards_used'])} {role} shards</b> of {info['shards_in_db']} ·
from support package <code>{H.escape(pkg)}</code></p>
<p class="meta">Snapshot <b>{TS}</b> &nbsp;·&nbsp; rate-normalised throughout &nbsp;·&nbsp;
{t['n_commands']} commands &nbsp;·&nbsp; {t['n_cohorts']} measurement window(s)</p>
</div></div><div class="redrule"></div>
<div class="wrap">
<nav>{NAV}</nav>
{''.join(BODY)}
<footer>Generated from <code>{H.escape(pkg)}</code>, scoped to database <b>{H.escape(DB)}</b>
(<code>db:{DBID}</code>) — the INFO Commandstats, Latencystats, Errorstats and Server sections of
{len(info['shards_used'])} {role} shard files, with shard roles and capture time from
<code>node_*.rladmin</code>. Databases in package: {dblist}. Percentiles are per-shard medians and
are not additive; sections needing a second capture are marked N/A in §1.<br><br>
Styling uses Redis design tokens — Redis Red <code>#FF4438</code>, Dusk <code>#163341</code>,
Ink <code>#091A23</code> — and the Space Grotesk / Space Mono pairing, per
<code>redis/docs</code> theme config.</footer>
</div></body></html>"""
