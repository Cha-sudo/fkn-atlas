"""Offline demo, bounded reconnaissance and portable evidence exports."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .storage import Store
from .crawler import run_demo, run_scout
from .resources import Catalog, YandexDiskPublicAdapter, PUBLIC_EXAMPLE, extract_resource
from .providers import YouTubeResourceAdapter, GitHubArchiveAdapter
from .ingest import import_local_file
from .reconcile import Reconciler


def main(argv=None):
    parser = argparse.ArgumentParser(description="FKN Atlas — provenance-first pilot")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("demo", "scout", "report", "export", "yandex-scan", "yandex-fetch", "yandex-probe", "resources", "extract", "youtube-probe", "github-archive", "import-file", "reconcile-import", "reconcile-report", "reconcile-candidates"):
        item = sub.add_parser(name)
        item.add_argument("--db", default=f"runtime/{'demo' if name == 'demo' else 'pilot'}.sqlite")
        if name == "scout":
            item.add_argument("--max-pages", type=int, default=8)
            item.add_argument("--max-depth", type=int, default=2)
            item.add_argument("--delay", type=float, default=1.5)
        if name == "export":
            item.add_argument("--output", required=True)
        if name in ("yandex-scan", "yandex-fetch", "yandex-probe"):
            item.add_argument("--public-url", default=PUBLIC_EXAMPLE)
        if name == "yandex-scan":
            item.add_argument("--max-pages", type=int, default=20)
            item.add_argument("--max-depth", type=int, default=2)
            item.add_argument("--page-size", type=int, default=100)
        if name == "yandex-fetch":
            item.add_argument("--path", required=True)
            item.add_argument("--max-mib", type=int, default=16)
            item.add_argument("--no-extract", action="store_true")
        if name == "extract":
            item.add_argument("--resource-id", required=True)
        if name == "youtube-probe":
            item.add_argument("--video-id", required=True)
        if name == "github-archive":
            item.add_argument("--repository", required=True)
            item.add_argument("--source-url", required=True)
            item.add_argument("--max-mib", type=int, default=8)
        if name == "import-file":
            item.add_argument("--filename", required=True)
            item.add_argument("--source-url", required=True)
            item.add_argument("--discovered-on", required=True)
            item.add_argument("--expected-sha256")
            item.add_argument("--expected-size", type=int)
            item.add_argument("--acquisition", choices=("local_import", "google_drive_connector"), default="local_import")
        if name == "reconcile-import":
            item.add_argument("--dossier", required=True)
        if name == "reconcile-report":
            item.add_argument("--dossier-id")
        if name == "reconcile-candidates":
            item.add_argument("--material-id", required=True)
            item.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    if args.action in ("report", "export", "import-file", "reconcile-import", "reconcile-report", "reconcile-candidates") and not Path(args.db).is_file():
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
        elif args.action == "yandex-scan":
            stats = YandexDiskPublicAdapter(store, args.public_url).scan(args.max_pages, args.max_depth, args.page_size)
        elif args.action == "yandex-fetch":
            stats = YandexDiskPublicAdapter(store, args.public_url).fetch(args.path, args.max_mib*1024*1024, not args.no_extract)
        elif args.action == "yandex-probe":
            stats = YandexDiskPublicAdapter(store, args.public_url).probe()
        elif args.action == "resources":
            stats = Catalog(store).stats()
        elif args.action == "extract":
            extract_resource(store, args.resource_id)
            stats = Catalog(store).stats()
        elif args.action == "youtube-probe":
            stats = YouTubeResourceAdapter(store).probe(args.video_id)
        elif args.action == "github-archive":
            stats = GitHubArchiveAdapter(store).fetch(args.repository, args.source_url, args.max_mib*1024*1024)
        elif args.action == "import-file":
            stats = import_local_file(store, args.filename, args.source_url, args.discovered_on,
                expected_sha256=args.expected_sha256, expected_size=args.expected_size, acquisition=args.acquisition)
        elif args.action == "reconcile-import":
            stats = Reconciler(store).import_dossier(json.loads(Path(args.dossier).read_text(encoding="utf-8")))
        elif args.action == "reconcile-report":
            stats = Reconciler(store).report(args.dossier_id)
        elif args.action == "reconcile-candidates":
            stats = Reconciler(store).rank_candidates(args.material_id, args.limit)
        else:
            stats = store.stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    finally:
        store.close()
