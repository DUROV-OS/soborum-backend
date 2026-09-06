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
