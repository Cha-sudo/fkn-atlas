"""Durable educational frontier over the existing jobs queue, not semantic inference."""
import json
import re
import time
from urllib.parse import urlsplit, parse_qsl, unquote
from .store import uid
from .providers import provider, provider_links

POLICY = 'educational-links-1'
EDUCATION = re.compile(r'лекци|семинар|учеб|курс|материал|задач|задан|практи|лаборатор|экзамен|конспект|архив|программ|алгоритм|lecture|course|syllabus|notes|tutorial|exercise|assignment|material|textbook', re.I)
DOCUMENT = re.compile(r'\.(pdf|djvu|docx?|pptx?|xlsx?|tex|txt|md|ipynb|py|cpp|c|h|r|jl|zip|tar|gz|mp4|webm|mp3|wav|ogg|flac|png|jpe?g|svg|vtt|srt)$', re.I)
SERVICE = re.compile(r'(?:^|[/?:=_-])(login|logout|signin|signout|signup|register|calendar|search|feed|rss|wp-admin)(?:$|[/?:=_-])', re.I)
NAMESPACE = re.compile(r'/wiki/(Special|Служебная|User|Участник|User_talk|Обсуждение|Talk|Category|Категория|Template|Шаблон|MediaWiki):', re.I)


def classify(link, source_url):
    """Conservative admission by observed context. Never a judgment of content quality."""
    url = link['url']; p = urlsplit(url); decoded = unquote(p.path)
    if p.scheme not in {'https','http'} or p.port not in {None,80,443}:
        return 'excluded', 'unsafe_scheme_or_port'
    if link.get('navigation'):
        return 'excluded', 'navigation'
    if re.search(r'ведомост|оценки студентов|student grades',link['headings']+' '+link['label'],re.I):
        return 'excluded', 'administrative_records'
    if p.hostname in {'t.me','telegram.me'} or (re.search(r'преподавател|ассистент',link['headings'],re.I) and not EDUCATION.search(link['label'])):
        return 'pending', 'contact_not_teaching_content'
    if link['path'].startswith('/tree/'):
        return 'pending', 'legacy_tree_without_verified_commit'
    if NAMESPACE.search(decoded) or SERVICE.search(decoded):
        return 'excluded', 'service_or_namespace'
    query = dict(parse_qsl(p.query,keep_blank_values=True))
    if set(query)&{'action','oldid','diff','redlink','printable','feed','search','sort','filter','calendar','page'}:
        return 'excluded', 'service_or_query_cycle'
    # Only our provider parser can issue these bounded API/pagination links.
    if link['path'].startswith('/provider/'):
        if link['context'].startswith('PENDING:'):
            if link['context'].startswith('PENDING:download_host_unverified:') and p.hostname=='downloader.disk.yandex.ru' and p.scheme=='https':
                return 'admitted', 'provider_public_download'
            return 'pending', link['context'].split(':',2)[1]
        return 'admitted', 'provider_public_api'
    kind = provider(url)
    if query and not (kind == 'youtube' and set(query)<={'v','list','t','index'}):
        return 'pending', 'unreviewed_query'
    if kind == 'google_drive':
        return 'pending', 'drive_listing_adapter_unavailable'
    if p.hostname == 'gitlab.com':
        return 'pending', 'gitlab_adapter_unavailable'
    if kind == 'mediawiki' and '/wiki/' in decoded:
        return 'admitted', 'educational_wiki_content'
    context = ' '.join([link['label'],link['context'],link['headings']])
    if EDUCATION.search(context) or DOCUMENT.search(decoded):
        return 'admitted', 'observed_educational_context'
    return 'pending', 'educational_relation_uncertain'


class Crawl:
    def __init__(self, store, root_url, max_depth=4, max_frontier=5000):
        if not 0<=max_depth<=20 or not 1<=max_frontier<=50000:
            raise ValueError('Depth must be 0..20; frontier 1..50000')
        self.store,self.max_depth,self.max_frontier = store,max_depth,max_frontier
        self.root = store.resource(root_url,'crawl_root')
        self.id = uid('crawl',self.root,POLICY)
        self.processed = set()
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO crawl_scopes VALUES(?,?,?,?)',
                             (self.id,self.root,POLICY,time.time()))
        self.admit(self.root,0)

    def admit(self, rid, depth):
        with self.store.db:
            self.store.db.execute('''INSERT INTO frontier VALUES(?,?,?) ON CONFLICT(scope_id,resource_id)
                DO UPDATE SET depth=min(depth,excluded.depth)''',(self.id,rid,depth))
        self.store.enqueue(rid)

    def ids(self):
        return {r['resource_id'] for r in self.store.rows('SELECT resource_id FROM frontier WHERE scope_id=? AND depth<=?',(self.id,self.max_depth))}

    def expand_cached(self):
        # Repeating after a crash, a shallower path or changed depth limit is harmless.
        # All occurrences remain; a resource has one global fetch job.
        while True:
            pending = self.store.rows('''SELECT f.resource_id,f.depth,s.id AS sid,r.url FROM frontier f
                JOIN resources r ON r.id=f.resource_id JOIN snapshots s ON s.resource_id=f.resource_id
                WHERE f.scope_id=? AND f.depth<=? AND s.created=(SELECT max(created) FROM snapshots WHERE resource_id=f.resource_id)
                ORDER BY f.depth,f.resource_id''',(self.id,self.max_depth))
            work=[r for r in pending if (r['resource_id'],r['sid'],r['depth']) not in self.processed]
            if not work: break
            for row in work:
                self.processed.add((row['resource_id'],row['sid'],row['depth']))
                self.expand(row)

    def expand(self, row):
        from .ingest import extract
        try:
            extract(self.store,row['sid'])
            provider_links(self.store,row['sid'])
        except Exception as e:
            self.store.attempt(row['resource_id'],'expansion_error',error=type(e).__name__+':'+str(e)[:300],sid=row['sid'])
            return
        links=self.store.rows('''SELECT o.*,r.url,coalesce(c.navigation,0) AS navigation FROM occurrences o
            JOIN resources r ON r.id=o.target_id LEFT JOIN link_context c ON c.occurrence_id=o.id
            WHERE o.snapshot_id=? ORDER BY o.path''',(row['sid'],))
        count=len(self.ids()); known=self.ids()
        for link in links:
            state,reason=classify(link,row['url'])
            depth=row['depth']+1
            if state=='admitted' and depth>self.max_depth:
                state,reason='pending','depth_limit'
            if state=='admitted' and link['target_id'] not in known and count>=self.max_frontier:
                state,reason='pending','frontier_limit'
            if state=='admitted':
                self.admit(link['target_id'],depth)
                if link['target_id'] not in known: count+=1; known.add(link['target_id'])
            with self.store.db:
                self.store.db.execute('''INSERT INTO crawl_edges VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(scope_id,occurrence_id) DO UPDATE SET depth=excluded.depth,state=excluded.state,reason=excluded.reason''',
                    (self.id,link['id'],row['resource_id'],link['target_id'],depth,state,reason))

    def summary(self):
        return {'scope_id':self.id,'policy':POLICY,'max_depth':self.max_depth,
                'resources_admitted':len(self.ids()),'semantics':'not_inferred_from_links',
                'edges':self.store.rows('SELECT state,reason,count(*) AS n FROM crawl_edges WHERE scope_id=? GROUP BY state,reason',(self.id,)),
                'queue':self.store.rows('''SELECT j.state,count(*) AS n FROM jobs j JOIN frontier f ON f.resource_id=j.resource_id
                    WHERE f.scope_id=? AND f.depth<=? GROUP BY j.state''',(self.id,self.max_depth))}

    def record_run(self, result):
        with self.store.db:
            self.store.db.execute('INSERT INTO crawl_runs VALUES(?,?,?,?)',
                (uid(self.id,time.time_ns()),self.id,time.time(),json.dumps(result,ensure_ascii=False)))
