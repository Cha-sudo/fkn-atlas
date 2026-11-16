"""Private readable report; escaped source text and no embedded source scripts."""
import html
import json
from pathlib import Path
from urllib.parse import urlsplit, unquote
from .research import report


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
