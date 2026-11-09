from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from app.crm.sales_fields import CustomerProfile, CustomerNeeds, PurchaseTerms, SaleTerms, AgencyProfile, AgencyReferral


class ContactData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field("", max_length=100)
    email: str = Field("", max_length=255)
    company_id: int | None = None
    responsible_user_id: int | None = None
    custom_fields: list = Field(default_factory=list, max_length=100)
    is_unsorted: bool = False
    customer: CustomerProfile | None = None
    referral: AgencyReferral | None = None


class DealData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline_id: int
    stage_id: int
    amount: float = Field(0, ge=0, le=1e12, allow_inf_nan=False)
    currency: str = Field("RUB", pattern="^[A-Z]{3}$")
    contact_ids: list[int] = Field(default_factory=list, max_length=100)
    company_id: int | None = None
    responsible_user_id: int | None = None
    loss_reason: str = Field("", max_length=2000)
    custom_fields: list = Field(default_factory=list, max_length=100)
    is_unsorted: bool = False
    client_id: int | None = Field(None, ge=1)
    client_request_id: int | None = Field(None, ge=1)
    primary_contact_id: int | None = Field(None, ge=1)
    inquiry_customer_snapshot: CustomerProfile | None = None
    needs: CustomerNeeds | None = None
    purchase_terms: PurchaseTerms | None = None
    sale_terms: SaleTerms | None = None
    agency_profile: AgencyProfile | None = None
    referral: AgencyReferral | None = None


class InquiryData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_spam: bool = False
    pipeline_id: int | None = None
    source_name: str = Field("", max_length=1000)
    contact_name: str = Field("", max_length=1000)
    phone: str = Field("", max_length=100)
    email: str = Field("", max_length=255)
    deal_id: int | None = None
    accepted_deal_id: int | None = None
    reason: str = Field("", max_length=2000)
    client_id: int | None = Field(None, ge=1)
    client_request_id: int | None = Field(None, ge=1)
    customer: CustomerProfile | None = None
    referral: AgencyReferral | None = None


class TaskData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: int | None = None
    responsible_user_id: int | None = None
    due_at: datetime | None = None
    completed: bool = False
    result: str = Field("", max_length=5000)
    description: str = Field('', max_length=20000)
    task_type: str = Field("Задача", max_length=100)


class PipelineData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sort: int = 0
    purpose: Literal['sales', 'agency'] | None = None


class StageData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline_id: int
    sort: int = 0
    color: str = Field("#dbeafe", pattern="^#[0-9A-Fa-f]{6}$")
    outcome: Literal["open", "won", "lost"] = "open"
    agency_source_eligible: bool = False


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=1000)
    archived: bool = False
    data: dict = Field(default_factory=dict)


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    name: str | None = Field(None, min_length=1, max_length=1000)
    archived: bool | None = None
    data: dict | None = None


class Note(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
