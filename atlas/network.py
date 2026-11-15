"""Bounded public network worker; robots, explicit host scope and durable failures."""
from __future__ import annotations
import ipaddress
import os
import socket
import time
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import httpx
from .store import canonical
from .ingest import extract
from .providers import download_route

AGENT = "FKNAtlas/1.0 (personal educational archive; bounded single-worker retrieval)"


class AccessError(Exception):
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

    def request(self, url, limit=None, deadline=None, robots_request=False):
        deadline = deadline or time.monotonic()+70
        for _ in range(6):
            validate_url(url,self.allowed)
            host = urlsplit(url).hostname
            time.sleep(max(0,self.delay-(time.monotonic()-self.last.get(host,0))))
            self.last[host] = time.monotonic()
            with self.client.stream("GET",url) as r:
                if r.status_code in {301,302,303,307,308}:
                    if "location" not in r.headers:
                        raise AccessError("Redirect has no Location")
                    url = urljoin(url,r.headers["location"])
                    validate_url(url,self.allowed)
                    if not robots_request:
                        self.check_robots(url)
                    continue
                cap = limit or self.max_bytes
                body = bytearray()
                for chunk in r.iter_bytes():
                    if len(body)+len(chunk)>cap:
                        raise AccessError("Response exceeds byte limit")
                    if time.monotonic()>deadline:
                        raise AccessError("Response exceeds time limit")
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

    def run(self, budget=10):
        done = []
        for _ in range(budget):
            job = self.store.claim(lease_seconds=600,allowed_hosts=self.allowed)
            if not job:
                break
            rid = job["resource_id"]
            url = self.store.rows("SELECT url FROM resources WHERE id=?",(rid,))[0]["url"]
            try:
                route=download_route(url)
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
                    self.store.attempt(rid,"obtained",status,sid=sid)
                    try:
                        extraction = extract(self.store,sid)
                    except Exception as e:
                        extraction = "extraction_error:"+type(e).__name__+":"+str(e)[:300]
                        self.store.attempt(rid,"extraction_error",error=extraction,sid=sid)
                    self.store.finish(job,"done",extraction if extraction.startswith("extraction_error") else None)
                done.append({"url":url,"http":status})
            except (AccessError,httpx.HTTPError,OSError) as e:
                error = type(e).__name__+":"+str(e)[:400]
                self.store.attempt(rid,"blocked" if isinstance(e,AccessError) else "network_error",error=error)
                state = "blocked" if isinstance(e,AccessError) or job["tries"]>=4 else "retry"
                self.store.finish(job,state,error,30*2**job["tries"])
                done.append({"url":url,"error":error})
        return done
