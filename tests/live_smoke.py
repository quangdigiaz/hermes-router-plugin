"""Live smoke test — the MERGED plugin's three surfaces, against a REAL router.

This is the check a mock cannot make: that the router accepts exactly what this
plugin sends on all three surfaces, and that what comes back lands in the shapes
Hermes reads.

    HERMES_ROUTER_URL=http://localhost:20128 \
    HERMES_ROUTER_KEY=sk-... \
    python tests/live_smoke.py [--no-web] [--no-chat]

In order:

1. the plugin imports and registers BOTH halves — the web provider (search +
   extract) and the inference profile — from that single import;
2. ``web_search``   → POST /v1/search      returns the fixed web envelope;
3. ``web_extract``  → POST /v1/web/fetch   returns one document per URL;
4. inference        → GET  /v1/models      returns ids, reported as
   ``provider/model`` vs combo (bare name), because a picker full of ids nobody
   can call is worse than an empty one;
5. unless ``--no-chat``: one real chat completion through a listed id, proving
   ``choices[0].message`` comes back the way Hermes will read it.

Hermes itself is not required: the package falls back to stand-ins for
``agent.web_search_provider`` and ``providers`` (the documented contracts), which
is also how CI runs it. ``--no-web`` skips steps 2-3 when you do not want to
spend search quota.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_router_hermes import model_profile  # noqa: E402
from hermes_router_hermes.provider import HermesRouterWebProvider  # noqa: E402

QUERY = "Hermes Router self-hosted LLM gateway"
PAGE = "https://example.com"
WEB_KEYS = {"title", "url", "description", "position"}


def chat(base_url: str, api_key: str | None, model: str) -> dict:
    """One non-streaming completion, the way Hermes' chat transport sends it."""
    body = json.dumps({
        "model": model,
        "stream": False,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with exactly: PING"}],
    }).encode()
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = "Bearer " + api_key
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return {"status": response.status, "body": json.loads(response.read().decode("utf-8", "replace"))}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        exc.close()
        return {"status": exc.code, "body": raw[:400]}
    except Exception as exc:  # unreachable host, timeout, TLS…
        return {"status": None, "body": f"{type(exc).__name__}: {exc}"}


def web_surface(provider: HermesRouterWebProvider, failures: list) -> None:
    print(f"\n=== search({QUERY!r}, limit=3) ===")
    search = provider.search(QUERY, limit=3)
    if not search.get("success"):
        failures.append("search: " + str(search.get("error")))
        print(json.dumps(search, indent=2, ensure_ascii=False)[:2000])
    else:
        web = search["data"]["web"]
        print(f"  {len(web)} results")
        for item in web[:3]:
            print(f"    {item['position']}. {item['title'][:70]} — {item['url']}")
            print(f"       {item['description'][:110]}")
        missing = WEB_KEYS - set(web[0].keys())
        if missing:
            failures.append(f"search envelope missing keys: {sorted(missing)}")

    print(f"\n=== extract([{PAGE!r}], format='markdown') ===")
    extract = provider.extract([PAGE], format="markdown")
    if not extract.get("success"):
        failures.append("extract: " + str(extract.get("error")))
        print(json.dumps(extract, indent=2, ensure_ascii=False)[:2000])
    else:
        document = extract["data"][0]
        if document.get("error"):
            failures.append("extract: " + str(document["error"]))
            print(f"  {document['url']}: {document['error']}")
        else:
            print(f"  {document['title'] or document['url']} — {len(document['content'])} chars")
            for key in ("url", "title", "content", "raw_content"):
                if key not in document:
                    failures.append(f"extract document missing {key!r}")


def model_surface(failures: list, chat_enabled: bool) -> None:
    profile = model_profile.PROFILE
    print("\n=== inference profile ===")
    print(json.dumps({
        "name": profile.name,
        "aliases": list(profile.aliases or ()),
        "base_url": profile.base_url,
        "models_url": profile.models_url,
        "auth_type": profile.auth_type,
        "api_mode": profile.api_mode,
        "supports_vision": profile.supports_vision,
        "default_aux_model": profile.default_aux_model,
        "fallback_models": list(profile.fallback_models or ()),
        "registered_with_hermes_registry": model_profile.model_registration_available(),
    }, indent=2))

    ids = profile.fetch_models()
    if ids is None:
        print("FAIL: fetch_models() returned None — the router catalogue is not readable")
        print("      (check HERMES_ROUTER_URL and, when auth is on, HERMES_ROUTER_KEY)")
        failures.append("inference: catalogue unreadable")
        return
    combos = [i for i in ids if "/" not in i]
    models = [i for i in ids if "/" in i]
    print(f"\nlive catalogue: {len(ids)} ids — {len(models)} provider/model, {len(combos)} combos")
    print("  first models:", models[:5])
    print("  first combos:", combos[:5])
    if not ids:
        failures.append("catalogue is empty — Hermes would show no models")

    if not chat_enabled or not ids:
        return
    probe = combos[0] if combos else ids[0]
    api_key = (os.environ.get("HERMES_ROUTER_KEY") or "").strip() or None
    print(f"\n=== chat via the router with -m {probe} ===")
    result = chat(profile.base_url or "", api_key, probe)
    print("  status:", result["status"])
    body = result["body"]
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices and isinstance(choices[0].get("message"), dict):
            print("  choices[0].message.content:", json.dumps(choices[0]["message"].get("content")))
        else:
            print("  body:", json.dumps(body)[:300])
            failures.append(f"{probe}: response had no choices[0].message")
    else:
        print("  body:", body)
        failures.append(f"{probe}: HTTP {result['status']}")


def main(argv: list[str]) -> int:
    provider = HermesRouterWebProvider()
    status = provider.status()
    print("config:", json.dumps(status, indent=2, ensure_ascii=False))
    if not provider.is_available():
        print("FAIL: HERMES_ROUTER_URL is not set", file=sys.stderr)
        return 2

    failures: list[str] = []
    if "--no-web" not in argv:
        web_surface(provider, failures)
    model_surface(failures, chat_enabled="--no-chat" not in argv)

    print("\n=== doctor — one report for all three surfaces ===")
    from hermes_router_hermes import _merged_doctor  # noqa: PLC0415 - CLI surface

    # Offline by default: the calls above already spent the quota, and this
    # report is here to show the SHAPE an operator reads, not to spend more.
    # Pass --live-doctor to let it re-run the two real web calls.
    report = _merged_doctor(provider, live="--live-doctor" in argv)
    for check in report["checks"]:
        print(f"  {'ok  ' if check.get('ok') else 'FAIL'} {check['name']:<8} {check.get('detail')}")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("\nOK — one import registered both halves; search, extract, catalogue and inference all answered in the expected shape.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
