from copy import deepcopy
from decimal import Decimal
import csv
import io
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.common.module_access import Module
from app.core.deps import require_view, require_edit
from app.db.session import get_db
from app.users.models import UserRole
from app.production.library_data import SITE_MODELS, MODEL_BY_KEY, STANDARDS, CATALOGUE_CHECKED_AT
from app.production.library_models import HouseTechnicalCard
from app.production.library_service import card_out, generate_cards

router=APIRouter(prefix='/library')
view=require_view(Module.PRODUCTION)
edit=require_edit(Module.PRODUCTION)


def get_card(db,key,lock=False):
    if key not in MODEL_BY_KEY:
        raise HTTPException(404,'Модель не найдена')
    query=db.query(HouseTechnicalCard).filter_by(model_key=key)
    if lock:
        query=query.populate_existing().with_for_update()
    card=query.first()
    if not card:
        raise HTTPException(404,'Черновик техкарты ещё не создан')
    return card


def visible_card(card,user):
    result=card_out(card)
    links=result['content'].get('project_links',{})
    result['content']['project_links']={
        kind: value if isinstance(value,dict) and (user.role==UserRole.ADMIN or value.get('production_safe')) else None
        for kind,value in links.items()
    }
    return result


@router.get('/standards')
def standards(user=Depends(view)):
    return {'checked_at':CATALOGUE_CHECKED_AT,'items':STANDARDS,
            'note':'Справочник официальных источников. Применимость и комплект изменений проверяются для конкретного проекта.'}


@router.get('/models')
def models(db:Session=Depends(get_db),user=Depends(view)):
    existing={r.model_key:r for r in db.query(HouseTechnicalCard)}
    items=[]
    for model in SITE_MODELS:
        card=existing.get(model['key'])
        content=card.content if card else {}
        lines=content.get('materials',[])
        items.append({**model,'version':card.version if card else 0,'status':'draft' if card else 'not_created',
            'basis':content.get('basis','missing'),'material_count':len(lines),
            'quantity_count':sum(line.get('quantity') is not None for line in lines),
            'operation_count':len(content.get('operations',[]))})
    return {'checked_at':CATALOGUE_CHECKED_AT,'items':items}


@router.post('/generate')
def generate(db:Session=Depends(get_db),user=Depends(edit)):
    try:
        result=generate_cards(db,user)
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Техкарты уже создаются. Обновите список.')


@router.get('/models/{key}')
def detail(key:str,db:Session=Depends(get_db),user=Depends(view)):
    return visible_card(get_card(db,key),user)


class QuantityChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    key:str=Field(max_length=20)
    quantity:Decimal|None=Field(None,gt=0,le=10000000,max_digits=17,decimal_places=6)


class CardChanges(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(ge=1)
    quantities:list[QuantityChange]=Field(default_factory=list,max_length=3000)
    notes:str=Field('',max_length=3000)


@router.patch('/models/{key}')
def update(key:str,body:CardChanges,db:Session=Depends(get_db),user=Depends(edit)):
    card=get_card(db,key,True)
    if card.version!=body.version:
        raise HTTPException(409,'Техкарта изменена другим сотрудником. Обновите её перед сохранением.')
    data=deepcopy(card.content)
    lines={line['key']:line for line in data.get('materials',[])}
    if len({r.key for r in body.quantities})!=len(body.quantities) or any(r.key not in lines for r in body.quantities):
        raise HTTPException(422,'Неизвестная или повторяющаяся строка материала')
    for change in body.quantities:
        line=lines[change.key]
        quantity=str(change.quantity) if change.quantity is not None else None
        if quantity!=line.get('quantity'):
            line['quantity']=quantity
            line['state']='manually_entered' if quantity else 'requires_calculation'
    data['notes']=body.notes
    data['status']='draft'
    data['permitted_for_manufacture']=False
    card.content=data;card.version+=1;card.manually_edited=True;card.updated_by_id=user.id
    db.commit()
    return visible_card(card,user)


class ProjectLink(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(ge=1)
    kind:Literal['AR','KR']
    url:HttpUrl|None=None
    revision:str=Field('',max_length=128)
    production_safe:bool=False


@router.put('/models/{key}/project-link')
def project_link(key:str,body:ProjectLink,db:Session=Depends(get_db),user=Depends(edit)):
    # A project may embed customer addresses, signatures and commercial data.
    # Only the owner may release the link to production after reviewing it.
    if user.role!=UserRole.ADMIN:
        raise HTTPException(403,'Ссылки на проект и допуск документа назначает собственник')
    if body.url and body.url.scheme!='https':
        raise HTTPException(422,'Используйте ссылку HTTPS')
    card=get_card(db,key,True)
    if card.version!=body.version:
        raise HTTPException(409,'Техкарта уже изменена. Обновите карточку.')
    data=deepcopy(card.content)
    data.setdefault('project_links',{})[body.kind]={'url':str(body.url),'revision':body.revision,
        'production_safe':body.production_safe} if body.url else None
    card.content=data;card.version+=1;card.manually_edited=True;card.updated_by_id=user.id
    db.commit()
    return visible_card(card,user)


@router.get('/models/{key}/export')
def export_card(key:str,db:Session=Depends(get_db),user=Depends(view)):
    card=get_card(db,key)
    stream=io.StringIO(newline='')
    writer=csv.writer(stream,delimiter=';')
    writer.writerow(['Модель','Версия','Статус','Материал','Единица','Количество для дома','Происхождение','Техкарты-источники'])
    def safe(value):
        value=str(value or '')
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
    for line in card.content.get('materials',[]):
        writer.writerow([MODEL_BY_KEY[key]['title'],card.version,'Черновик',safe(line['title']),safe(line['unit']),
            line.get('quantity') or '',line['state'],','.join(str(s['recipe_id']) for s in line['sources'])])
    return Response(content=('\ufeff'+stream.getvalue()).encode('utf-8'),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="{key}-v{card.version}-draft.csv"'})
