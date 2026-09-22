"""
RVP FDP Scraper — Direct API approach
======================================
Uses Playwright briefly to extract auth cookies from the persistent Chrome
profile, then calls the QAAS REST API directly with requests.

API flow:
  1. POST /queryapi/queries  (multipart/form-data) → handleId (XML or JSON)
  2. GET  /queryapi/queries/{handleId}              → poll until completed
  3. GET  /queryapi/queries/{handleId}/results/…    → download CSV

Date substitution in query.sql:
  {end_date}   → yesterday (YYYYMMDD)
  {start_date} → end_date minus query.lookback_days from config.yaml

Run:
    python scraper.py

Env vars required (see .env.example):
    FDP_USERNAME, FDP_PASSWORD
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as http_requests
import yaml
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

ROOT_DIR    = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
QUERY_FILE  = ROOT_DIR / "query.sql"

FDP_API_HEADERS = {
    "fdp-lens-api-key":   "ce5234ac-a24b-4fe4-8679-2ebac0ec941c",
    "fdp-lens-app-name":  "FlipQuery",
    "x-requested-with":   "XMLHttpRequest",
    "cache-control":      "no-cache",
}


# ── Config & credentials ──────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_credentials() -> tuple[str, str]:
    username = os.environ.get("FDP_USERNAME")
    password = os.environ.get("FDP_PASSWORD")
    if not username or not password:
        log.error("Set FDP_USERNAME and FDP_PASSWORD env vars (see .env.example)")
        sys.exit(1)
    return username, password


# ── Query preparation ─────────────────────────────────────────────────────────

def get_query(config: dict) -> str:
    if not QUERY_FILE.exists():
        log.error("query.sql not found at %s", QUERY_FILE)
        sys.exit(1)

    text = QUERY_FILE.read_text(encoding="utf-8").strip()
    # Strip SQL comments so the API doesn't choke on them
    lines = [l for l in text.splitlines() if not l.strip().startswith("--")]
    text  = "\n".join(lines).strip()

    lookback = int(config.get("query", {}).get("lookback_days", 90))
    end_dt   = datetime.now() - timedelta(days=1)          # yesterday
    start_dt = end_dt - timedelta(days=lookback - 1)

    end_date   = end_dt.strftime("%Y%m%d")
    start_date = start_dt.strftime("%Y%m%d")

    text = text.replace("{end_date}",   end_date)
    text = text.replace("{start_date}", start_date)

    log.info(
        "Query date range: %s → %s (%d days lookback)",
        start_date, end_date, lookback,
    )
    log.info("Loaded query from %s (%d chars)", QUERY_FILE.name, len(text))
    return text


def _file_size(path: Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── Step 1: Extract cookies via Playwright ────────────────────────────────────

def _test_api_cookies(cookies: dict[str, str], config: dict) -> bool:
    api_cfg  = config.get("api", {})
    base     = api_cfg.get("base_url", "http://fdp.fkinternal.com/p/fdp/lens/lensapi/queryapi")
    test_url = f"{base}/queries"
    try:
        resp = http_requests.get(
            test_url,
            cookies=cookies,
            headers=FDP_API_HEADERS,
            timeout=15,
            allow_redirects=False,
        )
        log.info("API cookie test: status %d", resp.status_code)
        return resp.status_code != 401
    except Exception as e:
        log.warning("API cookie test failed: %s", e)
        return False


def extract_cookies(config: dict, username: str, password: str) -> dict[str, str]:
    browser_cfg   = config.get("browser", {})
    profile_dir   = ROOT_DIR / browser_cfg.get("profile_dir", ".chrome_profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    login_cfg     = config.get("login", {})

    log.info("Extracting auth cookies from Chrome profile...")
    cookies: dict[str, str] = {}

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=browser_cfg.get("headless", False),
            viewport={"width": 1280, "height": 720},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page    = context.pages[0] if context.pages else context.new_page()
        timeout = browser_cfg.get("timeout_ms", 60_000)
        post_login_wait = login_cfg.get("post_login_timeout_ms", 180_000)

        try:
            page.goto(config["site"]["base_url"], timeout=timeout)
            time.sleep(3)
            current_url = page.url

            if "fdp.fkinternal.com" in current_url:
                log.info("Already on FDP domain")
            elif "2ndFactor" in current_url:
                log.info("2FA required — complete it in the Chrome window...")
                _wait_for_fdp(page, post_login_wait)
            else:
                log.info("Login required at: %s", current_url)
                _do_login(page, login_cfg, username, password)
                time.sleep(3)
                if "2ndFactor" in page.url:
                    log.info("2FA required after login — complete it now...")
                    _wait_for_fdp(page, post_login_wait)
                elif "fdp.fkinternal.com" not in page.url:
                    _wait_for_fdp(page, post_login_wait)

            log.info("Visiting query page to refresh API cookies...")
            page.goto(config["site"]["query_url"], timeout=timeout)
            time.sleep(3)

            raw_cookies = context.cookies("http://fdp.fkinternal.com")
            for c in raw_cookies:
                cookies[c["name"]] = c["value"]
            log.info("Extracted %d cookies: %s", len(cookies), list(cookies.keys()))

            if cookies and not _test_api_cookies(cookies, config):
                log.warning("Cookies stale — forcing fresh login...")
                login_url = config["site"]["base_url"] + "/s/fdp/login"
                page.goto(login_url, timeout=timeout)
                time.sleep(2)

                if "fdp.fkinternal.com/query" not in page.url:
                    log.info("Please complete login/2FA in the browser window...")
                    if "2ndFactor" not in page.url:
                        try:
                            _do_login(page, login_cfg, username, password)
                            time.sleep(3)
                        except Exception:
                            log.info("Auto-login failed, waiting for manual login...")
                    _wait_for_fdp(page, post_login_wait)

                page.goto(config["site"]["query_url"], timeout=timeout)
                time.sleep(3)

                cookies.clear()
                raw_cookies = context.cookies("http://fdp.fkinternal.com")
                for c in raw_cookies:
                    cookies[c["name"]] = c["value"]
                log.info("Re-extracted %d cookies after fresh login", len(cookies))

        finally:
            try:
                context.close()
            except Exception:
                pass

    if not cookies:
        log.error("No cookies extracted — auth may have failed")
        sys.exit(1)

    from backend.auth import save_cookies
    save_cookies(cookies)
    log.info("Saved session cookies to .fdp_cookies.json")
    return cookies


def _do_login(page, login_cfg: dict, username: str, password: str) -> None:
    try:
        page.wait_for_selector(login_cfg["username_selector"], timeout=15_000)
    except Exception:
        log.error("Login form not found at: %s", page.url)
        sys.exit(1)
    page.fill(login_cfg["username_selector"], username)
    page.fill(login_cfg["password_selector"], password)
    try:
        idp_sel = login_cfg.get("idp_dropdown_selector", "select")
        if page.locator(idp_sel).count() > 0:
            page.select_option(idp_sel, label=login_cfg.get("idp_value", "Flipkart LDAP"))
    except Exception:
        pass
    page.click(login_cfg["submit_selector"])


def _wait_for_fdp(page, max_wait_ms: int) -> None:
    for elapsed in range(0, max_wait_ms // 1000, 3):
        time.sleep(3)
        try:
            if "fdp.fkinternal.com" in page.url:
                log.info("Authenticated after ~%ds", elapsed + 3)
                return
        except Exception:
            continue
    log.error("Timed out waiting for FDP auth")
    sys.exit(1)


# ── Step 2: Submit query ──────────────────────────────────────────────────────

def submit_query(session: http_requests.Session, config: dict, sql: str) -> str:
    api_cfg    = config.get("api", {})
    base       = api_cfg.get("base_url", "http://fdp.fkinternal.com/p/fdp/lens/lensapi/queryapi")
    submit_url = f"{base}/queries"

    form_fields = {
        "sessionid":  (None, "undefined"),
        "query":      (None, sql),
        "operation":  (None, "execute"),
        "appendEOF":  (None, "true"),
        "sourceName": (None, api_cfg.get("source_name", "BIGQUERY")),
        "queue":      (None, api_cfg.get("queue_name", "fulfillment_adhoc")),
        "rhNodeId":   (None, api_cfg.get("team_name", "core-logistics-t")),
    }

    log.info("Submitting query to %s", submit_url)
    resp = session.post(submit_url, files=form_fields, timeout=120)
    log.info("Submit status: %d | body[:500]: %s", resp.status_code, resp.text[:500])

    if resp.status_code not in (200, 201, 202):
        log.error("Query submission failed: %d — %s", resp.status_code, resp.text[:1000])
        sys.exit(1)

    # JSON response
    try:
        data = resp.json()
        handle_id = (
            data.get("data", {}).get("handleId")
            or data.get("queryHandle", {}).get("handleId")
            or data.get("handleId")
        )
        if handle_id:
            log.info("handleId (JSON): %s", handle_id)
            return handle_id
    except Exception:
        pass

    # XML fallback
    m = re.search(r"<handleId>([^<]+)</handleId>", resp.text)
    if m:
        log.info("handleId (XML): %s", m.group(1))
        return m.group(1)

    log.error("Could not extract handleId: %s", resp.text[:500])
    sys.exit(1)


# ── Step 3: Poll for completion ───────────────────────────────────────────────

def poll_status(session: http_requests.Session, config: dict, handle_id: str) -> dict:
    api_cfg       = config.get("api", {})
    base          = api_cfg.get("base_url", "http://fdp.fkinternal.com/p/fdp/lens/lensapi/queryapi")
    status_url    = f"{base}/queries/{handle_id}"
    max_wait      = api_cfg.get("max_wait_seconds", 600)
    poll_interval = api_cfg.get("poll_interval_seconds", 15)

    log.info("Polling %s (max %ds)", status_url, max_wait)
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        try:
            resp = session.get(status_url, timeout=30)
        except Exception as e:
            log.warning("Poll error: %s", e)
            continue

        if resp.status_code != 200:
            log.warning("Poll returned %d", resp.status_code)
            continue

        try:
            data = resp.json()
        except Exception:
            log.info("  [%ds] non-JSON: %s", elapsed, resp.text[:200])
            continue

        raw_status = data.get("status", {})
        if isinstance(raw_status, dict):
            status           = str(raw_status.get("status", "")).lower()
            result_available = raw_status.get("isResultSetAvailable", False)
        else:
            status           = str(raw_status).lower()
            result_available = data.get("isResultSetAvailable", False)

        log.info("  [%ds/%ds] %s | available=%s", elapsed, max_wait, status, result_available)

        if status in ("completed", "succeeded", "success", "successful", "done", "finished"):
            log.info("Query completed after %ds", elapsed)
            return data
        if status in ("failed", "error", "cancelled", "killed"):
            log.error("Query %s: %s", status, json.dumps(data)[:500])
            sys.exit(1)

    log.error("Timed out after %ds", max_wait)
    sys.exit(1)


# ── Step 4: Download results ──────────────────────────────────────────────────

def download_results(
    session: http_requests.Session, config: dict, handle_id: str, result_data: dict
) -> Path:
    api_cfg      = config.get("api", {})
    base         = api_cfg.get("base_url", "http://fdp.fkinternal.com/p/fdp/lens/lensapi/queryapi")
    download_dir = ROOT_DIR / "downloads"
    download_dir.mkdir(exist_ok=True)

    signed_url      = result_data.get("signedUrl")
    result_set_path = result_data.get("resultSetPath")
    download_url    = result_data.get("downloadUrl") or result_data.get("resultUrl") or signed_url

    if not download_url:
        candidates = [
            f"{base}/queries/{handle_id}/results/download",
            f"{base}/queries/{handle_id}/download",
            f"{base}/query/{handle_id}/results/download",
            f"{base}/savedqueries/{handle_id}/results/download",
            f"{base}/queries/{handle_id}/results",
            f"http://fdp.fkinternal.com/p/fdp/lens/lensapi/queryapi/results/{handle_id}/download",
        ]
        if result_set_path:
            candidates.insert(0, f"{base}/results/download?path={result_set_path}")
            candidates.insert(0, result_set_path)

        for url in candidates:
            log.info("Trying: %s", url)
            try:
                resp = session.get(url, timeout=60, stream=True, allow_redirects=True)
                ct   = resp.headers.get("Content-Type", "")
                cl   = int(resp.headers.get("Content-Length", "0") or "0")
                log.info("  → %d | %s | %d bytes", resp.status_code, ct, cl)
                if resp.status_code == 200 and (
                    "csv" in ct or "octet" in ct or "text/plain" in ct or cl > 100
                ):
                    download_url = url
                    break
            except Exception as e:
                log.info("  → error: %s", e)

    if not download_url:
        log.error("Could not find download URL. Result page: http://fdp.fkinternal.com/query/result/%s", handle_id)
        sys.exit(1)

    log.info("Downloading from: %s", download_url)
    resp = session.get(download_url, timeout=300, stream=True)
    resp.raise_for_status()

    content_disp = resp.headers.get("Content-Disposition", "")
    if "filename=" in content_disp:
        filename = content_disp.split("filename=")[-1].strip('" ')
    else:
        filename = f"rvp_raw_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"

    dest = download_dir / filename
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    log.info("Downloaded: %s (%s)", dest.name, _file_size(dest))
    return dest


# ── Step 5: Save to data/ ─────────────────────────────────────────────────────

def save_output(downloaded: Path, config: dict) -> Path:
    out_cfg    = config.get("output", {})
    out_folder = ROOT_DIR / out_cfg.get("folder", "data")
    out_folder.mkdir(parents=True, exist_ok=True)

    filename   = out_cfg.get("filename")
    if not filename:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"rvp_data_{date_str}.csv"

    final_path = out_folder / filename
    shutil.copy2(str(downloaded), str(final_path))
    log.info("Saved to %s", final_path.relative_to(ROOT_DIR))
    return final_path


# ── Step 6: Git push (optional) ───────────────────────────────────────────────

def git_push(file_path: Path, config: dict) -> None:
    gh_cfg = config.get("github", {})
    if not gh_cfg.get("enabled", False):
        log.info("GitHub push disabled in config.yaml")
        return

    date_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message   = gh_cfg.get("commit_message", "chore: update RVP data {date}").replace("{date}", date_str)
    branch    = gh_cfg.get("branch", "main")
    remote    = gh_cfg.get("remote", "origin")
    repo_path = Path(gh_cfg.get("repo_path", "."))
    if not repo_path.is_absolute():
        repo_path = ROOT_DIR / repo_path

    root_copy = repo_path / file_path.name
    if file_path != root_copy:
        shutil.copy2(str(file_path), str(root_copy))

    for cmd in [
        ["git", "add", root_copy.name],
        ["git", "commit", "-m", message],
        ["git", "push", remote, branch],
    ]:
        result = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True)
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            if "nothing to commit" in combined:
                log.info("No changes to commit")
                return
            log.error("Git failed: %s\n%s", " ".join(cmd), combined)
            sys.exit(1)

    log.info("Pushed to %s/%s", remote, branch)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 55)
    log.info("  RVP FDP Scraper Starting")
    log.info("=" * 55)

    config   = load_config()
    username, password = get_credentials()
    sql      = get_query(config)

    log.info("Step 1/5: Authenticating with FDP...")
    cookies  = extract_cookies(config, username, password)

    session  = http_requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="fdp.fkinternal.com")
    session.headers.update(FDP_API_HEADERS)
    session.headers["accept"] = "application/json; q=1.0, text/*; q=0.8, */*; q=0.1"

    log.info("Step 2/5: Submitting query...")
    handle_id = submit_query(session, config, sql)

    log.info("Step 3/5: Polling for results...")
    result_data = poll_status(session, config, handle_id)

    log.info("Step 4/5: Downloading results...")
    downloaded  = download_results(session, config, handle_id, result_data)

    log.info("Step 5/5: Saving and pushing...")
    final_path  = save_output(downloaded, config)
    git_push(final_path, config)

    log.info("=" * 55)
    log.info("  Done! → %s", final_path.relative_to(ROOT_DIR))
    log.info("=" * 55)


if __name__ == "__main__":
    main()
