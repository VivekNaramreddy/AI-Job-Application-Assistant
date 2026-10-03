import unittest

from src.application import ApplicationField, ApplicationState
from src.form_filler import FormFiller


class ApplicationModelTests(unittest.TestCase):
    def test_application_states_include_human_review_gate(self):
        self.assertEqual(
            ApplicationState.READY_FOR_REVIEW.value, "READY_FOR_REVIEW"
        )
        self.assertEqual(
            ApplicationState.NEEDS_USER_INPUT.value, "NEEDS_USER_INPUT"
        )

    def test_normalized_field_is_serializable(self):
        field = ApplicationField(
            key="email",
            label="Email address",
            field_type="email",
            required=True,
            current_value="candidate@example.com",
            resolved_value="candidate@example.com",
            source="candidate profile",
            confidence=1.0,
        )
        self.assertEqual(field.as_dict()["field_type"], "email")
        self.assertTrue(field.as_dict()["required"])

    def test_profile_answer_has_priority_and_does_not_infer_unknown_facts(self):
        filler = FormFiller(None)
        resolution = filler.resolve_answer(
            "Email address",
            {"email": "candidate@example.com"},
            {"years_of_experience": "7"},
        )
        self.assertEqual(resolution.value, "candidate@example.com")
        self.assertEqual(resolution.source, "candidate profile")
        self.assertEqual(
            filler.resolve_answer("Do you hold a CPA license?", {}, {}).value,
            "",
        )

    def test_state_label_does_not_match_united_states_text(self):
        filler = FormFiller(None)
        resolution = filler.resolve_answer(
            "Are you legally authorized to work in the United States?",
            {"state": "NY"},
            {"work_authorization": "Yes"},
        )
        self.assertEqual(resolution.value, "Yes")


if __name__ == "__main__":
    unittest.main()
