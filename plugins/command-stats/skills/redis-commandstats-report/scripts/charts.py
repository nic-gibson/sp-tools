"""Figures for the single-snapshot report. Every label is derived from the data,
so the same code describes a 4-shard database and a 200-shard one correctly."""
from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import LogLocator, FuncFormatter

from brand import (P, CMAP, SI, DUR, INTF, si, dur, hours, style_axes, title,
                   figtitle, note, svg, on_color)

TOPN = 20


def _sh(n):
    return f"{n} shard{'s' if n != 1 else ''}"


# --------------------------------------------------------------- shard windows
def windows(D):
    ps, t = D['per_shard'], D['totals']
    fig, axes = plt.subplots(1, 2, figsize=(13.2, max(4.0, 0.30 * len(ps) + 2.4)))
    for ax, (col, lab, colour, head, sub) in zip(axes, [
            ('uptime_h', 'uptime at capture (hours)', P['cat'][0], 'Counter window per shard',
             "each shard's counters cover only its own uptime"),
            ('rate', 'lifetime mean calls/sec', P['cat'][3], 'Throughput per shard',
             'counts ÷ uptime — comparable across shards')]):
        g = ps.sort_values(col)
        y = np.arange(len(g))
        ax.barh(y, g[col], height=0.62, color=colour, linewidth=0)
        ax.set_yticks(y)
        ax.set_yticklabels([f'redis:{s}' for s in g['shard']], fontsize=8)
        ax.set_xlabel(lab)
        style_axes(ax)
        fmt = (lambda v: f'{v:,.0f}h') if col == 'uptime_h' else (lambda v: f'{v:,.0f}')
        for i, v in enumerate(g[col]):
            ax.text(v * 1.02, y[i], fmt(v), va='center', ha='left', fontsize=7.4, color=P['ink2'])
        ax.set_xlim(0, g[col].max() * 1.2)
        title(ax, head, sub)
    figtitle(fig, 'Why rates are required, not optional',
             f"Uptimes span {hours(t['uptime_min'])} to {hours(t['uptime_max'])} "
             f"({t['uptime_spread']:,.1f}× spread), so raw counters are not comparable "
             f"shard to shard; dividing by uptime removes the distortion.")
    note(fig, f"Source: INFO Server uptime_in_seconds and INFO Commandstats, "
              f"{_sh(t['n_shards'])} ({D['info']['role_filter']}).")
    fig.tight_layout(rect=(0, 0.03, 1, 0.88))
    return svg(fig)


# ----------------------------------------------------------------- cohorts
def cohorts(D):
    coh, order, t = D['cohort'], D['cohort_order'], D['totals']
    n = min(16, len(coh))
    per = coh.copy()
    keep = per.sum(axis=1).nlargest(n).index[::-1]
    y = np.arange(len(keep))
    hh = min(0.26, 0.78 / max(len(order), 1))
    fig, ax = plt.subplots(figsize=(12.4, max(4.5, 0.46 * len(keep) + 2.2)))
    floor = 1e-4
    for i, c in enumerate(order):
        off = ((len(order) - 1) / 2 - i) * (hh + 0.02)
        ax.barh(y + off, per.loc[keep, c].clip(lower=floor), height=hh,
                color=P['cat'][i % len(P['cat'])], linewidth=0, label=c)
    ax.set_yticks(y); ax.set_yticklabels(keep, fontsize=8.4)
    ax.set_xscale('log'); ax.xaxis.set_major_formatter(SI)
    ax.set_xlim(floor, max(per.values.max() * 6, floor * 10))
    ax.set_xlabel('calls/sec per shard, within the cohort (log)')
    style_axes(ax)
    ax.legend(loc='lower right', frameon=False, fontsize=8.4)
    figtitle(fig, f"The same database across {t['n_cohorts']} different measurement windows",
             'Shards grouped by uptime — shards that restarted together share a window, '
             'so the groups fall out of the gaps in the uptime list.')
    note(fig, 'Rates are per-shard means so cohorts of unequal size compare fairly. '
              'Bars at the axis floor are effectively zero.')
    fig.tight_layout(rect=(0, 0.022, 1, 0.90))
    return svg(fig)


# ------------------------------------------------------------------- mix / cost
def _ranked(ax, s, colour, xlabel, fmt=si, label_top=8):
    s = s.sort_values(ascending=True)
    y = np.arange(len(s))
    pos = s[s > 0]
    lo = max(pos.min() * 0.5, 1e-9) if len(pos) else 1e-9
    ax.barh(y, s.clip(lower=lo * 1.001), height=0.62, color=colour, linewidth=0)
    ax.set_yticks(y); ax.set_yticklabels(s.index, fontsize=8)
    ax.set_xscale('log'); ax.set_xlim(lo, s.max() * 4)
    ax.xaxis.set_major_locator(LogLocator(base=10, numticks=12))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, p: fmt(v)))
    ax.set_xlabel(xlabel)
    style_axes(ax)
    for i in range(len(s))[-label_top:]:
        ax.text(s.values[i] * 1.25, y[i], fmt(s.values[i]), va='center', ha='left',
                fontsize=7.8, color=P['ink2'])


def mix(D):
    pc, t = D['per_cmd'], D['totals']
    n = min(TOPN, len(pc))
    fig, axes = plt.subplots(1, 2, figsize=(13.2, max(4.5, 0.36 * n + 2.6)))
    _ranked(axes[0], pc.nlargest(n, 'rate')['rate'], P['cat'][0], 'calls/sec (log)')
    title(axes[0], 'Throughput', f"top {n} of {t['n_commands']} commands")
    _ranked(axes[1], pc.nlargest(n, 'cpu_frac')['cpu_frac'] * 1000, P['red'],
            'CPU milli-cores (log)', fmt=lambda v, p=None: f'{v:,.2f}m')
    title(axes[1], 'CPU cost', f'top {n} by CPU time ÷ uptime')
    figtitle(fig, 'What the database runs, and what it spends its time on',
             "Rate-normalised so every shard's contribution covers a comparable window. "
             'Log scale, ranked independently per panel.')
    note(fig, f"1 milli-core = 0.1% of one CPU core, summed across {_sh(t['n_shards'])}.")
    fig.tight_layout(rect=(0, 0.022, 1, 0.90))
    return svg(fig)


def shares(D):
    pc = D['per_cmd']
    n = min(18, len(pc))
    top = pc.nlargest(n, 'rate').index
    a, b = pc.loc[top, 'rate_share'] * 100, pc.loc[top, 'cpu_share'] * 100
    order = a.sort_values().index
    y = np.arange(len(order)); hh = 0.36
    fig, ax = plt.subplots(figsize=(11.8, max(4.2, 0.40 * n + 2.0)))
    ax.barh(y + hh/2 + 0.02, a.reindex(order), height=hh, color=P['cat'][0],
            linewidth=0, label='share of calls')
    ax.barh(y - hh/2 - 0.02, b.reindex(order), height=hh, color=P['red'],
            linewidth=0, label='share of CPU time')
    ax.set_yticks(y); ax.set_yticklabels(order, fontsize=8)
    ax.set_xlabel('% of database total')
    style_axes(ax); ax.legend(loc='lower right', frameon=False)
    for i, c in enumerate(order):
        for val, off in ((a[c], hh/2 + 0.02), (b[c], -hh/2 - 0.02)):
            if val >= 1.2:
                ax.text(val + 0.6, y[i] + off, f'{val:,.1f}%', va='center', ha='left',
                        fontsize=7.4, color=P['ink2'])
    figtitle(fig, 'Call share vs CPU share — the commands you run most are not the ones you pay for',
             f'Both are uptime-invariant, so they are valid on a single capture. Top {n} commands '
             'by call share.')
    fig.tight_layout(rect=(0, 0.01, 1, 0.91))
    return svg(fig)


# ---------------------------------------------------------------- concentration
def concentration(D):
    pc, c = D['per_cmd'], D['conc']
    s = pc['rate'].sort_values(ascending=False).values
    rank = np.arange(1, len(s) + 1)
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.8))
    ax = axes[0]
    ax.plot(rank, s, color=P['cat'][0], lw=1.6)
    ax.scatter(rank, s, s=14, color=P['cat'][0], zorder=3)
    ax.set_xscale('log'); ax.set_yscale('log')
    ax.xaxis.set_major_formatter(INTF); ax.yaxis.set_major_formatter(SI)
    ax.set_xlabel('command rank (log)'); ax.set_ylabel('calls/sec (log)')
    style_axes(ax, True, True)
    title(ax, 'Rank–frequency', 'steeper = more concentrated workload')
    ax = axes[1]
    cum = np.cumsum(s) / s.sum() * 100
    ax.plot(rank, cum, color=P['cat'][0], lw=1.8)
    ax.scatter(rank, cum, s=14, color=P['cat'][0], zorder=3)
    ax.set_xscale('log'); ax.xaxis.set_major_formatter(INTF)
    ax.set_ylim(0, 104)
    ax.set_xlabel('command rank (log)'); ax.set_ylabel('cumulative % of all calls')
    for lvl, key in ((50, 'n_for_50'), (90, 'n_for_90'), (99, 'n_for_99')):
        ax.axhline(lvl, color=P['axis'], lw=0.8, ls=(0, (4, 3)))
        ax.text(1.05, lvl + 1.4, f'{lvl}% of calls = top {c[key]} commands',
                fontsize=8, color=P['muted'])
    style_axes(ax, True, True)
    title(ax, 'Cumulative concentration', 'top n commands as a share of all calls')
    figtitle(fig, 'How concentrated is the workload?',
             f"Rate-normalised call counts across {_sh(D['totals']['n_shards'])}.")
    fig.tight_layout(rect=(0, 0.01, 1, 0.88))
    return svg(fig)


def bins(D):
    pc = D['per_cmd']
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.2))
    tables = []

    def binhist(ax, s, lo, hi, xlabel, colour):
        s = s.dropna(); s = s[s > 0]
        edges = 10.0 ** np.arange(lo, hi + 1)
        labels = [f'$10^{{{int(np.log10(a))}}}$–$10^{{{int(np.log10(b))}}}$'
                  for a, b in zip(edges[:-1], edges[1:])]
        counts = [int(((s >= a) & (s < b)).sum()) for a, b in zip(edges[:-1], edges[1:])]
        x = np.arange(len(labels))
        ax.bar(x, counts, width=0.66, color=colour, linewidth=0)
        for xi, v in zip(x, counts):
            if v:
                ax.text(xi, v + max(counts) * 0.02, str(v), ha='center', va='bottom',
                        fontsize=8, color=P['ink2'])
        ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=8)
        ax.set_xlabel(xlabel); ax.set_ylabel('number of commands')
        ax.set_ylim(0, max(counts) * 1.24)
        style_axes(ax, False, True)
        tables.append((counts, labels))

    r = pc['rate'][pc['rate'] > 0]
    binhist(axes[0], pc['rate'], int(np.floor(np.log10(r.min()))),
            int(np.ceil(np.log10(r.max()))), 'calls/sec (log bins)', P['cat'][0])
    title(axes[0], 'By throughput', 'how many commands are hot, warm, rare')
    a = pc['avg_us'][pc['avg_us'] > 0]
    binhist(axes[1], pc['avg_us'], int(np.floor(np.log10(a.min()))),
            int(np.ceil(np.log10(a.max()))), 'mean µs per call (log bins)', P['red'])
    title(axes[1], 'By cost per call', 'the latency profile of the command set')
    figtitle(fig, 'Distribution over magnitude bins',
             'The literal histogram: commands binned by order of magnitude.')
    fig.tight_layout(rect=(0, 0.01, 1, 0.87))
    return svg(fig), tables


def part_to_whole(D):
    pc = D['per_cmd']
    k = min(6, max(1, len(pc) - 1))
    fig, axes = plt.subplots(2, 1, figsize=(12.4, 4.4))
    for ax, (col, lbl) in zip(axes, [('rate_share', 'share of calls'),
                                     ('cpu_share', 'share of CPU time')]):
        top = pc.nlargest(k, col)
        vals = list(top[col] * 100) + [max(0.0, 100 - top[col].sum() * 100)]
        names = list(top.index) + ['Other (folded tail)']
        fills = list(P['cat'][:k]) + [P['deemph']]
        left = 0.0
        for v, nm, f in zip(vals, names, fills):
            ax.barh([0], [max(v - 0.25, 0)], left=[left], height=0.5, color=f, linewidth=0)
            if v >= 5:
                ax.text(left + v / 2, 0, f'{nm}\n{v:,.0f}%', ha='center', va='center',
                        fontsize=7.6, color=on_color(f), fontweight='bold')
            left += v
        ax.set_xlim(0, 100); ax.set_ylim(-0.45, 0.45); ax.set_yticks([])
        ax.set_xlabel(f'% of database total — {lbl}')
        for sp in ('top', 'right', 'left'):
            ax.spines[sp].set_visible(False)
        ax.grid(False)
    figtitle(fig, 'Part-to-whole: what the workload is made of',
             f'Top {k} commands plus a folded tail. Calls above, CPU time below.', y=1.06)
    fig.tight_layout(rect=(0, 0.01, 1, 0.86))
    return svg(fig)


# ------------------------------------------------------------------ percentiles
def percentiles(D):
    pc = D['per_cmd'].dropna(subset=['p50_med', 'p99_med'])
    if pc.empty:
        return None
    n = min(TOPN, len(pc))
    g = pc.nlargest(n, 'cpu_frac').sort_values('p99_med')
    y = np.arange(len(g))
    fig, axes = plt.subplots(1, 2, figsize=(13.2, max(4.5, 0.36 * n + 2.6)))
    ax = axes[0]
    ax.hlines(y, g['p50_med'], g['p999_med'], color=P['axis'], lw=1.3, zorder=1)
    for col, colour, lab in (('p50_med', P['cat'][5], 'p50'),
                             ('p99_med', P['cat'][3], 'p99'),
                             ('p999_med', P['cat'][0], 'p99.9')):
        ax.scatter(g[col], y, s=48, color=colour, zorder=3,
                   edgecolors=P['surface'], linewidths=1.5, label=lab)
    ax.scatter(g['avg_us'], y, s=44, marker='D', color=P['red'], zorder=5,
               edgecolors=P['surface'], linewidths=1.4, label='mean (usec/calls)')
    ax.set_yticks(y); ax.set_yticklabels(g.index, fontsize=8)
    ax.set_xscale('log'); ax.xaxis.set_major_formatter(DUR)
    ax.set_xlabel('time per call (log) — median across shards')
    style_axes(ax); ax.legend(loc='lower right', frameon=False, fontsize=8)
    title(ax, 'Mean vs the actual distribution', 'what commandstats alone cannot show')
    ax = axes[1]
    r = g['p99_med'] / g['avg_us']
    cols = [P['div_hi'] if v > 3 else (P['div_lo'] if v < 1 else P['div_mid']) for v in r]
    ax.barh(y, r.values, height=0.62, color=cols, linewidth=0)
    ax.axvline(1, color=P['axis'], lw=0.9)
    ax.set_yticks(y); ax.set_yticklabels([])
    ax.set_xscale('log')
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, p: f'{v:,.3g}×'))
    ax.set_xlabel('p99 ÷ mean (log) — tail spread')
    style_axes(ax)
    for i, v in enumerate(r.values):
        ax.text(v * 1.1, y[i], f'{v:,.1f}×', va='center', ha='left', fontsize=7.8, color=P['ink2'])
    ax.set_xlim(r.min() * 0.5, r.max() * 4)
    title(ax, 'Tail vs mean',
          '>1 = mean hides a slower tail; <1 = a few outliers inflate the mean')
    figtitle(fig, 'Real percentiles from INFO Latencystats',
             f'Top {n} commands by CPU cost. Percentiles are per-shard and cannot be summed, '
             f"so the median across {_sh(D['totals']['n_shards'])} is shown.")
    note(fig, 'Diamond = usec/calls from Commandstats. The gap between it and p50 is the '
              "distribution's skew.")
    fig.tight_layout(rect=(0, 0.022, 1, 0.90))
    return svg(fig)


# --------------------------------------------------------------------- scatter
def scatter(D):
    pc = D['per_cmd']
    g = pc[(pc['rate'] > 0) & (pc['avg_us'] > 0)]
    fig, ax = plt.subplots(figsize=(11.6, 7.8))
    sz = 26 + 900 * (g['cpu_frac'] / g['cpu_frac'].max()) ** 0.42
    ax.scatter(g['rate'], g['avg_us'], s=sz, color=P['cat'][0], alpha=0.66,
               linewidths=1.5, edgecolors=P['surface'], zorder=3)
    ax.set_xscale('log'); ax.set_yscale('log')
    x0, x1 = g['rate'].min() * 0.3, g['rate'].max() * 5
    y0, y1 = g['avg_us'].min() * 0.3, g['avg_us'].max() * 5
    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
    ax.xaxis.set_major_formatter(SI); ax.yaxis.set_major_formatter(DUR)
    ax.set_xlabel('throughput, calls/sec (log)'); ax.set_ylabel('mean time per call (log)')
    style_axes(ax, True, True)
    for frac, lbl in ((1e-6, '0.0001% of a core'), (1e-4, '0.01% of a core'),
                      (1e-2, '1% of a core'), (1.0, 'one full core')):
        xs = np.array([x0, x1])
        ax.plot(xs, frac * 1e6 / xs, color=P['axis'], lw=0.8, zorder=1)
        xa = min(x1 * 0.86, max(x0, frac * 1e6 / (y1 * 0.72)))
        ya = frac * 1e6 / xa
        if y0 < ya < y1:
            ax.text(xa, ya * 1.12, lbl, fontsize=7.4, color=P['muted'], ha='right')
    lab = (set(pc.nlargest(min(9, len(pc)), 'cpu_frac').index)
           | set(pc.nlargest(min(7, len(pc)), 'rate').index)
           | set(pc.nlargest(min(3, len(pc)), 'avg_us').index))
    for c in lab:
        if c in g.index:
            ax.annotate(c, (g.loc[c, 'rate'], g.loc[c, 'avg_us']), textcoords='offset points',
                        xytext=(9, 5), fontsize=8, color=P['ink'])
    figtitle(fig, 'Throughput against cost per call',
             'Marker area = total CPU time. Diagonals are iso-cost lines: every point on one '
             'consumes the same CPU.')
    note(fig, 'Distance out along the diagonals, not height on either axis alone, is what '
              'makes a command expensive.')
    fig.tight_layout(rect=(0, 0.022, 1, 0.90))
    return svg(fig)


# ------------------------------------------------------------------ shard skew
def shard_skew(D, top=8):
    pc, cmd = D['per_cmd'], D['cmd']
    keep = list(pc.nlargest(min(top, len(pc)), 'rate').index)
    piv = cmd[cmd.command.isin(keep)].pivot_table(index='command', columns='shard',
                                                  values='rate', aggfunc='sum').reindex(keep)
    fig, ax = plt.subplots(figsize=(max(9.0, 0.72 * piv.shape[1] + 2.2),
                                    max(3.4, 0.44 * piv.shape[0] + 1.9)))
    data = np.log10(piv.fillna(0).values + 1e-3)
    im = ax.imshow(data, aspect='auto', cmap=CMAP)
    ax.set_xticks(range(piv.shape[1])); ax.set_xticklabels(piv.columns, fontsize=8)
    ax.set_yticks(range(piv.shape[0])); ax.set_yticklabels(piv.index, fontsize=8.4)
    ax.set_xlabel('shard (redis:n)')
    raw, vmax = piv.values, np.nanmax(data)
    for i in range(raw.shape[0]):
        for j in range(raw.shape[1]):
            v = raw[i, j]
            lbl = '—' if (not np.isfinite(v) or v <= 0) else si(v)
            ax.text(j, i, lbl, ha='center', va='center', fontsize=6.6,
                    color='white' if data[i, j] > vmax - 0.8 else P['ink2'])
    ax.grid(False)
    cb = fig.colorbar(im, ax=ax, pad=0.012)
    cb.set_label('log₁₀ calls/sec', fontsize=8)
    cb.outline.set_visible(False)
    figtitle(fig, f'Load skew across shards — top {len(keep)} commands',
             'Rate per shard. Dashes are commands the shard never served.', y=1.02)
    fig.tight_layout(rect=(0, 0.01, 1, 0.87))
    return svg(fig), piv


def full_histogram(D):
    pc, t = D['per_cmd'], D['totals']
    g = pc.sort_values('rate', ascending=False)
    fig, ax = plt.subplots(figsize=(9.6, max(4.0, 0.235 * len(g) + 1.8)))
    yy = np.arange(len(g))[::-1]
    floor = 10 ** np.floor(np.log10(max(g['rate'][g['rate'] > 0].min(), 1e-9)))
    ax.barh(yy, g['rate'].clip(lower=floor), height=0.7, color=P['cat'][0], linewidth=0)
    ax.set_yticks(yy); ax.set_yticklabels(g.index, fontsize=7.6)
    ax.set_xscale('log'); ax.xaxis.set_major_formatter(SI)
    ax.set_xlim(floor, g['rate'].max() * 4)
    ax.set_xlabel('calls/sec (log)')
    style_axes(ax)
    for yv, v, n in zip(yy, g['rate'], g['shards']):
        ax.text(v * 1.3, yv, f"{si(v)}  ({n}/{t['n_shards']} shards)", va='center',
                ha='left', fontsize=6.9, color=P['muted'])
    figtitle(fig, f"Command frequency histogram — all {t['n_commands']} commands",
             'Rate-normalised. Shard count shows how many shards ran the command at all.',
             y=0.995)
    fig.tight_layout(rect=(0, 0.004, 1, 0.955))
    return svg(fig)
