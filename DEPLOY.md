# Deploy guide

## What you need

1. A MyFitnessPal account.
2. A host that can run Docker and keep environment variables secret.
3. A public HTTPS URL for the service.
4. A long random `API_TOKEN` that Muse will store in its secure connector form.

Do not send the MyFitnessPal password or the API token in chat or to GitHub.

## Render blueprint

Create a new Render Blueprint or Web Service from this repo. Render reads
`render.yaml`, builds `Dockerfile`, and exposes the service over HTTPS.

Set these environment variables in Render:

- `MFP_USERNAME`
- `MFP_PASSWORD`
- `API_TOKEN`

Health check path: `/health`.

After deploy, open:

```text
https://<your-service>.onrender.com/health
```

Expected before Muse connector setup: the service responds and reports it is
configured when `API_TOKEN` is present.

## First login and reCAPTCHA

MyFitnessPal can challenge automated login with reCAPTCHA. The upstream
server uses Camoufox and a persistent browser profile. A local Docker host
can expose VNC on port 5900 so Nicolle can complete the first login manually.
Render free does not provide convenient VNC or persistent disk, so the first
practical test is environment credentials; if MyFitnessPal blocks the login,
use the local/VNC route or keep the service on a host with persistent storage.

## Generic Docker deploy

```bash
cp .env.example .env
# Fill values only on the host, never in git.
docker compose up -d --build
curl http://localhost:8000/health
```

## Muse connector setup

When the public HTTPS URL works, Muse registers a custom connector for the
service hostname with auth scheme API key and placement `bearer_header`. Enter
the API token only in Muse's secure form. Then Muse uses the workspace skill
to read the diary, search foods, and add entries.
