"""
Solicitor & Firm Monitor
========================
Checks SRA register and Companies House daily for changes.
Sends an Outlook email report if any changes are detected.

Sources monitored:
  - SRA register: firm authorisation status, conditions, decisions
  - Companies House: director appointments and resignations
"""

import json
import os
import time
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
STATE_FILE      = "state.json"
SOLICITORS_FILE = "solicitors.json"

CH_API_KEY    = os.environ.get("CH_API_KEY",          "")
SRA_API_KEY   = os.environ.get("SRA_API_KEY",         "47c35079aebc4ed0bad283972c16857a")
EMAIL_FROM    = os.environ.get("EMAIL_FROM",           "alex.landau@mssgroup.co.uk")
EMAIL_TO      = os.environ.get("EMAIL_TO",             "alex.landau@mssgroup.co.uk")
GRAPH_TENANT  = os.environ.get("GRAPH_TENANT_ID",      "a5108403-1e5f-4ceb-b695-833b5e65948d")
GRAPH_CLIENT  = os.environ.get("GRAPH_CLIENT_ID",      "282d5235-f37b-4091-927a-717cfec99e94")
GRAPH_SECRET  = os.environ.get("GRAPH_CLIENT_SECRET",  "")

# SRA Data Sharing API — correct endpoint
SRA_API_BASE     = "https://sra-prod-apim.azure-api.net/datashare/api/V1/organisation"
SRA_API_HEADERS  = {
    "Ocp-Apim-Subscription-Key": SRA_API_KEY,
    "Accept": "application/json",
    "User-Agent": "SolicitorMonitor/1.0",
}

CH_API_BASE  = "https://api.company-information.service.gov.uk"

REQUEST_DELAY    = 0.5
CH_REQUEST_DELAY = 0.3


# ── File helpers ───────────────────────────────────────────────────────────────

def load_json(path, default):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )


# ── SRA functions ──────────────────────────────────────────────────────────────

def sra_search_firm(firm_name):
    """Search SRA Data Sharing API by firm name."""
    try:
        resp = requests.get(
            f"{SRA_API_BASE}/Search",
            params={"name": firm_name},
            headers=SRA_API_HEADERS,
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            orgs = data.get("Organisations", data if isinstance(data, list) else [])
            if orgs:
                # Exact name match first
                for o in orgs:
                    if o.get("PracticeName","").lower() == firm_name.lower():
                        return o
                return orgs[0]
        elif resp.status_code == 401:
            print(f"  SRA API: authentication failed — check SRA_API_KEY")
        elif resp.status_code == 404:
            return None
        else:
            print(f"  SRA API status: {resp.status_code}")
    except Exception as e:
        print(f"  SRA search error: {e}")
    return None


def sra_get_firm_detail(sra_number):
    """Get full firm detail from SRA Data Sharing API by SRA number."""
    try:
        resp = requests.get(
            f"{SRA_API_BASE}/{sra_number}",
            headers=SRA_API_HEADERS,
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 403:
            print(f"  SRA API blocked (403) — using stored status")
            return None
        elif resp.status_code == 404:
            return None
        else:
            print(f"  SRA detail API status: {resp.status_code}")
    except Exception as e:
        print(f"  SRA detail error: {e}")
    return None




def extract_sra_snapshot(detail):
    status     = detail.get("AuthorisationStatus", detail.get("status", ""))
    authorised = detail.get("AuthorisationStatus", detail.get("authorised", ""))
    closed_kws = ["closed", "closing", "revoked", "expired", "ceased", "lapsed"]
    is_closed     = any(kw in str(status).lower() for kw in closed_kws)
    is_intervened = "intervene" in str(status).lower()
    status_date   = (detail.get("AuthorisationStatusDate","") or "")[:10]
    return {
        "status":          status,
        "authorised":      authorised,
        "licence_type":    detail.get("AuthorisationType", detail.get("licenceType", "")),
        "conditions":      detail.get("conditions", []),
        "decisions":       detail.get("decisions", []),
        "practising":      detail.get("practising", ""),
        "regulated_since": detail.get("AuthorisationDate", detail.get("regulatedSince", "")),
        "status_date":     status_date,
        "practice_name":   detail.get("PracticeName", ""),
        "company_reg_no":  detail.get("CompanyRegNo", ""),
        "sra_number":      str(detail.get("SraNumber", "")),
        "is_closed":       is_closed,
        "is_intervened":   is_intervened,
    }


# ── Companies House functions ──────────────────────────────────────────────────

def ch_search_company(firm_name):
    try:
        resp = requests.get(
            f"{CH_API_BASE}/search/companies",
            params={"q": firm_name, "items_per_page": 10},
            auth=(CH_API_KEY, ""),
            timeout=15
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None

        # Clean firm name for comparison
        def clean(s):
            import re
            return re.sub(r'\b(ltd|llp|limited|llc|plc|solicitors|law|legal)\b', '',
                          s.lower()).strip()

        firm_clean = clean(firm_name)

        # 1. Exact match
        for item in items:
            if item.get("title", "").lower() == firm_name.lower():
                return item
        # 2. Match ignoring legal suffixes
        for item in items:
            if clean(item.get("title", "")) == firm_clean:
                return item
        # 3. First active company (not dissolved)
        for item in items:
            if item.get("company_status", "") not in ["dissolved", "converted-closed"]:
                return item
        # 4. First result
        return items[0]
    except Exception as e:
        print(f"  CH search error for '{firm_name}': {e}")
        return None


def ch_get_officers(company_number):
    try:
        resp = requests.get(
            f"{CH_API_BASE}/company/{company_number}/officers",
            params={"items_per_page": 100},
            auth=(CH_API_KEY, ""),
            timeout=15
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("items", [])
    except Exception as e:
        print(f"  CH officers error for {company_number}: {e}")
        return []


def ch_get_company_profile(company_number):
    try:
        resp = requests.get(
            f"{CH_API_BASE}/company/{company_number}",
            auth=(CH_API_KEY, ""),
            timeout=15
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        print(f"  CH profile error for {company_number}: {e}")
        return None


def extract_ch_snapshot(officers, profile):
    active, resigned = [], []
    for o in officers:
        entry = {
            "name":      o.get("name", ""),
            "role":      o.get("officer_role", ""),
            "appointed": o.get("appointed_on", ""),
        }
        if o.get("resigned_on"):
            entry["resigned"] = o["resigned_on"]
            resigned.append(entry)
        else:
            active.append(entry)
    return {
        "active_officers":   sorted(active,   key=lambda x: x["name"]),
        "resigned_officers": sorted(resigned, key=lambda x: x["name"]),
        "company_status":    profile.get("company_status", "") if profile else "",
        "company_type":      profile.get("type", "") if profile else "",
    }




# ── Additional research sources ────────────────────────────────────────────────

def bing_search(query, n=5):
    """Search Bing and return list of results. DuckDuckGo is blocked on GitHub Actions."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,*/*",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query},
            headers=headers,
            timeout=8
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for item in soup.select(".b_algo")[:n]:
            title = item.select_one("h2")
            link  = item.select_one("a")
            snip  = item.select_one(".b_caption p, p")
            if title:
                results.append({
                    "title":   title.get_text(strip=True),
                    "url":     link["href"] if link else "",
                    "snippet": snip.get_text(strip=True)[:300] if snip else "",
                })
        return results
    except Exception:
        return []


def search_gazette_insolvency(firm_name, company_number=""):
    """Check London Gazette for insolvency notices."""
    notices = []
    queries = [
        f'"{firm_name}" site:thegazette.co.uk',
        f'"{firm_name}" winding up liquidation administration insolvency gazette',
    ]
    if company_number:
        queries.append(f'{company_number} gazette insolvency winding')

    for q in queries:
        results = bing_search(q, 4)
        for r in results:
            combined = (r["title"] + r["snippet"]).lower()
            if any(x in combined for x in
                   ["winding", "liquidat", "administrat", "insolvenc",
                    "gazette", "receiver", "bankrupt"]):
                notice_type = ("Winding Up" if "winding" in combined
                               else "Liquidation" if "liquidat" in combined
                               else "Administration" if "administrat" in combined
                               else "Insolvency Notice")
                notices.append({
                    "type":    notice_type,
                    "title":   r["title"],
                    "snippet": r["snippet"][:200],
                    "url":     r["url"],
                })
        time.sleep(0.5)

    seen = set()
    return [n for n in notices if n["title"] not in seen and not seen.add(n["title"])][:3]


def search_law_gazette(firm_name):
    """Search Law Gazette for articles about the firm."""
    articles = []
    results  = bing_search(f'"{firm_name}" site:lawgazette.co.uk', 4)
    results += bing_search(f'"{firm_name}" solicitors "law gazette" disciplinary', 3)
    for r in results:
        if "lawgazette" in r["url"].lower() or "law gazette" in r["snippet"].lower():
            articles.append({"title": r["title"], "snippet": r["snippet"][:200]})
    seen = set()
    return [a for a in articles if a["title"] not in seen and not seen.add(a["title"])][:3]


def search_legal_futures(firm_name):
    """Search Legal Futures for news about the firm."""
    results  = bing_search(f'"{firm_name}" site:legalfutures.co.uk', 3)
    results += bing_search(f'"{firm_name}" legalfutures', 2)
    articles = []
    for r in results:
        if "legalfutures" in r["url"].lower() or "legal futures" in r["snippet"].lower():
            articles.append({"title": r["title"], "snippet": r["snippet"][:200]})
    seen = set()
    return [a for a in articles if a["title"] not in seen and not seen.add(a["title"])][:3]


def search_linkedin_firm(firm_name):
    """Get basic LinkedIn info for the firm."""
    results = bing_search(f'"{firm_name}" site:linkedin.com/company solicitors', 3)
    for r in results:
        if "linkedin.com" in r["url"].lower():
            snip = r["snippet"]
            employees = ""
            m = re.search(r'(\d+[-–]\d+|\d+\+?)\s+employees?', snip, re.I)
            if m:
                employees = m.group(0)
            return {
                "found":       True,
                "url":         r["url"],
                "description": snip[:300],
                "employees":   employees,
            }
    return {"found": False}


def search_general_news(firm_name):
    """Search for general news mentions."""
    articles = []
    results  = bing_search(
        f'"{firm_name}" solicitors complaint news disciplinary 2024 2025 2026', 5)
    for r in results:
        combined = (r["title"] + r["snippet"]).lower()
        # Filter for relevant results
        if any(x in combined for x in
               [firm_name.lower().split()[0], "solicitor", "law firm",
                "complaint", "disciplin", "sra", "intervention"]):
            articles.append({"title": r["title"], "snippet": r["snippet"][:200]})
    seen = set()
    return [a for a in articles if a["title"] not in seen and not seen.add(a["title"])][:4]



def detect_sra_changes(old, new):
    changes = []
    if old.get("status") != new.get("status"):
        changes.append(f"Status: '{old.get('status','?')}' → '{new.get('status','?')}'")
    if old.get("authorised") != new.get("authorised"):
        changes.append(f"Authorisation: '{old.get('authorised','')}' → '{new.get('authorised','')}'")
    old_conds = set(str(c) for c in old.get("conditions", []))
    new_conds  = set(str(c) for c in new.get("conditions", []))
    for c in new_conds - old_conds:
        changes.append(f"New condition added: {c}")
    for c in old_conds - new_conds:
        changes.append(f"Condition removed: {c}")
    old_decs = set(str(d) for d in old.get("decisions", []))
    new_decs  = set(str(d) for d in new.get("decisions", []))
    for d in new_decs - old_decs:
        changes.append(f"New decision recorded: {d}")
    return changes


def detect_ch_changes(old, new):
    changes = []
    old_active  = {o["name"]: o for o in old.get("active_officers", [])}
    new_active  = {o["name"]: o for o in new.get("active_officers", [])}
    old_resigned = {o["name"] for o in old.get("resigned_officers", [])}
    new_resigned = {o["name"]: o for o in new.get("resigned_officers", [])}

    for name, o in new_active.items():
        if name not in old_active:
            changes.append(
                f"New appointment: {name} ({o.get('role','')}) on {o.get('appointed','?')}"
            )
    for name in old_active:
        if name not in new_active and name not in old_resigned:
            res = new_resigned.get(name, {})
            changes.append(
                f"Resignation: {name} (resigned {res.get('resigned','?')})"
            )
    if old.get("company_status") != new.get("company_status"):
        changes.append(
            f"Company status: '{old.get('company_status','')}' → '{new.get('company_status','')}'"
        )
    return changes


# ── Email ──────────────────────────────────────────────────────────────────────

def build_email_html(all_changes, closed_firms, run_date, stats, research_results=None):
    has_changes = stats["changed"] > 0
    has_closed  = len(closed_firms) > 0

    # ── Closed firms section ──────────────────────────────────────
    closed_rows = ""
    for f in sorted(closed_firms, key=lambda x: x["name"]):
        # Format status date nicely
        sd = f.get("status_date","")
        try:
            from datetime import date, datetime
            if sd:
                sd_fmt = datetime.fromisoformat(sd).strftime("%-d %b %Y")
                # Calculate how long ago
                days = (date.today() - date.fromisoformat(sd)).days
                if days < 30:
                    ago = f"{days}d ago"
                elif days < 365:
                    ago = f"{days//30}mo ago"
                else:
                    ago = f"{days//365}yr {(days%365)//30}mo ago"
                sd_display = f"{sd_fmt} ({ago})"
            else:
                sd_display = "Date unknown"
        except Exception:
            sd_display = sd or "Date unknown"

        closed_rows += f"""
        <tr style="background:#fdecea">
          <td style="padding:10px 12px;border-bottom:1px solid #f5c6c6;
                     font-weight:bold;width:35%">{f['name']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f5c6c6;
                     color:#c0392b;font-weight:bold">{f['status']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f5c6c6;
                     color:#666;font-size:12px">{sd_display}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f5c6c6;
                     color:#666;font-size:12px">SRA: {f['sra']}</td>
        </tr>"""

    closed_section = ""
    if has_closed:
        closed_section = f"""
        <div style="margin-bottom:24px">
          <h3 style="color:#c0392b;margin:0 0 8px 0">
            ⚠️ Closed or Closing Firms ({len(closed_firms)})
          </h3>
          <p style="color:#666;font-size:12px;margin:0 0 10px 0">
            The following firms on your watchlist are shown as closed or closing
            on the SRA register. Please review any active cases with these firms.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border-collapse:collapse;border:1px solid #f5c6c6">
            <thead>
              <tr style="background:#c0392b;color:#fff">
                <th style="padding:8px 12px;text-align:left">Firm</th>
                <th style="padding:8px 12px;text-align:left">SRA Status</th>
                <th style="padding:8px 12px;text-align:left">Date</th>
                <th style="padding:8px 12px;text-align:left">SRA Number</th>
              </tr>
            </thead>
            <tbody>{closed_rows}</tbody>
          </table>
        </div>"""

    # ── Changes section ───────────────────────────────────────────
    rows = ""
    for firm, data in sorted(all_changes.items()):
        sra_items = data.get("sra_changes", [])
        ch_items  = data.get("ch_changes",  [])
        if not sra_items and not ch_items:
            continue
        items_html = ""
        for c in sra_items:
            items_html += f'<li style="color:#c0392b"><b>SRA:</b> {c}</li>'
        for c in ch_items:
            items_html += f'<li style="color:#2471a3"><b>Companies House:</b> {c}</li>'
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;
                     font-weight:bold;vertical-align:top;width:35%">{firm}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee">
            <ul style="margin:0;padding-left:18px">{items_html}</ul>
          </td>
        </tr>"""

    if not has_changes:
        changes_section = '<p style="font-size:16px;color:#27ae60">&#10003; No status changes detected today.</p>'
    else:
        changes_section = f"""
        <h3 style="margin:0 0 8px 0">Status Changes ({stats['changed']})</h3>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #ddd">
          <thead>
            <tr style="background:#2c3e50;color:#fff">
              <th style="padding:10px 12px;text-align:left">Firm</th>
              <th style="padding:10px 12px;text-align:left">Changes Detected</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;
        color:#333;max-width:900px;margin:0 auto">
      <div style="background:#2c3e50;color:#fff;padding:20px 24px">
        <h2 style="margin:0">Solicitor Monitor &mdash; Daily Report</h2>
        <p style="margin:4px 0 0;opacity:.8">{run_date}</p>
      </div>
      <div style="background:#f8f9fa;padding:12px 24px;border:1px solid #ddd;border-top:none">
        <b>Firms monitored:</b> {stats['total']} &nbsp;|&nbsp;
        <b>SRA checks:</b> {stats['sra_checked']} &nbsp;|&nbsp;
        <b>CH checks:</b> {stats['ch_checked']} &nbsp;|&nbsp;
        <b>Changes:</b>
        <span style="color:{'#c0392b' if has_changes else '#27ae60'};font-weight:bold">
          {stats['changed']}
        </span> &nbsp;|&nbsp;
        <b>Closed/Closing:</b>
        <span style="color:{'#c0392b' if has_closed else '#27ae60'};font-weight:bold">
          {stats['closed']}
        </span>
        {f'&nbsp;|&nbsp;<b>Gazette hits:</b> <span style="color:#c0392b;font-weight:bold">{stats.get("gazette_hits",0)}</span>' if stats.get("gazette_hits") else ''}
      </div>
      <div style="padding:20px 24px;border:1px solid #ddd;
                  border-top:none;border-radius:0 0 6px 6px">
        {closed_section}
        {changes_section}
        {_build_research_section(research_results)}
      </div>
      <p style="font-size:11px;color:#999;margin-top:12px">
        Solicitor Monitor &bull; SRA + Companies House + Gazette + Law Gazette &bull; {run_date}
      </p>
    </body></html>"""


def _build_research_section(research_results):
    """Build HTML section for weekly research findings."""
    if not research_results:
        return ""

    # Only show firms with notable findings
    notable = {k: v for k, v in research_results.items()
               if v.get("gazette") or v.get("law_gazette") or
               v.get("legal_futures") or v.get("news")}

    if not notable:
        return """
        <div style="margin-top:20px;padding:12px 16px;background:#f8f9fa;
                    border:1px solid #ddd;border-radius:4px">
          <h3 style="margin:0 0 4px 0;font-size:14px;color:#2c3e50">
            📰 Weekly Research — Gazette, Law Gazette, News
          </h3>
          <p style="margin:4px 0 0;color:#27ae60;font-size:13px">
            ✓ No notable findings in Gazette, Law Gazette or news sources this week.
          </p>
        </div>"""

    rows = ""
    for firm, data in sorted(notable.items()):
        items = ""
        for n in data.get("gazette", []):
            items += f'<li style="color:#c0392b"><b>⚠ Gazette ({n["type"]}):</b> {n["title"]}<br><small style="color:#666">{n["snippet"]}</small></li>'
        for a in data.get("law_gazette", []):
            items += f'<li style="color:#8e44ad"><b>Law Gazette:</b> {a["title"]}<br><small style="color:#666">{a["snippet"]}</small></li>'
        for a in data.get("legal_futures", []):
            items += f'<li style="color:#2471a3"><b>Legal Futures:</b> {a["title"]}<br><small style="color:#666">{a["snippet"]}</small></li>'
        for a in data.get("news", []):
            items += f'<li style="color:#555"><b>News:</b> {a["title"]}<br><small style="color:#666">{a["snippet"]}</small></li>'
        if items:
            rows += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #eee;
                         font-weight:bold;vertical-align:top;width:35%">{firm}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #eee">
                <ul style="margin:0;padding-left:18px;font-size:13px">{items}</ul>
              </td>
            </tr>"""

    return f"""
    <div style="margin-top:20px">
      <h3 style="margin:0 0 8px 0;font-size:14px;color:#2c3e50">
        📰 Weekly Research — Gazette, Law Gazette, Legal Futures, News
      </h3>
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;border:1px solid #ddd">
        <thead>
          <tr style="background:#6c3483;color:#fff">
            <th style="padding:10px 12px;text-align:left">Firm</th>
            <th style="padding:10px 12px;text-align:left">Findings</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def get_graph_token():
    import requests as _req
    resp = _req.post(
        f"https://login.microsoftonline.com/{GRAPH_TENANT}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": GRAPH_CLIENT,
              "client_secret": GRAPH_SECRET,
              "scope": "https://graph.microsoft.com/.default"},
        timeout=15
    )
    if resp.status_code != 200:
        raise Exception(f"Token failed: {resp.status_code} — {resp.text}")
    return resp.json()["access_token"]


def send_email(subject, html_body):
    import requests as _req
    token = get_graph_token()
    payload = {
        "message": {
            "subject": subject,
            "body":    {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": EMAIL_TO}}],
        },
        "saveToSentItems": "false"
    }
    resp = _req.post(
        f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM}/sendMail",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    if resp.status_code == 202:
        print("  Email sent via Microsoft Graph.")
    else:
        raise Exception(f"Send failed: {resp.status_code} — {resp.text}")
    print("  Email sent.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    run_date   = datetime.now().strftime("%A %d %B %Y at %H:%M")
    print(f"\n{'='*60}\nSolicitor Monitor — {run_date}\n{'='*60}\n")

    solicitors = load_json(SOLICITORS_FILE, [])
    state      = load_json(STATE_FILE, {})

    if not solicitors:
        print("ERROR: solicitors.json is empty or missing.")
        return

    all_changes = {}
    closed_firms    = []
    research_results = {}
    is_monday       = datetime.now().weekday() == 0
    stats = {
        "total": len(solicitors), "sra_checked": 0,
        "ch_checked": 0, "changed": 0, "errors": 0,
        "closed": 0, "research_firms": 0, "gazette_hits": 0
    }

    for i, firm in enumerate(solicitors):
        name = firm.get("name", "").strip()
        if not name:
            continue

        print(f"[{i+1}/{stats['total']}] {name}")
        firm_state   = state.get(name, {})
        firm_changes = {"sra_changes": [], "ch_changes": []}

        # ── SRA ──────────────────────────────────────────────────
        sra_number = firm.get("sra_number") or firm_state.get("sra_number")
        old_sra    = firm_state.get("sra_snapshot", {})

        if not sra_number:
            result = sra_search_firm(name)
            time.sleep(REQUEST_DELAY)
            if result:
                sra_number = str(result.get("SraNumber") or result.get("id") or
                                 result.get("sraNumber") or "")
                # Also grab CH number if returned by API
                ch_from_sra = str(result.get("CompanyRegNo","")).strip()
                if ch_from_sra and not firm.get("companies_house_number"):
                    firm["companies_house_number"] = ch_from_sra
                if sra_number:
                    firm["sra_number"] = sra_number
                    print(f"  SRA found: {sra_number}")
            else:
                print(f"  SRA: not on register")

        if sra_number:
            detail = sra_get_firm_detail(str(sra_number))
            time.sleep(REQUEST_DELAY)

            # If API blocked, build snapshot from stored data in solicitors.json
            if not detail:
                stored_status = firm.get("sra_status", "")
                stored_type   = firm.get("sra_authorisation_type", "")
                stored_date   = firm.get("sra_authorisation_date", "")
                if stored_status:
                    detail = {
                        "AuthorisationStatus": stored_status,
                        "AuthorisationType":   stored_type,
                        "AuthorisationDate":   stored_date,
                        "SraNumber":           sra_number,
                        "PracticeName":        name,
                        "CompanyRegNo":        firm.get("companies_house_number",""),
                    }
                    print(f"  SRA: using stored status ({stored_status})")

            if detail:
                new_sra = extract_sra_snapshot(detail)
                stats["sra_checked"] += 1
                if old_sra:
                    changes = detect_sra_changes(old_sra, new_sra)
                    firm_changes["sra_changes"] = changes
                    print(f"  SRA: {'⚠ '+str(len(changes))+' change(s)' if changes else '✓ no changes'}")
                else:
                    print(f"  SRA: baseline recorded ({new_sra.get('status','')})")
                firm_state["sra_number"]   = sra_number
                firm_state["sra_snapshot"] = new_sra
                # Flag closed/problematic/intervened firms
                firm_status   = new_sra.get("status","")
                status_date   = new_sra.get("status_date","")
                is_intervened = new_sra.get("is_intervened", False)
                is_bad        = new_sra.get("is_closed") or is_intervened

                if is_bad:
                    # Check if status date is over 1 year old — if so, remove from list
                    remove_firm = False
                    if status_date:
                        try:
                            from datetime import date
                            sd   = date.fromisoformat(status_date)
                            age  = (date.today() - sd).days
                            if age > 365:
                                remove_firm = True
                                print(f"  ℹ Removing from list — {firm_status} over 1 year ago ({status_date})")
                                firm["_remove"] = True
                        except Exception:
                            pass

                    if not remove_firm:
                        status_label = firm_status
                        if is_intervened:
                            status_label = "INTERVENED"
                        closed_firms.append({
                            "name":        name,
                            "status":      status_label,
                            "sra":         sra_number,
                            "status_date": status_date,
                        })
                        stats["closed"] += 1
                        print(f"  ⚠ {status_label} — {status_date or 'date unknown'}")
            else:
                print(f"  SRA: no status available")
                stats["errors"] += 1

        # ── Companies House ───────────────────────────────────────
        ch_number = firm.get("companies_house_number") or firm_state.get("ch_number")
        old_ch    = firm_state.get("ch_snapshot", {})

        if not ch_number and CH_API_KEY:
            result = ch_search_company(name)
            time.sleep(CH_REQUEST_DELAY)
            if result:
                ch_number = result.get("company_number")
                print(f"  CH found: {ch_number}")
            else:
                print(f"  CH: not found")

        # Always write discovered numbers back to solicitors.json
        if ch_number:
            firm["companies_house_number"] = ch_number
        if sra_number:
            firm["sra_number"] = sra_number

        if ch_number and CH_API_KEY:
            officers = ch_get_officers(ch_number)
            profile  = ch_get_company_profile(ch_number)
            time.sleep(CH_REQUEST_DELAY)
            new_ch = extract_ch_snapshot(officers, profile)
            stats["ch_checked"] += 1
            if old_ch:
                changes = detect_ch_changes(old_ch, new_ch)
                firm_changes["ch_changes"] = changes
                print(f"  CH: {'⚠ '+str(len(changes))+' change(s)' if changes else '✓ no changes'}")
            else:
                print(f"  CH: baseline recorded")
            firm_state["ch_number"]   = ch_number
            firm_state["ch_snapshot"] = new_ch

        # ── Save state ────────────────────────────────────────────
        state[name] = firm_state
        if firm_changes["sra_changes"] or firm_changes["ch_changes"]:
            all_changes[name] = firm_changes
            stats["changed"] += 1
        else:
            all_changes[name] = firm_changes

    # ── Weekly research checks (Gazette, Law Gazette, LinkedIn, News) ──────────
    # Run on Mondays only to avoid excessive requests
    research_results = {}
    is_monday = datetime.now().weekday() == 0

    if is_monday:
        print(f"\n{'='*60}")
        print("Running weekly research checks (Gazette, Law Gazette, News)...")
        print(f"{'='*60}")

        # Only check firms that have active cases (have SRA or CH numbers)
        active_firms = [f for f in solicitors
                        if f.get("sra_number") or f.get("companies_house_number")]
        print(f"Checking {len(active_firms)} firms with known SRA/CH numbers...")

        for i, firm in enumerate(active_firms[:50]):  # Cap at 50 per run
            name    = firm.get("name", "").strip()
            ch_num  = firm.get("companies_house_number", "")
            print(f"  [{i+1}] {name}")

            gazette     = search_gazette_insolvency(name, ch_num)
            law_gaz     = search_law_gazette(name)
            legal_fut   = search_legal_futures(name)
            linkedin    = search_linkedin_firm(name)
            news        = search_general_news(name)

            has_findings = gazette or law_gaz or legal_fut or news

            if has_findings or linkedin.get("found"):
                research_results[name] = {
                    "gazette":      gazette,
                    "law_gazette":  law_gaz,
                    "legal_futures":legal_fut,
                    "linkedin":     linkedin,
                    "news":         news,
                }
                if gazette:
                    print(f"    ⚠ {len(gazette)} Gazette notice(s)!")
                if law_gaz:
                    print(f"    ⚠ {len(law_gaz)} Law Gazette article(s)")
                if news:
                    print(f"    ℹ {len(news)} news item(s)")

            time.sleep(2)  # Be polite between firms

        print(f"Weekly research complete — {len(research_results)} firms with findings")
        stats["research_firms"] = len(research_results)
        stats["gazette_hits"]   = sum(1 for r in research_results.values() if r.get("gazette"))

    # Remove firms that have been closed/intervened for over 1 year
    firms_to_remove = [f.get("name") for f in solicitors if f.get("_remove")]
    if firms_to_remove:
        solicitors = [f for f in solicitors if not f.get("_remove")]
        for name in firms_to_remove:
            state.pop(name, None)
            print(f"  Removed from watchlist: {name}")
        print(f"  {len(firms_to_remove)} firm(s) removed from watchlist")

    save_json(STATE_FILE, state)
    save_json(SOLICITORS_FILE, solicitors)

    print(f"\n{'='*60}")
    print(f"Complete — {stats['changed']} firm(s) with changes | {stats['closed']} closed/closing")
    print(f"SRA: {stats['sra_checked']} checked | CH: {stats['ch_checked']} checked | Errors: {stats['errors']}")
    print(f"{'='*60}\n")

    if not GRAPH_SECRET and not os.environ.get("GRAPH_CLIENT_SECRET",""):
        print("Email not configured — skipping.")
        return

    gazette_total = stats.get("gazette_hits", 0)
    subject = (
        f"⚠️ Solicitor Monitor — {stats['changed']} change(s), {stats['closed']} closed"
        + (f", {gazette_total} Gazette hit(s)" if gazette_total else "")
        + f" — {run_date}"
        if stats["changed"] > 0 or stats["closed"] > 0 or gazette_total > 0
        else f"✅ Solicitor Monitor — No changes — {run_date}"
    )
    try:
        html = build_email_html(all_changes, closed_firms, run_date, stats,
                                research_results if is_monday else {})
        send_email(subject, html)
    except Exception as e:
        print(f"Email error: {e}")


if __name__ == "__main__":
    main()
