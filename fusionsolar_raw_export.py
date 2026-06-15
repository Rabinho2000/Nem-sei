import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"

OUT_DIR = PROJECT_DIR / "diagnostics"
OUT_DIR.mkdir(exist_ok=True)

RAW_JSON_PATH = OUT_DIR / "fusionsolar_raw_stations.json"
RAW_CSV_PATH = OUT_DIR / "fusionsolar_raw_stations.csv"
SUMMARY_PATH = OUT_DIR / "fusionsolar_raw_summary.txt"
EXPECTED_NAMES_PATH = PROJECT_DIR / "expected_fusionsolar_names.txt"
MISSING_EXPECTED_PATH = OUT_DIR / "missing_expected_stations.txt"


def load_dotenv_simple(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def build_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def normalize_name(value: str) -> str:
    import unicodedata
    import re

    value = value or ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"\s+", " ", value.strip().lower())
    return value


def get_station_name(row: dict[str, Any]) -> str:
    for key in [
        "stationName",
        "plantName",
        "name",
        "station_name",
        "plant_name",
        "dnName",
    ]:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def get_station_code(row: dict[str, Any]) -> str:
    for key in [
        "stationCode",
        "plantCode",
        "station_code",
        "plant_code",
        "id",
        "dn",
    ]:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def login() -> requests.Session:
    username = os.environ.get("FUSIONSOLAR_USERNAME", "").strip()
    password = os.environ.get("FUSIONSOLAR_PASSWORD", "").strip()
    base_url = os.environ.get("FUSIONSOLAR_BASE_URL", "").strip()
    login_endpoint = os.environ.get("FUSIONSOLAR_LOGIN_ENDPOINT", "/thirdData/login").strip()

    if not username or not password or not base_url:
        raise RuntimeError(
            "Faltam variáveis no .env: FUSIONSOLAR_USERNAME, "
            "FUSIONSOLAR_PASSWORD e/ou FUSIONSOLAR_BASE_URL."
        )

    session = requests.Session()
    login_url = build_url(base_url, login_endpoint)

    response = session.post(
        login_url,
        json={"userName": username, "systemCode": password},
        headers={"Content-Type": "application/json", "Accept": "application/json, */*"},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()

    if payload.get("success") is not True or int(payload.get("failCode") or 0) != 0:
        raise RuntimeError(
            f"Login FusionSolar falhou: {payload.get('message')} "
            f"(failCode={payload.get('failCode')})"
        )

    xsrf_token = None

    for key, value in response.headers.items():
        if key.lower() == "xsrf-token" and value:
            xsrf_token = value.strip()
            break

    if not xsrf_token:
        xsrf_token = session.cookies.get("XSRF-TOKEN") or session.cookies.get("xsrf-token")

    if not xsrf_token:
        raise RuntimeError("Login OK, mas não veio XSRF-TOKEN no header/cookies.")

    session.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json, */*",
            "XSRF-TOKEN": str(xsrf_token),
        }
    )

    return session


def post_fusionsolar(session: requests.Session, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = session.post(url, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()

    if data.get("success") is not True or int(data.get("failCode") or 0) != 0:
        raise RuntimeError(
            f"API FusionSolar falhou: {data.get('message')} "
            f"(failCode={data.get('failCode')})"
        )

    return data


def fetch_all_stations(session: requests.Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_url = os.environ.get("FUSIONSOLAR_BASE_URL", "").strip()
    stations_endpoint = os.environ.get("FUSIONSOLAR_STATIONS_ENDPOINT", "/thirdData/stations").strip()

    url = build_url(base_url, stations_endpoint)

    all_rows: list[dict[str, Any]] = []
    page_debug: list[dict[str, Any]] = []

    page_no = 1
    page_count = 1

    while page_no <= page_count:
        payload_sent = {
            "pageNo": page_no,
        }

        data = post_fusionsolar(session, url, payload_sent)
        page_data = data.get("data") or {}

        rows = page_data.get("list") or []
        if not isinstance(rows, list):
            raise RuntimeError("Resposta inesperada: data.list não é lista.")

        page_count = int(page_data.get("pageCount") or 1)
        page_size = page_data.get("pageSize")
        total = page_data.get("total")

        page_debug.append(
            {
                "pageNo": page_no,
                "pageCount": page_count,
                "pageSize": page_size,
                "total": total,
                "rows_on_page": len(rows),
            }
        )

        all_rows.extend([row for row in rows if isinstance(row, dict)])
        page_no += 1

    return all_rows, page_debug


def write_exports(rows: list[dict[str, Any]], page_debug: list[dict[str, Any]]) -> None:
    RAW_JSON_PATH.write_text(
        json.dumps(
            {
                "count": len(rows),
                "page_debug": page_debug,
                "stations": rows,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    fieldnames = [
        "station_name",
        "station_code",
        "capacity",
        "grid_connection_date",
        "raw_keys",
    ]

    with RAW_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    "station_name": get_station_name(row),
                    "station_code": get_station_code(row),
                    "capacity": row.get("capacity") or row.get("installedCapacity") or row.get("installCapacity") or "",
                    "grid_connection_date": row.get("gridConnectionDate") or row.get("connectDate") or "",
                    "raw_keys": ", ".join(sorted(row.keys())),
                }
            )


def compare_expected(rows: list[dict[str, Any]]) -> list[str]:
    if not EXPECTED_NAMES_PATH.exists():
        return []

    expected_names = [
        line.strip()
        for line in EXPECTED_NAMES_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    ]

    returned_names = {normalize_name(get_station_name(row)) for row in rows}
    missing = [
        name
        for name in expected_names
        if normalize_name(name) not in returned_names
    ]

    MISSING_EXPECTED_PATH.write_text(
        "\n".join(missing),
        encoding="utf-8",
    )

    return missing


def main() -> int:
    load_dotenv_simple(ENV_FILE)

    print("A fazer login FusionSolar...")
    session = login()

    print("A obter lista raw de centrais da API...")
    rows, page_debug = fetch_all_stations(session)

    write_exports(rows, page_debug)
    missing = compare_expected(rows)

    names = [get_station_name(row) for row in rows if get_station_name(row)]

    summary_lines = [
        "FusionSolar raw station export",
        "=============================",
        f"Total recebido pela API: {len(rows)}",
        "",
        "Paginação:",
        *[
            f"- pageNo={p['pageNo']} pageCount={p['pageCount']} "
            f"pageSize={p['pageSize']} total={p['total']} rows={p['rows_on_page']}"
            for p in page_debug
        ],
        "",
        "Primeiras 10 centrais:",
        *[f"- {name}" for name in names[:10]],
        "",
        "Últimas 10 centrais:",
        *[f"- {name}" for name in names[-10:]],
        "",
        f"JSON: {RAW_JSON_PATH}",
        f"CSV: {RAW_CSV_PATH}",
    ]

    if EXPECTED_NAMES_PATH.exists():
        summary_lines.extend(
            [
                "",
                f"Lista esperada: {EXPECTED_NAMES_PATH}",
                f"Total em falta face à lista esperada: {len(missing)}",
                f"Ficheiro de faltas: {MISSING_EXPECTED_PATH}",
            ]
        )

    SUMMARY_PATH.write_text("\n".join(summary_lines), encoding="utf-8")

    print()
    print("\n".join(summary_lines))
    print()
    print("Feito.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(1)