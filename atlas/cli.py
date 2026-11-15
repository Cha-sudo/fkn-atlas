import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from .store import Store
from .network import Fetcher
from .ingest import extract, import_local, render_page
from .providers import capability, metadata_route
from .research import candidates, report, export_graph, add_version, add_lesson, review
from .pedagogy import annotate, compare, trajectory


def main():
    p=argparse.ArgumentParser(description="ФКН Атлас: локальная библиотека с проверяемыми источниками")
    p.add_argument("--home",help="Private data directory; default ATLAS_HOME or ~/.fkn-atlas")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init")
    seed=sub.add_parser("seed"); seed.add_argument("urls",nargs="+"); seed.add_argument("--refresh",action="store_true")
    run=sub.add_parser("run"); run.add_argument("--allow",nargs="+",required=True); run.add_argument("--budget",type=int,default=10)
    links=sub.add_parser("links"); links.add_argument("--contains",default=""); links.add_argument("--limit",type=int,default=30)
    follow=sub.add_parser("follow"); follow.add_argument("resource_id"); follow.add_argument("--refresh",action="store_true")
    imp=sub.add_parser("import"); imp.add_argument("path"); imp.add_argument("--source-url")
    ext=sub.add_parser("extract"); ext.add_argument("snapshot_id")
    find=sub.add_parser("search"); find.add_argument("query"); find.add_argument("--limit",type=int,default=10)
    cand=sub.add_parser("candidates"); cand.add_argument("query"); cand.add_argument("--limit",type=int,default=10)
    cand.add_argument("--include-plans",action="store_true")
    nodes=sub.add_parser("nodes"); nodes.add_argument("snapshot_id"); nodes.add_argument("--contains",default="")
    sub.add_parser("report"); sub.add_parser("verify"); sub.add_parser("graph")
    sub.add_parser("report-html").add_argument("output")
    plan=sub.add_parser("plan"); plan.add_argument("json_file",help="Curated version + lessons, with source nodes")
    rev=sub.add_parser("review"); rev.add_argument("json_file",help="Evidence decision; exact quote validation")
    for name in ("annotate","compare","trajectory"):
        sub.add_parser(name).add_argument("json_file")
    sub.add_parser("fragments")
    sub.add_parser("trajectories")
    adapter=sub.add_parser("provider"); adapter.add_argument("url"); adapter.add_argument("--enqueue-metadata",action="store_true")
    render=sub.add_parser("render"); render.add_argument("snapshot_id"); render.add_argument("page",type=int); render.add_argument("output")
    args=p.parse_args()
    s=Store(args.home)
    out=None
    if args.command=="init": out={"home":str(s.home),"schema":1}
    elif args.command=="seed": out=[s.enqueue(s.resource(u,"seed"),args.refresh) for u in args.urls]
    elif args.command=="run":
        if not 1<=args.budget<=100: p.error("budget must be 1..100")
        out=Fetcher(s,args.allow).run(args.budget)
    elif args.command=="links": out=s.rows("SELECT o.*,r.url,r.provider FROM occurrences o JOIN resources r ON r.id=o.target_id WHERE o.label LIKE ? OR o.context LIKE ? OR r.url LIKE ? LIMIT ?",("%"+args.contains+"%",)*3+(args.limit,))
    elif args.command=="follow":
        if not s.rows("SELECT id FROM occurrences WHERE target_id=?",(args.resource_id,)): p.error("follow requires an observed educational link; use seed for explicit sources")
        out=s.enqueue(args.resource_id,args.refresh)
    elif args.command=="import": out=import_local(s,args.path,args.source_url)
    elif args.command=="extract": out=extract(s,args.snapshot_id)
    elif args.command=="search":
        out=s.rows("SELECT c.*,bm25(chunks_fts) AS rank FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.id WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",(args.query,args.limit))
    elif args.command=="candidates": out=candidates(s,args.query,args.limit,include_plans=args.include_plans)
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
    elif args.command in {"annotate","compare","trajectory"}:
        fn={"annotate":annotate,"compare":compare,"trajectory":trajectory}[args.command]
        out=fn(s,**json.loads(Path(args.json_file).read_text(encoding="utf8")))
    elif args.command=="fragments": out=s.rows("SELECT f.*,a.analysis,a.version FROM fragments f JOIN assessments a ON a.fragment_id=f.id WHERE a.version=(SELECT max(version) FROM assessments WHERE fragment_id=f.id)")
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
