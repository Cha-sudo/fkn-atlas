"""Reconcile the private SQLite ledger with saved Wiki HTML and authored lessons."""
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict, deque
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .store import canonical, digest
from .source_inventory import material_key, pmi_plans, site_index, video_id


ROOT_PATHS = {
    'fruitdet.ru': {'/wiki/Wiki_ФКН', '/wiki/Wiki_ФКН/Архив'},
    'wikics.site': {'/Wiki_ФКН', '/Wiki_ФКН/Архив'},
    'wiki.cs.hse.ru': {'/', '/wiki/Wiki_ФКН', '/Wiki_ФКН'},
}


def is_root(url):
    parts = urlsplit(url)
    return not parts.query and unquote(parts.path) in ROOT_PATHS.get(parts.hostname, ())


def lesson_coverage(home, resources, blob_shas):
    learning = files('atlas').joinpath('learning')
    archive = json.loads(learning.joinpath('seminars.json').read_text('utf8'))
    placements = defaultdict(list)
    for year, source in archive['years'].items():
        for group, records in source['groups'].items():
            for label, vid in records:
                placements[vid].append({'year': year, 'group': group, 'label': label, 'course': source['source']})
    listed = set(placements)
    papers = [sha for group in archive['group213'].values() for _, sha in group]
    papers += [sha for group in archive['handouts'].values() for _, _, sha in group['files']]
    videos, docs, lessons = set(), set(), {}
    linked = defaultdict(lambda: {'links': set(), 'players': set(), 'notes': set(), 'map': set()})
    for item in sorted(learning.iterdir(), key=lambda f: f.name):
        if not item.name.endswith('.html'):
            continue
        html = item.read_text('utf8')
        soup = BeautifulSoup(html, 'html.parser')
        if item.name == 'years.html':
            for anchor in soup.select('main a[href^="https://"]'):
                linked[material_key(anchor['href'])]['map'].add(item.name)
        if not soup.h1 or not re.match(r'Урок\s+\d+\.', soup.h1.get_text(strip=True)):
            continue
        selected = {video_id(a['href']) for a in soup.select('a.seminar-watch[href]')} - {None}
        for anchor in soup.select('main a[href]'):
            href = anchor['href']
            if not href.startswith(('http://', 'https://')) or anchor.find_parent('nav'):
                continue
            lookup = linked[material_key(href)]
            if anchor.find_parent('footer'):
                lookup['notes'].add(item.name)
                continue
            lookup['links'].add(item.name)
            if video_id(href) and set(anchor.get('class', ())) & {'watch-recording','clip-start','seminar-watch'}:
                lookup['players'].add(item.name)
        originals = set(re.findall(r'<!--\s*(?:document|excerpt)\s+(?:board\s+)?([0-9a-f]{64})\b', html))
        lessons[item.name] = {'seminar_ids': sorted(selected), 'original_shas': sorted(originals)}
        videos.update(selected)
        docs.update(originals)
    ids = {video_id(row['url']) for row in resources.values()} - {None}
    receipt = home / 'derived/seminars-pmi-20261122/published-videos.json'
    entries = {}
    if receipt.is_file():
        entries = json.loads(receipt.read_text('utf8'))
    crosswalk = {vid: {'video_id': vid, 'course_occurrences': placements[vid],
                       'lessons': [name for name, row in lessons.items() if vid in row['seminar_ids']],
                       'oembed_status': entries.get(vid, {}).get('status'),
                       'oembed_sha': entries.get(vid, {}).get('sha')} for vid in sorted(listed)}
    confirmed = sum(row['oembed_status'] == 200 for row in crosswalk.values()) if receipt.is_file() else None
    return {'lessons': lessons, 'archive_video_ids': len(listed), 'in_sqlite_watch_urls': len(listed & ids),
            'with_private_oembed_200': confirmed, 'archive_videos_in_lessons': len(listed & videos),
            'unlisted_videos_in_lesson_seminar_players': len(videos - listed),
            'archive_document_entries': len(papers), 'archive_document_unique_shas': len(set(papers)),
            'archive_document_shas_in_blobs': len(set(papers) & blob_shas),
            'archive_document_shas_shown_in_lessons': len(set(papers) & docs),
            'lesson_original_shas': len(docs), 'lesson_original_shas_in_blobs': len(docs & blob_shas)}, crosswalk, linked


def wiki_anchors(home, db, resources, states):
    result = []
    for sid, rid, sha, base in db.execute("""SELECT s.id,s.resource_id,s.sha,s.final_url FROM snapshots s
            JOIN resources r ON r.id=s.resource_id WHERE r.provider='mediawiki' AND s.mime LIKE '%html%'
            ORDER BY r.url,s.created"""):
        url = resources[rid]['url']
        entry = {'snapshot': sid, 'sha': sha, 'resource_id': rid, 'root': is_root(url)}
        if entry['root']:
            entry['source'] = unquote(url)
        result.append(entry)
        path = home / 'objects' / sha[:2] / sha
        if not path.is_file():
            entry['issue'] = 'object_missing'
            continue
        raw = path.read_bytes()
        if digest(raw) != sha:
            entry['issue'] = 'digest_mismatch'
            continue
        html = raw.decode('utf8', 'replace')
        soup = BeautifulSoup(html, 'html.parser')
        page_id = re.search(r'"wgArticleId"\s*:\s*(\d+)', html)
        entry['host'] = urlsplit(base).hostname
        entry['page_id'] = int(page_id[1]) if page_id else None
        body = soup.select_one('.mw-parser-output') or soup.select_one('main') or soup.body or soup
        original = Counter()
        invalid = 0
        for a in body.select('a[href]'):
            href = a['href']
            if not href or href.startswith('#') or a.find_parent(['script', 'style', 'noscript']):
                continue
            try:
                absolute = urljoin(base, href)
                if urlsplit(absolute).scheme in {'http', 'https'}:
                    original[(href, canonical(absolute))] += 1
            except ValueError:
                invalid += 1
        stored = Counter()
        targets = set()
        for href, target in db.execute("""SELECT href,target_id FROM occurrences
                WHERE snapshot_id=? AND path LIKE '/content/%'""", (sid,)):
            targets.add(target)
            stored[(href, resources[target]['url'])] += 1
        entry.update(html_anchors=original.total(), stored_anchors=stored.total(), invalid_http_hrefs=invalid,
                     missing=(original-stored).total(), extra=(stored-original).total(),
                     direct_targets=len(targets), target_states=dict(Counter(states[t] for t in targets)),
                     embedded_media_elements=len(body.select('iframe[src],video[src],audio[src],source[src],object[data],embed[src]')))
    return result


def write_ledger(home, resources, states, occurrences, jobs, outcomes, reasons, reachable, crosswalk):
    directory = home / 'derived'
    directory.mkdir(exist_ok=True)
    path = directory / 'coverage-urls.jsonl'
    temporary = None
    observed_videos = set()
    try:
        with NamedTemporaryFile(mode='w', dir=directory, encoding='utf8', delete=False) as out:
            temporary = Path(out.name)
            for rid, row in resources.items():
                data = {**dict(row), 'state': states[rid], 'occurrences': occurrences[rid],
                        'reachable': rid in reachable, 'job': jobs.get(rid),
                        'attempt_states': outcomes.get(rid, {}), 'crawl_reasons': sorted(reasons.get(rid, ()))}
                vid = video_id(row['url'])
                if vid in crosswalk:
                    data['seminar'] = crosswalk[vid]
                    observed_videos.add(vid)
                out.write(json.dumps(data, ensure_ascii=False) + '\n')
            for vid, details in crosswalk.items():
                if vid not in observed_videos:
                    data = {'url': 'https://www.youtube.com/watch?v=' + vid, 'state': 'seminar_catalog_only',
                            'id': None, 'seminar': details}
                    out.write(json.dumps(data, ensure_ascii=False) + '\n')
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return str(path)


def coverage(home=None, ledger=False, index_dir=None):
    home = Path(home or os.environ.get('ATLAS_HOME', Path.home()/'.fkn-atlas')).resolve()
    with closing(sqlite3.connect((home/'atlas.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        resources = {r['id']: r for r in db.execute('SELECT id,url,provider FROM resources ORDER BY id')}
        snapshots = dict(db.execute('SELECT resource_id,count(*) FROM snapshots GROUP BY resource_id'))
        jobs = dict(db.execute('SELECT resource_id,state FROM jobs'))
        outcomes = defaultdict(dict)
        for rid, status, count in db.execute('SELECT resource_id,status,count(*) FROM attempts GROUP BY 1,2'):
            outcomes[rid][status] = count
        partial = {r[0] for r in db.execute('SELECT resource_id FROM transfers WHERE received>0')}
        states = {}
        for rid in resources:
            if rid in snapshots:
                states[rid] = 'snapshot'
            elif rid in partial:
                states[rid] = 'partial_bytes_only'
            elif rid in outcomes:
                states[rid] = 'attempt_without_snapshot'
            else:
                states[rid] = 'no_attempt_or_bytes_in_db'
        graph = defaultdict(set)
        occurrences = Counter()
        for parent, target, count in db.execute("""SELECT s.resource_id,o.target_id,count(*) FROM occurrences o
                JOIN snapshots s ON s.id=o.snapshot_id GROUP BY 1,2"""):
            graph[parent].add(target)
            occurrences[target] += count
        for parent, target in db.execute('SELECT source_id,target_id FROM source_edges'):
            graph[parent].add(target)
        reachable = {rid for rid, row in resources.items() if is_root(row['url'])}
        queue = deque(reachable)
        while queue:
            new = graph[queue.popleft()] - reachable
            reachable.update(new)
            queue.extend(new)
        reasons = defaultdict(set)
        for target, state, reason in db.execute('SELECT DISTINCT target_id,state,reason FROM crawl_edges'):
            reasons[target].add(f'{state}/{reason}')
        wiki = wiki_anchors(home, db, resources, states)
        shas = {r[0] for r in db.execute('SELECT sha FROM blobs')}
        assessed = db.execute("""SELECT count(DISTINCT e.snapshot_id) FROM fragments f, json_each(f.anchors) a
                JOIN chunks c ON c.id=json_extract(a.value,'$.chunk_id')
                JOIN extractions e ON e.id=c.extraction_id
                WHERE EXISTS(SELECT 1 FROM assessments s WHERE s.fragment_id=f.id)""").fetchone()[0]
        counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in
                  ('resources','occurrences','snapshots','blobs','versions','lessons','fragments','assessments','jobs')}
        def counts_for(ids):
            return dict(Counter(states[t] for t in ids))
        reader, crosswalk, linked = lesson_coverage(home, resources, shas)
        plans, plan_entries = pmi_plans(db, resources, reader['lessons'], linked)
        result = {'counts': counts, 'url_states': counts_for(resources),
                  'occurrence_targets': counts_for(occurrences), 'root_reachable': counts_for(reachable),
                  'providers': {p: counts_for(t for t, r in resources.items() if r['provider']==p)
                                for p in sorted({r['provider'] for r in resources.values()})},
                  'queue': dict(Counter(jobs.values())),
                  'without_db_trace_by_queue': dict(Counter(jobs.get(t,'no_job') for t in resources
                                                           if states[t]=='no_attempt_or_bytes_in_db')),
                  'crawl_reasons_among_untraced_targets_nonexclusive': dict(Counter(
                      reason for t in resources if states[t]=='no_attempt_or_bytes_in_db' for reason in reasons[t])),
                  'occurrences_without_crawl_decision': db.execute("""SELECT count(*) FROM occurrences
                      WHERE id NOT IN (SELECT occurrence_id FROM crawl_edges)""").fetchone()[0],
                  'latest_extraction_per_snapshot': dict(db.execute("""SELECT status,count(*) FROM
                      (SELECT status,row_number() OVER (PARTITION BY snapshot_id ORDER BY created DESC,id DESC) n
                       FROM extractions) WHERE n=1 GROUP BY status""")),
                  'snapshots_with_assessed_anchors': assessed,
                  'plan_statuses': dict(db.execute('SELECT status,count(*) FROM lessons GROUP BY status')),
                  'wiki_html': wiki, 'course_reader': reader, 'pmi_lecture_plans': plans}
        additional = {'coverage-pmi-lectures.jsonl': plan_entries}
        if index_dir:
            result['site_indexes'], additional['coverage-wiki-pages.jsonl'] = site_index(index_dir, db, wiki)
        if ledger:
            result['private_ledger'] = write_ledger(home, resources, states, occurrences, jobs, outcomes,
                                                    reasons, reachable, crosswalk)
            for name, entries in additional.items():
                target = home/'derived'/name
                with NamedTemporaryFile('w', encoding='utf8', dir=target.parent, delete=False) as out:
                    temp = Path(out.name)
                try:
                    temp.write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in entries), encoding='utf8')
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
        return result
