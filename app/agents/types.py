from enum import StrEnum

from pydantic import BaseModel, Field

from app.agents.ids import AgentId


class LegalVerdict(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_CONDITIONS = "allow_with_conditions"
    BLOCK = "block"
    ESCALATE_HUMAN = "escalate_human"


class LegalCategory(StrEnum):
    NONE = "none"
    COMPETITOR_INTEL_ILLEGAL = "competitor_intel_illegal"
    CONTRACT = "contract"
    PERSONAL_DATA = "personal_data"
    PRICING_AUTHORITY = "pricing_authority"
    OWNER_PROMISE = "owner_promise"
    LABOR = "labor"
    SAFETY_CODE = "safety_code"
    INTELLECTUAL_PROPERTY = "intellectual_property"


class LegalFinding(BaseModel):
    category: LegalCategory
    verdict: LegalVerdict
    rule_id: str
    reason: str
    matched: str
    human_line: str


class LegalDecision(BaseModel):
    verdict: LegalVerdict
    findings: list[LegalFinding] = Field(default_factory=list)
    passport: str

    @property
    def blocked(self) -> bool:
        return self.verdict in {LegalVerdict.BLOCK, LegalVerdict.ESCALATE_HUMAN}

    @property
    def needs_lawyer(self) -> bool:
        return self.verdict != LegalVerdict.ALLOW or any(
            f.category != LegalCategory.NONE for f in self.findings
        )


class Route(BaseModel):
    specialists: list[AgentId]
    scores: dict[AgentId, int]
    reason: str


class Opinion(BaseModel):
    agent: AgentId
    stance: str
    citations: list[str] = Field(default_factory=list)


class ContextHit(BaseModel):
    source: str
    title: str
    excerpt: str
    kind: str = "fact"
    path: str | None = None


class SharedContext(BaseModel):
    hits: list[ContextHit] = Field(default_factory=list)
    policy: dict[str, str] = Field(default_factory=dict)


class RunResult(BaseModel):
    reply: str
    route: Route
    legal: LegalDecision
    opinions: list[Opinion] = Field(default_factory=list)
    context: SharedContext = Field(default_factory=SharedContext)
    released: bool
    trace_id: str
