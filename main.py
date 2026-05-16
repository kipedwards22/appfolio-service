import asyncio
import json
import re
import urllib.parse
from datetime import datetime
from typing import Any, Optional
import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(title="AppFolio Service", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

APPFOLIO_DOMAIN = "evergreenmanagement.appfolio.com"
APPFOLIO_URL = f"https://{APPFOLIO_DOMAIN}"
API_SECRET = os.environ.get("SERVICE_SECRET", "changeme")

# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_secret(x_service_secret: str = Header(...)):
    if x_service_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid service secret")

def get_cookies() -> str:
    cookie = os.environ.get("APPFOLIO_COOKIES", "")
    if not cookie:
        raise HTTPException(status_code=503, detail="AppFolio cookies not configured")
    return cookie

# ── HTTP client ───────────────────────────────────────────────────────────────

async def af_get(path: str, cookies: str, params: dict = None, extra_headers: dict = None) -> Any:
    headers = {
        "Host": APPFOLIO_DOMAIN,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Cookie": cookies,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    if extra_headers:
        headers.update(extra_headers)

    url = f"{APPFOLIO_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params, allow_redirects=True) as resp:
            if resp.status == 403:
                raise HTTPException(status_code=403, detail="AppFolio session expired — update APPFOLIO_COOKIES")
            if resp.status == 401:
                raise HTTPException(status_code=401, detail="AppFolio auth failed")
            text = await resp.text()
            return text

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_table_response(data: dict, headers_override: list = None) -> list:
    thead_html = data.get("thead_row", "")
    soup = BeautifulSoup(thead_html, "html.parser")
    col_headers = headers_override or [th.get_text(strip=True) for th in soup.find_all("th")]

    rows = []
    for row in data.get("body_row_data", []):
        cells = row.get("data", [])
        values = [BeautifulSoup(c.get("value", ""), "html.parser").get_text(strip=True) for c in cells]
        if len(values) < len(col_headers):
            values.extend([""] * (len(col_headers) - len(values)))
        row_dict = dict(zip(col_headers, values))

        # Extract IDs from links in first cell
        if cells:
            first_html = cells[0].get("value", "")
            a = BeautifulSoup(first_html, "html.parser").find("a")
            if a and a.get("href"):
                parts = a["href"].strip("/").split("/")
                for i, part in enumerate(parts):
                    if part == "occupancies" and i + 1 < len(parts):
                        row_dict["occupancy_id"] = parts[i + 1]
                    if part == "selected_tenant" and i + 1 < len(parts):
                        row_dict["tenant_id"] = parts[i + 1]
                    if part == "service_requests" and i + 1 < len(parts):
                        row_dict["service_request_id"] = parts[i + 1]
        rows.append(row_dict)
    return rows

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True, "service": "appfolio", "domain": APPFOLIO_DOMAIN}
@app.get("/debug-secret")
async def debug_secret():
    return {"secret_length": len(API_SECRET), "secret_first3": API_SECRET[:3], "raw": os.environ.get("SERVICE_SECRET", "NOT_FOUND")}
@app.get("/tenants", dependencies=[Depends(verify_secret)])
async def get_tenants(page: int = 1):
    cookies = get_cookies()
    raw = await af_get("/occupancies", cookies, params={
        "page": page, "sort[by]": "name", "sort[order]": "asc"
    })
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"page": page, "count": len(rows), "tenants": rows}

@app.get("/tenants/all", dependencies=[Depends(verify_secret)])
async def get_all_tenants():
    cookies = get_cookies()
    all_rows = []
    page = 1
    while True:
        raw = await af_get("/occupancies", cookies, params={
            "page": page, "sort[by]": "name", "sort[order]": "asc"
        })
        data = json.loads(raw)
        rows = parse_table_response(data)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 30:  # AppFolio default page size
            break
        page += 1
    return {"count": len(all_rows), "tenants": all_rows}

@app.get("/work-orders", dependencies=[Depends(verify_secret)])
async def get_work_orders(status: str = "Open", page: int = 1):
    cookies = get_cookies()
    status_codes = {
        "Open": "Open", "New": "0", "Assigned": "9", "Scheduled": "3",
        "Waiting": "6", "Estimate Requested": "1", "Estimated": "2",
        "Work Done": "8", "Ready to Bill": "12", "Completed": "4",
        "Canceled": "5",
    }
    code = status_codes.get(status, "Open")
    raw = await af_get("/maintenance/service_requests", cookies, params={
        "page": page,
        "filter[state]": code,
        "sort[by]": "created_at",
        "sort[order]": "desc",
    })
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"status": status, "page": page, "count": len(rows), "work_orders": rows}

@app.get("/work-orders/all", dependencies=[Depends(verify_secret)])
async def get_all_work_orders():
    cookies = get_cookies()
    statuses = ["Open", "New", "Assigned", "Scheduled", "Waiting",
                "Estimate Requested", "Estimated", "Work Done", "Ready to Bill"]
    all_orders = []
    for status in statuses:
        page = 1
        while True:
            try:
                result = await get_work_orders(status=status, page=page)
                rows = result["work_orders"]
                for r in rows:
                    r["status_group"] = status
                all_orders.extend(rows)
                if len(rows) < 30:
                    break
                page += 1
            except Exception:
                break
    return {"count": len(all_orders), "work_orders": all_orders}

@app.get("/delinquencies", dependencies=[Depends(verify_secret)])
async def get_delinquencies(page: int = 1):
    cookies = get_cookies()
    raw = await af_get("/reports/delinquency", cookies, params={"page": page})
    soup = BeautifulSoup(raw, "html.parser")
    rows = []
    table = soup.find("table")
    if table:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells and len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
    return {"page": page, "count": len(rows), "delinquencies": rows}

@app.get("/leases", dependencies=[Depends(verify_secret)])
async def get_leases(page: int = 1, expiring_days: int = None):
    cookies = get_cookies()
    params = {"page": page, "sort[by]": "lease_end", "sort[order]": "asc"}
    if expiring_days:
        params["filter[expiring_within_days]"] = expiring_days
    raw = await af_get("/occupancies", cookies, params=params)
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"page": page, "count": len(rows), "leases": rows}

@app.get("/leases/expiring", dependencies=[Depends(verify_secret)])
async def get_expiring_leases():
    """Leases expiring within 90 days"""
    cookies = get_cookies()
    all_rows = []
    page = 1
    while True:
        raw = await af_get("/occupancies", cookies, params={
            "page": page,
            "sort[by]": "lease_end",
            "sort[order]": "asc",
            "filter[status]": "current",
        })
        data = json.loads(raw)
        rows = parse_table_response(data)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 30:
            break
        page += 1
    # Filter for leases ending within 90 days
    today = datetime.today()
    expiring = []
    for row in all_rows:
        lease_end = row.get("Lease End", "") or row.get("lease_end", "")
        if lease_end:
            try:
                end_date = datetime.strptime(lease_end, "%m/%d/%Y")
                days_left = (end_date - today).days
                if 0 <= days_left <= 90:
                    row["days_until_expiry"] = days_left
                    expiring.append(row)
            except Exception:
                pass
    expiring.sort(key=lambda x: x.get("days_until_expiry", 999))
    return {"count": len(expiring), "expiring_leases": expiring}

@app.get("/properties", dependencies=[Depends(verify_secret)])
async def get_properties(page: int = 1):
    cookies = get_cookies()
    raw = await af_get("/properties", cookies, params={"page": page})
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"page": page, "count": len(rows), "properties": rows}

@app.get("/owner-statements", dependencies=[Depends(verify_secret)])
async def get_owner_statements(page: int = 1):
    cookies = get_cookies()
    raw = await af_get("/owner_statements", cookies, params={"page": page})
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"page": page, "count": len(rows), "owner_statements": rows}

@app.get("/vendors", dependencies=[Depends(verify_secret)])
async def get_vendors(page: int = 1):
    cookies = get_cookies()
    raw = await af_get("/vendors", cookies, params={"page": page})
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"page": page, "count": len(rows), "vendors": rows}

@app.get("/applications", dependencies=[Depends(verify_secret)])
async def get_applications(page: int = 1):
    cookies = get_cookies()
    raw = await af_get("/applications", cookies, params={"page": page})
    data = json.loads(raw)
    rows = parse_table_response(data)
    return {"page": page, "count": len(rows), "applications": rows}

# ── Master sync endpoint ──────────────────────────────────────────────────────

@app.get("/sync/all", dependencies=[Depends(verify_secret)])
async def sync_all():
    """
    Pull everything from AppFolio and return it formatted for
    Chief of Staff context sync (/api/public/context/sync).
    """
    results = {}
    errors = {}

    async def safe_fetch(key, coro):
        try:
            results[key] = await coro
        except Exception as e:
            errors[key] = str(e)

    await asyncio.gather(
        safe_fetch("tenants", get_all_tenants()),
        safe_fetch("work_orders", get_all_work_orders()),
        safe_fetch("expiring_leases", get_expiring_leases()),
        safe_fetch("applications", get_applications()),
    )

    # Format for CoS context sync
    lines = [f"# AppFolio Data Sync — {datetime.now().strftime('%B %d, %Y %I:%M %p')}\n"]

    if "tenants" in results:
        t = results["tenants"]
        lines.append(f"## Tenants ({t['count']} total)")
        for tenant in t["tenants"][:50]:  # Cap at 50 for context size
            name = tenant.get("Name", tenant.get("Tenant", "Unknown"))
            unit = tenant.get("Unit Name", tenant.get("Unit", ""))
            status = tenant.get("Status", "")
            balance = tenant.get("Balance", "")
            lines.append(f"- {name} | {unit} | {status} | Balance: {balance}")
        lines.append("")

    if "work_orders" in results:
        wo = results["work_orders"]
        lines.append(f"## Work Orders ({wo['count']} active)")
        for order in wo["work_orders"][:100]:
            desc = order.get("Description", order.get("Issue", "No description"))
            prop = order.get("Property", order.get("Unit", ""))
            status = order.get("Status", order.get("status_group", ""))
            created = order.get("Created", order.get("Date", ""))
            lines.append(f"- [{status}] {desc} | {prop} | {created}")
        lines.append("")

    if "expiring_leases" in results:
        el = results["expiring_leases"]
        lines.append(f"## Leases Expiring Within 90 Days ({el['count']})")
        for lease in el["expiring_leases"]:
            name = lease.get("Name", lease.get("Tenant", "Unknown"))
            unit = lease.get("Unit Name", lease.get("Unit", ""))
            end = lease.get("Lease End", "")
            days = lease.get("days_until_expiry", "?")
            lines.append(f"- {name} | {unit} | Expires: {end} ({days} days)")
        lines.append("")

    if "applications" in results:
        apps = results["applications"]
        lines.append(f"## Pending Applications ({apps['count']})")
        for app_row in apps["applications"][:20]:
            name = app_row.get("Name", "Unknown")
            unit = app_row.get("Unit", app_row.get("Property", ""))
            status = app_row.get("Status", "")
            lines.append(f"- {name} | {unit} | {status}")
        lines.append("")

    if errors:
        lines.append("## Sync Errors")
        for key, err in errors.items():
            lines.append(f"- {key}: {err}")

    return {
        "ok": True,
        "synced_at": datetime.now().isoformat(),
        "errors": errors,
        "cos_payload": {
            "title": f"AppFolio Live Data — {datetime.now().strftime('%b %d %Y')}",
            "category": "custom",
            "content": "\n".join(lines),
        },
        "raw": results,
    }


# ── Report endpoints ──────────────────────────────────────────────────────────

from reports import run_owner_statement, run_rent_roll, get_owners_list
from pydantic import BaseModel

class OwnerStatementRequest(BaseModel):
    owner_id: str          # e.g. "o_564"
    owner_name: str = ""
    date_from: str         # MM/DD/YYYY
    date_to: str           # MM/DD/YYYY
    consolidate: bool = False

@app.get("/owners", dependencies=[Depends(verify_secret)])
async def list_owners():
    """List all owners with their AppFolio IDs."""
    cookies = get_cookies()
    owners = await get_owners_list(cookies)
    return {"count": len(owners), "owners": owners}

@app.post("/reports/owner-statement", dependencies=[Depends(verify_secret)])
async def owner_statement(req: OwnerStatementRequest):
    """Run an owner statement for a specific owner and date range."""
    cookies = get_cookies()
    result = await run_owner_statement(
        cookies=cookies,
        owner_id=req.owner_id,
        date_from=req.date_from,
        date_to=req.date_to,
        owner_name=req.owner_name,
        consolidate=req.consolidate,
    )
    return result

@app.get("/reports/owner-statement/{owner_id}", dependencies=[Depends(verify_secret)])
async def owner_statement_get(
    owner_id: str,
    date_from: str = None,
    date_to: str = None,
    owner_name: str = "",
):
    """GET version — run owner statement with query params."""
    cookies = get_cookies()
    today = datetime.today()
    if not date_to:
        date_to = today.strftime("%m/%d/%Y")
    if not date_from:
        # Default to first of current month
        date_from = today.replace(day=1).strftime("%m/%d/%Y")
    result = await run_owner_statement(
        cookies=cookies,
        owner_id=owner_id,
        date_from=date_from,
        date_to=date_to,
        owner_name=owner_name,
    )
    return result

@app.get("/reports/rent-roll", dependencies=[Depends(verify_secret)])
async def rent_roll(as_of: str = None):
    """Run a rent roll report."""
    cookies = get_cookies()
    result = await run_rent_roll(cookies=cookies, date_as_of=as_of)
    return result

@app.get("/reports/all-owner-statements", dependencies=[Depends(verify_secret)])
async def all_owner_statements(date_from: str = None, date_to: str = None):
    """
    Run owner statements for ALL owners and return formatted summary.
    Used for the Chief of Staff monthly sync.
    """
    cookies = get_cookies()
    today = datetime.today()
    if not date_to:
        date_to = today.strftime("%m/%d/%Y")
    if not date_from:
        date_from = today.replace(day=1).strftime("%m/%d/%Y")

    owners = await get_owners_list(cookies)
    results = []
    errors = []

    for owner in owners:
        if not owner.get("owner_id"):
            continue
        try:
            stmt = await run_owner_statement(
                cookies=cookies,
                owner_id=owner["owner_id"],
                date_from=date_from,
                date_to=date_to,
                owner_name=owner["name"],
            )
            results.append({
                "owner": owner["name"],
                "owner_id": owner["owner_id"],
                "summary": stmt.get("summary", {}),
                "formatted": stmt.get("formatted", ""),
            })
        except Exception as e:
            errors.append({"owner": owner["name"], "error": str(e)})

    # Build CoS context payload
    lines = [f"# All Owner Statements — {date_from} to {date_to}\n"]
    for r in results:
        lines.append(r.get("formatted", ""))
        lines.append("")

    if errors:
        lines.append("## Errors")
        for e in errors:
            lines.append(f"- {e['owner']}: {e['error']}")

    return {
        "ok": True,
        "period": {"from": date_from, "to": date_to},
        "owner_count": len(results),
        "errors": errors,
        "statements": results,
        "cos_payload": {
            "title": f"Owner Statements — {date_from} to {date_to}",
            "category": "strategy",
            "content": "\n".join(lines),
        }
    }

@app.get("/analyze/owner/{owner_id}", dependencies=[Depends(verify_secret)])
async def analyze_owner_statement(
    owner_id: str,
    owner_name: str = "",
    date_from: str = None,
    date_to: str = None,
):
    """
    Run owner statement + ask Claude to analyze it and surface issues.
    Returns a plain-English summary ready for owner communication.
    """
    import os, httpx
    cookies = get_cookies()
    today = datetime.today()
    if not date_to:
        date_to = today.strftime("%m/%d/%Y")
    if not date_from:
        date_from = today.replace(day=1).strftime("%m/%d/%Y")

    stmt = await run_owner_statement(
        cookies=cookies,
        owner_id=owner_id,
        date_from=date_from,
        date_to=date_to,
        owner_name=owner_name,
    )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return {**stmt, "analysis": "ANTHROPIC_API_KEY not configured"}

    prompt = f"""You are analyzing an AppFolio owner statement for a property management company called Evergreen Management.

Here is the statement data:
{stmt.get('formatted', '')}

Line items:
{json.dumps(stmt.get('line_items', [])[:30], indent=2)}

Please provide:
1. A 2-3 sentence plain-English summary of this owner's financial position for the period
2. Any issues or anomalies worth flagging (unexpected expenses, low income, negative balance, etc.)
3. A suggested response if the owner asks "how did my property do this month?"

Be specific with dollar amounts when available. Keep it concise and professional."""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        analysis_data = resp.json()
        analysis = analysis_data.get("content", [{}])[0].get("text", "Analysis failed")

    return {
        "owner": owner_name or owner_id,
        "period": {"from": date_from, "to": date_to},
        "summary": stmt.get("summary", {}),
        "analysis": analysis,
        "formatted_statement": stmt.get("formatted", ""),
    }
