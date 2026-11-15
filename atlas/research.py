"""Explicit plans, candidate retrieval and append-only evidence decisions."""
import json
import math
import re
import time
from collections import Counter
from .store import uid

LESSON_STATES = {"not_in_plan","listed_without_link","link_unavailable","obtained_unread",
                 "partial","unverified","content_checked"}
VERDICTS = {"confirmed_scope","partial","unique_addition","rejected","insufficient_data"}


def add_version(store, program, year, course, stream, source_snapshot, evidence_node):
    node = store.rows("SELECT * FROM nodes WHERE id=? AND snapshot_id=?",(evidence_node,source_snapshot))
    if not node:
        raise ValueError("Version requires a node from its source snapshot")
    ident = uid("version",program,year,course,stream,source_snapshot)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?,?,?)",
                         (ident,program,year,course,stream,source_snapshot,evidence_node,"source_checked"))
    return ident


def add_lesson(store, version_id, label, position, topics, status, evidence_node, notes=""):
    if status not in LESSON_STATES or not isinstance(topics,list):
        raise ValueError("Invalid lesson state or topics")
    node = store.rows("SELECT n.id FROM nodes n JOIN versions v ON n.snapshot_id=v.source_snapshot WHERE n.id=? AND v.id=?",(evidence_node,version_id))
    if not node:
        raise ValueError("Lesson evidence must belong to version source")
    ident = uid("lesson",version_id,evidence_node,label)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO lessons VALUES(?,?,?,?,?,?,?,?)",
                         (ident,version_id,label,position,json.dumps(topics,ensure_ascii=False),status,evidence_node,notes))
    return ident


def candidates(store, query, limit=10, exclude_resource=None, include_plans=False):
    """Content-based lexical TF-IDF + character n-grams. Ranking is NOT verification."""
    docs = store.rows("""SELECT c.*,s.sha,s.resource_id,r.url FROM chunks c
        JOIN extractions e ON e.id=c.extraction_id JOIN snapshots s ON s.id=e.snapshot_id
        JOIN resources r ON r.id=s.resource_id WHERE c.quality!='metadata_only'
        AND (? OR r.provider!='mediawiki')""",(include_plans,))
    def tokens(text):
        words = re.findall(r"[a-zа-яё0-9]{3,}",text.lower())
        return Counter(["w:"+w for w in words]+["g:"+w[i:i+4] for w in words for i in range(len(w)-3)])
    vectors = [tokens(d["text"]) for d in docs]
    df = Counter(k for v in vectors for k in v)
    q = tokens(query)
    def weight(v):
        return {k:(1+math.log(n))*math.log(1+(len(docs)+1)/(df[k]+1)) for k,n in v.items()}
    qv=weight(q)
    qnorm=math.sqrt(sum(x*x for x in qv.values())) or 1
    found=[]
    for doc,vector in zip(docs,vectors):
        if doc["resource_id"]==exclude_resource:
            continue
        v=weight(vector)
        norm=math.sqrt(sum(x*x for x in v.values())) or 1
        score=sum(x*v.get(k,0) for k,x in qv.items())/(qnorm*norm)
        if score>0:
            found.append({**doc,"score":round(score,5),"status":"candidate_only"})
    return sorted(found,key=lambda r:(-r["score"],r["id"]))[:limit]


def source_belongs_to_version(store, chunk_id, version_id):
    target=store.rows('SELECT s.resource_id FROM chunks c JOIN extractions e ON e.id=c.extraction_id JOIN snapshots s ON s.id=e.snapshot_id WHERE c.id=?',(chunk_id,))
    if not target: return False
    reachable=store.rows('''WITH RECURSIVE edges(source,target) AS (
        SELECT s.resource_id,o.target_id FROM occurrences o JOIN snapshots s ON s.id=o.snapshot_id
        UNION SELECT source_id,target_id FROM source_edges), reached(id) AS (
        SELECT s.resource_id FROM versions v JOIN snapshots s ON s.id=v.source_snapshot WHERE v.id=?
        UNION SELECT edges.target FROM edges JOIN reached ON edges.source=reached.id)
        SELECT id FROM reached WHERE id=?''',(version_id,target[0]['resource_id']))
    return bool(reachable)


def review(store, lesson_id, verdict, covered, uncovered, reason, reviewer, evidence, transfer_authorization=None):
    if verdict not in VERDICTS or not reason.strip() or not reviewer.strip():
        raise ValueError("Decision, reason and reviewer required")
    lesson = store.rows("SELECT * FROM lessons WHERE id=?",(lesson_id,))
    if not lesson:
        raise ValueError("Unknown lesson")
    target_version=store.rows('SELECT * FROM versions WHERE id=?',(lesson[0]['version_id'],))[0]
    expected = set(json.loads(lesson[0]["topics"]))
    if set(covered) & set(uncovered) or set(covered)|set(uncovered) != expected:
        raise ValueError("Covered and uncovered must partition the explicit lesson topics")
    if verdict == "confirmed_scope" and (uncovered or not covered):
        raise ValueError("Confirmed scope needs all declared topics, without extrapolation")
    if verdict in {"confirmed_scope","partial","unique_addition"} and not evidence:
        raise ValueError("Positive decisions require evidence")
    validated=[]
    for item in evidence:
        chunk = store.rows("SELECT * FROM chunks WHERE id=?",(item["chunk_id"],))
        if not chunk or not item["quote"].strip() or item["quote"] not in chunk[0]["text"]:
            raise ValueError("Evidence quote must be an exact fragment of the stored source")
        if chunk[0]["quality"] == "metadata_only":
            raise ValueError("Metadata cannot prove content correspondence")
        if not item.get("check_method","").strip():
            raise ValueError("Independent source check method required")
        source_vid=item.get('source_version_id',target_version['id'])
        if not source_belongs_to_version(store,item['chunk_id'],source_vid):
            raise ValueError('Evidence needs a source version with a traceable provenance path')
        source_version=store.rows('SELECT * FROM versions WHERE id=?',(source_vid,))[0]
        cross_course=any(source_version[k]!=target_version[k] for k in ('program','course'))
        if cross_course and verdict in {'confirmed_scope','partial'}:
            authorization=transfer_authorization or {}
            nodes=store.rows('SELECT text FROM nodes WHERE id=? AND snapshot_id=?',(authorization.get('node_id'),target_version['source_snapshot']))
            if not nodes or not authorization.get('quote') or authorization['quote'] not in nodes[0]['text'] or not authorization.get('reason'):
                raise ValueError('Cross-course replacement requires an explicit instruction in the target course source; otherwise supplementary reading only')
        item={**item,'source_version_id':source_vid}
        validated.append(item)
    with store.db:
        version=store.db.execute("SELECT coalesce(max(version),0)+1 FROM reviews WHERE lesson_id=?",(lesson_id,)).fetchone()[0]
        ident=uid("review",lesson_id,version)
        store.db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?)",(ident,lesson_id,version,verdict,
            json.dumps(covered,ensure_ascii=False),json.dumps(uncovered,ensure_ascii=False),reason,reviewer,time.time()))
        for item in validated:
            store.db.execute("INSERT INTO evidence VALUES(?,?,?,?)",(ident,item["chunk_id"],item["quote"],item["check_method"]))
            store.db.execute('INSERT OR IGNORE INTO review_sources VALUES(?,?,?,?)',(ident,item['chunk_id'],item['source_version_id'],json.dumps(transfer_authorization,ensure_ascii=False) if transfer_authorization else None))
    return ident


def report(store):
    tables = ["resources","snapshots","occurrences","nodes","chunks","versions","lessons","reviews","fragments","assessments","comparisons","trajectories"]
    counts = {t:store.db.execute("SELECT count(*) FROM "+t).fetchone()[0] for t in tables}
    counts["resources_obtained"] = store.db.execute("SELECT count(DISTINCT resource_id) FROM snapshots").fetchone()[0]
    counts["resources_attempted"] = store.db.execute("SELECT count(DISTINCT resource_id) FROM attempts").fetchone()[0]
    counts["resources_unattempted"] = counts["resources"]-counts["resources_attempted"]
    return {"counts":counts,"scope":"This local sample only; no claim of all-Wiki coverage",
        "extraction_states":store.rows("SELECT status,count(*) AS n FROM extractions GROUP BY status"),
        "lesson_states":store.rows("SELECT status,count(*) AS n FROM lessons GROUP BY status"),
        "latest_decisions":store.rows("SELECT r.* FROM reviews r WHERE version=(SELECT max(version) FROM reviews WHERE lesson_id=r.lesson_id)"),
        "queue":store.rows("SELECT state,count(*) AS n FROM jobs GROUP BY state"),
        "providers":store.rows("SELECT provider,count(*) AS n FROM resources GROUP BY provider")}


def export_graph(store):
    # Provenance graph, deliberately not an automatically inferred prerequisite graph.
    return {"nodes":store.rows("SELECT id,url,provider,role FROM resources"),
            "edges":store.rows("SELECT o.id,s.resource_id AS source,o.target_id AS target,o.label,o.headings,o.path,o.snapshot_id FROM occurrences o JOIN snapshots s ON s.id=o.snapshot_id"),
            "derivations":store.rows("SELECT * FROM source_edges"),"kind":"observed_source_graph"}
