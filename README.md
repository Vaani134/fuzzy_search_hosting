# Fuzzy Search Hosting

Production-oriented Flask search platform with:
- Multi-database MySQL → SQLite sync
- Database-isolated indexing and search (`db_id`)
- Fuzzy ranking (RapidFuzz), synonyms, autocomplete, image-search, analytics
- Live sync progress/log/error observability

## 1. What Changed (Latest)

This project now enforces **source isolation** across sync, index, and search:
- Each connected source DB is identified by `connected_databases.id` (`db_id`).
- Synced rows are scoped with `source_db_id`.
- Search index instances are now **per database** (`get_engine(source_db_id=...)`).
- Search, autocomplete, rebuild, image-search support `db_id`.
- Search UI now has a **database dropdown** to switch sources visually.

## 2. High-Level Architecture

1. MySQL source DB(s) are configured in `connected_databases`.
2. `modules/sync_manager.py` syncs core tables + product metrics into local SQLite.
3. Data is isolated per source using `source_db_id` and scoped IDs.
4. `modules/fuzzy_search.py` builds in-memory index per `db_id`.
5. API/UI query the selected database index and rows only.

## 3. Repository Structure

```text
.
├─ app.py
├─ config.py
├─ requirements.txt
├─ SYNC_SYSTEM.md
├─ db/
│  ├─ database.py
│  ├─ schema.sql
│  └─ local.sqbpro
├─ modules/
│  ├─ fuzzy_search.py
│  ├─ sync_manager.py
│  ├─ sync_normalization.py
│  ├─ sync_errors.py
│  ├─ sync_live_logs.py
│  ├─ sync_checkpoints.py
│  ├─ db_manager.py
│  ├─ settings_manager.py
│  ├─ analytics.py
│  ├─ cache.py
│  ├─ autocomplete.py
│  ├─ image_search.py
│  ├─ synonym_suggester.py
│  ├─ zip_builder.py
│  └─ sync.py            # legacy sync path kept for compatibility
├─ routes/
│  ├─ search_routes.py
│  ├─ synonym_routes.py
│  └─ image_search_routes.py
└─ templates/
   ├─ index.html
   ├─ sync.html
   ├─ settings.html
   ├─ dashboard.html
   └─ ...
```

## 4. Core Data Model (SQLite)

Main entities:
- Catalog tables: `brands`, `categories`, `product_group`, `products`
- Source registry: `connected_databases`
- Sync tracking: `sync_jobs`, `sync_checkpoints`, `sync_log`, `sync_errors`
- Ranking signal: `product_metrics`
- Search analytics: `search_history`
- Synonyms: `synonyms`, `synonym_suggestions`
- Click signal: `product_clicks`

Isolation details:
- `products`, `brands`, `categories`, `product_group` contain `source_db_id`.
- Sync writes scoped IDs per source to avoid collisions across databases.
- `product_metrics` uses `(product_id, source_db_id)` uniqueness.

## 5. Sync Logic

Entry point:
- `POST /api/database/<db_id>/sync` (full/incremental)

Behavior:
- Creates `sync_jobs` row, initializes live state/log stream.
- Full sync deletes rows for that `source_db_id` then imports all rows.
- Incremental sync uses dual-key cursor (`updated_at`, `id`) for gap-free progress.
- Per-row error isolation logs bad rows to `sync_errors` without aborting table.
- On successful full sync: rebuilds index for that specific `db_id`.

Recovery:
- On app startup, stale `running` jobs are marked failed.
- API prevents duplicate runs by checking both live state and DB job state.

## 6. Search, Index, Ranking

Engine:
- `modules/fuzzy_search.py` maintains **per-db** in-memory engine instances.
- Query path passes `db_id` so only that source index is used.

Scoring:
- RapidFuzz blend + boost + composite ranking
- Popularity signal from `product_metrics`
- Click signal from `product_clicks`

Related features:
- Synonym normalization (`synonyms` table, hot reload)
- Query suggestion ("Did You Mean")
- Autocomplete filtered by `source_db_id`
- Image-search pipeline can search per `db_id`

## 7. UI Pages

- `/` Dashboard
- `/search` Main search page (now includes database dropdown selector)
- `/product/<id>` Product detail
- `/sync` Multi-database sync control + live status/log/errors
- `/settings` DB connection/settings screen
- `/analytics` Search analytics page

## 8. API Reference (Current)

### Search APIs (`routes/search_routes.py`)

- `GET /api/search`  
  Params: `q` (required), `db_id` (default `1`), `page`, `limit`, `sort`, `category`, `min_price`, `max_price`
- `POST /api/search/rebuild`  
  Params: optional `db_id` query param
- `GET /api/autocomplete`  
  Params: `q`, `db_id`, `limit`
- `GET /api/search/history`
- `GET /api/search/top`
- `GET /api/search/zero-results`
- `GET /api/search/trending`
- `GET /api/cache/stats`
- `POST /api/cache/clear`

### Image Search API (`routes/image_search_routes.py`)

- `POST /api/image-search`  
  Multipart image upload, supports `top_k`, `db_id`

### Synonym APIs (`routes/synonym_routes.py`)

- `GET /api/synonyms`
- `POST /api/synonyms/add`
- `DELETE /api/synonyms/<id>`
- `POST /api/synonyms/suggest`
- `GET /api/synonyms/suggestions`
- `GET /api/synonyms/suggestions/all`
- `POST /api/synonyms/approve/<id>`
- `POST /api/synonyms/reject/<id>`

### Multi-Database APIs (`app.py`)

- `GET /api/databases`
- `POST /api/databases`
- `GET /api/database/<db_id>`
- `PUT /api/database/<db_id>`
- `DELETE /api/database/<db_id>`
- `POST /api/database/<db_id>/test`
- `POST /api/database/<db_id>/sync`
- `POST /api/database/<db_id>/stop`
- `GET /api/database/<db_id>/status`
- `GET /api/database/<db_id>/logs`
- `GET /api/database/<db_id>/errors`
- `GET /api/databases/live`

### Other APIs (`app.py`)

- `GET /api/product/<id>`
- `POST /api/product/<id>/click`
- `GET /api/stats` (supports `db_id`)
- `POST /api/download-zip`

### Legacy APIs (kept for compatibility)

- `POST /api/sync`
- `GET /api/sync/live`
- `GET /api/sync/history`
- `GET /api/sync/status`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/settings/test`

## 9. Setup

### Requirements

- Python 3.10+
- SQLite (bundled with Python)
- MySQL source access (for sync)
- Optional Redis for cache backend

### Install

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## 10. Operational Notes

- After introducing isolation changes, run **one full sync per database**.
- Use `/search?db_id=<id>&q=...` to query a specific source directly.
- Search page dropdown now does this automatically.
- Incremental sync does not always rebuild index; use `POST /api/search/rebuild?db_id=<id>` when needed.

## 11. Key Files to Read First

- `app.py` (route composition and startup behavior)
- `modules/sync_manager.py` (sync engine + checkpoints + logs)
- `modules/fuzzy_search.py` (indexing and ranking)
- `routes/search_routes.py` (search API contracts)
- `templates/index.html` (search UI, db selector)
- `SYNC_SYSTEM.md` (deep sync internals)
