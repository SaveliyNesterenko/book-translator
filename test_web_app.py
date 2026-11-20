import copy
import io
import tempfile
import unittest
from pathlib import Path

import yaml
from starlette.datastructures import UploadFile

import book_translator as bt
import web_app


class WebAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_config_path = web_app.CONFIG_PATH
        self.old_sources_dir = web_app.SOURCES_DIR

        cfg = copy.deepcopy(bt.load_config(Path(__file__).with_name("config.yaml")))
        cfg.pop("_config_dir", None)
        cfg["paths"] = {"projects_dir": "projects", "output_dir": "output"}
        config_path = self.root / "config.yaml"
        config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        web_app.CONFIG_PATH = config_path
        web_app.SOURCES_DIR = self.root / "sources"

    def tearDown(self):
        web_app.CONFIG_PATH = self.old_config_path
        web_app.SOURCES_DIR = self.old_sources_dir
        self.temp.cleanup()

    async def test_create_and_list_project(self):
        upload = UploadFile(
            filename="sample.txt",
            file=io.BytesIO(b"CHAPTER 1\n\nCall me Ishmael. This is a short test paragraph."),
        )
        created = await web_app.create_project(upload, "Sample Book")
        self.assertTrue(created["name"].startswith('book-'))
        self.assertEqual(created["counts"]["pending"], created["total"])
        self.assertGreater(created["total"], 0)

        projects = web_app.list_projects()["projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["display_name"], "Sample Book")
        self.assertFalse(projects[0]["final_ready"])

    async def test_russian_title_has_independent_project_id(self):
        upload = UploadFile(
            filename="moby-dick.txt",
            file=io.BytesIO(b"CHAPTER 1\n\nCall me Ishmael. This is another short paragraph."),
        )
        created = await web_app.create_project(upload, "Моби Дик")
        self.assertTrue(created["name"].startswith('book-'))
        self.assertEqual(created["display_name"], "Моби Дик")

    async def test_archive_can_be_reversed_without_losing_source_or_versions(self):
        from web_features import chunk, edit_chunk, settings

        source = b"CHAPTER 1\n\nCall me Ishmael. This is a short test paragraph."
        upload = UploadFile(filename="sample.txt", file=io.BytesIO(source))
        name = (await web_app.create_project(upload, "Archive book"))['name']
        paths = web_app.project_paths(name)
        edit_chunk(name, 1, {'text': 'Ручной перевод.', 'expected_version': None})
        self.assertTrue(settings(name, {'archived': True})['archived'])
        self.assertFalse(settings(name, {'archived': False})['archived'])
        self.assertEqual(chunk(name, 1)['translated_text'], 'Ручной перевод.')
        self.assertEqual((paths['project_dir']/'source.txt').read_bytes(), source)

    def test_windows_launcher_is_codepage_independent(self):
        launcher = Path(__file__).with_name("Запустить переводчик.bat")
        content = launcher.read_bytes()
        self.assertTrue(content)
        self.assertTrue(all(byte < 128 for byte in content))
        self.assertIn(b".venv\\Scripts\\python.exe", content)



if __name__ == "__main__":
    unittest.main()
