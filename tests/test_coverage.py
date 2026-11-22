import json
from hashlib import sha256

import pytest

from atlas.audit import coverage
from atlas.ingest import extract
from atlas.source_inventory import site_index
from atlas.store import Store
from test_crawl import worker


def test_coverage_partitions_urls_and_checks_archived_html(tmp_path):
    s = Store(tmp_path)
    url = 'https://fruitdet.ru/wiki/Wiki_ФКН'
    root = s.resource(url)
    sid = s.snapshot(root, b'<main><a href="/a">One</a><a href="/a">Again</a><a href="/b">Two</a></main>', 'text/html')
    extract(s, sid)
    s.attempt(root, 'unavailable', 502)
    a = s.resource('https://fruitdet.ru/a')
    s.attempt(a, 'blocked', error='robots')
    s.enqueue(s.resource('https://fruitdet.ru/b'))
    data = coverage(tmp_path, ledger=True)
    assert data['url_states'] == {'snapshot': 1, 'no_attempt_or_bytes_in_db': 1, 'attempt_without_snapshot': 1}
    assert data['root_reachable'] == data['url_states']
    assert data['wiki_html'][0]['html_anchors'] == 3
    assert data['wiki_html'][0]['missing'] == data['wiki_html'][0]['extra'] == 0
    assert data['course_reader']['with_private_oembed_200'] is None
    lines = [json.loads(line) for line in (tmp_path/'derived/coverage-urls.jsonl').read_text('utf8').splitlines()]
    assert next(line for line in lines if line['id'] == a)['occurrences'] == 2
    assert sum(line['state'] == 'seminar_catalog_only' for line in lines) == data['course_reader']['archive_video_ids']
    assert all(x['seminar']['oembed_status'] is None for x in lines if 'seminar' in x)
    with s.db:
        s.db.execute("DELETE FROM link_context WHERE occurrence_id IN (SELECT id FROM occurrences WHERE label='Again')")
        s.db.execute("DELETE FROM occurrences WHERE label='Again'")
    assert coverage(tmp_path)['wiki_html'][0]['missing'] == 1


def test_sampled_playback_survives_failed_request_without_claiming_video_bytes(tmp_path):
    s = Store(tmp_path)
    rid = s.resource('https://example.org/lecture')
    s.attempt(rid, 'browser_playback_sampled')
    s.attempt(rid, 'unavailable', 502)
    assert coverage(tmp_path)['url_states'] == {'sampled_playback_without_snapshot': 1}


def test_index_finish_and_host_identity(tmp_path):
    store = Store(tmp_path/'private')
    folder = tmp_path/'index'
    folder.mkdir()
    api = 'https://fruitdet.ru/wiki/api.php'
    query = '?action=query&list=allpages&apnamespace=0&format=json'
    meta = {'api': api, 'batches': []}
    for number, tail in enumerate(('', '&apcontinue=B&continue=-'), 1):
        body = {'query': {'allpages': [{'ns':0, 'pageid':number, 'title':chr(64+number)}]}}
        if number == 1:
            body['continue'] = {'apcontinue':'B', 'continue':'-'}
        raw = json.dumps(body).encode()
        sha = sha256(raw).hexdigest()
        (folder/f'fruitdet.ru-{sha}.bytes').write_bytes(raw)
        meta['batches'].append({'url':api+query+tail,'sha':sha,'http':200})
    name = folder/'fruitdet.ru.json'
    name.write_text(json.dumps(meta), 'utf8')
    result, _ = site_index(folder, store.db, [{'host':'wikics.site', 'page_id':1}])
    assert result['fruitdet.ru']['terminal']
    assert result['fruitdet.ru']['states'] == {'no_exact_title_url':2}
    meta['batches'].pop()
    name.write_text(json.dumps(meta), 'utf8')
    assert not site_index(folder, store.db, [])[0]['fruitdet.ru']['terminal']
    (folder/f"fruitdet.ru-{meta['batches'][0]['sha']}.bytes").write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='digest'):
        site_index(folder, store.db, [])


def test_pmi_rows_keep_other_links_and_same_numbers_on_different_years(tmp_path):
    s = Store(tmp_path)
    for year in ('2019/2020','2020/2021'):
        rid = s.resource('https://fruitdet.ru/wiki/Линейная_алгебра_и_геометрия_на_ПМИ_'+year+'_(основной_поток)')
        page = '<main><p>Лекция 1 (дата). <b><a href="/test.pdf">Слайды</a></b></p><p>Лекция 2 (дата).</p><a href="/sem">Seminar</a></main>'
        extract(s, s.snapshot(rid, page.encode(), 'text/html'))
    result = coverage(tmp_path, ledger=True)
    for plan in result['pmi_lecture_plans'].values():
        assert (plan['lectures'], plan['without_links'], plan['link_occurrences'], plan['in_lesson']) == (2,1,1,0)
        assert plan['outside_lecture_occurrences'] == 1
    assert len(result['pmi_lecture_plans']) == 2
    lines = [json.loads(x) for x in (tmp_path/'derived/coverage-pmi-lectures.jsonl').read_text('utf8').splitlines()]
    assert len(lines) == 6
    assert {x['state'] for x in lines} == {'no_direct_lesson_match','plan_row_without_anchor','outside_numbered_lecture_row'}


def test_targeted_run_keeps_other_host_jobs_pending(tmp_path, monkeypatch):
    monkeypatch.setattr('atlas.network.validate_url', lambda *args: None)
    s = Store(tmp_path)
    urls = ['https://example.org/a.txt', 'https://example.org/b.txt', 'https://other.example/c.txt']
    ids = [s.resource(url) for url in urls]
    for rid in ids:
        s.enqueue(rid)
    calls = []
    w = worker(s, dict.fromkeys(urls, 'text'), calls)
    w.allowed = {'example.org'}
    try:
        w.run(budget=4, resource_ids={ids[1],ids[2]})
    finally:
        w.client.close()
    assert [u for u in calls if not u.endswith('robots.txt')] == urls[1:2]
    assert dict(s.db.execute('SELECT resource_id,state FROM jobs')) == dict(zip(ids, ['pending','done','pending']))


def test_index_collection_follows_server_cursor_and_preserves_partial_batch(tmp_path, monkeypatch):
    from atlas.network import Fetcher
    from atlas.source_inventory import collect_site_index
    from urllib.parse import parse_qs, urlsplit

    store = Store(tmp_path)
    seen = []
    monkeypatch.setattr(Fetcher, 'check_robots', lambda self, url: None)

    def response(self, url, **kwargs):
        params = parse_qs(urlsplit(url).query)
        seen.append(params)
        if len(seen) == 1:
            value = {'query': {'allpages': [{'ns':0,'pageid':12,'title':'A'}]},
                     'continue': {'apcontinue':'B','continue':'-||'}}
            return 200, {}, json.dumps(value).encode(), url
        return 503, {}, b'unavailable', url

    monkeypatch.setattr(Fetcher, 'request', response)
    record = collect_site_index(store, 'fruitdet.ru')
    assert seen[1]['apcontinue'] == ['B']
    assert not record['terminal']
    result, _ = site_index(record['manifest_directory'], store.db, [])
    assert result['fruitdet.ru']['page_count'] == 1 and result['fruitdet.ru']['issue'] == 'HTTP 503'
