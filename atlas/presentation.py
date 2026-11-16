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
        blocks.append('<section><h2>'+esc(route['title'])+'</h2><p>'+esc(plan['limitations'])+'</p><ol>')
        for step in plan['steps']:
            title=store.rows('SELECT title FROM fragments WHERE id=?',(step['fragment_id'],))[0]['title']
            blocks.append(f'<li><a href="#f-{esc(step["fragment_id"])}">{esc(title)}</a><p>{esc(step["reason"])}</p><small>{esc(step["transition"])}</small></li>')
        blocks.append('</ol></section>')
    labels={'functions':'Образовательные функции','completeness':'Полнота','depth':'Глубина','correctness':'Корректность','clarity':'Ясность','sequence':'Последовательность','proofs':'Доказательства','examples':'Примеры','illustrations':'Иллюстрации','exercises':'Упражнения','prerequisites':'Предпосылки','learning_outputs':'Результаты обучения','uniqueness':'Уникальность','source_quality':'Источник','limitations':'Ограничения','confidence':'Уверенность','check_method':'Метод проверки'}
    for f in store.rows('SELECT f.*,a.analysis,a.reviewer FROM fragments f JOIN assessments a ON a.fragment_id=f.id WHERE a.version=(SELECT max(version) FROM assessments WHERE fragment_id=f.id) ORDER BY f.created'):
        blocks.append(f'<section id="f-{f["id"]}"><h2>{esc(f["title"])}</h2><p>{esc(f["topic"])}</p><table>')
        for k,v in json.loads(f['analysis']).items():
            blocks.append(f'<tr><th>{esc(labels.get(k,k))}</th><td>{esc(", ".join(v) if isinstance(v,list) else v)}</td></tr>')
        blocks.append('</table><h3>Проверяемые основания</h3>')
        for a in json.loads(f['anchors']):
            source=store.rows('SELECT c.locator,s.sha,r.url FROM chunks c JOIN extractions e ON e.id=c.extraction_id JOIN snapshots s ON s.id=e.snapshot_id JOIN resources r ON r.id=s.resource_id WHERE c.id=?',(a['chunk_id'],))[0]
            blocks.append(f'<p><a href="{esc(source["url"])}">Источник · {esc(source["locator"])}</a><br><small>SHA-256: <code>{source["sha"]}</code></small></p><details><summary>Исходный фрагмент</summary><pre>{esc(a["quote"])}</pre></details>')
        blocks.append('</section>')
    for decision in stats['latest_decisions']:
        blocks.append('<section><h2>Проверка восполнения: '+esc(decision['verdict'])+'</h2><p>'+esc(decision['reason'])+'</p><p>Покрыто: '+esc(', '.join(json.loads(decision['covered'])))+'</p><p>Осталось: '+esc(', '.join(json.loads(decision['uncovered'])))+'</p></section>')
    blocks.append('<section><h2>Ограничения автоматизации</h2><p>Поиск кандидатов лексический. Смысловые оценки внесены рецензентом по источникам. Видео не расшифрованы; полученные метаданные не считаются просмотром. OCR, автоматическая проверка доказательств и полноценный смысловой поиск ещё не реализованы.</p></section></html>')
    target=Path(output).resolve()
    target.write_text('\n'.join(blocks),encoding='utf8')
    return str(target)
