# Theta

The adaptive assessment service behind OnlineCampus: IRT scoring, adaptive
question selection, the question bank and live test monitoring.

Theta is **internal**. Browsers never call it. The OnlineCampus .NET API
authenticates the user, applies roles and Teaching Scope, then calls Theta over a
private network with a shared service token and the caller's context.

```
Angular (OnlineCampus) ──► OnlineCampus .NET API ──X-Theta-Token──► Theta ──► PostgreSQL + Redis
```

Design specification: https://claude.ai/code/artifact/ec88b91c-0f89-44d0-aa11-60e117956d50

## Run locally

Requires Python 3.12 and Docker.

```powershell
# 1. Configuration: copy, then set THETA_SERVICE_TOKEN
Copy-Item .env.example .env

# 2. PostgreSQL (host port 5433) and Redis (host port 6380)
docker compose up -d

# 3. Dependencies and schema
python -m venv venv
.\venv\Scripts\pip install -r requirements-dev.txt
.\venv\Scripts\alembic upgrade head

# 4. The API on http://localhost:8100 (docs at /docs)
.\venv\Scripts\uvicorn app.main:app --reload --port 8100
```

Tests need neither store running: `.\venv\Scripts\pytest`

## Configuration

| Variable | Purpose |
|---|---|
| `THETA_SERVICE_TOKEN` | Shared secret; must equal `Theta:ApiToken` in the OnlineCampus API. Empty refuses every protected call. |
| `THETA_DATABASE_URL` | `postgresql+asyncpg://…` |
| `THETA_REDIS_URL` | `redis://…` |
| `THETA_DB_PASSWORD` | Password docker-compose gives the database container |
| `THETA_ENVIRONMENT` | `production` hides `/docs` |
| `THETA_ANTHROPIC_API_KEY` | Anthropic API key for question writing and AI marking. Empty switches both off (`/v1/generate/questions` and `/v1/mark/*` answer 503). |
| `THETA_GENERATION_MODEL` | Claude model that writes questions; default `claude-opus-5` |
| `THETA_MARKING_MODEL` | Claude model that marks answers; default `claude-opus-5` |

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Process is alive (restart signal) |
| GET | `/ready` | none | Both stores reachable; 503 names the broken one |
| GET | `/v1/status` | service token + caller headers | Integration handshake: echoes the caller and both checks |
| GET · POST | `/v1/topics` | staff + scope | List (per class + subject) · create |
| PUT · DELETE | `/v1/topics/{id}` | staff + scope | Rename · deactivate (refused while live questions use it) |
| GET · POST | `/v1/questions` | staff + scope | Search (filters, paging) · create a draft |
| GET · PUT · DELETE | `/v1/questions/{id}` | staff + scope | Detail · edit (in place, or a new version once answered) · delete a draft |
| POST | `/v1/questions/{id}/status` | staff + scope | `pilot` (approve, sets the IRT prior) · `draft` · `retired` |
| POST | `/v1/generate/questions` | staff | Claude writes draft questions for a chapter or unit; each is checked against the bank's rules and returned (kept or rejected with the reason). Nothing is stored. |
| POST | `/v1/mark/short-answer` | any caller | Is this short answer the same as one of the accepted answers? A quick check (effort low); when it is unsure, a careful one (effort high) decides. |
| POST | `/v1/mark/rubric` | any caller | The mark scheme of a long-answer question: 1–6 points in half marks, adding up to the question's marks. OnlineCampus keeps the first one written. |
| POST | `/v1/mark/long-answer` | any caller | Marks a written answer against its scheme twice (a third time when the two differ by more than a mark), with the student's own words as evidence and feedback. |

Question lifecycle: **draft → pilot → active → retired**. Approval creates the item
parameters with a prior (b from the difficulty tag: easy −1, medium 0, hard +1;
c from the guessing chance). `active` is reached only by calibration (a later
milestone). Editing a question students have answered saves a new draft version.

Question writing is stateless: OnlineCampus checks the school's AI entitlement,
the teacher's Teaching Scope and the daily limits, keeps the log, and saves only
the drafts a teacher picks. Theta sends Claude the class, subject and topic names,
the teacher's notes and the stems already in the bank (so none is repeated); it
uses structured JSON output and Anthropic's default fallback model when a request
is declined. A decline answers 422, a cut-off or unreadable answer 502, and a busy
or rejected key 503.

Caller headers sent by OnlineCampus on every `/v1` call:

| Header | Example |
|---|---|
| `X-Theta-Token` | shared secret |
| `X-Theta-Institution-Id` | `12` |
| `X-Theta-User-Id` | `345` |
| `X-Theta-Role` | `admin` · `teacher` · `student` · `parent` |
| `X-Theta-Scope` | `all`, or `classId:subjectId` pairs such as `12:5,12:6` (required on topic and question endpoints; empty allows nothing) |

## Tests

`.\venv\Scripts\pytest` runs unit tests and, when the compose PostgreSQL is up,
API integration tests against a throwaway `theta_test` database the suite
creates and migrates itself (skipped with a message when PostgreSQL is down).
