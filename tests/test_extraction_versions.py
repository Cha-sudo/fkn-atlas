import io
import json
import logging
from pypdf import PdfWriter
from atlas.store import Store
from atlas.ingest import extract
from atlas.research import candidates,report
from atlas.pedagogy import validate_anchors


def test_pdf_upgrade_is_explicit_preserves_anchors_and_searches_latest(tmp_path,monkeypatch):
    s=Store(tmp_path/'private')
    writer=PdfWriter();writer.add_blank_page(width=100,height=100)
    buffer=io.BytesIO();writer.write(buffer)
    sid=s.snapshot(s.resource('https://example.org/own-fixture.pdf'),buffer.getvalue(),'application/pdf')
    monkeypatch.setattr('atlas.ingest.PDF_ENGINE','fixture-engine-old')
    monkeypatch.setattr('atlas.ingest.pdf_chunks',lambda *a:([('page:1','OldToken matrix definition','needs_visual_review')],'text_extracted_needs_visual_review'))
    extract(s,sid)
    old=s.rows('SELECT * FROM chunks')[0]
    monkeypatch.setattr('atlas.ingest.PDF_ENGINE','fixture-engine-new')
    monkeypatch.setattr('atlas.ingest.pdf_chunks',lambda *a:([('page:1','NewToken matrix definition','needs_visual_review')],'text_extracted_needs_visual_review'))
    extract(s,sid)  # Cached crawling is not a request to reprocess old PDFs.
    assert len(s.rows('SELECT * FROM extractions'))==1
    extract(s,sid,reprocess=True);extract(s,sid,reprocess=True)
    assert len(s.rows('SELECT * FROM extractions'))==2
    validate_anchors(s,[{'chunk_id':old['id'],'quote':'OldToken matrix definition'}])
    assert all(r['id']!=old['id'] for r in candidates(s,'matrix definition'))
    assert sum(x['n'] for x in report(s)['extraction_states'])==1
    assert sum(x['n'] for x in report(s)['extraction_history_states'])==2
    assert len(s.rows('SELECT * FROM snapshots'))==1 and not s.verify()['bad_objects']
    s.db.close()


def test_pdf_warnings_are_bounded_persisted_and_logger_restored(tmp_path,monkeypatch):
    s=Store(tmp_path/'private');rid=s.resource('https://example.org/own-fixture.pdf')
    sid=s.snapshot(rid,b'%PDF synthetic parser boundary','application/pdf')
    logger=logging.getLogger('pypdf.fixture');parent=logging.getLogger('pypdf')
    old=parent.propagate
    def parse(*args):
        for n in range(25):logger.warning('Warning %s %s',n,'x'*1000)
        return [('page:1','Mathematical content needs visual review','needs_visual_review')],'text_extracted_needs_visual_review'
    monkeypatch.setattr('atlas.ingest.pdf_chunks',parse)
    extract(s,sid)
    row=s.rows('SELECT * FROM extractions')[0]
    data=json.loads((s.home/'derived'/(row['id']+'.json')).read_text('utf8'))
    assert data['diagnostics']['warning_count']==25
    assert len(data['diagnostics']['messages'])==16
    assert all(len(x)<=600 for x in data['diagnostics']['messages'])
    assert parent.propagate==old
    assert s.rows('SELECT quality FROM chunks')[0]['quality']=='needs_visual_review'
    s.db.close()
