"""Apply an explicitly reviewed character map as a NEW derived text artifact.

Original bytes and extracted text remain unchanged. Mapping quality is a reviewer
decision: this utility checks provenance, not the linguistic correctness of a map.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.storage import Store, utc_now
from fkn_atlas.reconcile import Reconciler


def derive(store, resource_id, input_sha, text_sha, mapping, reviewer):
    if not reviewer.strip() or not isinstance(mapping, dict) or not mapping:
        raise ValueError('Explicit mapping and reviewer required')
    if any(not isinstance(k, str) or len(k) != 1 or not isinstance(v, str) or len(v) != 1
           for k, v in mapping.items()):
        raise ValueError('Only single-character substitutions are supported')
    if not store.db.execute('''SELECT 1 FROM derived_artifacts WHERE resource_id=?
        AND input_sha256=? AND artifact_sha256=? AND state='text_extracted' ''',
        (resource_id, input_sha, text_sha)).fetchone():
        raise ValueError('Text does not belong to this original/resource')
    checker = Reconciler(store)
    original = checker._text(text_sha)
    result = original.translate(str.maketrans(mapping))
    mapping_sha = hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()
    extractor = 'reviewed-character-map/1:' + mapping_sha
    metadata = {'parent_text_sha256': text_sha, 'mapping': mapping, 'reviewer': reviewer,
                'replacement_count': sum(a != b for a, b in zip(original, result)),
                'formula_fidelity_verified': False,
                'coverage': 'Character substitution only; no OCR, formula repair or invented text'}
    with store.transaction():
        sha = store.save_blob(result.encode('utf8'))
        store.db.execute('INSERT OR IGNORE INTO derived_artifacts VALUES (?,?,?,?,?,?,?)',
            (resource_id, input_sha, sha, extractor, 'text_extracted',
             json.dumps(metadata, ensure_ascii=False), utc_now()))
    return sha


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('db', 'resource-id', 'input-sha', 'text-sha', 'mapping', 'reviewer'):
        p.add_argument('--' + flag, required=True)
    a = p.parse_args()
    if not Path(a.db).is_file():
        p.error('Database does not exist')
    store = Store(a.db)
    try:
        print(derive(store, a.resource_id, a.input_sha, a.text_sha,
                     json.loads(Path(a.mapping).read_text(encoding='utf8')), a.reviewer))
    finally:
        store.close()
