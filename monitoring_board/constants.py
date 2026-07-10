from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

INTEGRATION_PROVIDER_FUSIONSOLAR = "FusionSolar"
INTEGRATION_PROVIDER_SIGENERGY = "Sigenergy"
INTEGRATION_PROVIDER_OPTIONS = [INTEGRATION_PROVIDER_FUSIONSOLAR, INTEGRATION_PROVIDER_SIGENERGY]
BACKGROUND_JOB_TYPES_PERFORMANCE = (
    "fusionsolar_production_sync",
    "fusionsolar_production_backfill",
    "fusionsolar_inverter_availability_backfill",
    "fusionsolar_month_cycle",
    "performance_reference_recalculation",
)
BACKGROUND_JOB_STALE_RUNNING_MINUTES = 30
DEFAULT_FUSIONSOLAR_SYNC_HOURS = "08:00,14:00"
DEFAULT_FUSIONSOLAR_LOGIN_ENDPOINT = "/thirdData/login"
DEFAULT_FUSIONSOLAR_STATIONS_ENDPOINT = "/thirdData/stations"
DEFAULT_FUSIONSOLAR_REALTIME_ENDPOINT = "/thirdData/getStationRealKpi"
DEFAULT_FUSIONSOLAR_DEVICES_ENDPOINT = "/thirdData/getDevList"
DEFAULT_FUSIONSOLAR_DEVICE_REALTIME_ENDPOINT = "/thirdData/getDevRealKpi"
DEFAULT_FUSIONSOLAR_DEVICE_HISTORY_ENDPOINT = "/thirdData/getDevHistoryKpi"
DEFAULT_FUSIONSOLAR_ALARMS_ENDPOINT = "/thirdData/getAlarmList"
DEFAULT_FUSIONSOLAR_DAY_KPI_ENDPOINT = "/thirdData/getKpiStationDay"
DEFAULT_FUSIONSOLAR_MONTH_KPI_ENDPOINT = "/thirdData/getKpiStationMonth"
DEFAULT_FUSIONSOLAR_ALARMS_LANGUAGE = "en_US"
DEFAULT_SIGENERGY_BASE_URL = "https://api-eu.sigencloud.com"
DEFAULT_SIGENERGY_AUTH_ENDPOINT = "/openapi/auth/login/key"
DEFAULT_SIGENERGY_SYSTEMS_ENDPOINT = "/openapi/system"
DEFAULT_SIGENERGY_ENERGY_FLOW_ENDPOINT = "/openapi/systems/{system_id}/energyFlow"
DEFAULT_SIGENERGY_ONBOARD_ENDPOINT = "/openapi/board/onboard"
DEFAULT_SIGENERGY_REGION = "eu"
DEFAULT_SIGENERGY_SNAPSHOT_RETENTION_DAYS = 90
FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_MINUTES = 60
FUSIONSOLAR_PERFORMANCE_KPI_DELAY_SECONDS = 65
FUSIONSOLAR_PERFORMANCE_MAX_API_CALLS = 20
FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL: datetime | None = None
DEFAULT_DEVICE_COMMUNICATION_THRESHOLD_MINUTES = 15
FUSIONSOLAR_INVERTER_DEVICE_TYPE_IDS = {1, 38}
INVERTER_AVAILABILITY_SLOT_MINUTES = 15
INVERTER_AVAILABILITY_EDGE_TOLERANCE_MINUTES = 30
LOW_INVERTER_AVAILABILITY_PCT = 90.0
LISBON_TIMEZONE = ZoneInfo("Europe/Lisbon")
DEFAULT_STRING_PRESENT_VOLTAGE_THRESHOLD = 100.0
DEFAULT_STRING_AUTO_LEARN_OBSERVATIONS = 2
STATUS_COLORS = {
    "Erro": "danger",
    "Desconectada": "warning",
    "Resolvido": "success",
    "Operacional": "success",
    "OK": "success",
    "Atenção": "warning",
    "Alerta": "warning",
    "Crítico": "danger",
    "Sem referência": "muted",
    "Sem dados": "muted",
    "Aberto": "danger",
    "Em analise": "warning",
    "Agendado": "accent",
    "Em visita": "accent",
    "Fechado": "muted",
}

TICKET_STATUSES = ["Aberto", "Em analise", "Agendado", "Em visita", "Resolvido", "Fechado"]
TICKET_URGENCIES = ["Baixa", "Media", "Alta", "Critica"]
TICKET_MATERIAL_STATUSES = ["Nao definido", "Sem material", "Necessario", "Pronto", "Bloqueado"]
TICKET_WORK_TYPES = ["Diagnostico", "Comunicacao", "Inversor", "String", "Estrutura", "Limpeza", "Preventiva", "Outro"]
MONTH_NAMES_PT = [
    "",
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]
MONITORING_SOURCES = ["FusionSolar", "Sigenergy", "Manual / Outro"]
ASSET_MONITORING_STATUSES = ["active", "silenced", "maintenance", "out_of_scope", "disabled"]
OK_MONITORING_STATUSES = {"Operacional", "Resolvido", "OK"}
PROBLEM_MONITORING_STATUSES = {"Erro", "Desconectada"}
ALERT_SCOPE_OPTIONS = ["all_assets", "only_o&m", "only_active_contracts", "only_selected_assets"]
ALERT_SETTING_DEFAULTS = {
    "TELEGRAM_ALERTS_ENABLED": "true",
    "ALERT_SCOPE": "only_o&m",
    "SEND_NEW_ERROR_ALERTS": "true",
    "SEND_OFFLINE_ALERTS": "true",
    "SEND_RESOLVED_ALERTS": "true",
    "SEND_PERSISTENT_ALERTS": "true",
    "SEND_RECURRENT_ALERTS": "false",
    "DAYTIME_OFFLINE_ONLY": "true",
    "IGNORE_HISTORICAL_ALERTS": "true",
    "MINIMUM_ALERT_SEVERITY": "info",
    "NEW_ERROR_COOLDOWN_MINUTES": "0",
    "OFFLINE_COOLDOWN_MINUTES": "120",
    "RESOLVED_COOLDOWN_MINUTES": "0",
    "PERSISTENT_COOLDOWN_HOURS": "24",
    "RECURRENT_COOLDOWN_HOURS": "24",
    "ALERT_BASELINE_AT": "",
}
RENEWAL_STATUSES = ["Por contactar", "Email enviado", "Em negociacao", "Renovado", "Sem interesse"]
INTEGRATION_STATUS_COLORS = {
    "success": "success",
    "error": "danger",
    "warning": "warning",
    "pending": "accent",
}
EXPORT_DATASETS = {
    "assets": {
        "label": "Instalacoes / centrais",
        "columns": [
            ("project_name", "Central"),
            ("location", "Localizacao"),
            ("address", "Morada"),
            ("contact_phone", "Contacto"),
            ("contact_name", "Nome"),
            ("access_type", "Acesso"),
            ("coverage_type", "Tipo de cobertura"),
            ("contract_type", "Contrato"),
            ("active_contract", "O&M"),
            ("company_name", "Empresa"),
            ("contact_email", "Email"),
        ],
    },
    "monitoring": {
        "label": "Monitorizacao filtrada",
        "columns": [
            ("record_date", "Data"),
            ("imported_at", "Importado em"),
            ("project_name", "Central"),
            ("location", "Localizacao"),
            ("contract_type", "Contrato"),
            ("active_contract", "O&M"),
            ("status", "Estado"),
            ("notes", "Notas"),
            ("source", "Origem"),
        ],
    },
    "tickets": {
        "label": "Intervencoes O&M",
        "columns": [
            ("project_name", "Central"),
            ("location", "Localizacao"),
            ("contract_type", "Contrato"),
            ("active_contract", "O&M"),
            ("title", "Titulo"),
            ("status", "Estado"),
            ("urgency", "Urgencia"),
            ("installation_ref", "Referencia"),
            ("next_action", "Proxima acao"),
            ("work_type", "Tipo de trabalho"),
            ("material_status", "Material"),
            ("planned_date", "Data planeada"),
            ("due_date", "Data limite"),
            ("estimated_minutes", "Minutos previstos"),
            ("assigned_to", "Equipa"),
            ("planning_notes", "Notas planeamento"),
            ("notes", "Notas"),
            ("created_at", "Criado em"),
            ("updated_at", "Atualizado em"),
        ],
    },
    "executive_report": {
        "label": "Relatorio executivo O&M",
        "columns": [
            ("section", "Seccao"),
            ("priority", "Prioridade"),
            ("project_name", "Central"),
            ("status", "Estado"),
            ("problem_days", "Dias em problema"),
            ("recurrence_count", "Recorrencias 90d"),
            ("open_tickets", "Tickets abertos"),
            ("source", "Origem"),
            ("notes", "Notas"),
        ],
    },
    "monitoring_report": {
        "label": "Relatorio limpo de monitorizacao",
        "columns": [
            ("period", "Periodo"),
            ("project_name", "Instalacao"),
            ("location", "Localizacao"),
            ("current_status", "Estado atual"),
            ("last_record_date", "Ultima monitorizacao"),
            ("monitoring_records", "Registos no periodo"),
            ("error_records", "Erros no periodo"),
            ("distinct_errors", "Erros diferentes"),
            ("error_types", "Tipos de erro"),
            ("open_tickets", "Tickets abertos"),
            ("visits_period", "Visitas no periodo"),
            ("last_visit_date", "Ultima visita"),
            ("latest_notes", "Notas"),
        ],
    },
    "production_report": {
        "label": "Relatorio de producao mensal/anual",
        "columns": [
            ("period", "Periodo"),
            ("project_name", "Instalacao"),
            ("location", "Localizacao"),
            ("provider", "Origem API"),
            ("production_kwh", "Producao kWh"),
            ("specific_yield", "kWh/kWp"),
            ("expected_kwh", "Producao esperada kWh"),
            ("deviation_pct", "Desvio %"),
            ("performance_status", "Estado performance"),
            ("data_points", "Pontos de dados"),
            ("data_source", "Tipo de dados"),
            ("last_update", "Ultima atualizacao"),
            ("notes", "Notas"),
        ],
    },
}

GROUP_INHERITED_FIELDS = [
    "company_name",
    "location",
    "address",
    "contract_type",
    "contact_name",
    "contact_role",
    "contact_email",
    "contact_phone",
    "access_type",
    "coverage_type",
]
