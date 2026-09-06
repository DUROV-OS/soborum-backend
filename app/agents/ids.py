from enum import StrEnum


class AgentId(StrEnum):
    COORDINATOR = "coordinator"
    SALES = "sales"
    MARKETER = "marketer"
    PRODUCTION = "production"
    WAREHOUSE = "warehouse"
    FINANCE = "finance"
    LAWYER = "lawyer"
    ENGINEER = "engineer"


RU_LABELS: dict[AgentId, str] = {
    AgentId.COORDINATOR: "координатор",
    AgentId.SALES: "продажник",
    AgentId.MARKETER: "маркетолог",
    AgentId.PRODUCTION: "производственник",
    AgentId.WAREHOUSE: "кладовщик",
    AgentId.FINANCE: "финансист",
    AgentId.LAWYER: "юрист",
    AgentId.ENGINEER: "инженер",
}

VAULT_PATHS: dict[AgentId, tuple[str, ...]] = {
    AgentId.COORDINATOR: ("00_Agent", "02_Business/00_Decision_Log", "02_Business/10_Strategy_and_Roadmap"),
    AgentId.SALES: ("02_Business/04_Sales_and_Clients", "03_Clients", "02_Business/05_Finance_and_Unit_Economics"),
    AgentId.MARKETER: ("02_Business/02_Positioning_and_Brand", "02_Business/09_Marketing_and_Leadgen"),
    AgentId.PRODUCTION: ("02_Business/01_Production",),
    AgentId.WAREHOUSE: ("02_Business/07_Logistics_and_Supply_Chain", "02_Business/01_Production"),
    AgentId.FINANCE: ("02_Business/05_Finance_and_Unit_Economics",),
    AgentId.LAWYER: ("00_Agent", "02_Business/06_Legal_and_Compliance"),
    AgentId.ENGINEER: ("02_Business/01_Production",),
}

DOES_NOT_OWN: dict[AgentId, str] = {
    AgentId.SALES: "окончательная цена и скидка сверх 5%",
    AgentId.MARKETER: "ворованные данные конкурентов и публикация без человека",
    AgentId.PRODUCTION: "обещание даты клиенту",
    AgentId.WAREHOUSE: "оплата поставщику",
    AgentId.FINANCE: "банк, инвестиция, окончательная цена клиенту",
    AgentId.LAWYER: "коммерческая рекомендация и подпись на договоре",
    AgentId.ENGINEER: "запуск в производство и выдача нестандарта как типового",
    AgentId.COORDINATOR: "доменные решения продаж, цеха, склада, права, инженерии",
}
