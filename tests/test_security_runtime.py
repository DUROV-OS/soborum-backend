"""Authentication compatibility and decoder boundaries on disposable inputs."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import jwt
import pytest
from PIL import Image
from app.core import security
from app.core.config import settings
from app.costing.schemas import image_value


def claims(**changes):
    return {'sub': '1', 'exp': datetime.now(timezone.utc) + timedelta(minutes=5), **changes}


def test_tokens_remain_standard_hs256_and_legacy_sessions_work():
    # Reproduce the old issuer's wire format independently of either library.
    enc = lambda data: base64.urlsafe_b64encode(data).rstrip(b'=')
    header = enc(b'{"alg":"HS256","typ":"JWT"}')
    payload = enc(json.dumps({'sub':'123','exp':int((datetime.now(timezone.utc)+timedelta(minutes=5)).timestamp())},separators=(',',':')).encode())
    body = header+b'.'+payload
    token = (body+b'.'+enc(hmac.new(settings.jwt_secret.encode(),body,hashlib.sha256).digest())).decode()
    assert security.decode_access_token(token) == '123'
    new = security.create_access_token('123')
    assert jwt.decode(new,settings.jwt_secret,algorithms=['HS256'])['sub'] == '123'


@pytest.mark.parametrize('change', [
    {'exp': 0}, {'sub': 1}, {'sub': ''}, {'sub': 'abc'}, {'sub':'-1'}, {'sub':'0'},
    {'sub':'2147483648'}, {'sub':'9'*1000}, {'sub':'١'}, {'nbf':4102444800},
])
def test_invalid_claims_are_unauthorized(change):
    token = jwt.encode(claims(**change), settings.jwt_secret, algorithm='HS256')
    assert security.decode_access_token(token) is None


@pytest.mark.parametrize('claim', ['exp','sub'])
def test_required_claims_cannot_be_omitted(claim):
    payload=claims();payload.pop(claim)
    assert security.decode_access_token(jwt.encode(payload,settings.jwt_secret,algorithm='HS256')) is None


def test_algorithm_and_signature_cannot_be_chosen_by_the_token():
    for token in [jwt.encode(claims(),key=None,algorithm='none'),
                  jwt.encode(claims(),settings.jwt_secret,algorithm='HS384'),
                  jwt.encode(claims(),'other-isolated-secret-with-at-least-32-bytes',algorithm='HS256'),
                  'invalid.token', '']:
        assert security.decode_access_token(token) is None


def test_unsupported_algorithm_configuration_is_rejected():
    from app.core.config import Settings
    from pydantic import ValidationError
    with pytest.raises(ValidationError): Settings(jwt_algorithm='RS256',_env_file=None)


def test_login_and_authorization_use_new_decoder_without_business_writes(db,make_user):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db.session import get_db
    user=make_user(admin=True)
    user.hashed_password=security.hash_password('isolated-test-password');db.commit()
    apps=[app]+[r.app for r in app.routes if hasattr(r,'app') and hasattr(r.app,'dependency_overrides')]
    saved=[dict(a.dependency_overrides) for a in apps]
    try:
        for a in apps:a.dependency_overrides[get_db]=lambda:db
        client=TestClient(app)
        login=client.post('/api/auth/login',data={'username':user.email,'password':'isolated-test-password'})
        assert login.status_code==200,login.text
        token=login.json()['access_token']
        assert client.get('/api/auth/me',headers={'Authorization':'Bearer '+token}).status_code==200
        bad=jwt.encode(claims(sub='not-an-id'),settings.jwt_secret,algorithm='HS256')
        assert client.get('/api/auth/me',headers={'Authorization':'Bearer '+bad}).status_code==401
        user.is_active=False;db.commit()
        assert client.get('/api/auth/me',headers={'Authorization':'Bearer '+token}).status_code==401
    finally:
        for a,previous in zip(apps,saved):a.dependency_overrides.clear();a.dependency_overrides.update(previous)


@pytest.mark.parametrize('format,mime', [('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp'),('GIF','image/gif')])
def test_supported_quote_images_survive_decoder_upgrade(format,mime):
    data=BytesIO();Image.new('RGB',(3,3),'white').save(data,format=format)
    value='data:'+mime+';base64,'+base64.b64encode(data.getvalue()).decode()
    assert image_value(value)==value


def test_image_mime_and_actual_decoder_must_agree():
    data=BytesIO();Image.new('RGB',(3,3),'white').save(data,format='TIFF')
    with pytest.raises(ValueError):image_value('data:image/png;base64,'+base64.b64encode(data.getvalue()).decode())
    data=BytesIO();Image.new('RGB',(3,3),'white').save(data,format='JPEG')
    with pytest.raises(ValueError):image_value('data:image/png;base64,'+base64.b64encode(data.getvalue()).decode())


def test_ocr_never_autodetects_external_interpreter_formats(monkeypatch,tmp_path):
    from app.common import file_text
    path=tmp_path/'fake.jpg';path.write_bytes(b'%!PS-Adobe-3.0 EPSF-3.0\n')
    original=Image.open
    def checked_open(file, **kwargs):
        assert kwargs['formats']==('JPEG','PNG','WEBP','TIFF','BMP')
        return original(file,**kwargs)
    monkeypatch.setattr(Image,'open',checked_open)
    monkeypatch.setattr(file_text,'_ocr_image',lambda *args:pytest.fail('Unsupported content reached OCR'))
    assert file_text._image_ocr(str(path))==''


def test_documented_purchase_upload_rejects_unsupported_decoder(native,db,api):
    from tests.test_purchase_controls import ready,P
    client,row,*_=ready(native,db,api)
    data=BytesIO();Image.new('RGB',(3,3),'white').save(data,format='TIFF')
    response=client.post(f'{P}/suppliers/{row.supplier_id}/documents',files={'file':('scan.png',data.getvalue(),'image/png')})
    assert response.status_code==422


from tests.test_costing_integration import native
