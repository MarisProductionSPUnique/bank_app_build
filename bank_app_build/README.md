# Maris Bank — Production Support Lab

A working-code banking classroom demo with Flask, PostgreSQL, HTML/CSS/JavaScript,
and an optional Nginx layer. Dummy money only; this is not a real banking system.

## Delivery status

Source code is built. Nine HTTP/configuration tests passed in the build workspace.
Eleven PostgreSQL integration cases are included but were not run there because a
PostgreSQL server could not be started. Run the integration suite before accepting
this as a verified deployment. No public deployment URL has been created yet.

## Features

- Session login/logout, CSRF protection, password hashing and 30-minute sessions.
- Ravi, Priya and trainer demo users; one account per user.
- Own-account balance, beneficiary add/list, transfer and latest 100 transactions.
- Integer paise arithmetic; one transaction for debit, credit, transfer and ledger.
- Ordered PostgreSQL row locks and per-sender idempotency keys prevent overspending
  and duplicate debits. Both UI retry logic and server-side checks are included.
- Request IDs, structured JSON application logs, API response inspector and health endpoints.
- Trainer-only controlled 400/500/503/slow-response scenarios.
- Login lockout after ten incorrect passwords, for ten minutes. Nginx also limits login rate.

## Quick start on your Ubuntu laptop (Docker)

Prerequisites: Docker Engine with the Compose plugin installed and running.
Extract the ZIP and open a terminal in the `bank_app_build` folder.

```bash
python3 setup_env.py
docker compose build
docker compose up -d db
docker compose run --rm app flask --app wsgi init-db
docker compose up -d
```

Open http://localhost:8080. Open `.env` in your editor to see the generated
RAVI_PASSWORD, PRIYA_PASSWORD and TRAINER_PASSWORD. Usernames are `ravi`, `priya`
and `trainer`. Never upload `.env` to GitHub.

Seed balances: Ravi ₹10,000, Priya ₹5,000 and trainer ₹10,000.
Account numbers: DEMO1001, DEMO1002 and DEMO1003 respectively.
`init-db` can run again without changing existing passwords or balances.
It creates the initial schema; it is not a migration system for later schema changes.

The browser connects to Nginx on localhost:8080; Nginx connects to Gunicorn/Flask
on internal port 8000; Flask connects to PostgreSQL on internal port 5432.
Only port 8080 is exposed, and only on your laptop's loopback interface.
Do not set COOKIE_SECURE=false on a public deployment.

## Logs and classroom exercises

```bash
docker compose logs -f app
docker compose logs -f web
docker compose logs -f db
```

The Nginx image writes its access/error output to container stdout/stderr. The
configuration uses `/var/log/nginx/access.log` and `/var/log/nginx/error.log`.
Flask writes JSON application events to stdout. Gunicorn writes access and error
logs to the same container stream. PostgreSQL logging defaults do not record
every query and do not automatically include the application request ID.

1. Log in as Ravi. Send ₹500 to Priya. Check Ravi ₹9,500 and Priya ₹5,500.
2. Copy the request ID from the API response panel. Search application logs:
   `docker compose logs app | grep 'PASTE-REQUEST-ID-HERE'`.
3. Log in as trainer. Trigger the application-error scenario. Find its exception
   type and stack frames in the JSON log. The HTTP response is 500.
4. Trigger simulated database failure. It returns 503 without stopping the database.
5. To demonstrate a REAL outage locally, use `docker compose stop db`, then refresh
   the account. Restore it with `docker compose start db`. Existing data is retained.
6. Stop only Flask with `docker compose stop app`: Nginx will report an upstream
   failure. Recover with `docker compose start app`.

`docker compose down` stops services and retains the database volume. Do not add
`-v` unless you intend to delete the disposable training data.

## Render + Supabase deployment

Use a NEW dedicated database/schema for this app, not an existing real application
database. The database account needs access only to this training database.

1. Create a dedicated GitHub repository, for example `bank-app-build`. Upload the
   CONTENTS of this folder at the repository root, including `.github`, but exclude
   `.env`, caches and virtual environments.
2. Create/select a dedicated PostgreSQL database in Supabase. Obtain its supported
   PostgreSQL connection string from the Connect panel. Use the session pooler if
   needed by your environment. Keep the supplied host, username and port intact.
   Append the provider's required TLS settings, such as `sslmode=require`.
3. On your laptop, create a Python virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. In your local `.env`, set DATABASE_URL to the dedicated Supabase PostgreSQL URL
   and ensure all three classroom passwords are generated. Do not paste secrets
   into chat or commit them. Then run `flask --app wsgi init-db` locally to initialize
   that cloud database.
5. In Render, create a Blueprint from the repository using `render.yaml`, or create
   a Python Web Service using the settings below. Enter DATABASE_URL in Render's
   environment settings. Blueprint setup generates SECRET_KEY automatically.
6. Confirm the selected plan and any charges in your own account before creating
   paid resources. The Blueprint requests a free web-service plan; availability
   and provider limits are account dependent.
7. Deploy, then visit `/health/ready`. Expect HTTP 200 and `database: reachable`.
8. Log in with Ravi's classroom password, make a transfer and inspect Render logs.
   Run PostgreSQL integration tests against a SEPARATE disposable database first.

Manual Render settings:

| Setting | Value |
|---|---|
| Runtime | Python |
| Build | `pip install -r requirements.txt` |
| Start | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --access-logfile - --error-logfile - wsgi:app` |
| Health path | `/health/ready` |
| DATABASE_URL | Dedicated PostgreSQL connection URL, supplied privately |
| SECRET_KEY | Unique random session secret |
| COOKIE_SECURE | `true` |
| ENABLE_LAB | `true` for the trainer exercises |

The cloud version runs behind Render's managed web edge. It does not provide your
own Nginx machine or a promised SSH login. Use Docker locally to teach individual
web/app/database services and their logs.

Render reference: https://render.com/docs/deploy-flask
Blueprint reference: https://render.com/docs/blueprint-spec
Flask/Gunicorn reference: https://flask.palletsprojects.com/en/stable/deploying/gunicorn/
PostgreSQL locking reference: https://www.postgresql.org/docs/18/transaction-iso.html

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

Without TEST_DATABASE_URL, HTTP checks run and PostgreSQL tests are explicitly
skipped. To run the full suite, provide TEST_DATABASE_URL pointing to a disposable
database whose name ends in `_test`. Tests CREATE AND DROP this app's tables there.
Never point this variable at classroom or real data.

The included GitHub Actions workflow provisions a disposable PostgreSQL 16 service
and runs the full suite on pushes and pull requests. It uses only test credentials.

## Limits

This is a training application, not production banking software. It has no real
payment-network integration, MFA, regulatory controls, immutable audit archive,
account provisioning UI, monitoring vendor integration, or migration framework.
Classroom accounts are shared if several students use the same credentials. Use
dummy information only. A multi-class deployment should provision isolated users.
The response inspector displays the most recent user action; background refresh
requests do not overwrite it. Server logs are viewed in Docker/Render separately.
