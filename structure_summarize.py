#!/usr/bin/env python3
"""Generate, for the current size-hotspot files (>=--hotspot-loc LOC):
  - a one-sentence PURPOSE summary for every function/class
  - for each "orchestrator" (a `run`/`main` entry point, or any other
    top-level function with enough nested helpers / bulk — see
    structure_report._mark_orchestrators), an ordered PROCESS breakdown:
    phases in the agent's own words (not tied to any framework-level step
    names you might recognize — those are usually coarser than the real
    program flow), each with a goal sentence and the real functions that
    implement it.

Both need actual reading/understanding — structure_report.py's mechanical
pass can find WHERE the orchestrator is, not what it's doing in what order.
Caches results in <out>/summaries.json by content hash, so a re-run only
pays for what changed. One `claude -p` call per hotspot FILE (not per
function/phase) — never run automatically; this is the only part of this
tool that spends real LLM calls.

Requires the `claude` CLI on PATH and already authenticated (inherits your
shell's environment as-is — no credential wiring of its own).

Usage: structure_summarize.py [--root PATH] [--out DIR] [--hotspot-loc N]
                               [--force] [--model MODEL]
  --root PATH      repo to analyze (default: cwd) — must match the --root
                    you used for structure_report.py
  --out DIR        where snapshot.json/summaries.json live
                    (default: <root>/.structure-report)
  --hotspot-loc N  must match structure_report.py's threshold (default: 800)
  --force          ignore the cache, re-generate everything
  --model MODEL    claude -p model tier (default: haiku — this is a bounded
                    judgment call, not code generation)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import structure_report as sr

PROMPT_TEMPLATE = """You are documenting an existing codebase, not writing new code.

File: {path}

Below is the file's full source (line-numbered).
{functions_block}
{phases_block}
Source:
{source}

Respond with ONLY a JSON object, no markdown fences, no commentary, shaped
exactly like this (omit the "functions" or "phases" top-level key entirely
if its section above was empty):
{{
  "functions": [{{"name": "<exact name from the list>", "summary": "<one sentence>"}}, ...],
  "phases": [
    {{"orchestrator": "<exact orchestrator name>",
      "phases": [{{"title": "<short phase title>", "goal": "<one-sentence goal>",
                   "functions": ["<real function names from this file that implement this phase>"]}}, ...]}}
  ]
}}
"""

FUNCTIONS_BLOCK = """
Functions/classes needing a one-sentence PURPOSE summary each — what each is
responsible for / why it exists, not a restatement of its signature:
{targets}
"""

PHASES_BLOCK = """
Orchestrator function(s) needing a PROCESS breakdown. For each, read what it
actually does and describe the REAL sequence of phases in your own words.
Ground every "functions" entry in names that really appear in this file
(nested helpers or calls) — never invent a name:
{targets}
"""


def _numbered_source(rel: str) -> str:
    lines = (sr.ROOT / rel).read_text(errors="replace").splitlines()
    return "\n".join(f"{i + 1:>5} {line}" for i, line in enumerate(lines))


def _call_claude(prompt: str, model: str) -> str:
    cmd = ["claude", "--permission-mode", "default",
           "--disallowedTools", "Bash,Read,Write,Edit,WebFetch,WebSearch,Agent",
           "--model", model, "-p", prompt]
    r = subprocess.run(cmd, cwd=sr.ROOT, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exited {r.returncode}: {(r.stderr or r.stdout)[:500]}")
    return r.stdout


def _parse_json_object(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object found in output: {text[:300]!r}")
    return json.loads(m.group(0))


def _file_cache(summaries: dict, rel: str) -> dict:
    c = summaries.get(rel)
    if c is None:
        c = {"functions": {}, "phases": {}}
    else:
        c.setdefault("functions", {})
        c.setdefault("phases", {})
    summaries[rel] = c
    return c


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default=None)
    ap.add_argument("--hotspot-loc", type=int, default=800)
    ap.add_argument("--force", action="store_true", help="ignore the cache, re-generate everything")
    ap.add_argument("--model", default="haiku")
    args = ap.parse_args()

    sr.ROOT = Path(args.root).resolve()
    out = Path(args.out).resolve() if args.out else sr.ROOT / ".structure-report"
    sr.SNAPSHOT_PATH = out / "snapshot.json"
    sr.SUMMARIES_PATH = out / "summaries.json"

    if not sr.SNAPSHOT_PATH.exists():
        print(f"no snapshot at {sr.SNAPSHOT_PATH} — run structure_report.py first", file=sys.stderr)
        sys.exit(1)
    snapshot = json.loads(sr.SNAPSHOT_PATH.read_text())
    summaries: dict = json.loads(sr.SUMMARIES_PATH.read_text()) if sr.SUMMARIES_PATH.exists() else {}

    hotspots = {rel: m for rel, m in snapshot["files"].items() if m["loc"] >= args.hotspot_loc}
    print(f"{len(hotspots)} hotspot file(s) (>={args.hotspot_loc} LOC)")

    total_fns = total_phases = failures = 0
    for rel, meta in sorted(hotspots.items()):
        outline = meta.get("outline", [])
        if not outline:
            continue
        cache = _file_cache(summaries, rel)

        stale_fns = [(e, sr.function_hash(rel, e["startLine"], e["endLine"])) for e in outline
                     if args.force or cache["functions"].get(e["name"], {}).get("hash")
                     != sr.function_hash(rel, e["startLine"], e["endLine"])]
        orchestrators = [e for e in outline if e.get("isOrchestrator")]
        stale_orchs = [(e, sr.function_hash(rel, e["startLine"], e["endLine"])) for e in orchestrators
                       if args.force or cache["phases"].get(e["name"], {}).get("hash")
                       != sr.function_hash(rel, e["startLine"], e["endLine"])]

        if not stale_fns and not stale_orchs:
            print(f"  {rel}: up to date ({len(outline)} entries, {len(orchestrators)} orchestrator(s))")
            continue

        functions_block = FUNCTIONS_BLOCK.format(
            targets="\n".join(f"- {e['name']} ({e['kind']}, lines {e['startLine']}-{e['endLine']})"
                               for e, _ in stale_fns)) if stale_fns else ""
        phases_block = PHASES_BLOCK.format(
            targets="\n".join(f"- {e['name']} (lines {e['startLine']}-{e['endLine']}, "
                               f"{e['nestedCount']} nested helper(s): "
                               f"{', '.join(c['name'] for c in outline if c.get('parent') == e['name']) or 'none'})"
                               for e, _ in stale_orchs)) if stale_orchs else ""
        prompt = PROMPT_TEMPLATE.format(path=rel, functions_block=functions_block,
                                         phases_block=phases_block, source=_numbered_source(rel))
        print(f"  {rel}: {len(stale_fns)} function summar{'y' if len(stale_fns) == 1 else 'ies'}, "
              f"{len(stale_orchs)} process breakdown(s)...", end=" ", flush=True)
        try:
            out_text = _call_claude(prompt, args.model)
            parsed = _parse_json_object(out_text)
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {e}")
            failures += 1
            continue

        got_fns = 0
        for entry, h in stale_fns:
            summary = next((r.get("summary") for r in parsed.get("functions", []) if r.get("name") == entry["name"]), None)
            if not summary:
                continue
            cache["functions"][entry["name"]] = {"hash": h, "summary": summary}
            got_fns += 1
        got_phases = 0
        for entry, h in stale_orchs:
            phases = next((r.get("phases") for r in parsed.get("phases", []) if r.get("orchestrator") == entry["name"]), None)
            if not phases:
                continue
            cache["phases"][entry["name"]] = {"hash": h, "phases": phases}
            got_phases += 1
        total_fns += got_fns
        total_phases += got_phases
        print(f"got {got_fns}/{len(stale_fns)} summaries, {got_phases}/{len(stale_orchs)} breakdowns")

    sr.SUMMARIES_PATH.write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {sr.SUMMARIES_PATH} — {total_fns} function summar{'y' if total_fns == 1 else 'ies'}, "
          f"{total_phases} process breakdown(s) (re)generated, {failures} file(s) failed. "
          f"Run structure_report.py again to refresh report.html with them.")


if __name__ == "__main__":
    main()
