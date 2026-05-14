# Autognosys Platform

AI-powered Platform Engineering and AIOps infrastructure.

## Structure

| Directory | Purpose |
|---|---|
| `infra/` | Pulumi IaC — GCP VM provisioning |
| `ansible/` | Ansible playbooks — VM configuration |
| `website/` | Next.js company website |
| `backend/` | FastAPI AI services |
| `otel/` | OpenTelemetry Collector config |
| `.github/workflows/` | CI/CD pipelines |
| `docs/decisions/` | Architecture Decision Records |

## Stack
- **IaC**: Pulumi (Python)
- **Config management**: Ansible
- **Reverse proxy**: Caddy
- **Frontend**: Next.js + Tailwind
- **Backend**: FastAPI + LangChain
- **Observability**: OpenTelemetry + OpenObserve
- **Cloud**: GCP us-central1
- **CI/CD**: GitHub Actions
