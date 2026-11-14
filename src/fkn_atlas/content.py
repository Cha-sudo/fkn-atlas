"""Bounded, offline inspection of already retrieved educational files.

``inspect_content(data: bytes, name: str) -> dict`` never writes input files,
executes downloaded code, loads macros, or recursively unpacks archives. Its
result has ``state``, ``extractor`` (including a version), ``text`` (at most
2 MiB as UTF-8), ``metadata`` and ``members``. A ZIP member has ``name``,
``size``, ``compressed_size``, ``state`` and ``sha256`` (None if bytes were not
read); safely supported documents additionally have ``body`` bytes and may
have ``text``, ``extractor`` and ``metadata``. Remove/store member ``body``
separately before serializing the result as JSON.

Archive rejection is atomic: no partial accepted members are returned. Text
truncation is explicit. An extracted text is never ``content_analyzed``; OCR,
formula fidelity and semantic review are separate operations. PDFs use an
isolated trusted Python worker with a deadline; POSIX also limits CPU/memory.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import subprocess
import sys
import unicodedata
import zlib
from importlib import metadata as package_metadata
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

MAX_INPUT_BYTES = 40 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_RATIO = 100
MAX_PDF_PAGES = 100
PDF_TIMEOUT_SECONDS = 20
TEXT_EXTENSIONS = {'.txt', '.md', '.rst', '.csv', '.tsv', '.py', '.tex', '.json', '.yaml', '.yml', '.r', '.c', '.cpp', '.h', '.hpp'}
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | {'.pdf', '.docx', '.ipynb'}
DEVICE_NAME = re.compile(r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$', re.I)


class ContentRejected(ValueError):
    """A fixed diagnostic code, without leaking input content into errors."""


class _NoDtdTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Parser callbacks see declarations after encoding detection (including UTF-16).
        raise ContentRejected('xml_entities_not_allowed')


def _result(state, extractor, *, text='', metadata=None, members=None):
    return {'state': state, 'extractor': extractor, 'text': text,
            'metadata': metadata or {}, 'members': members or []}


def _bounded_text(text, limit=MAX_TEXT_BYTES):
    raw = text.encode('utf-8', errors='replace')
    return raw[:limit].decode('utf-8', errors='ignore'), len(raw) > limit


def _normalized_member_name(info):
    name = info.orig_filename
    if not name or any(ord(c) < 32 for c in name) or '\\' in name or ':' in name or name.startswith('/'):
        raise ContentRejected('unsafe_member_path')
    parts = name.rstrip('/').split('/')
    if any(not part or part in ('.', '..') or part.endswith((' ', '.')) or DEVICE_NAME.fullmatch(part) for part in parts):
        raise ContentRejected('unsafe_member_path')
    return '/'.join(unicodedata.normalize('NFC', part).casefold() for part in parts)


def _preflight(archive):
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ContentRejected('too_many_members')
    seen = set()
    expanded = 0
    for info in infos:
        key = _normalized_member_name(info)
        if key in seen:
            raise ContentRejected('duplicate_member_path')
        seen.add(key)
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()):
            raise ContentRejected('non_regular_member')
        if info.flag_bits & 1:
            raise ContentRejected('encrypted_member')
        if info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
            raise ContentRejected('unsupported_compression')
        if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
            raise ContentRejected('member_size_limit')
        if info.is_dir() and info.file_size:
            raise ContentRejected('directory_has_content')
        if info.file_size > MAX_RATIO * max(1, info.compress_size):
            raise ContentRejected('compression_ratio_limit')
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise ContentRejected('expanded_size_limit')
    return infos


def _read_member(archive, info):
    with archive.open(info) as stream:
        body = stream.read(MAX_MEMBER_BYTES + 1)
    if len(body) > MAX_MEMBER_BYTES or len(body) != info.file_size:
        raise ContentRejected('member_size_mismatch')
    # ZipExtFile validates CRC while reading to EOF.
    return body


def _text_document(data, name):
    try:
        text = data.decode('utf-8-sig')
        encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        # Do not guess a legacy encoding and silently corrupt formula/source text.
        return _result('retrieved', 'text/1', metadata={'reason': 'unsupported_text_encoding'})
    if '\x00' in text:
        return _result('retrieved', 'text/1', metadata={'reason': 'binary_content'})
    text, truncated = _bounded_text(text)
    return _result('text_extracted', 'text/1', text=text,
                   metadata={'encoding': encoding, 'text_truncated': truncated,
                             'input_bytes': len(data), 'code_executed': False})


def _notebook(data):
    try:
        doc = json.loads(data.decode('utf-8-sig'))
        cells = doc.get('cells')
        if not isinstance(cells, list) or len(cells) > 10000:
            raise ContentRejected('invalid_notebook_cells')
        pieces = []
        supported = 0
        for cell in cells:
            if not isinstance(cell, dict):
                raise ContentRejected('invalid_notebook_cell')
            kind = cell.get('cell_type')
            if kind not in ('markdown', 'code', 'raw'):
                continue
            source = cell.get('source', '')
            if isinstance(source, list) and all(isinstance(x, str) for x in source):
                source = ''.join(source)
            if not isinstance(source, str):
                raise ContentRejected('invalid_notebook_source')
            pieces.append(f'[{kind}]\n{source}')
            supported += 1
        text, truncated = _bounded_text('\n\n'.join(pieces))
        return _result('text_extracted', 'ipynb-source/1', text=text,
            metadata={'cells_total': len(cells), 'cells_extracted': supported,
                      'outputs_extracted': False, 'code_executed': False,
                      'text_truncated': truncated})
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, RecursionError, ContentRejected):
        return _result('retrieved', 'ipynb-source/1', metadata={'reason': 'invalid_notebook'})


def _docx(data):
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            infos = _preflight(archive)
            by_name = {x.filename: x for x in infos}
            if 'word/document.xml' not in by_name:
                raise ContentRejected('missing_document_xml')
            raw = _read_member(archive, by_name['word/document.xml'])
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ContentRejected('xml_entities_not_allowed')
            xml = ElementTree.fromstring(raw, parser=ElementTree.XMLParser(target=_NoDtdTreeBuilder()))
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            paragraphs = []
            for paragraph in xml.iter(ns + 'p'):
                pieces = []
                for node in paragraph.iter():
                    if node.tag == ns + 't':
                        pieces.append(node.text or '')
                    elif node.tag == ns + 'tab':
                        pieces.append('\t')
                    elif node.tag in (ns + 'br', ns + 'cr'):
                        pieces.append('\n')
                paragraphs.append(''.join(pieces))
            text, truncated = _bounded_text('\n'.join(paragraphs))
            return _result('text_extracted', 'docx-document-xml/1', text=text,
                metadata={'paragraphs': len(paragraphs), 'text_truncated': truncated,
                          'coverage': 'word/document.xml only; headers, footnotes, drawings and equations not extracted',
                          'macros_executed': False})
    except ContentRejected as exc:
        return _result('rejected', 'docx-document-xml/1', metadata={'reason': str(exc)})
    except (BadZipFile, ElementTree.ParseError, RuntimeError, OSError, EOFError, NotImplementedError, LookupError, zlib.error):
        return _result('retrieved', 'docx-document-xml/1', metadata={'reason': 'invalid_docx'})


# This constant is trusted application code, never constructed from document content.
_PDF_WORKER = r'''
import io,json,sys
max_pages=int(sys.argv[1]);max_text_bytes=int(sys.argv[2])
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024,512*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU, (12,12))
except (ImportError,ValueError,OSError):
    pass
try:
    from pypdf import PdfReader
    reader=PdfReader(io.BytesIO(sys.stdin.buffer.read()),strict=False)
    if reader.is_encrypted:
        print(json.dumps({'error':'encrypted_pdf'}));sys.exit(0)
    total=len(reader.pages)
    chunks=[];used=0;read=0;truncated=False
    for page in reader.pages[:min(total,max_pages)]:
        raw=(page.extract_text() or '').encode('utf-8',errors='replace')
        available=max_text_bytes-used
        if chunks and available:
            chunks.append('\n');used+=1;available-=1
        chunks.append(raw[:available].decode('utf-8',errors='ignore'))
        used+=min(len(raw),available);read+=1
        if len(raw)>available:
            truncated=True;break
    print(json.dumps({'text':''.join(chunks),'pages_total':total,'pages_read':read,
                      'text_truncated':truncated,'page_limit_reached':read<total},ensure_ascii=False))
except Exception as exc:
    print(json.dumps({'error':type(exc).__name__}))
'''


def _pdf(data):
    try:
        version = package_metadata.version('pypdf')
    except package_metadata.PackageNotFoundError:
        return _result('extractor_unavailable', 'pypdf/unavailable', metadata={'dependency': 'pypdf'})
    extractor = 'pypdf/' + version
    try:
        run = subprocess.run([sys.executable, '-c', _PDF_WORKER, str(MAX_PDF_PAGES), str(MAX_TEXT_BYTES)], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PDF_TIMEOUT_SECONDS, check=False)
        if run.returncode:
            return _result('retrieved', extractor, metadata={'reason': 'pdf_worker_failed'})
        payload = json.loads(run.stdout)
        if payload.get('error'):
            return _result('retrieved', extractor, metadata={'reason': payload['error']})
        text = payload.pop('text', '')
        text, truncated = _bounded_text(text)
        payload['text_truncated'] = payload.get('text_truncated', False) or truncated
        payload['ocr_performed'] = False
        payload['formula_fidelity_verified'] = False
        return _result('text_extracted' if text.strip() else 'retrieved', extractor, text=text, metadata=payload)
    except subprocess.TimeoutExpired:
        return _result('retrieved', extractor, metadata={'reason': 'pdf_timeout'})
    except (OSError, ValueError, UnicodeError):
        return _result('retrieved', extractor, metadata={'reason': 'pdf_worker_error'})


def _archive(data):
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            infos = _preflight(archive)
            members = []
            remaining_text = MAX_TEXT_BYTES
            for info in infos:
                member = {'name': info.filename, 'size': info.file_size,
                          'compressed_size': info.compress_size, 'sha256': None,
                          'state': 'directory' if info.is_dir() else 'metadata_only'}
                suffix = PurePosixPath(info.filename.lower()).suffix
                if not info.is_dir() and suffix in DOCUMENT_EXTENSIONS:
                    body = _read_member(archive, info)
                    result = inspect_content(body, info.filename)
                    member.update(body=body, sha256=hashlib.sha256(body).hexdigest(),
                        state=result['state'], extractor=result['extractor'], metadata=result['metadata'])
                    text, truncated = _bounded_text(result['text'], remaining_text)
                    remaining_text -= len(text.encode('utf-8'))
                    member['text'] = text
                    if truncated:
                        member['metadata'] = {**member['metadata'], 'archive_text_truncated': True}
                else:
                    member['reason'] = 'directory' if info.is_dir() else 'unsupported_or_nested_archive_not_read'
                members.append(member)
            return _result('archive_inspected', 'zip-safe/1', members=members,
                metadata={'members_total': len(infos), 'members_read': sum('body' in x for x in members),
                          'declared_expanded_bytes': sum(x.file_size for x in infos),
                          'nested_archives_opened': False, 'code_executed': False,
                          'text_bytes': MAX_TEXT_BYTES - remaining_text})
    except ContentRejected as exc:
        return _result('rejected', 'zip-safe/1', metadata={'reason': str(exc), 'archive_rejected': True})
    except (BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError, zlib.error):
        return _result('rejected', 'zip-safe/1', metadata={'reason': 'invalid_zip', 'archive_rejected': True})


def inspect_content(data: bytes, name: str) -> dict:
    """Inspect bytes without network/filesystem extraction or semantic claims.

    The caller owns source provenance and must save the original ``data`` even
    when this returns unsupported/retrieved. ``metadata.text_truncated`` and
    PDF page counts delimit extracted coverage. Archive members are not
    independently downloaded resources; retain the parent archive relation.
    """
    if not isinstance(data, bytes):
        raise TypeError('data must be bytes')
    if len(data) > MAX_INPUT_BYTES:
        return _result('retrieved', 'bounded-inspector/1', metadata={'reason': 'input_size_limit'})
    suffix = PurePosixPath(name.lower()).suffix
    if suffix in TEXT_EXTENSIONS:
        return _text_document(data, name)
    if suffix == '.ipynb':
        return _notebook(data)
    if suffix == '.docx':
        return _docx(data)
    if suffix == '.pdf':
        return _pdf(data)
    if suffix == '.zip':
        return _archive(data)
    return _result('unsupported', 'bounded-inspector/1', metadata={'extension': suffix})
