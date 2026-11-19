"""Write "Funding Radar - pages scraped.xlsx": every source in sources.yaml with its last-run
status, plus every opportunity page from the latest run.

Run:  python tools/make_source_list.py
"""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Funding Radar - pages scraped.xlsx"

config = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
data = json.loads((ROOT / "docs" / "data" / "opportunities.json").read_text(encoding="utf-8"))
status = {s["id"]: s for s in data["sources"]}
active = Counter(i["source_id"] for i in data["items"] if i["active"])

HOW = {
    "html": "Listing page: each opportunity link on the page is collected",
    "table": "Database table: one row per opportunity (title, sponsor, deadline)",
    "rss": "RSS feed",
    "gcnews": "Government of Canada news feed, filtered to calls for proposals and new programs",
    "page": "Single program page: tracked as one program, flagged 'Updated' when the page changes",
    "wpjson": "Website's public program feed (WordPress API)",
    "sshrc": "SSHRC's published opportunity data file",
}
GROUPS = [
    ("Federal research", ["nserc", "cihr", "sshrc", "nfrf", "cfi", "genome-canada", "mitacs"]),
    ("Federal innovation & industry", ["nrc-irap", "isc", "ideas", "gc-news", "acoa-aif", "tcs-ipp", "global-clusters", "ngen"]),
    ("Atlantic Canada", ["acoa", "research-ns", "invest-ns", "innovation-pei", "ocean-supercluster", "nbif", "research-nb",
                         "nl-jgrd", "mun-rfd"]),
    ("Health research charities", ["cancer-society", "ms-canada"]),
    ("Intellectual property", ["ised-ip", "cipo-financing", "cipo-ktep", "springboard-ip", "nrc-ip-licensing", "dnd-ip"]),
    ("Emerging sectors", ["nrc-challenge", "nrc-ideation"]),
    ("Colleges & talent", ["cican", "swpp", "irap-youth"]),
]
# Sites that refuse automated reading; their last entries are kept on the radar.
NOTES = {
    "cfi": "Blocks GitHub's servers; refreshed when run from a Springboard computer",
    "research-nb": "Refuses some networks; check the site directly if the status is not OK",
}
group_of = {sid: g for g, ids in GROUPS for sid in ids}
order = {sid: n for n, (_, ids) in enumerate(GROUPS) for sid in ids}

font = Font(name="Arial", size=10)
bold = Font(name="Arial", size=10, bold=True)
head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
head_fill = PatternFill("solid", fgColor="103154")
link_font = Font(name="Arial", size=10, color="0563C1", underline="single")
wrap = Alignment(wrap_text=True, vertical="top")


def header(ws, row, names, widths):
    for c, (h, w) in enumerate(zip(names, widths), 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[row].height = 30


wb = Workbook()

# ---------------------------------------------------------------- Sources
ws = wb.active
ws.title = "Sources"
ws["A1"] = "Funding Radar – pages scraped every Monday"
ws["A1"].font = Font(name="Arial", size=14, bold=True, color="103154")
generated = datetime.fromisoformat(data["meta"]["generated"]).strftime("%B %d, %Y")
ws["A2"] = (f"Source list from sources.yaml; item counts and status from the last run ({generated}). "
            "Scope: Atlantic Canada and national programs; items naming other provinces are excluded. "
            "CIHR's ResearchNet does not allow automated reading, so CIHR calls come from CIHR's funding news and Memorial's database.")
ws["A2"].font = Font(name="Arial", size=10, italic=True, color="4C5A68")

hr = 4
header(ws, hr, ["#", "Group", "Source", "Funder", "Region", "Page scraped", "Link shown to users", "How it is read",
                "Detail pages opened", "Default topics", "Active items (last run)", "Last run status"],
       [5, 24, 34, 40, 18, 60, 60, 52, 14, 34, 14, 40])
sources = sorted(config["sources"], key=lambda s: (order.get(s["id"], 99), s["name"]))
r = hr
for n, src in enumerate(sources, 1):
    r += 1
    st = status.get(src["id"], {})
    ok = bool(st.get("ok"))
    if ok:
        state = "OK"
    elif src["url"].startswith("https://nrc.canada.ca"):
        state = "NRC's site now has a security check that blocks automated reading; last entries kept – check site directly"
    elif st:
        state = f"Problem: {st.get('error')}"
    else:
        state = "Not run yet"
    if src["id"] in NOTES:
        state += f" ({NOTES[src['id']]})"
    row = [n, group_of.get(src["id"], "Other"), src["name"], src.get("funder", src["name"]), src.get("region", "National"),
           src["url"], src.get("home") or src["url"], HOW.get(src.get("type", "html"), src.get("type")),
           "Yes" if src.get("detail") else "No", ", ".join(src.get("categories", [])), active.get(src["id"], 0), state]
    for c, v in enumerate(row, 1):
        cell = ws.cell(row=r, column=c, value=v)
        cell.font, cell.alignment = font, wrap
    for c in (6, 7):
        cell = ws.cell(row=r, column=c)
        cell.hyperlink, cell.font = cell.value, link_font
    if not ok:
        ws.cell(row=r, column=12).font = Font(name="Arial", size=10, color="C4242A")
last = r
for k, (label, formula) in enumerate([("Total active items", f"=SUM(K{hr + 1}:K{last})"),
                                      ("Sources", f"=COUNTA(C{hr + 1}:C{last})"),
                                      ("Sources OK last run", f'=COUNTIF(L{hr + 1}:L{last},"OK*")')], 1):
    ws.cell(row=last + k, column=10, value=label).font = bold
    ws.cell(row=last + k, column=11, value=formula).font = bold
ws.freeze_panes = ws.cell(row=hr + 1, column=4)
ws.auto_filter.ref = f"A{hr}:L{last}"

# ---------------------------------------------------------------- Opportunity pages
ws2 = wb.create_sheet("Opportunity pages")
header(ws2, 1, ["Opportunity", "Funder", "Source", "Region", "Type", "Status", "Deadline", "Topics", "Page", "First seen"],
       [60, 40, 32, 18, 14, 12, 13, 40, 70, 13])
items = sorted((i for i in data["items"] if i["active"]),
               key=lambda i: (i["deadline"] or "9999", i["source_name"], i["title"]))
for r2, it in enumerate(items, 2):
    vals = [it["title"], it["funder"], it["source_name"], it["region"], it["kind"],
            (it["status"] or "").capitalize() if it["status"] != "unknown" else "",
            datetime.fromisoformat(it["deadline"]) if it["deadline"] else None,
            ", ".join(it["categories"]), it["url"], datetime.fromisoformat(it["first_seen"])]
    for c, v in enumerate(vals, 1):
        cell = ws2.cell(row=r2, column=c, value=v)
        cell.font, cell.alignment = font, Alignment(vertical="top")
    for c in (7, 10):
        ws2.cell(row=r2, column=c).number_format = "yyyy-mm-dd"
    link = ws2.cell(row=r2, column=9)
    link.hyperlink, link.font = it["url"], link_font
ws2.freeze_panes = "B2"
ws2.auto_filter.ref = f"A1:J{len(items) + 1}"

wb.save(OUT)
print(f"{OUT.name}: {len(sources)} sources, {len(items)} opportunity pages")
