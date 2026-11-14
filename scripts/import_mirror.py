"""Import verified saved mirror responses, keeping mirror and retrieval provenance."""
import argparse,hashlib,json,re,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fkn_atlas.storage import Store
from fkn_atlas.crawler import process_html
from fkn_atlas.urls import normalize_url


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default='runtime/pilot.sqlite')
    p.add_argument('--evidence',default='runtime/research/followup_wiki')
    args=p.parse_args();root=Path(args.evidence)
    store=Store(args.db);count=0
    try:
        store.bind_mode('scout')
        for name in ('wikics_site_main','wikics_site_archive','wikics_site_programming_2021'):
            meta=json.loads((root/(name+'.json')).read_text())
            body_path=(root/meta['body_file']).resolve()
            if root.resolve() not in body_path.parents: raise ValueError('Evidence path escapes directory')
            raw=body_path.read_bytes()
            if meta.get('http_status')!=200 or meta.get('truncated') or hashlib.sha256(raw).hexdigest()!=meta['sha256']: raise ValueError('Invalid evidence response')
            url=normalize_url(meta['url'])
            key='mirror-import:'+url+':'+meta['sha256']
            if store.setting(key): continue
            html=raw.decode('utf-8')
            dates=re.findall(r'2026-11-12[^<"\s]{0,30}',html)
            with store.transaction():
                process_html(store,url,html,0,'wikics.site',0,raw=raw,origin='mirror_stale',stage='mirror_import',observed_at=meta['observed_at'],
                    evidence_details={'source_kind':'independent_mirror','cache_status':'stale','cache_dates_seen':sorted(set(dates)),
                        'discovery_parent':meta.get('source_url'),'official_status_verified':False})
                store.set_setting(key,'imported')
            count+=1
        print(json.dumps({'imported_mirror_pages':count,**store.stats()},ensure_ascii=False,indent=2))
    finally: store.close()


if __name__=='__main__':main()
