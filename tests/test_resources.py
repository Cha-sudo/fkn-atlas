import io,json,hashlib,sys,unittest,zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fkn_atlas.storage import Store
from fkn_atlas.resources import Catalog,YandexDiskPublicAdapter,PUBLIC_EXAMPLE,stable_id,extract_resource
from fkn_atlas.external import PublicHTTP,Response
from fkn_atlas.crawler import FetchFailure,process_html
from fkn_atlas.providers import YouTubeResourceAdapter,GitHubArchiveAdapter


def response(data): return Response(json.dumps(data).encode(),200,'application/json')


class FakeHTTP:
    def __init__(self,pages=(),body=b''):
        self.pages=list(pages);self.body=body;self.urls=[]
    def json(self,url):
        self.urls.append(url)
        value=self.pages.pop(0)
        if isinstance(value,Exception): raise value
        return value,response(value)
    def get(self,url,limit):
        self.urls.append(url)
        if len(self.body)>limit: raise FetchFailure('Too large',retryable=False,state='too_large')
        return Response(self.body,200,'application/octet-stream')


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.store=Store(str(Path(self.tmp.name)/'test.sqlite'));self.catalog=Catalog(self.store)
    def tearDown(self): self.store.close();self.tmp.cleanup()
    def file(self,body=b'hello'):
        return self.catalog.upsert('yandex_disk',PUBLIC_EXAMPLE,{'path':'/note.txt','name':'note.txt','type':'file','size':len(body),'sha256':hashlib.sha256(body).hexdigest()})
    def directory(self,offset,total,items):
        return {'path':'/','name':'root','type':'dir','_embedded':{'offset':offset,'total':total,'items':items}}

    def test_pagination_resumes_without_duplicate_resources(self):
        a={'path':'/a.txt','name':'a.txt','type':'file','size':1}
        b={'path':'/b.txt','name':'b.txt','type':'file','size':2}
        http=FakeHTTP([self.directory(0,2,[a]),self.directory(1,2,[b])])
        adapter=YandexDiskPublicAdapter(self.store,http=http)
        first=adapter.scan(max_pages=1,page_size=1)
        self.assertEqual(first['directory_jobs_by_state'],{'queued':1})
        second=adapter.scan(max_pages=1,page_size=1)
        self.assertEqual(second['discovered_files'],2)
        self.assertEqual(second['listed_directories'],1)
        self.assertEqual(adapter.scan()['metadata_pages_this_run'],0)
        self.assertIn('offset=1',http.urls[-1])

    def test_nested_directory_queue_keeps_parent_evidence(self):
        child={'path':'/inner','name':'inner','type':'dir'}
        http=FakeHTTP([self.directory(0,1,[child]),{'path':'/inner','name':'inner','type':'dir','_embedded':{'offset':0,'total':0,'items':[]}}])
        adapter=YandexDiskPublicAdapter(self.store,http=http)
        r=adapter.scan(max_depth=1)
        self.assertEqual(r['listed_directories'],2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM resource_edges').fetchone()[0],1)

    def test_invalid_pagination_preserves_response_and_failure(self):
        http=FakeHTTP([self.directory(7,8,[])])
        r=YandexDiskPublicAdapter(self.store,http=http).scan()
        self.assertEqual(r['directory_jobs_by_state'],{'blocked':1})
        event=self.store.db.execute("SELECT sha256 FROM source_events WHERE state='invalid_metadata'").fetchone()
        self.assertTrue(event[0])
        self.assertEqual(r['listed_directories'],0)

    def test_repeated_item_does_not_claim_complete_listing(self):
        a={'path':'/a.txt','name':'a.txt','type':'file'}
        http=FakeHTTP([self.directory(0,2,[a]),self.directory(1,2,[a])])
        r=YandexDiskPublicAdapter(self.store,http=http).scan()
        self.assertEqual(r['listed_directories'],0)
        self.assertEqual(r['discovered_files'],1)

    def test_signed_url_is_not_persisted(self):
        rid=self.file()
        http=FakeHTTP([{'href':'https://downloader.disk.yandex.ru/data?signature=SECRET','method':'GET'}],b'hello')
        r=YandexDiskPublicAdapter(self.store,http=http).fetch('/note.txt',extract=False)
        self.assertEqual(r['state'],'retrieved')
        dump='\n'.join(self.store.db.iterdump())
        self.assertNotIn('SECRET',dump)
        for path, in self.store.db.execute('SELECT path FROM blobs'):
            self.assertNotIn(b'SECRET',(self.store.path.parent/path).read_bytes())
        again=YandexDiskPublicAdapter(self.store,http=FakeHTTP()).fetch('/note.txt',extract=False)
        self.assertEqual(again['state'],'already_retrieved')

    def test_checksum_mismatch_is_not_retrieved(self):
        self.file()
        http=FakeHTTP([{'href':'https://downloader.disk.yandex.ru/data'}],b'WRONG')
        r=YandexDiskPublicAdapter(self.store,http=http).fetch('/note.txt',extract=False)
        self.assertEqual(r['state'],'integrity_error')
        self.assertEqual(self.catalog.stats()['downloaded_files'],0)

    def test_interrupted_last_download_becomes_exhausted(self):
        rid=self.file()
        self.store.db.execute("INSERT INTO resource_jobs(resource_id,stage,state,attempts,updated_at) VALUES (?,'content','running',3,'test')",(rid,));self.store.db.commit()
        r=YandexDiskPublicAdapter(self.store,http=FakeHTTP()).fetch('/note.txt',extract=False)
        self.assertEqual(r['state'],'exhausted')

    def test_new_metadata_hash_does_not_reuse_old_content_state(self):
        rid=self.file()
        self.store.db.execute("UPDATE resources SET content_state='text_extracted' WHERE id=?",(rid,));self.store.db.commit()
        self.file(b'changed')
        self.assertEqual(self.catalog.get(rid)['content_state'],'metadata_only')

    def test_unavailable_extractor_can_be_retried(self):
        rid=self.file()
        YandexDiskPublicAdapter(self.store,http=FakeHTTP([{'href':'https://downloader.disk.yandex.ru/data'}],b'hello')).fetch('/note.txt',extract=False)
        with patch('fkn_atlas.content.inspect_content',return_value={'state':'extractor_unavailable','extractor':'test/1','text':'','members':[],'metadata':{}}):
            extract_resource(self.store,rid)
        with patch('fkn_atlas.content.inspect_content',return_value={'state':'text_extracted','extractor':'test/1','text':'hello','members':[],'metadata':{}}):
            extract_resource(self.store,rid)
        self.assertEqual(self.catalog.stats()['text_extracted_resources'],1)
        self.assertEqual(self.catalog.stats()['content_reviewed_resources'],0)

    def test_video_metadata_is_never_content_analysis(self):
        adapter=YouTubeResourceAdapter(self.store,FakeHTTP([{'type':'video','title':'A lecture'}]))
        self.assertEqual(adapter.probe('mROFPH8WKZg')['state'],'metadata_only')
        self.assertEqual(self.catalog.stats()['downloaded_files'],0)
        self.assertEqual(self.catalog.stats()['content_reviewed_resources'],0)

    def test_video_transport_denial_is_recorded(self):
        adapter=YouTubeResourceAdapter(self.store,FakeHTTP([FetchFailure('Proxy tunnel returned 403',retryable=False,state='platform_restricted')]))
        self.assertEqual(adapter.probe('mROFPH8WKZg')['state'],'platform_restricted')
        self.assertEqual(self.store.db.execute("SELECT access FROM resources").fetchone()[0],'platform_restricted')

    def test_archive_requires_source_occurrence_and_preserves_members(self):
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z: z.writestr('course/lesson.md','# Actual lesson\nEvidence text')
        revision='a'*40
        http=FakeHTTP([{'full_name':'author/course','default_branch':'main'},{'sha':revision}],archive.getvalue())
        adapter=GitHubArchiveAdapter(self.store,http)
        with self.assertRaises(ValueError): adapter.fetch('author/course','https://fixture.local/wiki/Start')
        process_html(self.store,'https://fixture.local/wiki/Start','<a href="https://github.com/author/course">course</a>',0,'fixture.local',0)
        r=adapter.fetch('author/course','https://fixture.local/wiki/Start')
        self.assertEqual(r['state'],'retrieved')
        self.assertEqual(self.catalog.stats()['archive_members_obtained'],1)
        self.assertEqual(self.catalog.stats()['text_extracted_resources'],1)
        self.assertEqual(self.catalog.stats()['content_reviewed_resources'],0)

    def test_malformed_provider_url_is_classified(self):
        with self.assertRaises(FetchFailure) as caught: PublicHTTP(self.store,lambda h:True).validate('https://example.org:bad/x')
        self.assertEqual(caught.exception.state,'invalid_url')

    def test_response_read_timeout_is_retryable(self):
        class Broken:
            headers={}
            code=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):raise TimeoutError()
        http=PublicHTTP(self.store,lambda h:True)
        with patch.object(http.opener,'open',return_value=Broken()),patch.object(http,'wait'):
            with self.assertRaises(FetchFailure) as caught:http._request('https://example.org/test',20)
        self.assertEqual(caught.exception.state,'network_error')
        self.assertTrue(caught.exception.retryable)


if __name__=='__main__':unittest.main()
