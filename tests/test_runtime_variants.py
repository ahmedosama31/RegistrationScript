import sys
import types
import unittest
from unittest.mock import patch


def _install_import_stubs():
    requests = types.ModuleType("requests")
    requests.Session = object
    requests.RequestException = Exception
    requests.HTTPError = Exception
    sys.modules.setdefault("requests", requests)

    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = object
    sys.modules.setdefault("bs4", bs4)

    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.ChromeOptions = object
    by = types.ModuleType("selenium.webdriver.common.by")
    by.By = object
    service = types.ModuleType("selenium.webdriver.chrome.service")
    service.Service = object
    support_ui = types.ModuleType("selenium.webdriver.support.ui")
    support_ui.WebDriverWait = object
    expected_conditions = types.ModuleType("selenium.webdriver.support.expected_conditions")
    sys.modules.setdefault("selenium", selenium)
    sys.modules.setdefault("selenium.webdriver", webdriver)
    sys.modules.setdefault("selenium.webdriver.common", types.ModuleType("selenium.webdriver.common"))
    sys.modules.setdefault("selenium.webdriver.common.by", by)
    sys.modules.setdefault("selenium.webdriver.chrome", types.ModuleType("selenium.webdriver.chrome"))
    sys.modules.setdefault("selenium.webdriver.chrome.service", service)
    sys.modules.setdefault("selenium.webdriver.support", types.ModuleType("selenium.webdriver.support"))
    sys.modules.setdefault("selenium.webdriver.support.ui", support_ui)
    sys.modules.setdefault("selenium.webdriver.support.expected_conditions", expected_conditions)

    webdriver_manager = types.ModuleType("webdriver_manager")
    webdriver_manager_chrome = types.ModuleType("webdriver_manager.chrome")
    webdriver_manager_chrome.ChromeDriverManager = object
    sys.modules.setdefault("webdriver_manager", webdriver_manager)
    sys.modules.setdefault("webdriver_manager.chrome", webdriver_manager_chrome)

    pil = types.ModuleType("PIL")
    pil.Image = object
    sys.modules.setdefault("PIL", pil)


_install_import_stubs()
import register


class FakeResponse:
    def __init__(self, text, url="https://std.eng.cu.edu.eg/", status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise register.requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self):
        self.cookies = {}
        self.post_call = None
        self.get_count = 0

    def get(self, url, timeout=None):
        self.get_count += 1
        if self.get_count == 1:
            return FakeResponse(
                '<script>Ext.net.ResourceMgr.init({id:"ctl03"});'
                'new Ext.Window({buttons:[{id:"Button1",text:"Login"}]});</script>'
                '<form><input name="__VIEWSTATE" value="state">'
                '<input name="__VIEWSTATEGENERATOR" value="generator">'
                '<input name="__EVENTVALIDATION" value="validation"></form>'
            )
        return FakeResponse("authenticated", "https://std.eng.cu.edu.eg/SIS/Default.aspx")

    def post(self, url, data=None, headers=None, timeout=None):
        self.post_call = {"url": url, "data": data, "headers": headers}
        self.cookies["ASP.NET_SessionId"] = "test-session"
        return FakeResponse(r'{script:"window.location=\"/SIS/Default.aspx\";"}')


class FakeBrowserDriver:
    def __init__(self, selected):
        self.selected = selected
        self.clicked = []

    def execute_script(self, script, *args):
        if "const schId" in script:
            self.clicked.append(str(args[0]))
            return {"found": True, "selected": True, "elementId": "Lecture1"}
        return list(self.selected)


class DirectLoginTests(unittest.TestCase):
    def test_direct_login_uses_extnet_event_and_returns_session(self):
        fake_session = FakeSession()
        with patch.object(register.requests, "Session", return_value=fake_session):
            session, guid, page = register.login_with_requests("student", "secret")

        self.assertIs(session, fake_session)
        self.assertIsNone(guid)
        self.assertEqual("authenticated", page)
        self.assertEqual("ctl03", fake_session.post_call["data"]["__EVENTTARGET"])
        self.assertEqual("Button1|event|Click", fake_session.post_call["data"]["__EVENTARGUMENT"])
        self.assertEqual("student", fake_session.post_call["data"]["txtUsername"])
        self.assertEqual("secret", fake_session.post_call["data"]["txtPassword"])
        self.assertEqual("delta=true", fake_session.post_call["headers"]["X-Ext.Net"])


class VisibleSelectionTests(unittest.TestCase):
    @staticmethod
    def client(driver):
        client = register.RegistrationClient({}, browser_driver=driver)
        lecture = {
            "day": "Sunday",
            "code": "CMPS211",
            "type": "Lecture",
            "period_label": "9:10 (4:00-5:50)",
            "sch_id": "42",
        }
        client.new_selected_details = [lecture]
        client.selected_lecture_details = [lecture]
        return client

    def test_visible_click_accepts_matching_browser_selection(self):
        driver = FakeBrowserDriver(["42"])
        self.assertTrue(self.client(driver).click_selected_sections_in_browser(pause_seconds=0))
        self.assertEqual(["42"], driver.clicked)

    def test_visible_click_stops_when_browser_selection_conflicts(self):
        driver = FakeBrowserDriver([])
        self.assertFalse(self.client(driver).click_selected_sections_in_browser(pause_seconds=0))

    def test_visible_main_hands_off_without_submitting(self):
        calls = []

        class FakeStore:
            def __init__(self, remember=True):
                pass

            def ensure_login_credentials(self):
                return "student", "password"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.registration_html = None
                self.registration_table_ready = False

            def get_registration_page(self):
                return True

            def accept_approval(self):
                return True

            def get_timetable_and_select_course(self, *args, **kwargs):
                return True

            def click_selected_sections_in_browser(self):
                calls.append("clicked")
                return True

            def show_selected_course_summary(self):
                calls.append("summary")

            def submit_selection(self):
                raise AssertionError("visible mode must not submit")

        options = {
            "course": "CMPS211",
            "specific_sections": [],
            "interactive": False,
            "use_schedule_plan": False,
            "schedule_plan_student_id": "student",
            "schedule_plan_name": None,
            "schedule_source": "manual",
            "live": False,
            "visible_after_selection": "handoff",
        }
        driver = object()
        with (
            patch.object(register, "CredentialStore", FakeStore),
            patch.object(register, "collect_cli_options", return_value=options),
            patch.object(register, "confirm_pre_open_plan"),
            patch.object(register, "login_with_selenium", return_value=({"sid": "cookie"}, None, None, driver)),
            patch.object(register, "wait_for_registration_to_open_in_browser", return_value="registration page"),
            patch.object(register, "RegistrationClient", FakeClient),
            patch("builtins.input", return_value=""),
        ):
            register.main("visible")

        self.assertEqual(["clicked", "summary"], calls)

    def test_visible_main_can_continue_automatically(self):
        calls = []

        class FakeStore:
            def __init__(self, remember=True):
                pass

            def ensure_login_credentials(self):
                return "student", "password"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.registration_html = None
                self.registration_table_ready = False

            def get_registration_page(self):
                return True

            def accept_approval(self):
                return True

            def get_timetable_and_select_course(self, *args, **kwargs):
                return True

            def click_selected_sections_in_browser(self):
                calls.append("clicked")
                return True

            def confirm_selection_before_submit(self):
                calls.append("confirmed-selection")
                return True

            def submit_selection(self):
                calls.append("submitted-selection")
                return True

            def confirm_final_registration_request(self):
                calls.append("confirmed-final")
                return True

            def solve_captcha(self, manual=False):
                calls.append("captcha")
                return "1234"

            def finalize_registration(self, captcha_text, password):
                calls.append("finalized")
                return True

        options = {
            "course": "CMPS211",
            "specific_sections": [],
            "interactive": False,
            "use_schedule_plan": False,
            "schedule_plan_student_id": "student",
            "schedule_plan_name": None,
            "schedule_source": "manual",
            "live": False,
            "visible_after_selection": "automatic",
        }
        driver = object()
        with (
            patch.object(register, "CredentialStore", FakeStore),
            patch.object(register, "collect_cli_options", return_value=options),
            patch.object(register, "confirm_pre_open_plan"),
            patch.object(register, "login_with_selenium", return_value=({"sid": "cookie"}, None, None, driver)),
            patch.object(register, "wait_for_registration_to_open_in_browser", return_value="registration page"),
            patch.object(register, "RegistrationClient", FakeClient),
        ):
            register.main("visible")

        self.assertEqual(
            [
                "clicked",
                "confirmed-selection",
                "submitted-selection",
                "confirmed-final",
                "captcha",
                "finalized",
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
