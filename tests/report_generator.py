"""
tests/report_generator.py
--------------------------
Reads pytest-json-report output and generates:
  - tests/reports/test_report.json   (enriched)
  - tests/reports/test_report.csv
  - tests/reports/test_report.html
  - tests/reports/production_readiness_report.json
  - tests/reports/production_readiness_report.html
  - tests/reports/dashboard.html     (combined visual dashboard)

Usage:
    python tests/report_generator.py --json .report.json
    python tests/report_generator.py --json .report.json --benchmark benchmark_report.json
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(n: int, total: int) -> float:
    return round(n / total * 100, 1) if total else 0.0


def _load_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    print(f"  Saved {path}")


# ── Parse pytest-json-report ──────────────────────────────────────────────────

def parse_pytest_report(raw: Dict) -> List[Dict]:
    rows = []
    for test in raw.get("tests", []):
        node   = test.get("nodeid", "")
        parts  = node.split("::")
        module = parts[0].replace("/", ".").replace("\\", ".").rstrip(".py")
        name   = "::".join(parts[1:]) if len(parts) > 1 else node

        # Determine category from path
        if "unit"        in module: category = "unit"
        elif "integration" in module: category = "integration"
        elif "performance" in module: category = "performance"
        elif "concurrency" in module: category = "concurrency"
        elif "memory"    in module: category = "memory"
        else:                        category = "other"

        outcome = test.get("outcome", "unknown").upper()
        call    = test.get("call", {}) or {}
        setup   = test.get("setup", {}) or {}

        duration = round(
            (call.get("duration") or 0) + (setup.get("duration") or 0), 4
        )
        error_msg   = call.get("longrepr") or setup.get("longrepr") or ""
        crash       = call.get("crash") or setup.get("crash") or {}
        stack_trace = crash.get("traceback", "") if isinstance(crash, dict) else ""

        rows.append({
            "test_name":    name,
            "category":     category,
            "module":       module,
            "start_time":   "",
            "end_time":     "",
            "duration_s":   duration,
            "status":       outcome,
            "error_message": str(error_msg)[:500] if error_msg else "",
            "stack_trace":  str(stack_trace)[:1000] if stack_trace else "",
            "memory_mb":    "",
            "cpu_pct":      "",
        })

    return rows


# ── Save CSV ──────────────────────────────────────────────────────────────────

def save_csv(rows: List[Dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {path}")


# ── HTML test report ──────────────────────────────────────────────────────────

def _status_badge(status: str) -> str:
    colour = {
        "PASSED":  "#2ecc71",
        "FAILED":  "#e74c3c",
        "ERROR":   "#e74c3c",
        "SKIPPED": "#f39c12",
    }.get(status.upper(), "#95a5a6")
    return f'<span style="background:{colour};color:#fff;padding:2px 8px;border-radius:3px;font-size:0.8em">{status}</span>'


def save_html_report(rows: List[Dict], summary: Dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    total   = summary["total"]
    passed  = summary["passed"]
    failed  = summary["failed"]
    skipped = summary["skipped"]
    duration = summary["duration_s"]

    rows_html = ""
    for r in rows:
        rows_html += f"""
        <tr>
          <td style="max-width:400px;word-break:break-all">{r['test_name']}</td>
          <td><span class="cat">{r['category']}</span></td>
          <td>{_status_badge(r['status'])}</td>
          <td>{r['duration_s']:.4f}s</td>
          <td style="max-width:300px;font-size:0.8em;color:#c0392b">{r['error_message'][:150] if r['error_message'] else ''}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Test Report — Fuzzy Search</title>
<style>
  body{{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5}}
  h1{{color:#2c3e50}} table{{width:100%;border-collapse:collapse;background:#fff}}
  th{{background:#2c3e50;color:#fff;padding:8px;text-align:left}}
  td{{padding:6px 8px;border-bottom:1px solid #ddd;vertical-align:top}}
  tr:hover{{background:#f0f0f0}}
  .summary{{display:flex;gap:20px;margin:20px 0}}
  .card{{background:#fff;padding:15px 25px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,.1);text-align:center}}
  .card h2{{margin:0;font-size:2em}} .card p{{margin:4px 0;color:#666}}
  .cat{{background:#3498db;color:#fff;padding:2px 6px;border-radius:3px;font-size:0.75em}}
</style>
</head>
<body>
<h1>Test Report — Fuzzy Search Hosting</h1>
<p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="summary">
  <div class="card"><h2>{total}</h2><p>Total Tests</p></div>
  <div class="card" style="border-top:4px solid #2ecc71"><h2 style="color:#2ecc71">{passed}</h2><p>Passed</p></div>
  <div class="card" style="border-top:4px solid #e74c3c"><h2 style="color:#e74c3c">{failed}</h2><p>Failed</p></div>
  <div class="card" style="border-top:4px solid #f39c12"><h2 style="color:#f39c12">{skipped}</h2><p>Skipped</p></div>
  <div class="card"><h2>{duration:.2f}s</h2><p>Duration</p></div>
  <div class="card"><h2>{_pct(passed, total)}%</h2><p>Pass Rate</p></div>
</div>
<table>
<thead><tr><th>Test Name</th><th>Category</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Saved {path}")


# ── Production readiness report ───────────────────────────────────────────────

def build_production_readiness(
    rows: List[Dict],
    benchmark_data: Optional[Dict] = None,
) -> Dict:

    def _cat_pass_rate(cat: str) -> float:
        cat_rows = [r for r in rows if r["category"] == cat]
        if not cat_rows:
            return 100.0
        passed = sum(1 for r in cat_rows if r["status"] == "PASSED")
        return _pct(passed, len(cat_rows))

    categories = ["unit", "integration", "performance", "concurrency", "memory"]
    cat_results = {
        cat: {
            "pass_rate": _cat_pass_rate(cat),
            "total":  len([r for r in rows if r["category"] == cat]),
            "passed": len([r for r in rows if r["category"] == cat and r["status"] == "PASSED"]),
            "failed": len([r for r in rows if r["category"] == cat and r["status"] == "FAILED"]),
        }
        for cat in categories
    }

    # Score: weighted average of category pass rates
    weights = {"unit": 25, "integration": 25, "performance": 20, "concurrency": 20, "memory": 10}
    total_weight = sum(weights.values())
    score = sum(
        cat_results[cat]["pass_rate"] * weights[cat] / total_weight
        for cat in categories
    )
    score = round(score, 1)

    if score >= 90:
        grade, status = "A", "PASS"
    elif score >= 80:
        grade, status = "B", "PASS"
    elif score >= 70:
        grade, status = "C", "FAIL"
    else:
        grade, status = "D", "FAIL"

    recommendations = []
    if cat_results["unit"]["pass_rate"] < 95:
        recommendations.append("Fix failing unit tests — core module logic has defects")
    if cat_results["integration"]["pass_rate"] < 90:
        recommendations.append("Integration test failures indicate API contract issues")
    if cat_results["concurrency"]["pass_rate"] < 100:
        recommendations.append("CRITICAL: Concurrency failures indicate race conditions or data corruption")
    if cat_results["memory"]["pass_rate"] < 100:
        recommendations.append("Memory leak detected — investigate search/cache/rebuild loops")
    if cat_results["performance"]["pass_rate"] < 100:
        recommendations.append("Latency thresholds exceeded — profile and optimise hot paths")

    if not recommendations:
        recommendations.append("All checks passed — system is production ready")

    total_tests  = len(rows)
    total_passed = sum(1 for r in rows if r["status"] == "PASSED")
    total_failed = sum(1 for r in rows if r["status"] == "FAILED")

    report = {
        "generated_at":       datetime.now().isoformat(),
        "overall_status":     status,
        "production_readiness_score": score,
        "grade":              grade,
        "grade_label": {
            "A": "Production Ready (90-100)",
            "B": "Minor Improvements Needed (80-89)",
            "C": "Significant Improvements Needed (70-79)",
            "D": "Not Production Ready (<70)",
        }[grade],
        "summary": {
            "total":   total_tests,
            "passed":  total_passed,
            "failed":  total_failed,
            "pass_rate_pct": _pct(total_passed, total_tests),
        },
        "startup_tests":      cat_results.get("unit", {}),
        "cache_tests":        cat_results.get("unit", {}),
        "search_tests":       cat_results.get("integration", {}),
        "autocomplete_tests": cat_results.get("integration", {}),
        "concurrency_tests":  cat_results.get("concurrency", {}),
        "memory_tests":       cat_results.get("memory", {}),
        "performance_tests":  cat_results.get("performance", {}),
        "category_breakdown": cat_results,
        "benchmarks":         benchmark_data or {},
        "recommendations":    recommendations,
    }
    return report


def save_production_readiness_html(report: Dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    score   = report["production_readiness_score"]
    status  = report["overall_status"]
    grade   = report["grade"]
    colour  = "#2ecc71" if status == "PASS" else "#e74c3c"

    recs_html = "".join(
        f"<li>{r}</li>" for r in report.get("recommendations", [])
    )

    cats_html = ""
    for cat, data in report.get("category_breakdown", {}).items():
        pr = data.get("pass_rate", 0)
        bar_colour = "#2ecc71" if pr >= 90 else "#f39c12" if pr >= 70 else "#e74c3c"
        cats_html += f"""
        <tr>
          <td>{cat.title()}</td>
          <td>{data.get('total', 0)}</td>
          <td>{data.get('passed', 0)}</td>
          <td>{data.get('failed', 0)}</td>
          <td>
            <div style="background:#eee;border-radius:4px;height:16px;width:200px">
              <div style="background:{bar_colour};height:16px;border-radius:4px;width:{pr}%"></div>
            </div>
            {pr}%
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Production Readiness Report</title>
<style>
  body{{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5}}
  .score-circle{{width:150px;height:150px;border-radius:50%;background:{colour};
    display:flex;align-items:center;justify-content:center;flex-direction:column;
    margin:auto;color:#fff}}
  .score-circle h2{{font-size:3em;margin:0}} .score-circle p{{margin:0;font-size:1em}}
  table{{width:100%;border-collapse:collapse;background:#fff;margin-top:20px}}
  th{{background:#2c3e50;color:#fff;padding:8px}} td{{padding:8px;border-bottom:1px solid #ddd}}
  ul{{background:#fff;padding:20px 30px;border-radius:8px;margin-top:20px}}
  li{{margin:6px 0}}
</style>
</head>
<body>
<h1>Production Readiness Report — Fuzzy Search Hosting</h1>
<p>Generated: {report['generated_at']}</p>

<div class="score-circle">
  <h2>{score}</h2>
  <p>Grade: {grade}</p>
  <p>{status}</p>
</div>
<p style="text-align:center;margin-top:10px;color:#666">{report.get('grade_label','')}</p>

<h2>Category Breakdown</h2>
<table>
<thead><tr><th>Category</th><th>Total</th><th>Passed</th><th>Failed</th><th>Pass Rate</th></tr></thead>
<tbody>{cats_html}</tbody>
</table>

<h2>Recommendations</h2>
<ul>{recs_html}</ul>
</body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Saved {path}")


# ── Dashboard ─────────────────────────────────────────────────────────────────

def save_dashboard(
    rows: List[Dict],
    prod_report: Dict,
    benchmark_data: Optional[Dict],
    path: str,
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    score  = prod_report["production_readiness_score"]
    status = prod_report["overall_status"]
    colour = "#2ecc71" if status == "PASS" else "#e74c3c"

    total   = len(rows)
    passed  = sum(1 for r in rows if r["status"] == "PASSED")
    failed  = sum(1 for r in rows if r["status"] == "FAILED")
    skipped = sum(1 for r in rows if r["status"] == "SKIPPED")

    # Build category data for chart
    cat_labels = []
    cat_pass   = []
    cat_fail   = []
    for cat in ["unit", "integration", "performance", "concurrency", "memory"]:
        cat_rows = [r for r in rows if r["category"] == cat]
        if not cat_rows:
            continue
        cat_labels.append(cat.title())
        cat_pass.append(sum(1 for r in cat_rows if r["status"] == "PASSED"))
        cat_fail.append(sum(1 for r in cat_rows if r["status"] == "FAILED"))

    # Benchmark table rows
    bench_rows_html = ""
    if benchmark_data:
        for bname, bdata in benchmark_data.get("benchmarks", {}).items():
            mean_ms = round((bdata.get("stats", {}).get("mean", 0) or 0) * 1000, 3)
            bench_rows_html += f"<tr><td>{bname}</td><td>{mean_ms}ms</td></tr>"

    # Recent failed tests
    failed_rows_html = ""
    for r in [x for x in rows if x["status"] in ("FAILED", "ERROR")][:10]:
        failed_rows_html += f"""
        <tr>
          <td style="word-break:break-all">{r['test_name']}</td>
          <td>{r['category']}</td>
          <td style="color:#e74c3c">{r['error_message'][:120]}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Test Dashboard — Fuzzy Search</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  body{{font-family:Arial,sans-serif;margin:0;background:#1a1a2e;color:#eee}}
  .header{{background:#16213e;padding:20px 30px;display:flex;align-items:center;gap:20px}}
  .header h1{{margin:0;color:#e94560}} .header p{{margin:0;color:#aaa}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px;padding:20px}}
  .card{{background:#16213e;border-radius:10px;padding:20px;text-align:center}}
  .card h2{{font-size:2.5em;margin:0}} .card p{{color:#aaa;margin:4px 0}}
  .charts{{display:grid;grid-template-columns:1fr 1fr;gap:15px;padding:0 20px 20px}}
  .chart-box{{background:#16213e;border-radius:10px;padding:20px}}
  table{{width:100%;border-collapse:collapse}} th{{background:#0f3460;color:#fff;padding:8px}}
  td{{padding:7px;border-bottom:1px solid #333}}
  .section{{background:#16213e;border-radius:10px;margin:0 20px 20px;padding:20px}}
  .score{{font-size:4em;font-weight:bold;color:{colour}}}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>Fuzzy Search — Test Dashboard</h1>
    <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
  </div>
  <div style="margin-left:auto;text-align:right">
    <div class="score">{score}</div>
    <div style="color:{colour};font-size:1.2em">{'PRODUCTION READY' if status=='PASS' else 'NOT READY'}</div>
  </div>
</div>

<div class="grid">
  <div class="card"><h2>{total}</h2><p>Total Tests</p></div>
  <div class="card"><h2 style="color:#2ecc71">{passed}</h2><p>Passed</p></div>
  <div class="card"><h2 style="color:#e74c3c">{failed}</h2><p>Failed</p></div>
  <div class="card"><h2 style="color:#f39c12">{skipped}</h2><p>Skipped</p></div>
  <div class="card"><h2>{_pct(passed, total)}%</h2><p>Pass Rate</p></div>
</div>

<div class="charts">
  <div class="chart-box">
    <h3>Pass / Fail by Category</h3>
    <canvas id="catChart" height="250"></canvas>
  </div>
  <div class="chart-box">
    <h3>Overall Pass/Fail</h3>
    <canvas id="pieChart" height="250"></canvas>
  </div>
</div>

<div class="section">
  <h3>Failed Tests</h3>
  {f'<table><thead><tr><th>Test</th><th>Category</th><th>Error</th></tr></thead><tbody>{failed_rows_html}</tbody></table>' if failed_rows_html else '<p style="color:#2ecc71">No failures!</p>'}
</div>

{'<div class="section"><h3>Benchmark Summary</h3><table><thead><tr><th>Benchmark</th><th>Mean Latency</th></tr></thead><tbody>' + bench_rows_html + '</tbody></table></div>' if bench_rows_html else ''}

<script>
new Chart(document.getElementById('catChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(cat_labels)},
    datasets: [
      {{label:'Passed', data:{json.dumps(cat_pass)}, backgroundColor:'#2ecc71'}},
      {{label:'Failed', data:{json.dumps(cat_fail)}, backgroundColor:'#e74c3c'}}
    ]
  }},
  options: {{responsive:true, plugins:{{legend:{{labels:{{color:'#eee'}}}}}},
    scales:{{x:{{ticks:{{color:'#eee'}}}},y:{{ticks:{{color:'#eee'}}}}}}}}
}});
new Chart(document.getElementById('pieChart'), {{
  type: 'doughnut',
  data: {{
    labels: ['Passed','Failed','Skipped'],
    datasets: [{{data:[{passed},{failed},{skipped}],
      backgroundColor:['#2ecc71','#e74c3c','#f39c12']}}]
  }},
  options: {{responsive:true,plugins:{{legend:{{labels:{{color:'#eee'}}}}}}}}
}});
</script>
</body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Saved {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate test reports")
    parser.add_argument("--json",      required=True, help="pytest-json-report output file")
    parser.add_argument("--benchmark", default="",    help="pytest-benchmark JSON output")
    args = parser.parse_args()

    if not os.path.isfile(args.json):
        print(f"Error: JSON report not found: {args.json}")
        sys.exit(1)

    print(f"\nReading {args.json}…")
    raw = _load_json(args.json)

    benchmark_data = None
    if args.benchmark and os.path.isfile(args.benchmark):
        benchmark_data = _load_json(args.benchmark)

    rows = parse_pytest_report(raw)

    summary = raw.get("summary", {})
    summary_out = {
        "total":    summary.get("total", len(rows)),
        "passed":   summary.get("passed", 0),
        "failed":   summary.get("failed", 0),
        "skipped":  summary.get("skipped", 0),
        "duration_s": round(raw.get("duration", 0), 3),
    }

    print("Generating reports…")
    _save_json(f"{REPORTS_DIR}/test_report.json", {"summary": summary_out, "tests": rows})
    save_csv(rows, f"{REPORTS_DIR}/test_report.csv")
    save_html_report(rows, summary_out, f"{REPORTS_DIR}/test_report.html")

    prod = build_production_readiness(rows, benchmark_data)
    _save_json(f"{REPORTS_DIR}/production_readiness_report.json", prod)
    save_production_readiness_html(prod, f"{REPORTS_DIR}/production_readiness_report.html")

    save_dashboard(rows, prod, benchmark_data, f"{REPORTS_DIR}/dashboard.html")

    score  = prod["production_readiness_score"]
    status = prod["overall_status"]
    print(f"\n{'='*50}")
    print(f"Production Readiness Score: {score}/100  [{prod['grade']}]  {status}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
