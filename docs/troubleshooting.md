# Troubleshooting (Windows-first)

### "running scripts is disabled on this system" when running a `.ps1`
Windows' default execution policy (`Restricted`) blocks scripts. Run it for this one invocation only, without changing any machine setting:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_local_db.ps1
```

### `python` opens the Microsoft Store / "Python was not found"
The Store alias shadows the real interpreter. Use the launcher: `py -3.12 -m venv .venv`, then always call `.\.venv\Scripts\python.exe` (or activate with `.\.venv\Scripts\Activate.ps1`). If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### `psql` / `pg_isready` not recognised
They are not on PATH by default. Use the full path: `& "C:\Program Files\PostgreSQL\18\bin\psql.exe" ...`, or add that folder to your user PATH.

### `fe_sendauth: no password supplied` / `password authentication failed for user "last_seat_app"`
`.env` must contain the same password the bootstrap script set. Re-run `.\scripts\bootstrap_local_db.ps1` — it (re)sets the role's password from `.env` and never drops anything.

### `Refusing to use database 'xyz'`
Intentional safety guard (`app/core/config.py::assert_project_database`). `DATABASE_URL` / `TEST_DATABASE_URL` must end in `/last_seat_db` or `/last_seat_test_db`.

### `permission denied for schema public`
The project role must **own** the database (the bootstrap does this). On PostgreSQL 15+ `public` is owned by `pg_database_owner`, so the DB owner can create tables. Check with `\l` in psql.

### Tests exit immediately with "TEST_DATABASE_URL is not configured"
Add `TEST_DATABASE_URL=...last_seat_test_db` to `.env` (see `.env.example`).

### `alembic upgrade head` says nothing / "Can't locate revision"
Run it from the project root (where `alembic.ini` lives). `alembic current` shows what is applied.

### Port 8000 already in use
`Get-NetTCPConnection -LocalPort 8000 -State Listen` shows the owning PID. Start on another port: `uvicorn app.main:app --port 8001`, or for Docker `$env:API_HOST_PORT = "8001"; docker compose up -d`.

### Docker: `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`
Docker Desktop is not running (or is waiting on a dialog: update, sign-in, licence). Open it, wait until the whale icon says "Engine running", then retry. `wsl -l -v` should show `docker-desktop  Running`.

### Docker DB on 5433 vs local DB on 5432
They are completely separate servers. Workflow A uses your local PostgreSQL on **5432**; Workflow B's container is published on **5433**. To inspect the container DB from Windows: `psql -h localhost -p 5433 -U last_seat_app -d last_seat_db` (password: `last_seat_container_pw` unless you set `POSTGRES_PASSWORD`).

### Changed `POSTGRES_PASSWORD` but the container still uses the old one
The official postgres image only applies the password on **first** initialisation of an empty volume. Either keep the old password, or (destroys the container DB's data) `docker compose down -v` and start again.

### `503 DATABASE_BUSY` under heavy load
A request waited longer than `DB_LOCK_TIMEOUT_MS` (default 10 s) for a seat lock. It was rolled back cleanly; retry with the same `Idempotency-Key`.

### Locust shows many 409s
Expected: `409 SEAT_UNAVAILABLE` means "you lost the race", which is the correct answer for all but one student per seat. The locustfile marks 201 and 409 as successes and only 5xx/unexpected codes as failures.

### OneDrive "file in use" / slow installs
The project lives under OneDrive; syncing `.venv` can lock files. Pause OneDrive sync during `pip install`, or exclude `.venv` from sync.
