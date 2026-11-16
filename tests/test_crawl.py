import json
from collections import Counter
import httpx
import pytest
from atlas.store import Store
from atlas.crawl import Crawl
from atlas.network import Fetcher
from atlas.ingest import extract
from atlas.providers import provider_links


@pytest.fixture
def s(tmp_path,monkeypatch):
    monkeypatch.setattr('atlas.network.validate_url',lambda *a:None)
    store=Store(tmp_path/'private')
    yield store
    store.db.close()


def worker(s,pages,calls):
    def respond(req):
        calls.append(str(req.url))
        if req.url.path=='/robots.txt': return httpx.Response(404)
        data=pages.get(str(req.url))
        if data is None: return httpx.Response(404)
        if isinstance(data,dict): return httpx.Response(200,json=data)
        return httpx.Response(200,text=data,headers={'Content-Type':'text/html' if '<main>' in data else 'text/plain'})
    return Fetcher(s,[],delay=0,transport=httpx.MockTransport(respond))


def test_recursive_graph_resume_cycle_and_shared_file(s):
    base='https://example.org'; calls=[]
    pages={base+'/wiki':'<main><h1>Courses</h1><a href="/a">Course A</a><a href="/b">Course B</a><nav><a href="/menu">Course menu</a></nav></main>',
           base+'/a':'<main><h2>Course A 2023</h2><a href="/ma">Materials</a></main>',
           base+'/b':'<main><h2>Course B 2024</h2><a href="/mb">Materials</a></main>',
           base+'/ma':'<main><h2>A notes</h2><a href="/file.txt">Lecture</a><a href="/a">Course A</a><a href="?page=2">Notes</a></main>',
           base+'/mb':'<main><h2>B notes</h2><a href="/file.txt">Lecture</a></main>',
           base+'/file.txt':'Definition: a square matrix has equal row and column counts.'}
    first=worker(s,pages,calls).run(2,Crawl(s,base+'/wiki',3))
    assert first['stop']=='job_budget' and len(s.rows('SELECT * FROM snapshots'))==2
    s.db.close(); s.db=Store(s.home).db
    result=worker(s,pages,calls).run(30,Crawl(s,base+'/wiki',3))
    assert result['stop']=='no_ready_jobs'
    assert len(s.rows('SELECT * FROM snapshots'))==6
    counts=Counter(u for u in calls if not u.endswith('/robots.txt'))
    assert all(n==1 for n in counts.values())
    assert base+'/menu' not in counts and base+'/ma?page=2' not in counts
    rid=s.resource(base+'/file.txt')
    occurrences=s.rows('SELECT * FROM occurrences WHERE target_id=?',(rid,))
    assert len(occurrences)==2 and len({o['headings'] for o in occurrences})==2
    assert len(s.rows('SELECT * FROM jobs WHERE resource_id=?',(rid,)))==1
    assert s.verify()['sqlite']=='ok'
    before=len(calls); worker(s,pages,calls).run(30,Crawl(s,base+'/wiki',3))
    assert len(calls)==before


def test_depth_and_request_limit_preserve_frontier(s):
    base='https://example.org'; calls=[]
    pages={base+'/root':'<main><a href="/notes.txt">Lecture notes</a></main>',base+'/notes.txt':'Real source bytes in a fixture, not research.'}
    result=worker(s,pages,calls).run(10,Crawl(s,base+'/root',0),max_requests=1)
    assert result['requests']==1 and result['stop']=='request_limit'
    assert s.rows('SELECT state,tries FROM jobs')==[{'state':'pending','tries':0}]
    result=worker(s,pages,calls).run(10,Crawl(s,base+'/root',0))
    assert any(r['reason']=='depth_limit' for r in result['edges'])
    assert len(s.rows('SELECT * FROM snapshots'))==1
    result=worker(s,pages,calls).run(10,Crawl(s,base+'/root',1))
    assert len(s.rows('SELECT * FROM snapshots'))==2
    assert Counter(calls)[base+'/root']==1


def test_external_admission_unknown_redirect_and_oversize_continue_other_branches(s):
    base='https://example.org'; calls=[]
    pages={base+'/root':'<main><h2>Materials</h2><a href="https://teacher.example/notes.txt">Lecture notes</a><a href="/big.txt">Lecture</a><a href="/redirect">Course notes</a><a href="/ok.txt">Lecture</a></main>',
           'https://teacher.example/notes.txt':'Teaching content',base+'/big.txt':'x'*3000,base+'/ok.txt':'Retained original'}
    f=worker(s,pages,calls); original=f.client._transport
    def route(req):
        if req.url.path=='/redirect': return httpx.Response(302,headers={'Location':'https://unknown.example/file'})
        return original.handle_request(req)
    f.client=httpx.Client(transport=httpx.MockTransport(route)); f.max_bytes=1000
    result=f.run(30,Crawl(s,base+'/root',3))
    assert result['stop']=='no_ready_jobs'
    assert len(s.rows("SELECT * FROM jobs WHERE state='deferred'"))==2
    assert not any('unknown.example' in c for c in calls)
    assert s.rows('SELECT id FROM snapshots WHERE resource_id=?',(s.resource(base+'/ok.txt'),))
    assert s.rows('SELECT id FROM snapshots WHERE resource_id=?',(s.resource('https://teacher.example/notes.txt'),))


def test_github_pins_commit_not_tree_and_fetches_file(s):
    root='https://github.com/example/course'; api='https://api.github.com/repos/example/course'
    commit='a'*40; tree='b'*40; calls=[]
    raw='https://raw.githubusercontent.com/example/course/'+commit+'/notes.txt'
    pages={api:{'default_branch':'main'},api+'/commits/main':{'sha':commit,'commit':{'tree':{'sha':tree}}},
           api+'/git/trees/'+tree+'?recursive=1':{'sha':tree,'truncated':False,'tree':[{'type':'blob','path':'notes.txt','mode':'100644'}]},
           raw:'Lecture content, independently unverified.'}
    worker(s,pages,calls).run(10,Crawl(s,root,5))
    assert raw in calls and not any('/'+tree+'/notes.txt' in u for u in calls)
    assert s.rows('SELECT id FROM snapshots WHERE resource_id=?',(s.resource(raw),))
    state=s.rows("SELECT details FROM provider_states WHERE state='tree_listing_received'")[0]
    assert json.loads(state['details'])['listing_complete'] is True
    assert json.loads(state['details'])['materials_obtained'] is False


def test_provider_boundaries_and_yandex_pagination(s):
    from atlas.providers import provider,crawl_route
    from urllib.parse import parse_qs,urlsplit
    for path in ['/i/fixture-file','/d/fixture-folder']:
        url='https://disk.360.yandex.ru'+path
        assert provider(url)=='yandex_disk'
        route=urlsplit(crawl_route(url))
        assert route.hostname=='cloud-api.yandex.net'
        assert parse_qs(route.query)['public_key']==[url]
    base='https://cloud-api.yandex.net/v1/disk/public/resources?public_key=example&limit=100&offset=0'
    rid=s.resource(base)
    obj={'type':'dir','path':'/','_embedded':{'offset':0,'total':3,'items':[{'path':'/notes','name':'Notes','type':'dir'}]}}
    sid=s.snapshot(rid,json.dumps(obj).encode(),'application/json'); extract(s,sid); provider_links(s,sid)
    links=s.rows('SELECT r.url FROM occurrences o JOIN resources r ON r.id=o.target_id WHERE o.snapshot_id=?',(sid,))
    assert len(links)==2 and any('offset=1' in r['url'] for r in links)
    state=json.loads(s.rows('SELECT details FROM provider_states WHERE snapshot_id=?',(sid,))[0]['details'])
    assert state['listing_complete'] is False and not state['page_reaches_end']
    tree={'sha':'b'*40,'tree':[{'type':'blob','path':'notes.pdf'}],'truncated':True}
    rid=s.resource('https://api.github.com/repos/example/course/git/trees/main?recursive=1')
    sid=s.snapshot(rid,json.dumps(tree).encode(),'application/json'); extract(s,sid)
    assert not s.rows('SELECT id FROM occurrences WHERE snapshot_id=?',(sid,))
    assert s.rows('SELECT state FROM provider_states WHERE snapshot_id=?',(sid,))[0]['state']=='tree_listing_partial'


def test_cached_import_is_not_downloaded_again_and_library_links_original(s,tmp_path):
    from atlas.presentation import write_library
    url='https://example.org/notes.txt'; rid=s.resource(url); sid=s.snapshot(rid,b'Original bytes','text/plain'); extract(s,sid)
    calls=[]; worker(s,{},calls).run(10,Crawl(s,url,3)); assert not calls
    output=tmp_path/'library.html'; write_library(s,output)
    rendered=output.read_text(encoding='utf8')
    sha=s.rows('SELECT sha FROM snapshots WHERE id=?',(sid,))[0]['sha']
    assert s.object_path(sha).as_uri() in rendered and 'notes.txt' in rendered


def test_total_bytes_stop_and_corrupt_pdf_does_not_stop_other_branches(s):
    base='https://example.org'; calls=[]
    pages={base+'/root':'<main><a href="/bad.pdf">Lecture notes</a><a href="/good.txt">Lecture</a></main>',
           base+'/bad.pdf':'%PDF-1.4\ninvalid fixture',base+'/good.txt':'Usable original'}
    result=worker(s,pages,calls).run(10,Crawl(s,base+'/root',2),total_bytes=10)
    assert result['stop']=='total_byte_limit'
    assert not s.rows('SELECT id FROM snapshots')
    result=worker(s,pages,calls).run(10,Crawl(s,base+'/root',2))
    assert len(s.rows('SELECT id FROM snapshots'))==3
    assert s.rows("SELECT id FROM attempts WHERE status='extraction_error'")
    assert s.rows("SELECT id FROM chunks WHERE text='Usable original'")


def test_contacts_records_unknown_links_and_drive_are_not_downloaded(s):
    from atlas.crawl import classify
    base={'url':'https://teacher.example/profile','label':'Name','context':'','headings':'["Преподаватели и учебные ассистенты"]','path':'/content/a[1]'}
    assert classify(base,'https://example.org/course')[0]=='pending'
    assert classify({**base,'headings':'["Ведомости текущего контроля"]'},'https://example.org/course')[0]=='excluded'
    assert classify({**base,'headings':'["Формула оценивания"]','label':'Ведомость'},'https://example.org/course')==('excluded','administrative_records')
    assert classify({**base,'url':'https://drive.google.com/drive/folders/fixture','label':'Lecture notes','headings':'[]'},'https://example.org/course')==('pending','drive_listing_adapter_unavailable')
    assert classify({**base,'headings':'[]','label':'Other'},'https://example.org/course')==('pending','educational_relation_uncertain')
