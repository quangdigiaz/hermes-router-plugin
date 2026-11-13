# Hermes Router plugin for Hermes Agent — install & configure guide

One install points all three Hermes Agent surfaces — **inference** (a
`hermes-router` model provider), **`web_search`** (`POST /v1/search`) and
**`web_extract`** (`POST /v1/web/fetch`) — at a Hermes Router: one URL, one
key, one usage ledger. Plugin source lives in [`hermes-plugin/`](../hermes-plugin);
the human-facing mechanism doc is `hermes-plugin/README.md`. This file is the
procedure to follow — written for both operators and coding agents.

If there is no Hermes Router yet, install it first — the router playbook is
the `AGENTS.md` shipped with the router source. This guide assumes a
reachable router.

---

## 0. Non-negotiable rules

1. **Router key ≠ provider key.** The plugin needs at most a *router* API key
   (Router Dashboard → API Keys) — it only guards access to the user's own
   router. Never ask for, read, or touch upstream *provider* credentials
   (OpenAI, Tavily, …); those live in the router's database and are entered
   in the router dashboard by the user.
2. **Credential entry.** The router key goes into the user's environment
   (`~/.hermes/.env`, shell profile) — never into tracked files, commit-able
   dotfiles, or chat. Redact it in anything reported back (first 4 + last 4
   characters). If the router does not require auth, no key is needed at all.
3. **No billed calls without consent.** `doctor --live`, `search`, `fetch`,
   `model --live` and `hermes-plugin/tests/live_smoke.py` send real requests
   that spend quota — ask first. Offline surfaces (`status`, `doctor` without
   `--live`, `/hermes-router-status`) make no network calls.
4. **Preserve unrelated config.** Only `hermes-router`-owned lines in
   `config.yaml` / `.env` may be written. Other providers, model entries and
   comments stay byte-for-byte.
5. **Fail closed.** Report "configured" only when every check in §5 passes.
   A partially working install is reported as partially working, with the
   exact failing command.

## 1. Preflight

1. Hermes Agent must be installed: `hermes --version`. If missing, stop —
   this plugin is not a standalone tool.
2. Router reachable: `curl -fsS $ROUTER_URL/api/health` → `{"ok":true}`.
   Ask the user for the router URL if not already in their env; default local
   is `http://localhost:20128`.
3. Ask whether the router requires an API key (`requireApiKey` in router
   settings / Dashboard → API Keys).

## 2. Install

```sh
hermes plugins install quangdigiaz/hermes-router-plugin --enable
```

or pip:

```sh
pip install "git+https://github.com/quangdigiaz/hermes-router-plugin"
```

One install wires all three surfaces. `plugin.yaml` declares a
single `kind: backend` — the kind whose loader *imports* the package — and
that import registers the model-provider profile at module level
(`hermes_router_hermes/model_profile.py`). Do **not** add a second manifest
or invent `provides_model_providers`; that field does not exist.

## 3. Configure — one URL, one key

```sh
export HERMES_ROUTER_URL="http://localhost:20128"   # trailing /v1 optional
export HERMES_ROUTER_KEY="sk-..."                    # only when router auth is on
```

Persist via `~/.hermes/.env` or the user's shell profile. Both halves read
the same pair — never set different URLs for inference vs web.

`hermes config set` is usually **not needed**:

- web — with `web.search_backend` / `web.extract_backend` unset, Hermes
  auto-detects the backend from whichever URL/key is present, and the router
  wins when it's the only one. Pin only if the user also has keys for another
  web provider.
- inference — `--provider hermes-router` (aliases `hermesrouter`,
  `hermes_router`) selects it per call. To make it the *default*, one line in
  `config.yaml`: `model.provider: hermes-router`.

`hermes hermes-router setup` prints exactly those lines;
`hermes hermes-router status` shows what both halves resolved.

### Inference-only alternative — do not stack

If the user only wants inference (no web tools), the router itself can write
`~/.hermes/config.yaml` via `POST /api/cli-tools/hermes-settings` (Dashboard →
CLI Tools → Hermes; preset builder: `src/lib/cliTools/hermesPreset.js`). Pick
**one** path for `model.provider` — do not let the router preset and this
plugin fight over the same field.

## 4. Fallback — provider missing from `/model`

If a Hermes build loads plugins *after* its first provider lookup, the
profile can be missed for that process. `model_profile.py` is self-contained
(stdlib + `providers` only) — copy it as the documented drop-in, read on the
next provider lookup with **no restart**:

```sh
mkdir -p "$HERMES_HOME/plugins/model-providers/hermes-router"   # Windows: %LOCALAPPDATA%\hermes\plugins\...
cp hermes_router_hermes/model_profile.py \
   "$HERMES_HOME/plugins/model-providers/hermes-router/__init__.py"
```

Repeat per profile (`hermes -p <profile> …`) when several profiles run.

## 5. Verify — the doctor gate

Report "configured" only when every check is OK:

```sh
hermes hermes-router status    # all three surfaces resolve: url, auth, models
hermes hermes-router doctor    # offline checks — every line must be "ok"
```

With explicit consent (billed, Rule 0.3):

```sh
hermes hermes-router doctor --live   # + one real search, one real fetch, catalogue
hermes hermes-router model --live    # the ids the /model picker will show
```

In-session check: `/hermes-router-status [live]`.

**Legacy fallback** (Hermes builds without the plugin CLI surface): verify
the package imports and the router answers instead —

```sh
python -c "import hermes_router_hermes"          # registers profile at import
curl -fsS -H "Authorization: Bearer $HERMES_ROUTER_KEY" \
     "$HERMES_ROUTER_URL/v1/models"              # non-empty data[]
```

If any check fails: diagnose (`doctor` detail lines name the failing
surface), apply the smallest fix (missing env → §3; missed profile → §4),
and re-run the full gate. Never report done with a FAIL outstanding.

## 6. Report back / undo

Report: what was installed, which surfaces are wired, which files were
modified (paths + field names, key redacted), the doctor check list, and what
only the user can do (restart the Hermes session, paste the router key).

Undo: `hermes plugins uninstall hermes-router`, remove the env lines, delete
the drop-in `__init__.py` if §4 was used.

## Appendix — environment variables

| Env var | Default | Meaning |
|---|---|---|
| `HERMES_ROUTER_URL` | — (required) | Base URL; `…/` and `…/v1` both work. Serves inference, search, fetch, `/v1/models` |
| `HERMES_ROUTER_BASE_URL` | — | Same value under Hermes' base-URL-override name; wins if both set |
| `HERMES_ROUTER_KEY` | none | `Authorization: Bearer …` on all three surfaces + catalogue |
| `HERMES_ROUTER_AUX_MODEL` | unset | Cheap model id for auxiliary tasks (compression/titles/vision) |
| `HERMES_ROUTER_MODELS` | unset | Comma-separated ids used only when the live catalogue fetch fails |
| `HERMES_ROUTER_SEARCH_MODEL` | `auto` | `auto` = router routing, or an id from `/v1/models/web` |
| `HERMES_ROUTER_FETCH_MODEL` | `auto` | Same, for fetch (`jina-reader`, `fetch-combo`, …) |
| `HERMES_ROUTER_SEARCH_TIMEOUT_S` | `30` | Whole-request timeout for one search |
| `HERMES_ROUTER_FETCH_TIMEOUT_S` | `110` | Per-URL fetch timeout |
| `HERMES_ROUTER_MAX_CHARACTERS` | router default | Hard cap on extracted text |

Known limits (deliberate): no hardcoded model list (catalogue is discovered
live; `HERMES_ROUTER_MODELS` is the offline fallback), no `fetch_account_usage`
(router ledger is dashboard-authenticated), no deep crawl (each URL fetched
once), `max_tokens` → `max_characters` ≈ 4 chars/token, router `429/402`
surface verbatim (no plugin retry — the router fails over internally).
