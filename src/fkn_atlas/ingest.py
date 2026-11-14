"""Import locally obtained public documents without losing their discovery trail."""
from pathlib import Path
import hashlib
import json
from urllib.parse import urlsplit
from .resources import Catalog, extract_resource
from .storage import utc_now
from .urls import normalize_url


def import_local_file(store, filename, source_url, discovered_on, *,
                      expected_sha256=None, expected_size=None, acquisition='local_import'):
    """Use for authorized browser/connector downloads; never infer access from a title."""
    if acquisition not in ('local_import', 'google_drive_connector'):
        raise ValueError('Unknown acquisition method')
    source_url = normalize_url(source_url)
    discovered_on = normalize_url(discovered_on)
    if source_url is None or discovered_on is None:
        raise ValueError('Expected stable source/discovery URLs without credentials or signed parameters')
    parts = urlsplit(source_url)
    if parts.scheme != 'https':
        raise ValueError('Expected a stable HTTPS source URL')
    if acquisition == 'google_drive_connector' and parts.hostname not in ('drive.google.com', 'docs.google.com'):
        raise ValueError('Google Drive acquisition requires a Google Drive source')
    occurrences = store.db.execute('''SELECT id,snapshot_id FROM link_occurrences
        WHERE source_url=? AND target_url=? AND snapshot_id IS NOT NULL''',
        (discovered_on, source_url)).fetchall()
    if not occurrences:
        raise ValueError('Source must occur in a saved discovery page')
    path = Path(filename)
    with path.open('rb') as stream:
        body = stream.read(32 * 1024 * 1024 + 1)
    if len(body) > 32 * 1024 * 1024:
        raise ValueError('Local import exceeds 32 MiB')
    digest = hashlib.sha256(body).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('Local input SHA256 mismatch')
    if expected_size is not None and len(body) != expected_size:
        raise ValueError('Local input size mismatch')
    evidence = {'source_url': source_url, 'discovered_on': discovered_on,
                'occurrence_ids': [x[0] for x in occurrences],
                'snapshot_ids': sorted({x[1] for x in occurrences}),
                'acquisition': acquisition, 'bytes': len(body), 'sha256': digest,
                'provider_access': 'obtained; sharing permission not inferred'}
    provider = 'google_drive' if parts.hostname in ('drive.google.com', 'docs.google.com') else 'local_document'
    catalog = Catalog(store)
    with store.transaction():
        meta_sha = store.save_blob(json.dumps(evidence, ensure_ascii=False).encode())
        rid = catalog.upsert(provider, source_url,
            {'path': '/', 'name': path.name, 'type': 'file', 'size': len(body), 'sha256': digest},
            meta_sha, source_url)
        sha = store.save_blob(body)
        store.db.execute('INSERT OR IGNORE INTO resource_contents VALUES (?,?,?,?,?)',
            (rid, sha, 'application/octet-stream', meta_sha, utc_now()))
        store.db.execute("""UPDATE resources SET content_state=CASE WHEN content_state='metadata_only'
            THEN 'retrieved' ELSE content_state END,access='unknown' WHERE id=?""", (rid,))
        if not store.setting('local-import:' + rid + ':' + sha):
            store.event(source_url, 'local_import', 'retrieved', details=evidence)
            store.set_setting('local-import:' + rid + ':' + sha, 'complete')
    extract_resource(store, rid, input_sha256=sha)
    return {'resource_id': rid, 'sha256': sha, 'bytes': len(body),
            'content_state': catalog.get(rid)['content_state']}
