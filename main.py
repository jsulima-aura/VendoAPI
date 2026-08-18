import csv
import os
import secrets
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


app = FastAPI(title="Vendo CSV Export API")
bearer_scheme = HTTPBearer(auto_error=False)

EXPORT_QUERY = """
SELECT id, nazwa, data_utworzenia, status
FROM public.zamowienia
WHERE data_utworzenia >= %s AND data_utworzenia < %s
ORDER BY data_utworzenia;
"""
CSV_HEADERS = ("id", "nazwa", "data_utworzenia", "status")


def require_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    expected_token = os.environ.get("API_TOKEN", "")
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not expected_token
        or not secrets.compare_digest(credentials.credentials, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nieprawidlowy lub brakujacy token API",
            headers={"WWW-Authenticate": "Bearer"},
        )


def previous_full_week(now: datetime | None = None) -> tuple[datetime, datetime]:
    timezone_name = os.environ.get("TIMEZONE", "Europe/Warsaw")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Nieznana strefa czasowa: {timezone_name}") from exc

    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    current_monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    previous_monday = current_monday - timedelta(days=7)
    return previous_monday, current_monday


def database_connection() -> psycopg.Connection:
    required_variables = (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    )
    missing_variables = [name for name in required_variables if not os.environ.get(name)]
    if missing_variables:
        raise RuntimeError(
            f"Brak wymaganych zmiennych srodowiskowych: {', '.join(missing_variables)}"
        )

    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/export/weekly", dependencies=[Depends(require_api_token)])
def export_weekly() -> Response:
    start_date, end_date = previous_full_week()

    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(EXPORT_QUERY, (start_date, end_date))
            rows = cursor.fetchall()

    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    writer.writerows(rows)

    filename = f"eksport_{start_date.date()}_{end_date.date()}.csv"
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )