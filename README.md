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
- **Load balancer / TLS**: GCP global external Application Load Balancer with a Certificate Manager certificate (`infra/load_balancer.py`)
- **Frontend**: Next.js + Tailwind
- **Backend**: FastAPI + LangChain
- **Observability**: OpenTelemetry + OpenObserve
- **Cloud**: GCP us-central1
- **CI/CD**: GitHub Actions
