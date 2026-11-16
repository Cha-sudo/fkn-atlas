import pytest
from atlas.store import Store
from atlas.ingest import extract
from atlas.pedagogy import annotate,compare,trajectory,FIELDS


def setup(tmp_path):
    s=Store(tmp_path)
    rid=s.resource('https://example.org/material')
    sid=s.snapshot(rid,b'Gaussian elimination. Pivot then back substitution.','text/plain')
    extract(s,sid)
    cid=s.rows('SELECT id FROM chunks')[0]['id']
    analysis={k:'unverified' for k in FIELDS}
    analysis.update(functions=['method_demonstration'],prerequisites=['arithmetic'],learning_outputs=['elimination'],confidence='medium',limitations='No proof checked',check_method='Read exact source')
    args=dict(title='Method',topic='Linear systems',subtopics=['pivots'],anchors=[{'chunk_id':cid,'quote':'Gaussian elimination.'}],analysis=analysis,reviewer='test')
    return s,args


def test_individual_analysis_and_history(tmp_path):
    s,args=setup(tmp_path)
    first=annotate(s,**args); second=annotate(s,**args)
    assert first['fragment_id']==second['fragment_id'] and second['version']==2
    with pytest.raises(ValueError): annotate(s,**{**args,'analysis':{**args['analysis'],'quality_score':100}})
    with pytest.raises(ValueError): annotate(s,**{**args,'anchors':[{'chunk_id':args['anchors'][0]['chunk_id'],'quote':'invented'}]})
    s.db.close()


def test_route_requires_prerequisites_and_pins_assessments(tmp_path):
    s,args=setup(tmp_path)
    fragment=annotate(s,**args)
    step={'fragment_id':fragment['fragment_id'],'reason':'introduce elimination','transition':'start from arithmetic'}
    with pytest.raises(ValueError): trajectory(s,'Route',[],[step],'Partial route')
    trajectory(s,'Route',['arithmetic'],[step],'Partial route')
    assert fragment['assessment_id'] in s.rows('SELECT plan FROM trajectories')[0]['plan']
    compare(s,fragment['fragment_id'],fragment['fragment_id'],'insufficient_data',['clarity'],'No second source','Test only')
    with pytest.raises(ValueError): compare(s,fragment['fragment_id'],fragment['fragment_id'],'best_overall',['size'],'Larger','None')
    s.db.close()


def test_comparison_cannot_infer_course_from_link_reachability(tmp_path):
    from atlas.research import add_version,bind_material
    s,args=setup(tmp_path)
    rid=s.resource('https://example.org/course')
    sid=s.snapshot(rid,b'<main><p>Course A <a href="/material">Lecture</a></p></main>','text/html'); extract(s,sid)
    node=s.rows("SELECT id FROM nodes WHERE snapshot_id=? AND tag='p'",(sid,))[0]['id']
    vid=add_version(s,'A','2024','Course A','A',sid,node)
    fragment=annotate(s,**args)['fragment_id']
    with pytest.raises(ValueError,match='reviewed bindings'):
        compare(s,fragment,fragment,'partial_replacement',['content'],'Self comparison cannot infer course','Fixture')
    material=s.rows('SELECT e.snapshot_id FROM chunks c JOIN extractions e ON e.id=c.extraction_id WHERE c.id=?',(args['anchors'][0]['chunk_id'],))[0]['snapshot_id']
    bind_material(s,vid,material,'Manually checked course membership','reviewer')
    compare(s,fragment,fragment,'partial_replacement',['content'],'Identity fixture after binding','No content fit inferred automatically')
    s.db.close()
