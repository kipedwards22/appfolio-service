import re
import json
import urllib.parse
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, date
from typing import Optional

APPFOLIO_DOMAIN = "evergreenmanagement.appfolio.com"
APPFOLIO_URL = f"https://{APPFOLIO_DOMAIN}"


def base_headers(cookies: str) -> dict:
    return {
        "Host": APPFOLIO_DOMAIN,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": APPFOLIO_URL,
        "Referer": f"{APPFOLIO_URL}/reports/owner_statement.html?customize=true",
        "Cookie": cookies,
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }


async def get_authenticity_token(cookies: str, report_path: str) -> str:
    """Fetch the CSRF authenticity token from the report customize page."""
    url = f"{APPFOLIO_URL}{report_path}?customize=true"
    headers = base_headers(cookies)
    headers["Accept"] = "text/html,application/xhtml+xml,*/*"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status == 403:
                raise Exception("AppFolio session expired — refresh APPFOLIO_COOKIES")
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", {"name": "authenticity_token"})
    if not token_input:
        # Try meta tag
        meta = soup.find("meta", {"name": "csrf-token"})
        if meta:
            return meta.get("content", "")
        raise Exception("Could not find authenticity_token in report page")
    return token_input.get("value", "")


async def post_report(cookies: str, report_path: str, form_data: dict) -> str:
    """POST to an AppFolio report endpoint and return the HTML."""
    token = await get_authenticity_token(cookies, report_path)
    form_data["authenticity_token"] = token
    form_data["commit"] = "Run Report"

    url = f"{APPFOLIO_URL}{report_path}"
    headers = base_headers(cookies)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, data=form_data, allow_redirects=True
        ) as resp:
            if resp.status == 403:
                raise Exception("AppFolio session expired — refresh APPFOLIO_COOKIES")
            return await resp.text()


def parse_owner_statement(html: str) -> dict:
    """Parse owner statement HTML into structured data."""
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "owner": "",
        "period": "",
        "properties": [],
        "summary": {
            "total_income": "",
            "total_expenses": "",
            "net_income": "",
            "owner_draw": "",
            "ending_balance": "",
        },
        "line_items": [],
    }

    # Owner name
    owner_el = soup.find(class_=re.compile(r"owner.name|report.owner", re.I))
    if owner_el:
        result["owner"] = owner_el.get_text(strip=True)

    # Period
    period_el = soup.find(string=re.compile(r"\d{2}/\d{2}/\d{4}.*\d{2}/\d{2}/\d{4}"))
    if period_el:
        result["period"] = period_el.strip()

    # Parse all tables
    tables = soup.find_all("table")
    for table in tables:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            if headers and len(cells) == len(headers):
                result["line_items"].append(dict(zip(headers, cells)))
            elif cells:
                result["line_items"].append({"row": cells})

    # Try to find summary figures
    for label, keys in [
        ("Total Income", ["total_income"]),
        ("Total Expense", ["total_expenses"]),
        ("Net Income", ["net_income"]),
        ("Owner Draw", ["owner_draw"]),
        ("Ending Balance", ["ending_balance"]),
        ("Beginning Balance", ["beginning_balance"]),
    ]:
        el = soup.find(string=re.compile(label, re.I))
        if el:
            parent = el.find_parent()
            if parent:
                # Find next sibling or adjacent td with amount
                amount_el = parent.find_next(string=re.compile(r"[\$\-\(][\d,\.]+"))
                if amount_el:
                    for key in keys:
                        result["summary"][key] = amount_el.strip()

    return result


def format_owner_statement_for_cos(data: dict, owner_name: str = "") -> str:
    """Format parsed owner statement as readable text for Chief of Staff context."""
    lines = []
    name = data.get("owner") or owner_name
    period = data.get("period", "")

    lines.append(f"## Owner Statement: {name}")
    if period:
        lines.append(f"Period: {period}")
    lines.append("")

    summary = data.get("summary", {})
    if any(summary.values()):
        lines.append("### Summary")
        for label, key in [
            ("Total Income", "total_income"),
            ("Total Expenses", "total_expenses"),
            ("Net Income", "net_income"),
            ("Owner Draw", "owner_draw"),
            ("Ending Balance", "ending_balance"),
        ]:
            val = summary.get(key)
            if val:
                lines.append(f"- {label}: {val}")
        lines.append("")

    items = data.get("line_items", [])
    if items:
        lines.append("### Line Items")
        for item in items[:50]:  # cap for context size
            if isinstance(item, dict):
                if "row" in item:
                    lines.append("- " + " | ".join(str(v) for v in item["row"] if v))
                else:
                    parts = [f"{k}: {v}" for k, v in item.items() if v]
                    if parts:
                        lines.append("- " + " | ".join(parts))

    return "\n".join(lines)


async def parse_rent_roll(html: str) -> dict:
    """Parse rent roll HTML into structured data."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    tables = soup.find_all("table")
    for table in tables:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not headers:
            continue
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if cells and len(cells) >= 3:
                if headers and len(cells) == len(headers):
                    rows.append(dict(zip(headers, cells)))
                else:
                    rows.append({"row": cells})

    return {"count": len(rows), "rows": rows}


async def run_owner_statement(
    cookies: str,
    owner_id: str,
    date_from: str,
    date_to: str,
    owner_name: str = "",
    consolidate: bool = False,
) -> dict:
    """
    Run an owner statement report.
    owner_id: AppFolio format like 'o_564'
    date_from/date_to: MM/DD/YYYY
    """
    form_data = {
        "filters[party_ids][]": owner_id,
        "filters[property_selection_ids][]": "all",
        "filters[posted_on_from]": date_from,
        "filters[posted_on_to]": date_to,
        "filters[consolidate]": "1" if consolidate else "0",
    }
    if owner_name:
        form_data["owner_search_term"] = owner_name

    html = await post_report(cookies, "/reports/owner_statement.html", form_data)
    parsed = parse_owner_statement(html)
    parsed["owner"] = parsed["owner"] or owner_name
    parsed["owner_id"] = owner_id
    parsed["date_from"] = date_from
    parsed["date_to"] = date_to
    parsed["formatted"] = format_owner_statement_for_cos(parsed, owner_name)
    parsed["raw_html_length"] = len(html)
    return parsed


async def run_rent_roll(
    cookies: str,
    date_as_of: str = None,
    property_ids: list = None,
) -> dict:
    """Run a rent roll report."""
    if not date_as_of:
        date_as_of = datetime.today().strftime("%m/%d/%Y")

    form_data = {
        "filters[as_of_date]": date_as_of,
        "filters[property_selection_ids][]": "all",
        "filters[status][]": "current",
    }
    if property_ids:
        form_data["filters[property_selection_ids][]"] = property_ids

    html = await post_report(cookies, "/reports/rent_roll.html", form_data)
    parsed = await parse_rent_roll(html)
    parsed["as_of"] = date_as_of
    return parsed


async def get_owners_list(cookies: str) -> list:
    """
    Scrape the owner search/list to get owner IDs and names.
    These are needed to run per-owner statements.
    """
    headers = base_headers(cookies)
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    headers["X-Requested-With"] = "XMLHttpRequest"

    owners = []
    page = 1
    async with aiohttp.ClientSession() as session:
        while True:
            url = f"{APPFOLIO_URL}/ownerships"
            params = {"page": page, "sort[by]": "name", "sort[order]": "asc"}
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    break
                try:
                    data = json.loads(await resp.text())
                except Exception:
                    break

            rows = data.get("body_row_data", [])
            if not rows:
                break

            for row in rows:
                cells = row.get("data", [])
                if not cells:
                    continue
                first_cell_html = cells[0].get("value", "")
                soup = BeautifulSoup(first_cell_html, "html.parser")
                a = soup.find("a")
                name = a.get_text(strip=True) if a else ""
                owner_id = ""
                if a and a.get("href"):
                    parts = a["href"].strip("/").split("/")
                    for i, p in enumerate(parts):
                        if p == "ownerships" and i + 1 < len(parts):
                            owner_id = f"o_{parts[i+1]}"
                if name:
                    owners.append({"name": name, "owner_id": owner_id})

            if len(rows) < 30:
                break
            page += 1

    return owners
