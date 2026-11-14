"""Evidence-backed course comparison, layered on the existing SQLite/blob Store.

Offsets are Unicode character offsets in a saved UTF-8 text blob, not PDF bytes.
The importer checks provenance and evidence integrity; semantic judgments remain
explicit reviewer decisions. Lexical ranking produces candidates, never verdicts.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import re

from .storage import utc_now


STATUSES = frozenset(('verified', 'partial', 'additional', 'rejected', 'insufficient_evidence'))
GAP_REASONS = frozenset(('unlisted', 'link_missing', 'broken_link', 'access_blocked',
                         'not_transcribed', 'content_incomplete', 'not_checked'))
RECON_TABLES = ('recon_versions', 'recon_materials', 'recon_gaps', 'recon_comparisons', 'recon_dossiers')
SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_versions (
 id TEXT PRIMARY KEY, course TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_materials (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES recon_versions(id),
 resource_id TEXT NOT NULL REFERENCES resources(id), input_sha256 TEXT REFERENCES blobs(sha256),
 text_sha256 TEXT REFERENCES blobs(sha256), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_gaps (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES recon_versions(id), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_comparisons (
 id TEXT PRIMARY KEY, left_id TEXT NOT NULL REFERENCES recon_materials(id),
 right_id TEXT NOT NULL REFERENCES recon_materials(id),
 gap_id TEXT REFERENCES recon_gaps(id), status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_dossiers (
 id TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES blobs(sha256), imported_at TEXT NOT NULL
);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _required(record, *keys):
    if not isinstance(record, dict):
        raise ValueError('Expected a JSON object')
    for key in keys:
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError('Required nonempty string: ' + key)


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(field + ' must be a list of nonempty strings')


class Reconciler:
    def __init__(self, store):
        self.store = store
        # Additive extension; existing snapshots/resources are neither migrated nor replaced.
        self.store.db.executescript(SCHEMA)

    def _blob(self, sha):
        row = self.store.db.execute('SELECT path,bytes FROM blobs WHERE sha256=?', (sha,)).fetchone()
        if not row:
            raise ValueError('Evidence blob is not registered: ' + str(sha))
        path = (self.store.path.parent / row[0]).resolve()
        if not path.is_relative_to(self.store.blob_dir.resolve()):
            raise ValueError('Evidence path escapes blob store')
        raw = path.read_bytes()
        if len(raw) != row[1] or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Evidence blob failed integrity check')
        return raw

    def _text(self, sha):
        try:
            return self._blob(sha).decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('Evidence must be saved UTF-8 text') from exc

    def _evidence(self, spans, allowed_sha=None, required=True):
        if not isinstance(spans, list) or (required and not spans):
            raise ValueError('Nonempty evidence list required')
        for span in spans:
            _required(span, 'sha256', 'quote')
            if allowed_sha is not None and span['sha256'] != allowed_sha:
                raise ValueError('Evidence must refer to this material text')
            text = self._text(span['sha256'])
            start, end = span.get('start'), span.get('end')
            if (type(start) is not int or type(end) is not int or
                    not 0 <= start < end <= len(text) or text[start:end] != span['quote']):
                raise ValueError('Evidence quote/character offsets do not match saved text')

    def _material(self, item, versions):
        _required(item, 'id', 'version_id', 'resource_id', 'title', 'kind', 'source_url')
        _strings(item.get('topics', []), 'topics')
        if item['version_id'] not in versions:
            raise ValueError('Unknown course version')
        if not self.store.db.execute('SELECT 1 FROM resources WHERE id=?', (item['resource_id'],)).fetchone():
            raise ValueError('Material resource is not cataloged')
        original, text = item.get('input_sha256'), item.get('text_sha256')
        if original:
            if not self.store.db.execute('SELECT 1 FROM resource_contents WHERE resource_id=? AND sha256=?',
                                         (item['resource_id'], original)).fetchone():
                raise ValueError('Original is not a retrieved version of this resource')
            self._blob(original)
        if text:
            if not original or not self.store.db.execute("""SELECT 1 FROM derived_artifacts
                WHERE resource_id=? AND input_sha256=? AND artifact_sha256=? AND state='text_extracted'""",
                (item['resource_id'], original, text)).fetchone():
                raise ValueError('Text is not an extracted artifact of this original')
            self._text(text)
        review = item.get('review')
        if review:
            _required(review, 'reviewer', 'summary')
            if not text:
                raise ValueError('Content review requires extracted content')
            self._evidence(review.get('evidence'), text)

    @staticmethod
    def _index(items):
        if not isinstance(items, list):
            raise ValueError('Dossier sections must be lists')
        result = {}
        for item in items:
            _required(item, 'id')
            if item['id'] in result:
                raise ValueError('Duplicate dossier object ID')
            result[item['id']] = item
        return result

    def import_dossier(self, dossier):
        """Validate then atomically import a complete, immutable reviewed dossier.

        Reimporting identical IDs/content is a no-op. Revised assertions need new
        IDs, preserving the previous evidence and decisions instead of overwriting.
        """
        _required(dossier, 'id', 'course')
        versions = self._index(dossier.get('versions', []))
        materials = self._index(dossier.get('materials', []))
        gaps = self._index(dossier.get('gaps', []))
        comparisons = self._index(dossier.get('comparisons', []))
        if not versions:
            raise ValueError('At least one evidenced version is required')
        for version in versions.values():
            _required(version, 'title', 'academic_year', 'program', 'stream')
            self._evidence(version.get('evidence'))
        for item in materials.values():
            self._material(item, versions)
        for gap in gaps.values():
            _required(gap, 'version_id', 'title', 'reason', 'status', 'limitations')
            _strings(gap.get('expected_topics', []), 'expected_topics')
            _strings(gap.get('candidate_material_ids', []), 'candidate_material_ids')
            if gap['version_id'] not in versions or gap['reason'] not in GAP_REASONS:
                raise ValueError('Unknown gap version/reason')
            if gap['status'] not in ('unresolved', 'partial_support', 'coverage_supported'):
                raise ValueError('Unknown gap status')
            self._evidence(gap.get('evidence'))
            if any(cid not in materials for cid in gap.get('candidate_material_ids', [])):
                raise ValueError('Unknown gap candidate')
        for decision in comparisons.values():
            _required(decision, 'left_id', 'right_id', 'status', 'reason', 'reviewer', 'limitations')
            _strings(decision.get('scope', []), 'scope')
            if decision['status'] not in STATUSES:
                raise ValueError('Unknown comparison status')
            left, right = materials.get(decision['left_id']), materials.get(decision['right_id'])
            if left is None or right is None or left['id'] == right['id']:
                raise ValueError('Comparison needs two different cataloged materials')
            if decision.get('gap_id') is not None and decision['gap_id'] not in gaps:
                raise ValueError('Unknown comparison gap')
            if decision.get('gap_id') and left['version_id'] != gaps[decision['gap_id']]['version_id']:
                raise ValueError('Gap comparison must start from the affected course version')
            supported = decision['status'] != 'insufficient_evidence'
            for side, material in (('left', left), ('right', right)):
                if supported and not material.get('review'):
                    raise ValueError('Substantive decisions require both content reviews')
                spans = decision.get(side + '_evidence', [])
                if spans and not material.get('text_sha256'):
                    raise ValueError('Comparison cites material without extracted content')
                self._evidence(spans, material.get('text_sha256'), required=supported)
            if supported and not decision.get('scope'):
                raise ValueError('Decision must identify its reviewed scope')
        for gap in gaps.values():
            relevant = [c for c in comparisons.values() if c.get('gap_id') == gap['id'] and
                        c['right_id'] in gap.get('candidate_material_ids', [])]
            if gap['status'] == 'partial_support' and not any(c['status'] in ('partial', 'verified') for c in relevant):
                raise ValueError('Gap support requires an evidenced comparison')
            if gap['status'] == 'coverage_supported':
                expected = set(gap.get('expected_topics', []))
                covered = {topic for c in relevant if c['status'] == 'verified' for topic in c.get('scope', [])}
                if not expected or not expected <= covered:
                    raise ValueError('Gap coverage requires all explicit expected topics, not a missing number')
        with self.store.transaction():
            for item in versions.values():
                self._insert('recon_versions', item, (item['id'], dossier['course'], _json(item)))
            for item in materials.values():
                self._insert('recon_materials', item, (item['id'], item['version_id'], item['resource_id'],
                    item.get('input_sha256'), item.get('text_sha256'), _json(item)))
            for item in gaps.values():
                self._insert('recon_gaps', item, (item['id'], item['version_id'], _json(item)))
            for item in comparisons.values():
                self._insert('recon_comparisons', item, (item['id'], item['left_id'], item['right_id'],
                    item.get('gap_id'), item['status'], _json(item)))
            sha = self.store.save_blob(_json(dossier).encode('utf-8'))
            row = self.store.db.execute('SELECT sha256 FROM recon_dossiers WHERE id=?', (dossier['id'],)).fetchone()
            if row and row[0] != sha:
                raise ValueError('Dossier ID already has different immutable content; use a revision ID')
            self.store.db.execute('INSERT OR IGNORE INTO recon_dossiers VALUES (?,?,?)', (dossier['id'], sha, utc_now()))
        return self.report(dossier['id'])

    def _insert(self, table, item, values):
        previous = self.store.db.execute('SELECT * FROM ' + table + ' WHERE id=?', (item['id'],)).fetchone()
        if previous and tuple(previous) != tuple(values):
            raise ValueError('Object ID already has different immutable content: ' + item['id'])
        self.store.db.execute('INSERT OR IGNORE INTO ' + table + ' VALUES (' + ','.join('?' for _ in values) + ')', values)

    def rank_candidates(self, material_id, limit=10):
        """TF-IDF cosine over saved content only; no automatic equivalence claim."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Candidate limit must be 1..100')
        rows = self.store.db.execute('''SELECT m.payload,v.course FROM recon_materials m
            JOIN recon_versions v ON v.id=m.version_id ORDER BY m.id''').fetchall()
        indexed = {json.loads(payload)['id']: (json.loads(payload), course) for payload, course in rows}
        if material_id not in indexed:
            raise ValueError('Unknown material ID')
        query, course = indexed[material_id]
        if not query.get('text_sha256'):
            return []
        docs = [m for m, c in indexed.values() if c == course and m.get('text_sha256')]
        texts = {m['id']: self._text(m['text_sha256']) for m in docs}
        tokens = {mid: Counter(re.findall(r'[^\W_]{2,}', text.casefold())) for mid, text in texts.items()}
        df = Counter(token for bag in tokens.values() for token in bag)
        vectors = {mid: {token: (1 + math.log(count)) * (1 + math.log((1 + len(docs))/(1 + df[token])))
                         for token, count in bag.items()} for mid, bag in tokens.items()}
        q = vectors[material_id]
        qnorm = math.sqrt(sum(x*x for x in q.values()))
        result = []
        for material in docs:
            if material['version_id'] == query['version_id']:
                continue
            vector = vectors[material['id']]
            denominator = qnorm * math.sqrt(sum(x*x for x in vector.values()))
            score = sum(value * vector.get(token, 0) for token, value in q.items()) / denominator if denominator else 0
            result.append({'material_id': material['id'], 'score': round(min(1.0, score), 6), 'state': 'candidate',
                'same_original_bytes': material['input_sha256'] == query['input_sha256'],
                'same_extracted_text': material['text_sha256'] == query['text_sha256'],
                'shared_tokens': sorted(set(q) & set(vector))[:20], 'method': 'content-tfidf-v1'})
        return sorted(result, key=lambda x: (-x['score'], x['material_id']))[:limit]

    def report(self, dossier_id=None):
        # A revision is a complete dossier, not an extra set of current decisions.
        # Default to the latest import instead of double-counting old judgments.
        if dossier_id is None:
            latest = self.store.db.execute(
                'SELECT id FROM recon_dossiers ORDER BY imported_at DESC,rowid DESC LIMIT 1').fetchone()
            if latest:
                dossier_id = latest[0]
        if dossier_id is not None:
            row = self.store.db.execute('SELECT sha256 FROM recon_dossiers WHERE id=?', (dossier_id,)).fetchone()
            if not row:
                raise ValueError('Unknown dossier ID')
            data = json.loads(self._text(row[0]))
        else:
            data = {key: [json.loads(row[0]) for row in self.store.db.execute('SELECT payload FROM '+table+' ORDER BY id')]
                    for key, table in zip(('versions', 'materials', 'gaps', 'comparisons'), RECON_TABLES)}
        materials, gaps, comparisons = (data.get(key, []) for key in ('materials', 'gaps', 'comparisons'))
        decisions = Counter(c['status'] for c in comparisons)
        data['statistics'] = {
            'course_versions': len(data.get('versions', [])),
            'material_assignments': len(materials),
            'found_resources': len({m['resource_id'] for m in materials}),
            'retrieved_resources': len({m['resource_id'] for m in materials if m.get('input_sha256')}),
            'distinct_original_files': len({m['input_sha256'] for m in materials if m.get('input_sha256')}),
            'text_extracted_resources': len({m['resource_id'] for m in materials if m.get('text_sha256')}),
            'content_reviewed_resources': len({m['resource_id'] for m in materials if m.get('review')}),
            'comparisons_by_status': dict(sorted(decisions.items())),
            'confirmed_content_pairs': decisions['verified'],
            'partial_content_pairs': decisions['partial'],
            'gaps_by_status': dict(sorted(Counter(g['status'] for g in gaps).items())),
            'unresolved_gaps': sum(g['status'] != 'coverage_supported' for g in gaps),
            'coverage_supported_gaps': sum(g['status'] == 'coverage_supported' for g in gaps),
            'historical_missing_originals_recovered': 0,
            'basis': 'This reviewed dossier only. Scoped content equivalence and plan coverage do not prove recovery of an unavailable historical original.',
        }
        return data
