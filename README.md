# Parts

A lightweight self-hosted electronics component database with distributor search, price comparison and project lists.

- Manual components with images, PDF datasheets, specs, links and personal notes
- Concurrent search across Mouser and Nexar/Octopart with deduplicated, grouped results
- Nested categories, tags, projects with quantities and estimated cost
- Authentik (OIDC) login or simple local login
- Read-only JSON API protected by `API_KEY` (`/api/v1/parts`, `/api/v1/parts/<id>`, `/api/v1/categories`; send `X-API-Key`)

## Run

```
cp .env.example .env
# edit .env: set SECRET_KEY, LOCAL_PASSWORD (or the OIDC_* values)
docker compose up -d
```

Open http://localhost:8000. Data is stored in `./data`. The first user to log in becomes administrator; enable providers and enter API keys under Settings.
