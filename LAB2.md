# Ćwiczenie laboratoryjne nr 2

## Zadanie 1 — środowiska baz danych

GitHub Actions uruchamia bazy PostGIS na tym komputerze przez runnera w kontenerze Docker. Docker Desktop i komputer muszą działać podczas wykonywania zadań.

| Gałąź i środowisko GitHub | Projekt Compose | Port na komputerze | Wolumen danych |
| --- | --- | --- | --- |
| `env-test` | `remote-db-env-test` | `127.0.0.1:5445` | `remote-db-env-test_pgdata` |
| `env-qa` | `remote-db-env-qa` | `127.0.0.1:5446` | `remote-db-env-qa_pgdata` |
| `main` produkcja | `remote-db-main` | `127.0.0.1:5447` | `remote-db-main_pgdata` |

## Przygotowanie

1. Uruchom Docker Desktop w trybie kontenerów Linux.
2. Na koncie GitHub utwórz personal access token **classic**, z zakresem `repo` i określonym terminem ważności. Nie umieszczaj go w repozytorium ani w czacie. Po zakończeniu cyklu zajęć usuń token.
3. W PowerShell uruchom `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/Initialize-Lab.ps1`. Wklej token do zamaskowanego pola w tym oknie.
4. Skrypt tworzy środowiska `env-test`, `env-qa`, `main`. W każdym ustawia zmienne `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PORT` i sekret `POSTGRES_PASSWORD`.
5. Skrypt rejestruje runnera `psip-local-docker` z etykietą `psip-db`. Runner korzysta z oficjalnego obrazu GitHuba i silnika Docker Desktop przez gniazdo Dockera. Nie wymaga trybu `privileged` ani osobnego silnika Docker wewnątrz kontenera.

Hasła są losowane osobno dla środowisk i zapisane lokalnie w `.local/*.env`. Katalog `.local` jest wykluczony z Git. Ponowne uruchomienie skryptu zachowuje hasła. Jeśli istnieje już wolumen bazy, a brakuje jej lokalnej konfiguracji, skrypt zatrzymuje się i wymaga odtworzenia konfiguracji. Token classic istnieje tylko w pamięci procesu podczas konfiguracji; runner otrzymuje krótkotrwały token rejestracyjny przez standardowe wejście. Pliki poświadczeń zarejestrowanego runnera pozostają w jego osobnym wolumenie.

## Automatyczne uruchamianie

`start_services.yml` wykonuje walidację przy pull requeście. Po wysłaniu zmian lub scaleniu pull requesta do `env-test`, `env-qa` albo `main` uruchamia odpowiednią bazę. Sprawdza jej gotowość, logowanie przez TCP i działanie PostGIS.

Polecenie dla gałęzi testowej odpowiada:

```bash
docker compose -f docker_compose.yml -p remote-db-env-test up -d --build --wait postgis
```

Wartości poświadczeń trafiają z sekretów do środowiska procesu i Compose; nie są wypisywane do logów. Zdefiniowane wolumeny mają automatycznie różne prefiksy projektów Compose. Wszystkie bazy mogą działać jednocześnie.

Pull requesty są sprawdzane na runnerze GitHuba. Lokalny runner z dostępem do Dockera wykonuje tylko zadania z zaufanych gałęzi po `push` lub po ręcznym uruchomieniu. Nie dodawaj uruchamiania obcego kodu z pull requestów na tym runnerze.

## Ręczne zatrzymywanie i usuwanie

W zakładce **Actions** wybierz **Stop PostGIS** albo **Destroy PostGIS**, następnie **Run workflow** i właściwą gałąź.

- **Stop PostGIS** zatrzymuje kontenery i zachowuje dane.
- **Destroy PostGIS** usuwa kontenery, sieć i wolumen wyłącznie wybranej gałęzi. Wymaga zaznaczenia `confirm_delete_data`. Ta operacja usuwa wszystkie dane wybranej bazy.

Ręczne uruchamianie jest dostępne po umieszczeniu definicji workflow na domyślnej gałęzi `main`.

## Sprawdzenie wykonania

1. W **Settings → Actions → Runners** sprawdź status `Idle` / `online` runnera `psip-local-docker`.
2. W **Settings → Environments** sprawdź trzy środowiska i nazwy zmiennych oraz sekretu.
3. W **Actions** sprawdź zakończone powodzeniem uruchomienia **Start PostGIS** dla trzech gałęzi.
4. W Docker Desktop sprawdź trzy projekty `remote-db-*` i zdrowe kontenery PostGIS.
5. Utwórz pull request `env-test` → `env-qa`, sprawdź walidację i scal zmiany. Po scaleniu sprawdź automatyczne wdrożenie `env-qa`. Analogicznie przenieś sprawdzone zmiany do `main`.
6. Zatrzymaj `env-test` przez **Stop PostGIS**, uruchom je ponownie przez **Start PostGIS** i sprawdź, że dane pozostały. Usuwanie testuj wyłącznie na danych, które możesz utracić.

Przykładowe polecenia lokalne w PowerShell:

```powershell
docker compose --env-file .local/env-test.env -f docker_compose.yml -p remote-db-env-test ps
docker compose --env-file .local/env-qa.env -f docker_compose.yml -p remote-db-env-qa ps
docker compose --env-file .local/main.env -f docker_compose.yml -p remote-db-main ps
docker compose -p psip-db-runner -f runner-compose.yml logs --tail 30 runner
```

Kontener Pythona jest dostępny przez profil `tools`. W zadaniu 1 uruchamiana jest tylko baza, a w zadaniu 2 kontener Pythona generuje i ładuje siatki.

## Zadanie 2 — podpunkty 1–3

Wybrany obszar to **powiat biłgorajski, kod TERYT 0602**. Granica pochodzi z pliku `data/powiat.shp` z pierwszych zajęć. Plik ten zawiera 380 powiatów; do zadania wybierany jest tylko rekord `JPT_KOD_JE = 0602`.

1. `app/python/h3_generator.py` generuje pełne komórki H3 dla czterech kolejnych rozdzielczości: **6, 7, 8 i 9**. Większy numer oznacza mniejsze komórki. Każda siatka jest liczona osobno z granicy powiatu.
2. Do H3 trafia granica WGS84 (EPSG:4326). Tryb `overlap` obejmuje również komórki przecinające granicę powiatu, których środek leży poza nim. Wersja `h3==4.5.0` jest przypięta w `requirements.txt`.
3. Zaktualizowany `add/load.py` uruchamia loader z `app/python/load.py`. Loader zapisuje granicę i cztery siatki w schemacie **`grid`** każdego środowiska. Geometrie w bazie mają układ **EPSG:2180**, dzięki czemu pola powierzchni są wyrażone w m².

| Tabela w każdej bazie | Zawartość | Liczba obiektów |
| --- | --- | ---: |
| `grid.powiat` | Granica wybranego powiatu | 1 |
| `grid.h3_r6` | Siatka H3, rozdzielczość 6 | 76 |
| `grid.h3_r7` | Siatka H3, rozdzielczość 7 | 426 |
| `grid.h3_r8` | Siatka H3, rozdzielczość 8 | 2630 |
| `grid.h3_r9` | Siatka H3, rozdzielczość 9 | 17361 |

W tabelach siatek `geom` przechowuje pełny heksagon, a `geom_clipped` jego część wewnątrz powiatu. Kolumny `area_m2` i `area_in_county_m2` zawierają odpowiadające im powierzchnie. `h3_id` jest identyfikatorem komórki. Geometrie mają indeksy przestrzenne. Komórki dotykające powiatu wyłącznie krawędzią lub punktem są pomijane przy ładowaniu.

Loader korzysta ze zmiennych połączenia i hasła przekazanych przez Compose. Zapis odbywa się w transakcji; błąd walidacji cofa cały import. Ponowne uruchomienie zastępuje tylko siatki wybranego powiatu i podanych rozdzielczości. Inne schematy i powiaty pozostają zachowane.

### Uruchomienie przez GitHub

W **Actions → Load H3 grids → Run workflow** wybierz gałąź `main`. Workflow sprawdza generator, a następnie ładuje te same cztery siatki do `env-test`, `env-qa` i `main`. Docker Desktop i runner muszą działać. Kod oraz wybrana granica są częścią obrazu Pythona, dlatego zadanie nie wymaga montowania folderów z systemu runnera.

### Uruchomienie lokalne

Z katalogu projektu w PowerShell:

```powershell
foreach ($s in @('env-test', 'env-qa', 'main')) {
    docker compose --env-file ".local/$s.env" -f docker_compose.yml -p "remote-db-$s" --profile tools run --rm --build -T python
    if ($LASTEXITCODE -ne 0) { throw "Import nie powiodl sie: $s" }
}
```

Sprawdzenie istniejących danych bez ich ponownego zapisu:

```powershell
docker compose --env-file .local/env-test.env -f docker_compose.yml -p remote-db-env-test --profile tools run --rm -T python python /app/python/load.py --validate-only
```

Wynik zawiera liczby komórek, sumy kontrolne identyfikatorów, powierzchnię i kontrolę pokrycia powiatu. Dla każdej rozdzielczości suma kontrolna i liczba rekordów powinny być identyczne w trzech bazach.

### Przygotowanie granicy i sam eksport siatki

`app/python/prepare_county.py` wybiera powiat według kodu, odczytuje układ z pliku `.prj` i przelicza go do WGS84. Obsługuje też istniejącą w danych nazwę `powiat.dbf.dbf`. Wynik znajduje się w `app/data/powiat_bilgorajski.geojson`.

Po zainstalowaniu zależności z `app/python/requirements.txt` można odtworzyć granicę i wyeksportować siatki bez bazy:

```powershell
python app/python/prepare_county.py --county-code 0602
python app/python/h3_generator.py --resolutions 6 7 8 9 --output .local/grid_0602.geojson
python -m unittest discover -s app/python/tests -v
```

Ten etap kończy się na podpunkcie 3. Pobieranie BDOT10K i agregacja danych należą do kolejnych podpunktów.
