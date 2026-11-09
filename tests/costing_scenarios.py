"""Explicit synthetic approval setup shared by arithmetic and integration tests."""
from uuid import uuid4
from app.common.module_access import Module
from app.costing.governance_models import CostingApprovalPolicy
from tests.test_costing_approvals import G, P, review, upload, production, accounting


class ApprovalScenario:
    def __init__(self,db,api,make_user,owner):
        self.db,self.api,self.owner=db,api,owner
        operator=make_user(Module.PRODUCTION,Module.CLIENTS)
        accountant=make_user(admin=True)
        db.add(CostingApprovalPolicy(id=1,production_user_id=operator.id,accounting_user_id=accountant.id,director_user_id=owner.id));db.commit()
        self.f={'db':db,'api':api,'owner':owner,'production':operator,'accountant':accountant}
        self.month=None

    def restore(self):
        return self.api(self.owner)

    def item(self,row):
        response=self.api(self.f['production']).post(f'{G}/items/{row["id"]}/production',json=review(row['version'],work_scope='Сборка и проверка тестовой комплектации'))
        assert response.status_code==200,response.text
        self.restore()

    def plan(self):
        if self.month: return self.month
        # Tiny but nonzero documented overhead leaves existing cent-rounding boundary cases meaningful.
        response=self.restore().post(G+'/month-plans',json={'month':'2026-11-01','planned_units':1000000,
            'factory':'0.01','warehouse':'0','staff':'0.01','other':'0','note':'Синтетические расходы для проверки арифметики'})
        assert response.status_code==201,response.text
        plan=response.json();doc=upload(self.f,'month',plan['id'])
        evidence=[{'key':f['key'],'value':f['value'],'document_id':doc['id'],'locator':'Страница 1, '+f['label']} for f in plan['facts']]
        assert self.api(self.f['accountant']).post(f'{G}/month-plans/{plan["id"]}/accounting',json=review(1,evidence=evidence)).status_code==200
        assert self.restore().post(f'{G}/month-plans/{plan["id"]}/director',json=review(1)).status_code==200
        self.month=plan['id'];return self.month

    def priced(self,body):
        response=self.restore().post(P+'/estimates',json={**body,'overhead_plan_id':self.plan()})
        assert response.status_code==201,response.text
        estimate=response.json()
        production(self.f,estimate)
        return self.restore().get(f'{P}/estimates/{estimate["id"]}').json()

    def approve(self,estimate):
        detail=self.restore().get(f'{G}/estimates/{estimate["id"]}').json()
        accounting(self.f,detail)
        response=self.restore().post(f'{P}/estimates/{estimate["id"]}/approve',json=review(estimate['version']))
        assert response.status_code==200,response.text
        return response.json()


def priced(client,body):
    return client.approvals.priced(body)
