# Ćwiczenie laboratoryjne nr 2 Zadanie 1

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

Opcjonalny kontener Pythona z poprzedniego ćwiczenia jest dostępny przez profil `tools`. W zadaniu 1 uruchamiana jest tylko baza.
