"""Private readable report; escaped source text and no embedded source scripts."""
import html
from collections import defaultdict
import re
from itertools import groupby
from importlib.resources import files
import json
from pathlib import Path
from urllib.parse import quote, urlsplit, unquote
from .research import report
from .store import uid


LESSON_LABELS = {
    'not_in_plan': 'Не входит в план',
    'listed_without_link': 'В записи нет ссылки',
    'link_unavailable': 'Доступ по ссылке не подтверждён после проверки',
    'obtained_unread': 'Источник получен, содержание не проверено',
    'partial': 'Проверена часть содержания',
    'unverified': 'Не проверено',
    'content_checked': 'Содержание проверено в указанном объёме',
}


def write_catalog(store, output):
    """Browse confirmed course identities alongside the actual registered plan rows."""
    escape = html.escape
    target = Path(output).resolve()
    versions = store.rows('''SELECT v.*, r.url, s.sha, n.path FROM versions v
        JOIN snapshots s ON s.id=v.source_snapshot JOIN resources r ON r.id=s.resource_id
        JOIN nodes n ON n.id=v.evidence_node ORDER BY v.program,v.year DESC,v.course,v.stream,v.id''')
    pieces = [
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>ФКН Атлас — учебный каталог</title>',
        '<style>body{font:16px/1.55 system-ui;max-width:1040px;margin:36px auto;padding:0 20px;color:#172c36}'
        'section{margin:24px 0}summary{cursor:pointer}details{margin:10px 0;padding-left:12px;border-left:2px solid #a6b9ba}'
        'summary:focus-visible,a:focus-visible{outline:2px solid #176d70}code{overflow-wrap:anywhere}'
        'a{color:#125d63}small{color:#435d65}p{margin:0.6em 0}</style>',
        '<h1>Учебный каталог ФКН Атласа</h1>'
        '<p>Каталог содержит проверенные привязки к программе и году. '
        'Состояние занятия относится к зафиксированному объёму проверки.</p>',
    ]
    reviews = store.rows('''SELECT c.*,r.url,n.path,s.sha FROM catalog_program_reviews c
        JOIN nodes n ON n.id=c.node_id JOIN snapshots s ON s.id=n.snapshot_id
        JOIN resources r ON r.id=s.resource_id WHERE c.revision=(SELECT max(x.revision)
        FROM catalog_program_reviews x WHERE x.version_id=c.version_id)''')
    membership = {row['version_id']: row for row in reviews}
    by_program = defaultdict(list)
    series = defaultdict(list)
    for version in versions:
        review = membership.get(version['id'])
        programs = [version['program']]
        if review:
            data = json.loads(review['programs'])
            programs = sorted(data['members'])
            key = (data['series'], tuple(programs))
            series[key].append(version)
            version['series'] = key
        for program in programs:
            by_program[program].append(version)
    names = sorted(by_program)
    introductory_versions = {v['id'] for v in versions if v['program']=='ПМИ'
                             and v['course']=='Линейная алгебра и геометрия' and v['stream']=='основной'}
    start_link = ('<p><a href="atlas-lessons/matrices.html">Начать линейную алгебру ПМИ · '
                  'Урок 1. Матрицы и первые операции</a> · '
                  '<a href="atlas-lessons/index.html">Оглавление курса</a></p>')
    if introductory_versions:
        destination = target.parent/'atlas-lessons'
        destination.mkdir(exist_ok=True)
        learning = files('atlas').joinpath('learning')
        for name in ('course.css', 'lecture-video.js', 'matrices.html', 'gauss.html'):
            (destination/name).write_bytes(learning.joinpath(name).read_bytes())
        outline = learning.joinpath('index.html').read_text(encoding='utf8')
        catalog_link = f'<a href="../{quote(target.name, safe="")}">Все программы Атласа</a>'
        (destination/'index.html').write_text(outline.replace('<!-- catalog-link -->', catalog_link),
                                               encoding='utf8', newline='\n')
        pieces.append(start_link)
    if names:
        pieces.append('<nav aria-label="Программы"><ul>'+''.join(
            f'<li><a href="#program-{i}">{escape(name)}</a></li>' for i,name in enumerate(names)
        )+'</ul></nav>')
    else:
        pieces.append('<p>Версии курсов ещё не зарегистрированы.</p>')
    for i, program in enumerate(names):
        entries = by_program[program]
        pieces.append(f'<section id="program-{i}"><h2>{escape(program)}</h2>')
        for key in sorted({v['series'] for v in entries if 'series' in v}):
            pieces.append(f'<p><a href="#course-{uid(*key)}">{escape(key[0])}</a>'
                          f' · {escape(" + ".join(key[1]))} · все годы</p>')
        for year, year_entries in groupby(sorted(entries, key=lambda v: _catalog_year(v['year']),
                                                 reverse=True), key=lambda v: _catalog_year(v['year'])):
            pieces.append(f'<h3>{escape(year)}</h3>')
            for version in sorted(year_entries, key=lambda v: (v['course'],v['stream'],v['id'])):
                if 'series' in version:
                    pieces.append(f'<p><a href="#v-{escape(version["id"])}">{escape(version["course"])}'
                                  f' · {escape(version["stream"])}</a></p>')
                else:
                    _catalog_version(store, version, pieces)
        pieces.append('</section>')
    for key, entries in sorted(series.items()):
        pieces.append(f'<section id="course-{uid(*key)}"><h2>{escape(key[0])}</h2>'
                      f'<p>{escape(" + ".join(key[1]))}</p>')
        if any(v['id'] in introductory_versions for v in entries):
            pieces.append(start_link)
        _catalog_topics(store, entries, pieces)
        _catalog_readings(store, entries, pieces)
        pieces.append('<h3>Версии по учебным годам</h3>')
        for version in sorted(entries, key=lambda v: _catalog_year(v['year']), reverse=True):
            _catalog_version(store, version, pieces, membership[version['id']])
        pieces.append('</section>')
    pieces.append("""<script>
    function openTarget() {
        let target = document.getElementById(location.hash.slice(1));
        if (!target) return;
        for (let item = target; item; item = item.parentElement) {
            if (item.tagName === 'DETAILS') item.open = true;
        }
        target.scrollIntoView();
    }
    addEventListener('hashchange', openTarget);
    openTarget();
    </script>""")
    pieces.append('</html>')
    target.write_text('\n'.join(pieces),encoding='utf8',newline='\n')
    return {'output':str(target),'programs':len(names),'versions':len(versions),
            'lessons':store.rows('SELECT count(*) AS n FROM lessons')[0]['n'],'shared_courses':len(series)}


def _catalog_year(year):
    match = re.fullmatch(r'(\d{4})/(\d{4})', year)
    if match and int(match[2]) == int(match[1]) + 1:
        return match[1] + '/' + match[2][-2:]
    return year


def _catalog_topics(store, entries, pieces):
    version_ids = [v['id'] for v in entries]
    placeholders = ','.join('?' for _ in version_ids)
    rows = store.rows(f'''SELECT c.title,c.quote,l.id,l.label,v.year FROM catalog_sections c
        JOIN lessons l ON l.id=c.lesson_id JOIN versions v ON v.id=l.version_id
        WHERE v.id IN ({placeholders}) ORDER BY c.position,c.title,v.year,l.position,l.id''',version_ids)
    if rows:
        pieces.append('<h3>Темы по опубликованным планам</h3>')
    for title, matches in groupby(rows, key=lambda row: row['title']):
        pieces.append(f'<details><summary>{html.escape(title)}</summary><ul>')
        for row in matches:
            pieces.append(f'<li><a href="#lesson-{row["id"]}">{html.escape(_catalog_year(row["year"]))}'
                          f' · {html.escape(row["label"])}</a> · {html.escape(row["quote"])}</li>')
        pieces.append('</ul></details>')


def _catalog_readings(store, entries, pieces):
    versions = {v['id']: _catalog_year(v['year']) for v in entries}
    placeholders = ','.join('?' for _ in versions)
    rows = store.rows(f'''WITH sources(version_id,snapshot_id) AS (
        SELECT id,source_snapshot FROM versions WHERE id IN ({placeholders})
        UNION SELECT version_id,snapshot_id FROM version_snapshots WHERE version_id IN ({placeholders})
    ) SELECT f.id,f.title,f.anchors,a.analysis,src.version_id FROM fragments f
        JOIN assessments a ON a.fragment_id=f.id AND a.version=(
            SELECT max(newest.version) FROM assessments newest WHERE newest.fragment_id=f.id)
        JOIN json_each(f.anchors) anchor JOIN chunks c ON c.id=json_extract(anchor.value,'$.chunk_id')
        JOIN extractions e ON e.id=c.extraction_id JOIN sources src ON src.snapshot_id=e.snapshot_id
        GROUP BY f.id,src.version_id HAVING count(*)=json_array_length(f.anchors)
        ORDER BY f.title,f.id''', [*versions,*versions])
    if rows:
        pieces.append('<h3>Разобранные фрагменты материалов</h3>')
    for _, matches in groupby(rows, key=lambda row: row['id']):
        group = list(matches)
        row = group[0]
        years = ', '.join(sorted({versions[r['version_id']] for r in group}))
        analysis = json.loads(row['analysis'])
        pieces.append(f'<details><summary>{html.escape(row["title"])} · {html.escape(years)}</summary>')
        for field in ('completeness','correctness','limitations'):
            pieces.append(f'<p>{html.escape(analysis[field])}</p>')
        for anchor in json.loads(row['anchors']):
            source = store.rows('''SELECT c.locator,r.url,s.sha FROM chunks c
                JOIN extractions e ON e.id=c.extraction_id JOIN snapshots s ON s.id=e.snapshot_id
                JOIN resources r ON r.id=s.resource_id WHERE c.id=?''',(anchor['chunk_id'],))[0]
            pieces.append(f'<p><a href="{html.escape(source["url"])}">Оригинал · {html.escape(source["locator"])}</a>'
                          f'<br><small>SHA-256 <code>{html.escape(source["sha"])}</code></small></p>')
        pieces.append('</details>')


def _catalog_version(store, version, pieces, membership=None):
    escape = html.escape
    lessons = store.rows('''SELECT l.*,ctx.stream AS section_stream,ctx.heading_path
        FROM lessons l LEFT JOIN lesson_streams ctx ON ctx.lesson_id=l.id
        WHERE l.version_id=? ORDER BY ctx.stream,l.position,l.id''',(version['id'],))
    title = f'{version["course"]} · {version["stream"]}'
    sectioned = any(l['section_stream'] for l in lessons)
    if sectioned:
        title = f'{version["course"]} · потоки по разделам страницы'
    if membership:
        title = version['year'] + ' · ' + title
    pieces.append(f'<details id="v-{escape(version["id"])}"><summary>{escape(title)} · '
                  f'записей в каталоге — {len(lessons)}</summary>'
                  f'<p><a href="{escape(version["url"])}">Страница версии</a> · '
                  f'<code>{escape(version["path"])}</code><br>'
                  f'<small>SHA-256 <code>{escape(version["sha"])}</code></small></p>')
    if membership:
        pieces.append(f'<p>Программы по источнику · <a href="{escape(membership["url"])}">'
                      f'{escape(membership["quote"])}</a><br><small>{escape(membership["path"])} · '
                      f'SHA-256 <code>{escape(membership["sha"])}</code></small></p>')
    if not lessons:
        pieces.append('<p>Занятия для этой версии пока не внесены в каталог.</p>')
    for stream, group in groupby(lessons, key=lambda l: l['section_stream']):
        items = list(group)
        if sectioned:
            pieces.append(f'<details><summary>{escape(stream or "Поток не установлен")} · '
                          f'занятий в плане — {len(items)}</summary>')
        for lesson in items:
            _catalog_lesson(store, lesson, pieces)
        if sectioned:
            pieces.append('</details>')
    materials = store.rows('''SELECT s.id,s.sha,r.url,m.original_name,b.reason
        FROM version_snapshots b JOIN snapshots s ON s.id=b.snapshot_id
        JOIN resources r ON r.id=s.resource_id
        LEFT JOIN retrieval_metadata m ON m.snapshot_id=s.id
        WHERE b.version_id=? ORDER BY r.url,s.created''',(version['id'],))
    if materials:
        pieces.append('<details><summary>Проверенные привязки материалов к версии</summary><ul>')
        for material in materials:
            name = material['original_name'] or unquote(urlsplit(material['url']).path.rsplit('/',1)[-1])
            pieces.append(f'<li><a href="{escape(material["url"])}">{escape(name or "Источник")}</a>'
                          f'<p>{escape(material["reason"])}</p><small>Снимок '
                          f'<code>{escape(material["id"])}</code><br>SHA-256 '
                          f'<code>{escape(material["sha"])}</code></small></li>')
        pieces.append('</ul></details>')
    pieces.append('</details>')


def _catalog_lesson(store, lesson, pieces):
    escape = html.escape
    label = LESSON_LABELS.get(lesson['status'], lesson['status'])
    pieces.append(f'<details id="lesson-{escape(lesson["id"])}"><summary>{escape(lesson["label"])}'
                  f' · {escape(label)}</summary>')
    if lesson['notes']:
        pieces.append(f'<p>{escape(lesson["notes"])}</p>')
    if lesson['section_stream']:
        pieces.append(f'<p>{escape(lesson["section_stream"])} · заголовок источника '
                      f'<code>{escape(lesson["heading_path"])}</code></p>')
    topics = json.loads(lesson['topics'])
    if topics:
        pieces.append('<ul>'+''.join(f'<li>{escape(topic)}</li>' for topic in topics)+'</ul>')
    links = store.rows('''SELECT r.url,o.label,o.path,o.context,j.state,
        (SELECT count(*) FROM snapshots s WHERE s.resource_id=r.id) AS captures
        FROM lesson_materials m JOIN resources r ON r.id=m.resource_id
        LEFT JOIN occurrences o ON o.id=m.occurrence_id LEFT JOIN jobs j ON j.resource_id=r.id
        WHERE m.lesson_id=? ORDER BY o.path,r.url''',(lesson['id'],))
    if links:
        pieces.append('<ul>')
        for link in links:
            state = 'снимков: '+str(link['captures']) if link['captures'] else 'снимок не получен'
            if link['state']:
                state += ' · очередь: '+link['state']
            locator = f' · {link["path"]}' if link['path'] else ''
            pieces.append(f'<li><a href="{escape(link["url"])}">{escape(link["label"] or "Материал")}</a> '
                          f'<small>{escape(state+locator)}</small>')
            if link['context'] and link['context'] != link['label']:
                pieces.append('<details><summary>Контекст ссылки</summary>'
                              f'<p>{escape(link["context"])}</p></details>')
            pieces.append('</li>')
        pieces.append('</ul>')
    pieces.append('</details>')


def write_library(store, output):
    """Private local file navigation. Acquisition, extraction and review stay separate."""
    esc=lambda x:html.escape(str(x),quote=True)
    rows=store.rows('''SELECT s.*,r.url,b.size,m.original_name FROM snapshots s JOIN resources r ON r.id=s.resource_id
        JOIN blobs b ON b.sha=s.sha LEFT JOIN retrieval_metadata m ON m.snapshot_id=s.id ORDER BY s.created''')
    blocks=['<!doctype html><html lang="ru"><meta charset="utf-8"><title>ФКН Атлас — локальные оригиналы</title>',
            '<style>body{font:16px/1.5 system-ui;margin:32px}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #ddd;text-align:left}code{overflow-wrap:anywhere}details{max-width:500px}</style>',
            '<h1>Локальные оригиналы</h1><p>Сохранённые байты открываются по ссылке на файл. Метаданные провайдера и страницы каталога не означают получения всех материалов. Проверка отдельных фрагментов не означает проверки всего файла. Не публикуйте эту страницу.</p>',
            '<table><tr><th>Оригинал</th><th>Получено</th><th>Извлечение</th><th>Содержательная проверка</th><th>Происхождение</th></tr>']
    for row in rows:
        name=row['original_name'] or unquote(urlsplit(row['url']).path.rsplit('/',1)[-1]) or 'document'
        states=store.rows('SELECT status FROM extractions WHERE snapshot_id=?',(row['id'],))
        checked=store.rows('''SELECT DISTINCT f.id FROM fragments f JOIN assessments a ON a.fragment_id=f.id
            WHERE EXISTS(SELECT 1 FROM json_each(f.anchors) anchor JOIN chunks c ON c.id=json_extract(anchor.value,'$.chunk_id')
            JOIN extractions e ON e.id=c.extraction_id WHERE e.snapshot_id=?)''',(row['id'],))
        local=store.object_path(row['sha']).as_uri()
        origins=store.rows('''SELECT o.label,o.headings,o.path,r.url FROM occurrences o JOIN snapshots s ON s.id=o.snapshot_id
            JOIN resources r ON r.id=s.resource_id WHERE o.target_id=?''',(row['resource_id'],))
        provenance=''.join('<li>'+esc(o['headings']+' / '+o['label']+' / '+o['path'])+' <a href="'+esc(o['url'])+'">Источник</a></li>' for o in origins)
        blocks.append('<tr><td><a download="'+esc(name)+'" href="'+esc(local)+'">'+esc(name)+'</a></td><td>'+esc(row['mime'])+'<br>'+str(row['size'])+' байт</td><td>'+esc(', '.join(s['status'] for s in states) or 'не извлечено')+'</td><td>'+str(len(checked))+' проверенных фрагментов; весь файл не подтверждён</td><td><a href="'+esc(row['url'])+'">Исходный URL</a><details><summary>SHA и вхождения</summary><code>'+row['sha']+'</code><ul>'+provenance+'</ul></details></td></tr>')
    blocks.append('</table></html>')
    target=Path(output).resolve(); target.write_text('\n'.join(blocks),encoding='utf8',newline='\n')
    return {'output':str(target),'snapshots':len(rows),'bytes':sum(r['size'] for r in rows)}


def write_report(store, output):
    esc=lambda x:html.escape(str(x),quote=True)
    stats=report(store)
    blocks=['<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ФКН Атлас — локальный отчёт</title>',
        '<style>body{font:17px/1.6 system-ui;max-width:1050px;margin:40px auto;padding:0 24px;color:#182938;background:#fafaf7}h1,h2{line-height:1.2}section{background:white;border:1px solid #d8e0df;border-radius:12px;padding:24px;margin:24px 0}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}code{overflow-wrap:anywhere}summary{cursor:pointer}a{color:#086a69}small{color:#53636f}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
        '<h1>ФКН Атлас</h1><p>Локальное исследование · источники, проверенные фрагменты и учебная траектория</p>',
        '<p><strong>Обнаружение ссылки не означает прочтение материала.</strong> Числа ниже относятся только к выбранной выборке. Полнота всей Wiki неизвестна. Отчёт содержит локальные данные: не публикуйте его в Git.</p>',
        '<section><h2>Состояние коллекции</h2><table>']
    for key,value in stats['counts'].items(): blocks.append(f'<tr><th>{esc(key)}</th><td>{value}</td></tr>')
    blocks.append('</table></section>')
    for route in store.rows('SELECT t.* FROM trajectories t WHERE revision=(SELECT max(revision) FROM trajectories WHERE title=t.title) ORDER BY created'):
        plan=json.loads(route['plan'])
        blocks.append('<section><h2>'+esc(route['title'])+'</h2><p>'+esc(plan['limitations'])+
                      '</p><p>Предпосылки: '+esc(', '.join(plan.get('initial_knowledge', [])))+'</p><ol>')
        for step in plan['steps']:
            title=store.rows('SELECT title FROM fragments WHERE id=?',(step['fragment_id'],))[0]['title']
            link = 'a-'+step['assessment_id'] if step.get('assessment_id') else 'f-'+step['fragment_id']
            blocks.append(f'<li><a href="#{esc(link)}">{esc(title)}</a><p>{esc(step["reason"])}</p><small>{esc(step["transition"])}</small></li>')
        blocks.append('</ol></section>')
    comparisons = store.rows('''SELECT c.*,l.title AS left_title,r.title AS right_title
        FROM comparisons c JOIN fragments l ON l.id=c.left_fragment
        JOIN fragments r ON r.id=c.right_fragment ORDER BY c.created''')
    if comparisons:
        relation_names = {
            'full_replacement':'Полное замещение в указанном объёме',
            'partial_replacement':'Частичное замещение',
            'clearer_explanation':'Более ясное объяснение',
            'deeper_theory':'Углубление теории',
            'additional_practice':'Дополнительная практика',
            'alternative_approach':'Альтернативный подход',
            'unique_content':'Уникальное содержание',
            'insufficient_data':'Недостаточно данных',
            'rejected':'Отклонено',
        }
        blocks.append('<section><h2>Сравнения фрагментов</h2>')
        for item in comparisons:
            dimensions = json.loads(item['dimensions'])
            refs = dimensions.get('assessment_refs', [])
            links = []
            for index, side in enumerate(('left', 'right')):
                link = 'a-'+refs[index] if index < len(refs) else 'f-'+item[side+'_fragment']
                links.append(f'<a href="#{esc(link)}">{esc(item[side+"_title"])}</a>')
            blocks.append(f'<details><summary>{esc(relation_names.get(item["relation"],item["relation"]))}</summary>'
                          f'<p>{" / ".join(links)}</p><p>{esc(item["reason"])}</p>'
                          f'<p>{esc(item["limitations"])}</p>')
            criteria = dimensions.get('criteria', dimensions)
            if isinstance(criteria, dict):
                blocks.append('<ul>'+''.join(f'<li>{esc(key)}: {esc(value)}</li>'
                                              for key,value in criteria.items())+'</ul>')
            elif isinstance(criteria, list):
                blocks.append('<ul>'+''.join(f'<li>{esc(value)}</li>' for value in criteria)+'</ul>')
            blocks.append('</details>')
        blocks.append('</section>')
    labels={'functions':'Образовательные функции','completeness':'Полнота','depth':'Глубина','correctness':'Корректность','clarity':'Ясность','sequence':'Последовательность','proofs':'Доказательства','examples':'Примеры','illustrations':'Иллюстрации','exercises':'Упражнения','prerequisites':'Предпосылки','learning_outputs':'Результаты обучения','uniqueness':'Уникальность','source_quality':'Источник','limitations':'Ограничения','confidence':'Уверенность','check_method':'Метод проверки'}
    for f in store.rows('SELECT * FROM fragments ORDER BY created'):
        blocks.append(f'<section id="f-{esc(f["id"])}"><h2>{esc(f["title"])}</h2>'
                      f'<p>{esc(f["topic"])} · {esc(", ".join(json.loads(f["subtopics"])))}</p>')
        for a in json.loads(f['anchors']):
            source=store.rows('SELECT c.locator,s.id,s.sha,r.url FROM chunks c JOIN extractions e ON e.id=c.extraction_id JOIN snapshots s ON s.id=e.snapshot_id JOIN resources r ON r.id=s.resource_id WHERE c.id=?',(a['chunk_id'],))[0]
            versions = store.rows('''SELECT v.program,v.year,v.course,v.stream FROM versions v
                WHERE v.source_snapshot=? OR EXISTS(
                    SELECT 1 FROM version_snapshots b WHERE b.version_id=v.id AND b.snapshot_id=?)''',
                (source['id'],source['id']))
            scope = ' / '.join(' · '.join(v[k] for k in ('program','year','course','stream')) for v in versions)
            blocks.append(f'<p><a href="{esc(source["url"])}">Источник · {esc(source["locator"])}</a><br>'
                          f'<small>{esc(scope or "Версия курса не подтверждена")}<br>'
                          f'Снимок: <code>{esc(source["id"])}</code><br>SHA-256: <code>{esc(source["sha"])}</code></small></p>'
                          f'<details><summary>Исходный фрагмент</summary><pre>{esc(a["quote"])}</pre></details>')
        for assessment in store.rows('SELECT * FROM assessments WHERE fragment_id=? ORDER BY version DESC',(f['id'],)):
            blocks.append(f'<div id="a-{esc(assessment["id"])}"><h3>Оценка · версия {assessment["version"]}</h3>'
                          f'<p>{esc(assessment["reviewer"])}</p><table>')
            for name,value in json.loads(assessment['analysis']).items():
                detail = ', '.join(value) if isinstance(value,list) else value
                blocks.append(f'<tr><th>{esc(labels.get(name,name))}</th><td>{esc(detail)}</td></tr>')
            blocks.append('</table></div>')
        blocks.append('</section>')
    for decision in stats['latest_decisions']:
        lesson = store.rows('''SELECT l.label,v.program,v.year,v.course FROM lessons l
            JOIN versions v ON v.id=l.version_id WHERE l.id=?''',(decision['lesson_id'],))[0]
        heading = ' · '.join(lesson[k] for k in ('program','year','course','label'))
        blocks.append(f'<section><h2>{esc(heading)}</h2><p>Решение {decision["version"]} · {esc(decision["verdict"])}</p>'
                      f'<p>{esc(decision["reason"])}</p><p>Покрыто: {esc(", ".join(json.loads(decision["covered"])))}</p>'
                      f'<p>Осталось: {esc(", ".join(json.loads(decision["uncovered"])))}</p>')
        for evidence in store.rows('''SELECT ev.quote,ev.check_method,c.locator,s.sha,r.url FROM evidence ev
            JOIN chunks c ON c.id=ev.chunk_id JOIN extractions e ON e.id=c.extraction_id
            JOIN snapshots s ON s.id=e.snapshot_id JOIN resources r ON r.id=s.resource_id
            WHERE ev.review_id=?''',(decision['id'],)):
            blocks.append(f'<details><summary>{esc(evidence["locator"])}</summary>'
                          f'<p><a href="{esc(evidence["url"])}">Источник</a> · <code>{esc(evidence["sha"])}</code></p>'
                          f'<pre>{esc(evidence["quote"])}</pre><p>{esc(evidence["check_method"])}</p></details>')
        blocks.append('</section>')
    blocks.append('<section><h2>Ограничения автоматизации</h2><p>Поиск кандидатов лексический. Смысловые оценки внесены рецензентом по источникам. Видео не расшифрованы; полученные метаданные не считаются просмотром. OCR, автоматическая проверка доказательств и полноценный смысловой поиск ещё не реализованы.</p></section></html>')
    target=Path(output).resolve()
    target.write_text('\n'.join(blocks),encoding='utf8')
    return str(target)
