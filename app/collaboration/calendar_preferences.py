"""Personal calendar preferences, isolated from dashboard preferences."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy.orm import Session
from app.accounting.evidence_schemas import Strict
from app.core.deps import get_current_user
from app.db.session import get_db
from app.users.models import User

router = APIRouter()

class Category(Strict):
    label: str = Field(min_length=1, max_length=40)
    color: str = Field(pattern=r'^#[0-9a-fA-F]{6}$')

class Template(Strict):
    id: UUID
    title: str = Field(min_length=1,max_length=200)
    description: str = Field(default='',max_length=10000)
    location: str = Field(default='',max_length=1000)
    category: Literal['meeting','work','personal','anniversary'] = 'meeting'
    duration_minutes: int = Field(default=60,ge=1,le=1440)
    reminder_minutes: Literal[0,5,15,30,60,1440] | None = None
    recurrence: Literal['none','daily','weekly','monthly'] = 'none'
    count: int = Field(default=12,ge=1,le=366)

class Preferences(Strict):
    version: int = Field(default=0, ge=0)
    templates: list[Template] = Field(default_factory=list,max_length=20)
    timezone: str = 'Europe/Moscow'
    work_start: int = Field(default=480, ge=0, lt=1440)
    work_end: int = Field(default=1140, gt=0, lt=1440)
    weekdays: list[int] = Field(default_factory=lambda: [0,1,2,3,4], min_length=1, max_length=7)
    categories: dict[str, Category] = Field(default_factory=lambda: {
        'meeting': Category(label='Встречи', color='#4776d0'),
        'work': Category(label='Задачи', color='#138b78'),
        'personal': Category(label='Личное', color='#8854bf'),
        'anniversary': Category(label='Юбилеи', color='#b06b0d')})

    @model_validator(mode='after')
    def valid(self):
        try: ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('Неизвестный часовой пояс')
        if self.work_end <= self.work_start: raise ValueError('Окончание рабочего дня должно быть позже начала')
        if len(set(self.weekdays)) != len(self.weekdays) or any(d not in range(7) for d in self.weekdays):
            raise ValueError('Выберите неповторяющиеся дни недели от 0 до 6')
        if set(self.categories) != {'meeting','work','personal','anniversary'}:
            raise ValueError('Сохраните все четыре категории')
        if any(not c.label.strip() for c in self.categories.values()): raise ValueError('Введите название категории')
        if len({t.id for t in self.templates})!=len(self.templates) or any(not t.title.strip() for t in self.templates): raise ValueError('Шаблоны должны иметь разные идентификаторы и заполненную тему')
        return self

@router.get('/preferences', response_model=Preferences)
def get_preferences(user: User=Depends(get_current_user)):
    result=Preferences.model_validate((user.workspace_preferences or {}).get('calendar', {}))
    if result.categories['work'].label=='Работа': result.categories['work'].label='Задачи'
    return result

@router.put('/preferences', response_model=Preferences)
def put_preferences(body: Preferences, db: Session=Depends(get_db), user: User=Depends(get_current_user)):
    owner = db.query(User).filter_by(id=user.id).populate_existing().with_for_update().one()
    current = get_preferences(owner)
    if body.version != current.version:
        raise HTTPException(409, 'Настройки изменены в другой вкладке. Обновите страницу.')
    saved = body.model_copy(update={'version': current.version+1}).model_dump(mode='json')
    if 'templates' not in body.model_fields_set: saved['templates']=current.model_dump(mode='json')['templates']
    owner.workspace_preferences = {**(owner.workspace_preferences or {}), 'calendar': saved}
    db.commit()
    return saved
