"""Bounded public network worker; robots, explicit host scope and durable failures."""
from __future__ import annotations
import ipaddress
import os
import socket
import shutil
import re
import time
from urllib.parse import urlsplit, urljoin, unquote
from email.message import Message
from urllib.robotparser import RobotFileParser
import httpx
from .store import canonical
from .ingest import extract
from .providers import download_route, crawl_route

AGENT = "FKNAtlas/1.0 (personal educational archive; bounded single-worker retrieval)"


class AccessError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class DeferredAccess(AccessError):
    pass


class SegmentComplete(Exception):
    """A valid server-limited range ended; durable job can fetch the next segment."""


def validate_url(url, allowed):
    p = urlsplit(canonical(url))
    if p.scheme not in {"http","https"} or p.hostname not in allowed or p.port not in {None,80,443}:
        raise AccessError("Host/scheme/port outside explicit scope")
    addresses = socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=="https" else 80),type=socket.SOCK_STREAM)
    # Some managed desktops resolve all public hosts through a synthetic DNS proxy.
    # Explicit opt-in is narrow: no loopback, RFC1918 or link-local exceptions.
    proxy = os.environ.get("ATLAS_BENCHMARK_DNS_PROXY") == "1"
    def public(address):
        ip = ipaddress.ip_address(address)
        return ip.is_global or (proxy and ip in ipaddress.ip_network("198.18.0.0/15"))
    if not addresses or any(not public(a[4][0]) for a in addresses):
        raise AccessError("Non-public network address")


class Fetcher:
    def __init__(self, store, allowed, transport=None, delay=1.0, max_bytes=1024*1024*1024):
        self.store,self.allowed,self.delay,self.max_bytes = store,set(allowed),delay,max_bytes
        self.client = httpx.Client(timeout=httpx.Timeout(25,connect=12),follow_redirects=False,
                                   headers={"User-Agent":AGENT},transport=transport,trust_env=False)
        self.robots = {}
        self.last = {}
        self.requests = self.bytes = 0
        self.request_limit = self.byte_limit = self.deadline = None
        self.crawler = None
        self.min_free_bytes = 1024*1024*1024
        self.max_storage_bytes = 20*1024*1024*1024
        self.used_bytes = store.payload_bytes()

    def check_time(self):
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise BudgetExceeded('time_limit')

    def check_budget(self, incoming=0, request=True):
        if request and self.request_limit is not None and self.requests>=self.request_limit:
            raise BudgetExceeded('request_limit')
        if request and self.byte_limit is not None and self.bytes>=self.byte_limit:
            raise BudgetExceeded('total_byte_limit')
        self.check_time()
        if self.used_bytes+incoming>self.max_storage_bytes:
            raise BudgetExceeded('storage_quota')
        if shutil.disk_usage(self.store.home).free < self.min_free_bytes+incoming:
            raise BudgetExceeded('free_disk_reserve')

    def request(self, url, limit=None, deadline=None, robots_request=False):
        deadline = deadline or time.monotonic()+70
        if self.deadline is not None: deadline=min(deadline,self.deadline)
        for _ in range(6):
            self.check_budget()
            validate_url(url,self.allowed)
            host = urlsplit(url).hostname
            pause=max(0,self.delay-(time.monotonic()-self.last.get(host,0)))
            if time.monotonic()+pause>=deadline: raise BudgetExceeded('time_limit')
            time.sleep(pause)
            self.last[host] = time.monotonic()
            self.check_budget()
            self.requests+=1
            with self.client.stream("GET",url,timeout=max(.1,min(25,deadline-time.monotonic()))) as r:
                if r.status_code in {301,302,303,307,308}:
                    if "location" not in r.headers:
                        raise AccessError("Redirect has no Location")
                    redirected = urljoin(url,r.headers["location"])
                    if self.crawler and urlsplit(redirected).hostname!=host:
                        raise DeferredAccess('redirect_host_needs_scope_review: '+urlsplit(redirected).netloc)
                    url = redirected
                    validate_url(url,self.allowed)
                    if not robots_request:
                        self.check_robots(url)
                    continue
                cap = limit or min(self.max_bytes,32*1024*1024)
                length=r.headers.get('content-length','')
                if length.isdigit() and int(length)>cap:
                    raise DeferredAccess('response_byte_limit')
                body = bytearray()
                for chunk in r.iter_bytes():
                    self.bytes+=len(chunk)
                    if self.byte_limit is not None and self.bytes>self.byte_limit:
                        raise BudgetExceeded('total_byte_limit')
                    if len(body)+len(chunk)>cap:
                        raise DeferredAccess('response_byte_limit')
                    if time.monotonic()>deadline:
                        raise BudgetExceeded('time_limit')
                    body.extend(chunk)
                return r.status_code,dict(r.headers),bytes(body),str(r.url)
        raise AccessError("Redirect limit")

    def download(self, job, url):
        """Identity-encoded bytes on disk; resume only with a strong entity validator."""
        from .transfers import Transfer
        transfer=Transfer(self.store,job,self.check_time)
        self.used_bytes=self.store.payload_bytes()
        if transfer.row['state']=='complete':
            return 200,{},transfer.publish(),transfer.row['final_url']
        etag=transfer.row['etag'] or ''
        if transfer.row['received'] and not re.fullmatch(r'"[^"\r\n]*"',etag):
            transfer.reset('strong_etag_unavailable')
            self.used_bytes=self.store.payload_bytes()
        for _ in range(8):
            self.check_budget()
            validate_url(url,self.allowed)
            host=urlsplit(url).hostname
            pause=max(0,self.delay-(time.monotonic()-self.last.get(host,0)))
            if self.deadline and time.monotonic()+pause>=self.deadline: raise BudgetExceeded('time_limit')
            time.sleep(pause); self.last[host]=time.monotonic(); self.check_budget()
            offset=transfer.row['received']
            request_headers={'Accept-Encoding':'identity'}
            if offset:
                request_headers.update({'Range':f'bytes={offset}-','If-Range':transfer.row['etag']})
            self.requests+=1
            timeout=max(.1,min(25,self.deadline-time.monotonic())) if self.deadline else 25
            with self.client.stream('GET',url,headers=request_headers,timeout=timeout) as response:
                status=response.status_code; headers=dict(response.headers); final=str(response.url)
                if status in {301,302,303,307,308}:
                    if 'location' not in headers: raise AccessError('Redirect has no Location')
                    target=urljoin(url,headers['location'])
                    if self.crawler and urlsplit(target).hostname!=host:
                        raise DeferredAccess('redirect_host_needs_scope_review: '+urlsplit(target).netloc)
                    validate_url(target,self.allowed); self.check_robots(target); url=target
                    continue
                if status==416 and offset:
                    transfer.reset('range_416_restart'); self.used_bytes=self.store.payload_bytes()
                    continue
                if status not in {200,206}: return status,headers,None,final
                if headers.get('content-encoding','identity').lower() not in {'','identity'}:
                    raise DeferredAccess('encoded_response_cannot_use_byte_checkpoint')
                length=headers.get('content-length')
                if length is not None and not re.fullmatch(r'\d+',length):
                    raise DeferredAccess('invalid_content_length')
                length=int(length) if length is not None else None
                if status==206:
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',headers.get('content-range',''))
                    if not offset or not match: raise DeferredAccess('invalid_content_range')
                    start,end,total=map(int,match.groups())
                    if start!=offset or not start<=end<total or (length is not None and length!=end-start+1):
                        raise DeferredAccess('invalid_content_range')
                    if headers.get('etag')!=transfer.row['etag'] or canonical(final)!=canonical(transfer.row['final_url']):
                        raise DeferredAccess('resume_validator_or_url_changed')
                    if transfer.row['total'] is not None and total!=transfer.row['total']:
                        raise DeferredAccess('resume_total_changed')
                    segment_length=end-start+1
                else:
                    if offset:
                        transfer.reset('range_answered_200_restart')
                        self.used_bytes=self.store.payload_bytes(); offset=0
                    total=length; segment_length=length
                disposition=Message(); disposition['Content-Disposition']=headers.get('content-disposition','')
                name=disposition.get_filename() or unquote(urlsplit(url).path.rsplit('/',1)[-1]) or 'document'
                transfer.headers(headers.get('etag'),headers.get('last-modified'),total,final,
                                 headers.get('content-type','application/octet-stream'),name)
                if total is not None and total>self.max_bytes:
                    raise DeferredAccess('file_byte_limit')
                received=0
                # Identity encoding avoids decompression/range ambiguities; buffering
                # can exceed a network budget by one bounded chunk, never a whole file.
                for block in response.iter_bytes(chunk_size=64*1024):
                    self.bytes+=len(block)
                    if segment_length is not None and received+len(block)>segment_length:
                        raise DeferredAccess('response_exceeds_declared_length')
                    if transfer.row['received']+len(block)>self.max_bytes:
                        raise DeferredAccess('file_byte_limit')
                    allowed=len(block)
                    if self.byte_limit is not None:
                        allowed=max(0,min(allowed,self.byte_limit-(self.bytes-len(block))))
                    if allowed:
                        self.check_budget(incoming=allowed,request=False)
                        transfer.append(block[:allowed]); self.used_bytes+=allowed
                    received+=len(block)
                    if allowed<len(block): raise BudgetExceeded('total_byte_limit')
                if segment_length is not None and received!=segment_length:
                    raise httpx.ReadError('Response ended before declared length')
                if total is not None and transfer.row['received']<total:
                    raise SegmentComplete('server_range_segment_complete')
                transfer.complete()
                return status,headers,transfer.publish(),final
        raise DeferredAccess('redirect_or_range_restart_limit')

    def check_robots(self, url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            status,_,data,_ = self.request(origin+"/robots.txt",limit=256*1024,robots_request=True)
            if status == 404:
                data = b"User-agent: *\nDisallow:"
            elif status != 200:
                raise AccessError(f"robots.txt unavailable: HTTP {status}; access deferred")
            rp = RobotFileParser()
            rp.parse(data.decode("utf8","replace").splitlines())
            self.robots[origin] = rp
        rp = self.robots[origin]
        if not rp.can_fetch(AGENT,url):
            raise AccessError("robots.txt disallows URL")
        self.delay = max(self.delay,rp.crawl_delay(AGENT) or 0)

    def run(self, budget=10, crawler=None, max_requests=100, total_bytes=128*1024*1024, seconds=300):
        self.requests=self.bytes=0
        self.request_limit,self.byte_limit,self.deadline=max_requests,total_bytes,time.monotonic()+seconds
        self.crawler=crawler
        self.used_bytes=self.store.payload_bytes()
        done = []
        stop='job_budget'
        for _ in range(budget):
            if crawler: crawler.expand_cached()
            try: self.check_budget()
            except BudgetExceeded as e: stop=str(e); break
            job = self.store.claim(lease_seconds=600,allowed_hosts=None if crawler else self.allowed,
                                   resource_ids=crawler.ids() if crawler else None,acquisition_first=bool(crawler))
            if not job:
                stop='no_ready_jobs'
                break
            rid = job["resource_id"]
            url = self.store.rows("SELECT url FROM resources WHERE id=?",(rid,))[0]["url"]
            try:
                old=self.store.rows('SELECT id FROM snapshots WHERE resource_id=? ORDER BY created DESC LIMIT 1',(rid,))
                if old and not job['refresh']:
                    if self.store.rows("SELECT 1 FROM transfers WHERE resource_id=? AND state='complete'",(rid,)):
                        from .transfers import Transfer
                        Transfer(self.store,job,self.check_time).publish()
                    error=None
                    try: extract(self.store,old[0]['id'])
                    except Exception as e:
                        error='extraction_error:'+type(e).__name__+':'+str(e)[:300]
                        self.store.attempt(rid,'extraction_error',error=error,sid=old[0]['id'])
                    self.store.finish(job,'done',error)
                    done.append({'url':url,'cached':True})
                    continue
                route=crawl_route(url) if crawler else download_route(url)
                if crawler:
                    self.allowed={urlsplit(url).hostname,urlsplit(route).hostname}
                if not self.store.rows("SELECT 1 FROM transfers WHERE resource_id=? AND state='complete'",(rid,)):
                    self.check_robots(route)
                status,headers,sid,final = self.download(job,route)
                if status not in {200,206}:
                    transient = status in {408,429,500,502,503,504}
                    error = f"HTTP {status}"
                    self.store.attempt(rid,"unavailable",status,error)
                    retry_after = headers.get("retry-after","")
                    delay = min(86400,int(retry_after)) if retry_after.isdigit() else min(3600,30*2**job["tries"])
                    self.store.finish(job,"retry" if transient and job["tries"]<4 else "blocked",error,delay)
                else:
                    self.store.attempt(rid,"obtained",status,sid=sid)
                    try:
                        extraction = extract(self.store,sid)
                    except Exception as e:
                        extraction = "extraction_error:"+type(e).__name__+":"+str(e)[:300]
                        self.store.attempt(rid,"extraction_error",error=extraction,sid=sid)
                    self.store.finish(job,"done",extraction if extraction.startswith("extraction_error") else None)
                done.append({"url":url,"http":status})
                self.used_bytes=self.store.payload_bytes()
            except SegmentComplete as e:
                self.store.finish(job,'pending',str(e))
                with self.store.db:
                    self.store.db.execute('UPDATE jobs SET tries=max(0,tries-1) WHERE id=?',(job['id'],))
                done.append({'url':url,'partial':True})
            except BudgetExceeded as e:
                self.store.finish(job,'pending','budget_deferred:'+str(e))
                with self.store.db:
                    self.store.db.execute('UPDATE jobs SET tries=max(0,tries-1) WHERE id=?',(job['id'],))
                stop=str(e)
                break
            except (AccessError,httpx.HTTPError,OSError,ValueError) as e:
                error = type(e).__name__+":"+str(e)[:400]
                self.store.attempt(rid,"blocked" if isinstance(e,AccessError) else "network_error",error=error)
                state = 'deferred' if isinstance(e,DeferredAccess) else ("blocked" if isinstance(e,(AccessError,ValueError)) or job["tries"]>=4 else "retry")
                self.store.finish(job,state,error,30*2**job["tries"])
                done.append({"url":url,"error":error})
        if crawler:
            crawler.expand_cached()
            result={**crawler.summary(),'processed':done,'requests':self.requests,'bytes_received':self.bytes,
                    'stop':stop,'limits':{'jobs':budget,'requests':max_requests,'bytes':total_bytes,
                    'file_bytes':self.max_bytes,'seconds':seconds,'free_disk_reserve':self.min_free_bytes,
                    'storage_quota':self.max_storage_bytes}}
            crawler.record_run(result)
            return result
        return done
