#!/usr/bin/env python3
"""Enumerate the standard library's public API surface by introspection.

Emits one JSON record per documentable entity (JSONL) for THIS interpreter, build,
and platform only -- what exists here, which differs across OS and Python version.

Stdlib only, no third-party deps. Run:
    python stdlib_introspect.py [-o out.jsonl] [--include-dunders] [--include-private]
                                [--md-summary PATH] [--min-entities N]

When --md-summary PATH is given (or the env var GITHUB_STEP_SUMMARY is set), the
stats are also rendered as Markdown there. --min-entities is a sanity gate: a build
that produces fewer records than that exits non-zero, because a near-empty dump means
something broke rather than a real result.

Per-type dunders are excluded by default (the docs cover them in the data model
section); --include-dunders keeps them, flagged with is_dunder=True.
"""
from __future__ import annotations
import sys, os, io, json, inspect, pkgutil, importlib, warnings, argparse, contextlib, platform

# Modules with import-time side effects (browser/print) or that we never document.
SKIP_MODULES = {
    "antigravity",          # opens a web browser on import
    "this",                 # prints the Zen of Python
    "__hello__", "__phello__",   # print on import
    "test",                 # CPython's own test suite
    "idlelib",              # the IDLE GUI app
    "turtledemo",           # demo scripts
    "lib2to3",              # grammar/test heavy; removed in 3.13
}
TEST_PARTS = {"test", "tests"}

def is_dunder(name):  return len(name) > 4 and name.startswith("__") and name.endswith("__")
def is_private(name): return name.startswith("_") and not is_dunder(name)

@contextlib.contextmanager
def _silenced():
    """Swallow stdout/stderr/warnings during risky imports."""
    sink = io.StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield

def safe_import(name):
    short = name.split(".")[-1]
    if name in SKIP_MODULES or short in SKIP_MODULES:
        return None
    try:
        with _silenced():
            return importlib.import_module(name)
    except KeyboardInterrupt:
        raise
    except BaseException:        # ImportError, platform gates, missing C libs, etc.
        return None

def roster(include_private):
    names = getattr(sys, "stdlib_module_names", None)
    if not names:
        sys.exit("Requires Python 3.10+ (needs sys.stdlib_module_names).")
    seen = set()
    for top in sorted(n for n in names if include_private or not n.startswith("_")):
        yield from _emit(top, include_private, seen)

def _emit(name, include_private, seen):
    if name in seen:
        return
    seen.add(name)
    short = name.split(".")[-1]
    if name in SKIP_MODULES or short in SKIP_MODULES or short in TEST_PARTS:
        return
    mod = safe_import(name)
    yield name, mod
    if mod is not None and hasattr(mod, "__path__"):
        try:
            with _silenced():
                subs = [mi.name for mi in pkgutil.iter_modules(mod.__path__, name + ".")]
        except Exception:
            subs = []
        for sub in sorted(subs):
            ss = sub.split(".")[-1]
            if (is_private(ss) and not include_private) or ss in TEST_PARTS:
                continue
            yield from _emit(sub, include_private, seen)

SEEN = {}       # id(obj) -> canonical qualname (first sighting)
RECORDS = {}
PENDING = {}    # id(obj) -> [alias qualnames seen before the canonical one]

def kind_of(obj, in_class):
    if inspect.ismodule(obj):       return "module"
    if isinstance(obj, type):
        return "exception" if issubclass(obj, BaseException) else "class"
    if isinstance(obj, property):   return "property"
    if inspect.isgetsetdescriptor(obj) or inspect.ismemberdescriptor(obj):
        return "descriptor"
    if inspect.isroutine(obj):      # function / builtin / method / method_descriptor
        return "method" if in_class else "function"
    return "data"

def get_signature(obj):
    try:
        return str(inspect.signature(obj)), "inspect"
    except (ValueError, TypeError):
        ts = getattr(obj, "__text_signature__", None)
        return (ts, "text_signature") if ts else (None, "none")

def doc_info(obj):
    own = bool(getattr(obj, "__doc__", None))
    resolved = inspect.getdoc(obj)
    first = ""
    if resolved and resolved.strip():
        first = resolved.strip().splitlines()[0][:100]
    return own, bool(resolved), first

def attach(canonical, alias):
    rec = RECORDS.get(canonical)
    if rec is not None and alias != canonical and alias not in rec["aliases"]:
        rec["aliases"].append(alias)

def note_alias(obj, qualname):
    try:
        oid = id(obj)
    except Exception:
        return
    if oid in SEEN:
        attach(SEEN[oid], qualname)
    else:
        PENDING.setdefault(oid, []).append(qualname)

def record(qualname, kind, module, parent, obj, short):
    sig, sig_src = get_signature(obj) if kind in {"function", "method", "class", "exception"} else (None, "n/a")
    own, resolved, first = doc_info(obj)
    RECORDS[qualname] = {
        "qualname": qualname, "kind": kind, "module": module, "parent": parent,
        "is_dunder": is_dunder(short),
        "signature": sig, "sig_source": sig_src,
        "doc_own": own, "doc_resolved": resolved, "doc_firstline": first,
        "aliases": [],
    }

def process(obj, qualname, parent, modname, short, in_class, dnd, priv):
    k = kind_of(obj, in_class)
    if k == "data":
        # Value-typed: identity is NOT meaningful (small ints / interned strings
        # share id() across the whole stdlib), so no id-dedup and no recursion.
        if qualname not in RECORDS:
            record(qualname, k, modname, parent, obj, short)
        return
    oid = id(obj)
    if oid in SEEN:
        attach(SEEN[oid], qualname)        # re-export under another name
        return
    SEEN[oid] = qualname
    k = kind_of(obj, in_class)
    record(qualname, k, modname, parent, obj, short)
    for a in PENDING.pop(oid, []):
        attach(qualname, a)
    # Recurse into a class's OWN members only (mirrors "document where defined").
    if k in {"class", "exception"}:
        for cname, cval in sorted(vars(obj).items()):
            if (is_dunder(cname) and not dnd) or (is_private(cname) and not priv):
                continue
            process(cval, f"{qualname}.{cname}", qualname, modname, cname, True, dnd, priv)

def walk_module(mod, dnd, priv):
    modname = mod.__name__
    # vars() (not dir()+getattr) so we don't trigger lazy __getattr__ / property side effects.
    for name, val in sorted(vars(mod).items()):
        if (is_dunder(name) and not dnd) or (is_private(name) and not priv):
            continue
        if inspect.ismodule(val):       # imported module ref (ast.sys, os.path); roster handles real modules
            continue
        owner = getattr(val, "__module__", None)
        if owner is not None and owner != modname:
            note_alias(val, f"{modname}.{name}")     # imported from elsewhere
            continue
        process(val, f"{modname}.{name}", modname, modname, name, False, dnd, priv)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="stdlib_api.jsonl")
    ap.add_argument("--include-dunders", action="store_true")
    ap.add_argument("--include-private", action="store_true")
    ap.add_argument("--md-summary", metavar="PATH",
                    help="also write the summary as Markdown here "
                         "(defaults to $GITHUB_STEP_SUMMARY when that is set)")
    ap.add_argument("--min-entities", type=int, default=5000,
                    help="sanity gate: exit non-zero if fewer than N records were "
                         "produced (a near-empty dump means the build is broken)")
    args = ap.parse_args()

    scanned, failed = 0, []
    for name, mod in roster(args.include_private):
        scanned += 1
        if mod is None:
            failed.append(name)
            continue
        own, resolved, first = doc_info(mod)
        RECORDS[name] = {
            "qualname": name, "kind": "module", "module": name,
            "parent": name.rpartition(".")[0] or None, "is_dunder": False,
            "signature": None, "sig_source": "n/a",
            "doc_own": own, "doc_resolved": resolved, "doc_firstline": first,
            "aliases": [],
        }
        try:
            walk_module(mod, args.include_dunders, args.include_private)
        except Exception as e:
            failed.append(f"{name} (walk: {e!r})")

    recs = sorted(RECORDS.values(), key=lambda r: r["qualname"])
    # Default encoding is cp1252 on Windows (crashes on non-ASCII docstrings) and text
    # mode there translates \n -> \r\n; pin UTF-8 + LF so every cell emits identical bytes.
    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

    stats = _stats(recs, scanned, failed)
    _text_summary(stats, args.output)
    md_path = args.md_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if md_path:
        _markdown_summary(stats, args.output, md_path)

    # Gate last, after the output and summaries are written, so a broken cell still
    # uploads its dump and renders a summary before the non-zero exit fails the job.
    if len(recs) < args.min_entities:
        sys.exit(f"\nSANITY GATE: only {len(recs)} records (< --min-entities "
                 f"{args.min_entities}); this build looks broken.")

def _stats(recs, scanned, failed):
    from collections import Counter
    callables = [r for r in recs if r["kind"] in {"function", "method"}]
    return {
        "n_recs": len(recs),
        "scanned": scanned,
        "failed": failed,
        "kinds": Counter(r["kind"] for r in recs),
        "n_callables": len(callables),
        "with_sig": sum(1 for r in callables if r["signature"]),
        "with_doc": sum(1 for r in recs if r["doc_resolved"]),
        "by_mod": Counter(r["qualname"].split(".")[0] for r in recs),
    }

def _pct(part, whole):
    return f"{100*part//whole}%" if whole else "n/a"

def _text_summary(s, out):
    print(f"\n=== stdlib introspection summary =========================")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    print(f"modules scanned        : {s['scanned']}  ({len(s['failed'])} not introspectable here)")
    print(f"total entities         : {s['n_recs']}")
    print(f"  by kind              : " + ", ".join(f"{k}={n}" for k, n in s['kinds'].most_common()))
    if s['n_callables']:
        print(f"callables w/ signature : {s['with_sig']}/{s['n_callables']}  ({_pct(s['with_sig'], s['n_callables'])})")
    if s['n_recs']:
        print(f"entities w/ docstring  : {s['with_doc']}/{s['n_recs']}  ({_pct(s['with_doc'], s['n_recs'])})")
    print(f"\ntop 12 modules by entity count:")
    for m, n in s['by_mod'].most_common(12):
        print(f"  {n:5d}  {m}")
    if s['failed']:
        show = ", ".join(s['failed'][:18])
        more = f"  (+{len(s['failed'])-18} more)" if len(s['failed']) > 18 else ""
        print(f"\nnot introspectable on this build ({len(s['failed'])}): {show}{more}")
    print(f"\nwrote {s['n_recs']} records -> {out}")
    print("=" * 58)

def _markdown_summary(s, out, path):
    pyver = sys.version.split()[0]
    L = [
        f"## stdlib introspection — Python {pyver} on `{sys.platform}`",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| platform | `{platform.platform()}` |",
        f"| modules scanned | {s['scanned']} |",
        f"| modules not introspectable | {len(s['failed'])} |",
        f"| total entities | {s['n_recs']} |",
    ]
    if s['n_callables']:
        L.append(f"| callables w/ signature | {s['with_sig']}/{s['n_callables']} ({_pct(s['with_sig'], s['n_callables'])}) |")
    L.append(f"| entities w/ docstring | {s['with_doc']}/{s['n_recs']} ({_pct(s['with_doc'], s['n_recs'])}) |")

    L += ["", "### Entities by kind", "", "| kind | count |", "| --- | --- |"]
    L += [f"| {k} | {n} |" for k, n in s['kinds'].most_common()]

    L += ["", "### Top modules by entity count", "", "| module | entities |", "| --- | --- |"]
    L += [f"| `{m}` | {n} |" for m, n in s['by_mod'].most_common(12)]

    failed = s['failed']
    L += ["", f"### Not introspectable on this build ({len(failed)})", ""]
    L.append(", ".join(f"`{m}`" for m in sorted(failed)) if failed else "_none_")
    L.append("")

    # Append: $GITHUB_STEP_SUMMARY is an append target, and the file is fresh per step.
    # newline="\n" keeps the summary byte-identical on the Windows runner.
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")

if __name__ == "__main__":
    main()
