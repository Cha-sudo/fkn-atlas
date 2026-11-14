"""Synthetic evidence tests: no live network or inferred course equivalence."""
import copy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.reconcile import Reconciler
from fkn_atlas.resources import Catalog
from fkn_atlas.storage import Store, utc_now


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / 'test.sqlite'))
        self.engine = Reconciler(self.store)
        self.course_text = 'ПМИ основной поток 2023/2024 и 2024/2025. Лекция: линейные пространства.'
        self.course_sha = self.store.save_blob(self.course_text.encode())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def span(sha, text):
        return {'sha256': sha, 'start': 0, 'end': len(text), 'quote': text}

    def material(self, ident, year, text, title='Lecture 1', reviewed=True):
        rid = Catalog(self.store).upsert('fixture', 'https://fixture.local/'+ident,
            {'path': '/lecture.txt', 'name': 'lecture.txt', 'type': 'file'})
        sha = self.store.save_blob(text.encode())
        self.store.db.execute('INSERT OR IGNORE INTO resource_contents VALUES (?,?,?,?,?)',
                             (rid, sha, 'text/plain', None, utc_now()))
        self.store.db.execute('INSERT OR IGNORE INTO derived_artifacts VALUES (?,?,?,?,?,?,?)',
                             (rid, sha, sha, 'fixture-text', 'text_extracted', '{}', utc_now()))
        self.store.db.commit()
        result = {'id': ident, 'version_id': year, 'resource_id': rid, 'input_sha256': sha,
                  'text_sha256': sha, 'title': title, 'kind': 'lecture',
                  'source_url': 'https://fixture.local/'+ident, 'topics': []}
        if reviewed:
            result['review'] = {'reviewer': 'fixture-reviewer', 'summary': 'Read the complete synthetic document.',
                                'evidence': [self.span(sha, text)]}
        return result

    def dossier(self):
        return {'id': 'pilot', 'course': 'linear-algebra', 'versions': [
            {'id': year, 'title': 'Линейная алгебра', 'academic_year': year,
             'program': 'ПМИ', 'stream': 'основной', 'evidence': [self.span(self.course_sha, self.course_text)]}
            for year in ('2023/2024', '2024/2025')], 'materials': [
                self.material('old', '2023/2024', 'Векторное пространство. Базис определяет размерность.'),
                self.material('new', '2024/2025', 'Векторное пространство. Базис определяет размерность.')],
            'gaps': [], 'comparisons': []}

    def decision(self, dossier, status='verified'):
        left, right = dossier['materials'][:2]
        return {'id': 'comparison', 'left_id': left['id'], 'right_id': right['id'], 'status': status,
                'scope': ['basis'], 'reason': 'Same synthetic definition, reviewed in both originals.',
                'limitations': 'Only the stated definition; no historical lecture reconstruction.',
                'reviewer': 'fixture-reviewer',
                'left_evidence': copy.deepcopy(left['review']['evidence']),
                'right_evidence': copy.deepcopy(right['review']['evidence'])}

    def test_repeat_import_preserves_two_origins_for_identical_bytes(self):
        dossier = self.dossier()
        dossier['comparisons'].append(self.decision(dossier))
        first = self.engine.import_dossier(dossier)
        second = self.engine.import_dossier(dossier)
        self.assertEqual(first, second)
        stats = first['statistics']
        self.assertEqual(stats['found_resources'], 2)
        self.assertEqual(stats['distinct_original_files'], 1)
        self.assertEqual(stats['content_reviewed_resources'], 2)
        self.assertEqual(stats['confirmed_content_pairs'], 1)
        self.assertEqual(stats['historical_missing_originals_recovered'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM recon_dossiers').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_same_title_does_not_rank_as_content_match(self):
        dossier = self.dossier()
        unrelated = self.material('wrong-title-match', '2024/2025', 'Зебры пасутся среди саванны.')
        dossier['materials'].append(unrelated)
        dossier['materials'][1]['title'] = 'Completely different lecture number'
        self.engine.import_dossier(dossier)
        candidates = self.engine.rank_candidates('old')
        self.assertEqual(candidates[0]['material_id'], 'new')
        self.assertEqual(candidates[0]['score'], 1)
        self.assertTrue(candidates[0]['same_original_bytes'])
        self.assertEqual(candidates[1]['score'], 0)
        self.assertTrue(all(c['state'] == 'candidate' for c in candidates))
        self.assertEqual(self.engine.report()['statistics']['confirmed_content_pairs'], 0)

    def test_latest_dossier_does_not_double_count_a_revised_judgment(self):
        original = self.dossier()
        original['comparisons'].append(self.decision(original))
        self.engine.import_dossier(original)
        revision = copy.deepcopy(original)
        revision['id'] = 'pilot-v2'
        revision['comparisons'][0]['id'] = 'comparison-v2'
        revision['comparisons'][0]['status'] = 'partial'
        self.engine.import_dossier(revision)
        latest = self.engine.report()['statistics']
        self.assertEqual(latest['confirmed_content_pairs'], 0)
        self.assertEqual(latest['partial_content_pairs'], 1)
        self.assertEqual(self.engine.report('pilot')['statistics']['confirmed_content_pairs'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM recon_comparisons').fetchone()[0], 2)

    def test_invalid_unicode_evidence_rolls_back_entire_import(self):
        dossier = self.dossier()
        evidence = dossier['materials'][1]['review']['evidence'][0]
        evidence['end'] = len(evidence['quote'].encode('utf-8'))  # byte count is not character count
        with self.assertRaisesRegex(ValueError, 'offsets'):
            self.engine.import_dossier(dossier)
        self.assertEqual(self.engine.report()['statistics']['material_assignments'], 0)

    def test_evidence_cannot_cite_another_material(self):
        dossier = self.dossier()
        dossier['materials'][1]['review']['evidence'] = [self.span(self.course_sha, self.course_text)]
        with self.assertRaisesRegex(ValueError, 'this material'):
            self.engine.import_dossier(dossier)

    def test_forged_extraction_link_is_rejected(self):
        dossier = self.dossier()
        dossier['materials'][1]['text_sha256'] = self.course_sha
        with self.assertRaisesRegex(ValueError, 'extracted artifact'):
            self.engine.import_dossier(dossier)

    def test_changed_blob_bytes_fail_integrity(self):
        dossier = self.dossier()
        sha = dossier['materials'][0]['input_sha256']
        row = self.store.db.execute('SELECT path FROM blobs WHERE sha256=?', (sha,)).fetchone()
        (self.store.path.parent / row[0]).write_bytes(b'corrupted evidence')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.engine.import_dossier(dossier)

    def test_metadata_only_material_cannot_be_verified(self):
        dossier = self.dossier()
        decision = self.decision(dossier)
        dossier['comparisons'].append(decision)
        missing = dossier['materials'][1]
        missing.pop('input_sha256')
        missing.pop('text_sha256')
        missing.pop('review')
        with self.assertRaisesRegex(ValueError, 'content reviews'):
            self.engine.import_dossier(dossier)
        decision.update(status='insufficient_evidence', right_evidence=[])
        report = self.engine.import_dossier(dossier)
        self.assertEqual(report['statistics']['retrieved_resources'], 1)
        self.assertEqual(report['statistics']['comparisons_by_status'], {'insufficient_evidence': 1})
        self.assertEqual(self.engine.rank_candidates('new'), [])

    def test_revised_assertion_requires_new_id_without_overwrite(self):
        dossier = self.dossier()
        self.engine.import_dossier(dossier)
        dossier['materials'][1]['title'] = 'Changed assertion'
        with self.assertRaisesRegex(ValueError, 'immutable content'):
            self.engine.import_dossier(dossier)
        self.assertEqual(self.engine.report('pilot')['materials'][1]['title'], 'Lecture 1')

    def test_gap_closure_requires_expected_topics_and_verified_scope(self):
        dossier = self.dossier()
        gap = {'id': 'gap-6', 'version_id': '2023/2024', 'title': 'Missing slides for planned basis topic',
               'reason': 'link_missing', 'status': 'coverage_supported', 'expected_topics': [],
               'evidence': [self.span(self.course_sha, self.course_text)], 'candidate_material_ids': ['new'],
               'limitations': 'Historical slides unavailable; topical support is all that is claimed.'}
        dossier['gaps'].append(gap)
        dossier['comparisons'].append({**self.decision(dossier), 'gap_id': gap['id']})
        with self.assertRaisesRegex(ValueError, 'explicit expected topics'):
            self.engine.import_dossier(dossier)
        gap['expected_topics'] = ['basis', 'determinant']
        with self.assertRaisesRegex(ValueError, 'explicit expected topics'):
            self.engine.import_dossier(dossier)
        gap['expected_topics'] = ['basis']
        stats = self.engine.import_dossier(dossier)['statistics']
        self.assertEqual(stats['coverage_supported_gaps'], 1)
        self.assertEqual(stats['unresolved_gaps'], 0)
        self.assertEqual(stats['historical_missing_originals_recovered'], 0)

    def test_unchecked_and_partial_gaps_remain_unresolved(self):
        dossier = self.dossier()
        dossier['gaps'].append({'id': 'gap', 'version_id': '2023/2024', 'title': 'Seminar coverage not checked',
            'reason': 'not_checked', 'status': 'unresolved', 'expected_topics': [],
            'evidence': [self.span(self.course_sha, self.course_text)], 'candidate_material_ids': [],
            'limitations': 'No claim that a seminar never existed.'})
        stats = self.engine.import_dossier(dossier)['statistics']
        self.assertEqual(stats['unresolved_gaps'], 1)
        self.assertEqual(stats['confirmed_content_pairs'], 0)

    def test_invalid_status_is_not_silently_promoted(self):
        dossier = self.dossier()
        dossier['comparisons'].append(self.decision(dossier, 'probably_verified'))
        with self.assertRaisesRegex(ValueError, 'Unknown comparison status'):
            self.engine.import_dossier(dossier)


if __name__ == '__main__':
    unittest.main()
