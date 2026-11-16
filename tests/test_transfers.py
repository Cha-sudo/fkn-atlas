"""Own HTTP fixtures, never reported as real educational research."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from atlas.store import Store, file_digest
from atlas.network import Fetcher
from atlas.transfers import Transfer
from atlas.ingest import extract,import_local,MAX_PARSE_BYTES

URL='https://example.org/lecture.mp4'
BLOCK=65536
BODY=b'\x00\x00\x00\x18ftypmp42'+b'A'*(3*BLOCK-12)+b'end'


class Stream(httpx.SyncByteStream):
    def __init__(self,data,fail_after=None): self.data,self.fail_after=data,fail_after
    def __iter__(self):
        for offset in range(0,len(self.data),BLOCK):
            if self.fail_after is not None and offset>=self.fail_after:
                raise httpx.ReadError('Synthetic interrupted connection')
            yield self.data[offset:offset+BLOCK]


@pytest.fixture
def s(tmp_path,monkeypatch):
    monkeypatch.setattr('atlas.network.validate_url',lambda *a:None)
    store=Store(tmp_path/'private'); store.enqueue(store.resource(URL))
    yield store
    store.db.close()


def worker(s,handler,calls):
    def route(req):
        if req.url.path=='/robots.txt': return httpx.Response(404)
        calls.append(dict(req.headers))
        return handler(req)
    return Fetcher(s,['example.org'],delay=0,transport=httpx.MockTransport(route))


def response(data=BODY,status=200,etag='"v1"',fail_after=None,headers=None):
    metadata={'Content-Type':'video/mp4','Content-Length':str(len(data))}
    if etag is not None: metadata['ETag']=etag
    metadata.update(headers or {})
    return httpx.Response(status,headers=metadata,stream=Stream(data,fail_after))


def ready(s):
    with s.db: s.db.execute("UPDATE jobs SET state='pending',due=0 WHERE state!='done'")


def interrupt(s,etag='"v1"'):
    calls=[]
    worker(s,lambda r:response(etag=etag,fail_after=BLOCK),calls).run(1)
    assert not s.rows('SELECT id FROM snapshots')
    assert s.rows('SELECT received FROM transfers')[0]['received']==BLOCK
    ready(s)
    return calls


def assert_original(s,body=BODY):
    rows=s.rows('SELECT sha FROM snapshots')
    assert len(rows)==1 and rows[0]['sha']==hashlib.sha256(body).hexdigest()
    assert s.object_path(rows[0]['sha']).read_bytes()==body
    assert not s.rows('SELECT * FROM transfers')
    assert not list((s.home/'partials').iterdir())
    assert s.rows('SELECT status FROM extractions')[0]['status']=='media_untranscribed'


def test_interruption_restart_range_and_deduplication(s):
    calls=interrupt(s)
    s.db.close(); s.db=Store(s.home).db
    def resume(req):
        assert req.headers['range']==f'bytes={BLOCK}-'
        assert req.headers['if-range']=='"v1"' and req.headers['accept-encoding']=='identity'
        return response(BODY[BLOCK:],206,headers={'Content-Range':f'bytes {BLOCK}-{len(BODY)-1}/{len(BODY)}'})
    worker(s,resume,calls).run(1); assert_original(s)
    assert s.verify()['bad_objects']==[]
    another=s.resource('https://example.org/other.mp4'); s.enqueue(another)
    worker(s,lambda req:response(),[]).run(1)
    assert len(s.rows('SELECT * FROM snapshots'))==2
    assert len(s.rows('SELECT * FROM blobs'))==1
    count=len(calls); worker(s,resume,calls).run(10); assert len(calls)==count


@pytest.mark.parametrize('etag',[None,'W/"weak"'])
def test_without_strong_validator_restart_instead_of_append(s,etag):
    calls=interrupt(s,etag)
    def full(req):
        assert 'range' not in req.headers and 'if-range' not in req.headers
        return response()
    worker(s,full,calls).run(1); assert_original(s)
    assert 'strong_etag_unavailable' in s.rows("SELECT detail FROM transfer_events WHERE event='restart'")[0]['detail']


@pytest.mark.parametrize('new_etag',['"v1"','"v2"'])
def test_200_on_range_never_appends_previous_version(s,new_etag):
    calls=interrupt(s); body=b'NEW VERSION'*14000
    worker(s,lambda req:response(body,etag=new_etag),calls).run(1)
    assert_original(s,body)
    assert any('range_answered_200_restart' in r['detail'] for r in s.rows("SELECT detail FROM transfer_events WHERE event='restart'"))


@pytest.mark.parametrize('change',[
    {'Content-Range':f'bytes 0-{len(BODY)-BLOCK-1}/{len(BODY)}'},
    {'Content-Range':'garbage'},
    {'Content-Range':f'bytes {BLOCK}-{len(BODY)-1}/{len(BODY)+1}'},
    {'ETag':'"v2"'},
    {'Content-Length':'7'},
    {'Content-Encoding':'gzip'},
])
def test_invalid_range_is_not_published_or_appended(s,change):
    calls=interrupt(s)
    headers={'Content-Range':f'bytes {BLOCK}-{len(BODY)-1}/{len(BODY)}',**change}
    worker(s,lambda req:response(BODY[BLOCK:],206,headers=headers),calls).run(1)
    assert s.rows('SELECT state FROM jobs')[0]['state']=='deferred'
    assert s.rows('SELECT received FROM transfers')[0]['received']==BLOCK
    assert not s.rows('SELECT id FROM snapshots')


def test_range_on_changed_redirect_target_is_not_appended(s):
    calls=interrupt(s)
    def handler(req):
        if req.url.path=='/lecture.mp4': return httpx.Response(302,headers={'Location':'/new.mp4'})
        return response(BODY[BLOCK:],206,headers={'Content-Range':f'bytes {BLOCK}-{len(BODY)-1}/{len(BODY)}'})
    worker(s,handler,calls).run(1)
    assert 'resume_validator_or_url_changed' in s.rows('SELECT last_error FROM jobs')[0]['last_error']
    assert not s.rows('SELECT id FROM snapshots')


def test_416_restarts_and_unsolicited_206_is_rejected(s):
    calls=interrupt(s)
    def handler(req):
        if req.headers.get('range'): return httpx.Response(416,headers={'Content-Range':f'bytes */{len(BODY)}'})
        return response()
    worker(s,handler,calls).run(1); assert_original(s)
    other=s.resource('https://example.org/other.mp4'); s.enqueue(other)
    worker(s,lambda req:response(BODY,206,headers={'Content-Range':f'bytes 0-{len(BODY)-1}/{len(BODY)}'}),[]).run(1)
    assert not s.rows('SELECT id FROM snapshots WHERE resource_id=?',(other,))


def test_short_complete_response_stays_partial(s):
    calls=[]
    worker(s,lambda req:response(BODY[:BLOCK],headers={'Content-Length':str(len(BODY))}),calls).run(1)
    assert not s.rows('SELECT id FROM snapshots')
    assert s.rows('SELECT state FROM jobs')[0]['state']=='retry'
    assert s.rows('SELECT received FROM transfers')[0]['received']==BLOCK


def test_budget_quota_free_space_and_resume(s,monkeypatch):
    calls=[]
    result=worker(s,lambda req:response(),calls).run(1,total_bytes=70000)
    assert s.rows('SELECT received FROM transfers')[0]['received']==70000
    assert s.rows('SELECT state,tries FROM jobs')==[{'state':'pending','tries':0}]
    f=worker(s,lambda req:response(BODY[70000:],206,headers={'Content-Range':f'bytes 70000-{len(BODY)-1}/{len(BODY)}'}),calls)
    f.max_storage_bytes=s.payload_bytes()+1000
    f.run(1)
    assert s.rows('SELECT received FROM transfers')[0]['received']==70000
    assert 'storage_quota' in s.rows('SELECT last_error FROM jobs')[0]['last_error']
    monkeypatch.setattr('atlas.network.shutil.disk_usage',lambda path:SimpleNamespace(free=1))
    before=len(calls); f.run(1)
    assert len(calls)==before and not s.rows('SELECT id FROM snapshots')


def test_corrupt_or_uncommitted_partial(s):
    calls=interrupt(s)
    path=s.home/'partials'/s.rows('SELECT filename FROM transfers')[0]['filename']
    with path.open('ab') as f: f.write(b'uncommitted tail')
    def resume(req):
        assert req.headers['range']==f'bytes={BLOCK}-'
        return response(BODY[BLOCK:],206,headers={'Content-Range':f'bytes {BLOCK}-{len(BODY)-1}/{len(BODY)}'})
    worker(s,resume,calls).run(1); assert_original(s)
    assert s.rows("SELECT id FROM transfer_events WHERE event='truncate_uncommitted_tail'")
    s.enqueue(s.resource(URL),refresh=True)
    worker(s,lambda req:response(fail_after=BLOCK),[]).run(1); ready(s)
    path=s.home/'partials'/s.rows('SELECT filename FROM transfers')[0]['filename']
    with path.open('r+b') as f: f.write(b'broken')
    def fresh(req):
        assert 'range' not in req.headers
        return response()
    worker(s,fresh,[]).run(1); assert_original(s)
    assert any('partial_integrity_failed' in e['detail'] for e in s.rows("SELECT detail FROM transfer_events WHERE event='restart'"))


def test_crash_after_atomic_rename_recovers_without_network(s,monkeypatch):
    replace=os.replace
    def crash(src,dst):
        replace(src,dst); raise OSError('Synthetic crash after rename before SQLite commit')
    monkeypatch.setattr('atlas.store.os.replace',crash)
    worker(s,lambda req:response(),[]).run(1)
    assert not s.rows('SELECT id FROM snapshots')
    assert s.rows('SELECT state FROM transfers')[0]['state']=='complete'
    monkeypatch.setattr('atlas.store.os.replace',replace); ready(s)
    calls=[]; worker(s,lambda req:pytest.fail('Completed local bytes must not be downloaded again'),calls).run(1)
    assert not calls; assert_original(s)


def test_stale_worker_cannot_append_or_publish(s):
    job=s.claim(); t=Transfer(s,job); t.append(b'prefix')
    with s.db: s.db.execute('UPDATE jobs SET lease_until=0')
    current=s.claim()
    with pytest.raises(RuntimeError,match='Stale'): t.append(b'bad')
    assert t.path.read_bytes()==b'prefix'
    t2=Transfer(s,current); t2.append(b'-suffix')
    assert t.path.read_bytes()==b'prefix-suffix'


def test_large_media_archive_import_and_verify_do_not_read_whole_file(s,tmp_path,monkeypatch):
    media=tmp_path/'lecture.mp4'; archive=tmp_path/'materials.zip'
    for path,prefix in ((media,b'\x00\x00\x00\x18ftypmp42'),(archive,b'PK\x03\x04')):
        with path.open('wb') as f: f.write(prefix); f.truncate(MAX_PARSE_BYTES+4096)
    def forbid(path): raise AssertionError('whole-file read_bytes is forbidden for large originals')
    monkeypatch.setattr(Path,'read_bytes',forbid)
    _,video_status=import_local(s,media)
    _,archive_status=import_local(s,archive)
    assert video_status=='media_untranscribed' and archive_status=='archive_stored_unread'
    assert s.verify()['bad_objects']==[]
    assert not s.rows('SELECT id FROM chunks')
    assert not (tmp_path/'materials').exists()


def test_time_budget_and_file_quota_keep_resumable_checkpoint(s):
    f=None
    class TimedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield BODY[:BLOCK]
            f.deadline=0
            yield BODY[BLOCK:2*BLOCK]
    f=worker(s,lambda req:httpx.Response(200,headers={'ETag':'"v1"','Content-Length':str(len(BODY)),'Content-Type':'video/mp4'},stream=TimedStream()),[])
    f.run(1)
    assert s.rows('SELECT received FROM transfers')[0]['received']==BLOCK
    assert 'time_limit' in s.rows('SELECT last_error FROM jobs')[0]['last_error']
    def resume(req):
        assert req.headers['range']==f'bytes={BLOCK}-'
        return response(BODY[BLOCK:],206,headers={'Content-Range':f'bytes {BLOCK}-{len(BODY)-1}/{len(BODY)}'})
    small=worker(s,resume,[]); small.max_bytes=BLOCK; small.run(1)
    assert s.rows('SELECT state FROM jobs')[0]['state']=='deferred'
    s.resume(s.resource(URL))
    worker(s,resume,[]).run(1); assert_original(s)


def test_valid_limited_range_segments_and_unknown_length(s):
    interrupt(s)
    def resume(req):
        start=int(req.headers['range'].split('=')[1].split('-')[0]); end=min(start+BLOCK,len(BODY))-1
        return response(BODY[start:end+1],206,headers={'Content-Range':f'bytes {start}-{end}/{len(BODY)}'})
    calls=[]; worker(s,resume,calls).run(4)
    assert len(calls)==3; assert_original(s)
    rid=s.resource('https://example.org/chunked.mp4'); s.enqueue(rid)
    worker(s,lambda req:httpx.Response(200,headers={'Content-Type':'video/mp4'},stream=Stream(BODY)),[]).run(1)
    assert len(s.rows('SELECT id FROM snapshots'))==2 and len(s.rows('SELECT sha FROM blobs'))==1


def test_real_local_http_audio_interrupt_resume(s):
    """Real sockets and a valid generated WAV, explicitly not a teaching recording."""
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import io,socket,threading,wave
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(44100)
        wav.writeframes(b'\x00\x00'*(44100*3))
    data=buffer.getvalue(); seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path=='/robots.txt':
                self.send_response(404); self.end_headers(); return
            start=int(self.headers.get('Range','bytes=0-').split('=')[1].split('-')[0])
            seen.append((start,self.headers.get('If-Range')))
            self.send_response(206 if start else 200)
            self.send_header('Content-Type','audio/wav'); self.send_header('ETag','"generated-wav-v1"')
            self.send_header('Content-Length',str(len(data)-start))
            if start: self.send_header('Content-Range',f'bytes {start}-{len(data)-1}/{len(data)}')
            self.end_headers()
            if not start:
                self.wfile.write(data[:BLOCK]); self.wfile.flush(); self.connection.shutdown(socket.SHUT_WR)
            else: self.wfile.write(data[start:])
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        url=f'http://127.0.0.1:{server.server_port}/generated.wav'; rid=s.resource(url); s.enqueue(rid)
        first=Fetcher(s,['127.0.0.1'],delay=0); first.run(1); first.client.close()
        assert not s.rows('SELECT id FROM snapshots')
        ready(s)
        second=Fetcher(s,['127.0.0.1'],delay=0); second.run(1); second.client.close()
        snapshot=s.rows('SELECT sha FROM snapshots WHERE resource_id=?',(rid,))[0]
        assert snapshot['sha']==hashlib.sha256(data).hexdigest()
        assert seen==[(0,None),(BLOCK,'"generated-wav-v1"')]
        assert s.rows('SELECT status FROM extractions')[0]['status']=='media_untranscribed'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
