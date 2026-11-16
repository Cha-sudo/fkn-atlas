import pytest
from atlas.store import Store
from atlas.ingest import extract
from atlas.connectors import drive_listing
from atlas.providers import provider_links


def test_drive_listing_is_provenance_not_completeness(tmp_path):
    s=Store(tmp_path/'private')
    url='https://drive.google.com/drive/folders/fixture_folder?usp=sharing'
    payload={'files':[{'id':'fixture_pdf','url':'https://drive.google.com/file/d/fixture_pdf/view',
                       'title':'Own synthetic exercise.pdf','mime_type':'application/pdf','size':'123'}]}
    with pytest.raises(ValueError,match='provenance'):
        drive_listing(s,url,payload)
    root=s.resource('https://example.org/course')
    extract(s,s.snapshot(root,('<main><a href="'+url+'">Lecture materials</a></main>').encode(),'text/html'))
    result=drive_listing(s,url,payload)
    assert result['observed_children']==1 and not result['listing_complete']
    assert drive_listing(s,url,payload)['snapshot_id']==result['snapshot_id']
    provider_links(s,result['snapshot_id'])
    assert s.rows('SELECT state FROM provider_states')[0]['state']=='connector_directory_partial'
    assert len(s.rows('SELECT id FROM occurrences WHERE snapshot_id=?',(result['snapshot_id'],)))==1
    assert not s.rows('SELECT * FROM version_materials')
    assert not s.rows("SELECT s.id FROM snapshots s JOIN resources r ON r.id=s.resource_id WHERE r.url LIKE '%fixture_pdf%'")
    bad={'files':[{**payload['files'][0],'url':'https://other.example/file/d/fixture_pdf/view'}]}
    with pytest.raises(ValueError,match='provider ID'):
        drive_listing(s,url,bad)
    assert len(s.rows('SELECT id FROM snapshots'))==2
    s.db.close()
