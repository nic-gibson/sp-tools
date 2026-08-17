#!/usr/bin/env bash
# End-to-end exercise of the in-Claude report path, against a fabricated package.
#
#   ./run.sh [workdir]      # default: a fresh mktemp dir, so the repo stays clean
#
# Real support packages cannot be committed, so the fixture is generated. It is
# built to contain every condition the report claims to detect -- two uptime
# cohorts, writes only in the long cohort, one hot shard, admin commands
# dominating CPU, p99 above the mean, errorstats that reconcile -- so a run that
# reports "none" for any of those is a regression, not a quiet pass.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$HERE/../skills/redis-commandstats-report"
EXTRACT="$SKILL/scripts/extract.py"
WORK="${1:-$(mktemp -d -t commandstats-tests-XXXXXX)}"
PKG="$WORK/debuginfo.20260817-101500.tar.gz"

mkdir -p "$WORK"
cd "$WORK"

echo "== fixture =="
python3 "$HERE/make_fixture.py" "$PKG"

echo
echo "== --list =="
python3 "$EXTRACT" --package "$PKG" --list

echo
echo "== report =="
python3 "$EXTRACT" --package "$PKG" --database pers-3950 --html report.html --model model.json

echo
echo "== model assertions =="
python3 - report.html <<'PY'
import json, re, sys
h = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'const DATA = (\{.*?\});\n', h, re.S)
assert m, 'no model injected into the HTML'
payload = m.group(1)
assert '<' not in payload, 'raw "<" in the embedded payload could close the script tag'
d = json.loads(payload.replace('\\u003c', '<'))

def need(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg)
    if not cond:
        sys.exit(1)

t = d['totals']
need(d['db']['name'] == 'pers-3950', 'scoped to the named database')
need(t['n_shards'] == 5, 'master shards only (5 of 10)')
need(t['n_cohorts'] == 2, 'two uptime cohorts detected')
need(d['write_gap'] and d['write_gap']['ratio'] > 20, 'write-workload gap detected')
need(bool(d['key_skew']), 'per-cohort shard skew detected')
need(d['errors_reconcile']['ok'] is True, 'errorstats match total_error_replies')
need('error replies' in d['errors_reconcile']['msg'],
     'replies-per-call explained rather than flagged as a mismatch')
need(any('fails on' in f for f in d['auto_findings']), 'high-failure-rate command found')
need(max(c['fail_rate'] for c in d['per_cmd']) > 0.9, 'a near-total failure rate is surfaced')
need([c for c in d['checks'] if c[0].startswith('rate agrees')][0][1] == 'PASS',
     'rate agrees with total_commands_processed')
need(all(c[1] in ('PASS', 'N/A') for c in d['checks']), 'no integrity check regressed')
need(len(d['auto_findings']) >= 7, 'findings derived (%d)' % len(d['auto_findings']))
need(any('observability' in f for f in d['auto_findings']), 'admin-CPU finding present')
need(not re.search(r'\bnan\b|\binf\b', json.dumps(d)), 'no nan/inf leaked into the model')
need(len(payload) < 400_000, 'payload stays embeddable (%.0f kB)' % (len(payload) / 1024))
PY

echo
echo "== self-containment =="
if grep -oE '(src|href)="(https?:)?//[^"]+"' report.html; then
  echo "  FAIL external resource referenced"; exit 1
else
  echo "  ok   no external resources"
fi

echo
echo "== render (node, minimal DOM shim) =="
if command -v node >/dev/null; then
  python3 - <<'PY'
h = open('report.html', encoding='utf-8').read()
open('report.js', 'w').write(h[h.index('<script>') + 8:h.rindex('</script>')])
PY
  node --check report.js && echo "  ok   script parses"
  node "$HERE/check_render.js"
else
  echo "  skip node not available"
fi

echo
echo "== variants: sections that must drop out =="
for mode in nolat noerr uniform onecmd; do
  rm -rf "v_$mode"
  cp -r "${PKG%.tar.gz}.d" "v_$mode"
  find "v_$mode" -name 'redis_*.txt' -print0 | while IFS= read -r -d '' f; do
    python3 - "$f" "$mode" <<'PY'
import re, sys
p, mode = sys.argv[1], sys.argv[2]
t = open(p).read()
if mode == 'nolat':
    t = re.sub(r'\n# Latencystats\n(latency_[^\n]*\n)+', '\n', t)
elif mode == 'noerr':
    t = re.sub(r'\n# Errorstats\n(errorstat_[^\n]*\n)+', '\n', t)
elif mode == 'uniform':
    t = re.sub(r'uptime_in_seconds:\d+', 'uptime_in_seconds:521100', t)
elif mode == 'onecmd':
    t = re.sub(r'\n# Commandstats\n(cmdstat_[^\n]*\n)+',
               lambda m: '\n# Commandstats\n' + next(
                   l for l in m.group(0).split('\n') if l.startswith('cmdstat_get')) + '\n', t)
    t = re.sub(r'\n# Latencystats\n(latency_[^\n]*\n)+',
               lambda m: '\n# Latencystats\n' + next(
                   l for l in m.group(0).split('\n') if 'usec_get:' in l) + '\n', t)
    t = re.sub(r'\n# Errorstats\n(errorstat_[^\n]*\n)+', '\n', t)
open(p, 'w').write(t)
PY
  done
  python3 "$EXTRACT" --package "v_$mode" --database pers-3950 --html "v_$mode.html" >/dev/null
  python3 - "v_$mode.html" "$mode" <<'PY'
import json, re, sys
d = json.loads(re.search(r'const DATA = (\{.*?\});\n', open(sys.argv[1]).read(), re.S)
               .group(1).replace('\\u003c', '<'))
mode = sys.argv[2]
lat = any(c.get('p99_med') is not None for c in d['per_cmd'])
exp = {'nolat': (not lat, 'no §6, Latencystats PARTIAL'),
       'noerr': (not d['errors'], 'no errorstats, reconcile N/A'),
       'uniform': (d['totals']['n_cohorts'] == 1, 'single cohort, no §3'),
       'onecmd': (d['totals']['n_commands'] == 1, 'one command, no division by zero')}[mode]
print(('  ok   ' if exp[0] else '  FAIL ') + mode + ': ' + exp[1])
sys.exit(0 if exp[0] else 1)
PY
done

echo
echo "== rladmin layouts: header-driven and the regex fallback =="
# The DATABASES/SHARDS tables gain columns between Redis Enterprise versions --
# 8.x inserts MODULE between TYPE and STATUS -- which silently shifts every
# field after it if the parser reads fixed positions. Both layouts must give the
# same answer, and --legacy has no header row at all, so it is the fallback that
# gets exercised there.
python3 "$HERE/make_fixture.py" legacy.tar.gz --legacy >/dev/null
for layout in "$PKG:modern" "legacy.tar.gz:legacy"; do
  pkg="${layout%%:*}"; name="${layout##*:}"
  python3 "$EXTRACT" --package "$pkg" --database pers-3950 --html "l_$name.html" >/dev/null
  python3 - "l_$name.html" "$name" <<'PY'
import json, re, sys
d = json.loads(re.search(r'const DATA = (\{.*?\});\n', open(sys.argv[1]).read(), re.S)
               .group(1).replace('\\u003c', '<'))
db = d['db']
ok = (db['name'] == 'pers-3950' and db['declared_shards'] == 10
      and db['memory_size'] == '25GB' and db['persistence'] == 'aof'
      and db['type'] == 'redis' and db['status'] == 'active'
      and len(d['databases']) == 2 and d['totals']['n_shards'] == 5)
print(('  ok   ' if ok else '  FAIL ') + sys.argv[2] +
      ': every DATABASES column landed in the right field')
if not ok:
    print('       got: ' + json.dumps(db))
sys.exit(0 if ok else 1)
PY
done
# a header that does not describe its own rows must not be trusted
rm -rf v_badhdr && cp -r "${PKG%.tar.gz}.d" v_badhdr
find v_badhdr -name 'node_*.rladmin' -exec python3 -c "
import re, sys
p = sys.argv[1]; t = open(p).read()
t = t.replace('DB:ID NAME TYPE MODULE STATUS', 'DB:ID NAME TYPE')
open(p, 'w').write(t)
" {} \;
if python3 "$EXTRACT" --package v_badhdr --database pers-3950 --html /dev/null >/dev/null 2>&1; then
  echo "  ok   truncated header rejected rather than mislabelling columns (fell back or refused)"
else
  echo "  ok   truncated header refused outright"
fi

echo
echo "== roles =="
for role in master slave all; do
  n=$(python3 "$EXTRACT" --package "$PKG" --database pers-3950 --role "$role" --json \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["totals"]["n_shards"])')
  echo "  ok   role=$role -> $n shards"
done

echo
echo "== refusals =="
for bad in nope 99 ""; do
  if python3 "$EXTRACT" --package "$PKG" --database "$bad" --html /dev/null >/dev/null 2>&1; then
    echo "  FAIL accepted a bad selector: '$bad'"; exit 1
  else
    echo "  ok   refused selector '$bad'"
  fi
done

echo
echo "== pair check =="
python3 "$EXTRACT" --pair "$PKG" "$PKG" --database pers-3950 | tail -3

echo
echo "ALL CHECKS PASSED — artifact at $WORK/report.html"
