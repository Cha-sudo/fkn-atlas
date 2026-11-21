import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from .store import Store
from .network import Fetcher
from .ingest import extract, import_local, render_page
from .providers import capability, metadata_route
from .research import candidates, report, export_graph, add_version, add_lesson, review, bind_material
from .pedagogy import FUNCTIONS, annotate, compare, find_fragments, trajectory


def main():
    p=argparse.ArgumentParser(description="ФКН Атлас: локальная библиотека с проверяемыми источниками")
    p.add_argument("--home",help="Private data directory; default ATLAS_HOME or ~/.fkn-atlas")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init")
    seed=sub.add_parser("seed"); seed.add_argument("urls",nargs="+"); seed.add_argument("--refresh",action="store_true")
    run=sub.add_parser("run"); run.add_argument("--allow",nargs="+",required=True); run.add_argument("--budget",type=int,default=10)
    crawl=sub.add_parser('crawl',help='Resume bounded traversal from an explicit educational root')
    crawl.add_argument('url'); crawl.add_argument('--budget',type=int,default=10)
    crawl.add_argument('--max-depth',type=int,default=4); crawl.add_argument('--max-frontier',type=int,default=5000)
    for runner in (run,crawl):
        runner.add_argument('--max-requests',type=int,default=50); runner.add_argument('--max-total-mib',type=int,default=128)
        runner.add_argument('--max-file-mib',type=int,default=1024); runner.add_argument('--seconds',type=int,default=300)
        runner.add_argument('--max-storage-gib',type=int,default=20); runner.add_argument('--reserve-free-mib',type=int,default=1024)
    sub.add_parser('transfers',help='Incomplete local bytes and validators; not library documents')
    sub.add_parser('resume',help='Requeue a deferred/retry job preserving .part').add_argument('resource_id')
    sub.add_parser('frontier').add_argument('--limit',type=int,default=100)
    sub.add_parser('library').add_argument('output')
    sub.add_parser('catalog',help='Write the private program and course catalog as HTML').add_argument('output')
    links=sub.add_parser("links"); links.add_argument("--contains",default=""); links.add_argument("--limit",type=int,default=30)
    follow=sub.add_parser("follow"); follow.add_argument("resource_id"); follow.add_argument("--refresh",action="store_true")
    imp=sub.add_parser("import"); imp.add_argument("path"); imp.add_argument("--source-url")
    listing=sub.add_parser('drive-listing',help='Import a normalized response from an existing Drive connector')
    listing.add_argument('url'); listing.add_argument('json_file')
    ext=sub.add_parser("extract"); ext.add_argument("snapshot_id")
    ext.add_argument('--reprocess',action='store_true',help='Append extraction under the current engine; preserve previous versions')
    find=sub.add_parser("search"); find.add_argument("query"); find.add_argument("--limit",type=int,default=10)
    cand=sub.add_parser("candidates"); cand.add_argument("query"); cand.add_argument("--limit",type=int,default=10)
    cand.add_argument('--lesson',help='Restrict gap candidates to reviewed materials of the same course; otherwise supplementary discovery')
    cand.add_argument("--include-plans",action="store_true")
    nodes=sub.add_parser("nodes"); nodes.add_argument("snapshot_id"); nodes.add_argument("--contains",default="")
    sub.add_parser("report"); sub.add_parser("verify"); sub.add_parser("graph")
    sub.add_parser("report-html").add_argument("output")
    plan=sub.add_parser("plan"); plan.add_argument("json_file",help="Curated version + lessons, with source nodes")
    rev=sub.add_parser("review"); rev.add_argument("json_file",help="Evidence decision; exact quote validation")
    for name in ("annotate","compare","trajectory","bind"):
        sub.add_parser(name).add_argument("json_file")
    sub.add_parser("fragments")
    assessed = sub.add_parser("find-fragments", help="Search reviewed fragments in versions of the lesson's course")
    assessed.add_argument("--lesson", required=True)
    assessed.add_argument("--function", choices=sorted(FUNCTIONS))
    assessed.add_argument("--outcome", help="Exact learning outcome from a fragment assessment")
    assessed.add_argument("--limit", type=int, default=20)
    sub.add_parser("trajectories")
    adapter=sub.add_parser("provider"); adapter.add_argument("url"); adapter.add_argument("--enqueue-metadata",action="store_true")
    render=sub.add_parser("render"); render.add_argument("snapshot_id"); render.add_argument("page",type=int); render.add_argument("output")
    args=p.parse_args()
    s=Store(args.home)
    out=None
    if args.command=="init": out={"home":str(s.home),"schema":4}
    elif args.command=="seed": out=[s.enqueue(s.resource(u,"seed"),args.refresh) for u in args.urls]
    elif args.command in {'crawl','run'}:
        from .crawl import Crawl
        if not (1<=args.budget<=1000 and 1<=args.max_requests<=2000 and 1<=args.max_total_mib<=65536
                and 1<=args.max_file_mib<=32768 and 1<=args.seconds<=3600
                and 1<=args.max_storage_gib<=4096 and 64<=args.reserve_free_mib<=1048576):
            p.error('Invalid limits: file 1..32768 MiB, storage 1..4096 GiB, reserve >=64 MiB, time <=3600 s')
        crawler=Crawl(s,args.url,args.max_depth,args.max_frontier) if args.command=='crawl' else None
        fetcher=Fetcher(s,[] if crawler else args.allow,max_bytes=args.max_file_mib*1024*1024)
        fetcher.max_storage_bytes=args.max_storage_gib*1024**3
        fetcher.min_free_bytes=args.reserve_free_mib*1024**2
        try: out=fetcher.run(args.budget,crawler,args.max_requests,args.max_total_mib*1024*1024,args.seconds)
        finally: fetcher.client.close()
    elif args.command=='transfers':
        out=s.rows('SELECT t.*,r.url FROM transfers t JOIN resources r ON r.id=t.resource_id')
    elif args.command=='resume': out=s.resume(args.resource_id)
    elif args.command=='frontier':
        out=s.rows('''SELECT e.*,r.url FROM crawl_edges e JOIN resources r ON r.id=e.target_id
            WHERE e.state!='admitted' ORDER BY e.state,e.reason LIMIT ?''',(args.limit,))
    elif args.command=='library':
        from .presentation import write_library
        out=write_library(s,args.output)
    elif args.command=='catalog':
        from .presentation import write_catalog
        out=write_catalog(s,args.output)
    elif args.command=="links": out=s.rows("SELECT o.*,r.url,r.provider FROM occurrences o JOIN resources r ON r.id=o.target_id WHERE o.label LIKE ? OR o.context LIKE ? OR r.url LIKE ? LIMIT ?",("%"+args.contains+"%",)*3+(args.limit,))
    elif args.command=="follow":
        if not s.rows("SELECT id FROM occurrences WHERE target_id=?",(args.resource_id,)): p.error("follow requires an observed educational link; use seed for explicit sources")
        out=s.enqueue(args.resource_id,args.refresh)
    elif args.command=="import": out=import_local(s,args.path,args.source_url)
    elif args.command=='drive-listing':
        from .connectors import drive_listing
        out=drive_listing(s,args.url,json.loads(Path(args.json_file).read_text(encoding='utf8')))
    elif args.command=="extract": out=extract(s,args.snapshot_id,reprocess=args.reprocess)
    elif args.command=="search":
        out=s.rows("SELECT c.*,bm25(chunks_fts) AS rank FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.id JOIN extractions e ON e.id=c.extraction_id WHERE chunks_fts MATCH ? AND e.created=(SELECT max(e2.created) FROM extractions e2 WHERE e2.snapshot_id=e.snapshot_id) ORDER BY rank LIMIT ?",(args.query,args.limit))
    elif args.command=="candidates": out=candidates(s,args.query,args.limit,include_plans=args.include_plans,lesson_id=args.lesson)
    elif args.command=="nodes": out=s.rows("SELECT * FROM nodes WHERE snapshot_id=? AND text LIKE ? ORDER BY ordinal",(args.snapshot_id,"%"+args.contains+"%"))
    elif args.command=="report": out=report(s)
    elif args.command=="report-html":
        from .presentation import write_report
        out=write_report(s,args.output)
    elif args.command=="verify":
        out=s.verify()
        print(json.dumps(out,ensure_ascii=False,indent=2))
        raise SystemExit(0 if out["sqlite"]=="ok" and not out["bad_objects"] and not out["foreign_keys"] else 1)
    elif args.command=="graph": out=export_graph(s)
    elif args.command=="plan":
        obj=json.loads(Path(args.json_file).read_text(encoding="utf8"))
        vid=add_version(s,**obj["version"])
        out={"version":vid,"lessons":[add_lesson(s,version_id=vid,**x) for x in obj["lessons"]]}
    elif args.command=="review": out=review(s,**json.loads(Path(args.json_file).read_text(encoding="utf8")))
    elif args.command in {"annotate","compare","trajectory","bind"}:
        fn={"annotate":annotate,"compare":compare,"trajectory":trajectory,"bind":bind_material}[args.command]
        out=fn(s,**json.loads(Path(args.json_file).read_text(encoding="utf8")))
    elif args.command=="fragments": out=s.rows("SELECT f.*,a.analysis,a.version FROM fragments f JOIN assessments a ON a.fragment_id=f.id WHERE a.version=(SELECT max(version) FROM assessments WHERE fragment_id=f.id)")
    elif args.command=="find-fragments":
        out = find_fragments(s,args.lesson,args.function,args.outcome,args.limit)
    elif args.command=="trajectories": out=s.rows("SELECT * FROM trajectories")
    elif args.command=="provider":
        out=capability(args.url)
        if args.enqueue_metadata and metadata_route(args.url):
            rid=s.resource(args.url,"provider_probe")
            meta=s.resource(metadata_route(args.url),"provider_metadata:"+rid)
            out["job"]=s.enqueue(meta)
    elif args.command=="render": render_page(s,args.snapshot_id,args.page,args.output); out={"output":args.output}
    print(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
