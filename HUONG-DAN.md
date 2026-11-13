# Hermes Router plugin cho Hermes Agent — hướng dẫn khai báo

> File này giải thích cơ chế cho người đọc. Quy trình từng bước cho coding
> agent cài đặt nằm ở [`docs/HERMES_AGENT_PLUGIN.md`](docs/HERMES_AGENT_PLUGIN.md).

Một lần cài wire **cả 3 mặt**: inference (model provider) + `web_search` + `web_extract`.
Một URL, một key, một sổ chi phí.

## Thể thức khai báo (đã verify đúng logic)

| Lớp | File | Cơ chế |
|---|---|---|
| Manifest | `plugin.yaml` | `kind: backend` (loader import package) + `provides_web_providers: [hermes-router]` + `requires_env` |
| Web provider | `hermes_router_hermes/__init__.py` → `register(ctx)` | `ctx.register_web_search_provider(provider)` — phục vụ cả search lẫn extract |
| Model provider | `hermes_router_hermes/model_profile.py` | `register_provider(PROFILE)` **module-level** — profile tự đăng ký ngay khi package được import |

Tại sao 3 mặt chỉ cần 1 manifest: manifest chỉ nhận **một `kind`**, và `backend`
là kind có loader **import package**. Import đó chính là sợi dây — `model_profile.py`
đăng ký profile ở module level, đúng cách Hermes khám phá provider ngoài cây.
Không có `provides_model_providers` trong manifest — field đó không tồn tại,
khai thì hứa một loader không có.

## Cài đặt

```bash
hermes plugins install quangdigiaz/hermes-router-plugin --enable
```

hoặc pip:

```bash
pip install "git+https://github.com/quangdigiaz/hermes-router-plugin"
```

## Cấu hình — đúng 1 export

```bash
export HERMES_ROUTER_URL="https://router.quangdigi.com"   # /v1 tự thêm
export HERMES_ROUTER_KEY="sk-..."                         # chỉ khi router bật auth
```

Hai biến này dùng chung cho cả 3 surface — không thể lệch URL giữa inference
và web.

| Env var | Mặc định | Ý nghĩa |
|---|---|---|
| `HERMES_ROUTER_URL` | bắt buộc | Base URL router (`…/` và `…/v1` đều được) |
| `HERMES_ROUTER_BASE_URL` | — | Alias theo tên override của Hermes; thắng nếu cả hai có |
| `HERMES_ROUTER_KEY` | none | `Authorization: Bearer …` cho cả 3 surface + catalog |
| `HERMES_ROUTER_AUX_MODEL` | unset | Model rẻ cho việc phụ (nén context, title, vision) |
| `HERMES_ROUTER_MODELS` | unset | Danh sách id dự phòng khi fetch catalog sống lỗi |
| `HERMES_ROUTER_SEARCH_MODEL` | `auto` | `auto` = router tự routing, hoặc id từ `/v1/models/web` |
| `HERMES_ROUTER_FETCH_MODEL` | `auto` | Tương tự cho fetch (`jina-reader`, `fetch-combo`…) |
| `HERMES_ROUTER_*_TIMEOUT_S` | 30 / 110 | Timeout search / per-URL fetch |
| `HERMES_ROUTER_MAX_CHARACTERS` | router default | Cap cứng ký tự text trích xuất (`0` = mặc định router) |

## Model picker

`fetch_models()` gọi `GET {base}/v1/models` — trả đúng những gì router liệt kê:
`<provider>/<model>`, bare combo name (khuyên pin combo — router tự failover),
và `auto-router` templates. Router chết → fallback `HERMES_ROUTER_MODELS`,
không hiển thị catalog sai.

## Escape hatch — provider không hiện trong `/model`

`model_profile.py` self-contained (stdlib + `providers` chỉ) → copy drop-in được:

```bash
mkdir -p "$HERMES_HOME/plugins/model-providers/hermes-router"
cp hermes_router_hermes/model_profile.py \
   "$HERMES_HOME/plugins/model-providers/hermes-router/__init__.py"
```

Đọc lại ở lần lookup kế tiếp, **không cần restart**.

## Chẩn đoán

```bash
hermes hermes-router status           # cả 3 mặt: config, endpoints — offline
hermes hermes-router doctor           # check offline cả 3 surface
hermes hermes-router doctor --live    # + 1 search thật + 1 fetch thật + catalog
hermes hermes-router model --live     # profile + ids picker sẽ hiển thị
```

Trong session: `/hermes-router-status [live]`.

## Verify (tests sẵn trong repo)

```bash
python tests/test_provider.py        # 24 test — web envelopes, errors, register
python tests/test_model_profile.py   # 12 test — profile + catalog
python tests/test_merged_plugin.py   # 11 test — một install wire cả 3

HERMES_ROUTER_URL=... HERMES_ROUTER_KEY=... python tests/live_smoke.py
```

## Giới hạn cố ý (không phải thiếu sót)

- **Không danh sách model cứng** — catalog discover sống (`HERMES_ROUTER_MODELS`
  chỉ là fallback offline).
- **`/usage` trống** — ledger router nằm sau dashboard auth, không gọi được
  bằng API key → không ship `fetch_account_usage`; xem chi phí ở dashboard.
- **Deep crawl không hỗ trợ** — mỗi URL fetch một lần.
- **`max_tokens` → `max_characters`** xấp xỉ 4 ký tự/token (router cap theo ký tự).
- **429/402 của router** surface nguyên văn — plugin không retry vì router đã
  failover bên trong.
- **`supports_vision: true`** khai provider-wide (router proxy ảnh xuyên qua);
  per-model caps đọc từ models.dev nên id router không có — pin model ảnh bằng
  `model_overrides` trong `config.yaml` nếu cần route ảnh native.
