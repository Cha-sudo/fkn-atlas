"""Private readable report; escaped source text and no embedded source scripts."""
import html
from itertools import groupby
import json
from pathlib import Path
from urllib.parse import urlsplit, unquote
from .research import report


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
    programs = sorted({version['program'] for version in versions})
    if programs:
        pieces.append('<nav aria-label="Программы"><ul>'+''.join(
            f'<li><a href="#program-{i}">{escape(name)}</a></li>' for i,name in enumerate(programs)
        )+'</ul></nav>')
    else:
        pieces.append('<p>Версии курсов ещё не зарегистрированы.</p>')
    count_lessons = 0
    for i, (program, group) in enumerate(groupby(versions, key=lambda row: row['program'])):
        pieces.append(f'<section id="program-{i}"><h2>{escape(program)}</h2>')
        for year, yearly in groupby(group, key=lambda row: row['year']):
            pieces.append(f'<h3>{escape(year)}</h3>')
            for version in yearly:
                lessons = store.rows('SELECT * FROM lessons WHERE version_id=? ORDER BY position,id',(version['id'],))
                count_lessons += len(lessons)
                title = f'{version["course"]} · {version["stream"]}'
                pieces.append(f'<details id="v-{escape(version["id"])}"><summary>{escape(title)} · '
                              f'{len(lessons)} записей плана</summary>'
                              f'<p><a href="{escape(version["url"])}">Страница версии</a> · '
                              f'<code>{escape(version["path"])}</code><br>'
                              f'<small>SHA-256 <code>{escape(version["sha"])}</code></small></p>')
                if not lessons:
                    pieces.append('<p>Занятия для этой версии пока не внесены в каталог.</p>')
                for lesson in lessons:
                    _catalog_lesson(store, lesson, pieces)
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
        pieces.append('</section>')
    pieces.append('</html>')
    target = Path(output).resolve()
    target.write_text('\n'.join(pieces),encoding='utf8',newline='\n')
    return {'output':str(target),'programs':len(programs),'versions':len(versions),'lessons':count_lessons}


def _catalog_lesson(store, lesson, pieces):
    escape = html.escape
    label = LESSON_LABELS.get(lesson['status'], lesson['status'])
    pieces.append(f'<details id="lesson-{escape(lesson["id"])}"><summary>{escape(lesson["label"])}'
                  f' · {escape(label)}</summary>')
    if lesson['notes']:
        pieces.append(f'<p>{escape(lesson["notes"])}</p>')
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
