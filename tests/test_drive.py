from pathlib import Path
from uuid import uuid4
import pytest
from app.core.config import settings
from app.drive.models import DriveFile, Revision, Change
from app.drive.router import blob_path

P='/api/drive'
PDF=b'%PDF-1.4\nfirst version'


@pytest.fixture(autouse=True)
def storage(monkeypatch,tmp_path):
    monkeypatch.setattr(settings,'storage_dir',str(tmp_path))


def upload(client,scope='personal',name='Смета.pdf',data=PDF,**extra):
    return client.post(P+'/files',data={'request_id':str(uuid4()),'scope':scope,**extra},files={'file':(name,data,'application/pdf')})


def change(client,row,action,**extra):
    body={'request_id':str(uuid4()),'version':row['version'],**extra}
    method=client.patch if action in ('name','folder') else client.post
    return method(P+f'/files/{row["id"]}/{action}',json=body)


def test_private_files_hidden_even_from_administrator(api,make_user):
    owner,other,admin=make_user(),make_user(),make_user(admin=True)
    row=upload(api(owner)).json()
    for user in (other,admin):
        c=api(user)
        assert c.get(P+'/files',params={'scope':'personal'}).json()['total']==0
        assert c.get(P+f'/files/{row["id"]}/download').status_code==404
        assert c.get(P+f'/files/{row["id"]}/history').status_code==404
        assert change(c,row,'trash').status_code==404


def test_shared_read_edit_scope_and_trash(api,make_user):
    owner,other,admin=make_user(),make_user(),make_user(admin=True)
    row=upload(api(owner),'company').json()
    c=api(other)
    assert c.get(P+'/files',params={'scope':'company'}).json()['items'][0]['can_edit'] is False
    assert c.get(P+f'/files/{row["id"]}/download').content==PDF
    assert change(c,row,'trash').status_code==403
    assert upload(c,'company',file_id=row['id'],version=row['version']).status_code==403
    c=api(admin);trashed=change(c,row,'trash').json()
    assert c.get(P+f'/files/{row["id"]}/download').status_code==409
    assert api(other).get(P+f'/files/{row["id"]}/history').status_code==404
    assert api(other).get(P+'/files',params={'scope':'company','trash':True}).json()['total']==0
    restored=change(api(owner),trashed,'restore')
    assert restored.status_code==200
    assert api(other).get(P+f'/files/{row["id"]}/download').content==PDF


def test_versions_rename_stale_retry_and_checksums(api,make_user,db):
    c=api(make_user());request=str(uuid4())
    row=upload(c,request_id=request).json()
    assert upload(c,request_id=request).json()['id']==row['id']
    assert upload(c,request_id=request,data=PDF+b'other').status_code==409
    new_data=PDF+b' second'
    newer=upload(c,file_id=row['id'],version=1,data=new_data).json()
    assert upload(c,file_id=row['id'],version=1).status_code==409
    assert change(c,newer,'name',name='Смета.exe').status_code==422
    renamed=change(c,newer,'name',name='Смета проекта.pdf').json()
    history=c.get(P+f'/files/{row["id"]}/history').json()
    assert history['history_verified'] and len(history['versions'])==2 and len(history['changes'])==3
    assert c.get(P+f'/files/{row["id"]}/download').content==new_data
    old=history['versions'][-1]
    assert c.get(P+f'/files/{row["id"]}/download',params={'revision_id':old['id']}).content==PDF
    blob_path(old['id']).write_bytes(b'corrupted')
    assert c.get(P+f'/files/{row["id"]}/download',params={'revision_id':old['id']}).status_code==409
    req=str(uuid4())
    assert change(c,renamed,'trash',request_id=req).status_code==200
    assert change(c,renamed,'trash',request_id=req).status_code==200
    assert db.query(Change).count()==4


def test_folders_scope_move_and_search(api,make_user):
    owner,other=make_user(),make_user();c=api(owner)
    personal=c.post(P+'/folders',json={'request_id':str(uuid4()),'scope':'personal','name':'Сметы'}).json()
    shared=c.post(P+'/folders',json={'request_id':str(uuid4()),'scope':'company','name':'Договоры'}).json()
    assert upload(api(other),folder_id=personal['id']).status_code==404
    assert upload(api(other),'company',folder_id=personal['id']).status_code==404
    assert upload(api(other),'company',folder_id=shared['id']).status_code==201
    c=api(owner);row=upload(c,name='100%_budget.pdf').json()
    assert change(c,row,'folder',folder_id=shared['id']).status_code==404
    moved=change(c,row,'folder',folder_id=personal['id'])
    assert moved.status_code==200
    assert c.get(P+'/files',params={'scope':'personal'}).json()['total']==0
    assert c.get(P+'/files',params={'scope':'personal','folder_id':personal['id']}).json()['total']==1
    assert c.get(P+'/files',params={'scope':'personal','q':'%_'}).json()['total']==1
    assert c.get(P+'/files',params={'scope':'personal','q':'not found'}).json()['total']==0


def test_invalid_files_and_cross_file_revision(api,make_user,db):
    c=api(make_user())
    for name,data in [('script.exe',b'MZ'),('fake.pdf',b'not a PDF'),('../bad.pdf',PDF),('empty.pdf',b'')]:
        assert upload(c,name=name,data=data).status_code in (413,422)
    assert db.query(DriveFile).count()==0
    a,b=upload(c).json(),upload(c).json()
    assert c.get(P+f'/files/{a["id"]}/download',params={'revision_id':b['revision']['id']}).status_code==404


def test_history_detects_changed_audit_data(api,make_user,db):
    c=api(make_user());row=upload(c).json()
    event=db.query(Change).first();event.data={**event.data,'details':{'size':999}};db.commit()
    assert not c.get(P+f'/files/{row["id"]}/history').json()['history_verified']


def test_history_detects_unrecorded_metadata_and_removed_history(api,make_user,db):
    c=api(make_user());row=upload(c).json()
    item=db.get(DriveFile,row['id']);item.name='changed.pdf';db.commit()
    assert not c.get(P+f'/files/{row["id"]}/history').json()['history_verified']
    db.query(Change).delete();db.commit()
    assert not c.get(P+f'/files/{row["id"]}/history').json()['history_verified']


def test_failed_commit_removes_new_blob(api,make_user,db,monkeypatch):
    user=make_user();c=api(user)
    def fail():raise RuntimeError('synthetic commit failure')
    monkeypatch.setattr(db,'commit',fail)
    with pytest.raises(RuntimeError):upload(c)
    assert not list((Path(settings.storage_dir)/'drive').glob('*'))
    assert db.query(DriveFile).count()==0 and db.query(Revision).count()==0


def test_authentication_and_private_cache_policy(api,make_user):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get(P+'/files',params={'scope':'company'}).status_code==401
    response=api(make_user()).get(P+'/files',params={'scope':'company'})
    assert response.status_code==200 and response.headers['Cache-Control']=='private, no-store'
