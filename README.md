# Funding Radar

A weekly-updated list of Canadian funding, support and program opportunities
(research, commercialization, industry engagement, spin-offs, SMEs, internships/talent)
for tech-transfer offices, built to be embedded in a SharePoint site.

**How it works**

```
sources.yaml ──► scraper/scrape.py ──► docs/data/opportunities.json ──► docs/index.html
 (what to check)   (runs every Monday     (the data)                      (the page you embed
                    on GitHub Actions)                                      in SharePoint)
```

- The scraper reads only official funder websites listed in `sources.yaml`, respects each
  site's `robots.txt`, identifies itself, and waits between requests.
- Each item keeps a **first seen** date, so the page can flag what is **New** this week.
- Deadlines are detected from the funder's page where possible; topics (Commercialization,
  Startups & spin-offs, Talent & internships, …) and audiences are tagged automatically.
- If a site is down or changes its layout, its previous items are kept and the
  **Sources checked** panel at the bottom of the page shows which site needs attention.

## 1. Put it on GitHub (one-time, ~10 minutes)

1. Create a free GitHub account (or use your organization's) and a new repository,
   e.g. `funding-radar`. It must be **public** for free GitHub Pages hosting
   (the data is public funder information, so this is fine).
2. Upload the contents of this folder (everything except `.venv`). With Git installed:
   ```bash
   git init
   git add .
   git commit -m "Funding Radar"
   git branch -M main
   git remote add origin https://github.com/<your-account>/funding-radar.git
   git push -u origin main
   ```
3. In the repository: **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
   branch **main**, folder **/docs** → Save.
   After a minute the page is live at `https://<your-account>.github.io/funding-radar/`.
4. **Settings → Actions → General → Workflow permissions → Read and write permissions** → Save
   (lets the weekly job save the new data).
5. **Actions → Weekly funding scrape → Run workflow** to test it once. From then on it runs
   every Monday at 10:00 UTC automatically.

### Hosting on Vercel instead of GitHub Pages

GitHub keeps running the weekly scrape; Vercel only serves the page, and redeploys
automatically each time the weekly job commits new data.

1. Sign in at vercel.com with the GitHub account → **Add New… → Project** →
   import `springboard-atlantic/funding-radar`.
2. Leave the settings as detected (`vercel.json` already sets the output folder to `docs`,
   no build step) → **Deploy**. The page is live at `https://funding-radar-<…>.vercel.app`.
3. Optional: **Settings → Domains** to add a custom domain such as `radar.springboardatlantic.ca`.

`vercel.json` also allows the page to be framed only by SharePoint (`*.sharepoint.com`).

## 2. Embed it in SharePoint

1. A SharePoint admin allows the site once: **Site settings → HTML Field Security**
   → add `vercel.app` (or `github.io`, or your custom domain) to the allowed domains.
   (Site contents → ⚙ → Site information → View all site settings → HTML Field Security.)
2. On the hub page: **Edit → + → Embed** web part, paste:
   ```html
   <iframe src="https://<your-account>.github.io/funding-radar/" width="100%" height="1400" style="border:0"></iframe>
   ```
3. Publish. Opportunity links open in a new tab.

**Compact "Upcoming opportunities" embed** — for a small SharePoint section, add `?view=upcoming`
to the address. It shows only the next deadlines (each expandable to its details and the
funder's link) and a link to open the full Funding Radar:
```html
<iframe src="https://<your-site>/?view=upcoming" width="100%" height="720" style="border:0"></iframe>
```

**Scope** — Atlantic Canada and national programs only. `exclude_regions` at the top of
`sources.yaml` drops anything whose title names another province, territory or regional agency.

**Pre-filtered views** — add a hash to the URL to open the page with filters applied, e.g. an
Atlantic-only commercialization view:
`https://<your-account>.github.io/funding-radar/#region=Atlantic&cat=Commercialization`
(supported: `region`, `cat` (repeatable), `audience`, `q`).

## 3. Adding or changing sources

Edit `sources.yaml` (directly on GitHub is fine). The comments at the top explain each field.
The simplest addition is a single program page you want watched for changes:

```yaml
  - id: my-new-program
    name: Example Program
    funder: Example Agency
    region: Atlantic
    type: page
    url: https://example.gc.ca/program.html
    kind: Program
    categories: [Commercialization]
```

For a page that lists many opportunities, use `type: html` with an `include:` pattern that
matches the opportunity links (see the existing entries for examples). Test locally:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python scraper/scrape.py my-new-program
```

## Notes and limits

- **CIHR**: the full CIHR list lives on ResearchNet, which forbids automated access in its
  robots.txt, so the tool follows CIHR's "Funding news" page and links to ResearchNet in Sources.
- **Genome Canada, NGen, Alberta Innovates** build their lists with JavaScript or behind a
  member login, so their main funding page is tracked as one item and flagged **Updated** when it changes.
- Status (Open / Upcoming / Closed) and deadlines are read from page text and are best-effort.
  The page reminds users to confirm details on the official site.
- To change the schedule, edit the `cron` line in `.github/workflows/weekly-scrape.yml`
  (e.g. `0 10 * * 1,4` for Monday and Thursday).
