"""Import normalized connector evidence; this module does not access an account."""
import json
import re
from urllib.parse import urlsplit
from .store import canonical, uid


def drive_listing(store, source_url, payload):
    """Preserve one bounded folder response without inferring listing completeness.

    The connector exposes a capped list, without a reliable end-of-pagination signal.
    Each returned child is discovery evidence, never a downloaded/studied document.
    """
    p=urlsplit(source_url)
    if p.scheme!='https' or p.hostname!='drive.google.com' or not re.fullmatch(r'/drive/folders/[\w-]+',p.path):
        raise ValueError('Expected an observed Drive folder URL')
    parent=store.rows('SELECT id FROM resources WHERE url=?',(canonical(source_url),))
    if not parent or not store.rows('SELECT id FROM occurrences WHERE target_id=?',(parent[0]['id'],)):
        raise ValueError('Folder must have an observed educational provenance link')
    files=payload.get('files') if isinstance(payload,dict) else None
    if not isinstance(files,list) or len(files)>100:
        raise ValueError('Expected a normalized connector files list of at most 100 children')
    validated=[]
    for item in files:
        ident=item.get('id',''); url=item.get('url',''); q=urlsplit(url)
        folder=item.get('mime_type')=='application/vnd.google-apps.folder'
        expected='/drive/folders/'+ident if folder else '/file/d/'+ident+'/view'
        if (not re.fullmatch(r'[A-Za-z0-9_-]+',ident) or q.scheme!='https'
                or q.hostname!='drive.google.com' or q.path!=expected
                or q.username is not None or q.password is not None or q.port not in {None,443}):
            raise ValueError('Child URL and provider ID must agree; arbitrary links are not accepted')
        validated.append((item,url))
    body=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode('utf8')
    rid=parent[0]['id']; sid=store.snapshot(rid,body,'application/json',source_url)
    with store.db:
        for n,(item,url) in enumerate(validated):
            child=store.resource(url,'connector_observed_child')
            path='/connector/drive/'+str(n); node=uid(sid,path)
            context='Drive folder child observed through existing connector; content and course membership unverified'
            store.db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?,?)',
                (node,sid,None,n,'provider_entry',path,context,'[]'))
            store.db.execute('INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?,?,?)',
                (uid(node,'link'),sid,node,child,url,item.get('title',item['id']),context,'[]',path))
        store.db.execute('INSERT OR REPLACE INTO provider_states VALUES(?,?,?)',
            (sid,'connector_directory_partial',json.dumps({'entries':len(files),'listing_complete':False,
                'reason':'Capped connector response has no verified pagination end; not raw HTTP bytes',
                'materials_obtained':False})))
    store.attempt(rid,'connector_listing',sid=sid,error='Normalized connector response; completeness unknown')
    return {'snapshot_id':sid,'observed_children':len(files),'listing_complete':False}
