def fetch_mdi_jobs():
    """
    Parses the public MDI jobs listing page. Every listing on this site is
    by definition a medical device role in Israel, so these bypass the
    keyword filter. The site doesn't expose exact posting timestamps, so we
    use its own "New Job" badge as a recency signal, and rely on dedup to
    prevent repeats. If MDI ever redesigns their page, this function may
    need updating (unlike the API-based sources above).
    """
    results = []
    try:
        resp = requests.get(MDI_JOBS_URL, timeout=REQUEST_TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        headings = [h for h in soup.find_all(["h2", "h3"])
                    if h.find("a", href=lambda x: x and "/job/" in x)]
        print(f"MDI: HTTP {resp.status_code}, page length {len(resp.text)} chars, "
              f"{len(headings)} job headings found")

        for h in headings:
            a = h.find("a", href=lambda x: x and "/job/" in x)
            title = a.get_text(strip=True)
            link = a["href"].strip()
            if not title or not link:
                continue

            company = category = location = date_text = ""
            is_new = False

            steps = 0
            for elem in h.find_all_next():
                if elem.name in ("h2", "h3", "hr"):
                    break
                if elem.name == "ul":
                    lis = [li.get_text(strip=True) for li in elem.find_all("li")]
                    joined = " ".join(lis).lower()
                    if "new job" in joined:
                        is_new = True
                    elif lis and not company:
                        company = lis[0] if len(lis) > 0 else ""
                        category = lis[1] if len(lis) > 1 else ""
                        location = lis[2] if len(lis) > 2 else ""
                        date_text = lis[3] if len(lis) > 3 else ""
                steps += 1
                if steps > 60:
                    break

            if not is_new:
                continue  # only take listings MDI itself flags as recent

            snippet_parts = [p for p in [category, location, date_text] if p]
            results.append({
                "title": title,
                "company": company or "MDI listing",
                "location": location or "Israel",
                "snippet": " | ".join(snippet_parts),
                "link": link,
                "updated": datetime.now(timezone.utc).isoformat(),
                "source": "MDI",
            })

        print(f"MDI: {len(results)} new-flagged listings found on page 1")
    except requests.RequestException as e:
        print(f"ERROR fetching MDI jobs page: {e}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR parsing MDI jobs page: {e}", file=sys.stderr)

    return results
