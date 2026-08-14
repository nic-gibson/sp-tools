"""Redis design tokens and matplotlib styling.

Colour values and the type pairing come from Redis's own theme configuration
(redis/docs, tailwind.config.js): redis-red-500 #FF4438, redis-pen-800 "Dusk"
#163341, redis-ink-900 #091A23, redis-yellow-500 #DCFF1E, redis-indigo-500
#5961FF, the redis-pen grey ramp, and Space Grotesk / Space Mono.

Note the aggregator sites still publish Redis's pre-2024 palette (Poppy
#D82C20 and friends); those values are stale and should not be used.

Chart layout is computed with DejaVu Sans while the SVG declares Space Grotesk,
because the webfont cannot be installed locally. DejaVu is the wider face, so
rendered text under-fills the space reserved for it -- labels gain air rather
than colliding, which is the safe direction for a substitution we cannot verify
at render time.
"""
import io

import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter, LogLocator  # noqa: F401  (re-exported)

RED500, RED600, RED700 = '#FF4438', '#D52D1F', '#E4291E'
YEL500, YEL300, YEL100 = '#DCFF1E', '#EDFF8E', '#FBFFE8'
IND500, IND600 = '#5961FF', '#454CD5'
PEN200, PEN300, PEN400 = '#E8EBEC', '#B9C2C6', '#8A99A0'
PEN600, PEN700, PEN800 = '#5C707A', '#2D4754', '#163341'
PEN900, INK900, NEU200 = '#8A221C', '#091A23', '#F9F9F9'

P = dict(
    surface='#FFFFFF', plane=NEU200,
    ink=INK900, ink2=PEN700, muted=PEN600,
    grid=PEN200, axis=PEN300,
    # Dusk leads so Redis Red stays available for emphasis and polarity rather
    # than becoming the default series colour. Every entry clears 3:1 on white.
    cat=[PEN800, RED500, IND500, PEN600, PEN900, PEN700, RED600, PEN400],
    div_lo=IND500, div_hi=RED500, div_mid=PEN300,
    red=RED500, red_dark=RED600, dusk=PEN800, yellow=YEL500, indigo=IND500,
    deemph=PEN300,
)

CMAP = LinearSegmentedColormap.from_list('redis_dusk',
                                         ['#FFFFFF', PEN200, PEN400, PEN700, INK900])

FONT_SANS = ['Space Grotesk', 'DejaVu Sans', 'Helvetica', 'Arial', 'sans-serif']
FONT_MONO = ['Space Mono', 'DejaVu Sans Mono', 'monospace']

mpl.rcParams.update({
    'svg.fonttype': 'none',
    'figure.facecolor': P['surface'], 'axes.facecolor': P['surface'],
    'savefig.facecolor': P['surface'],
    'font.family': 'sans-serif', 'font.sans-serif': FONT_SANS, 'font.monospace': FONT_MONO,
    'font.size': 9.5, 'axes.titlesize': 10,
    'text.color': P['ink'], 'axes.labelcolor': P['ink2'],
    'xtick.color': P['ink2'], 'ytick.color': P['ink2'],
    'axes.edgecolor': P['axis'], 'axes.linewidth': 0.8,
    'mathtext.fontset': 'dejavusans',
})


def si(v, _p=None):
    v = float(v); a = abs(v)
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if a >= div:
            return f'{v/div:,.2f}'.rstrip('0').rstrip('.') + suf
    return f'{v:,.0f}' if a >= 10 else f'{v:,.2f}'.rstrip('0').rstrip('.')


def dur(us, _p=None):
    us = float(us)
    if us < 1000:
        return f'{us:,.1f}µs'
    if us < 1e6:
        return f'{us/1e3:,.1f}ms'
    return f'{us/1e6:,.2f}s'


def hours(s):
    h = float(s) / 3600
    return f'{h:,.1f}h' if h < 48 else f'{h/24:,.1f}d'


SI = FuncFormatter(si)
DUR = FuncFormatter(dur)
PCT = FuncFormatter(lambda v, _p: f'{v:,.0f}%')
INTF = FuncFormatter(lambda v, _p: f'{v:,.0f}')


def style_axes(ax, xgrid=True, ygrid=False):
    for sp in ('top', 'right'):
        ax.spines[sp].set_visible(False)
    ax.grid(bool(xgrid), axis='x', **({'color': P['grid'], 'lw': 0.7} if xgrid else {}))
    ax.grid(bool(ygrid), axis='y', **({'color': P['grid'], 'lw': 0.7} if ygrid else {}))
    ax.set_axisbelow(True)
    ax.tick_params(length=3, width=0.7)


def title(ax, head, sub=None):
    ax.set_title(head, loc='left', color=P['ink'], fontweight='bold', pad=14 if sub else 8)
    if sub:
        ax.text(0, 1.012, sub, transform=ax.transAxes, ha='left', va='bottom',
                fontsize=8.4, color=P['muted'])


def figtitle(fig, head, sub=None, y=0.985):
    fig.text(0.008, y, head, ha='left', va='top', fontsize=13.5,
             fontweight='bold', color=P['ink'])
    if sub:
        fig.text(0.008, y - 0.038, sub, ha='left', va='top', fontsize=8.8, color=P['muted'])


def note(fig, text, y=0.006):
    fig.text(0.008, y, text, ha='left', va='bottom', fontsize=7.8, color=P['muted'])


def on_color(hx):
    """Ink or white for text sitting on `hx`, whichever has more contrast."""
    c = [int(hx.lstrip('#')[i:i+2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    L = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    return '#FFFFFF' if (1.05 / (L + 0.05)) >= ((L + 0.05) / 0.0614) else INK900


def svg(fig):
    buf = io.StringIO()
    fig.savefig(buf, format='svg', bbox_inches='tight')
    plt.close(fig)
    s = buf.getvalue()
    return s[s.index('<svg'):]
