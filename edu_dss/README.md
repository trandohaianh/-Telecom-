# Edu DSS service split

```text
Browser --HTTPS:443--> Nginx --/--> Streamlit (stateless view)
                         |              | HTTPX AsyncClient over one loop/pool
                         +--/api/*------+--> FastAPI --JWT/RBAC--> Celery queue
                                                   |                    |
                                                   +-- Redis cache <----+-- Celery workers
                                                      (internal network, ACL)
```

`backend/app/main.py` provides JWT-protected Instructor, Student, and Admin routes. `ui/app.py` only renders forms, sends async HTTPX requests, and polls the task endpoint in a Streamlit fragment. Redis and Celery are on an internal-only data network. Only Nginx publishes ports 80 and 443. The gateway redirects HTTP to HTTPS and forwards Streamlit WebSocket upgrade headers. Only the Celery worker has outbound network access for configured adapters; constrain it with host/cloud egress policy. A Redis atomic counter enforces a global requests-per-minute cap in addition to Celery's worker rate limit.

## Local container smoke setup

From the repository root in PowerShell, run `.\edu_dss\bootstrap-local.ps1` then `docker compose -f edu_dss/docker-compose.yml up --build`. The first command creates disposable local JWT/Redis secrets, a self-signed localhost certificate, and `EDU_DEMO_MODE=1` in `.env`. Your browser will show the expected certificate warning. Then run `docker compose -f edu_dss/docker-compose.yml exec fastapi-backend python -m app.demo_tokens` and paste a role token into the UI. Replace local secrets/certificates and disable demo mode before deploying. Keep the `secrets/` directory private and untracked.

Issue JWTs through your institution's identity provider. Required claims: `iss=edu-dss`, `sub`, `exp`, and `role` (`instructor`, `student`, or `admin`). This service intentionally has no password/token-minting endpoint. Use a managed identity layer to issue and refresh tokens.

## Domain adapters required

The workspace did not contain the institution's Edu DSS pipeline or labeled grade data. In local demo mode, deterministic AutoML/CMS/advice fixtures exercise the queue/cache/UI path and are labeled demo_fixture_only; they are not trained institutional models or valid recommendations. A separate synthetic OLS grade baseline is documented below. With demo mode disabled, EDU_AUTOML_ADAPTER, EDU_EXPLANATION_ADAPTER, and EDU_ADVICE_ADAPTER must point to TLS-protected internal services implementing the JSON exchanges in backend/app/services.py. The workers send a shared bearer credential from the Compose secret; configure credential rotation and adapter-side validation. The knowledge-graph endpoint reports adapter_required until the real graph store is connected. The API fails closed on course access: Student JWTs need a course_ids membership claim and Instructor JWTs need teaching_course_ids; Admin is global. Local demo tokens receive access only to demo-course. Production IdP claims must be short-lived and reflect current enrollment/teaching assignments. Add tenant policy, revocation handling and audit before real student data.

Use durable external Redis or managed Redis with backups, monitoring, TLS, and tested failover for production. The Compose Redis volume is single-node persistence, not HA. Add an external audit sink, OIDC/JWKS validation (or asymmetric JWT keys), per-tenant quotas, schema migrations, retention policy, and observability before a production go-live. The included role checks are functional RBAC, not complete institutional authorization.

## Grade prediction baseline

The student-only API exposes POST /api/student/grade-baseline and derives identity from the verified JWT; course access is checked before model access. In EDU_DEMO_MODE=1 it fits ordinary least squares on seven synthetic prior-GPA/final-GPA pairs, using a fixed demo GPA of 2.5, and reports a two-row synthetic holdout MAE/RMSE. The response is marked demo_fixture_only and must not be presented as a result trained or validated on real student records. Outside demo mode, configure EDU_GRADE_ADAPTER; the adapter receives only the authenticated subject and course so it can resolve trusted academic data, and the API validates its prediction is finite and within the 0..4 scale. This exercises the prediction contract; it is not a production academic decision model.

## Authorization changes (audit 2026-11-17)

- `POST /api/instructor/automl` now requires `course_id`; the caller must teach that course, and the adapter receives `course_id` so it resolves `dataset_ref` only inside it.
- Demo tokens carry `"demo": true` and are rejected when `EDU_DEMO_MODE` is not `1`.
- Set `JWT_AUDIENCE` to require and verify the `aud` claim.
- The Celery worker no longer receives `JWT_SECRET`; only the API (and the demo-token CLI) load it.
