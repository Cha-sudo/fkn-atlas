"""Offline checks of MediaWiki's namespace index and the published PMI lecture plans."""
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from .store import digest


def collect_site_index(store, host):
    from .network import Fetcher
    paths = {'fruitdet.ru':'/wiki/api.php','wikics.site':'/api.php'}
    api = 'https://' + host + paths[host]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = store.home/'derived'/'wiki-index'/stamp
    directory.mkdir(parents=True)
    record = {'api':api, 'namespace':0, 'batches':[]}
    manifest = directory/(host+'.json')
    params = {'action':'query','list':'allpages','apnamespace':'0','aplimit':'500','format':'json','formatversion':'2'}
    fetcher = Fetcher(store, [host], delay=1.5)
    fetcher.request_limit, fetcher.byte_limit, fetcher.deadline = 38, 16*1024**2, time.monotonic()+120
    seen = set()
    try:
        for _ in range(32):
            url = api + '?' + urlencode(params)
            fetcher.check_robots(url)
            code, _, raw, _ = fetcher.request(url, limit=2*1024**2)
            sha = digest(raw)
            (directory/f'{host}-{sha}.bytes').write_bytes(raw)
            record['batches'].append({'url':url, 'http':code, 'sha':sha})
            manifest.write_text(json.dumps(record), 'utf8')
            if code != 200:
                raise ValueError('HTTP ' + str(code))
            body = json.loads(raw)
            if not isinstance(body['query']['allpages'], list):
                raise ValueError('Invalid allpages response')
            step = body.get('continue')
            if step is None:
                record['terminal'] = True
                break
            token = step['apcontinue']
            if token in seen or set(step) - {'continue','apcontinue'}:
                raise ValueError('Invalid continuation')
            seen.add(token)
            params.update(step)
        else:
            record['stop'] = 'batch_limit'
    except Exception as error:
        record['stop'] = f'{type(error).__name__}: {error}'
    finally:
        fetcher.client.close()
        manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf8')
    return {'manifest_directory':str(directory), 'batches':len(record['batches']),
            'terminal':record.get('terminal', False), 'stop':record.get('stop')}


def site_index(directory, db, wiki):
    """Validate the continuation chain and intersect page IDs only on their own host."""
    results, rows = {}, []
    manifests = sorted(Path(directory).glob('*.json'))
    if not manifests:
        raise ValueError('No saved allpages manifests in the specified directory')
    for manifest in manifests:
        meta = json.loads(manifest.read_text('utf8'))
        api = urlsplit(meta['api'])
        saved_ids = {page['page_id'] for page in wiki if page.get('host') == api.hostname and page.get('page_id')}
        known_titles = set()
        for url, in db.execute("SELECT url FROM resources WHERE provider='mediawiki'"):
            url = urlsplit(url)
            if url.hostname != api.hostname or url.query:
                continue
            prefix = api.path.removesuffix('api.php')
            if not url.path.startswith(prefix):
                continue
            title = unquote(url.path.removeprefix(prefix)).replace('_', ' ')
            known_titles.add(title)
        pages, continuation = {}, {}
        terminal, issue = False, meta.get('stop')
        for batch in meta['batches']:
            if terminal:
                raise ValueError('Unexpected batch after end of index')
            url = urlsplit(batch['url'])
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if ((url.netloc,url.path) != (api.netloc,api.path) or query.get('list') != 'allpages'
                    or query.get('apnamespace') != '0' or
                    {k: v for k,v in query.items() if k in {'continue','apcontinue'}} != continuation):
                raise ValueError('Batch does not continue this namespace index')
            sha = batch['sha']
            if not re.fullmatch(r'[0-9a-f]{64}', sha) or manifest.stem != api.hostname:
                raise ValueError('Invalid inventory receipt')
            raw = manifest.with_name(f'{api.hostname}-{sha}.bytes').read_bytes()
            if digest(raw) != sha:
                raise ValueError('Inventory digest mismatch')
            if batch['http'] != 200:
                issue = 'HTTP ' + str(batch['http'])
                break
            try:
                response = json.loads(raw)
                batch_pages = response['query']['allpages']
            except (KeyError, ValueError, TypeError):
                issue = 'Invalid allpages response'
                break
            for page in batch_pages:
                if page['ns'] != 0 or page['pageid'] in pages:
                    raise ValueError('Invalid namespace or repeated page ID')
                pages[page['pageid']] = page['title']
            continuation = response.get('continue', {})
            terminal = not continuation
        local = []
        for pid, title in pages.items():
            state = ('saved_page_id' if pid in saved_ids else 'known_title_url' if title in known_titles
                     else 'no_exact_title_url')
            local.append({'host': api.hostname, 'pageid': pid, 'title': title, 'state': state})
        rows.extend(local)
        results[api.hostname] = {'terminal': terminal, 'issue': issue, 'batches': len(meta['batches']),
                                 'page_count': len(pages), 'states': dict(Counter(x['state'] for x in local))}
    return results, rows


def video_id(url):
    parts = urlsplit(url)
    if parts.hostname == 'youtu.be':
        value = parts.path.strip('/')
    elif parts.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com'}:
        if parts.path == '/watch':
            value = parse_qs(parts.query).get('v', [''])[0]
        elif parts.path.startswith(('/embed/', '/shorts/', '/live/')):
            value = parts.path.split('/')[-1]
        else:
            return None
    else:
        return None
    return value if re.fullmatch(r'[A-Za-z0-9_-]{11}', value) else None


def material_key(url):
    """Match a stable provider file/video ID or an exact canonical URL. Never match titles."""
    from .store import canonical
    vid = video_id(url)
    if vid:
        return 'youtube:' + vid
    parts = urlsplit(url)
    if parts.hostname == 'drive.google.com':
        match = re.match(r'/file/d/([\w-]+)(?:/|$)', parts.path)
        key = match[1] if match else parse_qs(parts.query).get('id', [''])[0]
        if key:
            return 'drive:' + key
    return canonical(url)


def pmi_plans(db, resources, lessons, lesson_links):
    sha_lessons = defaultdict(set)
    for name, lesson in lessons.items():
        for sha in lesson['original_shas']:
            sha_lessons[sha].add(name)
    by_sha = defaultdict(set)
    for rid, sha in db.execute('SELECT resource_id,sha FROM snapshots'):
        if sha in sha_lessons:
            by_sha[material_key(resources[rid]['url'])].update(sha_lessons[sha])
    snapshots = db.execute("""SELECT r.url,s.id,s.sha FROM resources r JOIN snapshots s ON s.resource_id=r.id
                              WHERE r.provider='mediawiki' ORDER BY s.created DESC,s.id""")
    years, ledger = {}, []
    for url, sid, sha in snapshots:
        parts = urlsplit(url)
        year = re.fullmatch(r'/wiki/Линейная_алгебра_и_геометрия_на_ПМИ_(\d{4}/\d{4})_\(основной_поток\)',
                            unquote(parts.path))
        if parts.hostname != 'fruitdet.ru' or not year or year[1] in years:
            continue
        links = defaultdict(list)
        for target, path, label in db.execute('SELECT target_id,path,label FROM occurrences WHERE snapshot_id=?', (sid,)):
            links[path.rsplit('/',1)[0]].append((target,path,label))
        plan = dict(lectures=0, without_links=0, link_occurrences=0, in_lesson=0, by_sha=0, player=0,
                    local_edition=0, url_reference_only=0, first_four_entries=0, first_four_matched=0)
        keys, lecture_paths = set(), set()
        for path, body in db.execute("SELECT path,text FROM nodes WHERE snapshot_id=? AND tag='p'", (sid,)):
            lecture = re.match(r'^Лекци[яи]\s+(\d+)', body, re.I)
            if not lecture:
                continue
            number = int(lecture[1])
            plan['lectures'] += 1
            found = [link for parent, batch in links.items() if parent == path or parent.startswith(path+'/')
                     for link in batch]
            if not found:
                plan['without_links'] += 1
                ledger.append({'year':year[1], 'snapshot':sid, 'sha':sha, 'lecture':number, 'path':path,
                               'entry':body, 'state':'plan_row_without_anchor'})
            for rid, linkpath, label in found:
                lecture_paths.add(linkpath)
                key = material_key(resources[rid]['url'])
                keys.add(key)
                linked = lesson_links.get(key, {})
                displayed = by_sha.get(key, set())
                players = linked.get('players', set())
                referenced = linked.get('links', set())
                local = linked.get('editions', set())
                used = displayed | players | referenced
                state = ('by_sha' if displayed else 'player' if players else 'local_edition' if local
                         else 'url_reference_only' if referenced
                         else 'lesson_notes_only' if linked.get('notes') else 'year_map_only' if linked.get('map')
                         else 'no_direct_lesson_match')
                ledger.append({'year':year[1], 'snapshot':sid, 'sha':sha, 'lecture':number, 'path':linkpath,
                               'label':label, 'resource_id':rid, 'url':resources[rid]['url'], 'entry':body,
                               'state':state, 'lessons':sorted(used), 'lesson_notes':sorted(linked.get('notes', ())),
                               'year_map':bool(linked.get('map'))})
                plan['link_occurrences'] += 1
                if used:
                    plan['in_lesson'] += 1
                    plan[state] += 1
                elif state != 'no_direct_lesson_match':
                    plan[state] = plan.get(state, 0) + 1
                if number <= 4:
                    plan['first_four_entries'] += 1
                    plan['first_four_matched'] += bool(used)
        plan['distinct_material_keys'] = len(keys)
        rest = db.execute("""SELECT o.id,o.target_id,o.path,o.label,o.headings,coalesce(c.navigation,0)
                 FROM occurrences o LEFT JOIN link_context c ON c.occurrence_id=o.id WHERE o.snapshot_id=?""", (sid,))
        plan['outside_lecture_occurrences'] = 0
        for occurrence, rid, path, label, headings, navigation in rest:
            if path in lecture_paths:
                continue
            plan['outside_lecture_occurrences'] += 1
            ledger.append({'year':year[1], 'snapshot':sid, 'sha':sha, 'path':path, 'resource_id':rid,
                           'url':resources[rid]['url'], 'occurrence':occurrence, 'label':label,
                           'headings':json.loads(headings), 'navigation':bool(navigation),
                           'state':'outside_numbered_lecture_row'})
        years[year[1]] = plan
    return dict(sorted(years.items())), ledger
