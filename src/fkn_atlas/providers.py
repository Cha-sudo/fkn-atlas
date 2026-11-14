"""Small public provider adapters: metadata-only video and source-linked Git archive."""
import hashlib
import json
import re
from urllib.parse import urlencode,quote,urlsplit
from .external import PublicHTTP
from .crawler import FetchFailure
from .resources import Catalog,stable_id,extract_resource
from .storage import utc_now
from .urls import normalize_url


class YouTubeResourceAdapter:
    def __init__(self,store,http=None):
        self.store=store
        self.http=http or PublicHTTP(store,lambda host:host in ('www.youtube.com','youtube.com'))

    def probe(self,video_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id): raise ValueError('Expected an 11-character video ID')
        url='https://www.youtube.com/watch?v='+video_id
        catalog=Catalog(self.store)
        rid=catalog.upsert('youtube',url,{'path':'/','name':video_id,'type':'video'})
        try:
            meta,response=self.http.json('https://www.youtube.com/oembed?'+urlencode({'url':url,'format':'json'}))
            if meta.get('type')!='video' or not isinstance(meta.get('title'),str):
                raise FetchFailure('Invalid video metadata',retryable=False,body=response.body,state='invalid_metadata')
            with self.store.transaction():
                sha=self.store.save_blob(response.body)
                catalog.upsert('youtube',url,{'path':'/','name':meta['title'],'type':'video'},sha,url)
                self.store.event(url,'youtube_metadata','metadata_only',response.status,body=response.body,
                    details={'resource_id':rid,'origin':'explicit_video_probe','content_analyzed':False})
            return {'resource_id':rid,'state':'metadata_only','title':meta['title']}
        except FetchFailure as exc:
            self.store.event(url,'youtube_metadata',exc.state,exc.status,str(exc),exc.body,
                details={'resource_id':rid,'content_obtained':False})
            self.store.db.execute('UPDATE resources SET access=? WHERE id=?',('platform_restricted' if exc.state=='platform_restricted' else 'unknown',rid))
            self.store._commit()
            return {'resource_id':rid,'state':exc.state,'error':str(exc)}


class GitHubArchiveAdapter:
    def __init__(self,store,http=None):
        self.store=store
        self.http=http or PublicHTTP(store,lambda host:host in ('api.github.com','codeload.github.com'))

    def fetch(self,repository,source_url,max_bytes=8*1024*1024):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository): raise ValueError('Expected owner/repository')
        if not 1<=max_bytes<=32*1024*1024: raise ValueError('Archive byte limit must be 1..32 MiB')
        repo_url='https://github.com/'+repository
        occurrence=self.store.db.execute('SELECT id FROM link_occurrences WHERE source_url=? AND target_url=? LIMIT 1',(normalize_url(source_url),repo_url)).fetchone()
        if occurrence is None: raise ValueError('Repository must be linked from a saved source page')
        api='https://api.github.com/repos/'+repository
        try:
            meta,response=self.http.json(api)
            canonical=meta.get('full_name','')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',canonical) or not meta.get('default_branch'):
                raise FetchFailure('Invalid repository metadata',retryable=False,state='invalid_metadata')
            self.store.event(api,'github_metadata','metadata_only',response.status,body=response.body,
                details={'source_occurrence_id':occurrence[0],'canonical_repository':canonical})
            commit,commit_response=self.http.json('https://api.github.com/repos/'+canonical+'/commits/'+quote(meta['default_branch'],safe=''))
            revision=commit.get('sha','')
            if not re.fullmatch(r'[0-9a-f]{40}',revision): raise FetchFailure('Invalid commit identity',retryable=False,state='invalid_metadata')
            archive_url='https://codeload.github.com/'+canonical+'/zip/'+revision
            catalog=Catalog(self.store)
            with self.store.transaction():
                msha=self.store.save_blob(commit_response.body)
                rid=catalog.upsert('github_archive',repo_url,{'path':'/archive/'+revision+'.zip','name':canonical.split('/')[1]+'.zip','type':'file'},msha,api)
            existing=self.store.db.execute('SELECT sha256 FROM resource_contents WHERE resource_id=?',(rid,)).fetchone()
            if existing:
                extract_resource(self.store,rid)
                return {'resource_id':rid,'state':'already_retrieved','revision':revision}
            archive=self.http.get(archive_url,max_bytes)
            if not archive.body.startswith(b'PK\x03\x04'):
                raise FetchFailure('Response is not a ZIP archive',status=archive.status,retryable=False,state='invalid_archive')
            with self.store.transaction():
                sha=self.store.save_blob(archive.body)
                self.store.db.execute('INSERT OR IGNORE INTO resource_contents VALUES (?,?,?,?,?)',(rid,sha,archive.content_type,msha,utc_now()))
                self.store.db.execute("UPDATE resources SET content_state='retrieved',size=?,access='public' WHERE id=?",(len(archive.body),rid))
                self.store.event(archive_url,'github_archive','retrieved',archive.status,body=archive.body,
                    details={'resource_id':rid,'source_occurrence_id':occurrence[0],'source_url':source_url,'repository':canonical,'revision':revision})
            extract_resource(self.store,rid)
            return {'resource_id':rid,'state':'retrieved','revision':revision,'bytes':len(archive.body),'sha256':sha}
        except FetchFailure as exc:
            self.store.event(api,'github_archive',exc.state,exc.status,str(exc),exc.body,details={'source_occurrence_id':occurrence[0]})
            return {'state':exc.state,'error':str(exc)}
