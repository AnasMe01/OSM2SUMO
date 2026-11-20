import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import xml.etree.ElementTree as ET

from app import create_app, execution_times
from osm_to_sumo import limit_generated_trips


OSM = b'<osm version="0.6"><node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="1"/><node id="3" lat="2" lon="2"/><way id="10"><nd ref="1"/><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>'


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.directory.name})
        self.client = self.app.test_client()
        self.client.get('/')
        with self.client.session_transaction() as session:
            self.token = session['csrf']

    def post(self, path, **data):
        return self.client.post(path, data=dict(csrf=self.token, **data), follow_redirects=True)

    def test_complete_workflow_preserves_original(self):
        self.assertIn(b'File imported', self.post('/import', osm=(io.BytesIO(OSM), 'map.osm')).data)
        self.assertIn(b'Analysis complete', self.post('/analyze').data)
        response = self.post('/repair', repairs=['geometry', 'isolated-nodes'], revision='0')
        self.assertIn(b'Click Analyze when you want updated results', response.data)
        self.assertNotIn(b'Post-repair analysis', response.data)
        self.assertEqual(self.client.get('/download/report').status_code, 404)
        audit_pdf = self.client.get('/download/audit.pdf')
        self.assertEqual(audit_pdf.mimetype, 'application/pdf')
        self.assertTrue(audit_pdf.data.startswith(b'%PDF-1.4'))
        self.assertEqual(next(Path(self.directory.name).glob('*/original.osm')).read_bytes(), OSM)
        self.assertIn(b'Re-analysis of the repaired file complete', self.post('/analyze').data)
        report_pdf = self.client.get('/download/report.pdf')
        self.assertEqual(report_pdf.mimetype, 'application/pdf')
        self.assertIn('report.pdf', report_pdf.headers['Content-Disposition'])
        self.assertTrue(report_pdf.data.startswith(b'%PDF-1.4'))
        state = json.loads(next(Path(self.directory.name).glob('*/state.json')).read_text())
        self.assertEqual(state['report']['nodes'], 2)
        self.assertIn('comparison', state['audit'])
        with self.client.get('/download/osm') as download:
            self.assertEqual(download.status_code, 200)
        self.assertIn(b'outdated', self.post('/repair', repairs=['geometry'], revision='0').data)

    def test_invalid_uploads_and_missing_state(self):
        self.assertIn(b'Import an OSM file first', self.post('/analyze').data)
        for content, name in [(b'broken', 'bad.osm'), (b'<root/>', 'bad.osm'), (OSM, 'map.txt')]:
            self.post('/import', osm=(io.BytesIO(content), name))
            self.assertEqual(self.client.get('/download/osm').status_code, 404)
        self.assertEqual(self.client.post('/analyze').status_code, 400)

    def test_analyze_has_five_second_loading_state(self):
        response = self.client.get('/')
        self.assertIn(b'id="analysis-loading"', response.data)
        self.assertIn(b'window.setTimeout', response.data)
        self.assertIn(b'5000', response.data)

    def test_download_links_use_secondary_button_style(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))
        response = self.post('/analyze')
        self.assertIn(b'action="/download/osm" method="get"><button type="submit" class="secondary"', response.data)
        self.assertIn(b'action="/download/report.pdf" method="get"><button type="submit" class="secondary"', response.data)

    def test_traffic_demand_section_is_rendered(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))
        response = self.post('/analyze')
        self.assertIn(b'3. Traffic demand', response.data)
        self.assertIn(b'name="demand"', response.data)
        self.assertIn(b'Generate Traffic', response.data)

    def test_execution_times_are_not_displayed(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))
        self.post('/analyze')
        state_path = next(Path(self.directory.name).glob('*/state.json'))
        state = json.loads(state_path.read_text())
        state['execution_times'] = {'analysis': 0.004, 'repair': 0.003, 'netconvert': 0.0, 'routing': 0.0, 'validation': 0.0, 'total': 0.007}
        state_path.write_text(json.dumps(state))

        response = self.client.get('/')
        self.assertNotIn(b'Execution-time breakdown', response.data)
        self.assertNotIn(b'0.004 s', response.data)
        self.assertNotIn(b'0.003 s', response.data)

    def test_execution_times_preserve_demand_generation_memory(self):
        times = execution_times({'routing': 0.125, 'demand_generation_average_memory_bytes': 2621440})

        self.assertEqual(times['routing'], 0.125)
        self.assertEqual(times['demand_generation_average_memory_bytes'], 2621440)
        self.assertEqual(times['total'], 0.125)

    def test_generated_trip_count_is_capped_at_requested_demand(self):
        trip_file = Path(self.directory.name) / 'trips.trips.xml'
        trip_file.write_text(
            '<routes><vType id="passenger"/>'
            '<trip id="0"/><trip id="1"/><trip id="2"/><trip id="3"/>'
            '</routes>',
            encoding='utf-8',
        )

        self.assertEqual(limit_generated_trips(trip_file, 3), 3)
        root = ET.parse(trip_file).getroot()
        self.assertEqual(len(root.findall('trip')), 3)
        self.assertEqual(root.find('vType').get('id'), 'passenger')

    def test_demand_sets_period_independently_of_profile(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))

        def generate(command, **kwargs):
            period = command[command.index('--period') + 1]
            self.assertEqual(period, '30.0')
            self.assertEqual(command[command.index('--max-trips') + 1], '1')
            output = Path(command[command.index('--output-dir') + 1])
            for name in ('network.net.xml', 'network.poly.xml', 'trips.trips.xml', 'scenario.sumocfg'):
                (output / name).write_text('<root/>')
            (output / 'network.rou.xml').write_text('<routes><vehicle id="0"/></routes>')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch('app.subprocess.run', side_effect=generate), patch('app.find_sumo_home', return_value=Path(self.directory.name)), patch('app.validate_scenario', return_value={'execution_times': {'analysis': 0.0, 'repair': 0.0, 'netconvert': 0.0, 'routing': 0.0, 'validation': 0.0}, 'validation': {'status': 'PASS', 'checks': []}}):
            self.post('/traffic', duration='30', demand='1', seed='42', profile='high', vehicle_class='passenger')

    def test_startup_clears_previous_generated_data(self):
        stale_folder = Path(self.directory.name) / 'old-upload'
        stale_folder.mkdir()
        (stale_folder / 'state.json').write_text('{}')
        (Path(self.directory.name) / 'old-report.json').write_text('{}')

        create_app({'TESTING': True, 'DATA_DIR': self.directory.name})

        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_scenario_pdf_export_replaces_old_controls(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))
        state_path = next(Path(self.directory.name).glob('*/state.json'))
        state = json.loads(state_path.read_text())
        validation = {'status': 'PENDING', 'checks': [{'name': 'Simulation startup', 'status': 'NOT_CHECKED', 'detail': 'SUMO unavailable'}]}
        state['traffic'] = {'report': {'requested': 10}, 'validation': validation,
                            'files': ['scenario-report.json', 'scenario-report.html']}
        state_path.write_text(json.dumps(state))
        output = state_path.parent / 'traffic'
        output.mkdir()
        (output / 'scenario-report.json').write_text(json.dumps({'input': {'source_name': 'map.osm'}, 'validation': validation}))
        response = self.client.get('/')
        self.assertIn(b'Export Report', response.data)
        for removed in (b'Launch SUMO-GUI', b'Export JSON Report', b'Export HTML Report', b'<pre>', b'Simulation startup'):
            self.assertNotIn(removed, response.data)
        with self.client.get('/scenario/report.pdf') as pdf:
            self.assertEqual(pdf.mimetype, 'application/pdf')
            self.assertTrue(pdf.data.startswith(b'%PDF'))
            self.assertIn('scenario-report.pdf', pdf.headers['Content-Disposition'])
        self.assertEqual(self.client.post('/scenario/launch', data={'csrf': self.token}).status_code, 404)
        self.assertEqual(self.client.get('/download/traffic/scenario-report.json').status_code, 404)
        self.assertEqual(self.client.get('/download/traffic/scenario-report.html').status_code, 404)

    def test_generation_waits_for_validate_click_and_lists_simulation_files(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))
        def generate(command, **kwargs):
            self.assertNotIn('--validate-scenario', command)
            output = Path(command[command.index('--output-dir') + 1])
            (output / 'execution-times.json').write_text(json.dumps({
                'demand_generation_average_memory_bytes': 2621440,
            }))
            for name in ('network.net.xml', 'network.poly.xml', 'trips.trips.xml', 'scenario.sumocfg'):
                (output / name).write_text('<root/>')
            (output / 'network.rou.xml').write_text('<routes><vehicle id="0"/></routes>')
            return subprocess.CompletedProcess(command, 0, '', '')
        result = {'validation': {'status': 'PASS', 'checks': []}}
        with patch('app.subprocess.run', side_effect=generate), patch('app.find_sumo_home', return_value=Path(self.directory.name)), patch('app.validate_scenario', return_value=result) as validate:
            response = self.post('/traffic', duration='30', demand='1', seed='42', vehicle_class='passenger')
            validate.assert_not_called()
            self.assertIn(b'Not validated yet', response.data)
            self.assertIn(b'Validate Scenario', response.data)
            self.assertNotIn(b'Run Simulation', response.data)
            self.assertNotIn(b'Export Report', response.data)
            self.assertIn(b'file-list simulation-files', response.data)
            self.assertIn(b'network.net.xml', response.data)
            self.assertNotIn(b'MiB', response.data)
            self.assertNotIn(b'Demand-generation average memory', response.data)
            state_path = next(Path(self.directory.name).glob('*/state.json'))
            state = json.loads(state_path.read_text())
            self.assertEqual(state['traffic']['report']['demand_generation_average_memory_bytes'], 2621440)
            for hidden in (b'analysis.json', b'network_analysis.py', b'generation.log', b'REPRODUCE.txt'):
                self.assertNotIn(hidden, response.data)
            with patch('app.subprocess.Popen') as launch:
                response = self.post('/scenario/run')
                self.assertIn(b'Validate the current scenario successfully', response.data)
                launch.assert_not_called()
            response = self.post('/validate')
            self.assertIn(b'Run Simulation', response.data)
            validate.assert_called_once()
            output = state_path.parent / 'traffic'
            gui = Path(self.directory.name) / 'sumo-gui.exe'
            with patch('app.find_tool', return_value=gui), patch('app.subprocess.Popen') as launch:
                self.post('/scenario/run')
                launch.assert_called_once_with([str(gui), '-c', str(output / 'scenario.sumocfg'), '--start'], cwd=output)
            state = json.loads(state_path.read_text())
            self.assertEqual(state['traffic']['report']['execution_times']['demand_generation_average_memory_bytes'], 2621440)

    def test_custom_composition_is_forwarded_to_generation_and_replay(self):
        self.post('/import', osm=(io.BytesIO(OSM), 'map.osm'))

        def generate(command, **kwargs):
            output = Path(command[command.index('--output-dir') + 1])
            (output / 'network.rou.xml').write_text('<routes><vehicle id="0"/></routes>')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch('app.subprocess.run', side_effect=generate) as run, patch('app.find_sumo_home', return_value=Path(self.directory.name)):
            response = self.post('/traffic', duration='30', demand='10', seed='42', vehicle_class='custom', buses='30')
            self.assertIn(b'total 100%', response.data)
            run.assert_not_called()
            response = self.post('/traffic', duration='30', demand='10', seed='42', vehicle_class='custom', buses='70', trucks='30')
            self.assertIn(b'Traffic generation completed.', response.data)

        state_path = next(Path(self.directory.name).glob('*/state.json'))
        report = json.loads(state_path.read_text())['traffic']['report']
        expected = {'passenger_cars': 0, 'buses': 70, 'trucks': 30, 'motorcycles': 0, 'emergency': 0}
        for command in (run.call_args.args[0], report['reproduction_command']):
            self.assertEqual(json.loads(command[command.index('--composition') + 1]), expected)


if __name__ == '__main__':
    unittest.main()
