import io
import json
import time
import zipfile
import httpx
import pytest
from atlas.store import Store, canonical
from atlas.ingest import extract, import_local
from atlas.network import Fetcher, AccessError, validate_url
from atlas.research import add_version, add_lesson, review, candidates


@pytest.fixture
def s(tmp_path):
    result=Store(tmp_path/"private")
    yield result
    result.db.close()


def html(s, text):
    rid=s.resource("https://example.org/course")
    sid=s.snapshot(rid,text.encode(),"text/html")
    extract(s,sid)
    return rid,sid


def test_occurrences_structure_and_idempotency(s):
    _,sid=html(s,'<main><h2>2024</h2><ul><li>Lecture <a href="a.pdf">PDF</a><ul><li><a href="a.pdf">Again</a></li></ul></li></ul><h2>2025</h2><p><a href="a.pdf">PDF</a></p></main>')
    rows=s.rows('SELECT * FROM occurrences ORDER BY path')
    assert len(rows)==3 and len({r['target_id'] for r in rows})==1
    assert len({r['id'] for r in rows})==3
    assert {r['headings'] for r in rows}=={'["2024"]','["2025"]'}
    assert all(r['parent_id'] for r in s.rows("SELECT * FROM nodes WHERE tag='a'"))
    extract(s,sid)
    assert len(s.rows('SELECT * FROM occurrences'))==3


def test_history_error_does_not_erase_success(s):
    rid=s.resource('https://example.org/a')
    a=s.snapshot(rid,b'old','text/plain'); b=s.snapshot(rid,b'new','text/plain')
    s.attempt(rid,'obtained',200,sid=a); s.attempt(rid,'unavailable',502,'bad gateway')
    assert len(s.rows('SELECT * FROM snapshots'))==2
    assert s.snapshot(rid,b'old','text/plain')==a
    assert s.verify()['bad_objects']==[]
    sha=s.rows('SELECT sha FROM snapshots WHERE id=?',(b,))[0]['sha']
    s.object_path(sha).write_bytes(b'corrupted')
    assert s.verify()['bad_objects']==[sha]
    with pytest.raises(ValueError): s.snapshot(rid,b'new','text/plain')


def test_crash_resume_and_stale_worker(s):
    rid=s.resource('https://example.org/a'); s.enqueue(rid); s.enqueue(rid)
    old=s.claim(lease_seconds=-1)
    new=Store(s.home)
    current=new.claim()
    assert current['tries']==2 and current['token']!=old['token']
    with pytest.raises(RuntimeError): s.finish(old,'done')
    new.finish(current,'done'); assert new.claim() is None
    new.db.close()


def test_future_retry_is_not_claimed(s):
    s.enqueue(s.resource('https://example.org/a'))
    job=s.claim(); s.finish(job,'retry','rate limited',3600)
    assert s.claim() is None


def test_worker_scope_does_not_block_other_jobs(s):
    rid=s.resource('https://other.example/a'); s.enqueue(rid)
    assert s.claim(allowed_hosts={'example.org'}) is None
    assert s.rows('SELECT state FROM jobs')[0]['state']=='pending'


def test_network_http_failure_keeps_snapshot(s,monkeypatch):
    monkeypatch.setattr('atlas.network.validate_url',lambda *a: None)
    rid=s.resource('https://example.org/a'); sid=s.snapshot(rid,b'success','text/plain'); s.enqueue(rid,refresh=True)
    def route(request):
        if request.url.path=='/robots.txt': return httpx.Response(404)
        return httpx.Response(429,headers={'Retry-After':'700'})
    Fetcher(s,['example.org'],transport=httpx.MockTransport(route),delay=0).run(1)
    assert s.rows('SELECT * FROM jobs')[0]['state']=='retry'
    assert s.rows('SELECT * FROM snapshots')[0]['id']==sid
    assert s.rows('SELECT * FROM jobs')[0]['due']>time.time()+690


def test_robots_and_size_limits(s,monkeypatch):
    monkeypatch.setattr('atlas.network.validate_url',lambda *a: None)
    rid=s.resource('https://example.org/private'); s.enqueue(rid)
    calls=[]
    def route(request):
        calls.append(request.url.path)
        return httpx.Response(200,text='User-agent: *\nDisallow: /private')
    Fetcher(s,['example.org'],transport=httpx.MockTransport(route),delay=0).run(1)
    assert calls==['/robots.txt']
    assert s.rows('SELECT * FROM jobs')[0]['state']=='blocked'
    fetch=Fetcher(s,['example.org'],transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*20)),delay=0,max_bytes=10)
    with pytest.raises(AccessError): fetch.request('https://example.org/a')


@pytest.mark.parametrize('url',['file:///etc/passwd','http://127.0.0.1/x','https://user:pass@example.org/','https://example.org:8080/x'])
def test_private_and_unsafe_urls(url):
    with pytest.raises((ValueError,AccessError)): validate_url(url,{'example.org','127.0.0.1'})


def test_archive_never_extracts(s,tmp_path):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('../../escape.py','print("NEVER RUN")')
        z.writestr('macro.exe',b'unknown')
    path=tmp_path/'evil.zip'; path.write_bytes(data.getvalue())
    _,status=import_local(s,path)
    assert status=='archive_inventory_only'
    assert not (tmp_path/'escape.py').exists()
    inventory=json.loads(s.rows('SELECT text FROM chunks')[0]['text'])
    assert inventory[0]['unsafe_path']
    assert candidates(s,'NEVER RUN')==[]


def test_notebook_and_vtt_are_unexecuted(s,tmp_path):
    path=tmp_path/'lecture.ipynb'
    path.write_text(json.dumps({'cells':[{'cell_type':'code','source':['raise RuntimeError()']}]}))
    snapshot, status = import_local(s, path, 'https://example.org/lesson?file=1')
    assert status == 'notebook_unexecuted'
    assert s.rows('SELECT mime FROM snapshots WHERE id=?', (snapshot,))[0]['mime'] == 'application/x-ipynb+json'
    vtt=tmp_path/'lecture.vtt'; vtt.write_text('WEBVTT\n\n00:01.000 --> 00:02.000\nmatrix rank\n')
    assert import_local(s,vtt)[1]=='timed_transcript_only'
    assert s.rows("SELECT locator FROM chunks WHERE quality='transcript_not_board_review'")[0]['locator'].startswith('time:')


def plan(s, extra=''):
    _,sid=html(s,'<main><h2>Course 2024</h2><p>Matrix rank theorem.</p>'+extra+'</main>')
    node=s.rows("SELECT id FROM nodes WHERE tag='p'")[0]['id']
    vid=add_version(s,'PROGRAM','2024','Linear algebra','A',sid,node)
    lid=add_lesson(s,vid,'Missing notes',1,['rank','examples'],'listed_without_link',node)
    chunk=s.rows("SELECT * FROM chunks WHERE text='Matrix rank theorem.'")[0]
    return lid,chunk


def test_decision_requires_real_fragment_and_partition(s):
    lid,c=plan(s)
    good={'chunk_id':c['id'],'quote':'Matrix rank theorem.','check_method':'Read source independently'}
    args=dict(lesson_id=lid,verdict='partial',covered=['rank'],uncovered=['examples'],reason='No worked examples',reviewer='reviewer',evidence=[good])
    ident=review(s,**args)
    review(s,**args)
    assert len(s.rows('SELECT * FROM reviews'))==2
    assert s.rows('SELECT status FROM lessons')[0]['status']=='listed_without_link'
    with pytest.raises(ValueError): review(s,**{**args,'verdict':'confirmed_scope'})
    with pytest.raises(ValueError): review(s,**{**args,'uncovered':[]})
    with pytest.raises(ValueError): review(s,**{**args,'evidence':[{**good,'quote':'invented proof'}]})


def test_candidate_search_does_not_make_decisions(s):
    plan(s)
    found=candidates(s,'matrix rank')
    assert found and all(r['status']=='candidate_only' for r in found)
    assert all(r['purpose']=='supplementary_discovery' for r in found)
    assert not s.rows('SELECT * FROM reviews')


def test_gap_candidates_filter_course_before_ranking(s):
    from atlas.research import bind_material
    lid,_=plan(s)
    target=s.rows('SELECT * FROM versions')[0]
    root=s.rows('SELECT resource_id FROM snapshots WHERE id=?',(target['source_snapshot'],))[0]['resource_id']
    page=s.snapshot(root,b'<main><p>Materials <a href="/older.txt">Older notes</a><a href="/unbound.txt">Unknown</a></p></main>','text/html')
    extract(s,page)
    older_node=s.rows("SELECT id FROM nodes WHERE snapshot_id=? AND tag='p'", (page,))[0]['id']
    older=add_version(s,target['program'],'2023',target['course'],'Other stream',page,older_node)
    donor=s.resource('https://example.org/older.txt')
    donor_sid=s.snapshot(donor,b'Matrix rank definition and examples. A longer unrelated introduction.','text/plain')
    extract(s,donor_sid)
    bind_material(s,older,donor_sid,'Reviewed older version of this course','test')
    unbound=s.resource('https://example.org/unbound.txt')
    extract(s,s.snapshot(unbound,b'Matrix rank.','text/plain'))
    for program,course,suffix in [('OTHER',target['course'],'program'),(target['program'],'Calculus','course')]:
        rid=s.resource('https://example.org/'+suffix)
        sid=s.snapshot(rid,b'<main><p>Matrix rank.</p></main>','text/html'); extract(s,sid)
        node=s.rows("SELECT id FROM nodes WHERE snapshot_id=? AND tag='p'",(sid,))[0]['id']
        add_version(s,program,'2023',course,'A',sid,node)
    # Three exact lexical matches are ineligible, even when limit=1.
    found=candidates(s,'matrix rank',limit=1,exclude_resource=root,lesson_id=lid)
    assert len(found)==1 and found[0]['resource_id']==donor
    assert found[0]['source_version_ids']==[older]
    assert found[0]['purpose']=='same_course_gap_candidate'
    assert found[0]['content_correspondence']=='unverified'
    assert not s.rows('SELECT * FROM reviews')
    assert len(candidates(s,'matrix rank',exclude_resource=root))>1


def test_gap_candidates_fail_closed_for_unknown_lesson(s):
    plan(s)
    with pytest.raises(ValueError,match='Unknown target lesson'):
        candidates(s,'matrix rank',lesson_id='unknown')


def test_binding_is_pinned_to_bytes_and_legacy_url_binding_is_not_migrated(s):
    from atlas.research import bind_material,source_belongs_to_version
    lid,_=plan(s,'<a href="/mutable.txt">Course lecture</a>'); v=s.rows('SELECT * FROM versions')[0]
    root=s.rows('SELECT resource_id FROM snapshots WHERE id=?',(v['source_snapshot'],))[0]['resource_id']
    new_page=s.snapshot(root,b'<main><a href="/mutable.txt">Course lecture</a><a href="/later.txt">Only in new revision</a></main>','text/html')
    extract(s,new_page)
    rid=s.resource('https://example.org/mutable.txt')
    a=s.snapshot(rid,b'Matrix rank, original teaching scope.','text/plain'); extract(s,a)
    # Old schema declarations survive, but must never approve unknown bytes.
    with s.db: s.db.execute('INSERT INTO version_materials VALUES(?,?,?,?)',(v['id'],rid,'Legacy URL binding','test'))
    restarted=Store(s.home)
    def chunk(sid):
        return restarted.rows('SELECT c.id FROM chunks c JOIN extractions e ON e.id=c.extraction_id WHERE e.snapshot_id=?',(sid,))[0]['id']
    assert not source_belongs_to_version(restarted,chunk(a),v['id'])
    later=restarted.snapshot(restarted.resource('https://example.org/later.txt'),b'Later material','text/plain')
    with pytest.raises(ValueError,match='observed provenance path'):
        bind_material(restarted,v['id'],later,'Link occurs only in newer page','test')
    with pytest.raises(ValueError,match='own version'):
        bind_material(restarted,v['id'],new_page,'Different bytes of course page','test')
    bind_material(restarted,v['id'],a,'Read original bytes and course reference','test')
    b=restarted.snapshot(rid,b'Matrix rank, different teaching scope.','text/plain'); extract(restarted,b)
    assert source_belongs_to_version(restarted,chunk(a),v['id'])
    assert not source_belongs_to_version(restarted,chunk(b),v['id'])
    found=candidates(restarted,'matrix rank',lesson_id=lid,exclude_resource=root)
    assert {r['id'] for r in found}=={chunk(a)}
    with pytest.raises(ValueError,match='reviewed source-version binding'):
        review(restarted,lid,'partial',['rank'],['examples'],'New bytes are unreviewed','test',[
            {'chunk_id':chunk(b),'quote':'Matrix rank','check_method':'Read','source_version_id':v['id']}])
    bind_material(restarted,v['id'],b,'Separately reviewed new bytes','test')
    assert source_belongs_to_version(restarted,chunk(b),v['id'])
    assert restarted.rows('SELECT * FROM version_materials')
    assert len(restarted.rows('SELECT * FROM version_snapshots'))==2
    restarted.db.close()


@pytest.mark.parametrize('verdict,covered,uncovered', [
    ('partial',['rank'],['examples']), ('confirmed_scope',['rank','examples'],[])])
def test_cross_course_replacement_rejects_unrelated_authentic_quote(s,verdict,covered,uncovered):
    lid,c=plan(s)
    target=s.rows('SELECT * FROM versions')[0]
    other=add_version(s,'OTHER PROGRAM','2023','Other course','B',target['source_snapshot'],target['evidence_node'])
    evidence=[{'chunk_id':c['id'],'quote':'Matrix rank theorem.','check_method':'Independent source reading','source_version_id':other}]
    with pytest.raises(ValueError,match='Cross-course'):
        review(s,lid,'partial',['rank'],['examples'],'Similar topic is insufficient','test',evidence)
    authorization={'node_id':target['evidence_node'],'quote':'Matrix rank theorem.',
                   'reason':'This authentic quote does not authorize another course'}
    with pytest.raises(ValueError,match='Cross-course'):
        review(s,lid,verdict,covered,uncovered,'Title similarity','test',evidence,authorization)
    assert not s.rows('SELECT * FROM reviews')


def test_retracted_decision_is_history_only(s,tmp_path):
    from atlas.research import report
    from atlas.presentation import write_report
    lid,c=plan(s)
    evidence=[{'chunk_id':c['id'],'quote':'Matrix rank theorem.','check_method':'Read source'}]
    old=review(s,lid,'partial',['rank'],['examples'],'WithdrawnMarker','test',evidence)
    latest=review(s,lid,'rejected',[],['rank','examples'],'CorrectionMarker','test',[])
    assert [r['id'] for r in report(s)['latest_decisions']]==[latest]
    assert s.rows('SELECT id FROM reviews WHERE id=?',(old,))
    assert not s.rows("SELECT id FROM chunks_fts WHERE chunks_fts MATCH 'WithdrawnMarker'")
    assert not candidates(s,'WithdrawnMarker')
    output=tmp_path/'report.html'; write_report(s,output)
    rendered=output.read_text(encoding='utf8')
    assert 'CorrectionMarker' in rendered and 'WithdrawnMarker' not in rendered
    assert s.rows('SELECT status FROM lessons')[0]['status']=='listed_without_link'


def test_private_catalog_shows_plan_and_link_state(s, tmp_path):
    from bs4 import BeautifulSoup
    from atlas.presentation import write_catalog

    rid, snapshot = html(s, '<main><p>Written plan <a href="/missing.pdf">Handout</a></p></main>')
    node = s.rows("SELECT id FROM nodes WHERE snapshot_id=? AND tag='p'", (snapshot,))[0]['id']
    version = add_version(s, 'Program', '2024', 'Course', 'Stream', snapshot, node)
    lesson = add_lesson(s, version, 'Lecture 1', 1, ['Subtopic'], 'unverified', node)
    edge = s.rows('SELECT id,target_id FROM occurrences WHERE snapshot_id=?', (snapshot,))[0]
    with s.db:
        s.db.execute('INSERT INTO lesson_materials VALUES(?,?,?)',(lesson,edge['target_id'],edge['id']))
    output = tmp_path / 'catalog.html'
    result = write_catalog(s, output)
    page = BeautifulSoup(output.read_text('utf8'), 'html.parser')
    assert (result['programs'],result['versions'],result['lessons']) == (1,1,1)
    registered = page.find(id='lesson-'+lesson)
    assert 'Subtopic' in registered.get_text()
    assert 'снимок не получен' in registered.get_text()
    assert registered.find('a',string='Handout')['href'].endswith('/missing.pdf')
    assert page.find(id='v-'+version)


def test_joint_course_navigation_preserves_versions_and_scope(s, tmp_path):
    from bs4 import BeautifulSoup
    from atlas.research import catalog_programs, catalog_section
    from atlas.presentation import write_catalog

    lesson, _ = plan(s)
    first, = s.rows('SELECT * FROM versions')
    page = first['source_snapshot']
    node = first['evidence_node']
    program = first['program']
    series = 'Algebra · shared main stream'
    shared = {'Economics':'Economics', program:program}
    evidence = f'Shared {program} and Economics'
    url, = s.rows('SELECT r.url FROM snapshots sn JOIN resources r ON r.id=sn.resource_id WHERE sn.id=?',(page,))
    index = s.snapshot(s.resource('https://example.org/archive'),
                       f'<main><h1>2024/25</h1><a href="{url["url"]}">{evidence}</a></main>'.encode(),
                       'text/html')
    extract(s,index)
    anchor, = s.rows('SELECT node_id FROM occurrences WHERE snapshot_id=?',(index,))
    joint = add_version(s, program,'2024/2025',first['course'],'main',page,node)
    separate = add_version(s, program,'2024/2025',first['course'],'pilot',page,node)
    item = add_lesson(s,joint,'Lecture',1,['rank'],'unverified',node)
    assign = dict(programs=shared,quote=evidence,node_id=anchor['node_id'],reviewer='test',series=series)
    assert catalog_programs(s,joint,**assign) == catalog_programs(s,joint,**assign) == 1
    other_year = add_version(s, program,'2022/23',first['course'],'main',page,node)
    with pytest.raises(ValueError,match='academic year'):
        catalog_programs(s,other_year,**assign)
    with pytest.raises(ValueError,match='explicit name'):
        catalog_programs(s,joint,**{**assign,'programs':{**shared,'Math':'Math'}})
    phrase = s.rows('SELECT text FROM nodes WHERE id=?',(node,))[0]['text']
    catalog_section(s,item,'Linear systems',1,phrase)
    with pytest.raises(ValueError,match='exact quote'):
        catalog_section(s,item,'Linear systems',1,'made-up condition')
    output = tmp_path/'catalog.html'
    stats = write_catalog(s, output)
    dom = BeautifulSoup(output.read_text('utf8'),'html.parser')
    assert stats['shared_courses'] == 1 and stats['lessons'] == 2
    assert len(dom.select(f'a[href="#v-{joint}"]')) == 2
    assert len(dom.find_all(id='lesson-'+item)) == 1
    assert dom.find(id='v-'+separate).find_parent('section').h2.get_text() == program
    assert dom.find(id='v-'+joint).find_parent('section').h2.get_text() == series
    assert dom.find('a',href='#lesson-'+item) and dom.find(id='lesson-'+lesson)
    assert len({e['id'] for e in dom.select('[id]')}) == len(dom.select('[id]'))
    assert not s.rows('SELECT 1 FROM reviews') and not s.rows('SELECT 1 FROM version_snapshots')


def test_same_course_historical_evidence_is_allowed(s):
    lid,c=plan(s)
    target=s.rows('SELECT * FROM versions')[0]
    older=add_version(s,target['program'],'2023',target['course'],target['stream'],target['source_snapshot'],target['evidence_node'])
    evidence=[{'chunk_id':c['id'],'quote':'Matrix rank theorem.','check_method':'Read content','source_version_id':older}]
    review(s,lid,'partial',['rank'],['examples'],'Only rank topic is covered','test',evidence)
    assert s.rows('SELECT source_version_id FROM review_sources')[0]['source_version_id']==older


def test_canonical_preserves_meaning():
    assert canonical('https://EXAMPLE.org/a%2Fb?year=2024#lecture')=='https://example.org/a%2Fb?year=2024'
    assert canonical('https://example.org/курс').startswith('https://example.org/%D0')


def test_discovery_path_does_not_assign_other_course_material(s):
    from atlas.research import source_belongs_to_version, resource_reachable_from_version, bind_material
    lid,_=plan(s,'<p>Other course <a href="/other">notes</a></p>')
    target=s.rows('SELECT * FROM versions')[0]
    other_rid=s.resource('https://example.org/other')
    other_sid=s.snapshot(other_rid,b'<main><p>Course B <a href="/donor.txt">Lecture notes</a></p></main>','text/html'); extract(s,other_sid)
    other_node=s.rows("SELECT id FROM nodes WHERE snapshot_id=? AND tag='p'",(other_sid,))[0]['id']
    donor=s.resource('https://example.org/donor.txt'); donor_sid=s.snapshot(donor,b'Matrix rank theorem.','text/plain'); extract(s,donor_sid)
    chunk=s.rows('SELECT c.id FROM chunks c JOIN extractions e ON e.id=c.extraction_id WHERE e.snapshot_id=?',(donor_sid,))[0]['id']
    assert resource_reachable_from_version(s,donor,target['id'])
    assert not source_belongs_to_version(s,chunk,target['id'])
    evidence={'chunk_id':chunk,'quote':'Matrix rank theorem.','check_method':'Read actual source'}
    # Even an unregistered donor course cannot inherit the target course by default.
    for source in ({},{'source_version_id':target['id']}):
        with pytest.raises(ValueError,match='reviewed source-version binding'):
            review(s,lid,'partial',['rank'],['examples'],'Similar content','test',[{**evidence,**source}])
    other=add_version(s,'OTHER','2024','Course B','B',other_sid,other_node)
    bind_material(s,other,donor_sid,'Reviewed explicit course B notes link','reviewer')
    assert source_belongs_to_version(s,chunk,other)
    with pytest.raises(ValueError,match='Cross-course'):
        review(s,lid,'partial',['rank'],['examples'],'Similar content','test',[{**evidence,'source_version_id':other}])


def test_course_entry_and_stream_headings(s, tmp_path):
    from bs4 import BeautifulSoup
    from atlas.presentation import write_catalog

    _, snapshot = html(s, '<main><h2>Информация для пилотного потока</h2><p>Geometry</p>'
                       '<h2>Информация для основного потока</h2><p>Equations</p>'
                       '<h2>Архив</h2><p>Stream unknown</p></main>')
    rows = s.rows("SELECT * FROM nodes WHERE snapshot_id=? AND tag='p' ORDER BY ordinal", (snapshot,))
    course = 'Линейная алгебра и геометрия'
    archive = add_version(s, 'ПМИ', '2016/17', course, 'поток не указан', snapshot, rows[0]['id'])
    ids = [add_lesson(s, archive, 'Лекция 1', 1, [r['text']], 'unverified', r['id']) for r in rows]
    output = tmp_path/'catalog.html'
    write_catalog(s, output)
    assert not (tmp_path/'atlas-lessons').exists()
    add_version(s, 'ПМИ', '2025/26', course, 'основной', snapshot, rows[1]['id'])
    write_catalog(s, output)
    page = BeautifulSoup(output.read_text('utf8'), 'html.parser')
    path = page.find('a', string=lambda t: t and 'Начать линейную алгебру' in t)['href']
    unit = BeautifulSoup((tmp_path/path).read_text('utf8'), 'html.parser')
    assert unit.h1.get_text() == 'Урок 1. Матрицы и первые операции'
    # A table in a paragraph makes browsers close the paragraph before the
    # table, leaving its matrix brackets empty.
    matrices = unit.select('.matrix')
    assert matrices and len(matrices) == len(unit.select('main table'))
    assert all(m.name == 'div' and m.select_one(':scope > table') for m in matrices)
    assert all(m.find_parent(class_='formula') and not m.find_parent('p') for m in matrices)
    assert len(unit.select('#check li details')) == 4
    course_dir = (tmp_path/path).parent
    assert (course_dir/unit.select_one('link[rel=stylesheet]')['href']).is_file()
    files = ['matrices.html', 'products-and-systems.html', 'gauss.html', 'homogeneous.html',
             'elementary.html', 'matrix-equations.html']
    titles = ['Матрицы и первые операции', 'Свойства произведения и системы уравнений',
              'Метод Гаусса и общее решение', 'Однородные системы',
              'Элементарные матрицы', 'Матричные уравнения']
    lessons = [BeautifulSoup((course_dir/name).read_text('utf8'), 'html.parser') for name in files]
    for index, doc in enumerate(lessons):
        assert doc.h1.get_text() == f'Урок {index + 1}. {titles[index]}'
        assert [a['href'] for a in doc.select('a[rel=prev]')] == files[max(index-1, 0):index] * 2
        assert [a['href'] for a in doc.select('a[rel=next]')] == files[index+1:index+2] * 2
        assert len(doc.select('#check li > details')) == (4 if index == 0 else 3)
    _, systems, gauss, homogeneous, elementary, equations = lessons
    assert systems.select_one('#video a[href*="1tIpQMfut5MBLMTasAOUMWBgljQoCHxQe"]')
    assert unit.select_one('#video a[href*="12TC_aols6gzchKDBK4cvNT5GqCaNpIG1"]')
    assert unit.select_one('#matrix-system a')['href'] == 'products-and-systems.html#matrix-system'
    assert systems.select_one('section#matrix-system')
    years = BeautifulSoup((course_dir/'years.html').read_text('utf8'), 'html.parser')
    assert [s['id'] for s in years.select('section[id]')] == [f'year-{y}' for y in range(2015,2027)]
    for lesson in lessons:
        assert lesson.select_one('a[href^="years.html"]')
        for step in lesson.select('section[id]:not(#video):not(#check)'):
            assert step.select_one('aside.primary-source a[href^="https://"]')
        assert not lesson.select('img[src^="excerpts/"], a.source-page')
        citations = [a['href'][1:] for a in lesson.select('section .source-ref a[href^="#"]')]
        notes = lesson.select('footer .lesson-sources li[id]')
        assert citations and set(citations) == {note['id'] for note in notes}
        assert all(note.select_one('a[href^="https://"]') for note in notes)
    assert (course_dir/'lecture-video.js').is_file()
    assert (course_dir/'material-reader.js').is_file()
    assert all(doc.select_one('script[src="material-reader.js"]') for doc in lessons)
    from urllib.parse import parse_qs, urlsplit
    for doc, video_id, seconds in [(unit,'m3sgbykms3Y',[1495,2700,3256,4027]),
                                   (systems,'F1nR4mAbECQ',[540,1500,2145,2220,3500,3850,4300]),
                                   (gauss,'x6TG2Q1D2TE',[1069,3098,4042,4703]),
                                   (homogeneous,'Wg4HctY-2u4',[208,317,504,943,1190]),
                                   (elementary,'fxvXX4PatBA',[1563,1649,1827,2069,2238]),
                                   (equations,'fxvXX4PatBA',[2515,2604,2679,2830])]:
        video = doc.select_one('section#video')
        assert doc.main.find('section') == video
        start = f'&t={seconds[0]}s' if doc in (elementary, equations) else ''
        assert video.select_one('.watch-recording')['href'] == 'https://www.youtube.com/watch?v='+video_id+start
        assert doc.select_one('script[src="lecture-video.js"]')
        assert video.select_one('.inline-video[data-title]')
        assert [parse_qs(urlsplit(a['href']).query) for a in video.select('.video-moments a')] == [
            {'v':[video_id], 't':[str(n)+'s']} for n in seconds]
    clip = unit.select_one('#transpose .inline-video')
    assert clip['data-end'] == '3795'
    assert [parse_qs(urlsplit(a['href']).query).get('t') for a in
            unit.select('#transpose .primary-source a[href*="youtube.com"]')] == [
                ['3256s'], ['3479s'], ['3745s']]
    assert unit.select_one('#transpose a.clip-start')
    assert unit.select_one('#transpose a[href*="11XxXAh-z1DW"]')
    assert unit.select_one('#product a[href*="11XxXAh-z1DW"]')
    def values(selector):
        return [[int(c.get_text().replace('−', '-')) for c in r.select('td')]
                for r in unit.select(selector + ' tr')]
    left, right = values('#left'), values('#right')
    products = [[sum(a*b for a, b in zip(row, col)) for col in zip(*right)] for row in left]
    assert products == values('#result') == [[4, 1], [3, -2]]
    outline = BeautifulSoup((course_dir/'index.html').read_text('utf8'), 'html.parser')
    assert [a['href'] for a in outline.select('ol a')] == files
    assert outline.select_one('a[href="years.html"]')
    assert 'Запланирован' in outline.select_one('ol li:last-child').get_text()
    assert not outline.select('ol li:last-child a')
    assert (course_dir/outline.footer.a['href']).resolve() == output
    for ident, expected in zip(ids, ('Пилотный поток','Основной поток','Поток не установлен')):
        item = page.find(id='lesson-'+ident)
        assert item.parent.summary.get_text().startswith(expected)
        assert len(page.find_all(id='lesson-'+ident)) == 1

def test_private_excerpt_checks_bytes_and_links_the_exact_pdf_page(s, tmp_path):
    import io
    from importlib.util import find_spec

    from bs4 import BeautifulSoup
    from pypdf import PdfWriter

    from atlas.excerpts import render_excerpts
    from atlas.store import digest

    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=100, height=80)
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    sha = digest(data)
    marker = f"<!-- excerpt fold {sha} 2 3 4 43 24 | Формула из тестовой страницы -->"
    original = '<a href="https://example.org/course.pdf">Слайд 2</a>'
    assert render_excerpts(original + marker, s, tmp_path) == original
    path = s.object_path(sha)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"incorrect content")
    with pytest.warns(UserWarning, match="SHA-256 mismatch"):
        assert render_excerpts(original + marker, s, tmp_path) == original
    path.write_bytes(data)
    lesson = BeautifulSoup(render_excerpts(original + marker, s, tmp_path), 'html.parser')
    target, page = lesson.select_one('.source-page')['href'].split('#')
    assert page == 'page=2' and (tmp_path/target).read_bytes() == data
    if find_spec('pymupdf'):
        img = lesson.select_one('figure.source-image img')
        assert not img.find_parent('details')
        assert img['alt'] == 'Формула из тестовой страницы'
        assert (img['width'], img['height']) == ('120', '60')
        assert img.parent['href'] == img['src']
        assert (tmp_path/img['src']).read_bytes().startswith(b'\x89PNG')

def test_course_document_shows_every_page_of_the_pinned_pdf(s, tmp_path, monkeypatch):
    import io
    import sys
    from bs4 import BeautifulSoup
    from pypdf import PdfWriter
    from atlas.excerpts import render_excerpts
    from atlas.store import digest

    pytest.importorskip('pymupdf')
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=80)
    pdf.add_blank_page(width=80, height=100)
    buffer = io.BytesIO()
    pdf.write(buffer)
    data = buffer.getvalue()
    sha = digest(data)
    source = s.object_path(sha)
    marker = f'<!-- document {sha} 2 | Лекция & задачи -->'
    fallback = '<a href="https://example.org">Источник</a>'
    assert render_excerpts(marker + fallback, s, tmp_path) == fallback
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(data)
    markup = render_excerpts(marker + fallback, s, tmp_path)
    doc = BeautifulSoup(markup, 'html.parser')
    viewer = doc.select_one('.material-reader')
    assert viewer['data-start'] == '2' and viewer.figcaption.get_text() == 'Лекция & задачи'
    pages = viewer.select('.material-pages img')
    assert [p['src'] for p in pages] == [f'pages/{sha}-{n}.png' for n in (1, 2)]
    assert [p['width'] for p in pages] == ['1800', '1440']
    assert all((tmp_path/p['src']).is_file() for p in pages)
    assert (tmp_path / viewer.select_one('.material-file')['href']).read_bytes() == data
    with monkeypatch.context() as context:
        context.setitem(sys.modules, 'pymupdf', None)
        native = BeautifulSoup(render_excerpts(marker + fallback, s, tmp_path), 'html.parser')
        assert native.select_one('object')['data'] == f'originals/{sha}.pdf#page=2'
        assert native.select_one('object a')['href'] == f'originals/{sha}.pdf'
    source.write_bytes(b'changed')
    with pytest.warns(UserWarning, match='SHA-256 mismatch'):
        assert render_excerpts(marker + fallback, s, tmp_path) == fallback


def test_source_board_renders_inert_images(s, tmp_path):
    pytest.importorskip('pymupdf')
    from bs4 import BeautifulSoup
    from atlas.excerpts import render_excerpts
    from atlas.store import digest

    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="90" height="40"><rect width="45" height="20"/></svg>'
    sha = digest(data)
    path = s.object_path(sha)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    marker = f"<!-- excerpt board {sha} 1 0 0 45 20 | Фрагмент доски -->"
    doc = BeautifulSoup(render_excerpts(marker, s, tmp_path), 'html.parser')
    for asset in [doc.select_one('.source-page')['href'], doc.img['src']]:
        assert asset.endswith('.png')
        assert (tmp_path/asset).read_bytes().startswith(b'\x89PNG')
    assert path.read_bytes() == data

def test_private_video_frame_is_bound_to_its_png_digest(s, tmp_path):
    from bs4 import BeautifulSoup
    fitz = pytest.importorskip('pymupdf')
    from atlas.excerpts import render_excerpts
    from atlas.store import digest

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    pix.clear_with(255)
    data = pix.tobytes('png')
    sha = digest(data)
    folder = s.home/'derived'/'video-frames'
    folder.mkdir(parents=True)
    path = folder/f'{sha}.png'
    marker = f'<!-- excerpt frame {sha} 1 0 0 30 30 | Запись на доске. -->'
    assert render_excerpts(marker, s, tmp_path) == ''
    path.write_bytes(data)
    doc = BeautifulSoup(render_excerpts(marker, s, tmp_path), 'html.parser')
    assert (tmp_path/doc.select_one('.source-page')['href']).read_bytes() == data
    assert (tmp_path/doc.img['src']).is_file()
    path.write_bytes(b'changed')
    with pytest.warns(UserWarning, match='SHA-256 mismatch'):
        assert render_excerpts(marker, s, tmp_path) == ''


def test_seminar_archive_keeps_video_identity_and_document_fallbacks(s, tmp_path):
    import json
    from importlib.resources import files
    from urllib.parse import parse_qs, urlsplit
    from bs4 import BeautifulSoup
    from atlas.seminars import CHECKED, write_seminars

    curated = json.loads(files('atlas').joinpath('learning', 'seminars.json').read_text('utf8'))
    write_seminars(s, tmp_path, {})
    archive = BeautifulSoup((tmp_path / 'seminars.html').read_bytes(), 'html.parser')
    ids = [parse_qs(urlsplit(link['href']).query)['v'][0] for link in archive.select('.seminar-watch')]
    groups = [g for year in curated['years'].values() for g in year['groups'].values()]
    assert len(ids) == len(set(ids)) == sum(map(len, groups))
    for number, docs in curated['group213'].items():
        page = BeautifulSoup((tmp_path / f'seminar-213-{number}.html').read_bytes(), 'html.parser')
        assert len(page.select('a[href^="https://disk.yandex.ru/d/"]')) == len(docs)
        for link in page.select('.seminar-watch'):
            assert parse_qs(urlsplit(link['href']).query)['v'][0] in ids
        if number in CHECKED:
            assert f't={CHECKED[number][3]}s' in page.select('.seminar-watch')[-1]['href']
    for group, source in curated['handouts'].items():
        for number, file_id, sha in source['files']:
            handout = BeautifulSoup((tmp_path / f'seminar-{group}-{number}.html').read_bytes(), 'html.parser')
            assert handout.find('a', href=f'https://drive.google.com/file/d/{file_id}/view')
            assert len(sha) == 64
    for file in files('atlas').joinpath('learning').iterdir():
        if not file.name.endswith('.html'):
            continue
        lesson = BeautifulSoup(file.read_text('utf8'), 'html.parser')
        if lesson.select_one('.lecture-video'):
            timed, start = lesson.select('#seminar .seminar-watch')
            link = parse_qs(urlsplit(timed['href']).query)
            assert link['v'][0] in ids and link['t'][0].endswith('s')
            assert parse_qs(urlsplit(start['href']).query) == {'v': link['v']}
