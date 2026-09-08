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

DAILY_QUESTIONS: dict[AgentId, str] = {
    AgentId.COORDINATOR: "Что сейчас самое важное для компании и какой специалист это закрывает?",
    AgentId.SALES: "Какие сделки зависли и что мешает закрыть ближайшую оплату?",
    AgentId.MARKETER: "Какой следующий контакт с рынком опирается на проверенный факт, а не на домысел?",
    AgentId.PRODUCTION: "Что сегодня тормозит ближайший дом и какой материал или этап в узком месте?",
    AgentId.WAREHOUSE: "Чего не хватит ближайшему дому и что уже едет?",
    AgentId.FINANCE: "Где сейчас утекает маржа — в скидке, сроке, комплектации или неоплате?",
    AgentId.LAWYER: "Какое сегодняшнее действие создаёт юридический риск, если его выпустить как есть?",
    AgentId.ENGINEER: "Какое сегодняшнее отклонение от техкарты создаёт риск на площадке?",
}

# Who must read whose draft. Lawyer sees everyone.
CROSS_REVIEWERS: dict[AgentId, tuple[AgentId, ...]] = {
    AgentId.COORDINATOR: (AgentId.LAWYER,),
    AgentId.SALES: (AgentId.FINANCE, AgentId.PRODUCTION, AgentId.LAWYER),
    AgentId.MARKETER: (AgentId.LAWYER, AgentId.SALES),
    AgentId.PRODUCTION: (AgentId.WAREHOUSE, AgentId.ENGINEER, AgentId.LAWYER),
    AgentId.WAREHOUSE: (AgentId.PRODUCTION, AgentId.FINANCE),
    AgentId.FINANCE: (AgentId.SALES, AgentId.LAWYER),
    AgentId.LAWYER: (AgentId.COORDINATOR,),
    AgentId.ENGINEER: (AgentId.PRODUCTION, AgentId.LAWYER),
}

WATCHES: dict[AgentId, str] = {
    AgentId.COORDINATOR: "Сводит картину: что сейчас главное и кого будить",
    AgentId.SALES: "Смотрит, какие сделки зависли и что мешает оплате",
    AgentId.MARKETER: "Ищет следующий контакт с рынком только на проверенном факте",
    AgentId.PRODUCTION: "Смотрит, что тормозит ближайший дом в цехе",
    AgentId.WAREHOUSE: "Смотрит, чего не хватит ближайшему дому и что уже едет",
    AgentId.FINANCE: "Ищет, где утекает маржа — скидка, срок, комплектация, неоплата",
    AgentId.LAWYER: "Проверяет, можно ли выпускать сегодняшние действия без человека",
    AgentId.ENGINEER: "Сверяет отклонение от техкарты с риском на площадке",
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
