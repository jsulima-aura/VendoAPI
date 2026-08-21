import csv
import os
import secrets
from datetime import date, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


app = FastAPI(title="Vendo CSV Export API")
bearer_scheme = HTTPBearer(auto_error=False)

EXPORT_QUERY = """
WITH cfg AS (
    SELECT
        %s::date AS data_od,
        %s::date AS data_do,
        'SKLEP1'::text AS sklep1_seria,
        'SKLEP2'::text AS sklep2_seria,
        6::numeric AS liczba_miesiecy
),
rodzaje_sprzedazy AS (
    SELECT tr_rodzaj, MIN(trt_skrot) AS trt_skrot
    FROM tg_rodzajtransakcji
    WHERE trt_skrot IN ('FV', 'KFV', 'PR', 'KPR')
    GROUP BY tr_rodzaj
),
dok_sprzedazy AS (
    SELECT
        t.tr_idtrans,
        t.tr_fullnumer,
        COALESCE(t.tr_datasprzedaz, t.tr_data4)::date AS data_sprzedazy,
        COALESCE(t.tr_datawystaw, t.tr_data4)::date AS data_wystawienia,
        TRIM(COALESCE(t.tr_seria, '')) AS seria,
        rs.trt_skrot AS typ_dokumentu,
        t.tr_zamknieta,
        t.k_idklienta,
        COALESCE(k.k_nazwa, t.tr_knazwa, '(brak klienta)') AS klient_nazwa,
        COALESCE(rk.rk_typrodzaju, '(brak segmentu klienta)') AS segment_klienta,
        CASE
            WHEN pr.p_imie IS NULL AND pr.p_nazwisko IS NULL THEN NULL
            ELSE CONCAT_WS(
                '.',
                NULLIF(LEFT(BTRIM(pr.p_imie), 1), ''),
                NULLIF(LEFT(BTRIM(pr.p_nazwisko), 1), '')
            ) || '.'
        END AS sprzedawca_inicjaly
    FROM tg_transakcje t
    JOIN rodzaje_sprzedazy rs ON rs.tr_rodzaj = t.tr_rodzaj
    LEFT JOIN tb_klient k ON k.k_idklienta = t.k_idklienta
    LEFT JOIN ts_rodzajklienta rk ON rk.rk_idrodzajklienta = k.rk_idrodzajklienta
    LEFT JOIN tb_pracownicy pr ON pr.p_idpracownika = t.tr_zaliczonedla
    CROSS JOIN cfg c
    WHERE COALESCE(t.tr_datasprzedaz, t.tr_data4) >= c.data_od
      AND COALESCE(t.tr_datasprzedaz, t.tr_data4) < c.data_do
),
role_map AS (
    SELECT
        p.ttw_idtowaru,
        MAX(CASE WHEN (p.tsk_flaga & 1) = 1 AND (p.tsk_flaga & 2) = 0 THEN 1 ELSE 0 END) AS is_finished,
        MAX(CASE WHEN (p.tsk_flaga & 1) = 0 AND (p.tsk_flaga & 2) = 0 THEN 1 ELSE 0 END) AS is_component
    FROM tg_produkcja p
    GROUP BY p.ttw_idtowaru
),
linia_raw AS (
    SELECT
        d.tr_idtrans,
        d.tr_fullnumer,
        d.data_sprzedazy,
        d.data_wystawienia,
        d.seria,
        d.typ_dokumentu,
        d.k_idklienta,
        d.klient_nazwa,
        d.segment_klienta,
        d.sprzedawca_inicjaly,
        te.ttw_idtowaru,
        tw.ttw_klucz AS produkt_kod,
        tw.ttw_nazwa AS produkt_nazwa,
        tw.ttw_ean AS ean,
        tw.ttw_aktywny,
        COALESCE(tw.ttw_usluga, false) AS ttw_usluga,
        tw.ttw_rtowaru,
        CASE
            WHEN tw.ttw_rtowaru = 16 THEN 'ZESTAW'
            ELSE 'PRODUKT'
        END AS rodzaj_pozycji,
        COALESCE(rm.is_finished, 0) AS is_finished,
        COALESCE(rm.is_component, 0) AS is_component,
        EXISTS (
            SELECT 1
            FROM tg_partie pp
            WHERE pp.ttw_idtowaru = tw.ttw_idtowaru
        ) AS has_partie,
        ROUND(te.tel_iloscf, 4) * mnoznikkorekt(d.tr_zamknieta)::numeric AS ilosc,
        ROUND(
            getwartoscnetto(
                te.tel_ilosc,
                te.tel_cenawal,
                te.tel_cenabwal,
                te.tel_flaga,
                te.tel_stawkavat::numeric
            )::mpq * te.tel_kurswal,
            2
        )::numeric AS linia_netto,
        ROUND(
            getwartoscbrutto(
                te.tel_ilosc,
                te.tel_cenawal,
                te.tel_cenabwal,
                te.tel_flaga,
                te.tel_stawkavat::numeric
            )::mpq * te.tel_kurswal,
            2
        )::numeric AS linia_brutto,
        te.tel_kosztnabycia AS linia_koszt
    FROM dok_sprzedazy d
    JOIN tg_transelem te ON te.tr_idtrans = d.tr_idtrans
    JOIN tg_towary tw ON tw.ttw_idtowaru = te.ttw_idtowaru
    LEFT JOIN role_map rm ON rm.ttw_idtowaru = tw.ttw_idtowaru
),
linia_full AS (
    SELECT lr.*, ROUND(lr.linia_netto - lr.linia_koszt, 2) AS linia_marza
    FROM linia_raw lr
),
linia_our AS (
    SELECT *
    FROM linia_full
    WHERE ttw_rtowaru = 16
       OR (
           ttw_usluga = false
           AND ttw_rtowaru = 1
           AND ttw_aktywny = 1
           AND has_partie
           AND is_finished = 1
           AND is_component = 0
       )
),
dok_produkt AS (
    SELECT
        tr_idtrans,
        tr_fullnumer,
        data_sprzedazy,
        data_wystawienia,
        seria,
        typ_dokumentu,
        ttw_idtowaru,
        produkt_kod,
        produkt_nazwa,
        ean,
        rodzaj_pozycji,
        k_idklienta,
        klient_nazwa,
        segment_klienta,
        sprzedawca_inicjaly,
        SUM(ilosc) AS ilosc_dokumentu,
        SUM(linia_netto) AS netto_dokumentu,
        SUM(linia_brutto) AS brutto_dokumentu,
        SUM(linia_koszt) AS koszt_dokumentu,
        SUM(linia_marza) AS marza_dokumentu
    FROM linia_our
    GROUP BY
        tr_idtrans, tr_fullnumer, data_sprzedazy, data_wystawienia, seria, typ_dokumentu,
        ttw_idtowaru, produkt_kod, produkt_nazwa, ean, rodzaj_pozycji, k_idklienta, klient_nazwa,
        segment_klienta, sprzedawca_inicjaly
),
prod_suma AS (
    SELECT
        dp.ttw_idtowaru,
        dp.produkt_kod,
        dp.produkt_nazwa,
        dp.ean,
        dp.segment_klienta,
        SUM(dp.ilosc_dokumentu) AS ilosc_ogolem,
        SUM(dp.netto_dokumentu) AS netto_ogolem,
        SUM(dp.brutto_dokumentu) AS brutto_ogolem,
        SUM(dp.koszt_dokumentu) AS koszt_ogolem,
        SUM(dp.marza_dokumentu) AS marza_ogolem,
        SUM(CASE WHEN dp.seria = c.sklep1_seria THEN dp.netto_dokumentu ELSE 0 END) AS sklep1_netto,
        SUM(CASE WHEN dp.seria = c.sklep2_seria THEN dp.netto_dokumentu ELSE 0 END) AS sklep2_netto
    FROM dok_produkt dp
    CROSS JOIN cfg c
    GROUP BY dp.ttw_idtowaru, dp.produkt_kod, dp.produkt_nazwa, dp.ean, dp.segment_klienta
),
stan_mag AS (
    SELECT
        p.ttw_idtowaru,
        SUM(s.ptm_stanmag) AS stan_ogolny
    FROM tg_partie p
    JOIN tg_partietm s ON s.prt_idpartii = p.prt_idpartii
    GROUP BY p.ttw_idtowaru
)
SELECT
    dp.rodzaj_pozycji AS "Rodzaj pozycji",
    ps.produkt_kod AS "Kod",
    ps.produkt_nazwa AS "Nazwa",
    ps.ean AS "EAN",
    ps.segment_klienta AS "Segment klienta",
    ROUND(ps.brutto_ogolem, 2) AS "Brutto (*)",
    ROUND(ps.netto_ogolem, 2) AS "Sprzedaz netto [PLN]",
    ROUND(ps.marza_ogolem, 2) AS "Marza [PLN]",
    ROUND(
        CASE
            WHEN ps.netto_ogolem = 0 THEN NULL
            ELSE ps.marza_ogolem * 100.0 / ps.netto_ogolem
        END, 2
    ) AS "%% marzy",
    ROUND(ps.ilosc_ogolem, 3) AS "Ilosc",
    ROUND(COALESCE(sm.stan_ogolny, 0), 3) AS "Stan ogolny:Dane rozszerzone",
    ROUND(ps.netto_ogolem / NULLIF(c.liczba_miesiecy, 0), 2) AS "Srednia sprzedaz netto [PLN]",
    ROUND(ps.koszt_ogolem, 2) AS "Koszt [PLN]",
    dp.typ_dokumentu AS "Typ dokumentu",
    dp.tr_fullnumer AS "Numer dokumentu",
    dp.data_sprzedazy AS "Data sprzedazy",
    dp.data_wystawienia AS "Data wystawienia",
    dp.klient_nazwa AS "Klient",
    dp.sprzedawca_inicjaly AS "Handlowiec - inicjaly",
    ROUND(dp.ilosc_dokumentu, 3) AS "Ilosc dokumentu",
    ROUND(dp.netto_dokumentu, 2) AS "Sprzedaz netto dokumentu [PLN]",
    ROUND(dp.brutto_dokumentu, 2) AS "Brutto dokumentu [PLN]",
    ROUND(dp.koszt_dokumentu, 2) AS "Koszt dokumentu [PLN]",
    ROUND(dp.marza_dokumentu, 2) AS "Marza dokumentu [PLN]"
FROM dok_produkt dp
JOIN prod_suma ps ON ps.ttw_idtowaru = dp.ttw_idtowaru
                 AND ps.segment_klienta = dp.segment_klienta
LEFT JOIN stan_mag sm ON sm.ttw_idtowaru = dp.ttw_idtowaru
CROSS JOIN cfg c
WHERE ROUND(dp.ilosc_dokumentu, 6) <> 0
   OR ROUND(dp.netto_dokumentu, 6) <> 0
ORDER BY
    ps.produkt_nazwa,
    ps.produkt_kod,
    dp.data_sprzedazy,
    dp.tr_fullnumer;
"""
CSV_HEADERS = (
    "Rodzaj pozycji", "Kod", "Nazwa", "EAN", "Segment klienta", "Brutto (*)", "Sprzedaz netto [PLN]",
    "Marza [PLN]", "% marzy", "Ilosc", "Stan ogolny:Dane rozszerzone",
    "Srednia sprzedaz netto [PLN]", "Koszt [PLN]", "Typ dokumentu",
    "Numer dokumentu", "Data sprzedazy", "Data wystawienia", "Klient",
    "Handlowiec - inicjaly", "Ilosc dokumentu",
    "Sprzedaz netto dokumentu [PLN]", "Brutto dokumentu [PLN]",
    "Koszt dokumentu [PLN]", "Marza dokumentu [PLN]",
)


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


def previous_full_day(now: datetime | None = None) -> tuple[datetime, datetime]:
    timezone_name = os.environ.get("TIMEZONE", "Europe/Warsaw")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Nieznana strefa czasowa: {timezone_name}") from exc

    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    current_day = local_now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    previous_day = current_day - timedelta(days=1)
    return previous_day, current_day


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


def export_for_range(start_date: date, end_date: date) -> Response:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(EXPORT_QUERY, (start_date, end_date))
            rows = cursor.fetchall()

    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    writer.writerows(rows)

    filename = f"eksport_{start_date}_{end_date}.csv"
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/export/weekly", dependencies=[Depends(require_api_token)])
def export_weekly() -> Response:
    start_date, end_date = previous_full_week()
    return export_for_range(start_date.date(), end_date.date())


@app.get("/export/daily", dependencies=[Depends(require_api_token)])
def export_daily() -> Response:
    start_date, end_date = previous_full_day()
    return export_for_range(start_date.date(), end_date.date())


@app.get("/export/range", dependencies=[Depends(require_api_token)])
def export_range(data_od: date, data_do: date) -> Response:
    if data_od >= data_do:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="data_do musi byc pozniejsza niz data_od",
        )

    return export_for_range(data_od, data_do)