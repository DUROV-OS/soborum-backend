"""Private calendar attachments. Uploads are drafts until a versioned event save binds them."""
import hashlib
import io
import re
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile, BadZipFile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, Session
from app.db.base import Base
from app.db.session import get_db
from app.core.config import settings
from app.core.deps import get_current_user
from app.users.models import User
from app.warehouse.documents import advisory

router = APIRouter()
MAX_FILE = 50 * 1024 * 1024
FORMATS = {'.pdf':'application/pdf', '.doc':'application/msword', '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
           '.xls':'application/vnd.ms-excel', '.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
           '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.png':'image/png', '.zip':'application/zip'}

class CalendarFile(Base):
    __tablename__ = 'team_calendar_files'
    __table_args__ = (UniqueConstraint('uploaded_by_id','request_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey('team_calendar_events.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(120))

def file_out(item):
    return {k:getattr(item,k) for k in ('id','filename','size','sha256','media_type')}

def file_path(id):
    return Path(settings.storage_dir) / 'calendar' / str(UUID(id))

def validate_file(filename,data):
    extension=Path(filename).suffix.lower()
    if extension not in FORMATS: raise HTTPException(422,'Поддерживаются PDF, DOC/DOCX, XLS/XLSX, JPG, PNG, ZIP')
    magic={'.pdf':b'%PDF-', '.doc':b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', '.xls':b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', '.png':b'\x89PNG\r\n\x1a\n', '.jpg':b'\xff\xd8\xff', '.jpeg':b'\xff\xd8\xff'}
    if extension in magic and not data.startswith(magic[extension]): raise HTTPException(422,'Содержимое файла не соответствует расширению')
    if extension in ('.zip','.docx','.xlsx'):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                names=set(archive.namelist())
                if extension!='.zip' and not {'[Content_Types].xml','word/document.xml' if extension=='.docx' else 'xl/workbook.xml'}.issubset(names):
                    raise HTTPException(422,'Некорректный документ Office')
        except BadZipFile: raise HTTPException(422,'Повреждённый ZIP или документ Office')
    return FORMATS[extension]

@router.post('/files',status_code=201)
def upload(file: UploadFile=File(...),request_id: UUID=Form(...),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    filename=re.split(r'[/\\]',file.filename or '')[-1]
    if not filename or len(filename)>255 or any(ord(c)<32 for c in filename): raise HTTPException(422,'Некорректное имя файла')
    data=file.file.read(MAX_FILE+1)
    if not data or len(data)>MAX_FILE: raise HTTPException(413,f'Размер файла должен быть от 1 байта до {MAX_FILE//1024//1024} МБ')
    media_type=validate_file(filename,data);digest=hashlib.sha256(data).hexdigest()
    advisory(db,'calendar-upload:'+str(user.id))
    old=db.query(CalendarFile).filter_by(uploaded_by_id=user.id,request_id=str(request_id)).first()
    if old:
        if old.sha256!=digest or old.filename!=filename: raise HTTPException(409,'Ключ загрузки уже использован')
        return file_out(old)
    if db.query(CalendarFile).filter_by(uploaded_by_id=user.id,event_id=None).count()>=30:
        raise HTTPException(409,'Слишком много несохранённых файлов. Удалите ненужные вложения из черновика.')
    item=CalendarFile(id=str(uuid4()),uploaded_by_id=user.id,request_id=str(request_id),filename=filename,size=len(data),sha256=digest,media_type=media_type)
    path=file_path(item.id);path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as stream: stream.write(data)
        db.add(item);db.commit()
    except Exception:
        db.rollback();path.unlink(missing_ok=True);raise
    return file_out(item)

def bind_files(db,user,row,ids):
    items=db.query(CalendarFile).filter(CalendarFile.id.in_(ids)).order_by(CalendarFile.id).populate_existing().with_for_update().all()
    if len(items)!=len(ids): raise HTTPException(422,'Вложение не найдено')
    for item in items:
        if item.event_id is None:
            if item.uploaded_by_id!=user.id: raise HTTPException(403,'Нельзя присоединить чужой черновик файла')
        elif item.event_id!=row.id: raise HTTPException(403,'Файл принадлежит другой записи')
        item.event_id=row.id
    by_id={item.id:item for item in items}
    return [file_out(by_id[id]) for id in ids]

@router.get('/files/drafts')
def drafts(db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return [file_out(f) for f in db.query(CalendarFile).filter_by(uploaded_by_id=user.id,event_id=None).order_by(CalendarFile.id)]

@router.get('/files/{id}')
def download(id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    from .calendar import Event,visible
    item=db.get(CalendarFile,str(id))
    if not item: raise HTTPException(404,'Файл не найден')
    if item.event_id is None:
        allowed=item.uploaded_by_id==user.id
    else:
        row=db.get(Event,item.event_id)
        allowed=row and not row.data.get('deleted') and visible(row,user) and item.id in row.data.get('attachment_ids',[])
    if not allowed: raise HTTPException(404,'Файл не найден')
    path=file_path(item.id)
    if not path.is_file() or path.stat().st_size!=item.size or hashlib.sha256(path.read_bytes()).hexdigest()!=item.sha256:
        raise HTTPException(409,'Файл недоступен или контрольная сумма не совпадает')
    return FileResponse(path,filename=item.filename,media_type=item.media_type,headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox"})

@router.delete('/files/{id}',status_code=204)
def discard_draft(id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    item=db.query(CalendarFile).filter_by(id=str(id)).with_for_update().first()
    if not item: return
    if item.uploaded_by_id!=user.id or item.event_id is not None: raise HTTPException(403,'Прикреплённые файлы удаляются через сохранение записи')
    path=file_path(item.id);db.delete(item);db.commit();path.unlink(missing_ok=True)
