import re
from playwright.sync_api import Playwright, sync_playwright

# ================= CONFIGURATION =================
# Set your desired 1-year date range here (Format: YYYY-MM-DD)
START_DATE = "2025-05-21"  
END_DATE = "2026-05-21"

RECIPIENT_EMAIL = "pillasaiganesh.vc@flipkart.com"
LOGIN_EMAIL = "pillasaiganesh.vc@flipkart.com"
LOGIN_PASSWORD = "uUtAsFC0uCiR6bO"
# =================================================

def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False, slow_mo=500)
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()

    # 1. OPEN WEBSITE
    print("Navigating to login page...")
    page.goto(
        "https://goswift.auth0.com/login?state=hKFo2SBMTW5WR3c4VkQ3M0xmbEtFdVFrbmg1MkpObjFCSXhOeaFupWxvZ2luo3RpZNkgeThpWlcwTE9xRGZYOWdkdFlSUFhLQ25Rc3pMalV1VzijY2lk2SBhN0swbGpsczFoVFhHRWRieFY2cEMxdl9Oc1ZKLWJMdA&client=a7K0ljls1hTXGEdbxV6pC1v_NsVJ-bLt&protocol=oauth2&audience=api.goswift.in&version=v2&appName=FULFILLMENT&mode=web&brand=Ekart&customLogo=https%3A%2F%2Fstorage.googleapis.com%2Ffs.goswift.in%2Fsite%2Fekart_logo.png&primaryColor=%23337ab7&redirect_uri=https%3A%2F%2Fapp.elite.ekartlogistics.in%2Fauthorize&scope=openid%20profile%20email&response_type=code&response_mode=query&nonce=a3NLfnZCUWdtLkZHWE91UXFUUk1UYkFmfmFMVUV6Q0MycllZS1Nffmd%2BXw%3D%3D&code_challenge=ZgqpJYHfxWf4_3iiYLMBfdWKXGM2vYNAaYAx0QSNRzI&code_challenge_method=S256&auth0Client=eyJuYW1lIjoiYXV0aDAtcmVhY3QiLCJ2ZXJzaW9uIjoiMS44LjAifQ%3D%3D",
        wait_until="networkidle"
    )

    # 2. LOGIN
    print("Logging in...")
    page.get_by_role("textbox", name="Email").fill(LOGIN_EMAIL)
    page.get_by_role("textbox", name="Password").fill(LOGIN_PASSWORD)
    page.get_by_role("button", name="Log In").click()
    page.wait_for_load_state("networkidle")
    
    # 3. NAVIGATE TO REPORTS CENTRE
    print("Opening Reports Centre...")
    reports_link = page.get_by_role("link", name="Reports Centre")
    reports_link.wait_for(state="visible")
    reports_link.click()

    # 4. SELECT SCHEDULED REPORT
    print("Selecting Scheduled Report option...")
    scheduled_report_tab = page.locator("div").filter(has_text=re.compile(r"^Scheduled Report$")).first
    scheduled_report_tab.wait_for(state="visible")
    scheduled_report_tab.click()

    # 5. CHOOSE REPORT TYPE
    print("Selecting Report Type...")
    page.get_by_role("combobox", name="* Report Type").click()
    page.wait_for_selector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    page.get_by_text("Onboarding Analytics Report").click()

    # 6. FILL "FROM" DATE (Direct input typing bypasses calendar clicking)
    print(f"Typing From Date: {START_DATE}")
    from_date_input = page.locator(".ant-picker-input input").first
    from_date_input.click()
    from_date_input.press("Control+A")  # Selects all pre-existing text inside input
    from_date_input.fill(START_DATE)
    from_date_input.press("Enter")      # Submits the input to lock it in
    page.wait_for_timeout(500)

    # 7. FILL "TO" DATE
    print(f"Typing To Date: {END_DATE}")
    to_date_input = page.locator(".ant-picker-input input").nth(1)
    to_date_input.click()
    to_date_input.press("Control+A")
    to_date_input.fill(END_DATE)
    to_date_input.press("Enter")
    page.wait_for_timeout(500)

    # 8. RECIPIENT EMAIL & SUBMIT
    print("Filling recipient information and scheduling...")
    page.get_by_role("textbox", name="Recipient Emails").fill(RECIPIENT_EMAIL)
    
    # Click schedule
    page.get_by_role("button", name="Schedule").click()

    # Wait for completion confirmation
    page.wait_for_timeout(3000)
    print("Report Scheduled Successfully for the requested date range!")

    context.close()
    browser.close()

if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)