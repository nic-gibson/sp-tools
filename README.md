# sp-tools

A Claude plugin marketplace holding diagnostic skills for Redis Enterprise
support packages. Add the marketplace once and then receive updates by
refreshing it, rather than being sent a new file each time something changes.

## For users: install

```
/plugin marketplace add nic-gibson/sp-tools
/plugin install command-stats@sp-tools
```

If the install summary says `Run /reload-plugins to activate.`, run that.

Then just ask for what you want — the skill triggers on its own:

> What's the activity-db database doing in ~/Downloads/debuginfo.ABC123.tar.gz?

> Which commands are eating CPU in this support package?

> Can I compare these two debuginfo bundles?

To invoke it explicitly, plugin skills are namespaced by plugin name:

```
/command-stats:redis-commandstats-report
```

### Getting updates

```
/plugin marketplace update sp-tools
```

Updates are gated on the `version` field, so a user only receives a change once
the version is bumped. See *Releasing a change* below.

### If you already installed the standalone skill

Anyone who installed `redis-commandstats-report.skill` directly should remove
that copy after installing the plugin, or two copies of the same skill will be
offered and only one of them will receive updates.

## What's in it

| Plugin | Skill | What it does |
| --- | --- | --- |
| `command-stats` | `redis-commandstats-report` | Builds a Redis-branded HTML report of `INFO commandstats` for one named database in a debuginfo package, and screens package pairs for counter resets |

One script is the entry point, and the skill drives it:

```bash
# what databases are in this package?
python3 scripts/extract.py --package debuginfo.XXX.tar.gz --list

# the report — a single self-contained HTML file
python3 scripts/extract.py \
    --package debuginfo.XXX.tar.gz --database pers-3950 --html /tmp/report.html

# can these two packages legitimately be differenced?
python3 scripts/extract.py --pair A.tar.gz B.tar.gz --database pers-3950
```

**No dependencies** — Python 3.7+ standard library only. The report is one HTML
document with inline SVG figures and no network resources, meant to be opened as
an artifact inside Claude but equally usable as a file.

## For maintainers

### Layout

```
sp-tools/
├── .claude-plugin/
│   └── marketplace.json          the catalogue: name, owner, plugin list
├── plugins/
│   └── command-stats/
│       ├── .claude-plugin/
│       │   └── plugin.json        the plugin manifest
│       ├── skills/
│       │   └── redis-commandstats-report/
│       │       ├── SKILL.md       frontmatter (name, description) + instructions
│       │       ├── assets/        the HTML report template the script fills in
│       │       ├── references/    docs loaded on demand
│       │       └── scripts/       executables the skill calls
│       └── tests/                 end-to-end suite (bash tests/run.sh)
└── README.md
```

Skills are discovered by convention — a directory under `skills/` containing a
`SKILL.md` is picked up with no registration entry needed. The `description` in
that frontmatter is what decides whether Claude reaches for the skill, so it is
the highest-leverage text in the repo.

Plugin `source` paths in `marketplace.json` resolve from the marketplace root
(the directory containing `.claude-plugin/`), not from inside it. `metadata.pluginRoot`
is set to `./plugins` so entries could also be written as `"source": "command-stats"`.

### First-time setup

```bash
cd sp-tools
git init -b main
git add -A
git commit -m "sp-tools 1.0.0"
git remote add origin git@github.com:nic-gibson/sp-tools.git
git push -u origin main
```

Test locally before pushing — a local directory works as a marketplace source:

```
/plugin marketplace add ./sp-tools
/plugin install command-stats@sp-tools
```

Validate the manifests with:

```bash
claude plugin validate ./plugins/command-stats
```

### Releasing a change

Users are pinned to the `version` string, so a push alone changes nothing for
them. Bump the version in **both** places — they must agree:

- `plugins/command-stats/.claude-plugin/plugin.json`
- the `command-stats` entry in `.claude-plugin/marketplace.json`

Then commit, push, and tell colleagues to run
`/plugin marketplace update sp-tools`.

### Adding another skill

Drop a new directory under `plugins/command-stats/skills/`, with its own
`SKILL.md`, and bump the plugin version. Nothing else needs editing — the
marketplace entry doesn't enumerate skills.

For a genuinely separate tool, add a sibling plugin under `plugins/` with its own
`.claude-plugin/plugin.json` and a new entry in the `plugins` array. One
marketplace can carry many plugins, and users install them individually.

### Distributing through org settings

On a Team or Enterprise plan this repo can be pushed to everyone via
**Organization settings → Plugins**, which removes the need for each person to
run `/plugin marketplace add`. Two constraints apply in that mode: the
marketplace repository must be private or internal, and each plugin source must
be a relative path within this repo or a `github` / `url` / `git-subdir` source.
The relative path used here satisfies that, so private plugin code never needs a
separate repository.
