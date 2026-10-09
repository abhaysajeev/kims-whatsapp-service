# KIMS WhatsApp acknowledgement service

Sends visitors a WhatsApp message with a link to their entry/exit slip (PDF) when
they are marked in or out in the ViziPlus visitor system. Runs as one small Docker
container beside the existing system; the Android app is not changed.

```
entry_exit_details ──trigger──► wa_notification_queue ◄──poll── worker (Docker)
   (hospital table)            (job per ENTRY / EXIT)            │
                                                                 ├─ build slip PDF
                                                                 ├─ upload to Bunny  ─► public link
                                                                 ├─ opt-in number (once per phone)
                                                                 └─ ACL send template (link in body)
```

- The triggers only insert a queue row. They never call anything external, and an
  error inside them can never block the hospital's insert or update.
- ENTRY is queued on insert; EXIT when `exit_date` goes from empty to a value.
  At most one ENTRY and one EXIT per visit.
- `ACCEPTED` means ACL accepted the request. ACL does not report WhatsApp delivery
  back to this service.

---

## 1. Setup order

> On some hosts the command is `docker-compose` instead of `docker compose`.

1. **Fill `.env`** (copy from `.env.example`). Check it:
   ```
   docker compose run --rm --entrypoint python wa-service -m app.config
   ```
   Every missing or wrong value is reported at once; passwords are never printed.
2. **Check `table_mapping.yaml`** matches the hospital table (already filled for
   `entry_exit_details`).
3. **Build:** `docker compose build`
4. **Create the database objects** (once per database; safe to re-run):
   ```
   docker compose run --rm wa-service setup
   docker compose run --rm wa-service setup --check
   ```
   Works on MariaDB 10.5+ and MySQL 5.7+. Uses the DB login from `.env`. Creates `wa_notification_queue`, `wa_phone_optin`
   and the triggers `trg_eed_wa_ai` / `trg_eed_wa_au`. After this, every new visit is
   queued, but nothing is sent until the worker runs.
5. **Start:** `docker compose up -d` → `docker compose ps` should show `healthy`
   within about a minute.
6. **Turn sending on** when ready: set `WHATSAPP_SEND_ENABLED=true` in `.env`, then
   `docker compose up -d --force-recreate`.

### The send switch: `WHATSAPP_SEND_ENABLED`

| Value | Behaviour |
|---|---|
| `false` | The worker runs and stays healthy but processes **no** jobs. New visits are still queued and wait as `PENDING`. The startup log says sending is disabled. |
| `true` | Jobs are processed and sent. |

Turning it on also sends everything that has been waiting. To start fresh instead,
first discard the waiting jobs:
```sql
DELETE FROM wa_notification_queue WHERE status = 'PENDING';
```
`--dry-run` and `setup` work the same either way.

### QA vs production

Only `.env` changes:

| Setting | QA | Production |
|---|---|---|
| `WHATSAPP_SEND_ENABLED` | `false` until you test | `true` when going live |
| `DB_*` | QA database | live database |
| `BUNNY_ENTRY_FOLDER` / `BUNNY_EXIT_FOLDER` | `qa_entry` / `qa_exit` | `entry` / `exit` |

If the database runs on the same VPS, use `DB_HOST=host.docker.internal`.
Then run step 4 once against the live database and start the container.

---

## 2. Operating

| Task | Command |
|---|---|
| Logs | `docker compose logs -f --tail 100` |
| Status | `docker compose ps` |
| Restart (e.g. after editing `.env` or the YAML files) | `docker compose up -d --force-recreate` |
| Stop | `docker compose stop` (finishes the current job) |
| Check DB objects | `docker compose run --rm wa-service setup --check` |
| Queue report | run `sql/report.sql` in a DB client |

`sql/report.sql` shows jobs by status, the age of the oldest PENDING job, FAILED jobs
with their reasons, and **uncertain** jobs.

**Uncertain jobs** are jobs where the worker stopped while ACL was being called
(`status = PROCESSING` and `last_attempt_at` set). They are retried automatically
after `STUCK_JOB_MINUTES` with the same `messageId`/`transactionId`, so ACL can
recognise a repeat.

Log lines are `key=value`, for example:
```
level=INFO  logger=worker msg="message accepted by ACL" job=12 event=ENTRY phone=91XXXXXXX210 status=ACCEPTED bsp_message_id=...
level=ERROR logger=worker msg="job failed permanently" job=13 event=ENTRY status=FAILED error="invalid phone: too short (5 digits)"
```
Phone numbers are masked; passwords, keys and tokens are never logged.

### Dry run: see exactly what would be sent

Writes nothing to the database and makes no ACL call (no token, no opt-in, no send):
```
docker compose run --rm wa-service --dry-run --source-id 142 --event ENTRY
docker compose run --rm wa-service --dry-run --job 12
docker compose run --rm wa-service --dry-run --source-id 142 --event ENTRY --upload
```
It prints the phone check, the PDF path and the exact JSON payload (token masked).
`--upload` also uploads the PDF to the configured Bunny folder so you can open the link.

### Test with a single queue row

With the worker running, add one job for an existing visit and a phone you control:
```sql
INSERT INTO wa_notification_queue
  (source_record_id, event_type, phone, next_attempt_at, created_at, updated_at)
VALUES (142, 'ENTRY', '9XXXXXXXXX', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6));
```
Watch the logs, then check the row:
```sql
SELECT id, status, attempt_count, bsp_message_id, pdf_url, error_message
FROM wa_notification_queue ORDER BY id DESC LIMIT 1;
```
A visit can have only one ENTRY and one EXIT job. To send again, delete the old row
first or requeue it (below).

### Requeue a FAILED job
```sql
UPDATE wa_notification_queue
SET status = 'PENDING', attempt_count = 0, next_attempt_at = UTC_TIMESTAMP(6),
    error_message = NULL
WHERE id = 13;
```
The same PDF link and message IDs are reused. To force a fresh opt-in for a number,
delete it from `wa_phone_optin`.

---

## 3. What happens when something fails

| Situation | Result |
|---|---|
| Invalid phone (empty, too short, not +91 mobile) | `FAILED` immediately; no PDF, no API call |
| Network error, timeout, HTTP 5xx / 408 / 429 | Retried after 1, 5, 15, 60 min (`RETRY_BACKOFF_MINUTES`) until `MAX_ATTEMPTS`, then `FAILED` |
| Other HTTP 4xx from ACL or Bunny (e.g. template mismatch) | `FAILED` immediately, with ACL's reason |
| ACL login wrong or auth server down, Bunny key wrong | Job put back **without** using an attempt; the worker pauses 1→2→5→10 min and logs one clear error. Fix `.env`, restart. |
| ACL send returns 401 | Token renewed once and the send retried with the same IDs |
| ACL returns 200 but not `success: true` | `FAILED` (not retried, to avoid a duplicate message) |
| Worker crashes mid-job | Job reset after `STUCK_JOB_MINUTES` and retried |
| Database unreachable | Reconnects each cycle; after `MAX_CONSECUTIVE_DB_FAILURES` the process exits and Docker restarts it |

On every attempt the PDF is uploaded only once (the link is stored), each phone is
opted in only once (`wa_phone_optin`), and the token is cached and renewed shortly
before it expires (ACL tokens last 10 hours).

---

## 4. Safety notes

- **The triggers run as the DB login that ran `setup`.** If that login is deleted,
  every insert into `entry_exit_details` fails (`ERROR 1449`). Use a permanent login.
  `setup --check` and the worker's startup check report this as **CRITICAL**;
  re-running `setup` with an existing login fixes it.
- **Uninstall:** run `sql/uninstall.sql`. It drops the triggers first. The tables are
  kept as message history; the file shows how to drop them afterwards.
- `.env` holds credentials. It is gitignored and never copied into the image.
- PDFs are public on the Bunny CDN, under random 32-character names that cannot be
  guessed from visit data. They are kept indefinitely.

---

## 5. Configuration reference

| File | Contents |
|---|---|
| `.env` | credentials and settings (see `.env.example` for every key with comments) |
| `table_mapping.yaml` | hospital table/column names, JSON keys, names of our tables and triggers |
| `whatsapp_templates.yaml` | template layouts; a new template = a new entry + `TEMPLATE_NAME` in `.env` |
| `templates/slip.html` | slip PDF layout (HTML/CSS) |
| `templates/assets/kims_logo.png` | logo at the top of every slip (replace the file to change it) |
| `sql/` | generated SQL for review or manual runs: `python -m app.render_sql` |

The YAML files are mounted into the container read-only, so editing them needs only
a restart. Changing `templates/slip.html` or the logo needs `docker compose build`.

---

## 6. Development

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m app.config                       # validate .env
python -m app.pdf_generator pdf_samples/   # sample slips, no database needed
pytest                                     # unit tests
docker build --target test .               # full suite inside the production image
```

Database integration tests need a **throwaway** MariaDB (they truncate tables):
```
docker run -d --name wa-test-mariadb -e MARIADB_ROOT_PASSWORD=rootpw \
  -e MARIADB_DATABASE=vizipluskims -p 127.0.0.1:33065:3306 mariadb:10.5
WA_TEST_DB=1 pytest
```

| Module | Role |
|---|---|
| `app/main.py` | CLI: worker, `setup`, `--once`, `--dry-run` |
| `app/worker.py` | polling loop, job processing, retry policy |
| `app/queue.py` | all queue and opt-in SQL |
| `app/acl.py` | token cache, opt-in, payload builder, send |
| `app/bunny.py` | PDF upload |
| `app/pdf_generator.py` | slip PDF and QR |
| `app/source.py` | reads a visit from the hospital table |
| `app/phone.py` | phone normalisation (`91XXXXXXXXXX`, no `+`) |
| `app/schema.py`, `app/setup.py` | database objects, setup and checks |
| `app/config.py` | loads and validates all configuration |
