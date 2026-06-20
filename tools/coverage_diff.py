#!/usr/bin/env python3
"""Diff the introspected stdlib surface against docs.python.org inventories.

For each Python minor in the union, fetch that minor's Sphinx ``objects.inv``,
keep the ``py`` domain, and split the OS-unioned introspected surface into
covered / missing-from-official-docs / docs-only. The target version's gap -- the
stdlib API the official docs.python.org reference does not document -- is the
headline artifact.

    python coverage_diff.py UNION.jsonl [--target-version 3.14]
        [-o official_docs_coverage_by_version.json] [--gap-out PATH]
        [--inventory-dir DIR] [--md-summary PATH]

The inventory carries no version metadata, so it cannot say when an entity was
added/removed -- those deltas come from the matrix (merge_summary.py), not here.
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import sphobjinv as soi

type Record = dict[str, Any]

INVENTORY_URL = "https://docs.python.org/{version}/objects.inv"
DEV_INVENTORY_URL = "https://docs.python.org/dev/objects.inv"
HTTP_NOT_FOUND = 404

# Introspected prefix -> docs-canonical prefix. The docs index posix|nt as os.*,
# posixpath|ntpath|genericpath as os.path.*, and builtins members unprefixed.
# Extend from the docs-only bucket: a cluster of real names there is usually
# another re-export alias to add.
PREFIX_ALIASES = {
    "builtins.": "",
    "posixpath.": "os.path.",
    "ntpath.": "os.path.",
    "genericpath.": "os.path.",
    "posix.": "os.",
    "nt.": "os.",
}
MODULE_ALIASES = {
    "posixpath": "os.path",
    "ntpath": "os.path",
    "genericpath": "os.path",
    "posix": "os",
    "nt": "os",
}

CELL_VERSION = re.compile(r"-py([0-9][^-]*)$")
TOP_MODULES = 15


def version_key(version: str) -> tuple[int, ...]:
    """Sort key for a version string: ``'3.14'`` -> ``(3, 14)``."""
    numbers = re.findall(r"\d+", version)
    return tuple(int(number) for number in numbers[:2]) if numbers else (0,)


def version_label(version: str) -> str:
    """Canonical ``X.Y`` label for a version string."""
    return ".".join(str(part) for part in version_key(version))


def cell_version(cell: str) -> str | None:
    """Extract the ``X.Y`` minor from a ``...-py3.14`` cell id, or ``None``."""
    match = CELL_VERSION.search(cell)
    return version_label(match.group(1)) if match else None


def normalize(qualname: str) -> str:
    """Map an introspected qualname onto its docs-canonical spelling."""
    if qualname in MODULE_ALIASES:
        return MODULE_ALIASES[qualname]
    for prefix, replacement in PREFIX_ALIASES.items():
        if qualname.startswith(prefix):
            return replacement + qualname[len(prefix) :]
    return qualname


def normalize_module(module: str) -> str:
    """Map an introspected module name onto its docs-canonical spelling."""
    return MODULE_ALIASES.get(module, module)


def percent(part: int, whole: int) -> float:
    """Return ``part / whole`` as a percentage rounded to one decimal."""
    return round(100 * part / whole, 1) if whole else 0.0


def load_union(path: str) -> list[Record]:
    """Read the union JSONL into a list of records."""
    records = []
    with Path(path).open(encoding="utf-8") as source_file:
        for line in source_file:
            stripped = line.strip()
            if stripped:
                records.append(json.loads(stripped))
    return records


def http_get(url: str, attempts: int = 4) -> bytes:
    """GET ``url``, retrying transient URL errors with exponential backoff."""
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "coverage-diff"})
            with urllib.request.urlopen(request, timeout=30) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError:
            raise  # 4xx/5xx: caller decides (404 -> dev fallback)
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError  # unreachable while attempts >= 1


def documented_names(version: str, inventory_dir: str | None = None) -> tuple[set[str], bool]:
    """Return (set of py-domain names, used_dev_fallback) for one minor."""
    if inventory_dir:
        local = Path(inventory_dir) / f"{version}.inv"
        if local.exists():
            inventory = soi.Inventory(zlib=local.read_bytes())  # ty: ignore[unknown-argument]
            return {obj.name for obj in inventory.objects if obj.domain == "py"}, False
    used_dev = False
    try:
        data = http_get(INVENTORY_URL.format(version=version))
    except urllib.error.HTTPError as error:
        if error.code != HTTP_NOT_FOUND:
            raise
        data = http_get(DEV_INVENTORY_URL)  # in-dev minor with no numbered inventory yet
        used_dev = True
    inventory = soi.Inventory(zlib=data)  # ty: ignore[unknown-argument]
    return {obj.name for obj in inventory.objects if obj.domain == "py"}, used_dev


def build_surface(union: list[Record], version: str) -> dict[str, Record]:
    """norm-qualname -> representative record, for one minor, OS-unioned."""
    surface: dict[str, Record] = {}
    for record in union:
        if record.get("is_dunder"):
            continue
        versions = {cell_version(cell) for cell in record.get("cells", [])}
        if version not in versions:
            continue
        name = normalize(record["qualname"])
        if name not in surface or record["qualname"] == name:
            surface[name] = record
    return surface


def diff_version(
    union: list[Record],
    version: str,
    inventory_dir: str | None,
) -> tuple[Record, dict[str, Record], set[str], set[str]]:
    """Split one minor's surface into (summary, surface, missing, docs-only)."""
    surface = build_surface(union, version)
    documented_upstream, used_dev = documented_names(version, inventory_dir)
    surface_names = set(surface)
    covered = surface_names & documented_upstream
    missing_from_official_docs = surface_names - documented_upstream
    docs_only = documented_upstream - surface_names

    by_module: dict[str, Record] = {}
    for name, record in surface.items():
        module = normalize_module(record["module"])
        bucket = by_module.setdefault(module, {"surface": 0, "covered": 0})
        bucket["surface"] += 1
        if name in documented_upstream:
            bucket["covered"] += 1
    for bucket in by_module.values():
        bucket["coverage_pct"] = percent(bucket["covered"], bucket["surface"])

    summary = {
        "surface": len(surface_names),
        "documented": len(documented_upstream),
        "covered": len(covered),
        "backlog": len(missing_from_official_docs),
        "docs_only": len(docs_only),
        "coverage_pct": percent(len(covered), len(surface_names)),
        "used_dev_fallback": used_dev,
        "by_module": dict(sorted(by_module.items())),
    }
    return summary, surface, missing_from_official_docs, docs_only


def gap_records(surface: dict[str, Record], missing_from_official_docs: set[str]) -> list[Record]:
    """Build the sorted gap rows for the entities missing from the official docs."""
    rows = []
    for name in missing_from_official_docs:
        record = surface[name]
        rows.append(
            {
                "qualname": name,
                "kind": record["kind"],
                "module": normalize_module(record["module"]),
                "signature": record.get("signature"),
                "has_docstring": bool(record.get("doc_resolved")),
                "is_data": record["kind"] == "data",
            },
        )
    rows.sort(key=lambda row: (row["module"], row["qualname"]))
    return rows


def default_target(versions: list[str]) -> str:
    """Latest stable minor: the penultimate entry (the highest is the in-dev branch)."""
    stable = versions[:-1] or versions  # drop the in-dev highest, unless it is all we have
    return stable[-1]


def main() -> None:
    """Parse arguments, diff every minor in the union, and write the outputs."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("union", help="stdlib_api_union.jsonl from the aggregate job")
    parser.add_argument(
        "--target-version",
        help="minor to build the docs.python.org gap for (default: latest stable = second-highest in the matrix)",
    )
    parser.add_argument("-o", "--output", default="official_docs_coverage_by_version.json")
    parser.add_argument("--gap-out", metavar="PATH", help="default: official_docs_gap_<target>.jsonl")
    parser.add_argument(
        "--inventory-dir",
        metavar="DIR",
        help="read <minor>.inv from here before fetching (offline/cached runs)",
    )
    parser.add_argument(
        "--md-summary",
        metavar="PATH",
        help="write the Markdown report here (defaults to $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args()

    union = load_union(args.union)
    versions = sorted(
        {minor for record in union for cell in record.get("cells", []) if (minor := cell_version(cell))},
        key=version_key,
    )

    results: dict[str, Record] = {}
    surfaces: dict[str, dict[str, Record]] = {}
    gaps: dict[str, set[str]] = {}
    docs_onlys: dict[str, set[str]] = {}
    for version in versions:
        summary, surface, missing, docs_only = diff_version(union, version, args.inventory_dir)
        results[version] = summary
        surfaces[version], gaps[version], docs_onlys[version] = surface, missing, docs_only
        flag = "  [dev fallback]" if summary["used_dev_fallback"] else ""
        print(
            f"  {version:8s} surface={summary['surface']:6d} covered={summary['covered']:6d} "
            f"missing={summary['backlog']:6d} docs-only={summary['docs_only']:6d} "
            f"({summary['coverage_pct']}%)" + flag,
        )

    target = args.target_version or (default_target(versions) if versions else None)
    target_surface = surfaces.get(target, {}) if target else {}
    target_missing = gaps.get(target, set()) if target else set()
    target_docs_only = docs_onlys.get(target, set()) if target else set()

    coverage = {"target_version": target, "versions": results}
    with Path(args.output).open("w", encoding="utf-8", newline="\n") as out_file:
        json.dump(coverage, out_file, indent=2)
        out_file.write("\n")

    gap_path = args.gap_out or f"official_docs_gap_{target}.jsonl"
    rows = gap_records(target_surface, target_missing)
    with Path(gap_path).open("w", encoding="utf-8", newline="\n") as out_file:
        out_file.writelines(json.dumps(row) + "\n" for row in rows)

    report(versions, results, target, rows, target_docs_only, args.output, gap_path, args)


def report(
    versions: list[str],
    results: dict[str, Record],
    target: str | None,
    gap_rows: list[Record],
    docs_only: set[str],
    output_path: str,
    gap_path: str,
    args: argparse.Namespace,
) -> None:
    """Render the run as a Markdown report and a console summary."""
    data_entries = sum(1 for row in gap_rows if row["is_data"])
    lines = ["# docs.python.org coverage — stdlib API missing from the official reference", ""]
    if not versions:
        no_versions = (
            "> **No versions found in the union.** The aggregate step produced "
            "no cells, or the union schema is missing `cells`."
        )
        lines += [no_versions, ""]
    else:
        intro = (
            "Each `undocumented` count is the introspected stdlib surface minus "
            "docs.python.org's `py` inventory, per minor (OS-unioned) — the API the "
            "official CPython reference does not document. The inventory has no version "
            "metadata; added/removed deltas come from the matrix, not from here."
        )
        lines += [
            f"Target version (gap): **{target}**.",
            "",
            intro,
            "",
            "## Coverage by version",
            "",
            "| version | surface | covered | undocumented | docs-only | coverage |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for version in versions:
            stats = results[version]
            marks = " ⭐" if version == target else (" (dev)" if stats["used_dev_fallback"] else "")
            lines.append(
                f"| {version}{marks} | {stats['surface']} | {stats['covered']} "
                f"| {stats['backlog']} | {stats['docs_only']} | {stats['coverage_pct']}% |",
            )
        lines.append("")

        lines += [
            f"## Missing from the official {target} docs — {len(gap_rows)} undocumented",
            "",
            f"Reference-entry core (callables/classes/etc.): **{len(gap_rows) - data_entries}**; "
            f"`data` entries: **{data_entries}**.",
            "",
            "| kind | count |",
            "| --- | ---: |",
        ]
        for kind, count in Counter(row["kind"] for row in gap_rows).most_common():
            lines.append(f"| {kind} | {count} |")
        lines.append("")

        module_stats = results.get(target, {}).get("by_module", {}) if target else {}
        per_module = Counter(row["module"] for row in gap_rows)
        lines += [
            f"## Top {TOP_MODULES} modules by undocumented count ({target})",
            "",
            "| module | undocumented | surface | coverage |",
            "| --- | ---: | ---: | ---: |",
        ]
        for module, count in per_module.most_common(TOP_MODULES):
            stats = module_stats.get(module, {})
            lines.append(f"| `{module}` | {count} | {stats.get('surface', '?')} | {stats.get('coverage_pct', '?')}% |")
        lines.append("")

        if docs_only:
            docs_only_note = (
                "In the official docs inventory but not introspected: removed/renamed "
                "API docs.python.org still lists, or names this run failed to "
                "enumerate (normalization QA signal)."
            )
            lines += [f"## Docs-only — {target} ({len(docs_only)})", "", docs_only_note, ""]

    markdown_path = args.md_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if markdown_path:
        with Path(markdown_path).open("a", encoding="utf-8", newline="\n") as summary_file:
            summary_file.write("\n".join(lines) + "\n")

    print("\n=== docs.python.org coverage summary =====================")
    if versions:
        print(f"target {target}; versions: {', '.join(versions)}")
        print(f"official docs gap: {len(gap_rows)} undocumented ({data_entries} data)")
    else:
        print("no versions found in union")
    print(f"wrote {output_path} and {gap_path}")
    print("=" * 58)


if __name__ == "__main__":
    main()
