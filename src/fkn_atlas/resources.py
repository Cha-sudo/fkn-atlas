"""Provider resources layered on the existing evidence Store; no course claims from names."""
from __future__ import annotations
import hashlib
import json
import posixpath
import time
from urllib.parse import urlencode, urlsplit
from .storage import Store, utc_now
from .crawler import FetchFailure
from .external import PublicHTTP

PUBLIC_EXAMPLE='https://disk.yandex.ru/d/lwE14jdmI4qZEQ'
API='https://cloud-api.yandex.net/v1/disk/public/resources'
META_FIELDS=('name','path','type','size','mime_type','sha256','md5','resource_id','modified','created','public_url')
FIELDS=','.join(META_FIELDS+tuple('_embedded.items.'+x for x in META_FIELDS)+('_embedded.total','_embedded.offset','_embedded.limit'))


def stable_id(provider,key,path):
    return hashlib.sha256(json.dumps([provider,key,path],ensure_ascii=False).encode()).hexdigest()


def row_dict(cursor):
    row=cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description],row)) if row is not None else None


class Catalog:
    def __init__(self,store): self.store=store

    def get(self,rid):
        row=row_dict(self.store.db.execute('SELECT * FROM resources WHERE id=?',(rid,)))
        if row is None: raise ValueError('Unknown resource ID')
        return row

    def upsert(self,provider,key,meta,sha=None,evidence_url=None):
        path=meta['path']
        if not isinstance(path,str) or not path.startswith('/') or any(x in ('.','..') for x in path.split('/')):
            raise ValueError('Invalid provider path')
        rid=stable_id(provider,key,path)
        safe={k:meta[k] for k in META_FIELDS if k in meta}
        self.store.db.execute('''INSERT INTO resources
          (id,provider,source_key,path,name,kind,size,expected_sha256,metadata_sha256,metadata_json,access,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          name=excluded.name,kind=excluded.kind,size=excluded.size,expected_sha256=excluded.expected_sha256,
          metadata_sha256=COALESCE(excluded.metadata_sha256,resources.metadata_sha256),
          content_state=CASE WHEN excluded.expected_sha256 IS NOT NULL AND excluded.expected_sha256 IS NOT resources.expected_sha256
            THEN 'metadata_only' ELSE resources.content_state END,
          metadata_json=excluded.metadata_json,access=excluded.access,updated_at=excluded.updated_at''',
          (rid,provider,key,path,meta.get('name',path),meta.get('type','unknown'),meta.get('size'),meta.get('sha256'),sha,
           json.dumps(safe,ensure_ascii=False),'public' if sha else 'unknown',utc_now()))
        if sha:
            self.store.db.execute('INSERT OR IGNORE INTO resource_observations VALUES (?,?,?,?)',(rid,sha,evidence_url or key,utc_now()))
        self.store._commit()
        return rid

    def edge(self,parent,child,relation,sha):
        self.store.db.execute('INSERT OR IGNORE INTO resource_edges VALUES (?,?,?,?)',(parent,child,relation,sha))
        self.store._commit()

    def enqueue(self,rid,depth):
        self.store.db.execute('''INSERT INTO resource_jobs(resource_id,stage,depth,updated_at) VALUES (?,'metadata',?,?)
          ON CONFLICT(resource_id,stage) DO UPDATE SET depth=MIN(resource_jobs.depth,excluded.depth)''',(rid,depth,utc_now()))
        self.store._commit()

    def recover(self):
        self.store.db.execute("UPDATE resource_jobs SET state=CASE WHEN attempts>=3 THEN 'exhausted' ELSE 'queued' END WHERE state IN ('running','failed')")
        self.store._commit()

    def failure(self,rid,stage,exc):
        row=self.store.db.execute('SELECT attempts FROM resource_jobs WHERE resource_id=? AND stage=?',(rid,stage)).fetchone()
        attempts=row[0] if row else 1
        state=('failed' if attempts<3 else 'exhausted') if exc.retryable else 'blocked'
        deadline=time.time()+max(exc.retry_after,30*2**max(0,attempts-1)) if exc.retryable else 0
        self.store.db.execute('UPDATE resource_jobs SET state=?,not_before=?,last_error=?,updated_at=? WHERE resource_id=? AND stage=?',
            (state,deadline,str(exc),utc_now(),rid,stage))
        access='platform_restricted' if exc.state in ('platform_restricted','robots_denied') or exc.status==403 else ('not_found' if exc.status==404 else 'transient_error')
        self.store.db.execute('UPDATE resources SET access=?,updated_at=? WHERE id=?',(access,utc_now(),rid))
        self.store._commit()

    def stats(self):
        db=self.store.db
        count=lambda sql: db.execute(sql).fetchone()[0]
        group=lambda sql: dict(db.execute(sql).fetchall())
        return {
            'resources_by_provider_kind':group("SELECT provider||':'||kind,COUNT(*) FROM resources GROUP BY provider,kind"),
            'directory_jobs_by_state':group("SELECT state,COUNT(*) FROM resource_jobs WHERE stage='metadata' GROUP BY state"),
            'download_jobs_by_state':group("SELECT state,COUNT(*) FROM resource_jobs WHERE stage='content' GROUP BY state"),
            'listed_directories':count("SELECT COUNT(*) FROM resources WHERE kind='dir' AND discovery='enumerated'"),
            'discovered_files':count("SELECT COUNT(*) FROM resources WHERE kind='file'"),
            'downloaded_files':count("SELECT COUNT(DISTINCT resource_id) FROM resource_contents WHERE resource_id IN (SELECT id FROM resources WHERE kind='file')"),
            'archive_members_obtained':count("SELECT COUNT(DISTINCT resource_id) FROM resource_contents WHERE resource_id IN (SELECT id FROM resources WHERE kind='archive_member')"),
            'file_versions':count('SELECT COUNT(*) FROM resource_contents'),
            'text_extracted_resources':count("SELECT COUNT(DISTINCT resource_id) FROM derived_artifacts WHERE state='text_extracted'"),
            'archive_inspected_resources':count("SELECT COUNT(DISTINCT resource_id) FROM derived_artifacts WHERE state='archive_inspected'"),
            'content_reviewed_resources':count('SELECT COUNT(DISTINCT resource_id) FROM content_reviews'),
            'retrieved_distinct_bytes':count('SELECT COALESCE(SUM(bytes),0) FROM blobs WHERE sha256 IN (SELECT sha256 FROM resource_contents)'),
            'coverage_basis':'Observed provider responses only; directory enumeration != file retrieval != text extraction != content review',
        }


class YandexDiskPublicAdapter:
    def __init__(self,store,public_url=PUBLIC_EXAMPLE,http=None):
        p=urlsplit(public_url)
        if p.scheme!='https' or p.hostname not in ('disk.yandex.ru','disk.yandex.com','yadi.sk') or p.query or p.fragment or p.username or p.port:
            raise ValueError('Expected a stable HTTPS public Yandex Disk URL without query/credentials')
        self.store,self.catalog,self.key=store,Catalog(store),public_url
        self.http=http or PublicHTTP(store,lambda h:h=='cloud-api.yandex.net' or h=='downloader.disk.yandex.ru' or h.endswith('.downloader.disk.yandex.ru') or h.endswith('.storage.yandex.net'))

    def endpoint(self,path,offset=0,limit=100,download=False):
        params={'public_key':self.key,'path':path}
        if not download: params.update({'offset':offset,'limit':limit,'fields':FIELDS})
        return API+('/download' if download else '')+'?'+urlencode(params)

    def probe(self,path='/'):
        url=self.endpoint(path)
        try:
            meta,response=self.http.json(url)
            if meta.get('path')!=path or meta.get('type') not in ('file','dir'):
                raise FetchFailure('Invalid resource metadata',retryable=False,body=response.body,state='invalid_metadata')
            with self.store.transaction():
                sha=self.store.save_blob(response.body)
                rid=self.catalog.upsert('yandex_disk',self.key,meta,sha,url)
                self.store.event(url,'yandex_probe','metadata_only',response.status,body=response.body,details={'resource_id':rid,'origin':'explicit_public_probe'})
            return {'resource_id':rid,'state':'metadata_only','name':meta.get('name'),'kind':meta['type'],'size':meta.get('size')}
        except FetchFailure as exc:
            self.store.event(url,'yandex_probe',exc.state,exc.status,str(exc),exc.body)
            return {'state':exc.state,'error':str(exc)}

    def scan(self,max_pages=20,max_depth=2,page_size=100):
        if not 1<=max_pages<=100 or not 0<=max_depth<=6 or not 1<=page_size<=1000:
            raise ValueError('Pilot requires max_pages 1..100, depth 0..6, page_size 1..1000')
        self.store.bind_mode('scout')
        root=stable_id('yandex_disk',self.key,'/')
        if self.store.db.execute('SELECT 1 FROM resources WHERE id=?',(root,)).fetchone() is None:
            self.catalog.upsert('yandex_disk',self.key,{'path':'/','name':'Uninspected public root','type':'dir'})
        self.catalog.enqueue(root,0)
        self.catalog.recover()
        processed=0
        while processed<max_pages:
            job=row_dict(self.store.db.execute('''SELECT j.*,r.path FROM resource_jobs j JOIN resources r ON r.id=j.resource_id
              WHERE r.provider='yandex_disk' AND r.source_key=? AND j.stage='metadata' AND j.state='queued'
              AND j.attempts<3 AND j.not_before<=? AND j.depth<=? ORDER BY j.depth,j.rowid LIMIT 1''',(self.key,time.time(),max_depth)))
            if not job: break
            rid=job['resource_id']; url=self.endpoint(job['path'],job['offset'],page_size)
            self.store.db.execute("UPDATE resource_jobs SET state='running',attempts=attempts+1 WHERE resource_id=? AND stage='metadata'",(rid,))
            self.store._commit();processed+=1
            try:
                response=None
                data,response=self.http.json(url)
                embedded=data.get('_embedded',{})
                items=embedded.get('items',[])
                total=embedded.get('total');offset=embedded.get('offset')
                if data.get('type')!='dir' or data.get('path')!=job['path'] or not isinstance(items,list) or not isinstance(total,int) or total<0 or offset!=job['offset'] or offset+len(items)>total:
                    raise FetchFailure('Invalid directory/pagination metadata',retryable=False,state='invalid_metadata')
                paths=[x.get('path','') for x in items]
                if len(set(paths))!=len(paths) or any(posixpath.dirname(x)!=job['path'].rstrip('/') and not (job['path']=='/' and posixpath.dirname(x)=='/') for x in paths):
                    raise FetchFailure('Duplicate or unrelated child paths',retryable=False,state='invalid_metadata')
                if not items and offset<total:
                    raise FetchFailure('Incomplete empty provider page',state='incomplete_listing')
                with self.store.transaction():
                    sha=self.store.save_blob(response.body)
                    self.catalog.upsert('yandex_disk',self.key,data,sha,url)
                    for child in items:
                        cid=self.catalog.upsert('yandex_disk',self.key,child,sha,url)
                        self.catalog.edge(rid,cid,'contains',sha)
                        if child.get('type')=='dir': self.catalog.enqueue(cid,job['depth']+1)
                    next_offset=offset+len(items)
                    done=next_offset>=total
                    if done:
                        observed=self.store.db.execute("SELECT COUNT(DISTINCT child_id) FROM resource_edges WHERE parent_id=? AND relation='contains'",(rid,)).fetchone()[0]
                        if observed!=total: raise FetchFailure('Enumeration count does not match distinct observed children',state='incomplete_listing')
                    self.store.db.execute("UPDATE resource_jobs SET state=?,offset=?,reported_total=?,attempts=0,last_error=NULL,updated_at=? WHERE resource_id=? AND stage='metadata'",
                        ('done' if done else 'queued',next_offset,total,utc_now(),rid))
                    self.store.db.execute('UPDATE resources SET discovery=? WHERE id=?',('enumerated' if done else 'partial',rid))
                    self.store.event(url,'yandex_metadata','enumerated' if done else 'partial',response.status,body=response.body,
                        details={'resource_id':rid,'offset':offset,'items':len(items),'total':total,'origin':'supplied_handoff_example','content_state':'metadata_only'})
                print(json.dumps({'directory':job['path'],'offset':offset,'listed':len(items),'total':total},ensure_ascii=False))
            except FetchFailure as exc:
                self.store.event(url,'yandex_metadata',exc.state,exc.status,str(exc),exc.body if exc.body is not None else (response.body if response else None),details={'resource_id':rid})
                self.catalog.failure(rid,'metadata',exc)
                print(json.dumps({'directory':job['path'],'error':str(exc)},ensure_ascii=False))
                if exc.status in (429,503) or exc.state in ('host_backoff','platform_restricted'): break
        return {'metadata_pages_this_run':processed,**self.catalog.stats()}

    def fetch(self,path,max_bytes=16*1024*1024,extract=True):
        if not 1<=max_bytes<=32*1024*1024: raise ValueError('Download limit must be 1 byte..32 MiB')
        rid=stable_id('yandex_disk',self.key,path);r=self.catalog.get(rid)
        if r['kind']!='file': raise ValueError('Only an inventoried file can be fetched; scan its parent first')
        existing=self.store.db.execute('SELECT sha256 FROM resource_contents WHERE resource_id=? ORDER BY retrieved_at DESC LIMIT 1',(rid,)).fetchone()
        if existing and (not r['expected_sha256'] or existing[0]==r['expected_sha256']):
            if extract: extract_resource(self.store,rid)
            return {'resource_id':rid,'state':'already_retrieved'}
        if r['size'] is not None and r['size']>max_bytes:
            self.store.event(self.key,'yandex_content','size_limit',details={'resource_id':rid,'path':path,'size':r['size'],'limit':max_bytes})
            return {'resource_id':rid,'state':'size_limit','size':r['size']}
        self.store.db.execute("INSERT OR IGNORE INTO resource_jobs(resource_id,stage,updated_at) VALUES (?,'content',?)",(rid,utc_now()))
        self.store.db.execute("UPDATE resource_jobs SET state=CASE WHEN attempts>=3 THEN 'exhausted' ELSE 'queued' END WHERE resource_id=? AND stage='content' AND state='running'",(rid,))
        self.store._commit()
        job=row_dict(self.store.db.execute("SELECT * FROM resource_jobs WHERE resource_id=? AND stage='content'",(rid,)))
        if job['attempts']>=3 or job['state'] in ('blocked','exhausted') or job['not_before']>time.time():
            return {'resource_id':rid,'state':job['state'],'not_before':job['not_before']}
        self.store.db.execute("UPDATE resource_jobs SET state='running',attempts=attempts+1 WHERE resource_id=? AND stage='content'",(rid,));self.store._commit()
        try:
            # This response contains a short-lived signed URL: deliberately never persist it.
            download,_=self.http.json(self.endpoint(path,download=True))
            if download.get('method','GET')!='GET' or not isinstance(download.get('href'),str):
                raise FetchFailure('Unsupported provider download descriptor',retryable=False,state='invalid_metadata')
            response=self.http.get(download['href'],max_bytes)
            digest=hashlib.sha256(response.body).hexdigest()
            if (r['size'] is not None and len(response.body)!=r['size']) or (r['expected_sha256'] and digest!=r['expected_sha256']):
                raise FetchFailure('Downloaded bytes do not match inventoried size/SHA256',state='integrity_error')
            with self.store.transaction():
                sha=self.store.save_blob(response.body)
                self.store.db.execute('INSERT OR IGNORE INTO resource_contents VALUES (?,?,?,?,?)',(rid,sha,response.content_type,r['metadata_sha256'],utc_now()))
                self.store.db.execute("UPDATE resources SET content_state='retrieved',access='public',updated_at=? WHERE id=?",(utc_now(),rid))
                self.store.db.execute("UPDATE resource_jobs SET state='done',last_error=NULL,updated_at=? WHERE resource_id=? AND stage='content'",(utc_now(),rid))
                self.store.event(self.key,'yandex_content','retrieved',response.status,body=response.body,details={'resource_id':rid,'path':path,'size':len(response.body),'sha256':sha})
            if extract: extract_resource(self.store,rid)
            return {'resource_id':rid,'state':'retrieved','bytes':len(response.body),'sha256':sha}
        except FetchFailure as exc:
            # No raw descriptor/error body: it can echo an ephemeral download URL.
            self.store.event(self.key,'yandex_content',exc.state,exc.status,str(exc),details={'resource_id':rid,'path':path})
            self.catalog.failure(rid,'content',exc)
            return {'resource_id':rid,'state':exc.state,'error':str(exc)}


def extract_resource(store,rid,input_sha256=None):
    from .content import inspect_content
    catalog=Catalog(store);r=catalog.get(rid)
    row=store.db.execute('''SELECT c.sha256,b.path FROM resource_contents c JOIN blobs b ON b.sha256=c.sha256
      WHERE c.resource_id=? AND (? IS NULL OR c.sha256=?) ORDER BY c.retrieved_at DESC,c.rowid DESC LIMIT 1''',
      (rid,input_sha256,input_sha256)).fetchone()
    if not row: raise ValueError('Resource bytes have not been retrieved')
    sha,path=row
    body=(store.path.parent/path).read_bytes()
    if hashlib.sha256(body).hexdigest()!=sha: raise ValueError('Stored content failed SHA256 verification')
    result=inspect_content(body,r['name'])
    existing=store.db.execute("SELECT state FROM derived_artifacts WHERE resource_id=? AND input_sha256=? AND extractor=? AND state IN ('text_extracted','archive_inspected')",(rid,sha,result['extractor'])).fetchone()
    if existing:
        store.db.execute('UPDATE resources SET content_state=? WHERE id=?',(existing[0],rid))
        store._commit()
        return
    with store.transaction():
        for member in result.get('members',[]):
            member_body=member.pop('body',None)
            if member_body is None: continue
            member_meta={'path':'/'+member['name'],'name':member['name'],'type':'archive_member','size':len(member_body),'sha256':hashlib.sha256(member_body).hexdigest()}
            cid=catalog.upsert('archive',rid,member_meta,sha,'archive:'+rid)
            catalog.edge(rid,cid,'archive_member',sha)
            child_sha=store.save_blob(member_body)
            store.db.execute('INSERT OR IGNORE INTO resource_contents VALUES (?,?,?,?,?)',(cid,child_sha,'application/octet-stream',sha,utc_now()))
            member['resource_id']=cid
            if member.get('text'):
                text_sha=store.save_blob(member['text'].encode('utf-8'))
                store.db.execute('INSERT OR IGNORE INTO derived_artifacts VALUES (?,?,?,?,?,?,?)',
                    (cid,child_sha,text_sha,member.get('extractor',result['extractor']),'text_extracted',json.dumps({'member':member['name']},ensure_ascii=False),utc_now()))
                store.db.execute("UPDATE resources SET content_state='text_extracted' WHERE id=?",(cid,))
        text=result.pop('text','')
        text_sha=store.save_blob(text.encode('utf-8') if text else json.dumps(result,ensure_ascii=False).encode())
        store.db.execute('''INSERT INTO derived_artifacts VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(resource_id,input_sha256,extractor) DO UPDATE SET artifact_sha256=excluded.artifact_sha256,
            state=excluded.state,metadata_json=excluded.metadata_json,created_at=excluded.created_at''',
            (rid,sha,text_sha,result['extractor'],result['state'],json.dumps(result,ensure_ascii=False),utc_now()))
        store.db.execute('UPDATE resources SET content_state=? WHERE id=?',(result['state'],rid))
        store.event(r['source_key'],'extract',result['state'],details={'resource_id':rid,'input_sha256':sha,'extractor':result['extractor'],'text_characters':len(text)})
