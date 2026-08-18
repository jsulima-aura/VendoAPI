# Vendo CSV Export API

Proste API w FastAPI eksportujace zamowienia z zewnetrznego PostgreSQL do pliku CSV.

## Wymagania

- Docker
- Docker Compose
- dostep sieciowy kontenera do serwera PostgreSQL

PostgreSQL nie jest uruchamiany przez ten projekt. Serwer bazy musi akceptowac polaczenia z hosta Dockera (ustawienia `listen_addresses`, `pg_hba.conf` i zapory sieciowej).

## Konfiguracja

Utworz plik `.env` na podstawie `.env.example`:

```powershell
Copy-Item .env.example .env
```

Uzupelnij dane polaczenia. Zmienna `POSTGRES_HOST` musi wskazywac adres osiagalny z kontenera. Nie uzywaj `localhost`, poniewaz wewnatrz kontenera oznacza on sam kontener API.

## Uruchomienie

```powershell
docker compose up --build -d
```

API bedzie dostepne pod adresem `http://localhost:8000`.

## Endpointy

### Sprawdzenie stanu

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Odpowiedz:

```json
{"status":"ok"}
```

### Eksport poprzedniego tygodnia

```powershell
Invoke-WebRequest `
  -Uri http://localhost:8000/export/weekly `
  -OutFile eksport.csv
```

Endpoint wyznacza poprzedni pelny tydzien w strefie z `TIMEZONE`: od poprzedniego poniedzialku 00:00 (wlacznie) do biezacego poniedzialku 00:00 (wylacznie). Zwracany plik ma kodowanie UTF-8, separator `;`, naglowki kolumn oraz nazwe `eksport_YYYY-MM-DD_YYYY-MM-DD.csv`.

## Zatrzymanie

```powershell
docker compose down
```