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
    from bs4 import BeautifulSoup
    from atlas.presentation import write_report

    s,args=setup(tmp_path)
    fragment=annotate(s,**args)
    step={'fragment_id':fragment['fragment_id'],'reason':'introduce elimination','transition':'start from arithmetic'}
    with pytest.raises(ValueError): trajectory(s,'Route',[],[step],'Partial route')
    trajectory(s,'Route',['arithmetic'],[step],'Partial route')
    assert fragment['assessment_id'] in s.rows('SELECT plan FROM trajectories')[0]['plan']
    compare(s,fragment['fragment_id'],fragment['fragment_id'],'insufficient_data',['clarity'],'No second source','Test only')
    revised = annotate(s, **{**args, 'analysis':{**args['analysis'], 'clarity':'Revised clarity'}})
    output = tmp_path / 'report.html'
    write_report(s, output)
    report = BeautifulSoup(output.read_text('utf8'), 'html.parser')
    first_anchor = 'a-' + fragment['assessment_id']
    revised_anchor = 'a-' + revised['assessment_id']
    assert len(report.find_all('a', href='#'+first_anchor)) == 3
    assert report.find(id=first_anchor) and report.find(id=revised_anchor)
    assert not report.find('a', href='#'+revised_anchor)
    assert 'Revised clarity' in report.find(id=revised_anchor).get_text()
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


def test_find_fragments_uses_current_assessment_and_one_complete_version(tmp_path):
    from atlas.pedagogy import find_fragments
    from atlas.research import add_lesson, add_version, bind_material

    s, args = setup(tmp_path)
    material = s.rows("SELECT snapshot_id FROM extractions")[0]["snapshot_id"]
    page = s.snapshot(s.resource("https://example.org/course"),
                      b'<main><p>Plan <a href="/material">notes</a><a href="/extra">exercise</a></p></main>',
                      "text/html")
    extract(s, page)
    node = s.rows("SELECT id FROM nodes WHERE snapshot_id=? AND tag='p'", (page,))[0]["id"]
    base = add_version(s, "Program", "2023", "Course", "Stream", page, node)
    adjacent = add_version(s, "Program", "2024", "Course", "Stream", page, node)
    other = add_version(s, "Different program", "2023", "Course", "Stream", page, node)
    lesson = add_lesson(s, base, "Lecture", 1, ["elimination"], "partial", node)
    foreign_lesson = add_lesson(s, other, "Lecture", 1, ["elimination"], "partial", node)
    bind_material(s, base, material, "First explicit assignment", "test")
    extra = s.snapshot(s.resource("https://example.org/extra"), b"Practice problem.", "text/plain")
    extract(s, extra)
    extra_chunk = s.rows("""SELECT c.id FROM chunks c JOIN extractions e ON e.id=c.extraction_id
                            WHERE e.snapshot_id=?""", (extra,))[0]["id"]
    bind_material(s, adjacent, extra, "Assignment in the adjacent version", "test")
    fragment = annotate(s, **args)
    both = annotate(s, **{**args, "title":"A mixed fragment", "anchors":[*args["anchors"],
                    {"chunk_id":extra_chunk, "quote":"Practice problem."}]})
    assert find_fragments(s, foreign_lesson) == []
    result, = find_fragments(s, lesson, "method_demonstration", "elimination", limit=1)
    assert result["assessment_id"] == fragment["assessment_id"]
    assert result["source_versions"][0]["id"] == base
    assert result["anchors"][0]["snapshot_id"] == material and result["lesson_fit"] == "unverified"
    bind_material(s, adjacent, material, "Second explicit assignment", "test")
    revised = annotate(s, **{**args, "analysis":{**args["analysis"], "functions":["assessment"],
                                                  "learning_outputs":["practice"]}})
    match, = find_fragments(s, lesson, "method_demonstration", "elimination")
    assert match["fragment_id"] == both["fragment_id"] and [v["id"] for v in match["source_versions"]] == [adjacent]
    match, = find_fragments(s, lesson, "assessment", "practice")
    assert match["assessment_id"] == revised["assessment_id"]
    assert find_fragments(s, lesson, "assessment", "elimination") == []
    with pytest.raises(ValueError, match="Unknown target"):
        find_fragments(s, "missing")
    s.db.close()
