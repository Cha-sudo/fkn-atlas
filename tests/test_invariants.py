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
    rid=s.resource('https://example.org/a'); sid=s.snapshot(rid,b'success','text/plain'); s.enqueue(rid)
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
    assert import_local(s,path)[1]=='notebook_unexecuted'
    vtt=tmp_path/'lecture.vtt'; vtt.write_text('WEBVTT\n\n00:01.000 --> 00:02.000\nmatrix rank\n')
    assert import_local(s,vtt)[1]=='timed_transcript_only'
    assert s.rows("SELECT locator FROM chunks WHERE quality='transcript_not_board_review'")[0]['locator'].startswith('time:')


def plan(s):
    _,sid=html(s,'<main><h2>Course 2024</h2><p>Matrix rank theorem.</p></main>')
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
    assert not s.rows('SELECT * FROM reviews')


def test_cross_course_replacement_requires_source_instruction(s):
    lid,c=plan(s)
    target=s.rows('SELECT * FROM versions')[0]
    other=add_version(s,'OTHER PROGRAM','2023','Other course','B',target['source_snapshot'],target['evidence_node'])
    evidence=[{'chunk_id':c['id'],'quote':'Matrix rank theorem.','check_method':'Independent source reading','source_version_id':other}]
    with pytest.raises(ValueError,match='Cross-course'):
        review(s,lid,'partial',['rank'],['examples'],'Similar topic is insufficient','test',evidence)
    assert not s.rows('SELECT * FROM reviews')


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
