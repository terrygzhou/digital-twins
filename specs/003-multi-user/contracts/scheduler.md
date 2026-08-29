# Contract: Scheduler & Status (003)

002's `digital_twins.scheduler` module surface is unchanged except for two
003 additions: the `StatusServer` auth hook (C-5) and the serve tick's
per-user config merge + owner tag (C-3/R5/R6). No new scheduler endpoints in
003 — `/status` remains the only route; 003 makes it auth-gated. The web UI
credential endpoints (`/signup`, `/signin`) live in the new `digital_twins.web`
package (see below), not in `scheduler/`.

## 002 surface (unchanged)

```python
# presets.py — pure, no I/O
def expand_next(preset, param, fire_time, now, anchor) -> datetime: ...
def preset_values() -> list[str]: ...

# schedules.py — CRUD over 001 db
def create_schedule(db, owner, source, preset, param=None, fire_time='03:00', now=None) -> dict: ...
def list_schedules(db, owner=None) -> list[dict]: ...
def update_schedule(db, schedule_id, **fields) -> dict: ...
def delete_schedule(db, schedule_id) -> None: ...
def due_schedules(db, now=None) -> list[dict]: ...
def claim_and_advance(db, schedule_id, fired_at) -> None: ...

# loop.py
def serve_once_tick(db, config) -> dict: ...
# 003 note (below): 003's serve tick resolves each schedule owner's
# user_config overrides into `config` before calling run_pipeline, and passes
# owner=schedule.owner to run_pipeline. The *signature* serve_once_tick(db,
# config) is unchanged — the merge is internal to the tick (the caller still
# hands it the global config; the tick does the per-owner merge).
# returns {fired: [schedule id...], skipped: [schedule id...], queue_depth: int}

def run_serve(db, config, status_port, *, status_server=None, tick_seconds=TICK_SECONDS, max_ticks=None) -> None: ...
```

## 003 addition 1 — `StatusServer` auth hook (C-5)

```python
# status.py
class StatusServer(ThreadingHTTPServer):
    def __init__(self, addr, db, config, *, auth_checker=None):
        # auth_checker: optional callable, auth_checker(headers: dict[str,str])
        #   -> bool | str
        #   True  -> allow (200)
        #   str   -> deny; the string is the 401/403 body error
        #   False -> deny with a generic "unauthorized" (401)
        # When auth_checker is None (002's callers), the do_GET path is
        # byte-for-byte the 002 behavior (no auth) — backward compatible.
        ...

    def do_GET(self) -> None:
        # path == "/status":
        #   if self.auth_checker is not None:
        #       result = self.auth_checker(self._request_headers())
        #       if result is True: ... build + send 200 payload ...
        #       elif isinstance(result, str): ... send 403 {"error": result} ...
        #       else: ... send 401 {"error": "unauthorized"} ...
        #   else: ... 002 path (no auth) ...
        # path != "/status": 404 (unchanged)
```

The **serve CLI** (`cli.py::serve`) builds the `auth_checker` and passes it:

```python
def _auth_checker(db, service_token_env):
    """Accept, in order: the shared service token (BR-10), a personal token,
    a session token. Missing/unknown -> False (401); a known-but-unusable
    credential -> a string reason (403)."""
    def check(headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False                      # 401: no credential
        token = auth[len("Bearer "):].strip()
        # 1) shared service token (BR-10)
        service = os.environ.get("DT_SERVICE_TOKEN")
        if service and hmac.compare_digest(token, service):
            return True
        # 2) personal token (personal_tokens table)
        if personal_tokens.verify(db, token):
            return True
        # 3) session token (sessions table, TTL + revoked)
        if sessions.verify(db, token):
            return True
        # 4) a Bearer that matches none of the above: 401 (unknown) — not 403,
        #    because there is no mutating /status route for a 403 to mean.
        return False
    return check
```

> `/status` is **read-only** in 003, so the 403 branch of `auth_checker` is
> reserved for the future mutating HTTP routes (account/config management).
> In 003 the checker distinguishes "no/unknown credential" (401) from
> "known credential" (200); the 403 path exists in the contract for the
> routes 004/005 will add. The `bool | str` return shape (R8) is what lets
> that extension happen without re-plumbing the handler.

**Backward compatibility (C-5)**: a `StatusServer` constructed without
`auth_checker` (002's `tests/unit/test_status.py`, any 002 caller) behaves
exactly as 002 — `do_GET` on `/status` returns 200 with no auth check. A red
test asserts this (002's no-auth `/status` still 200 when no checker is
passed).

## 003 addition 2 — serve tick: per-user config merge + owner tag (C-3/R5/R6)

`serve_once_tick(db, config)` is **signature-unchanged** (still takes the
global `config`), but its internal fire path changes:

```python
for schedule in due:
    source = schedule["source"]
    owner  = schedule["owner"]
    # 003: merge this owner's user_config overrides into a COPY of config
    # (never mutate the shared global). Pure: user_config.merge_user_config.
    owner_cfg = merge_user_config(config, db, owner, source=source)
    # 003: pass owner= so the points carry the owner tag (R6)
    run_pipeline(
        owner_cfg, db, qdrant, embedder,
        source_names=[source],
        trigger="schedule",
        scheduled_by=owner,
        owner=owner,            # NEW kwarg -> payload owner/owner_tag
    )
    claim_and_advance(db, schedule["id"], fired_at)
```

- `merge_user_config(config, db, owner, source)` (001's config dict + the
  `user_config` table) → a **new** config dict where `config["sources"][source]`
  has the owner's overrides applied (R5). The returned dict is what
  `run_pipeline` receives; the global `config` is not mutated (SC-003
  isolation).
- `owner=owner` on `run_pipeline` stamps `payload["owner"] = owner` and
  `payload["owner_tag"] = f"{owner}-ingest"` on every point of the run (R6).
  For a `system`-owned schedule, `owner="system"` (or omitted → legacy
  `None`, no tag — 002 behavior preserved for system runs).

## Web UI credential endpoints (003, new `digital_twins.web` package)

The minimal UI ships in 003 (A2) but is *not* an SPA: two credential
endpoints on a `ThreadingHTTPServer` (reusing 002's handler pattern), plus a
thin `/status` passthrough that reuses the 002 `status_payload` + the C-5
`auth_checker`.

```python
# web/server.py
# POST /signup  {email, password}  -> 200 {"email","role","created":true}
#   role = "first row in accounts? -> admin, else reader" (C-4, shared with
#   the CLI signup). Duplicate email -> 409 {"error":"account already exists"}.
#   The password is hashed with 001 hash_password (R1); created_at set.
#
# POST /signin  {email, password}  -> 200 {"session_token","expires_at"}
#   Authenticates via 001 auth.authenticate (password). On success: a 32-byte
#   session token is created, its pbkdf2 hash stored in `sessions`, and the
#   PLAINTEXT token returned to the UI (shown once). On failure: 401.
#   Also sets accounts.last_active.
#
# GET  /status  -> reuses 002 status_payload + the C-5 auth_checker
#   (a valid session / personal / service token, else 401)
```

- The session token is **short-lived** (default 8 h, `expires_at`) and
  **revocable** (`sessions.revoked`) — distinct from a personal token
  (which has no TTL) and from the shared service token (static, BR-10).
- The UI's subsequent requests carry the session token via
  `Authorization: Bearer <token>`; the *same* `auth_checker` that gates
  `serve`'s `/status` gates the UI's `/status` (C-2: one identity model, one
  guard).

## Invariants (003)

1. **C-5 backward-compat**: `StatusServer` without `auth_checker` == 002
   (no-auth `/status`). A red test pins this.
2. **C-3 isolation**: `merge_user_config` never mutates its input config dict;
   two owners' merges are independent (SC-003).
3. **R6 owner tag**: a run with `owner=U` stamps `payload["owner"]=U` and
   `payload["owner_tag"]=f"{U}-ingest"` on every point; a run with
   `owner=None` (legacy) stamps no owner field (002 behavior preserved).
4. **Last-admin guard** (SC-002): no role-change / account-delete that leaves
   zero admins is applied; the guard is checked within the same transaction as
   the mutation.
5. **Credential non-leak**: no password / token / session is ever logged,
   echoed to argv, or stored in plaintext (R1/R2/R4/R9). The DB stores only
   pbkdf2 hashes.
6. **One-record-not-N** (SC-006): the owner tag is a payload field, **not** a
   dedup key — the same content ingested by two users still yields one point
   (001's deterministic point IDs + high-water marks are untouched). The
   owner tag is for *query-time* filtering (US5), not for dedup.

## Status HTTP surface (003)

- `GET /status` → `200` JSON = `status_payload(...)` **when** the C-5
  `auth_checker` accepts the request (service / personal / session token);
  `401` when no/unknown credential; `403` reserved for future mutating routes;
  `404` for any other path.
- Port `scheduler.status_port` (default 8765); `0` disables the server
  entirely (002, unchanged).
- No auth in 002 → auth in 003 **only when** the serve CLI passes an
  `auth_checker` (the default serve now does; a caller that opts out by
  passing `auth_checker=None` gets 002 behavior).
