import sqlite3

from monitoring_board.services.financial_models import financial_billing_review_defaults


def test_financial_billing_defaults_use_explicit_model_fields() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE model (id INTEGER, base_year INTEGER, details_json TEXT)")
    conn.execute("INSERT INTO model VALUES (1, 2026, ?)", ('{"upac_summary":[{"key":"avoided_tariff_eur_kwh","value":0.18},{"key":"surplus_sale_eur_kwh","value":0.04},{"key":"ppa_tariff_eur_kwh","value":0.10}],"tariff_periods":[{"key":"cheia","value":0.2}]}',))
    values = financial_billing_review_defaults(conn.execute("SELECT * FROM model").fetchone())
    assert values["electricity_price"] == 0.18
    assert values["sell_price"] == 0.04
    assert values["solcor_price_per_kwh"] == 0.10
    assert values["tariff_prices"] == {"cheia": 0.2}
