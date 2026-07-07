# Security audit — 2026-07-06

Scope: pre-sharing review for a private deployment used by ~5–10 known,
trusted people (per operator). Not a hardened-public-internet posture. Covers
auth, session handling, secrets at rest, per-user isolation, the repo itself,
and the deploy path (deploy/DEPLOY.md: GCP VM + Caddy HTTPS + systemd).

## Findings

### 1. CRITICAL — secrets committed to GitHub (partially remediated; operator action required)

Commit `b9bc9d8` (and, for `.secret`/`tophat.db`, every commit back to the
first) published to the private GitHub repo:

- `data/.secret` — the master key that signs session cookies AND encrypts the
stored ProjectX API keys,
- `data/credentials.json` — the API keys, encrypted **under that same
published key** (so effectively plaintext to anyone with repo access),
- `data/tophat.db` — login emails + salted PBKDF2 password hashes.

Remediated in this session:

- All runtime data untracked at the tip and gitignored (`data/` runtime files,
`data/users/`, caches, tick exports).
- `data/.secret` regenerated locally; stored API keys re-encrypted under the
new secret (old sessions invalidated — log in again).

**Still required (only the operator can do these):**

1. **Rotate both ProjectX API keys** (`rizzbizzy786`, `rayyyu12`) at the
  provider — the currently-valid keys are recoverable from the repo history.
2. **Change the dashboard login password** for [rtxftw@gmail.com](mailto:rtxftw@gmail.com) (its hash is
  in history; PBKDF2-200k is slow but a weak password is crackable).
3. Before granting anyone repo access: **rewrite history** (`git filter-repo
  --path data/ --invert-paths` + force-push, then everyone re-clones) or
   start a fresh repository. Until then treat the history as containing live
   credentials.

### 2. HIGH — no per-user data isolation (FIXED this session)

All users previously shared one credentials store / registry / settings /
broker pool — any login saw and controlled the operator's accounts and could
reveal the operator's API keys. Implemented `tophat/store/tenant.py`: per-user
data dirs (`data/users/<uid>/`), tenant-scoped stores, per-user broker pools
and automation ticks, tenant-scoped caches and run locks. Verified by
`tests/test_tenant.py` (197 tests green) and a live two-user check: user B
sees no credentials, cannot reveal user A's key (404), independent settings.

### 3. MEDIUM — tracked runtime state breaks the deploy model (FIXED)

DEPLOY.md's contract is "state is untracked; `git reset --hard` never touches
it". Commit `b9bc9d8` made state tracked, so a VM auto-deploy would clobber or
conflict with live runtime state (settings edited in the UI would block `git pull`). Untracking data/ restores the contract.

### 4. LOW — self-XSS via unescaped aliases

`index.html` interpolates some user-controlled strings (account alias/notes)
without `esc()`. Post-tenancy a user can only inject into their own dashboard
(self-XSS), so impact is minimal for a trusted group. Harden opportunistically
when touching those templates.

### 5. LOW — no rate limiting on `/api/login`

PBKDF2-200k makes online brute force slow and expensive, and the user set is
tiny/invite-only (CLI `python -m tophat.server.auth adduser`). If exposure
grows, add Caddy's `rate_limit` on `/api/login`.

## Posture confirmed good

- **Sessions**: HMAC-SHA256-signed stateless cookies, `httponly`,
`samesite=lax`, `secure` when `TOPHAT_HTTPS=1` (deploy/setup.sh sets it),
7-day TTL, constant-time compares. Stateless means no server-side logout
revocation — acceptable at this scale (rotating `.secret` revokes all).
- **Password storage**: per-user random salt + PBKDF2-HMAC-SHA256 (200k).
- **At-rest crypto** (`store/crypto.py`): separate enc/MAC subkeys derived from
the master, random nonce, encrypt-then-MAC, version-tagged tokens. Sound
stdlib construction.
- **API keys never sent to clients** except via the explicit authenticated,
tenant-scoped reveal endpoint; list responses are masked.
- **Auth gating**: every route except `/login`, `/api/login`, `/assets` is
cookie-gated (middleware); the WebSocket verifies the cookie and closes
with 1008 otherwise; FastAPI docs endpoints disabled.
- **CSRF**: JSON POSTs + `samesite=lax` cookies — cross-site POSTs don't carry
the session cookie. Adequate here.
- **Registration**: none — users are created only from the server console
(invite-only by construction).
- **Ops**: single-instance lock guards double-fire; automation is disarmed by
default (`auto_execute=false`) for new users; `.env` is gitignored with a
placeholder-only `.env.example`; deploys terminate TLS at Caddy.

## Deployment checklist for sharing (5–10 users)

1. Rotate ProjectX keys + dashboard password (finding 1).
2. History rewrite or fresh repo (finding 1) — BEFORE adding collaborators.
3. Deploy per deploy/DEPLOY.md; confirm `TOPHAT_HTTPS=1` and a strong
  `TOPHAT_ADMIN_PASSWORD` in `/opt/tophat/.env`.
4. Create each friend's login on the VM:
  `python -m tophat.server.auth adduser friend@x.com '<their password>'`.
   They add their own ProjectX keys in Settings — keys are per-user and
   invisible to everyone else.
5. Keep the repo private; the app itself should be the only thing exposed.

