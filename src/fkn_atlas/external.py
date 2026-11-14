"""Bounded public provider transport. Credentials and transient download URLs are never logged."""
from dataclasses import dataclass
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import json
import math
from http.client import IncompleteRead
import time
from .crawler import USER_AGENT, FetchFailure, retry_after_seconds


@dataclass
class Response:
    body: bytes
    status: int
    content_type: str


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicHTTP:
    def __init__(self, store, allowed_host, delay=1.5):
        if not math.isfinite(delay) or delay < 1:
            raise ValueError('Finite delay >= 1 second required')
        self.store, self.allowed_host, self.delay = store, allowed_host, delay
        self.last = None
        self.robots = {}
        self.opener = build_opener(NoRedirect())

    def validate(self, url):
        try:
            p = urlsplit(url)
            valid = p.scheme == 'https' and not p.username and not p.password and p.port in (None,443) and self.allowed_host(p.hostname or '')
        except (ValueError,TypeError):
            raise FetchFailure('Malformed provider URL', retryable=False, state='invalid_url')
        if not valid:
            raise FetchFailure('Provider URL outside allowed HTTPS hosts', retryable=False, state='out_of_scope')
        return p

    def wait(self):
        if self.last is not None:
            time.sleep(max(0,self.delay-(time.monotonic()-self.last)))
        self.last=time.monotonic()

    def _request(self,url,limit):
        p=self.validate(url)
        deadline=float(self.store.setting('external_not_before:'+p.netloc,'0'))
        if time.time()<deadline:
            raise FetchFailure('Host cooldown is active',retry_after=deadline-time.time(),state='host_backoff')
        self.wait()
        req=Request(url,headers={'User-Agent':USER_AGENT,'Accept-Encoding':'identity'})
        try:
            response=self.opener.open(req,timeout=25)
        except HTTPError as exc:
            response=exc
        except (URLError,TimeoutError,OSError) as exc:
            # Keep an explicit tunnel-denial diagnosis without persisting the requested signed URL.
            denied='Tunnel connection failed: 403' in str(exc)
            raise FetchFailure('Proxy tunnel returned 403' if denied else 'Transport failure: '+type(exc).__name__,
                retryable=not denied,state='platform_restricted' if denied else 'network_error') from exc
        with response:
            headers=response.headers
            try:
                body=response.read(limit+1)
            except (TimeoutError,OSError,IncompleteRead) as exc:
                raise FetchFailure('Incomplete provider response: '+type(exc).__name__,state='network_error') from exc
            status=response.code
            if len(body)>limit:
                raise FetchFailure('Response byte limit exceeded',status=status,retryable=False,state='too_large')
            if status in (301,302,303,307,308):
                return None,urljoin(url,headers.get('Location',''))
            if not 200<=status<300:
                retry=retry_after_seconds(headers.get('Retry-After'))
                if status in (429,503) or retry:
                    self.store.set_setting('external_not_before:'+p.netloc,time.time()+max(60,retry))
                raise FetchFailure('HTTP '+str(status),status=status,retryable=status>=500 or status==429,
                    retry_after=retry,body=body,state='http_error')
            if status !=200 or headers.get('Content-Encoding','identity').lower() not in ('','identity'):
                raise FetchFailure('Unsupported partial/encoded response',status=status,retryable=False,state='unsupported_response')
            return Response(body,status,headers.get_content_type()),None

    def check_robots(self,url):
        p=self.validate(url)
        origin='https://'+p.netloc
        if origin not in self.robots:
            robots_url=origin+'/robots.txt'
            current=robots_url
            try:
                for _ in range(4):
                    result,redirect=self._request(current,512*1024)
                    if redirect:
                        target=self.validate(redirect)
                        if target.netloc!=p.netloc or target.path.lower()!='/robots.txt':
                            raise FetchFailure('Unsafe robots redirect',retryable=False,state='robots_unavailable')
                        current=redirect
                        continue
                    if result.body.lstrip().lower().startswith((b'<!doctype html',b'<html')):
                        raise FetchFailure('HTML returned for robots',status=result.status,body=result.body,state='robots_unavailable')
                    robot=RobotFileParser()
                    robot.parse(result.body.decode('utf-8',errors='replace').splitlines())
                    self.delay=max(self.delay,robot.crawl_delay(USER_AGENT) or robot.crawl_delay('*') or 0)
                    rate=robot.request_rate(USER_AGENT) or robot.request_rate('*')
                    if rate and rate.requests: self.delay=max(self.delay,rate.seconds/rate.requests)
                    self.robots[origin]=robot
                    self.store.event(robots_url,'external_robots','parsed',result.status,body=result.body)
                    break
                else:
                    raise FetchFailure('Too many robots redirects',retryable=False,state='robots_unavailable')
            except FetchFailure as exc:
                self.store.event(robots_url,'external_robots','absent' if exc.status in (404,410) else 'unavailable',exc.status,str(exc),exc.body)
                if exc.status in (404,410):
                    self.robots[origin]=None
                else:
                    raise
        robot=self.robots[origin]
        if robot is not None and not robot.can_fetch(USER_AGENT,url):
            raise FetchFailure('Disallowed by robots',retryable=False,state='robots_denied')

    def get(self,url,limit=2*1024*1024):
        current=url
        for _ in range(6):
            self.check_robots(current)
            result,redirect=self._request(current,limit)
            if redirect:
                self.validate(redirect)
                current=redirect
            else:
                return result
        raise FetchFailure('Too many redirects',retryable=False,state='redirect_limit')

    def json(self,url):
        result=self.get(url)
        try:
            data=json.loads(result.body)
        except (ValueError,UnicodeError) as exc:
            raise FetchFailure('Invalid provider JSON',status=result.status,body=result.body,state='invalid_metadata') from exc
        if not isinstance(data,dict):
            raise FetchFailure('Provider JSON must be an object',status=result.status,state='invalid_metadata')
        return data,result
