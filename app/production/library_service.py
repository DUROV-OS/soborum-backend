"""Draft BOMs from exact imported recipes, with explicit provenance and gaps.

No sale/customer data is consumed. No write to stock, orders or native recipes.
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from app.moysklad.recipes import Recipe
from app.moysklad.models import SourceRecord
from app.production.library_data import SITE_MODELS, STANDARDS, MODEL_BY_KEY
from app.production.library_models import HouseTechnicalCard


def decimal_value(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and 0 < result <= 10000000 else None


def recipe_identity(recipe, source_paths):
    name = (recipe.title + ' ' + source_paths.get(recipe.source_id, '')).casefold().replace('ё', 'е')
    number = re.search(r'dh[\s_-]*(\d+)', name)
    if not number:
        return None
    series = ('flat' if re.search(r'фл[еэ]т|flat', name) else
              'barn' if re.search(r'барн|barn|dh[\s_-]*\d+\s*[bвб](?!\w)', name) else None)
    return (series, int(number.group(1))) if series else None


def root_recipe(recipe):
    name = recipe.title.casefold().replace('ё', 'е')
    return ('домокомплект' in name or bool(re.fullmatch(
        r'dh[\s_-]*\d+\s*[bвб]?\s*(?:барн|фл[еэ]т)?\s*(?:новый|\(новый\))?\s*', name)))


def build_card(model, recipes, source_paths=None):
    source_paths = source_paths or {}
    active = [r for r in recipes if not r.archived and not str(r.source_id or '').startswith('costing-order:')]
    roots = [r for r in active if root_recipe(r) and r.content.get('inputs')]
    exact = [r for r in roots if recipe_identity(r, source_paths) == (model['series'], model['number'])]
    warnings = ['Черновик. Требуется сверка с АР/КР и утверждённой комплектацией.']
    by_output = defaultdict(list)
    for recipe in active:
        for line in recipe.content.get('outputs', []):
            if line.get('source_id'):
                by_output[line['source_id']].append(recipe)
    # A rich direct BOM is preferred to an empty or kit-only recipe. Alternatives
    # remain explicit; no averaging or summing mutually exclusive BOM versions.
    rank = lambda r: (len(r.content.get('inputs', [])), r.version, r.id)
    basis = max(exact, key=rank) if exact else None
    if len(exact) > 1:
        warnings.append('Есть альтернативные техкарты этой модели. Выбран наиболее заполненный вариант; комплектацию нужно проверить.')
    analogous = False
    if basis is None:
        candidates = [r for r in roots if (recipe_identity(r, source_paths) or (None,))[0] == model['series']]
        if candidates:
            basis = min(candidates, key=lambda r: (abs(recipe_identity(r,source_paths)[1]-model['number']),-rank(r)[0],r.id))
            analogous = True
            warnings.append('Точной техкарты модели нет. Состав и этапы предложены по аналогу; количества для этого дома не назначены.')
        else:
            warnings.append('Исходная техкарта этой серии не найдена.')
    materials = {}
    operations = []
    visited_sources = {}

    def add_line(line, quantity, recipe, path, unresolved=False):
        identity = (line.get('source_id') or str(line.get('material_id') or ''), line.get('unit',''), line.get('title',''))
        key = hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:20]
        entry = materials.setdefault(key, {'key':key,'title':str(line.get('title',''))[:255],
            'unit':str(line.get('unit',''))[:32], 'source_id':line.get('source_id'),
            'material_id':line.get('material_id'),'quantity':Decimal(0),'basis_quantity':Decimal(0),
            'state':'requires_calculation' if analogous else 'source_quantity','sources':[]})
        if unresolved or quantity is None:
            entry['state'] = 'requires_check'
        if quantity is not None:
            entry['basis_quantity'] += quantity
            entry['quantity'] += quantity
        entry['sources'].append({'recipe_id':recipe.id,'version':recipe.version,
                                 'line_path':path,'quantity':str(quantity) if quantity is not None else None})

    def expand(recipe, multiplier, chain):
        if recipe.id in chain or len(chain) >= 20:
            warnings.append('Обнаружена циклическая или слишком глубокая вложенность техкарт.')
            return
        visited_sources[recipe.id] = {'id':recipe.id,'version':recipe.version,'title':recipe.title}
        if recipe.content.get('warnings'):
            warnings.append(f'Исходная техкарта № {recipe.id} содержит замечания; проверьте её до применения.')
        for index, stage in enumerate(recipe.content.get('stages', [])):
            # Explicit allowlist: imported stages contain labourCost and prices.
            title = stage.get('name') or stage.get('processingProcessPosition',{}).get('name')
            if recipe is basis:
                operations.append({'key':f'{recipe.id}:{index}','title':str(title or f'Этап {index+1}')[:255],
                    'source_recipe_id':recipe.id,'source_index':index+1,
                    'hours':str(stage['standardHour']) if decimal_value(stage.get('standardHour')) else None})
        for index, line in enumerate(recipe.content.get('inputs', [])):
            qty = decimal_value(line.get('quantity'))
            qty = decimal_value(qty * multiplier) if qty is not None else None
            path = [*chain,recipe.id,index+1]
            children = by_output.get(line.get('source_id'),[])
            if children:
                child = max(children,key=rank)
                outputs = child.content.get('outputs',[])
                output = next((o for o in outputs if o.get('source_id')==line.get('source_id')),None)
                amount = decimal_value((output or {}).get('quantity'))
                if len(children)>1:
                    warnings.append(f'Несколько рецептур компонента: {line.get("title","")}. Проверьте выбранную версию.')
                if child.id in [*chain,recipe.id] or len(chain) >= 19 or not child.content.get('inputs') or qty is None or amount is None or len(outputs)!=1:
                    add_line(line,qty,recipe,path,True)
                    warnings.append(f'Не раскрыт состав компонента: {line.get("title","")}.')
                else:
                    expand(child,qty/amount,[*chain,recipe.id])
            else:
                add_line(line,qty,recipe,path,qty is None)

    valid_root_output = False
    if basis:
        outputs = basis.content.get('outputs',[])
        amount = decimal_value(outputs[0].get('quantity')) if len(outputs)==1 else None
        valid_root_output = amount is not None
        if amount is None:
            warnings.append('Не определён однозначный выход продукции исходной техкарты; количества требуют проверки.')
        expand(basis, Decimal(1)/(amount or Decimal(1)),[])
    rows=[]
    for row in materials.values():
        if not valid_root_output or not decimal_value(row['quantity']):
            row['state'] = 'requires_check'
        row['basis_quantity']=str(row['basis_quantity'].quantize(Decimal('.000001')).normalize())
        rounded = row['quantity'].quantize(Decimal('.000001'))
        if rounded <= 0:
            row['state'] = 'requires_check'
        row['quantity']=None if analogous or row['state']=='requires_check' else str(rounded.normalize())
        rows.append(row)
    return {'status':'draft','basis':'analogue' if analogous else 'exact' if basis else 'missing',
        'basis_recipe_id':basis.id if basis else None,'basis_title':basis.title if basis else None,
        'materials':rows,'operations':operations,'sources':list(visited_sources.values()),
        'warnings':list(dict.fromkeys(warnings)), 'notes':'', 'project_links':{'AR':'','KR':''},
        'standard_ids':[s['id'] for s in STANDARDS], 'standards_reviewed':False,
        'permitted_for_manufacture':False}


def generate_cards(db, user):
    recipes=db.query(Recipe).all()
    paths={r.source_id:r.payload.get('pathName','') for r in db.query(SourceRecord).filter_by(entity_type='processingplan')}
    result={'created':0,'unchanged':0,'preserved':0}
    for model in SITE_MODELS:
        content=build_card(model,recipes,paths)
        digest=hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        current=db.query(HouseTechnicalCard).filter_by(model_key=model['key']).first()
        if current:
            # Regeneration never overwrites a reviewed or manually changed draft.
            result['unchanged' if current.source_hash==digest else 'preserved']+=1
            continue
        db.add(HouseTechnicalCard(model_key=model['key'],content=content,source_hash=digest,updated_by_id=user.id))
        result['created']+=1
    db.flush()
    return result


def card_out(card):
    return {**MODEL_BY_KEY[card.model_key], 'version':card.version,'content':deepcopy(card.content),
            'updated_at':card.updated_at, 'manually_edited':card.manually_edited}
