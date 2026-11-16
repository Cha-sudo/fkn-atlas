"""Provider classification and public metadata routes; no login or scraping bypass."""
import json
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote


def crawl_route(url):
    """Public documented API instead of an interactive provider landing page."""
    p=urlsplit(url)
    if provider(url) in {'youtube','yandex_disk'}:
        return metadata_route(url)
    if p.hostname=='github.com':
        parts=p.path.strip('/').split('/')
        if len(parts)==2:
            return 'https://api.github.com/repos/'+'/'.join(parts)
        if len(parts)==4 and parts[2]=='tree':
            return 'https://api.github.com/repos/'+'/'.join(parts[:2])+'/commits/'+parts[3]
    return download_route(url)


def provider_links(store, sid):
    """Idempotent observed provider entries and explicit listing boundaries.

    SHA of a Git tree is NOT a commit SHA. Raw URLs are emitted only with the
    immutable commit obtained from a commit response; never from a guessed ref.
    """
    from .store import uid
    snap=store.rows('SELECT * FROM snapshots WHERE id=?',(sid,))[0]
    url=snap['final_url']; p=urlsplit(url)
    kind=provider(url)
    if p.hostname not in {'api.github.com','cloud-api.yandex.net','www.youtube.com','drive.google.com','docs.google.com','github.com'}:
        return
    ctxs=store.rows('SELECT context FROM provider_contexts WHERE resource_id=?',(snap['resource_id'],))
    ctx=json.loads(ctxs[0]['context']) if ctxs else {}
    def state(name, details):
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO provider_states VALUES(?,?,?)',
                             (sid,name,json.dumps(details,ensure_ascii=False)))
    def link(key, target, label, context, child_context=None):
        rid=store.resource(target,'provider_entry'); path='/provider/'+str(key); node=uid(sid,path)
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?,?)',
                (node,sid,None,0,'provider_entry',path,context,'[]'))
            store.db.execute('INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?,?,?)',
                (uid(node,'link'),sid,node,rid,target,label,context,'[]',path))
            if child_context:
                store.db.execute('INSERT OR IGNORE INTO provider_contexts VALUES(?,?)',
                                 (rid,json.dumps(child_context,ensure_ascii=False)))
        return rid
    if kind=='google_drive':
        state('landing_only',{'complete':False,'reason':'public folder listing adapter unavailable'})
        return
    try: obj=json.loads(store.object_path(snap['sha']).read_bytes())
    except (ValueError,UnicodeDecodeError):
        state('landing_only',{'complete':False,'reason':'interactive HTML is not a provider listing'})
        return
    if not isinstance(obj,dict): return
    if p.hostname=='www.youtube.com':
        state('video_metadata_only',{'complete':False,'video_obtained':False,'transcript_obtained':False})
    elif p.hostname=='api.github.com':
        parts=p.path.split('/'); repo='/'.join(parts[2:4])
        if len(parts)<4: return
        if 'default_branch' in obj:
            branch=obj['default_branch']
            link('commit','https://api.github.com/repos/'+repo+'/commits/'+quote(branch,safe=''),branch,'Public repository default branch; resolve immutable commit')
            state('repository_metadata_only',{'complete':False})
        elif isinstance(obj.get('commit'),dict) and re.fullmatch('[0-9a-f]{40}',obj.get('sha','')):
            tree=obj['commit'].get('tree',{}).get('sha','')
            if re.fullmatch('[0-9a-f]{40}',tree):
                link('tree','https://api.github.com/repos/'+repo+'/git/trees/'+tree+'?recursive=1',
                     'Repository tree','Pinned commit '+obj['sha'],{'repo':repo,'commit':obj['sha']})
            state('commit_metadata_only',{'complete':False,'commit':obj['sha']})
        elif isinstance(obj.get('tree'),list):
            commit=ctx.get('commit','')
            pinned=bool(re.fullmatch('[0-9a-f]{40}',commit) and ctx.get('repo')==repo)
            if pinned:
                for n,item in enumerate(obj['tree']):
                    if item.get('type')!='blob' or item.get('mode')=='120000': continue
                    path=item.get('path','')
                    if not path or '..' in path.split('/'): continue
                    target='https://raw.githubusercontent.com/'+repo+'/'+commit+'/'+quote(path,safe='/')
                    # Repository files are preserved, never executed. Binaries that
                    # are clearly tools rather than lessons remain pending.
                    pending=bool(re.search(r'\.(exe|dll|so|bat|cmd|ps1|msi)$',path,re.I))
                    prefix='PENDING:executable_not_educational:' if pending else ''
                    link(n,target,path,prefix+'Git file at '+commit+' path '+path)
            state('tree_listing_partial' if obj.get('truncated') or not pinned else 'tree_listing_received',
                  {'listing_complete':not obj.get('truncated',True) and pinned,
                   'materials_obtained':False,'entries':len(obj['tree']),
                   'reason':'truncated tree' if obj.get('truncated') else ('immutable commit missing' if not pinned else 'file queue remains separate')})
    elif p.hostname=='cloud-api.yandex.net':
        params=dict(parse_qsl(p.query)); key=params.get('public_key')
        if not key: return
        def resource_route(path, offset=0):
            return 'https://cloud-api.yandex.net/v1/disk/public/resources?'+urlencode({'public_key':key,'path':path,'limit':100,'offset':offset})
        if obj.get('type')=='dir':
            listing=obj.get('_embedded',{}); items=listing.get('items',[])
            offset=listing.get('offset',int(params.get('offset','0'))); total=listing.get('total')
            for n,item in enumerate(items):
                if item.get('path'):
                    link(n,resource_route(item['path']),item.get('name',item['path']),
                         'Public folder child '+item['path']+' type '+item.get('type','unknown'))
            next_offset=offset+len(items)
            more=isinstance(total,int) and next_offset<total
            if more and items:
                link('next',resource_route(params.get('path',obj.get('path','/')),next_offset),
                     'Next directory page','Bounded public API pagination offset '+str(next_offset))
            state('directory_page_received',{'offset':offset,'entries':len(items),'total':total,
                  'listing_complete':False,'page_reaches_end':isinstance(total,int) and next_offset>=total,
                  'reason':'descendant files and pages have separate jobs; no folder completeness inferred'})
        elif obj.get('type')=='file':
            href=obj.get('file')
            if href:
                # Signed URLs stay private; no arbitrary redirect or host expansion.
                target_host=urlsplit(href).hostname or ''
                trusted=(target_host.endswith('.disk.yandex.net') or target_host=='downloader.disk.yandex.ru') and urlsplit(href).scheme=='https'
                prefix='' if trusted else 'PENDING:download_host_unverified:'
                link('file',href,obj.get('name','file'),prefix+'Public file download '+obj.get('path',''))
            state('file_metadata_only',{'file_obtained':False,'download_link_found':bool(href)})


def provider(url):
    host = (urlsplit(url).hostname or "").lower()
    if host in {"fruitdet.ru", "wiki.cs.hse.ru", "wikics.site"}:
        return "mediawiki"
    if host in {"disk.yandex.ru", "yadi.sk", "disk.yandex.com"}:
        return "yandex_disk"
    if host in {"youtu.be", "www.youtube.com", "youtube.com"}:
        return "youtube"
    if host in {"drive.google.com", "docs.google.com"}:
        return "google_drive"
    if host in {"github.com", "gitlab.com", "raw.githubusercontent.com"}:
        return "code_host"
    if host in {"www.dropbox.com", "dropbox.com", "dl.dropboxusercontent.com"}:
        return "dropbox"
    return "local" if url.startswith("local:") else "web"


def metadata_route(url):
    kind = provider(url)
    if kind == "yandex_disk":
        return "https://cloud-api.yandex.net/v1/disk/public/resources?" + urlencode({"public_key":url,"limit":100,"offset":0})
    if kind == "youtube":
        return "https://www.youtube.com/oembed?" + urlencode({"url":url,"format":"json"})
    return None


def download_route(url):
    # Official public-link download switch; never changes permissions or access keys.
    # https://help.dropbox.com/share/force-download
    p=urlsplit(url)
    if provider(url)=="dropbox" and p.hostname in {"www.dropbox.com","dropbox.com"} and p.path.startswith(("/s/","/scl/fi/")):
        params=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k not in {"dl","raw"}]
        return urlunsplit((p.scheme,p.netloc,p.path,urlencode(params+[("dl","1")]),""))
    return url


def capability(url):
    return {"provider":provider(url),"metadata_url":metadata_route(url),
            "content_analyzed":False,"video":"Import original video + timed .vtt transcript; speech/board not inferred from metadata",
            "archives":"Inventory only; no extraction or execution","access":"Public URLs only; never bypass login/DRM/captcha"}
