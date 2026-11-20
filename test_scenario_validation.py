import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scenario_validation import validate_scenario, export_package


class ValidationTests(unittest.TestCase):
    def setUp(self):
        platform_patch = patch('scenario_validation.platform.platform', return_value='test-platform')
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name, content in {'network.net.xml': '<net/>', 'trips.trips.xml': '<routes/>',
                              'network.rou.xml': '<routes><vehicle id="0"/></routes>',
                              'scenario.sumocfg': '<configuration><input><net-file value="network.net.xml"/><route-files value="network.rou.xml"/></input><time><begin value="0"/><end value="3600"/></time></configuration>'}.items():
            (self.folder / name).write_text(content)

    def runner(self, command, **kwargs):
        if '--version' not in command:
            self.assertIn('--route-steps', command)
            (self.folder / 'validation-summary.xml').write_text('<summary><step inserted="1"/></summary>')
        return subprocess.CompletedProcess(command, 0, 'SUMO test', '')

    def test_pass_package_and_tamper_detection(self):
        for name in ('osm_to_sumo.py', 'requirements.txt', 'generation.log', 'original.osm'):
            (self.folder / name).write_text('Internal artifact; exclude from package.')
        with patch('scenario_validation.subprocess.run', side_effect=self.runner):
            report = validate_scenario(self.folder, {'seed': 7, 'source': '<script>'}, 'sumo')
        self.assertEqual(report['validation']['status'], 'PASS')
        self.assertTrue((self.folder / 'scenario-report.pdf').read_bytes().startswith(b'%PDF'))
        with zipfile.ZipFile(export_package(self.folder)) as archive:
            self.assertEqual(set(archive.namelist()), {
                'Scenario_001/' + name for name in ('network.net.xml', 'trips.trips.xml',
                'network.rou.xml', 'scenario.sumocfg', 'README.md')})
            instructions = archive.read('Scenario_001/README.md').decode('utf-8')
            self.assertIn('sumo-gui -c scenario.sumocfg', instructions)
            self.assertIn('sumo -c scenario.sumocfg', instructions)
        (self.folder / 'network.net.xml').write_text('<net changed="true"/>')
        with self.assertRaises(ValueError):
            export_package(self.folder)

    def test_missing_sumo_does_not_pass(self):
        self.assertEqual(validate_scenario(self.folder, {})['validation']['status'], 'PENDING')
        with self.assertRaises(ValueError):
            export_package(self.folder)

    def test_cli_generation_does_not_validate_by_default(self):
        import osm_to_sumo
        source = self.folder / 'map.osm'
        source.write_text('<osm/>')
        with patch('sys.argv', ['osm_to_sumo.py', '--osm', str(source), '--output-dir', str(self.folder), '--steps', 'config', '--no-gui']), patch('osm_to_sumo.validate_scenario') as validate:
            self.assertEqual(osm_to_sumo.main(), 0)
            validate.assert_not_called()

    def test_bad_config_never_executes(self):
        (self.folder / 'scenario.sumocfg').write_text('<configuration><input><net-file value="../outside.net.xml"/></input></configuration>')
        with patch('scenario_validation.subprocess.run') as run:
            self.assertEqual(validate_scenario(self.folder, {}, 'sumo')['validation']['status'], 'FAIL')
            run.assert_not_called()

    def test_failure_and_timeout_are_reported(self):
        for result in [subprocess.CompletedProcess([], 1, '', 'broken route'), subprocess.TimeoutExpired('sumo', 120)]:
            with patch('scenario_validation.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0, 'version', ''), result]):
                self.assertEqual(validate_scenario(self.folder, {}, 'sumo')['validation']['status'], 'FAIL')
            self.assertTrue((self.folder / 'scenario-report.json').exists())


if __name__ == '__main__':
    unittest.main()
