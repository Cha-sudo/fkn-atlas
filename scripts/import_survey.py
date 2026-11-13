"""Import the bounded source survey without fabricating Wiki link provenance.

No network access. Evidence hashes are checked before import; reruns are idempotent.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='runtime/pilot.sqlite')
    parser.add_argument('--survey', default='runtime/research/survey/results.json')
    args = parser.parse_args()
    source = Path(args.survey)
    records = json.loads(source.read_text(encoding='utf-8'))
    payload = []
    for record in records:
        if not record.get('observed_at'):
            continue  # A skipped request is not an observed remote response.
        body = None
        if record.get('body_file'):
            body_path = (source.parent / record['body_file']).resolve()
            if source.parent.resolve() not in body_path.parents:
                raise ValueError('Body path escapes survey directory')
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != record['sha256'] or len(body) != record['bytes']:
                raise ValueError('Survey body hash or size mismatch')
        details = {**record, 'origin': 'supplied_handoff_example_or_diagnostic', 'wiki_occurrence': False}
        state = 'http_response' if record.get('http_status') is not None else 'network_error'
        if record['name'] == 'yandex_metadata' and record.get('http_status') == 200:
            meta = json.loads(body)
            embedded = meta.get('_embedded', {})
            details.update({
                'origin': 'supplied_handoff_example',
                'resource_url': 'https://disk.yandex.ru/d/lwE14jdmI4qZEQ',
                'content_state': 'metadata_only', 'verification': 'unreviewed',
                'folder_name': meta.get('name'), 'reported_total': embedded.get('total'),
                'offset': embedded.get('offset'), 'limit': embedded.get('limit'),
                'children': [{k: item.get(k) for k in ('name', 'path', 'type')} for item in embedded.get('items', [])],
            })
            state = 'metadata_only'
        payload.append((record, body, details, state))
    store = Store(args.db)
    imported = 0
    try:
        store.bind_mode('scout')
        with store.transaction():
            for record, body, details, state in payload:
                key = 'survey:' + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
                if store.setting(key):
                    continue
                store.event(record['url'], 'source_survey', state, record.get('http_status'),
                    record.get('error'), body, details=details, observed_at=record['observed_at'])
                store.set_setting(key, 'imported')
                imported += 1
        print(json.dumps({'imported_observations': imported, **store.stats()}, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
