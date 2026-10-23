import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEMPLATE = (ROOT / "templates" / "form_step1.html").read_text(
    encoding="utf-8"
)
SOURCE = (ROOT / "app.py").read_text(encoding="utf-8-sig")


def load_husband_helpers():
    tree = ast.parse(SOURCE)
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("husband_fields_required", "normalize_husband_fields")
    ]
    namespace = {}
    exec(
        compile(ast.Module(body=functions, type_ignores=[]),
                str(ROOT / "app.py"), "exec"),
        namespace,
    )
    return namespace["husband_fields_required"], namespace["normalize_husband_fields"]


class HusbandFieldsTests(unittest.TestCase):
    def test_backend_matrix_matches_scheme_rule_and_clears_hidden_values(self):
        required, normalize = load_husband_helpers()
        cases = (
            ("Male", "Married", False),
            ("Male", "Unmarried", False),
            ("Male", "Widowed", False),
            ("Female", "Married", True),
            ("Female", "Unmarried", False),
            ("Female", "Widowed", True),
        )
        for gender, marital, applicable in cases:
            self.assertEqual(required(gender, marital), applicable)
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

    def test_frontend_rule_covers_married_and_widowed_female_applicants(self):
        self.assertIn("gender.value === 'Female'", TEMPLATE)
        self.assertIn("maritalStatus.value === 'Married'", TEMPLATE)
        self.assertIn("maritalStatus.value === 'Widowed'", TEMPLATE)
        self.assertIn("gender.addEventListener('change'", TEMPLATE)
        self.assertIn("maritalStatus.addEventListener('change'", TEMPLATE)
        self.assertIn("updateHusbandFields();", TEMPLATE)

    def test_hidden_fields_are_not_required_and_are_cleared(self):
        self.assertIn("husbandName.required = applicable;", TEMPLATE)
        self.assertIn("husbandEmployer.required = applicable;", TEMPLATE)
        self.assertIn("husbandName.value = '';", TEMPLATE)
        self.assertIn("husbandEmployer.value = '';", TEMPLATE)

    def test_backend_validation_uses_shared_rule(self):
        self.assertIn("def husband_fields_required(gender, marital_status):",
                      SOURCE)
        self.assertIn("if husband_fields_required(gender, marital):", SOURCE)
        self.assertIn(
            'marital_status in ("Married", "Widowed")',
            SOURCE,
        )

    def test_switching_to_hidden_combination_clears_values(self):
        _, normalize = load_husband_helpers()
        data = {
            "gender": "Female",
            "marital_status": "Married",
            "husband_name": "Existing name",
            "husband_employer": "Existing employer",
        }
        normalize(data)
        data["marital_status"] = "Unmarried"
        normalize(data)
        self.assertEqual(data["husband_name"], "")
        self.assertEqual(data["husband_employer"], "")


if __name__ == "__main__":
    unittest.main()
