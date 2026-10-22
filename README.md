# structure-report

See the shape of a codebase before you touch it: which files are big, how
they import each other, and — for the files that matter — what each
function is actually *for* and what order a big orchestrating function
really runs its work in.

Two scripts, no dependencies beyond the `claude` CLI for the optional
second one:

- **`structure_report.py`** — free, instant, deterministic. Walks the repo,
  computes per-file LOC and import fan-in/fan-out, extracts every
  function/class (at any nesting depth) with its line range and in-file
  calls, flags "orchestrator" candidates (a `run`/`main` entry point, or any
  function with enough nested helpers or raw size), and renders it all as
  one interactive `report.html` — a zoomable import graph plus a per-file
  size table. Also writes `snapshot.json` and diffs against the previous
  run, so "what changed structurally" is a real, reviewable thing.

- **`structure_summarize.py`** — optional, costs real `claude -p` calls
  (one per size-hotspot FILE, not per function), **never run
  automatically**. Reads each hotspot file and writes a one-sentence
  purpose for every function/class, plus — for each orchestrator — an
  ordered PROCESS breakdown in its own words (not tied to any
  framework-level step/pill names you might recognize; those are usually
  coarser than the real code). Results are cached by content hash in
  `summaries.json`, so a re-run only pays for what actually changed.

## Why not an existing dependency-graph tool?

`madge`, `pydeps`, `dependency-cruiser`, `pyan3`, `code2flow` all exist and
are fine tools — but they need a package registry reachable at install
time, and real-world import syntax (`import x`, `from x import y`,
`import {...} from "./x"`, workspace-package specifiers, tsconfig path
aliases) is simple enough to resolve with stdlib regex/AST, so this has
zero dependencies and works offline.

## Usage

```
python structure_report.py --root /path/to/repo
open /path/to/repo/.structure-report/report.html
```

Then, optionally (spends real LLM calls):

```
python structure_summarize.py --root /path/to/repo
python structure_report.py --root /path/to/repo   # refresh report.html with the new summaries
```

Or install it so the two scripts are plain commands anywhere:

```
pip install -e .
structure-report --root /path/to/repo
structure-summarize --root /path/to/repo
```

### Flags

`structure_report.py`:

| flag | default | meaning |
|---|---|---|
| `--root PATH` | cwd | repo to analyze |
| `--out DIR` | `<root>/.structure-report` | where `snapshot.json`/`report.html`/`summaries.json` live |
| `--hotspot-loc N` | 800 | "big file" threshold (informational highlighting, and the summarizer's scope) |
| `--orchestrator-min-nested N` | 5 | ≥N nested helpers makes a function an orchestrator candidate |
| `--orchestrator-min-loc N` | 150 | or ≥N lines of its own |
| `--graph-max-nodes N` | 500 | above this many tracked files, the rendered graph shows only hotspots + their direct neighbors (the file-by-size table always lists everything) |

`structure_summarize.py` takes the matching `--root`/`--out`/`--hotspot-loc`
(must agree with whatever you ran `structure_report.py` with), plus
`--force` (ignore the cache) and `--model` (default `haiku` — this is a
bounded "what does this do" judgment call, not code generation).

## What it understands

- **Python** (`.py`): real `ast` parsing — functions, classes, methods,
  arbitrarily nested local functions, `import`/`from` resolution against
  the repo's own packages (anything with an `__init__.py`).
- **JS/TS family** (`.js`/`.jsx`/`.ts`/`.tsx`): regex + brace-matching
  (no bundler/parser dependency, so it tolerates TS type annotations but
  doesn't *understand* the type system). Import resolution handles:
  - relative imports (`./x`, `../x`)
  - yarn/npm **workspace packages** — any `package.json` with a `"name"`
    gets registered, so `import "@scope/pkg/sub/path"` resolves to that
    package's directory + `/sub/path`
  - **tsconfig/jsconfig path aliases** (`compilerOptions.paths` +
    `baseUrl`, e.g. `~/*` → `src/*`) — tsconfig files are parsed as JSONC
    (comments, trailing commas), since that's what real-world tsconfigs
    actually contain

Everything else (Ruby, Go, Java, ...) isn't parsed at all — those files
are simply invisible to this tool today. Extending to another language
means adding an outline extractor (AST if one's available in Python
stdlib-reachable form, otherwise a regex+block-matching approach like the
JS one) and an edge resolver for that language's import/require syntax.

## Known limitations

- The JS/TS brace-matcher doesn't understand strings, comments, or
  template literals — a stray `{`/`}` inside one of those can occasionally
  throw off a function's detected end line.
- Workspace/alias resolution doesn't walk tsconfig `extends` chains — only
  what each config file itself declares. Most real path aliases are
  declared directly in the workspace that uses them, so this is usually
  fine, but an alias only defined in a parent config won't be picked up.
- No call-graph beyond one file: `calls`/orchestrator phase "functions" are
  real names from the same file; cross-file call tracing isn't attempted.
