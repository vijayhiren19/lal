# Developer Steps: Full Pipeline via REST API

End-to-end steps to run the Stock Scrip Scoring pipeline from a clean database using the Flask REST API only.

## Prerequisites

### Activate virtual environment

```bash
# Linux / macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### Install dependencies

```bash
python -m pip install -r requirements.txt
```

### Clean up temp scripts

After creating any `temp_*.py` or `check_*.py` scratch files during development, remove them before committing:

```bash
# Linux / macOS
rm -f temp_*.py check_*.py

# Windows (PowerShell)
Remove-Item -Path "temp_*.py", "check_*.py" -ErrorAction SilentlyContinue
```

```bash
git status  # verify no stray scripts
```

## 1. Start the Flask API Server

```bash
python -m src.api.app --host 0.0.0.0 --port=5000
```

The server auto-creates `mydb1.db` with all tables + `stock_universe` view on first request.

## 2. Health Check

```bash
curl -s http://127.0.0.1:5000/api/v1/health | python -m json.tool
```

## 3. Build Reference Tables (Optional but Recommended)

These are **not** part of the pipeline `--stages all` and must be triggered separately:

```bash
# F&O membership (311 rows)
curl -s -X POST http://127.0.0.1:5000/api/v1/pipeline/build/fno-membership

# Index membership for sector diversification (6,525 rows across 20+ indices)
curl -s -X POST http://127.0.0.1:5000/api/v1/pipeline/build/index-history
```

Wait for each to complete before proceeding (poll status endpoint).

## 4. Trigger Full Pipeline (Async)

```bash
curl -s -X POST http://127.0.0.1:5000/api/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{"stages":"all","start_date":"2025-01-01","end_date":"2026-06-30"}' | python -m json.tool
```

```powershell
$body = @{stages="all";start_date="2025-01-01";end_date="2026-06-30"} | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/pipeline/run" -Method Post -Body $body -ContentType "application/json"
```

Returns immediately with a `job_id`.

## 5. Poll Until Complete

```bash
JOB_ID="pipeline_20260704_205657_865520"  # replace with your job_id
while true; do
  STATUS=$(curl -s "http://127.0.0.1:5000/api/v1/pipeline/status?job_id=$JOB_ID" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "$(date '+%H:%M:%S') Status: $STATUS"
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
  sleep 10
done
curl -s "http://127.0.0.1:5000/api/v1/pipeline/status?job_id=$JOB_ID" | python -m json.tool
```

```powershell
$jobId = "pipeline_20260704_205657_865520"  # replace with your job_id
while ($true) {
    $s = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/pipeline/status?job_id=$jobId"
    Write-Output "$(Get-Date -Format 'HH:mm:ss') Status: $($s.status)"
    if ($s.status -in @('completed','failed')) { $s | ConvertTo-Json; break }
    Start-Sleep -Seconds 10
}
```

Full run (Jan 2025 – Jun 2026, ~367 trading days) completes in **~23 minutes** on modern hardware.

### Stage Timing (Reference)

| Stage | Time |
|-------|------|
| fetch | 5s |
| equity_master | <1s |
| enrich | 10s |
| technical | 38s |
| price_level | 25s |
| momentum | 18s |
| volatility | 12s |
| averages | 38s |
| derivatives | 3s |
| score | 2s |
| hits | <1s |

## 6. Verify Data

### Health Endpoint (table row counts)

```bash
curl -s http://127.0.0.1:5000/api/v1/health | python -m json.tool
```

Expected: `mydb1.db` ~1.5 GB, all 18 tables populated, `last_data_date: "2026-06-30"`.

### Available Date Ranges

```bash
curl -s http://127.0.0.1:5000/api/v1/dates | python -m json.tool
```

Expected: `daily.min_date: "2025-01-01"`, `daily.max_date: "2026-06-30"`.

### Top Picks for a Given Date

**Note:** Use `overall_score`, not `score` (the response contains the full stock_universe view with all scoring components).

```bash
curl -s "http://127.0.0.1:5000/api/v1/picks?date=2026-06-30&columns=symbol,overall_score,rank,sector&limit=5" | python -m json.tool
```

### Stock Data Query with Column Selection

```bash
curl -s "http://127.0.0.1:5000/api/v1/stocks?date=2026-06-30&symbol=RELIANCE&columns=symbol,close_price,day_return_pct,delivery_pct,rsi_14,overall_score" | python -m json.tool
```

### Flexible Filtering

```bash
# Order by score descending, min_score filter
curl -s "http://127.0.0.1:5000/api/v1/stocks?date=2026-06-30&min_score=40&order_by=overall_score&order_dir=desc&limit=10&columns=symbol,overall_score,day_return_pct" | python -m json.tool

# CSV export
curl -s "http://127.0.0.1:5000/api/v1/stocks?date=2026-06-30&columns=symbol,close_price,overall_score&format=csv" -o stocks_2026-06-30.csv
```

### Hit Analysis

```bash
curl -s http://127.0.0.1:5000/api/v1/hits/analyze | python -m json.tool
```

### History for a Symbol

```bash
curl -s "http://127.0.0.1:5000/api/v1/stocks/history?symbol=RELIANCE&start_date=2025-01-01&end_date=2025-01-31&columns=trade_date,close_price,day_return_pct" | python -m json.tool
```

### Search

```bash
curl -s "http://127.0.0.1:5000/api/v1/stocks/search?q=INFY" | python -m json.tool
```

### Column Discovery

```bash
curl -s http://127.0.0.1:5000/api/v1/columns | python -m json.tool
```

### Available Pipeline Stages

```bash
curl -s http://127.0.0.1:5000/api/v1/pipeline/stages | python -m json.tool
```

## 7. Re-run Scoring Only (After Membership Builds)

After building `fno_membership` and `index_membership`, re-run scoring to get sector-diversified picks and F&O boosts:

```bash
curl -s -X POST http://127.0.0.1:5000/api/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{"stages":"score,hits","start_date":"2025-01-01","end_date":"2026-06-30"}' | python -m json.tool
```

```powershell
$body = @{stages="score,hits";start_date="2025-01-01";end_date="2026-06-30"} | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/pipeline/run" -Method Post -Body $body -ContentType "application/json"
```

This re-scores all 367 dates in ~2 minutes (no re-fetching or re-computing technicals).

## 8. Daily Workflow

```bash
# After market close each day:
curl -s -X POST http://127.0.0.1:5000/api/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{"stages":"all","days_back":1}' | python -m json.tool
```

```powershell
# After market close each day:
$body = @{stages="all";days_back=1} | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/pipeline/run" -Method Post -Body $body -ContentType "application/json"
```

## Verification Results (Jan 2025 – Jun 2026)

| Metric | Value |
|--------|-------|
| DB size | 1.5 GB |
| Trading dates | 367 |
| Stage count | 11 |
| Total pipeline time | ~23 min |
| Scored symbols/day | ~1,450 avg |
| Scoring picks total | 7,342 |
| Picks per day | ~20 (sector-diversified) |
| Sectors present | 11 NSE index sectors |
| Hit rate (any target) | 19.86% |
| Tg1 (4% in 5d) | 1.15% |
| Tg2 (5% in 10d) | 9.25% |
| Tg3 (10% in 15d) | 9.46% |
| `fno_membership` | 311 rows |
| `index_membership` | 6,525 rows |
