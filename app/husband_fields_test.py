import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEMPLATE = (ROOT / "templates" / "form_step1.html").read_text(encoding="utf-8")
SOURCE = (ROOT / "app.py").read_text(encoding="utf-8-sig")


class HusbandFieldsTests(unittest.TestCase):
    def test_backend_matrix_clears_non_applicable_values(self):
        tree = ast.parse(SOURCE)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "normalize_husband_fields"
        )
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]),
                     str(ROOT / "app.py"), "exec"), namespace)
        normalize = namespace["normalize_husband_fields"]

        cases = (
            ("Male", "Unmarried", False),
            ("Male", "Married", False),
            ("Female", "Unmarried", False),
            ("Female", "Married", False),
            ("Female", "Widowed", True),
        )
        for gender, marital, applicable in cases:
            data = {
                "gender": gender,
                "marital_status": marital,
                "husband_name": "Existing name",
                "husband_employer": "Existing employer",
            }
            normalize(data)
            if applicable:
                self.assertEqual(data["husband_name"], "Existing name")
                self.assertEqual(
                    data["husband_employer"], "Existing employer"
                )
            else:
                self.assertEqual(data["husband_name"], "")
                self.assertEqual(data["husband_employer"], "")

    def test_template_updates_visibility_on_initial_load_and_changes(self):
        self.assertIn("gender.value === 'Female'", TEMPLATE)
        self.assertIn("maritalStatus.value === 'Widowed'", TEMPLATE)
        self.assertIn("gender.addEventListener('change'", TEMPLATE)
        self.assertIn("maritalStatus.addEventListener('change'", TEMPLATE)
        self.assertIn("updateHusbandFields();", TEMPLATE)

    def test_hidden_fields_are_not_required_and_are_cleared(self):
        self.assertIn("husbandName.required = applicable;", TEMPLATE)
        self.assertIn("husbandEmployer.required = applicable;", TEMPLATE)
        self.assertIn("husbandName.value = '';", TEMPLATE)
        self.assertIn("husbandEmployer.value = '';", TEMPLATE)

    def test_backend_requires_husband_fields_only_for_female_widowed(self):
        self.assertIn("def normalize_husband_fields(data):", SOURCE)
        self.assertIn('data.get("gender") != "Female"', SOURCE)
        self.assertIn('data.get("marital_status") != "Widowed"', SOURCE)
        self.assertIn('if gender == "Female" and marital == "Widowed":', SOURCE)


if __name__ == "__main__":
    unittest.main()
