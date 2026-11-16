"""Bounded public network worker; robots, explicit host scope and durable failures."""
from __future__ import annotations
import ipaddress
import os
import socket
import shutil
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
    def __init__(self, store, allowed, transport=None, delay=1.0, max_bytes=32*1024*1024):
        self.store,self.allowed,self.delay,self.max_bytes = store,set(allowed),delay,max_bytes
        self.client = httpx.Client(timeout=httpx.Timeout(25,connect=12),follow_redirects=False,
                                   headers={"User-Agent":AGENT},transport=transport,trust_env=False)
        self.robots = {}
        self.last = {}
        self.requests = self.bytes = 0
        self.request_limit = self.byte_limit = self.deadline = None
        self.crawler = None
        self.min_free_bytes = 1024*1024*1024

    def check_budget(self):
        if self.request_limit is not None and self.requests>=self.request_limit:
            raise BudgetExceeded('request_limit')
        if self.byte_limit is not None and self.bytes>=self.byte_limit:
            raise BudgetExceeded('total_byte_limit')
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise BudgetExceeded('time_limit')
        if shutil.disk_usage(self.store.home).free < self.min_free_bytes+self.max_bytes:
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
                cap = limit or self.max_bytes
                length=r.headers.get('content-length','')
                if length.isdigit() and int(length)>cap:
                    raise DeferredAccess('file_byte_limit; larger-media streaming not implemented')
                body = bytearray()
                for chunk in r.iter_bytes():
                    self.bytes+=len(chunk)
                    if self.byte_limit is not None and self.bytes>self.byte_limit:
                        raise BudgetExceeded('total_byte_limit')
                    if len(body)+len(chunk)>cap:
                        raise DeferredAccess("file_byte_limit; larger-media streaming not implemented")
                    if time.monotonic()>deadline:
                        raise BudgetExceeded('time_limit')
                    body.extend(chunk)
                return r.status_code,dict(r.headers),bytes(body),str(r.url)
        raise AccessError("Redirect limit")

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
                self.check_robots(route)
                status,headers,data,final = self.request(route)
                if status != 200:
                    transient = status in {408,429,500,502,503,504}
                    error = f"HTTP {status}"
                    self.store.attempt(rid,"unavailable",status,error)
                    retry_after = headers.get("retry-after","")
                    delay = min(86400,int(retry_after)) if retry_after.isdigit() else min(3600,30*2**job["tries"])
                    self.store.finish(job,"retry" if transient and job["tries"]<4 else "blocked",error,delay)
                else:
                    sid = self.store.snapshot(rid,data,headers.get("content-type","application/octet-stream"),final)
                    disposition=Message(); disposition['Content-Disposition']=headers.get('content-disposition','')
                    name=disposition.get_filename() or unquote(urlsplit(url).path.rsplit('/',1)[-1]) or 'document'
                    with self.store.db:
                        self.store.db.execute('INSERT OR IGNORE INTO retrieval_metadata VALUES(?,?)',(sid,name))
                        self.store.db.execute('UPDATE jobs SET refresh=0 WHERE id=? AND token=?',(job['id'],job['token']))
                    self.store.attempt(rid,"obtained",status,sid=sid)
                    try:
                        extraction = extract(self.store,sid)
                    except Exception as e:
                        extraction = "extraction_error:"+type(e).__name__+":"+str(e)[:300]
                        self.store.attempt(rid,"extraction_error",error=extraction,sid=sid)
                    self.store.finish(job,"done",extraction if extraction.startswith("extraction_error") else None)
                done.append({"url":url,"http":status})
            except BudgetExceeded as e:
                self.store.finish(job,'pending','budget_deferred:'+str(e))
                with self.store.db:
                    self.store.db.execute('UPDATE jobs SET tries=max(0,tries-1) WHERE id=?',(job['id'],))
                stop=str(e)
                break
            except (AccessError,httpx.HTTPError,OSError) as e:
                error = type(e).__name__+":"+str(e)[:400]
                self.store.attempt(rid,"blocked" if isinstance(e,AccessError) else "network_error",error=error)
                state = 'deferred' if isinstance(e,DeferredAccess) else ("blocked" if isinstance(e,AccessError) or job["tries"]>=4 else "retry")
                self.store.finish(job,state,error,30*2**job["tries"])
                done.append({"url":url,"error":error})
        if crawler:
            crawler.expand_cached()
            result={**crawler.summary(),'processed':done,'requests':self.requests,'bytes_received':self.bytes,
                    'stop':stop,'limits':{'jobs':budget,'requests':max_requests,'bytes':total_bytes,
                    'file_bytes':self.max_bytes,'seconds':seconds,'free_disk_reserve':self.min_free_bytes}}
            crawler.record_run(result)
            return result
        return done
