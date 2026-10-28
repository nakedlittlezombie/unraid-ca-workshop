import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import server


class RepositoryInspectionTests(unittest.TestCase):
    def test_demo_fixture_is_ready_and_has_one_docker_template(self):
        result = server.inspect_repo(server.ROOT / 'fixtures/demo-repository')
        self.assertTrue(result['connected'])
        self.assertTrue(result['ready'])
        self.assertEqual(len(result['templates']), 1)
        self.assertEqual(result['templates'][0]['repository'], 'nginx:alpine')

    def test_empty_repository_requires_a_template(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'ca_profile.xml').write_text('<CommunityApplications><Profile>Maintained repository.</Profile></CommunityApplications>')
            (root / 'LICENSE').write_text('MIT License\nCopyright 2026 Maintainer')
            result = server.inspect_repo(root)
            self.assertFalse(result['ready'])
            self.assertEqual(result['templates'], [])
            checks = {c['id']: c['ok'] for c in result['checks']}
            self.assertTrue(checks['profile-file'])
            self.assertTrue(checks['profile-content'])
            self.assertTrue(checks['license'])
            self.assertFalse(checks['templates'])

    def test_template_placeholders_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'templates').mkdir()
            (root / 'templates' / 'demo.xml').write_text('<Container><Name>demo</Name><Repository>ghcr.io/YOUR_NAME/demo</Repository></Container>')
            result = server.inspect_repo(root)
            self.assertFalse(result['templates'][0]['ok'])
            self.assertTrue(any('placeholder' in error.lower() for error in result['templates'][0]['errors']))



if __name__ == '__main__':
    unittest.main()
