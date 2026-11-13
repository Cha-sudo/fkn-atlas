"""Offline demo, bounded reconnaissance and portable evidence exports."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .storage import Store
from .crawler import run_demo, run_scout


def main():
    parser = argparse.ArgumentParser(description="FKN Atlas — provenance-first pilot")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("demo", "scout", "report", "export"):
        item = sub.add_parser(name)
        item.add_argument("--db", default=f"runtime/{'demo' if name == 'demo' else 'pilot'}.sqlite")
        if name == "scout":
            item.add_argument("--max-pages", type=int, default=8)
            item.add_argument("--max-depth", type=int, default=2)
            item.add_argument("--delay", type=float, default=1.5)
        if name == "export":
            item.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.action in ("report", "export") and not Path(args.db).is_file():
        parser.error("Database does not exist; run demo or scout first")
    store = Store(args.db)
    try:
        if args.action == "demo":
            stats = run_demo(store, Path(__file__).resolve().parents[2] / "fixtures")
        elif args.action == "scout":
            stats = run_scout(store, args.max_pages, args.max_depth, args.delay)
        elif args.action == "export":
            store.export(args.output)
            stats = {"export": args.output, **store.stats()}
        else:
            stats = store.stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        store.close()
