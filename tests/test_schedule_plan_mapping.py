import importlib
import sys
import types
import unittest
import xml.etree.ElementTree as ET


def _install_import_stubs():
    """Allow pure mapping tests to run even when optional runtime packages are absent."""
    requests = types.ModuleType("requests")
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
register = importlib.import_module("register")


class SchedulePlanGroupMappingTests(unittest.TestCase):
    def setUp(self):
        root = ET.fromstring(
            """
            <TimeTable>
              <day Name="Thursday">
                <lecture Code="CMPS999" Name="Example Course-_1(0/0)"
                         Type="Tutorial" Period="6:8" SchId="101" Selected="0" />
                <lecture Code="CMPS999" Name="Example Course-_2(0/0)"
                         Type="Tutorial" Period="6:8" SchId="202" Selected="0" />
              </day>
            </TimeTable>
            """
        )
        self.sis_lectures = register.RegistrationClient.collect_lectures(root)

    @staticmethod
    def planned_section(group):
        return {
            "courseCode": "CMPS999",
            "type": "Tutorial",
            "group": group,
            "sessions": [
                {
                    "day": "Thursday",
                    "startString": "1:00",
                    "endString": "3:50",
                }
            ],
        }

    def test_same_time_tutorials_are_resolved_by_group(self):
        results = register.build_schedule_plan_mapping(
            [self.planned_section(2)], self.sis_lectures
        )

        self.assertEqual([], results[0]["errors"])
        self.assertEqual(["202"], [match["sch_id"] for match in results[0]["matches"]])

    def test_same_time_tutorials_without_group_remain_ambiguous(self):
        results = register.build_schedule_plan_mapping(
            [self.planned_section(None)], self.sis_lectures
        )

        self.assertEqual([], results[0]["matches"])
        self.assertIn("ambiguous", results[0]["errors"][0])

    def test_group_is_extracted_from_sis_name_suffix(self):
        self.assertEqual(["1", "2"], [lecture["group"] for lecture in self.sis_lectures])

    def test_unmapped_section_does_not_discard_other_mapped_sections(self):
        unavailable_corequisite = {
            "courseCode": "COREQ100",
            "type": "Lecture",
            "group": 1,
            "sessions": [
                {
                    "day": "Sunday",
                    "startString": "4:00",
                    "endString": "5:50",
                }
            ],
        }

        selected_schids = register.map_schedule_plan_sections_to_sis(
            [unavailable_corequisite, self.planned_section(2)],
            self.sis_lectures,
        )

        self.assertEqual(["202"], selected_schids)


if __name__ == "__main__":
    unittest.main()
