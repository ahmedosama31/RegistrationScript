import time
import re
import json
import logging
import argparse
import sys
import os
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urljoin, quote
from PIL import Image
from io import BytesIO

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

BASE_URL = "https://std.eng.cu.edu.eg"
REGISTRATION_PATH = "/SIS/Modules/MetaLoader.aspx?path=~/SIS/Modules/Student/Registration/Registration.ascx"
XML_HANDLER_PATH = "/SIS/Modules/MyXMLHandler.ashx"
CAPTCHA_PATH = "/SIS/Modules/CaptchaImage.aspx"

# Optional local fallback if dynamic extraction fails.
# Keep empty in shared code; provide interactively when prompted.
MANUAL_STD_ID_GUID = ""
SLOT_TIME_BOUNDS = "8:00,8:50,9:00,9:50,10:00,10:50,11:00,11:50,12:00,12:50,1:00,1:50,2:00,2:50,3:00,3:50,4:00,4:50,5:00,5:50,6:00,6:50,7:00,7:50,8:00,8:50,9:00,9:50"


def build_slot_time_map(bounds_csv):
    """Map SIS slot number -> (start_time, end_time)."""
    items = [x.strip() for x in bounds_csv.split(",") if x.strip()]
    slot_map = {}
    for i in range(0, len(items), 2):
        slot = (i // 2) + 1
        if i + 1 < len(items):
            slot_map[slot] = (items[i], items[i + 1])
    return slot_map


SLOT_TIME_MAP = build_slot_time_map(SLOT_TIME_BOUNDS)


def period_to_time_range(period):
    """Convert SIS period notation like 9:10 into human time range."""
    m = re.fullmatch(r"(\d+):(\d+)", (period or "").strip())
    if not m:
        return None
    start_slot = int(m.group(1))
    end_slot = int(m.group(2))
    if start_slot > end_slot:
        return None
    if start_slot not in SLOT_TIME_MAP or end_slot not in SLOT_TIME_MAP:
        return None
    return SLOT_TIME_MAP[start_slot][0], SLOT_TIME_MAP[end_slot][1]


def format_period_with_time(period):
    rng = period_to_time_range(period)
    if not rng:
        return period
    return f"{period} ({rng[0]}-{rng[1]})"


def normalize_period_input(period_text):
    """
    Accept:
      - SIS slots: '9:10'
      - explicit clock range: '4-6', '4:00-6:00', '4 to 6'
    Returns: (normalized_period, note_or_none)
    """
    raw = (period_text or "").strip()
    if not raw or raw == "*":
        return raw, None

    # Native SIS period format.
    if re.fullmatch(r"\d+:\d+", raw):
        return raw, None

    normalized = re.sub(r"\s+to\s+", "-", raw, flags=re.IGNORECASE).replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::00)?-(\d{1,2})(?::00)?", normalized)
    if not m:
        return raw, None

    start_hour = int(m.group(1))
    end_hour = int(m.group(2))
    if end_hour <= start_hour:
        return raw, None

    start_label = f"{start_hour}:00"
    end_label = f"{end_hour - 1}:50"

    start_candidates = [slot for slot, bounds in SLOT_TIME_MAP.items() if bounds[0] == start_label]
    end_candidates = [slot for slot, bounds in SLOT_TIME_MAP.items() if bounds[1] == end_label]

    # Avoid ambiguous mappings (e.g., 8:00 appears twice).
    if len(start_candidates) != 1 or len(end_candidates) != 1:
        return raw, None

    start_slot = start_candidates[0]
    end_slot = end_candidates[0]
    if start_slot > end_slot:
        return raw, None

    converted = f"{start_slot}:{end_slot}"
    note = f"Interpreted time range '{raw}' as SIS period '{converted}' ({start_label}-{end_label})."
    return converted, note

def login_with_selenium():
    """Use Selenium to handle login and extract session cookies."""
    logger.info("Launching Selenium Chrome for login...")
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless')  # Comment out to see the browser
    driver = webdriver.Chrome(service=webdriver.chrome.service.Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get(BASE_URL)
        logger.info("Please log in manually in the browser window.")
        logger.info("Waiting for login to complete (redirect to /SIS/Default.aspx)...")
        
        # Wait up to 120 seconds for user to login
        WebDriverWait(driver, 120).until(
            EC.url_contains("/SIS/Default.aspx")
        )
        logger.info("Login detected! Extracting cookies...")
        
        selenium_cookies = driver.get_cookies()
        session_cookies = {}
        for cookie in selenium_cookies:
            session_cookies[cookie['name']] = cookie['value']
            
        logger.info(f"Extracted {len(session_cookies)} cookies.")
        
        # Extract Student GUID and User ID from dashboard source
        page_source = driver.page_source
        
        # GUID Pattern 1: image.aspx?FileName=GUID
        # GUID Pattern 2: stdid=GUID (common in AJAX calls)
        std_guid = None
        
        guid_match = re.search(r'FileName=([a-f0-9\-]{36})', page_source)
        if guid_match:
            std_guid = guid_match.group(1)
            logger.info(f"Found Student GUID (Pattern 1): {std_guid}")
        else:
            guid_match_2 = re.search(r'stdid=([a-f0-9\-]{36})', page_source)
            if guid_match_2:
                std_guid = guid_match_2.group(1)
                logger.info(f"Found Student GUID (Pattern 2): {std_guid}")
            
        return session_cookies, std_guid
        
    except Exception as e:
        logger.error(f"Login failed or timed out: {e}")
        return None, None
    finally:
        driver.quit()

class RegistrationClient:
    def __init__(self, cookies, std_guid=None, dry_run=True):
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.dry_run = dry_run
        self.viewstate = None
        self.eventvalidation = None
        self.viewstategenerator = None
        self.std_guid = std_guid
        self.user_id = None
        self.selected_lectures = []
        self.stdid_candidates = []
        if std_guid:
            self._add_stdid_candidate(std_guid)

    def _add_stdid_candidate(self, stdid):
        """Store possible stdid values while preserving insertion order."""
        if not stdid:
            return
        stdid = stdid.strip()
        if stdid and stdid not in self.stdid_candidates:
            self.stdid_candidates.append(stdid)

    def _extract_identifiers_from_html(self, html, source="page"):
        """
        Extract stdid/userName hints from registration HTML.
        The SIS pages often embed the exact MyXMLHandler URL in JS.
        """
        found_stdid = None
        found_user = None

        # Most reliable source once approval is accepted.
        loadxml_match = re.search(
            r"MyXMLHandler\.ashx\?stdid=([a-f0-9\-]+)&userName=(\d+)",
            html,
            re.IGNORECASE
        )
        if loadxml_match:
            found_stdid = loadxml_match.group(1)
            found_user = loadxml_match.group(2)

        # Fallback patterns.
        if not found_stdid:
            stdid_match = re.search(r"stdid=([a-f0-9\-]+)", html, re.IGNORECASE)
            if stdid_match:
                found_stdid = stdid_match.group(1)
        if not found_stdid:
            img_match = re.search(r"FileName=([a-f0-9\-]{36})", html, re.IGNORECASE)
            if img_match:
                found_stdid = img_match.group(1)

        if not found_user:
            user_match = re.search(r"userName=(\d+)", html)
            if user_match:
                found_user = user_match.group(1)
        if not found_user:
            # Seen in registration markup as: !isNaN('1234567')
            user_match = re.search(r"isNaN\('(\d+)'\)", html)
            if user_match:
                found_user = user_match.group(1)

        if found_stdid:
            self.std_guid = found_stdid
            self._add_stdid_candidate(found_stdid)
            logger.info(f"Found stdid from {source}: {found_stdid}")

        if found_user:
            self.user_id = found_user
            logger.info(f"Found User ID from {source}: {found_user}")

    def _build_stdid_candidates(self):
        """Return ordered stdid candidates with common SIS variants."""
        candidates = []

        def add(val):
            if val and val not in candidates:
                candidates.append(val)

        add(self.std_guid)
        for c in self.stdid_candidates:
            add(c)

        # SIS sometimes uses GUID + trailing zero in MyXMLHandler calls.
        base_list = list(candidates)
        for c in base_list:
            if re.fullmatch(r"[a-f0-9\-]{36}", c, flags=re.IGNORECASE):
                add(c + "0")
            if re.fullmatch(r"[a-f0-9\-]{37}", c, flags=re.IGNORECASE) and c.endswith("0"):
                add(c[:-1])

        return candidates

    @staticmethod
    def _looks_like_html_error(text):
        lowered = text[:400].lower()
        return (
            "<html" in lowered
            or "<!doctype html" in lowered
            or "aspxerrorpath" in lowered
            or "an error has occurred" in lowered
        )

    def get_registration_page(self):
        """Fetch the registration page and extract VIEWSTATE."""
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        logger.info(f"Fetching registration page: {url}")
        
        resp = self.session.get(url)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch registration page: {resp.status_code}")
            return False
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        self.viewstate = soup.find('input', {'id': '__VIEWSTATE'}).get('value')
        self.eventvalidation = soup.find('input', {'id': '__EVENTVALIDATION'}).get('value')
        self.viewstategenerator = soup.find('input', {'id': '__VIEWSTATEGENERATOR'}).get('value')
        self._extract_identifiers_from_html(resp.text, source="registration page")
             
        return True

    def accept_approval(self):
        """Step 1: Click 'Next' on the approval message."""
        logger.info("Step 1: Accepting approval message...")
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            '__EVENTVALIDATION': self.eventvalidation,
            'ctl07$ButMessageShown': 'Next>>',
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'BData': ''
        }
        
        resp = self.session.post(url, data=payload)
        if resp.status_code != 200:
            logger.error("Failed to accept approval.")
            return False
            
        # Update ViewState for next step
        soup = BeautifulSoup(resp.text, 'html.parser')
        self.viewstate = soup.find('input', {'id': '__VIEWSTATE'}).get('value')
        self.eventvalidation = soup.find('input', {'id': '__EVENTVALIDATION'}).get('value')
        self.viewstategenerator = soup.find('input', {'id': '__VIEWSTATEGENERATOR'}).get('value')
        self._extract_identifiers_from_html(resp.text, source="approval response")
        return True

    def get_timetable_and_select_course(self, target_course_code, specific_sections=None):
        """Fetch XML timetable and find the SchIds for the target course sections."""
        logger.info(f"Fetching timetable XML for {target_course_code}...")
        if not self.user_id:
            logger.warning("Could not auto-detect User ID.")
            self.user_id = input("Enter Student ID (e.g. 1234567): ").strip()

        candidates = self._build_stdid_candidates()
        if not candidates:
            logger.warning("Could not auto-detect Student GUID/stdid.")
            if MANUAL_STD_ID_GUID:
                prompt = f"Enter Student GUID/stdid (default: {MANUAL_STD_ID_GUID}): "
            else:
                prompt = "Enter Student GUID/stdid: "
            manual = input(prompt).strip() or MANUAL_STD_ID_GUID
            if not manual:
                logger.error("Student GUID/stdid is required when auto-detection fails.")
                return False
            self.std_guid = manual
            self._add_stdid_candidate(manual)
            candidates = self._build_stdid_candidates()

        import random
        import xml.etree.ElementTree as ET

        root = None
        xml_content = None
        last_response_text = None
        last_url = None

        for stdid in candidates:
            t_val = str(random.randint(10000000, 99999999))
            xml_url = urljoin(BASE_URL, f"{XML_HANDLER_PATH}?stdid={stdid}&userName={self.user_id}&t={t_val}")
            logger.info(f"Trying XML URL: {xml_url}")

            resp = self.session.get(xml_url)
            if resp.status_code != 200:
                logger.warning(f"MyXMLHandler returned status {resp.status_code} for stdid={stdid}")
                continue

            candidate_xml = resp.text.replace('&nbsp;', ' ')
            last_response_text = candidate_xml
            last_url = xml_url

            if self._looks_like_html_error(candidate_xml):
                logger.warning(f"MyXMLHandler returned HTML error page for stdid={stdid}")
                continue

            try:
                parsed = ET.fromstring(candidate_xml)
            except Exception as e:
                logger.warning(f"MyXMLHandler parse failed for stdid={stdid}: {e}")
                continue

            root = parsed
            xml_content = candidate_xml
            self.std_guid = stdid
            self._add_stdid_candidate(stdid)
            logger.info(f"Using stdid={stdid} for timetable parsing.")
            break

        if root is None:
            logger.error("Failed to fetch a valid timetable XML from MyXMLHandler.")
            if last_url:
                logger.error(f"Last attempted XML URL: {last_url}")
            if last_response_text:
                with open('debug_timetable.xml', 'w', encoding='utf-8') as f:
                    f.write(last_response_text)
                logger.info("Saved failing MyXMLHandler response to 'debug_timetable.xml'")
                logger.error(f"Response preview: {last_response_text[:500]}")
            return False

        with open('debug_timetable.xml', 'w', encoding='utf-8') as f:
            f.write(xml_content)
        logger.info("Saved timetable response to 'debug_timetable.xml'")

        # Find the course
        found_lectures = []
        found_any_matching_code = False
        
        # specific_sections = [("Day", "Period"), ("Day2", "Period2")] OR None
        
        for day in root:
            day_name = day.attrib.get('Name', '')
            
            for lecture in day:
                code = lecture.attrib.get('Code', '')
                if target_course_code.upper() in code.upper():
                    found_any_matching_code = True
                    
                    sch_id = lecture.attrib.get('SchId')
                    l_type = lecture.attrib.get('Type')
                    period = lecture.attrib.get('Period')
                    period_label = format_period_with_time(period)
                    
                    is_match = False
                    if specific_sections:
                        # Check if this lecture matches ANY of the target specs
                        for (target_day, target_period) in specific_sections:
                            day_match = target_day.lower() in day_name.lower()
                            # If period is '*', we match ANY period on that day
                            period_match = (target_period == '*') or (target_period == period)
                            if day_match and period_match:
                                is_match = True
                                break
                    else:
                        # No specific sections = math ALL sections (risky but default)
                        is_match = True

                    if is_match:
                        logger.info(f"Found MATCH: {code} ({l_type}) on {day_name} {period_label} [SchId={sch_id}]")
                        found_lectures.append(sch_id)
                    else:
                        logger.info(f"Skipping: {code} ({l_type}) on {day_name} {period_label} (Did not match targets)")

        
        if not found_lectures:
            if found_any_matching_code:
                logger.error(f"Course {target_course_code} found, but no sections matched your criteria.")
            else:
                logger.error(f"Course {target_course_code} not found in timetable!")
                available_codes = sorted({
                    lecture.attrib.get('Code', '')
                    for day in root
                    for lecture in day
                    if lecture.attrib.get('Code')
                })
                if available_codes:
                    logger.info(f"Available course codes in timetable: {', '.join(available_codes)}")
            return False
            
        # Warn if selecting MANY sections without specificity
        if len(found_lectures) > 2 and not specific_sections:
            logger.warning(f"Found {len(found_lectures)} sections! You are registering for ALL of them. Use --section to be specific.")
            
        # Preserve existing selections
        existing_selected = []
        for day in root:
            for lecture in day:
                if lecture.attrib.get('Selected') == '1':
                    existing_selected.append(lecture.attrib.get('SchId'))
        
        logger.info(f"Preserving {len(existing_selected)} existing registrations.")
        
        # Combine
        all_schids = list(set(existing_selected + found_lectures))
        # Format: ,id1,id2,id3,
        self.selected_lectures_str = "," + ",".join(all_schids) + ","
        logger.info(f"Final Selection String: {self.selected_lectures_str}")
        
        return True

    def submit_selection(self):
        """Step 2: Submit the selected lectures."""
        logger.info("Step 2: Submitting course selection...")
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            '__EVENTVALIDATION': self.eventvalidation,
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'ctl07$nextPage': 'Next+>>',
            'StdSelectedLecs': self.selected_lectures_str,
            'StdSelectedLecs1': self.selected_lectures_str,
            'BData': ''
        }
        
        resp = self.session.post(url, data=payload)
        if resp.status_code != 200:
            logger.error("Failed to submit selection.")
            return False

        # Update ViewState for final step
        soup = BeautifulSoup(resp.text, 'html.parser')
        self.viewstate = soup.find('input', {'id': '__VIEWSTATE'}).get('value')
        self.eventvalidation = soup.find('input', {'id': '__EVENTVALIDATION'}).get('value')
        self.viewstategenerator = soup.find('input', {'id': '__VIEWSTATEGENERATOR'}).get('value')
        self.final_soup = soup # Store for captcha guid extraction
        return True

    def solve_captcha(self):
        """Step 3a: Get captcha image."""
        # Find captcha GUID
        # Pattern: CaptchaImage.aspx?guid=...
        captcha_img = self.final_soup.find('img', src=re.compile(r'CaptchaImage\.aspx'))
        if not captcha_img:
            logger.error("Captcha image not found on final page!")
            return None
            
        captcha_url = urljoin(BASE_URL, f"/SIS/Modules/{captcha_img['src']}")
        logger.info(f"Downloading Captcha: {captcha_url}")
        
        resp = self.session.get(captcha_url)
        img = Image.open(BytesIO(resp.content))
        img.show()
        
        captcha_text = input("Enter Captcha Text: ").strip()
        return captcha_text

    def finalize_registration(self, captcha_text, password):
        """Step 3b: Final Registration (DRY RUN SAFEGUARD)."""
        logger.info("Preparing Final Registration Request...")
        
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            '__EVENTVALIDATION': self.eventvalidation,
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'ctl07$txtEmail': '',
            'ctl07$txtTel': '',
            'ctl07$txtComment': '',
            'ctl07$txtPassword': password,
            'ctl07$CaptchaControl1': captcha_text,
            'ctl07$ButAccept': 'Register',
            'StdSelectedLecs2': self.selected_lectures_str,
            'BData': ''
        }
        
        if self.dry_run:
            logger.info("!!! DRY RUN MODE ACTIVE !!!")
            logger.info("The following payload WOULD be sent:")
            logger.info(json.dumps(payload, indent=2, default=str)[:500] + "...")
            logger.info("Request NOT sent.")
            return True
        else:
            logger.info("SENDING REGISTRATION REQUEST...")
            resp = self.session.post(url, data=payload)
            if resp.status_code == 200:
                logger.info("Registration request sent! Check result.")
                logger.info(resp.text[:500])
                if "success" in resp.text.lower() or "registered" in resp.text.lower():
                     logger.info("SUCCESS: Registration likely successful.")
                else:
                     logger.warning("WARNING: Success message not strictly found. Verify manually.")
                return True
            else:
                logger.error(f"Registration failed: {resp.status_code}")
                return False

def main():
    parser = argparse.ArgumentParser(description="SIS Registration Bot")
    parser.add_argument('--course', type=str, default="CMPS211", help="Target Course Code")
    parser.add_argument('--add-section', action='append', help="Add a target section (Format: 'Day Period', e.g. 'Monday 2:3'). Can be used multiple times.")
    parser.add_argument('--live', action='store_true', help="DISABLE Dry Run (Send real requests)")
    args = parser.parse_args()
    
    # Parse section targets
    # input: ["Monday 2:3", "Sunday 4:5"]
    # output: [("Monday", "2:3"), ("Sunday", "4:5")]
    specific_sections = []
    if args.add_section:
        for s in args.add_section:
            parts = s.strip().split()
            if len(parts) >= 2:
                # "Monday 2:3" -> day="Monday", period="2:3"
                # If period has spaces? Unlikely based on XML.
                day = parts[0]
                period, note = normalize_period_input(parts[1])
                if note:
                    logger.info(note)
                mapped = period_to_time_range(period)
                if mapped and re.fullmatch(r"\d+:\d+", period):
                    logger.info(f"Section filter '{day} {period}' maps to actual time {mapped[0]}-{mapped[1]}.")
                specific_sections.append((day, period))
            else:
                logger.warning(f"Invalid section format: '{s}'. Expected 'Day Period'. Ignoring.")

    # 1. Login
    cookies, std_guid = login_with_selenium()
    if not cookies:
        sys.exit(1)
        
    client = RegistrationClient(cookies, std_guid=std_guid, dry_run=not args.live)
    
    # 2. Get Page
    if not client.get_registration_page():
        sys.exit(1)
        
    # 3. Accept Approval
    if not client.accept_approval():
        sys.exit(1)
        
    # 4. Get Timetable & Select
    if not client.get_timetable_and_select_course(args.course, specific_sections):
        sys.exit(1)
        
    # 5. Submit Selection
    if not client.submit_selection():
        sys.exit(1)
        
    # 6. Captcha
    captcha_text = client.solve_captcha()
    if not captcha_text:
        sys.exit(1)
        
    password = input("Enter your SIS Password: ").strip()
    
    # 7. Finalize
    client.finalize_registration(captcha_text, password)

if __name__ == "__main__":
    main()
