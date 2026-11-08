import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('self_inspection', Path(__file__).parent / 'astrbot_plugin_companion_chat' / 'self_inspection.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class RuntimeTests(unittest.TestCase):
    def test_shape_and_unknown_resources(self):
        result = module.runtime()
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['complete'])
        self.assertEqual(result['details']['host_resources']['status'], 'unknown')
        self.assertEqual(result['details']['cpu']['status'], 'unknown')

    def test_container_is_never_called_host_scope(self):
        original = Path.exists
        with patch.object(Path, 'exists', lambda p: True if p.name == '.dockerenv' else original(p)):
            result = module.runtime()
        self.assertEqual(result['scope'], 'container')
        self.assertEqual(result['details']['host_resources']['status'], 'unknown')

    def test_exception_is_sanitized(self):
        with patch.object(module.shutil, 'disk_usage', side_effect=OSError('secret-token')):
            result = module.runtime()
        self.assertNotIn('secret-token', repr(result))
        self.assertEqual(result['errors'], ['runtime_observation_unavailable'])

if __name__ == '__main__': unittest.main()

