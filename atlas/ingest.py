"""Structure-preserving HTML and conservative document extraction. Never executes content."""
from __future__ import annotations
import io
import json
import re
import time
import zipfile
import os
import shutil
import uuid
import logging
from contextlib import contextmanager
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from urllib.parse import urljoin, urlsplit, quote
from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader
from .store import uid

ENGINE = "atlas-extract-1"
try:
    FONT_VERSION = version('fonttools')
except PackageNotFoundError:
    FONT_VERSION = 'absent'
PDF_ENGINE = 'atlas-pdf-2;pypdf='+version('pypdf')+';fonttools='+FONT_VERSION
MAX_PARSE_BYTES = 32*1024*1024


@contextmanager
def pdf_diagnostics():
    """Bounded diagnostic evidence, rather than losing warnings in terminal output.

    Extraction is synchronous in this local worker. Restore logger state on failure.
    """
    result={'warning_count':0,'messages':{}}
    class Capture(logging.Handler):
        def emit(self, record):
            result['warning_count']+=1
            message=record.getMessage()[:600]
            if message in result['messages'] or len(result['messages'])<16:
                result['messages'][message]=result['messages'].get(message,0)+1
    logger=logging.getLogger('pypdf'); handler=Capture(logging.WARNING)
    previous=logger.propagate
    logger.addHandler(handler); logger.propagate=False
    try: yield result
    finally:
        logger.removeHandler(handler); logger.propagate=previous


def pdf_chunks(store, sid, data, base):
    chunks=[]
    reader=PdfReader(io.BytesIO(data))
    if reader.is_encrypted: return chunks,'encrypted'
    for n,p in enumerate(reader.pages,1):
        text=p.extract_text() or ''
        chunks.append((f'page:{n}',text,'needs_visual_review' if len(text.strip())>40 else 'needs_ocr'))
        for a,ref in enumerate(p.get('/Annots',[])):
            annotation=ref.get_object(); action=annotation.get('/A',{})
            if hasattr(action,'get_object'): action=action.get_object()
            href=action.get('/URI') if hasattr(action,'get') else None
            if href: document_link(store,sid,f'/page/{n}/link/{a}',str(href),str(href),text[:2000],base)
    return chunks,'text_extracted_needs_visual_review'


def html_structure(store, sid, data, base):
    soup = BeautifulSoup(data.decode("utf-8", "replace"), "html.parser")
    root = soup.select_one(".mw-parser-output") or soup.find("main") or soup.body or soup
    headings = []
    records, links, contexts = [], [], []
    ordinal = 0

    def walk(tag, path, parent=None):
        nonlocal ordinal
        if tag.name in {"script", "style", "noscript"}:
            return
        ordinal += 1
        ident = uid(sid, path)
        if re.fullmatch(r"h[1-6]", tag.name or ""):
            level = int(tag.name[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level,tag.get_text(" ",strip=True)))
        hp = json.dumps([h[1] for h in headings],ensure_ascii=False)
        txt = tag.get_text(" ",strip=True)
        # Large containers retain direct text; leaves, paragraphs, rows and lists keep full context.
        if len(txt)>20000:
            txt = " ".join(str(s).strip() for s in tag.find_all(string=True,recursive=False))
        records.append((ident,sid,parent,ordinal,tag.name or "document",path,txt,hp))
        if tag.name == "a" and tag.get("href"):
            href = tag["href"]
            if not href.startswith(("#","mailto:","javascript:","data:")):
                try:
                    rid = store.resource(urljoin(base,href))
                    context = tag.find_parent(["tr","li","p","dd"]) or tag.parent
                    links.append((uid(sid,path,"link"),sid,ident,rid,href,
                                  tag.get_text(" ",strip=True),context.get_text(" ",strip=True),hp,path))
                    navigation=any(a.name in {'nav','header','footer'} or a.get('role')=='navigation'
                        or set(a.get('class',[]))&{'mw-editsection','toc','navbox','navigation'}
                        for a in [tag,*tag.parents] if isinstance(a,Tag))
                    contexts.append((uid(sid,path,'link'),int(navigation)))
                except ValueError:
                    pass
        counts = {}
        for child in tag.children:
            if isinstance(child,Tag):
                counts[child.name] = counts.get(child.name,0)+1
                walk(child,f"{path}/{child.name}[{counts[child.name]}]",ident)
    walk(root,"/content")
    with store.db:
        store.db.executemany("INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?,?)", records)
        store.db.executemany("INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?,?,?)", links)
        store.db.executemany("INSERT OR IGNORE INTO link_context VALUES(?,?)",contexts)
    return [(f"dom:{r[5]}",r[6],"text_only") for r in records
            if r[4] in {"p","tr","li","h1","h2","h3","h4"} and r[6]]


def extract(store, sid, reprocess=False):
    row = store.rows("SELECT * FROM snapshots WHERE id=?",(sid,))[0]
    path=store.object_path(row['sha'])
    with path.open('rb') as f: prefix=f.read(32)
    engine=PDF_ENGINE if prefix.startswith(b'%PDF') else ENGINE
    eid = uid(sid,engine)
    old = store.rows("SELECT status FROM extractions WHERE id=?",(eid,))
    if old:
        return old[0]["status"]
    previous=store.rows('SELECT status FROM extractions WHERE snapshot_id=? ORDER BY created DESC LIMIT 1',(sid,))
    if previous and not reprocess:
        return previous[0]['status']  # Parser upgrade never silently reprocesses the corpus.
    mime = row["mime"].split(";")[0].lower()
    media=(mime.startswith(('video/','audio/')) or prefix[4:8]==b'ftyp'
           or urlsplit(row['final_url']).path.lower().endswith(('.mp4','.webm','.mkv','.mp3','.wav','.flac','.ogg','.mov','.m4a')))
    oversized=path.stat().st_size>MAX_PARSE_BYTES
    data=b'' if media or oversized else path.read_bytes()
    chunks = []
    diagnostics=None
    status = "unsupported"
    if media:
        status='media_untranscribed'
    elif oversized:
        status='archive_stored_unread' if prefix[:2]==b'PK' or 'zip' in mime else 'extraction_deferred_size'
    elif data.startswith(b"%PDF"):
        with pdf_diagnostics() as diagnostics:
            try: chunks,status=pdf_chunks(store,sid,data,row['final_url'])
            except Exception:
                store.attempt(row['resource_id'],'extraction_diagnostics',sid=sid,
                    error=json.dumps({'engine':engine,**diagnostics},ensure_ascii=False))
                raise
    elif "html" in mime or data.lstrip().lower().startswith((b"<!doctype html",b"<html")):
        chunks = html_structure(store,sid,data,row["final_url"])
        status = "text_extracted"
    elif data[:2]==b"PK":
        # Inventory paths only. No member extraction, shell execution, macros or remote OOXML relations.
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist())>10000 or sum(x.file_size for x in z.infolist())>512*1024*1024:
                status = "archive_limit"
            else:
                inventory = [{"name":x.filename,"size":x.file_size,"compressed":x.compress_size,
                              "unsafe_path":x.filename.startswith(("/","\\")) or ".." in x.filename.replace("\\","/").split("/") or ":" in x.filename}
                             for x in z.infolist()]
                chunks = [("archive:inventory",json.dumps(inventory,ensure_ascii=False),"metadata_only")]
                status = "archive_inventory_only"
    elif row["final_url"].lower().endswith((".vtt",".srt")) or data.startswith(b"WEBVTT"):
        text = data.decode("utf8","replace")
        for block in re.split(r"\r?\n\s*\r?\n",text):
            lines = block.splitlines()
            for i,line in enumerate(lines):
                if " --> " in line:
                    chunks.append(("time:"+line," ".join(lines[i+1:]),"transcript_not_board_review"))
        status = "timed_transcript_only"
    elif "json" in mime or row["final_url"].endswith(".ipynb"):
        obj = json.loads(data)
        from .providers import provider_links
        provider_links(store,sid)
        if isinstance(obj,dict) and "cells" in obj:
            for n,cell in enumerate(obj["cells"],1):
                chunks.append((f"cell:{n}","".join(cell.get("source",[])),"unexecuted_"+cell.get("cell_type","unknown")))
            status = "notebook_unexecuted"
        else:
            chunks = [("json:metadata",json.dumps(obj,ensure_ascii=False),"metadata_only")]
            status = "metadata_only"
    elif mime.startswith("text/"):
        text=data.decode("utf8","replace")
        chunks = [("text:1",text,"text_only")]
        for n,match in enumerate(re.finditer(r'\[([^\]]+)\]\(([^\s)]+)\)|\\(?:url|href)\{([^}]+)\}',text)):
            href=match.group(2) or match.group(3)
            document_link(store,sid,f'/text/link/{n}',href,match.group(1) or href,
                          text[max(0,match.start()-150):match.end()+150],row['final_url'])
        status = "text_extracted"
    elif mime.startswith(("video/","audio/")):
        status = "media_untranscribed"
    with store.db:
        store.db.execute("INSERT INTO extractions VALUES(?,?,?,?,?)",(eid,sid,engine,status,time.time()))
        for n,(loc,text,quality) in enumerate(chunks):
            cid = uid(eid,n,loc)
            store.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?)",(cid,eid,loc,text,quality))
            if text and quality != "metadata_only":
                store.db.execute("INSERT INTO chunks_fts(id,text) VALUES(?,?)",(cid,text))
    (store.home/"derived"/(eid+".json")).write_text(json.dumps({"snapshot":sid,"engine":engine,"status":status,"chunks":chunks,'diagnostics':diagnostics},ensure_ascii=False,indent=2),encoding="utf8",newline='\n')
    return status


def document_link(store,sid,path,href,label,context,base):
    try: rid=store.resource(urljoin(base,href))
    except ValueError: return
    node=uid(sid,path)
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?,?)',
                         (node,sid,None,0,'document_link',path,context,'[]'))
        store.db.execute('INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?,?,?)',
                         (uid(node,'link'),sid,node,rid,href,label,context,'[]',path))


def import_local(store, path, source_url=None):
    import mimetypes
    from .store import file_digest
    path = Path(path).resolve()
    before=path.stat()
    if before.st_size>32*1024**3: raise ValueError('Local import limit: 32 GiB')
    sha=file_digest(path)
    url = source_url or "local://import/"+sha+"/"+path.name
    rid = store.resource(url,"local_import")
    staging=store.home/'partials'/(rid+'.'+uuid.uuid4().hex+'.part')
    if not store.object_path(sha).exists():
        if store.payload_bytes()+before.st_size>20*1024**3:
            raise ValueError('Default local import storage quota: 20 GiB')
        with path.open('rb') as source, staging.open('xb') as dest:
            for block in iter(lambda:source.read(1024*1024),b''):
                if shutil.disk_usage(store.home).free<1024**3+len(block):
                    raise ValueError('Free disk reserve reached during import; private staging retained')
                dest.write(block)
            dest.flush(); os.fsync(dest.fileno())
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('Local file changed during import; staging retained, no snapshot published')
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    if path.suffix.lower() == '.ipynb':
        mime = 'application/x-ipynb+json'
    sid=store.install_file(rid,staging,sha,before.st_size,mime,url)
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO retrieval_metadata VALUES(?,?)',(sid,path.name))
    store.attempt(rid,"local_import",sid=sid)
    return sid,extract(store,sid)


def render_page(store, sid, page, output):
    import pymupdf
    row = store.rows("SELECT sha FROM snapshots WHERE id=?",(sid,))[0]
    with pymupdf.open(store.object_path(row["sha"])) as doc:
        doc[page-1].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).save(output)
