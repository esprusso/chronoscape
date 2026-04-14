import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"


def load_app_module(data_dir: str):
    os.environ["DATA_DIR"] = data_dir
    module_name = f"chronoscape_main_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module = load_app_module(self.temp_dir.name)
        self.client = TestClient(self.module.app)
        self._clear_database()

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()
        os.environ.pop("DATA_DIR", None)

    def _clear_database(self):
        db = self.module.SessionLocal()
        try:
            db.query(self.module.Event).delete()
            db.query(self.module.Era).delete()
            db.commit()
        finally:
            db.close()

    def _create_sample_records(self):
        era_one = self.client.post(
            "/api/eras",
            json={
                "name": "School Years",
                "start_date": "2008-01-01",
                "start_date_precision": "year",
                "end_date": "2012-06-01",
                "end_date_precision": "month",
                "color_hex": "#B8C4D4",
            },
        ).json()
        era_two = self.client.post(
            "/api/eras",
            json={
                "name": "First Job",
                "start_date": "2013-01-01",
                "start_date_precision": "year",
                "end_date": "2015-12-31",
                "end_date_precision": "day",
                "color_hex": "#C4B8A8",
            },
        ).json()

        moved = self.client.post(
            "/api/events",
            json={
                "headline": "Moved to the city",
                "explanation": "I arrived with two bags and no real plan.",
                "date": "2013-09-01",
                "date_precision": "month",
                "sentiment_score": 2,
                "era_id": era_two["id"],
                "reflection_qa": {
                    "questions": ["What did the train smell like?"],
                    "answers": ["Metal, coffee, and rain."],
                },
            },
        ).json()
        graduation = self.client.post(
            "/api/events",
            json={
                "headline": "Graduation day",
                "explanation": "My family stayed late for photos outside the hall.",
                "date": "2012-01-01",
                "date_precision": "year",
                "sentiment_score": 4,
                "era_id": era_one["id"],
            },
        ).json()

        reorder = self.client.post(
            "/api/events/reorder",
            json={"ids": [graduation["id"], moved["id"]]},
        )
        self.assertEqual(reorder.status_code, 200)

    def _backup_file(self, fmt: str):
        response = self.client.get(f"/api/backup?format={fmt}")
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _restore_file(self, filename: str, content: str):
        return self.client.post(
            "/api/restore",
            files={"file": (filename, content.encode("utf-8"), "text/plain")},
        )

    def test_csv_and_markdown_round_trip_replace_existing_data(self):
        for fmt in ("csv", "md"):
            with self.subTest(format=fmt):
                self._clear_database()
                self._create_sample_records()
                backup_text = self._backup_file(fmt)

                self.client.post(
                    "/api/eras",
                    json={
                        "name": "Temporary Era",
                        "start_date": "2020-01-01",
                        "end_date": "2020-12-31",
                        "color_hex": "#A8C4B8",
                    },
                )
                self.client.post(
                    "/api/events",
                    json={
                        "headline": "Temporary memory",
                        "date": "2020-03-03",
                        "sentiment_score": -1,
                    },
                )

                restored = self._restore_file(f"backup.{fmt}", backup_text)
                self.assertEqual(restored.status_code, 200)
                self.assertEqual(restored.json()["eras_restored"], 2)
                self.assertEqual(restored.json()["events_restored"], 2)

                eras = self.client.get("/api/eras").json()
                events = self.client.get("/api/events").json()

                self.assertEqual([era["name"] for era in eras], ["School Years", "First Job"])
                self.assertEqual([event["headline"] for event in events], ["Graduation day", "Moved to the city"])
                self.assertNotIn("Temporary Era", [era["name"] for era in eras])
                self.assertNotIn("Temporary memory", [event["headline"] for event in events])

                eras_by_id = {era["id"]: era for era in eras}
                moved = next(event for event in events if event["headline"] == "Moved to the city")
                self.assertEqual(eras_by_id[moved["era_id"]]["name"], "First Job")
                self.assertEqual(
                    moved["reflection_qa"],
                    {
                        "questions": ["What did the train smell like?"],
                        "answers": ["Metal, coffee, and rain."],
                    },
                )

    def test_event_reorder_updates_persisted_sort_indexes(self):
        self._create_sample_records()

        events = self.client.get("/api/events").json()
        moved = next(event for event in events if event["headline"] == "Moved to the city")
        graduation = next(event for event in events if event["headline"] == "Graduation day")

        response = self.client.post(
            "/api/events/reorder",
            json={"ids": [moved["id"], graduation["id"]]},
        )
        self.assertEqual(response.status_code, 200)

        reordered = self.client.get("/api/events").json()
        self.assertEqual(
            [event["headline"] for event in reordered],
            ["Moved to the city", "Graduation day"],
        )
        self.assertEqual(
            [event["sort_index"] for event in reordered],
            [0, 1],
        )

    def test_restore_rejects_invalid_uploads_without_mutating_data(self):
        self._create_sample_records()
        original_events = self.client.get("/api/events").json()
        original_eras = self.client.get("/api/eras").json()

        invalid_cases = [
            (
                "backup.txt",
                "plain text",
                "Unsupported backup file type",
            ),
            (
                "backup.csv",
                "record_type,backup_version,generated_at\nmeta,2,2026-01-01T00:00:00\n",
                "Unsupported backup version",
            ),
            (
                "backup.csv",
                "record_type,backup_version,generated_at\nmystery,1,2026-01-01T00:00:00\n",
                "Unknown CSV record_type",
            ),
            (
                "backup.md",
                "# no payload here\n",
                "embedded payload block",
            ),
            (
                "backup.md",
                "<!-- CHRONOSCAPE_BACKUP_V1_BEGIN -->\n{bad json}\n<!-- CHRONOSCAPE_BACKUP_V1_END -->",
                "invalid embedded JSON",
            ),
        ]

        for filename, content, message in invalid_cases:
            with self.subTest(filename=filename):
                response = self._restore_file(filename, content)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["detail"])
                self.assertEqual(self.client.get("/api/events").json(), original_events)
                self.assertEqual(self.client.get("/api/eras").json(), original_eras)

    def test_empty_restore_stays_empty_after_restart(self):
        empty_backup = (
            "# Chronoscape Backup\n\n"
            "<!-- CHRONOSCAPE_BACKUP_V1_BEGIN -->\n"
            + json.dumps(
                {
                    "backup_version": "1",
                    "generated_at": "2026-04-13T12:00:00",
                    "eras": [],
                    "events": [],
                }
            )
            + "\n<!-- CHRONOSCAPE_BACKUP_V1_END -->\n"
        )

        response = self._restore_file("empty.md", empty_backup)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/events").json(), [])
        self.assertEqual(self.client.get("/api/eras").json(), [])

        restarted_module = load_app_module(self.temp_dir.name)
        restarted_client = TestClient(restarted_module.app)
        try:
            self.assertEqual(restarted_client.get("/api/events").json(), [])
            self.assertEqual(restarted_client.get("/api/eras").json(), [])
        finally:
            restarted_client.close()


if __name__ == "__main__":
    unittest.main()
