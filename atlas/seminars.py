"""Publish the course's curated seminar index and its private original documents."""
import json
from html import escape
from importlib.resources import files
from urllib.parse import quote

from .excerpts import render_excerpts


FOLDER = "https://disk.yandex.ru/d/idvfp8FiufvVLA"
PLAYLISTS = {
    "2020/21": "PLEwK9wdS5g0qXDi2_tibNsuox7fO4lWjI",
    "2021/22": "PLEwK9wdS5g0rM11uVo3KyYjlCU2Da-Ncq",
    "2022/23": "PLEwK9wdS5g0qz0T0B6fEcJWMnJhu-1NcE",
}
# Only pairs checked against the lesson recording, board and seminar material.
CHECKED = {
    "1": ("5U16wnINxgU", "6 сентября 2021", "26:40", 1600, "Согласование размеров при умножении матриц"),
    "2": ("m4ldEAXsx60", "13 сентября 2021", "15:50", 950, "Матрицы, коммутирующие с диагональной"),
    "3": ("AStD0u_Rhpc", "20 сентября 2021", "38:20", 2300, "Преобразования системы в методе Гаусса"),
    "4": ("8_fTfv3jVYU", "27 сентября 2021", "37:30", 2250, "Матричное уравнение при нильпотентной A"),
}
DOCUMENT_LABELS = {"problems": "Задачи", "algorithms": "Алгоритмы", "additional": "Дополнение",
                   "seminar": "Разбор семинара"}


def page(title, body):
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title} · ФКН Атлас</title><link rel="stylesheet" href="course.css">'
            '<script defer src="seminar-player.js"></script><script defer src="material-reader.js"></script>'
            '</head><body><main><header><p class="course-name">Линейная алгебра · ПМИ · основной поток</p>'
            f'<h1>{title}</h1><a href="index.html">Оглавление курса</a></header>{body}</main></body></html>')


def video_link(vid, text, start=0):
    url = f'https://www.youtube.com/watch?v={vid}'
    if start:
        url += f'&amp;t={start}s'
    return f'<a class="seminar-watch" href="{url}">▶ {escape(text)}</a>'


def document_label(name):
    if name.endswith(".svg"):
        return "Доска"
    part = name.rsplit(".", 1)[0].split("_")
    return DOCUMENT_LABELS[part[-1]] if len(part) > 1 else "Конспект"


def document_groups(data):
    collections = {}
    sessions = {}
    for number, documents in data["group213"].items():
        sessions[number] = [(document_label(name), sha, name.endswith(".svg"), FOLDER + "/" + quote(name))
                            for name, sha in documents]
    collections["213"] = ("2021/22", FOLDER, sessions)
    for group, source in data.get("handouts", {}).items():
        sessions = {}
        for number, file_id, sha in source["files"]:
            sessions[str(number)] = [("Материалы семинара", sha, False,
                                      f'https://drive.google.com/file/d/{file_id}/view')]
        collections[group] = (source["year"], "https://drive.google.com/drive/folders/" + source["folder"], sessions)
    return collections


def write_documents(store, destination, page_images, data):
    sections = ['<section id="papers"><h2>Материалы к семинарам</h2>']
    groups = document_groups(data)
    sections.append('<nav aria-label="Материалы групп">' + ''.join(
        f'<a href="#papers-{g}">БПМИ{g} · {year}</a>' for g, (year, _, _) in groups.items()) + '</nav>')
    for group, (year, folder, sessions) in groups.items():
        sections.append(f'<h3 id="papers-{group}">БПМИ{group} · {year}</h3>'
                        f'<p>Номера занятий приведены по <a href="{folder}">папке преподавателя</a>.</p>'
                        '<ol class="seminar-papers">')
        for number, documents in sessions.items():
            filename = f'seminar-{group}-{number}.html'
            labels = ', '.join(doc[0].lower() for doc in documents)
            sections.append(f'<li><a href="{filename}">Семинар {number}</a> · {labels}</li>')
            title = f'Семинар {number} · БПМИ{group} · {year}'
            neighbors = [f'<a href="seminars.html#papers-{group}">Все материалы группы</a>']
            for offset, caption in [(-1, 'Предыдущий'), (1, 'Следующий')]:
                other = str(int(number) + offset)
                if other in sessions:
                    neighbors.append(f'<a href="seminar-{group}-{other}.html">{caption} · семинар {other}</a>')
            body = ['<nav aria-label="Материалы группы">' + ''.join(neighbors) + '</nav>']
            if group == "213" and number in CHECKED:
                vid, date, clock, sec, topic = CHECKED[number]
                body.append(f'<div class="seminar-item"><h2>Видеозапись · {date}</h2>'
                            + video_link(vid, 'С начала') + ' · ' + video_link(vid, clock + ' · ' + topic, sec) + '</div>')
            for label, sha, board, url in documents:
                kind = 'board ' if board else ''
                body.append(f'<section><h2>{label}</h2><!-- document {kind}{sha} 1 | {label} · семинар {number} -->'
                            f'<a href="{url}">Оригинал в папке преподавателя</a></section>')
            markup = render_excerpts(page(title, ''.join(body)), store, destination, page_images)
            (destination / filename).write_text(markup, encoding='utf8', newline='\n')
        sections.append('</ol>')
    return ''.join(sections) + '</section>'


def write_seminars(store, destination, page_images):
    data = json.loads(files("atlas").joinpath("learning", "seminars.json").read_text(encoding="utf8"))
    years = data["years"]
    total = sum(len(items) for year in years.values() for items in year["groups"].values())
    parts = [f'<p>{total} видеозаписей. Выберите год и группу, затем нажмите на дату или номер '
             'семинара. Номера и даты взяты из подписей ФКН.</p>']
    nav = ['<a href="#papers">Конспекты, доски и задачи</a>']
    for year in years:
        nav.append(f'<a href="#year-{year}">{year}/{str(int(year) + 1)[2:]}</a>')
    parts.append('<nav aria-label="Годы и материалы">' + ''.join(nav) + '</nav>')
    companions = {value[0]: (f'seminar-213-{number}.html', value[4]) for number, value in CHECKED.items()}
    companions["pTzD67Gs3bU"] = ('matrix-equations.html#seminar', 'Три системы с одной матрицей')
    for year, info in years.items():
        parts.append(f'<section id="year-{year}"><h2>{year}/{str(int(year) + 1)[2:]}</h2>'
                     f'<p><a href="{escape(info["source"], quote=True)}">Группы в программе курса</a></p>')
        parts.append('<nav aria-label="Группы">' + ''.join(
            f'<a href="#group-{group}">БПМИ{group}</a>' for group in info["groups"]) + '</nav>')
        for group, records in info["groups"].items():
            parts.append(f'<h3 id="group-{group}">БПМИ{group} · записей {len(records)}</h3><ul class="seminar-list">')
            for label, vid in records:
                title = f'Семинар {label}' if label.isdecimal() else label
                parts.append(f'<li id="v-{vid}" class="seminar-item">{video_link(vid, title)}')
                if vid in companions:
                    path, topic = companions[vid]
                    parts.append(f' · <a href="{path}">{escape(topic)}</a>')
                parts.append('</li>')
            parts.append('</ul>')
        parts.append('</section>')
    parts.append(write_documents(store, destination, page_images, data))
    parts.append('<section><h2>Границы архива</h2><p>Страницы курса сверены за 2015/16–2026/27. '
                 'В просмотренном плейлисте 2022/23 все 34 позиции подписаны как лекции. '
                 'За годы вне трёх перечисленных подборок отдельные видео семинаров основного потока '
                 'пока не подтверждены. Полное число проведённых занятий по каждой группе неизвестно.</p>'
                 '<p>Принадлежность записей проверена по публикациям канала ФКН и спискам групп на Wiki. '
                 'Таймкоды в уроках отмечают отдельно проверенные фрагменты.</p>'
                 '<p>В открытых сообщениях групп <a href="https://t.me/s/LA_246_sem">БПМИ246</a> и '
                 '<a href="https://t.me/s/LA_256_sem">БПМИ256</a> найдены задания и материалы к семинарам. '
                 'Видеозаписи в просмотренных сообщениях пока не подтверждены.</p><p>')
    parts.append(' · '.join(f'<a href="https://www.youtube.com/playlist?list={pid}">Плейлист ФКН {year}</a>'
                            for year, pid in PLAYLISTS.items()))
    parts.append('</p><a href="years.html">Карта источников по годам</a></section>')
    (destination / 'seminars.html').write_text(page('Видеозаписи семинаров', ''.join(parts)), encoding='utf8', newline='\n')
