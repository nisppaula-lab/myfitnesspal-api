# MyFitnessPal API for Muse

REST bridge for Nicolle's MyFitnessPal account. Muse reaches this service
through a custom connector with a Bearer token. MyFitnessPal credentials stay
only in the host environment; Muse never receives them.

## Auth

All routes except `/health` and `/openapi.json` require:

```http
Authorization: Bearer <API_TOKEN>
```

## Environment variables

- `MFP_USERNAME`: MyFitnessPal email or username.
- `MFP_PASSWORD`: MyFitnessPal password for the account.
- `API_TOKEN`: long random bearer token Muse stores in the secure connector.
- `MFP_TRANSPORT`: keep `streamable-http`.
- `MFP_SYNC_DAYS`: optional, default 30.

Never commit real values. Keep them only in the host's secret manager.

## Endpoints

### GET /health

Returns `{ "status": "ok" }` from the upstream server and
`{ "ok": true, "configured": true/false }` from the REST wrapper when both are
mounted. The connector test should use `/diary?date=YYYY-MM-DD` after login.

### GET /diary?date=YYYY-MM-DD

Reads the food diary for one date.

### GET /foods/search?q=TEXT&limit=10

Searches the MyFitnessPal food database.

### GET /foods/{food_id}

Reads detailed nutrition for one food.

### POST /diary/entries

Adds a food to the diary. JSON body:

```json
{
  "food_id": "123456",
  "meal": "Lunch",
  "date": "2026-10-08",
  "quantity": 1,
  "unit": "container"
}
```

`meal` must be one of Breakfast, Lunch, Dinner, Snacks.

### POST /diary/entries/remove

Removes an entry by `entry_id`, or by `name_contains` + optional `meal`.
Use sparingly and only with an exact match.

### GET /water?date=YYYY-MM-DD

Reads water for a date.

### POST /water

Sets water for a date. JSON body: `{ "cups": 2, "date": "2026-10-08" }`.

### GET /goals?date=YYYY-MM-DD

Reads daily nutrition goals.

### POST /goals

Updates goals. JSON body may include `calories`, `protein`,
`carbohydrates`, and `fat`.

### GET /measurements?measurement=Weight&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

Reads body measurement history.

### POST /measurements

Logs a measurement for today. JSON body:
`{ "measurement": "Weight", "value": 154.0 }`.

## Deployment

Use `render.yaml` or the generic Docker flow in `DEPLOY.md`. The first login
may require MyFitnessPal reCAPTCHA. On Render free, use environment
credentials and expect the session to need refreshing after deploys or expiry;
a paid persistent disk or local Docker host keeps the browser profile longer.
