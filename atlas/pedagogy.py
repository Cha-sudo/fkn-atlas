"""Individual fragment assessments and pedagogical routes, never a scalar quality score.

Codex/a reviewer supplies content judgments grounded in source anchors. This module validates
and preserves them; it does not impersonate an autonomous semantic or mathematical evaluator.
"""
import json
import time
from .store import uid

FUNCTIONS={"introduction","intuition","definition","theoretical_justification","proof",
           "method_demonstration","application","worked_problems","common_errors","deepening","assessment"}
RELATIONS={"full_replacement","partial_replacement","clearer_explanation","deeper_theory",
           "additional_practice","alternative_approach","unique_content","insufficient_data","rejected"}
FIELDS={"functions","completeness","depth","correctness","clarity","sequence","proofs","examples",
        "illustrations","exercises","prerequisites","learning_outputs","uniqueness","source_quality",
        "limitations","confidence","check_method"}


def validate_anchors(store, anchors):
    if not anchors:
        raise ValueError("A semantic fragment needs exact source anchors")
    for a in anchors:
        rows=store.rows("SELECT * FROM chunks WHERE id=?",(a["chunk_id"],))
        if not rows or not a.get("quote") or a["quote"] not in rows[0]["text"]:
            raise ValueError("Anchor must quote an existing extracted fragment exactly")
        if rows[0]["quality"]=="metadata_only":
            raise ValueError("Provider metadata is not educational content")


def annotate(store, title, topic, subtopics, anchors, analysis, reviewer):
    validate_anchors(store,anchors)
    if set(analysis)!=FIELDS or not topic.strip() or not reviewer.strip():
        raise ValueError("All pedagogical dimensions required; use explicit 'unverified' where needed")
    if not analysis["functions"] or not set(analysis["functions"])<=FUNCTIONS:
        raise ValueError("Specify one or more actual educational functions")
    if analysis["confidence"] not in {"high","medium","low","unknown"}:
        raise ValueError("Confidence is categorical, not a quality rating")
    if not analysis["limitations"] or not analysis["check_method"]:
        raise ValueError("Analysis boundaries and source check method required")
    for key in ("prerequisites","learning_outputs"):
        if not isinstance(analysis[key],list): raise ValueError(key+" must be a list")
    fid=uid("fragment",topic,anchors)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO fragments VALUES(?,?,?,?,?,?)",(fid,title,topic,
            json.dumps(subtopics,ensure_ascii=False),json.dumps(anchors,ensure_ascii=False),time.time()))
        version=store.db.execute("SELECT coalesce(max(version),0)+1 FROM assessments WHERE fragment_id=?",(fid,)).fetchone()[0]
        aid=uid(fid,version)
        store.db.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?)",(aid,fid,version,json.dumps(analysis,ensure_ascii=False),reviewer,time.time()))
    return {"fragment_id":fid,"assessment_id":aid,"version":version}


def compare(store, left_fragment, right_fragment, relation, dimensions, reason, limitations):
    if relation not in RELATIONS or not dimensions or not reason or not limitations:
        raise ValueError("Comparison needs an explicit relation, dimensions, reason and limitations")
    if relation in {'full_replacement','partial_replacement'}:
        from .research import source_belongs_to_version
        scopes=[]
        for fid in (left_fragment,right_fragment):
            fragment=store.rows('SELECT anchors FROM fragments WHERE id=?',(fid,))
            if not fragment: raise ValueError('Unknown fragment')
            anchors=json.loads(fragment[0]['anchors'])
            scopes.append({(v['program'],v['course']) for v in store.rows('SELECT * FROM versions')
                           if all(source_belongs_to_version(store,a['chunk_id'],v['id']) for a in anchors)})
        if not scopes[0]&scopes[1]:
            raise ValueError('Replacement comparison requires reviewed bindings to the same course; reachability alone is insufficient')
    refs=[]
    for fid in (left_fragment,right_fragment):
        current=store.rows('SELECT id FROM assessments WHERE fragment_id=? ORDER BY version DESC LIMIT 1',(fid,))
        if not current:
            raise ValueError("Individually assess both fragments before comparison")
        refs.append(current[0]['id'])
    dimensions={'criteria':dimensions,'assessment_refs':refs}
    cid=uid(left_fragment,right_fragment,relation,dimensions,reason,limitations)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO comparisons VALUES(?,?,?,?,?,?,?,?)",(cid,left_fragment,right_fragment,relation,
            json.dumps(dimensions,ensure_ascii=False),reason,limitations,time.time()))
    return cid


def trajectory(store, title, initial_knowledge, steps, limitations):
    if not steps or not limitations: raise ValueError("A route needs steps and explicit boundaries")
    available=set(initial_knowledge)
    enriched=[]
    for step in steps:
        rows=store.rows('SELECT * FROM assessments WHERE fragment_id=? ORDER BY version DESC LIMIT 1',(step['fragment_id'],))
        if not rows: raise ValueError('Every step needs an individual assessment')
        assessment=rows[0]; analysis=json.loads(assessment['analysis'])
        missing=set(analysis['prerequisites'])-available
        if missing: raise ValueError('Unmet prerequisites: '+', '.join(sorted(missing)))
        if not step.get('reason') or not step.get('transition'): raise ValueError('Explain selection and transition')
        available.update(analysis['learning_outputs'])
        enriched.append({**step,'assessment_id':assessment['id']})
    rev=store.db.execute('SELECT coalesce(max(revision),0)+1 FROM trajectories WHERE title=?',(title,)).fetchone()[0]
    payload={'initial_knowledge':initial_knowledge,'steps':enriched,'limitations':limitations,
             'kind':'reviewer_curated_derivative','originals_preserved':True}
    tid=uid(title,rev,payload)
    with store.db:
        store.db.execute('INSERT INTO trajectories VALUES(?,?,?,?,?)',(tid,title,rev,json.dumps(payload,ensure_ascii=False),time.time()))
    return tid
