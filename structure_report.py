#!/usr/bin/env python3
"""Generate a structure report for ANY repo: per-file LOC + import fan-in/
fan-out, a per-file function/class outline (any nesting depth), a diff
against the last snapshot, and a static HTML page to browse it all —
including, once `structure_summarize.py` has run, a plain-English PURPOSE
per function and an ordered PROCESS breakdown for big orchestrating
functions.

Why this exists instead of an OSS dependency-graph tool (madge, pydeps,
dependency-cruiser, pyan3, code2flow): those all need a package registry,
and this kind of import syntax (plain `import x` / `from x import y` in
Python, `import {...} from "./x"` in JS/TS) is simple enough to regex-parse
without one. Stdlib only, zero dependencies.

Supports Python (.py) and the JS/TS family (.js/.jsx/.ts/.tsx) today.

Usage:
  structure_report.py [--root PATH] [--out DIR] [--hotspot-loc N]
                       [--orchestrator-min-nested N] [--orchestrator-min-loc N]
                       [--graph-max-nodes N]

  --root PATH                   repo to analyze (default: cwd)
  --out DIR                     where snapshot.json/report.html/summaries.json
                                 live (default: <root>/.structure-report)
  --hotspot-loc N                "big file" threshold, informational (default: 800)
  --orchestrator-min-nested N     >=N nested helpers makes a function an
                                   "orchestrator" candidate for a process
                                   breakdown (default: 5)
  --orchestrator-min-loc N        or >=N lines of its own (default: 150)
  --graph-max-nodes N             above this many tracked files, the rendered
                                   graph shows only hotspots + their direct
                                   neighbors, not everything (default: 500) —
                                   the full file-by-size table is unaffected

Commit snapshot.json (and summaries.json, if you've run the summarizer) in
the TARGET repo if you want "what changed structurally" to show up as an
ordinary diff in review. report.html is pure derived output — regenerate
on demand, no need to commit it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ---- config (set by main() from CLI args; the rest of the module reads
# these as module globals so build_snapshot/render_html stay simple) --------

ROOT = Path.cwd()
OUT_DIR = ROOT / ".structure-report"
SNAPSHOT_PATH = OUT_DIR / "snapshot.json"
REPORT_PATH = OUT_DIR / "report.html"
SUMMARIES_PATH = OUT_DIR / "summaries.json"
LOC_HOTSPOT = 800
ORCHESTRATOR_MIN_NESTED = 5
ORCHESTRATOR_MIN_LOC = 150
GRAPH_MAX_NODES = 500

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx"}
TRACKED_EXT = PY_EXT | JS_EXT

_DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", "vendor", ".next", "coverage", "target", ".tox", ".mypy_cache",
    ".pytest_cache", "tmp", "log", "public/assets", "coverage",
}
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)(test_|spec_)|"
                            r"(\.(test|spec))\.(py|js|jsx|ts|tsx)$")


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _is_test_path(rel: str) -> bool:
    return bool(_TEST_PATH_RE.search(rel))


def _is_excluded(rel_parts: tuple) -> bool:
    return any(part in _DEFAULT_EXCLUDE_DIRS or part == "vendor" for part in rel_parts[:-1])


# ---- JSONC (tsconfig/jsconfig allow // and /* */ comments + trailing
# commas — plain json.loads chokes on real-world tsconfig files) -----------

_JSONC_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_jsonc_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == '"':
            m = _JSONC_STRING_RE.match(text, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        if text[i:i + 2] == "//":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _parse_jsonc(text: str) -> dict:
    return json.loads(_TRAILING_COMMA_RE.sub(r"\1", _strip_jsonc_comments(text)))


def _tracked_json_files(basename_re: re.Pattern) -> list[Path]:
    """Every tracked file whose basename matches `basename_re` — via `git
    ls-files` when possible so excluded dirs (node_modules, vendor, ...) are
    skipped for free, same as the main file collector."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                              text=True, timeout=30, check=True).stdout
        all_paths = [ROOT / line for line in out.splitlines()]
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        all_paths = [p for p in ROOT.rglob("*") if p.is_file()
                     and not _is_excluded(p.relative_to(ROOT).parts)]
    return [p for p in all_paths if basename_re.match(p.name)
            and not _is_excluded(p.relative_to(ROOT).parts) and p.is_file()]


_PACKAGE_JSON_RE = re.compile(r"^package\.json$")
_TSCONFIG_RE = re.compile(r"^(tsconfig|jsconfig)[\w.-]*\.json$")


def _find_packages() -> dict[str, Path]:
    """Local workspace package name -> its directory, from every
    package.json NOT under node_modules/vendor/etc. Lets `import
    "@scope/pkg/sub/path"` resolve in a yarn/npm-workspaces monorepo even
    though that's not a relative import at all."""
    pkgs: dict[str, Path] = {}
    for p in _tracked_json_files(_PACKAGE_JSON_RE):
        try:
            name = json.loads(p.read_text(errors="replace")).get("name")
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(name, str) and name:
            pkgs.setdefault(name, p.parent)
    return pkgs


def _find_ts_path_aliases() -> list[tuple[str, Path]]:
    """[(alias_prefix, target_dir), ...] from every tsconfig*/jsconfig*.json
    `compilerOptions.paths` (+ `baseUrl`), e.g. `{"baseUrl":"src","paths":
    {"~/*":["*"]}}` in frontend/gp/tsconfig.json becomes
    ("~/", <root>/frontend/gp/src/). Doesn't walk `extends` chains — only
    what each file itself declares, which is where real workspace aliases
    usually live."""
    aliases: list[tuple[str, Path]] = []
    for p in _tracked_json_files(_TSCONFIG_RE):
        try:
            data = _parse_jsonc(p.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        co = data.get("compilerOptions") or {}
        paths = co.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        base = (p.parent / co.get("baseUrl", ".")).resolve()
        for pattern, targets in paths.items():
            if not targets or not isinstance(targets, list):
                continue
            target = targets[0]
            alias_prefix = pattern[:-1] if pattern.endswith("*") else pattern
            target_prefix = target[:-1] if target.endswith("*") else target
            aliases.append((alias_prefix, (base / target_prefix).resolve()))
    return aliases


def _collect_files() -> dict[str, dict]:
    """path (repo-relative, str) -> {loc, kind, group, isTest}.

    Uses `git ls-files` when ROOT is a git repo (so .gitignore is respected
    for free, no reimplementation needed); falls back to a plain walk with a
    conservative default exclude-list otherwise. Either way, the exclude-dir
    list (vendor/, node_modules/, build/, ...) and the minified-file check
    apply uniformly — `git ls-files` lists tracked vendor files too if
    someone committed them, so it needs the same filtering."""
    paths: list[Path] = []
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                              text=True, timeout=30, check=True).stdout
        for line in out.splitlines():
            p = ROOT / line
            if p.suffix in TRACKED_EXT and p.is_file():
                paths.append(p)
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        for p in ROOT.rglob("*"):
            if p.suffix in TRACKED_EXT and p.is_file():
                paths.append(p)

    files: dict[str, dict] = {}
    for p in sorted(set(paths)):
        rel = p.relative_to(ROOT).as_posix()
        if _is_excluded(p.relative_to(ROOT).parts) or rel.endswith(".min.js"):
            continue
        group = rel.split("/", 1)[0] if "/" in rel else "."
        kind = "py" if p.suffix in PY_EXT else p.suffix.lstrip(".")
        files[rel] = {"loc": len(_read_lines(p)), "kind": kind, "group": group,
                       "isTest": _is_test_path(rel)}
    return files


# ---- import parsing -------------------------------------------------------

_PY_IMPORT_LINE_RE = re.compile(r"^\s*import\s+(.+)$")
_PY_FROM_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\b")
# Catches `from "x"` / `import "x"` (covers `import {...} from`, `export
# {...} from`, bare side-effect imports) and `require("x")` — relative
# (`./x`) AND bare/aliased specifiers (`@scope/pkg/sub`, `~/foo`) alike;
# resolving the latter needs _find_packages()/_find_ts_path_aliases().
_JS_SPECIFIER_RE = re.compile(
    r"""(?:^|[\s;])(?:from|import)\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_RESOLVE_SUFFIXES = ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx",
                         "/index.js", "/index.jsx"]


def _py_module_map(files: dict[str, dict]) -> dict[str, str]:
    """module name (as it'd appear in an import statement) -> repo-relative
    file path, for every Python file we track. Handles `pkg.sub` for any
    directory containing an `__init__.py` (a real Python package), and bare
    top-level module names."""
    mod_map: dict[str, str] = {}
    packages = {rel.rsplit("/", 1)[0] for rel in files
                if rel.endswith("/__init__.py")}
    for rel in files:
        if not rel.endswith(".py"):
            continue
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if d in packages:
            stem = rel[len(d) + 1:-len(".py")]
            dotted = d.replace("/", ".")
            mod_map[f"{dotted}.{stem}"] = rel
            if stem == "__init__":
                mod_map[dotted] = rel
        elif "/" not in rel:
            mod_map[rel[:-len(".py")]] = rel
    return mod_map


def _py_edges(rel: str, mod_map: dict[str, str]) -> set[str]:
    edges: set[str] = set()
    pkg = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else None
    for line in _read_lines(ROOT / rel):
        names = []
        m = _PY_IMPORT_LINE_RE.match(line)
        if m:
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if name:
                    names.append(name)
        m = _PY_FROM_RE.match(line)
        if m:
            mod = m.group(1)
            if mod == "." and pkg:
                mod = pkg
            elif mod.startswith(".") and pkg:
                mod = f"{pkg}{mod}"
            names.append(mod)
        for n in names:
            target = mod_map.get(n)
            if target and target != rel:
                edges.add(target)
    return edges


def _resolve_js_spec(spec: str, base_dir: Path, files: dict[str, dict],
                      packages: dict[str, Path], aliases: list[tuple[str, Path]]) -> str | None:
    """A module specifier -> repo-relative tracked file path, or None.
    Tries, in order: relative (`./x`), workspace package name
    (`@scope/pkg/sub` where `@scope/pkg` is a local package.json), then
    tsconfig path alias (`~/foo`). The first candidate base that resolves
    to an actually-tracked file (after trying extension suffixes) wins."""
    bases: list[Path] = []
    if spec.startswith("."):
        bases.append((base_dir / spec).resolve())
    else:
        for name, pkg_dir in packages.items():
            if spec == name:
                bases.append(pkg_dir.resolve())
            elif spec.startswith(name + "/"):
                bases.append((pkg_dir / spec[len(name) + 1:]).resolve())
        for prefix, target_dir in aliases:
            if spec.startswith(prefix):
                bases.append((target_dir / spec[len(prefix):]).resolve())
    for base in bases:
        for suffix in _JS_RESOLVE_SUFFIXES:
            try:
                target_rel = Path(str(base) + suffix).relative_to(ROOT).as_posix()
            except ValueError:
                continue
            if target_rel in files:
                return target_rel
    return None


def _js_edges(rel: str, files: dict[str, dict], packages: dict[str, Path],
              aliases: list[tuple[str, Path]]) -> set[str]:
    edges: set[str] = set()
    base_dir = (ROOT / rel).parent
    for line in _read_lines(ROOT / rel):
        if "import" not in line and "from" not in line and "require(" not in line:
            continue
        for m in _JS_SPECIFIER_RE.finditer(line):
            spec = m.group(1) or m.group(2)
            target = _resolve_js_spec(spec, base_dir, files, packages, aliases)
            if target:
                edges.add(target)
    edges.discard(rel)
    return edges


# ---- per-file outline (functions/classes, line ranges, in-file calls) -----
# Cheap + deterministic — runs every time, for every file. The expensive part
# (a one-line PURPOSE for each, and a PROCESS breakdown for orchestrators) is
# a separate, explicitly-triggered pass; see structure_summarize.py. This
# just answers "what's in here and where".

def function_hash(rel: str, start_line: int, end_line: int) -> str:
    """Content hash for one outline entry's exact source lines — lets the
    summarizer tell "unchanged" from "stale" without re-asking Claude."""
    lines = _read_lines(ROOT / rel)[start_line - 1:end_line]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def _py_outline(rel: str) -> list[dict]:
    """Every function/class at ANY nesting depth — not just module/class
    level. A big file's real shape is often one large function with a dozen
    local helpers closed over its state; missing those misses the point."""
    try:
        tree = ast.parse((ROOT / rel).read_text(errors="replace"))
    except SyntaxError:
        return []
    entries: list[dict] = []

    def direct_calls(node) -> set[str]:
        """Calls made directly in `node`'s own body — NOT inside a nested
        def (that nested def gets its own entry with its own calls)."""
        calls: set[str] = set()
        skip: set[int] = set()
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node:
                skip.update(id(d) for d in ast.walk(n))
        for n in ast.walk(node):
            if not isinstance(n, ast.Call) or id(n) in skip:
                continue
            f = n.func
            if isinstance(f, ast.Name):
                calls.add(f.id)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self":
                calls.add(f.attr)
        return calls

    def visit(node, class_prefix: str, parent_name: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = ("async method" if class_prefix else "async function") if isinstance(child, ast.AsyncFunctionDef) \
                    else ("method" if class_prefix else "function")
                qualname = f"{class_prefix}{child.name}"
                entry = {"name": qualname, "kind": kind, "startLine": child.lineno,
                          "endLine": child.end_lineno, "calls": sorted(direct_calls(child))}
                if parent_name:
                    entry["parent"] = parent_name
                entries.append(entry)
                visit(child, "", qualname)
            elif isinstance(child, ast.ClassDef):
                entry = {"name": child.name, "kind": "class", "startLine": child.lineno,
                          "endLine": child.end_lineno, "calls": []}
                if parent_name:
                    entry["parent"] = parent_name
                entries.append(entry)
                visit(child, f"{child.name}.", None)
            elif isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
                visit(child, class_prefix, parent_name)

    visit(tree, "", None)
    entries.sort(key=lambda e: e["startLine"])
    known = {e["name"] for e in entries} | {e["name"].split(".")[-1] for e in entries}
    for e in entries:
        e["calls"] = sorted(c for c in e["calls"] if c in known and c != e["name"])
    return entries


# `function foo(...)`, `async function foo(...)`, `export function foo(...)`,
# `export const foo = (...) =>`, `const foo = (...) =>` / `async (...) =>` —
# tolerates TS type annotations after the params (`): ReturnType`) since
# those don't change where the signature line starts, at any indentation —
# a builder like a React component or a big handler is often one top-level
# function with a dozen local helpers nested inside it, and those nested
# helpers are exactly "where do I look" for anyone touching it.
_JS_FUNC_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(async\s+)?function\s*\*?\s+(\w+)\s*(?:<[^>]*>)?\s*\(" r"|"
    r"^\s*(?:export\s+)?(?:default\s+)?const\s+(\w+)\s*(?::\s*[^=]+)?=\s*(async\s+)?\([^)]*\)\s*(?::\s*[^=]+)?=>"
)


def _js_outline(rel: str) -> list[dict]:
    lines = _read_lines(ROOT / rel)
    entries: list[dict] = []
    for i, line in enumerate(lines):
        m = _JS_FUNC_RE.match(line)
        if not m:
            continue
        name = m.group(2) or m.group(3)
        is_async = bool(m.group(1) or m.group(4))
        end = _js_brace_end(lines, i)
        entries.append({"name": name, "kind": "async function" if is_async else "function",
                         "startLine": i + 1, "endLine": end + 1, "calls": []})
    entries.sort(key=lambda e: e["startLine"])

    # Nesting: the SMALLEST other entry whose line range contains this one.
    for e in entries:
        best = None
        for other in entries:
            if other is e or other["startLine"] > e["startLine"] or other["endLine"] < e["endLine"]:
                continue
            if best is None or (other["endLine"] - other["startLine"]) < (best["endLine"] - best["startLine"]):
                best = other
        if best:
            e["parent"] = best["name"]

    known = {e["name"] for e in entries}
    for e in entries:
        child_ranges = [(c["startLine"], c["endLine"]) for c in entries if c.get("parent") == e["name"]]
        own_lines = [ln for idx, ln in enumerate(lines[e["startLine"]:e["endLine"]], start=e["startLine"] + 1)
                     if not any(lo <= idx <= hi for lo, hi in child_ranges)]
        body = "\n".join(own_lines)
        e["calls"] = sorted(n for n in known
                             if n != e["name"] and re.search(rf"\b{re.escape(n)}\s*\(", body))
    return entries


def _js_brace_end(lines: list[str], start_idx: int) -> int:
    """Index (0-based) of the line containing the closing `}` for the block
    opened on/after `start_idx`. Approximate — doesn't understand strings,
    comments, or template literals, so a `{`/`}` inside any of those can
    throw it off on rare lines."""
    depth = 0
    seen_open = False
    for idx in range(start_idx, len(lines)):
        for ch in lines[idx]:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
        if seen_open and depth <= 0:
            return idx
    return len(lines) - 1


# An "orchestrator" is a candidate for a process/phase breakdown: a
# top-level entry that either IS a conventional entry point (`run`, `main`)
# or has enough nested helpers / bulk that it's plausibly one big function
# doing many real things in sequence. Mechanical signal only — WHAT the
# phases actually are needs reading the code; that's the agent's job
# (structure_summarize.py), not this one.
_ENTRY_POINT_NAMES = {"run", "main"}


def _mark_orchestrators(entries: list[dict]) -> None:
    nested_count = {e["name"]: 0 for e in entries}
    for e in entries:
        p = e.get("parent")
        if p in nested_count:
            nested_count[p] += 1
    for e in entries:
        if e.get("parent") or e["kind"] == "class":
            continue  # only top-level, callable entry points are orchestrators
        loc = e["endLine"] - e["startLine"] + 1
        e["nestedCount"] = nested_count[e["name"]]
        e["isOrchestrator"] = (
            e["name"] in _ENTRY_POINT_NAMES
            or nested_count[e["name"]] >= ORCHESTRATOR_MIN_NESTED
            or loc >= ORCHESTRATOR_MIN_LOC
        )


def build_snapshot() -> dict:
    files = _collect_files()
    mod_map = _py_module_map(files)
    packages = _find_packages()
    aliases = _find_ts_path_aliases()
    all_edges: list[tuple[str, str]] = []
    for rel, meta in files.items():
        edges = _py_edges(rel, mod_map) if meta["kind"] == "py" else _js_edges(rel, files, packages, aliases)
        for target in sorted(edges):
            all_edges.append((rel, target))

    fan_out: dict[str, int] = {rel: 0 for rel in files}
    fan_in: dict[str, int] = {rel: 0 for rel in files}
    for src, dst in all_edges:
        fan_out[src] += 1
        fan_in[dst] += 1

    for rel, meta in files.items():
        meta["fanIn"] = fan_in[rel]
        meta["fanOut"] = fan_out[rel]
        outline = _py_outline(rel) if meta["kind"] == "py" else _js_outline(rel)
        _mark_orchestrators(outline)
        meta["outline"] = outline

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(ROOT),
        "config": {"locHotspot": LOC_HOTSPOT, "orchestratorMinNested": ORCHESTRATOR_MIN_NESTED,
                   "orchestratorMinLoc": ORCHESTRATOR_MIN_LOC},
        "files": files,
        "edges": [list(e) for e in all_edges],
    }


# ---- diff against the previous snapshot ------------------------------------

def diff_snapshots(old: dict | None, new: dict) -> dict:
    if not old:
        return {"isFirstRun": True, "newFiles": [], "removedFiles": [], "locDeltas": [],
                "newEdges": [], "removedEdges": []}
    old_files = old.get("files", {})
    new_files = new["files"]
    new_paths, old_paths = set(new_files), set(old_files)

    loc_deltas = []
    for rel in sorted(new_paths & old_paths):
        before, after = old_files[rel]["loc"], new_files[rel]["loc"]
        delta = after - before
        if abs(delta) >= 30 or (before and abs(delta) / before >= 0.15):
            loc_deltas.append({"path": rel, "before": before, "after": after, "delta": delta})

    old_edges = {tuple(e) for e in old.get("edges", [])}
    new_edges = {tuple(e) for e in new["edges"]}

    return {
        "isFirstRun": False,
        "newFiles": sorted(new_paths - old_paths),
        "removedFiles": sorted(old_paths - new_paths),
        "locDeltas": sorted(loc_deltas, key=lambda d: -abs(d["delta"])),
        "newEdges": sorted(new_edges - old_edges),
        "removedEdges": sorted(old_edges - new_edges),
    }


def load_summaries() -> dict:
    """{rel: {"functions": {name: {hash, summary}}, "phases": {orchestratorName:
    {hash, phases: [{title, goal, functions}]}}}} — only present for files
    that went through structure_summarize.py. Never written here."""
    if not SUMMARIES_PATH.exists():
        return {}
    try:
        return json.loads(SUMMARIES_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def with_summaries(snapshot: dict, summaries: dict) -> dict:
    """A DISPLAY copy of `snapshot` with per-function summaries and
    per-orchestrator phase breakdowns merged in, for embedding in the HTML.
    The on-disk snapshot.json stays agent-free and purely
    structural/deterministic — summaries.json is the separate, agent-written
    source of truth for both of those."""
    view = json.loads(json.dumps(snapshot))  # deep copy
    for rel, meta in view["files"].items():
        cached = summaries.get(rel, {})
        fn_cache = cached.get("functions", {})
        phase_cache = cached.get("phases", {})
        for entry in meta.get("outline", []):
            c = fn_cache.get(entry["name"])
            entry["summary"] = c.get("summary") if c else None
            entry["stale"] = bool(c) and c.get("hash") != function_hash(rel, entry["startLine"], entry["endLine"])
            if entry.get("isOrchestrator"):
                p = phase_cache.get(entry["name"])
                entry["phases"] = p.get("phases") if p else None
                entry["phasesStale"] = bool(p) and p.get("hash") != function_hash(rel, entry["startLine"], entry["endLine"])
    return view


# ---- color palette (groups are whatever top-level dirs the target repo
# actually has — assign colors deterministically, not from a fixed map) -----

_PALETTE = ["#0969da", "#bc4c00", "#8250df", "#1a7f37", "#cf222e", "#9a6700",
            "#57606a", "#0550ae", "#8a63d2", "#d4a72c"]


def _color_for(group: str) -> str:
    return _PALETTE[hash(group) % len(_PALETTE)]


# ---- HTML rendering ---------------------------------------------------

def render_html(snapshot: dict, diff: dict, repo_name: str) -> str:
    files = snapshot["files"]
    by_loc = sorted(files.items(), key=lambda kv: -kv[1]["loc"])
    total_loc = sum(m["loc"] for m in files.values())
    app_loc = sum(m["loc"] for m in files.values() if not m["isTest"])
    test_loc = sum(m["loc"] for m in files.values() if m["isTest"])
    groups = sorted({m["group"] for m in files.values()})

    graph_capped = len(files) > GRAPH_MAX_NODES
    if graph_capped:
        hotspots = {rel for rel, m in files.items() if m["loc"] >= LOC_HOTSPOT}
        neighborhood = set(hotspots)
        for a, b in snapshot["edges"]:
            if a in hotspots or b in hotspots:
                neighborhood.add(a)
                neighborhood.add(b)
        graph_files = {rel: m for rel, m in files.items() if rel in neighborhood}
        graph_edges = [e for e in snapshot["edges"] if e[0] in neighborhood and e[1] in neighborhood]
    else:
        graph_files = files
        graph_edges = snapshot["edges"]

    graph_payload = {"files": graph_files, "edges": graph_edges}
    data_json = json.dumps(graph_payload).replace("</", "<\\/")

    def diff_section() -> str:
        if diff["isFirstRun"]:
            return "<p class='muted'>First run — no prior snapshot to diff against.</p>"
        parts = []
        if diff["newFiles"]:
            parts.append("<h3>New files</h3><ul>" + "".join(f"<li><code>{p}</code></li>" for p in diff["newFiles"]) + "</ul>")
        if diff["removedFiles"]:
            parts.append("<h3>Removed files</h3><ul>" + "".join(f"<li><code>{p}</code></li>" for p in diff["removedFiles"]) + "</ul>")
        if diff["locDeltas"]:
            rows = "".join(
                f"<tr><td><code>{d['path']}</code></td><td>{d['before']}</td><td>{d['after']}</td>"
                f"<td class='{'grow' if d['delta'] > 0 else 'shrink'}'>{'+' if d['delta'] > 0 else ''}{d['delta']}</td></tr>"
                for d in diff["locDeltas"])
            parts.append(f"<h3>LOC changes (&ge;30 lines or &ge;15%)</h3><table><tr><th>File</th><th>Before</th><th>After</th><th>Delta</th></tr>{rows}</table>")
        if diff["newEdges"]:
            parts.append("<h3>New import edges</h3><ul>" + "".join(f"<li><code>{a}</code> &rarr; <code>{b}</code></li>" for a, b in diff["newEdges"]) + "</ul>")
        if diff["removedEdges"]:
            parts.append("<h3>Removed import edges</h3><ul>" + "".join(f"<li><code>{a}</code> &rarr; <code>{b}</code></li>" for a, b in diff["removedEdges"]) + "</ul>")
        if not parts:
            return "<p class='muted'>No structural change since the last snapshot.</p>"
        return "".join(parts)

    rows_html = "".join(
        f"<tr class='{'hot' if m['loc'] >= LOC_HOTSPOT else ''}'>"
        f"<td><code>{rel}</code></td><td>{m['group']}</td><td>{m['kind']}</td>"
        f"<td>{m['loc']}</td><td>{m['fanIn']}</td><td>{m['fanOut']}</td></tr>"
        for rel, m in by_loc)

    legend_html = "".join(
        f'<span><i style="background:{_color_for(g)}"></i>{g}</span>' for g in groups)

    graph_note = (
        f"Showing {len(graph_files)} of {len(files)} tracked files in the graph "
        f"(hotspots &ge;{LOC_HOTSPOT} LOC + their direct neighbors) — too many files to "
        f"render all of them usefully. The table below still lists everything."
        if graph_capped else
        f"Node size = LOC; labeled nodes are &ge;{LOC_HOTSPOT}-LOC hotspots."
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{repo_name} structure report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
  body {{ font-family: -apple-system, "Segoe UI", system-ui, sans-serif; margin: 0; padding: 28px 32px 60px;
    background: #fff; color: #1f2328; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #57606a; font-size: 13px; margin: 0 0 24px; }}
  .cards {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 28px; }}
  .card {{ border: 1px solid #d0d7de; border-radius: 8px; padding: 10px 16px; background: #f6f8fa; min-width: 120px; }}
  .card b {{ display: block; font-size: 20px; }}
  .card span {{ font-size: 12px; color: #57606a; }}
  section {{ margin-bottom: 36px; }}
  h2 {{ font-size: 15px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 5px 10px; border-bottom: 1px solid #eaeef2; }}
  th {{ color: #57606a; font-weight: 600; }}
  tr.hot td {{ background: #fff8f0; }}
  tr.hot td:first-child {{ border-left: 3px solid #bc4c00; }}
  code {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12.5px; }}
  .muted {{ color: #57606a; }}
  td.grow {{ color: #9a6700; }}
  td.shrink {{ color: #1a7f37; }}
  .graph-row {{ display: flex; gap: 14px; align-items: flex-start; }}
  #graph {{ border: 1px solid #d0d7de; border-radius: 8px; flex: 1; min-width: 0; }}
  #graph text {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 9.5px; fill: #1f2328;
    pointer-events: none; paint-order: stroke; stroke: #fff; stroke-width: 3px; }}
  #graph circle {{ cursor: pointer; }}
  #graph .dim {{ opacity: 0.12; }}
  #node-detail {{ width: 320px; flex: none; border: 1px solid #d0d7de; border-radius: 8px; padding: 12px 14px;
    font-size: 12.5px; background: #f6f8fa; align-self: stretch; overflow-y: auto; max-height: 640px; }}
  #node-detail h3 {{ margin: 0 0 6px; font-size: 13px; font-family: ui-monospace, "SF Mono", Menlo, monospace;
    word-break: break-all; }}
  #node-detail .nd-row {{ margin: 2px 0 10px; color: #57606a; }}
  #node-detail .nd-list {{ margin: 4px 0 12px; padding-left: 16px; }}
  #node-detail .nd-list li {{ margin-bottom: 2px; }}
  #node-detail .nd-empty {{ color: #57606a; }}
  .nd-fn {{ border: 1px solid #eaeef2; border-radius: 6px; padding: 6px 8px; margin-bottom: 6px; background: #fff; }}
  .nd-fn-head {{ display: flex; justify-content: space-between; gap: 6px; }}
  .nd-fn-name {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; font-weight: 600; }}
  .nd-fn-lines {{ color: #57606a; font-size: 11px; white-space: nowrap; }}
  .nd-fn-summary {{ margin: 4px 0 0; font-size: 12px; }}
  .nd-fn-nosummary {{ margin: 4px 0 0; font-size: 11.5px; color: #8c959f; font-style: italic; }}
  .nd-stale {{ display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; color: #9a6700;
    background: #fff8c5; padding: 1px 5px; border-radius: 3px; }}
  .nd-fn-calls {{ margin: 4px 0 0; font-size: 11px; color: #57606a; }}
  .nd-orch {{ border: 1px solid #0969da; border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; background: #f1f8ff; }}
  .nd-orch-head {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; font-weight: 700;
    color: #0969da; margin-bottom: 6px; }}
  .nd-phase {{ border-left: 2px solid #0969da; padding: 2px 0 6px 10px; margin-bottom: 6px; }}
  .nd-phase:last-child {{ margin-bottom: 0; }}
  .nd-phase-title {{ font-size: 12.5px; font-weight: 600; }}
  .nd-phase-goal {{ font-size: 12px; margin: 2px 0 3px; }}
  .nd-phase-fns {{ font-size: 11px; color: #57606a; }}
  .nd-phase-fns code {{ background: #eaeef2; padding: 0 3px; border-radius: 3px; }}
  details.nd-allfns {{ margin-top: 4px; }}
  details.nd-allfns > summary {{ cursor: pointer; font-size: 11.5px; color: #57606a;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; padding: 2px 0; }}
  .tooltip {{ position: fixed; pointer-events: none; background: #1f2328; color: #fff; font-size: 12px;
    padding: 5px 9px; border-radius: 5px; max-width: 320px; z-index: 10; opacity: 0; transition: opacity 0.08s; }}
  .tooltip code {{ color: #fff; background: rgba(255,255,255,0.15); padding: 0 3px; border-radius: 3px; }}
  .legend {{ font-size: 12px; color: #57606a; margin-top: 6px; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 4px; margin-right: 14px; }}
  .legend i {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
</style>
</head>
<body>
<h1>{repo_name} structure report</h1>
<p class="sub">generated {snapshot['generatedAt']} &middot; {snapshot['root']}</p>

<div class="cards">
  <div class="card"><b>{len(files)}</b><span>tracked files</span></div>
  <div class="card"><b>{total_loc}</b><span>total LOC</span></div>
  <div class="card"><b>{app_loc}</b><span>app LOC</span></div>
  <div class="card"><b>{test_loc}</b><span>test LOC</span></div>
</div>

<section>
  <h2>Since last snapshot</h2>
  {diff_section()}
</section>

<section>
  <h2>Import graph</h2>
  <div class="graph-row">
    <div id="graph"></div>
    <div id="node-detail"><p class="nd-empty">Click a node to see its imports/importers. Hover any node for a quick summary.</p></div>
  </div>
  <div class="legend">{legend_html}</div>
  <p class="muted">{graph_note} Drag to rearrange, hover for a quick summary, click a node to see what it
  imports / is imported by, plus its function/class breakdown and process view (purpose summaries need
  <code>structure_summarize.py</code> to have run).</p>
</section>
<div class="tooltip" id="tooltip"></div>

<section>
  <h2>Files by size (hotspot = &ge;{LOC_HOTSPOT} LOC)</h2>
  <table>
    <tr><th>File</th><th>Group</th><th>Kind</th><th>LOC</th><th>Fan-in</th><th>Fan-out</th></tr>
    {rows_html}
  </table>
</section>

<script type="application/json" id="snapshot-data">{data_json}</script>
<script>
const payload = JSON.parse(document.getElementById("snapshot-data").textContent);
const HOTSPOT = {LOC_HOTSPOT};
const nodes = Object.entries(payload.files).map(([path, m]) => ({{ id: path, ...m }}));
const links = payload.edges.map(([source, target]) => ({{ source, target }}));
function colorFor(group) {{
  const palette = {json.dumps(_PALETTE)};
  let h = 0;
  for (let i = 0; i < group.length; i++) h = (h * 31 + group.charCodeAt(i)) | 0;
  return palette[((h % palette.length) + palette.length) % palette.length];
}}

// imports[path] = files it imports; importedBy[path] = files that import it.
const imports = new Map(nodes.map((n) => [n.id, []]));
const importedBy = new Map(nodes.map((n) => [n.id, []]));
for (const [src, dst] of payload.edges) {{
  imports.get(src)?.push(dst);
  importedBy.get(dst)?.push(src);
}}
const neighbors = (id) => new Set([id, ...(imports.get(id) || []), ...(importedBy.get(id) || [])]);

const width = Math.min(1100, document.body.clientWidth - 380), height = 640;
const svg = d3.select("#graph").append("svg").attr("width", width).attr("height", height);
const g = svg.append("g");
svg.call(d3.zoom().scaleExtent([0.2, 4]).on("zoom", (ev) => g.attr("transform", ev.transform)));

const sim = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(links).id((d) => d.id).distance(60).strength(0.25))
  .force("charge", d3.forceManyBody().strength(-120))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collide", d3.forceCollide((d) => 4 + Math.sqrt(d.loc) * 0.6));

const link = g.append("g").attr("stroke", "#d0d7de").selectAll("line")
  .data(links).join("line").attr("stroke-width", 1);

const node = g.append("g").selectAll("circle")
  .data(nodes).join("circle")
  .attr("r", (d) => 3 + Math.sqrt(d.loc) * 0.6)
  .attr("fill", (d) => colorFor(d.group))
  .attr("stroke", "#fff").attr("stroke-width", 1)
  .call(d3.drag()
    .on("start", (ev, d) => {{ if (!ev.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
    .on("drag", (ev, d) => {{ d.fx = ev.x; d.fy = ev.y; }})
    .on("end", (ev, d) => {{ if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }}));

const label = g.append("g").selectAll("text")
  .data(nodes.filter((d) => d.loc >= HOTSPOT)).join("text")
  .attr("dy", (d) => -(5 + Math.sqrt(d.loc) * 0.6))
  .attr("text-anchor", "middle")
  .text((d) => d.id.split("/").pop());

// Custom tooltip (SVG <title> is slow to appear and unstyled — this follows the cursor instantly).
const tooltip = d3.select("#tooltip");
function showTooltip(ev, d) {{
  tooltip.style("opacity", 1)
    .style("left", (ev.clientX + 14) + "px").style("top", (ev.clientY + 12) + "px")
    .html(`<code>${{d.id}}</code><br>${{d.loc}} LOC &middot; fan-in ${{d.fanIn}} &middot; fan-out ${{d.fanOut}}`);
}}
function hideTooltip() {{ tooltip.style("opacity", 0); }}

const detail = d3.select("#node-detail");
let pinned = null;

function renderOutline(d) {{
  const outline = d.outline || [];
  if (!outline.length) return `<p class="nd-empty">No functions/classes detected.</p>`;
  return outline.map((e) => `
    <div class="nd-fn">
      <div class="nd-fn-head">
        <span class="nd-fn-name">${{e.name}} <span class="nd-row" style="display:inline">(${{e.kind}})</span></span>
        <span class="nd-fn-lines">L${{e.startLine}}&ndash;${{e.endLine}}</span>
      </div>
      ${{e.summary
        ? `<p class="nd-fn-summary">${{e.summary}}${{e.stale ? '<span class="nd-stale" title="source changed since this summary was generated">stale</span>' : ""}}</p>`
        : `<p class="nd-fn-nosummary">no summary yet — run structure_summarize.py</p>`}}
      ${{e.calls && e.calls.length ? `<div class="nd-fn-calls">calls: ${{e.calls.join(", ")}}</div>` : ""}}
    </div>
  `).join("");
}}

function renderProcess(d) {{
  const orchestrators = (d.outline || []).filter((e) => e.isOrchestrator);
  if (!orchestrators.length) return "";
  return orchestrators.map((o) => {{
    if (!o.phases) {{
      return `<div class="nd-orch"><div class="nd-orch-head">${{o.name}}() &mdash; process</div>
        <p class="nd-fn-nosummary">no phase breakdown yet &mdash; run structure_summarize.py</p></div>`;
    }}
    const stale = o.phasesStale
      ? '<span class="nd-stale" title="source changed since this breakdown was generated">stale</span>' : "";
    const phases = o.phases.map((p) => `
      <div class="nd-phase">
        <div class="nd-phase-title">${{p.title}}</div>
        <p class="nd-phase-goal">${{p.goal}}</p>
        ${{p.functions && p.functions.length
          ? `<div class="nd-phase-fns">${{p.functions.map((f) => `<code>${{f}}</code>`).join(" ")}}</div>` : ""}}
      </div>`).join("");
    return `<div class="nd-orch"><div class="nd-orch-head">${{o.name}}() &mdash; process${{stale}}</div>${{phases}}</div>`;
  }}).join("");
}}

function renderDetail(d) {{
  const outs = (imports.get(d.id) || []).sort();
  const ins = (importedBy.get(d.id) || []).sort();
  const list = (arr, empty) => arr.length
    ? `<ul class="nd-list">${{arr.map((p) => `<li><code>${{p}}</code></li>`).join("")}}</ul>`
    : `<p class="nd-empty">${{empty}}</p>`;
  const process = renderProcess(d);
  detail.html(`
    <h3>${{d.id}}</h3>
    <div class="nd-row">${{d.group}} / ${{d.kind}} &middot; ${{d.loc}} LOC</div>
    ${{process}}
    <b>Imports (${{outs.length}})</b>${{list(outs, "imports nothing of ours")}}
    <b>Imported by (${{ins.length}})</b>${{list(ins, "nothing of ours imports this")}}
    <details class="nd-allfns"><summary>All functions/classes (${{(d.outline || []).length}})</summary>${{renderOutline(d)}}</details>
  `);
}}

function applyHighlight(id) {{
  if (!id) {{
    node.classed("dim", false);
    link.classed("dim", false);
    label.classed("dim", false);
    return;
  }}
  const keep = neighbors(id);
  node.classed("dim", (d) => !keep.has(d.id));
  label.classed("dim", (d) => !keep.has(d.id));
  link.classed("dim", (d) => !(keep.has(d.source.id ?? d.source) && keep.has(d.target.id ?? d.target)));
}}

node.on("mousemove", (ev, d) => showTooltip(ev, d))
    .on("mouseleave", hideTooltip)
    .on("click", (ev, d) => {{
      ev.stopPropagation();
      pinned = pinned === d.id ? null : d.id;
      applyHighlight(pinned);
      if (pinned) renderDetail(d);
      else detail.html('<p class="nd-empty">Click a node to see its imports/importers. Hover any node for a quick summary.</p>');
    }});
svg.on("click", () => {{ pinned = null; applyHighlight(null); detail.html('<p class="nd-empty">Click a node to see its imports/importers. Hover any node for a quick summary.</p>'); }});

sim.on("tick", () => {{
  link.attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y)
      .attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
  node.attr("cx", (d) => d.x).attr("cy", (d) => d.y);
  label.attr("x", (d) => d.x).attr("y", (d) => d.y);
}});
</script>
</body>
</html>"""


def main() -> None:
    global ROOT, OUT_DIR, SNAPSHOT_PATH, REPORT_PATH, SUMMARIES_PATH
    global LOC_HOTSPOT, ORCHESTRATOR_MIN_NESTED, ORCHESTRATOR_MIN_LOC, GRAPH_MAX_NODES

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repo to analyze (default: cwd)")
    ap.add_argument("--out", default=None, help="output dir (default: <root>/.structure-report)")
    ap.add_argument("--hotspot-loc", type=int, default=800)
    ap.add_argument("--orchestrator-min-nested", type=int, default=5)
    ap.add_argument("--orchestrator-min-loc", type=int, default=150)
    ap.add_argument("--graph-max-nodes", type=int, default=500)
    args = ap.parse_args()

    ROOT = Path(args.root).resolve()
    OUT_DIR = Path(args.out).resolve() if args.out else ROOT / ".structure-report"
    SNAPSHOT_PATH = OUT_DIR / "snapshot.json"
    REPORT_PATH = OUT_DIR / "report.html"
    SUMMARIES_PATH = OUT_DIR / "summaries.json"
    LOC_HOTSPOT = args.hotspot_loc
    ORCHESTRATOR_MIN_NESTED = args.orchestrator_min_nested
    ORCHESTRATOR_MIN_LOC = args.orchestrator_min_loc
    GRAPH_MAX_NODES = args.graph_max_nodes

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    old = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else None
    snapshot = build_snapshot()
    diff = diff_snapshots(old, snapshot)
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    REPORT_PATH.write_text(render_html(with_summaries(snapshot, load_summaries()), diff, ROOT.name))
    print(f"analyzed {ROOT} — {len(snapshot['files'])} tracked file(s)")
    print(f"wrote {SNAPSHOT_PATH} and {REPORT_PATH}")
    if diff["isFirstRun"]:
        print("first run — commit snapshot.json (in the TARGET repo) to start tracking structural history")
    else:
        changed = (len(diff["newFiles"]) + len(diff["removedFiles"]) + len(diff["locDeltas"])
                   + len(diff["newEdges"]) + len(diff["removedEdges"]))
        print(f"{changed} structural change(s) since the last snapshot")


if __name__ == "__main__":
    main()
