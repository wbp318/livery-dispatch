# Livery Dispatch — Production Deployment Runbook

End-to-end checklist for taking the dispatch backend from "runs locally on
the dispatcher's laptop" to "live on the public internet under
`DISPATCH_ENV=production`."

Scope: this is the **dispatch backend**, plus the private routing app it
calls ([`osrm/README.md`](osrm/README.md)). A marketing site that posts
to it is separate and not in this repository.

For the security posture and pre-launch security checklist, see
[`SECURITY.md`](SECURITY.md). DEPLOY.md and
SECURITY.md overlap intentionally: SECURITY.md is the auditor-facing
summary, DEPLOY.md is the ops-facing runbook with every command.

---

## Production topology

In production the static site stays on Netlify, the dispatch app and its
SQLite volume run on Fly.io next to a private OSRM app, and Twilio and the
browser push services are the only outside parties the app talks to:

```mermaid
flowchart LR
    subgraph people["People"]
        guest["Guest browser"]
        desk["Dispatcher browser<br/>/dashboard, /map, /admin"]
        pwa["Driver PWA<br/>chauffeur phone, /driver"]
        phone["Callers + SMS replies"]
    end
    subgraph netlify["Netlify"]
        site["Static marketing site<br/>example.com"]
        nforms["Netlify Forms<br/>fallback when dispatch is down"]
    end
    subgraph fly["Fly.io, region atl"]
        app["livery-dispatch<br/>FastAPI + uvicorn, HTTPS"]
        vol[("Volume dispatch_data<br/>/data/dispatch.db")]
        osrm["livery-osrm<br/>private, no public IPs"]
        maps["livery-maps<br/>QGIS Server: our basemap + overlays"]
    end
    twilio["Twilio<br/>voice + SMS"]
    pushsvc["Web push services<br/>FCM, Mozilla, Apple"]
    guest --> site
    site -->|"forms, /quote, /osrm<br/>allowed by CORS_ORIGINS"| app
    site -.->|"if the fetch fails"| nforms
    desk -->|"session cookie + CSRF"| app
    pwa -->|"driver token + CSRF"| app
    phone --> twilio
    twilio -->|"signed webhooks"| app
    app -->|"SMS + call TwiML"| twilio
    app --> vol
    app -->|"Flycast: livery-osrm.flycast"| osrm
    app -->|"VAPID push, flag-gated"| pushsvc
    desk -->|"basemap tiles (MAP_SERVER_URL)"| maps
    guest -->|"basemap tiles (data-qgis-url)"| maps
    pushsvc --> pwa
```

---

## What the fail-closed boot guard enforces

`_validate_production_config()` in `app.py` refuses to start when
`DISPATCH_ENV=production` is set but any of these are missing or weak.
Every line below corresponds to a `RuntimeError` you'll see if you try
to boot without it:

| Variable | Rule | Generate it |
|---|---|---|
| `ADMIN_USERNAME` | Non-empty string | Pick one. Not `admin` for production. |
| `ADMIN_PASSWORD_HASH` | bcrypt format (`$2a$` / `$2b$` / `$2y$`) | `python -c "from admin_auth import hash_password; print(hash_password('your-strong-password'))"` |
| `ADMIN_SESSION_SECRET` | ≥32 characters | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DRIVER_CSRF_SECRET` | ≥32 characters | Same as above (mint a fresh one — different from the session secret) |
| `CORS_ORIGINS` | No wildcards (`*`); pinned to live site origin(s) | `https://example.com,https://www.example.com` |
| `NOTIFICATION_PROVIDER` | `twilio` only (`console` refused in prod) | Set after Twilio account is provisioned |
| `TWILIO_AUTH_TOKEN` | Non-empty when provider is `twilio` | Twilio console → Account → Auth Token |
| `TWILIO_ACCOUNT_SID`, `TWILIO_FROM_NUMBER` | Non-empty when provider is `twilio` (v1.1.17, E4). Without them the app used to fall back to console SMS silently | Twilio console |
| `PUBLIC_BASE_URL` | `https://` origin, when provider is `twilio` (v1.1.17, E5) | `https://dispatch.example.com` |
| `CLIENT_IP_HEADER` | Always (v1.1.17, E5): the header your edge proxy sets to the client IP, or `none` | `Fly-Client-IP` |
| `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTACT_EMAIL` | Non-empty when `PUSH_NOTIFICATIONS_ENABLED=true` | See [Web push notifications](#web-push-notifications-v110-flag-gated-off-by-default) |
| `MAX_REQUEST_BYTES` | Optional (v1.4.1). If set, a whole number ≥ 1024; unset means 32768 | Leave unset. Bodies over the cap get a 413 before anything reads them |

All failures are collected and raised as one `RuntimeError: Production
configuration is incomplete…` listing each missing item (the CORS
wildcard check runs first, on its own).

One check only **warns**: while `OSRM_BASE_URL` still points at the public
OSRM demo, `_warn_on_public_osrm_in_production()` logs a warning at boot
instead of refusing, because every routing consumer degrades gracefully.
See [Routing — self-hosted OSRM](#routing--self-hosted-osrm-v1115-recommended-before-paying-customers).

The full set of env vars (including the optional / tunable ones) is in
[`.env.example`](.env.example) and [GUIDE.md § 10](GUIDE.md#10-environment-variables-and-configuration).

---

## Deployment target: Fly.io (recommended)

Three platforms can host this app well:

| Platform | Pros | Cons |
|---|---|---|
| **Fly.io** (recommended) | Anycast network, per-region deploy, persistent volumes for SQLite, private networking (Flycast) for the OSRM app, low pay-as-you-go cost at this scale, simple `flyctl deploy` | Fewer one-click integrations than Render |
| Render | Polished UI, managed Postgres included, easy GitHub integration | More expensive at the same scale; sleep on free tier breaks Twilio webhooks |
| Railway | Simple deploy, good DX | Recently changed pricing; less battle-tested for small ops |

Below is the Fly.io runbook. The other two follow the same shape — the
env-var list and Twilio configuration steps are identical.

---

## First deploy at a glance

The numbered steps below run in this order; the boot guard sends you back
to the secrets step until the configuration is complete:

```mermaid
flowchart TD
    s1["1. Generate secrets locally<br/>bcrypt hash + two 48-char tokens"] --> s2["2. flyctl apps create<br/>livery-dispatch; Dockerfile + fly.toml are committed"]
    s2 --> s3["3. Create volume dispatch_data<br/>mounted at /data"]
    s3 --> s4["4. flyctl secrets set<br/>admin, CSRF, CORS, Twilio, PUBLIC_BASE_URL"]
    s4 --> osrmq{"Self-host OSRM now?"}
    osrmq -->|"yes, recommended"| osrm["Deploy livery-osrm<br/>set OSRM_BASE_URL"]
    osrmq -->|"not yet"| s5
    osrm --> s5["5. flyctl deploy --ha=false"]
    s5 --> guard{"Boot guard passes?"}
    guard -->|"no: RuntimeError names what is missing"| s4
    guard -->|yes| s6["6. Schema: automatic<br/>boot.py ran alembic upgrade head"]
    s6 --> rc["7a. Load rate cards<br/>cli.py rates seed-defaults<br/>or /admin/rates/new"]
    rc --> s7["7b. Smoke test<br/>health, headers, gated surfaces,<br/>/quote resolves"]
    s7 --> s8["8. Custom domain + cert<br/>point the site's data attributes at it"]
    s8 --> s9["9. Twilio webhooks<br/>voice + SMS"]
    s9 --> s10["10. HSTS preload<br/>after 30 days"]
```

Push notifications stay off through all of this; they have their own
gated rollout ([below](#step-by-step-push-rollout-operational-checklist-v111)).

---

## One-time setup

### 1. Generate secrets

```powershell
# From the repository root, after `pip install -r requirements.txt`:
python -c "from admin_auth import hash_password; print(hash_password('CHANGE-ME-strong-pass'))"
python -c "import secrets; print(secrets.token_urlsafe(48))"  # ADMIN_SESSION_SECRET
python -c "import secrets; print(secrets.token_urlsafe(48))"  # DRIVER_CSRF_SECRET
```

Save these to a password manager. Never commit them.

### 2. Provision Fly.io app

```bash
# Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
flyctl auth login
cd dispatch
flyctl apps create livery-dispatch
```

`atl` (Atlanta, `primary_region` in `fly.toml`) is the closest region to
the Coastal GA / North FL corridor. Latency to Sea Island / Jekyll /
Jacksonville is single-digit ms.

**The image and the Fly config are committed (v1.1.21):**
[`Dockerfile`](Dockerfile), [`fly.toml`](fly.toml),
[`.dockerignore`](.dockerignore) and the entrypoint [`boot.py`](boot.py).
**Don't run `flyctl launch`**: it generates its own Dockerfile and
rewrites `fly.toml`. Build the image and boot it locally once before the first
`flyctl deploy`, so that isn't the first time it runs in production mode.
`tests/test_deploy_config.py` pins the Dockerfile and `fly.toml` against
each other.

| | What the image and `fly.toml` do |
|---|---|
| Base | `python:3.11-slim-bookworm` (CI's Python), plus the `sqlite3` CLI (backups below) and `tzdata` (quiet hours) |
| Packages | [`requirements-runtime.txt`](requirements-runtime.txt) only. `requirements.txt` includes it and adds pytest, bandit and pip-audit, which stay out of the image |
| Defaults | `DISPATCH_ENV=production` and `DATABASE_URL=sqlite:////data/dispatch.db` are baked in, so without the secrets in § 4 the container refuses to start |
| User | Starts as root only so `boot.py` can hand the root-owned `/data` volume to the `dispatch` user (uid 10001), then drops root for good. App code stays root-owned and read-only |
| Schema | `boot.py` runs `alembic upgrade head` on every start, before uvicorn (§ 6) |
| Process | One uvicorn worker on port 8080. Rate limits, the `/osrm` cache and SQLite all assume a single process |
| Machines | Exactly one, always running (`auto_stop_machines = "off"`, `min_machines_running = 1`): it answers calls, SMS webhooks and driver GPS at any hour. `/health` is the health check; `force_https` is on |
| `[env]` | The two non-secret settings: `CLIENT_IP_HEADER=Fly-Client-IP` and `NOTIFICATION_PROVIDER=twilio`. Everything secret goes in with `flyctl secrets set` (§ 4) |

SQLite on a persistent volume is the v1 database. Move to Postgres when
call volume justifies it (see [SQLite → Postgres](#sqlite--postgres-migration)).

**Behind Fly's proxy (handled in the app since v1.1.17, E5):** Fly
terminates TLS and forwards plain HTTP from its own address. The app no
longer depends on uvicorn trusting `X-Forwarded-*`. Two settings, both
**required by the boot guard in production**, cover it:

- **`PUBLIC_BASE_URL`**, for example `https://dispatch.example.com`.
  Twilio webhook signatures are checked against `PUBLIC_BASE_URL + path`,
  because that is the URL Twilio signed. Use the exact origin you enter in
  the Twilio console.
- **`CLIENT_IP_HEADER=Fly-Client-IP`**, already set in `fly.toml`'s
  `[env]`. Per-IP rate limits key on the header Fly's edge sets to the
  real client address, so visitors don't share one bucket. A
  client-supplied `X-Forwarded-For` is ignored.
  ⚠️ Planning-grade: confirm on the first deploy that Fly *overwrites*
  `Fly-Client-IP` on every request (`flyctl logs` while you hit the site
  with a hand-set `Fly-Client-IP` header).

You don't need `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`. Leave
uvicorn's default; trusting `'*'` would let any client spoof the
rate-limit key through `X-Forwarded-For`.

### 3. Attach a persistent volume for SQLite

```bash
flyctl volumes create dispatch_data --region atl --size 1
```

`fly.toml` already mounts `dispatch_data` at `/data`, and the image's
`DATABASE_URL=sqlite:////data/dispatch.db` points there (four slashes:
three for `sqlite://` plus one for the absolute path). It also keeps **30
days of daily volume snapshots** (`snapshot_retention = 30`; Fly's default
is 5). Check after the first deploy: `flyctl volumes list` for the volume
id, then `flyctl volumes snapshots list <volume-id>` the next day. See
[Backups](#backups-sqlite).

**Run exactly one machine.** A volume belongs to one machine, so a second
machine would get its own volume and its own, different database. That's
why the deploy command below is `flyctl deploy --ha=false`. Never
`flyctl scale count 2` this app while it is on SQLite; `flyctl scale count 1`
fixes it if a second machine ever appears.

### 4. Set production secrets via `flyctl secrets`

Each secret is set individually and triggers a redeploy:

`DISPATCH_ENV=production` and `DATABASE_URL` come from the image, and
`CLIENT_IP_HEADER` and `NOTIFICATION_PROVIDER` from `fly.toml`. Don't set
those four as secrets too.

```bash
flyctl secrets set ADMIN_USERNAME=your-chosen-username
flyctl secrets set ADMIN_PASSWORD_HASH='$2b$12$...your-hash-here...'
flyctl secrets set ADMIN_SESSION_SECRET='paste-48-char-token-here'
flyctl secrets set DRIVER_CSRF_SECRET='paste-48-char-token-here'
flyctl secrets set CORS_ORIGINS='https://example.com,https://www.example.com'
flyctl secrets set TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
flyctl secrets set TWILIO_AUTH_TOKEN=your-twilio-token
flyctl secrets set TWILIO_FROM_NUMBER=+19125551234
flyctl secrets set PUBLIC_BASE_URL=https://dispatch.example.com  # the origin Twilio calls (v1.1.17)
flyctl secrets set DISPATCHER_PHONE=+19045550000  # head dispatcher's mobile (E.164)
```

**Quoting note (Windows PowerShell):** the bcrypt hash contains `$`
characters. PowerShell expands `$` in double-quoted strings — use
single quotes (`'`) around the value to preserve it literally. The
example above shows single quotes; that is correct.

Verify with `flyctl secrets list` — values are not echoed back.

#### Routing — self-hosted OSRM (v1.1.15, recommended before paying customers)

`OSRM_BASE_URL` drives **both** fare distance and the `/osrm` proxy that
`/map` (and, once `data-osrm-url` is set, the marketing-site maps) draw
road routes through. Left unset it is the public OSRM demo — non-commercial
use only, \~1 req/s, no SLA — and the app logs a boot **warning** in
production (deliberately not a refusal: every routing consumer degrades to
haversine / straight lines, so it must not block a soft launch). Stand up
the private corridor OSRM app and point the secret at it. From the repository root
(where step 2 left you):

```bash
# Full runbook + verification: osrm/README.md
cd osrm
flyctl apps create livery-osrm
flyctl deploy --no-public-ips              # remote build, ~10–20 min the first time
flyctl ips allocate-v6 --private           # Flycast: http://livery-osrm.flycast
flyctl ips list                            # expect only a private v6 address
cd ..
flyctl secrets set -a livery-dispatch OSRM_BASE_URL=http://livery-osrm.flycast
```

Once the dispatch app is up (step 7), the proxy check is
`curl -i "https://<dispatch>/osrm/route/v1/driving/-81.6879,30.4941;-81.3438,31.1969?overview=false"`:
`"code":"Ok"` the first time, `X-Cache: HIT` on a repeat, and no OSRM
warning in `flyctl logs`.

#### Map server — our own basemap (v1.3.0, recommended before launch)

The dispatcher's `/map` and the site's six maps used CARTO's free dark tiles,
which now return an "API KEY REQUIRED" placeholder. They now draw **our own
map server** when it's configured: QGIS Server with a dark corridor basemap
built from OpenStreetMap plus our public drive-time overlays. Until then they
use OpenStreetMap's standard tiles, darkened in the browser, and production
logs a boot **warning**. OSM's tile policy discourages heavy commercial use,
so deploy the server before real traffic. From the repo root:

```bash
# Full runbook + checks: gis/server/README.md
flyctl apps create livery-maps
flyctl deploy gis --config gis/server/fly.toml --dockerfile gis/server/Dockerfile   # ~15 min the first time
flyctl secrets set -a livery-dispatch MAP_SERVER_URL=https://livery-maps.fly.dev
```

Then set `data-qgis-url="https://livery-maps.fly.dev"` on `#map` in the six
site map pages, and pin `NGINX_CORS_ALLOW_ORIGIN` in `gis/server/fly.toml` to
the site origin. `MAP_SERVER_URL` must be a bare `https://` origin; anything
else is ignored (and the CSP only ever allows that origin). The server
publishes public layers only; the internal QGIS project never ships.

#### Web push notifications (v1.1.0, flag-gated, off by default)

Push lands as code in v1.1.0 but defaults to disabled. Flip the flag only
after you've done the live phone smoke test of the v0.9.14 PWA shell
(add `/driver` to home screen, log in, walk a fake trip) so you know the
chauffeur UX feels right before push complexity stacks on top.

To enable push:

```bash
# Generate a VAPID keypair (one time):
python -c "from py_vapid import Vapid; v = Vapid(); v.generate_keys(); \
  print('PRIVATE:', v.private_pem().decode()); \
  print('PUBLIC:', v.public_key.public_bytes_raw().hex())"

flyctl secrets set PUSH_NOTIFICATIONS_ENABLED=true
flyctl secrets set VAPID_PRIVATE_KEY='-----BEGIN EC PRIVATE KEY-----...'
flyctl secrets set VAPID_PUBLIC_KEY='paste-public-key-here'
flyctl secrets set VAPID_CONTACT_EMAIL='mailto:desk@example.com'
```

The fail-closed boot guard (v1.0.3) refuses to start in production when
`PUSH_NOTIFICATIONS_ENABLED=true` and any VAPID variable is missing.

**Rotating the VAPID keypair** invalidates every existing browser
subscription — chauffeurs have to tap "Enable notifications" again. Plan
rotations during low-traffic windows; document them in the dispatcher
ops log so chauffeurs know to re-enable.

**Behavior with the flag on:** for each driver in a broadcast fanout, if
they have an active push subscription, the dispatcher's push provider
sends them push (skipping SMS). If they don't, SMS as before. If push
delivery fails for transient reasons, SMS fires as fallback. If the push
service returns 410/404 (subscription gone), the dispatcher deletes the
stale row in the same transaction and SMS as fallback.

**Cascade-delete:** `POST /admin/drivers/{id}/action/rotate-token` deletes
every push subscription for that driver as a side effect — rotating the
bearer token means "this device is no longer trusted" and the same logic
applies to its push subscription. Chauffeurs have to re-enable on each
device.

#### Step-by-step push rollout (operational checklist, v1.1.1)

The brief enumerated list in the v1.1.0 release notes is a six-bullet
recap. This is the runbook — what to actually do when you sit down to
flip the flag for the first time. Block out \~90 minutes the first time
you do this, \~20 minutes for any subsequent re-rollouts (key rotation,
re-onboarding chauffeurs after a token rotation, etc.).

Each step below has a gate; a failed gate loops back rather than moving
on, and the flag can be turned off at any point:

```mermaid
flowchart TD
    g1{"Step 1 gate: live phone smoke test<br/>passed on an iPhone and an Android?"}
    g1 -->|no| fixux["Fix the PWA nits first<br/>push stays off, SMS only"]
    fixux --> g1
    g1 -->|yes| keys["Step 2: generate the VAPID keypair<br/>locally, store it in the vault"]
    keys --> sec["Step 3: set the four secrets<br/>flag + three VAPID values"]
    sec --> boot{"App boots and vapid-key<br/>returns enabled true?"}
    boot -->|no| fixsec["Fix the secret the error names<br/>or set the flag back to false"]
    fixsec --> sec
    boot -->|yes| enable["Step 4: chauffeurs tap<br/>Enable notifications"]
    enable --> rows{"Subscription row exists<br/>for each device?"}
    rows -->|no| enable
    rows -->|yes| dry["Step 5: dry run with one chauffeur<br/>ROLLOUT TEST trip"]
    dry --> pushok{"Push within 3-5 s, no SMS,<br/>tap opens the trip?"}
    pushok -->|no| logs["Read flyctl logs for webpush errors"]
    logs --> dry
    pushok -->|yes| expire{"Revoked permission:<br/>row deleted and SMS fallback?"}
    expire -->|no| logs
    expire -->|yes| steady["Step 6: steady state<br/>push first, SMS fallback"]
    steady --> trigger{"Rollback trigger?<br/>missed broadcasts, push 5xx, risky peak"}
    trigger -->|no| steady
    trigger -->|yes| off["Set PUSH_NOTIFICATIONS_ENABLED=false<br/>instant, SMS for everyone"]
```

##### Quick smoke-test checklist (\~15 min)

The condensed turnkey path once you've done the full rollout once. Steps 1–6 below stay the deep reference — read them the first time, and whenever a step here fails. **Flipping the flag is a deliberate go/no-go decision**: do not enable push in production until the live phone smoke test of the v0.9.14 PWA shell has passed (that's Step 1 below — the gate). With the flag off, broadcast always uses SMS, so steps 1–3 dry-run safely.

1. **Generate the VAPID keypair** — one time; reuse forever (rotating it forces every chauffeur to re-tap "Enable notifications"). Run locally, not on the Fly console:
   ```powershell
   # from the repository root, after `pip install -r requirements.txt`
   python -c "from py_vapid import Vapid; v = Vapid(); v.generate_keys(); print('PRIVATE:', v.private_pem().decode()); print('PUBLIC:', v.public_key.public_bytes_raw().hex())"
   ```
   - **Success:** prints a `PRIVATE:` PEM block + a `PUBLIC:` hex string — save both to the password manager. If keys already exist in the vault, reuse them.
   - **Fail:** `ModuleNotFoundError: py_vapid` → venv inactive or deps missing (`pip show py-vapid`, expect `>=1.9`).

2. **Set the four secrets** (each triggers a rolling redeploy):
   ```bash
   flyctl secrets set PUSH_NOTIFICATIONS_ENABLED=true VAPID_PUBLIC_KEY='<hex>' VAPID_CONTACT_EMAIL='mailto:desk@example.com' --app livery-dispatch
   flyctl secrets set VAPID_PRIVATE_KEY='-----BEGIN EC PRIVATE KEY-----
   ...paste PEM, newlines intact...
   -----END EC PRIVATE KEY-----' --app livery-dispatch
   flyctl secrets list --app livery-dispatch   # confirm all four names present
   ```
   - **Success:** `secrets list` shows `PUSH_NOTIFICATIONS_ENABLED`, `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTACT_EMAIL`.
   - **Fail:** the flag is case-sensitive (`true`, not `True`). Single-quote the PEM so its newlines survive.

3. **Confirm boot + push live:**
   ```bash
   flyctl logs --app livery-dispatch | head -50          # want "Application startup complete."
   curl https://livery-dispatch.fly.dev/driver/push/vapid-key -H "Authorization: Bearer <a-real-driver-token>"
   ```
   - **Success:** the curl returns `{"publicKey": "...", "enabled": true}`.
   - **Fail:** `RuntimeError: Production configuration is incomplete` → the fail-closed boot guard caught a missing VAPID var (the error names it). `{"enabled": false}` → the flag secret didn't land. **Instant rollback:** `flyctl secrets set PUSH_NOTIFICATIONS_ENABLED=false --app livery-dispatch` reverts to SMS-only, no other state change.

4. **Enable notifications on a real phone.** Open the driver dashboard from the home-screen icon (iOS 16.4+ — Add to Home Screen first; push won't prompt in a plain Safari tab) or `/driver` in Chrome (Android), log in with a test driver's token, tap **Enable notifications** → **Allow**.
   - **Success:** the button disappears and the status reads "Notifications enabled." — a `POST /driver/push/subscribe` persisted a row.
   - **Fail:** no button → step 3 isn't actually live (the client reveals it only when `/driver/push/vapid-key` returns `enabled:true`); hard-refresh the PWA.

5. **Broadcast a test trip.** Create a low-stakes SUV trip (note it "ROLLOUT TEST — do not drive") and broadcast it to the subscribed test driver.
   - **Success:** a push arrives in 3–5s; tapping it opens `/driver/trips/{id}` (the trip detail page, not the dashboard root); no SMS fires to that driver.
   - **Fail:** no push within \~15s → `flyctl logs --app livery-dispatch | grep -i webpush` for `pywebpush` errors (usually VAPID misconfig). A 410/404 from the push service means a stale subscription — the server deletes the row inline and falls back to SMS; redo step 4 to re-subscribe.

6. **Confirm SMS fallback.** Broadcast a trip to a second test driver that has **no** push subscription → they must receive it via SMS. (No SMS → a Twilio / `NOTIFICATION_PROVIDER` issue, independent of push.)

If all six pass, push is operationally ready — leaving the flag on for real traffic is still that go/no-go decision. Tear down the test driver + trips, and record the enable date + subscribed devices in your operations log per "What to write down after the rollout" below.

##### Step 1 — Live phone smoke test (the gate)

The single most important step. Without this, you're shipping push on
top of a PWA shell that's never been touched by a human thumb. UX nits
the chauffeur will hit don't show up in Playwright.

**Devices to test on:**

- One **iPhone with iOS 16.4 or newer** — iOS Safari only added Web Push
  in 16.4 (March 2023). Older iPhones cannot receive push at all; SMS
  remains their channel.
- One **Android phone running Chrome** (any modern version). Push has
  worked on Android for years.

**What to do (\~25 minutes per device):**

1. **Mint a test driver row.** The admin pages have no "new driver"
   form, so use the CLI on the machine:
   ```bash
   flyctl ssh console --app livery-dispatch
   cd /app        # the image's WORKDIR, where cli.py and alembic.ini live
   python cli.py drivers add "SMOKE TEST" "+19125550123" --classes suv
   python cli.py drivers status <id> available --lat 31.1499 --lon -81.4915
   ```
   Type the phone in exact E.164 (`+1` + 10 digits): the CLI stores it
   as given, and SMS replies only match when `From` equals it exactly.
   The driver needs `available` status and a location inside the
   broadcast radius to receive a broadcast. Then open
   `/admin/drivers/<id>` and copy the full bearer token from the
   `<code>` block (the page mints one on first view).
2. **Open `/driver/login` on the mobile browser.** Paste the token
   into the field, submit. You should land on the driver dashboard.
3. **iOS only — Add to Home Screen FIRST.** Tap the Safari share icon
   → "Add to Home Screen". This is non-negotiable for iOS push: Safari
   refuses `Notification.requestPermission()` from a browser tab. The
   chauffeur must launch the app from the home-screen icon. **Document
   this in your driver-onboarding notes so the
   real chauffeurs aren't surprised.**
4. **Verify the dashboard renders cleanly.** Tap targets should be
   thumb-sized (≥44px), no double-tap-zoom on text, the status pill
   readable in sunlight, the brand wordmark legible. If anything feels
   cramped or unclear, **write it down** before continuing.
5. **From a separate browser tab on the admin desk, create a fake
   trip + broadcast.** Use real coordinates from the corridor (e.g.
   JAX → Cloister). The driver's phone should buzz with an SMS
   (push is still off at this stage — that's the point).
6. **Accept from SMS.** Reply `ACCEPT N` (where N is the trip id),
   OR tap the Accept button in the driver dashboard. The status pill
   should flip to "Accepted" within \~1 second.
7. **Walk the status lifecycle.** Open the trip from the driver
   dashboard and tap "Mark on the way". Then "Mark arrived". Then
   "Start trip". Then "Mark complete". Each transition should be one
   tap, with no confirmation modal, and should visibly update the chip
   color + status name.
8. **Verify GPS on `/map`.** From the admin desk's `/map` page, look
   for the driver's pin updating its position every \~30 seconds. The
   first ping should fire within 5 seconds of trip acceptance. If
   the pin doesn't appear, check that the chauffeur granted location
   permission when the browser prompted.

**What "feels right" means:**

- The chauffeur never has to read instructions.
- Every button does exactly what its label says.
- The keyboard doesn't pop up unexpectedly.
- The viewport doesn't zoom on tap, jiggle, or scroll horizontally.
- The accept button is impossible to miss.

**Document any nits** in your operations log BEFORE continuing to step 2. If the walking
skeleton has UX problems, push notifications won't fix them — they'll
amplify them.

**Tear down:** delete the SMOKE TEST driver and the fake trip rows
from the admin surface (or the test DB).

##### Step 2 — Generate the VAPID keypair (one time)

Done once per production deployment. The keypair lives in your
password manager, not in git, not in Slack, not in email.

```bash
# Run on your local machine (NOT on the Fly machine — the Fly app
# doesn't have py_vapid installed unless you ship it in requirements).
python -c "from py_vapid import Vapid; v = Vapid(); v.generate_keys(); \
  print('PRIVATE:'); print(v.private_pem().decode()); \
  print('PUBLIC:'); print(v.public_key.public_bytes_raw().hex())"
```

Output looks like:

```
PRIVATE:
-----BEGIN EC PRIVATE KEY-----
MHcCAQE...
-----END EC PRIVATE KEY-----

PUBLIC:
04a7b1c2d3...
```

**Store these in 1Password (or whichever vault you use).** Title the
entry "Livery Dispatch VAPID — production". Add a note with the date
generated and which Fly app it's bound to.

**What NOT to do:**

- ❌ Commit the keys to git, even briefly. Once they're in a commit,
  they're in the reflog, the GitHub mirror, the GHA cache, etc.
- ❌ Paste in Slack / Telegram / email. These leak.
- ❌ Generate on a shared machine (don't run the command on the Fly
  console — that leaves it in shell history).
- ❌ Reuse the same keypair across multiple environments. If you ever
  spin up a staging instance, generate a fresh keypair for it.

**Verify locally before deploying** (optional but recommended): set
the env vars in a local `.env`, start `uvicorn`, hit
`GET /driver/push/vapid-key` with a driver token — should return
`{"publicKey": "04a7...", "enabled": true}`. If it doesn't, the env
vars aren't being read; fix that locally before touching production.

##### Step 3 — Set the four secrets on Fly

One `flyctl secrets set` per variable. Each one triggers a rolling
redeploy, which is fine — Fly handles concurrent secret-sets by
batching them when run close together, but explicit one-at-a-time
makes the failure mode obvious if you typo'd one.

```bash
# Run from any machine with flyctl auth + the app name configured.
flyctl secrets set PUSH_NOTIFICATIONS_ENABLED=true --app livery-dispatch
flyctl secrets set VAPID_PRIVATE_KEY='-----BEGIN EC PRIVATE KEY-----
...
-----END EC PRIVATE KEY-----' --app livery-dispatch
flyctl secrets set VAPID_PUBLIC_KEY='04a7b1c2d3...' --app livery-dispatch
flyctl secrets set VAPID_CONTACT_EMAIL='mailto:desk@example.com' --app livery-dispatch
```

**PowerShell quoting note:** the private key spans multiple lines and
contains `$`-free content (EC keys don't use `$`, unlike bcrypt
hashes), so PowerShell single-quotes work. The newlines inside the
private key DO need to be preserved — paste the key with its
linebreaks intact, surrounded by single quotes, and PowerShell will
hand the multi-line string to flyctl correctly. If you're on bash,
same idea: single-quote the whole thing.

**Verify the secrets are set** (values not echoed):

```bash
flyctl secrets list --app livery-dispatch
```

You should see four new entries: `PUSH_NOTIFICATIONS_ENABLED`,
`VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTACT_EMAIL`. Each
line shows the secret name + a digest, never the value.

**Verify the app booted cleanly:**

```bash
flyctl logs --app livery-dispatch | head -50
```

Look for `INFO:     Application startup complete.` and the absence of
`RuntimeError: Production configuration is incomplete`. If the
fail-closed guard fires, the bullet list in the error message tells
you exactly which VAPID variable is missing or malformed — fix the
typo and re-set that one secret.

**Verify push is now reachable** (smoke test from any machine):

```bash
# Should return enabled: true now. The publicKey field is what the
# browser uses to subscribe.
curl https://livery-dispatch.fly.dev/driver/push/vapid-key \
  -H "Authorization: Bearer <a-real-driver-token>"
```

If this returns `{"enabled": false}`, the `PUSH_NOTIFICATIONS_ENABLED`
secret didn't take effect — check `flyctl secrets list` for typos
(case matters: `True` ≠ `true`).

##### Step 4 — Chauffeurs tap "Enable notifications"

Each device the chauffeur wants reachable is a separate subscription.
A chauffeur with an iPhone + a tablet ends up as two rows in
`driver_push_subscriptions`.

**What they see** (after step 3 is done):

1. They open their driver dashboard from the home-screen icon (iOS)
   or just `/driver` in Chrome (Android).
2. A new section appears below "You" with a brass **Enable
   notifications** button and the caption *"Enable push so the
   dispatcher can reach you when SMS is slow."*
3. They tap **Enable notifications**.
4. The browser prompts: *"Allow Livery Dispatch to send you
   notifications?"* — they tap **Allow**.
5. The button disappears and the status line reads *"Notifications
   enabled. The dispatcher will reach you here."*

**iOS-specific gotchas** (you will hit these — they're not bugs):

- The "Enable notifications" button does nothing if the chauffeur is
  in a plain Safari tab. They must **Add to Home Screen** first, then
  launch the dashboard from the home-screen icon. If they don't, the
  button still appears but the permission prompt never fires.
- iOS Safari sometimes silently denies permission if the user has
  declined notifications globally. Have them check Settings → Safari
  → Advanced → Website Data is not blocking your domain, and
  Settings → Notifications → [Driver] allows alerts.
- Force-quitting the PWA from the app switcher can revoke the
  subscription on next launch — iOS Push is finicky here. The
  dispatcher's stale-subscription cleanup handles it automatically
  on the next broadcast.

**Android (Chrome) is much simpler:** permission prompt → Allow →
done. No home-screen-add requirement.

**If the chauffeur declines permission:** nothing breaks. SMS remains
their channel. They can re-tap the button later if they change their
mind. Permission denied is a permanent browser-side state for that
origin; they have to clear it in browser settings to retry.

**Verify each driver actually has a subscription row** (admin desk):

```sql
-- From flyctl ssh console + sqlite3 /data/dispatch.db
-- (the image ships the sqlite3 CLI since v1.1.21):
SELECT d.name, d.phone, COUNT(s.id) AS subscriptions
FROM drivers d
LEFT JOIN driver_push_subscriptions s ON s.driver_id = d.id
GROUP BY d.id;
```

A chauffeur with 0 subscriptions still gets SMS. A chauffeur with 1
gets push on that one device. The dispatcher doesn't need to take
any action either way.

##### Step 5 — Watch a real broadcast (the dry-run)

Do this with **one chauffeur** subscribed first, not all four. If
push delivery has an issue you didn't catch in steps 1-4, you don't
want to find out during a real RSM Sunday departure.

**Setup:**

1. Pick one chauffeur who's already on shift (so the broadcast feels
   real to them).
2. Confirm they've completed step 4 — the `driver_push_subscriptions`
   table has at least one row for their driver_id.
3. From the admin desk, create a real-but-low-stakes trip: a known
   internal address pair, vehicle class SUV. Pickup time 30 minutes
   from now. Mark it clearly in the notes as "ROLLOUT TEST — do not
   actually drive."

**What to watch for:**

| Signal | Expected | Failure mode |
|---|---|---|
| Push notification appears on chauffeur's phone | Within 3–5 seconds of `POST /trips/{id}/broadcast` | If >15 seconds: check `flyctl logs` for `pywebpush` errors; could be VAPID misconfiguration or push service throttling |
| Notification body | "Trip #N — SUV — JAX → Cloister..." with distance | Truncated text on iOS means the payload exceeds the body length; report the trip's pickup_label length |
| Tap notification → opens `/driver/trips/{id}` | Trip detail page loads | If it opens `/driver` (dashboard) instead, the `notificationclick` handler's URL routing is wrong; check Service Worker logs |
| SMS does NOT also fire to this driver | No SMS at all (push is the only channel for subscribed drivers) | If SMS also fires, the broadcast loop's `pushed` flag isn't being set correctly — file a bug |
| `last_success_at` on the subscription row updates | Current UTC timestamp | If null after a successful push, the in-memory commit didn't reach the DB |

**Then test the expired-subscription cleanup path:**

1. On the chauffeur's phone, go to browser settings → Notifications →
   revoke permission for the dispatch domain.
2. From the admin desk, broadcast a second trip to this driver.
3. Watch the broadcast log. Expected sequence:
   - Push send attempt → push service returns 410 (Gone)
   - `WebPushResult(expired=True)` triggers row delete
   - Broadcast loop falls back to SMS for this driver
   - The chauffeur gets the SMS they would've gotten before push was on
4. Verify the row is gone:
   `SELECT * FROM driver_push_subscriptions WHERE driver_id = ?;`

If the cleanup didn't fire (subscription row still there), the
`expired` flag detection is broken — investigate before scaling to
more chauffeurs.

##### Step 6 — Push is the primary channel (steady state)

After step 5 passes, push is operationally live. The dispatcher's
view doesn't change — they still hit "Broadcast" the same way; the
system picks push or SMS per-driver based on subscription state.

**What "primary" means operationally:**

- For each driver in a broadcast fanout, the system tries push first
  if they have a subscription, otherwise SMS.
- Push success → SMS skipped for that driver only.
- Push failure (network, throttling, expired) → SMS fires as fallback,
  the driver is reached on at least one channel.
- A chauffeur who never subscribes still gets SMS — push doesn't
  exclude anyone.

**Monitoring (informal, no dashboard yet):**

- **SMS bill** is the leading indicator. Push adoption should reduce
  Twilio SMS volume as more chauffeurs subscribe. If it's flat,
  chauffeurs aren't actually opted in (look at the
  `driver_push_subscriptions` table).
- **Chauffeur complaints** ("I didn't get the broadcast") are the
  trailing indicator. Most likely cause: permission revoked
  browser-side without re-subscribing. Resolution: have them
  re-tap "Enable notifications", or revert them to SMS-only — from
  their device (`DELETE /driver/push/subscribe`), or from the admin
  desk with **Rotate token** on their driver page, which deletes every
  subscription for that driver (they then need the new token).
- **`flyctl logs --app livery-dispatch | grep -i 'webpush\|push send'`**
  surfaces failures in real time. Build a habit of checking this
  before every peak week.

**When to roll back (set `PUSH_NOTIFICATIONS_ENABLED=false`):**

- ≥3 chauffeurs report missed broadcasts on the same day
- pywebpush is returning 5xx from FCM/Mozilla/Apple consistently
- You're about to do a critical broadcast (RSM Sunday-evening
  departure) and you're not confident push is delivering — flip to
  SMS-only for the peak window, then re-enable after

**Rollback is instant and safe:**

```bash
flyctl secrets set PUSH_NOTIFICATIONS_ENABLED=false --app livery-dispatch
```

The next broadcast uses SMS for everyone. No other state change
needed; subscription rows stay in the DB so re-enabling later is
zero-friction.

##### What to write down after the rollout

Record in your operations log:

1. Any UX nits found in step 1 (font sizes, button placement,
   confusing labels).
2. The date push was enabled.
3. The list of chauffeurs subscribed and on which devices.
4. Any failure modes observed during the dry-run (step 5) and how
   they were resolved.
5. The next planned VAPID key rotation date (defaults to "when
   leaked or when leaving the role of head dispatcher" — there's
   no calendar-based rotation requirement at this scale).

### 5. First deploy

```bash
cd dispatch
flyctl deploy --ha=false
```

This builds the committed [`Dockerfile`](Dockerfile), pushes it, and starts
one machine. In `flyctl logs` a good boot reads, in order:

```
boot: dropped root; running as dispatch (uid 10001)
INFO  [alembic.runtime.migration] Running upgrade  -> dc2823d2aef2, ...   (fresh volume only)
boot: schema at alembic head
INFO:     Application startup complete.
```

Watch for the fail-closed guard. If you forgot any of the env vars above,
uvicorn exits with `RuntimeError: Production configuration is
incomplete...` and the bullet list of what's missing. Fix and redeploy.

### 6. Database schema (Alembic — v1.0.12+)

The dispatch backend uses [Alembic](https://alembic.sqlalchemy.org/) for
schema migrations. Every schema change since v1.0.12 is a migration file
under `alembic/versions/`.

**Migrations run automatically (v1.1.21).** The image's entrypoint,
[`boot.py`](boot.py), runs `alembic upgrade head` on every start, before
uvicorn, on the machine that has the volume:

```mermaid
flowchart TD
    boot0["Machine starts<br/>python boot.py, as root"] --> vol["chown /data and dispatch.db*<br/>to the dispatch user"]
    vol --> drop["Drop root for good<br/>setgroups, setgid, setuid"]
    drop --> look{"What is in the database?"}
    look -->|"empty volume"| up["alembic upgrade head<br/>builds every table"]
    look -->|"alembic_version at head"| run
    look -->|"alembic_version at an older known revision"| copy{"Copy the database to<br/>/data/backups/ first"}
    copy -->|"copy made and integrity-checked"| up2["alembic upgrade head<br/>applies the new migrations"]
    copy -->|"copy failed"| stop["Refuse to start<br/>nothing migrated"]
    look -->|"tables but no alembic_version"| refuse["Refuse to start<br/>stamp the right revision by hand"]
    look -->|"a revision this image does not know"| ahead["Warn and skip migrations<br/>a rollback: older code, newer schema"]
    up --> run["exec uvicorn app:app<br/>one worker, port 8080"]
    up2 --> run
    ahead --> run
```

So a fresh volume is built straight to head (the app's boot-time
`create_all` then finds nothing to do), and a release that ships a
migration, such as v1.1.20's `trips.completed_at` column, applies it on
its first boot. There is no step to remember, and no window where new code
runs on an old schema. `flyctl logs` shows `boot: schema at alembic head`.

**A copy before every migration (v1.4.1).** When the database is at an
older revision, boot first copies it to
`/data/backups/dispatch-pre-migration-<revision>-<UTC time>.db` (the
newest ten are kept) and logs `boot: backed up the database before
migrating off <revision>`. If the copy can't be made, usually because the
volume is full, boot stops **before migrating** and says so; free space
(`flyctl ssh console -C "df -h /data"`, delete old copies in
`/data/backups/`) and redeploy. `SKIP_PRE_MIGRATION_BACKUP=1` (a secret or
`[env]` entry) migrates without the copy; use it only on purpose, and
remove it afterwards. To undo a migration that went wrong, restore that
copy ([Backups](#backups-sqlite)).

It isn't a `fly.toml` `release_command` on purpose: Fly runs release
commands on a temporary machine *without* the app's volumes, so it would
migrate a throwaway empty SQLite file.

**If boot refuses with "tables but no alembic_version":** the database was
built by `create_all` and never stamped (the pre-v1.1.21 runbook did that
by hand). Guessing a revision could skip a migration, so stamp the one
that matches the schema, then redeploy. To run Alembic by hand, SSH in:

```bash
flyctl ssh console --app livery-dispatch
cd /app            # the image's WORKDIR; `ls alembic.ini` should succeed here
alembic current    # expect the newest revision, marked (head)
```

**Existing deployments (pre-v1.0.12 schema)** — the first time you upgrade
to v1.0.12, the database already has all the tables `database.init_db()`
created via `create_all`. Tell Alembic the baseline is already applied so
it doesn't try to re-create them:

```bash
alembic stamp dc2823d2aef2
# Then for any future migrations:
alembic upgrade head
```

For Postgres deployments (Fly Postgres, Supabase, Neon), the same commands
run from anywhere with network reach to the DB.

**Local dev (SQLite)** — `init_db()` still runs `create_all` on boot for
zero-friction local development; Alembic is the canonical path for
production schema evolution. The two coexist because the ORM definitions
in `models.py` are the single source of truth — `tests/test_alembic.py`
checks that `alembic upgrade head` on an empty database produces the
same schema as `create_all`.

**Generating a new migration** after a model change:

```bash
DATABASE_URL=sqlite:///./dispatch.db alembic revision --autogenerate -m "describe_change"
# Review the generated file under alembic/versions/ — autogenerate gets
# most things right but misses ENUM value additions and some constraint
# renames. Always read the diff before committing.
alembic upgrade head
```

### 7. Smoke-test the live deploy

**First, load rate cards (fresh volume only).** A new production database
has no rate cards, so the public `/quote` estimate soft-fails ("Estimate
unavailable…") and the trip page's **Quote fare** has nothing to price
with. `seed.py` is dev-only, but since v1.1.18 the planning-grade SUV +
Sprinter cards load on their own:

```bash
flyctl ssh console --app livery-dispatch
cd /app
python cli.py rates seed-defaults   # "Seeded 2 planning-grade rate cards: …"
python cli.py rates list            # confirm both cards are active
```

`flyctl ssh console` logs in as root, and the CLI works fine as root. The
database file keeps its owner when root writes to it, and the next boot
hands anything root created on the volume back to the `dispatch` user.

`rates seed-defaults` does nothing when any card already exists ("N rate
card(s) already exist; nothing seeded."), so it's safe to re-run and never
overwrites the dispatcher's edits. The alternative is to sign in and use
**+ New rate card** at `/admin/rates/new`. Either way, the numbers are
sample values until you replace them; edit them at
`/admin/rates`.

Then check the deploy:

```bash
# /health returns 200 with the version from app.py's FastAPI(version=...)
curl https://livery-dispatch.fly.dev/health
# Expect: {"status":"ok","service":"livery-dispatch","version":"1.1.21"}
# (the version should match the release you just deployed)

# Security headers present
curl -I https://livery-dispatch.fly.dev/health | grep -iE 'content-security|strict-transport|x-frame|referrer'

# /admin/login renders (200 with the form; 303 to /dashboard if already signed in)
curl -i https://livery-dispatch.fly.dev/admin/login

# /quote resolves now that rate cards exist (expect "resolved": true, "From $…")
curl -sS -X POST https://livery-dispatch.fly.dev/quote \
  -H "Content-Type: application/json" \
  -d '{"pickup_label":"JAX","dropoff_label":"The Cloister","vehicle_pref":"escalade-esv"}'
```

The first time you load `/admin/login` in a browser, log in with the
`ADMIN_USERNAME` + plaintext password you used to generate the hash. If
login fails, you used the wrong plaintext when generating the hash —
regenerate and re-set `ADMIN_PASSWORD_HASH`.

### 8. Configure custom domain (optional but recommended)

```bash
flyctl certs create dispatch.example.com
# Follow the DNS instructions to add the CNAME at your registrar.
```

After DNS propagates, the app is reachable at
`https://dispatch.example.com` with a Let's Encrypt cert.
`CORS_ORIGINS` lists the *marketing site's* origins (the browsers that
call in), not the dispatch host — it needs no change for the new domain.
What does need changing is the marketing site:

- `data-dispatch-url="https://dispatch.example.com"` on every form:
  the reserve form on `index.html` and the five resort pages, both forms
  on `concierge.html`, and the form on `account.html`.
- `data-osrm-url="https://dispatch.example.com/osrm"` on the `#map`
  div of `corridor.html` and the five resort pages, once the
  [self-hosted OSRM](#routing--self-hosted-osrm-v1115-recommended-before-paying-customers)
  is live.

### 9. Configure Twilio webhooks

In the Twilio console (Phone Numbers → Manage → Active Numbers → click
your dispatch number):

- **Voice / "A call comes in"** →
  `POST https://dispatch.example.com/webhooks/twilio/voice`
- **Voice / "Call status changes"** → leave blank. The voice TwiML's
  `<Dial action="/webhooks/twilio/dial-status">` already reports the dial
  outcome, and the whisper and voicemail callbacks are set in the TwiML
  too.
- **Voice / "Primary handler fails"** → leave blank or point at the same
  URL (the app handles transient failures gracefully)
- **Messaging / "A message comes in"** →
  `POST https://dispatch.example.com/webhooks/twilio/sms`

Signature verification is automatic when `TWILIO_AUTH_TOKEN` is set —
the app rejects requests with bad or missing signatures. **Do not** set
`TWILIO_VALIDATE_SIGNATURE=false` outside of local development. If every
webhook returns 403 even with the right token, uvicorn isn't trusting
Fly's forwarded headers (see the note in step 2).

Test the voice flow with a real phone call to the Twilio number. Within
a few seconds, the `DISPATCHER_PHONE` handset should ring. If it doesn't:

1. `flyctl logs` will show the `/webhooks/twilio/voice` request and
   Twilio's `From`/`To` parameters.
2. Check that `DISPATCHER_PHONE` is in E.164 format (`+1...` with country code).
3. Check the Twilio console "Calls" log for the SID and any error codes.
4. Check the clock: between 02:00 and 08:00 ET the quiet-hours gate skips
   the dial by design (after-hours message + SMS + voicemail instead).

### 10. Submit HSTS preload (after 30 days)

The app emits `Strict-Transport-Security: max-age=31536000;
includeSubDomains; preload` in production. After serving this header
for ≥30 days from the production domain, submit the domain at
`https://hstspreload.org/`. This bakes HTTPS-only into browser builds
and protects first-time visitors.

---

## Ongoing operations

### Monitoring

- **Uptime:** Point a UptimeRobot / BetterUptime monitor at
  `https://dispatch.example.com/health`. Alert if it returns
  non-200 for 2 consecutive checks.
- **Logs:** `flyctl logs --app livery-dispatch -f` for live tail.
  For retention, attach Fly's built-in log shipping to a service like
  Logtail / Axiom (free tier sufficient for early-MVP volume).
- **Brute force watch:** Alert on a surge of `429` responses on
  `/admin/login` or `/driver/login` — that's the login rate limiter
  catching credential stuffing in progress.
- **Routing:** a second monitor on
  `/osrm/route/v1/driving/-81.6879,30.4941;-81.3438,31.1969?overview=false`
  catches a dead OSRM app. A failure there means the maps are drawing
  straight lines and fares are falling back to haversine — degraded, not
  down.

### Backups (SQLite)

SQLite on a Fly volume has no built-in point-in-time recovery. Since v1.4.1
three layers cover it, two of them automatic:

| Layer | When | Where | Kept | Protects against |
|---|---|---|---|---|
| **Fly volume snapshots** | Daily, automatic (`fly.toml`: `scheduled_snapshots = true`, `snapshot_retention = 30`) | Fly's snapshot storage, off the volume | 30 days | Losing the volume or the machine; a bad day of data |
| **Pre-migration copy** | Automatic: `boot.py`, before `alembic upgrade` moves an existing database ([§ 6](#6-database-schema-alembic--v1012)) | `/data/backups/dispatch-pre-migration-<revision>-<UTC time>.db` | Newest 10 | A migration that goes wrong |
| **On-demand copy** | When you run it: before a risky change, or to take a copy off Fly | `/data/backups/dispatch-YYYY-MM-DD.db` | 30 daily + the first copy of each of the last 12 months | Anything, once downloaded |

**Take a copy** (consistent even while the app is writing; integrity-checked
before it takes its name; prunes old copies to the policy above):

```bash
flyctl ssh console --app livery-dispatch -C "python /app/backup.py --db /data/dispatch.db"
# backup: wrote /data/backups/dispatch-2027-01-15.db (1,234,567 bytes, integrity ok)
```

**Take it off Fly** (weekly is enough; keep the downloads somewhere that
isn't this laptop alone):

```bash
flyctl ssh sftp get /data/backups/dispatch-2027-01-15.db --app livery-dispatch
```

Don't copy `dispatch.db` with `cp` or `sftp get` directly: while the app
runs, the newest commits sit in `dispatch.db-wal`, and a plain copy can
catch a half-written page. `backup.py` uses SQLite's backup API, which
copies a consistent snapshot including the WAL.

**Restore a copy** (it replaces the live data; take a fresh copy first if
there is anything worth keeping):

```bash
flyctl ssh console --app livery-dispatch
python /app/backup.py --db /data/dispatch.db           # keep what's there now
sqlite3 /data/dispatch.db ".restore /data/backups/dispatch-2027-01-15.db"
exit
flyctl machine restart <machine-id> --app livery-dispatch
```

`.restore` goes through SQLite's locking, so the running app sees the
restored data on its next query; the restart clears in-process state (rate
limits, the `/osrm` cache). If the restored copy is from before a migration,
the restart migrates it forward again (and copies it first).

**Restore a Fly snapshot** (the volume itself is lost or unusable):

```bash
flyctl volumes list --app livery-dispatch                 # the volume id
flyctl volumes snapshots list <volume-id>                     # pick a snapshot id
flyctl volumes create dispatch_data --snapshot-id <snapshot-id> --region atl --size 1
```

Then move the app onto the new volume: destroy the old machine (or its
volume) so only the new `dispatch_data` volume is left, and `flyctl deploy
--ha=false`. Fly attaches a machine to a volume with the mount's name.
These steps are from Fly's documentation and haven't been run against
this app yet.

When migrating to Postgres (see below), switch to managed PG with
point-in-time recovery; these file copies are for SQLite.

### Rollback

Fly retains the previous release. To roll back:

```bash
flyctl releases --app livery-dispatch       # See recent releases
flyctl releases rollback <release-version>      # Roll back to a known good one
```

**An older image boots on a newer database (v1.1.21).** `boot.py` sees a
revision it doesn't know, logs `WARNING: the database is at revision …`,
skips migrations, and starts. Older code runs fine on a newer *additive*
schema (an extra nullable column or table), which is every migration so
far. If the older code needs the old schema, downgrade **before** rolling
back, while the newer release (which knows how) is still running, then roll
back straight away. A restart of the newer release in between would
upgrade the database again.

If the rollback also needs a DB schema downgrade, use Alembic:

```bash
flyctl ssh console --app livery-dispatch
cd /app                  # the folder holding alembic.ini
alembic downgrade -1     # one step back
# or
alembic downgrade <revision>     # back to a specific revision
```

Schema downgrades only work cleanly when the migration was written with a
proper `downgrade()` function — autogenerate produces sane downgrades for
column adds and table creates, but column drops + data migrations need
hand-written rollback logic. When in doubt, restore from backup.

### Updating secrets

```bash
flyctl secrets set ADMIN_PASSWORD_HASH='$2b$12$...new-hash...'
```

Setting a secret triggers a redeploy. Plan secret rotation during
low-traffic windows.

---

## SQLite → Postgres migration

When call volume justifies it (rough threshold: >5 concurrent
dispatchers or >500 trips/month), switch to Postgres. The dispatch app
uses SQLAlchemy with no SQLite-specific features, so the migration is
straightforward.

### Steps

1. **Provision Postgres** — Fly Postgres or Supabase or Neon. All work.
   ```bash
   flyctl postgres create --name livery-dispatch-db --region atl
   flyctl postgres attach --app livery-dispatch livery-dispatch-db
   ```
   This sets `DATABASE_URL=postgres://...` automatically.

2. **Alembic is already wired** (since v1.0.12 — `alembic/`).
   Against the fresh Postgres instance, `alembic upgrade head` creates
   the full schema. Same command you use for SQLite — no special-case
   logic.

3. **Export from SQLite, import to Postgres** — for the soft-launch
   data:
   ```bash
   # On the Fly machine:
   sqlite3 /data/dispatch.db ".dump" > dump.sql
   # Translate SQLite-isms to Postgres-isms, then:
   psql $POSTGRES_URL < dump.sql
   ```
   For a clean cutover, freeze writes for 5–10 minutes during the dump.

4. **Verify the swap** — boot a staging instance against the new
   Postgres URL before redirecting production traffic. Run the test
   suite against the staging URL (it uses the same SQLAlchemy code
   path) to catch any subtle differences.

5. **Cutover** — update `DATABASE_URL` secret on the production app,
   redeploy. Keep the SQLite file as a snapshot for a few weeks before
   deleting.

---

## Operational gotchas

These are decisions baked into the code that you might fight against
during deploy — flagged here so you don't unwind them by accident:

- **CORS in production is fail-closed.** If `DISPATCH_ENV=production`
  and `CORS_ORIGINS=*`, the app refuses to start. Pin to specific
  origins.
- **Console notification provider is refused in production.** If you
  see `RuntimeError: NOTIFICATION_PROVIDER=console must not be used in
  production`, set provider to `twilio` (and configure the TWILIO_*
  vars).
- **`/docs`, `/redoc`, `/openapi.json` don't exist in production**
  (v1.1.17, E2). They're auth-exempt for local Swagger debugging, so the
  `FastAPI(...)` constructor drops them when `DISPATCH_ENV=production`; they
  return 404. Locally, Swagger's "Try it out" can't drive admin mutations
  anyway, because it doesn't send the CSRF header.
- **CSRF on every admin mutation (v1.1.13).** REST writes to `/trips`,
  `/drivers`, `/subcontractors` and `PATCH /reservations/{id}` /
  `PATCH /inquiries/{id}` need the session cookie **and** an
  `X-CSRF-Token` header (or `csrf_token` form field) bound to it, so
  ad-hoc `curl` mutations against production return 403. Use the admin
  pages, or the CLI inside `flyctl ssh console`.
- **Quiet hours skip the dial.** Default is 02:00–08:00 ET. Don't widen
  it — 18:00–02:00 is peak business (resort dinners, evening events,
  late-night returns). See `core.is_quiet_hours()`.
- **Routing defaults to the public OSRM demo.** One `OSRM_BASE_URL`
  serves fare distance and the `/osrm` map proxy; until it points at the
  private `livery-osrm` app the boot log warns, and heavy map use
  can hit the demo's rate limit. The app falls back to haversine fares
  and straight-line maps on any routing failure, so this is reliability
  and licensing (the demo is non-commercial), not correctness.
- **Twilio webhook URLs must be HTTPS.** Twilio won't deliver to plain
  HTTP. The Fly default `*.fly.dev` URL is HTTPS-only; custom domains
  must have TLS configured before pointing Twilio at them.
- **Single-region deploy is fine for v1.** All operations and customers
  are within \~300mi of `atl`. Multi-region adds Postgres
  read-replication complexity that the business doesn't need yet.

---

## Pre-cutover smoke test (every redeploy)

Run this checklist after every production deploy:

```bash
# 1. Health
curl -fsS https://dispatch.example.com/health

# 2. Security headers
curl -I https://dispatch.example.com/health | grep -iE \
  'content-security|strict-transport|x-frame|referrer|permissions'

# 3. Public surface reachable
curl -fsS -X POST https://dispatch.example.com/quote \
  -H "Content-Type: application/json" \
  -d '{"pickup_label":"JAX","dropoff_label":"The Cloister","vehicle_pref":"escalade-esv"}'
# Expect HTTP 200 JSON with "resolved": true and a "From $… — estimate"
# message. "resolved": false, "Estimate unavailable…" means no active SUV
# rate card: load them per § 7 (`python cli.py rates seed-defaults` in
# flyctl ssh console, or /admin/rates/new).
# (Counts against the 5-per-minute public-form bucket for your IP.)

# 4. Admin surface gated
curl -i https://dispatch.example.com/trips
# Expect 401 (no auth) or 307 redirect to /admin/login (HTML accept)

# 5. Driver surface gated
curl -i https://dispatch.example.com/driver
# Expect 401

# 6. Routing proxy (run twice)
curl -sS -D - -o /dev/null \
  "https://dispatch.example.com/osrm/route/v1/driving/-81.6879,30.4941;-81.3438,31.1969?overview=false" \
  | grep -iE '^HTTP|x-cache'
# Expect 200 both times, X-Cache: MISS then HIT (502/503/504 = routing down)
```

All six should pass before announcing the deploy is live.
