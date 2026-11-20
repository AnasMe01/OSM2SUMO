import tempfile
import unittest
from pathlib import Path
from network_analysis import analyze_osm, analyze_network

class AnalysisTests(unittest.TestCase):
    def test_repair_preserves_source_and_disconnected_roads(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.osm'
            target = Path(directory) / 'fixed.osm'
            xml = '<osm><node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/><way id="good"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way><way id="bad"><nd ref="1"/><nd ref="missing"/><tag k="highway" v="primary"/></way></osm>'
            source.write_text(xml)
            report = analyze_osm(source, target)
            self.assertEqual(report['invalid_ways'], ['bad'])
            self.assertEqual(report['removed_invalid_ways'], 1)
            self.assertEqual(source.read_text(), xml)
            self.assertEqual(analyze_osm(target)['road_ways'], 1)

    def test_permissions_components_and_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'net.xml'
            path.write_text('<net><junction id="a"/><junction id="b"/><edge id="1" from="a" to="b"><lane length="10" speed="10"/></edge><edge id="2" from="b" to="a"><lane length="10" speed="10" allow="bus"/></edge><edge id="3" from="b" to="a"><lane length="10" speed="10"/></edge><connection from="1" to="2" tl="missing" linkIndex="0"/></net>')
            report = analyze_network(path)
            self.assertEqual(report['forbidden_edges'], ['2'])
            self.assertEqual(report['components'], 2)
            self.assertEqual(report['disconnected_edges'], 1)
            self.assertEqual(len(report['tls_issues']), 1)


class RichQualityTests(unittest.TestCase):
    def osm(self, xml):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'map.osm'
            source.write_text(xml)
            return analyze_osm(source)

    def test_attributes_duplicates_and_isolation(self):
        report = self.osm('''<osm>
          <node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="1"/>
          <node id="3" lat="0" lon="1"/><node id="4" lat="8" lon="8"/>
          <way id="a"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/><tag k="oneway" v="-1"/><tag k="lanes" v="2"/><tag k="maxspeed" v="30 mph"/></way>
          <way id="b"><nd ref="2"/><nd ref="1"/><tag k="highway" v="footway"/></way>
          <way id="c"><nd ref="1"/><nd ref="3"/><tag k="highway" v="residential"/><tag k="lanes" v="-1"/><tag k="maxspeed" v="999"/></way>
        </osm>''')
        self.assertEqual(report['drivable_ways'], 2)
        self.assertEqual(report['non_drivable_ways'], 1)
        self.assertEqual(report['one_way_roads'], 1)
        self.assertEqual(report['isolated_nodes'], 1)
        self.assertEqual(report['coincident_node_groups'], 1)
        self.assertEqual(report['duplicate_way_geometry_groups'], 1)
        self.assertEqual(report['suspicious_attributes'], 2)
        self.assertEqual(report['speed_limit_distribution']['30 mph'], 1)
        self.assertEqual(report['ways_missing_maxspeed'], 0)

    def test_crossing_and_closed_ring(self):
        nodes = '<node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/><node id="3" lat="1" lon="0"/><node id="4" lat="0" lon="1"/>'
        crossing = self.osm('<osm>'+nodes+'<way id="x"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><tag k="highway" v="residential"/></way></osm>')
        ring = self.osm('<osm>'+nodes+'<way id="r"><nd ref="1"/><nd ref="3"/><nd ref="2"/><nd ref="4"/><nd ref="1"/><tag k="highway" v="residential"/><tag k="junction" v="roundabout"/></way></osm>')
        self.assertEqual(crossing['self_intersecting_ways'], 1)
        self.assertEqual(ring['self_intersecting_ways'], 0)
        self.assertEqual(ring['repeated_node_ways'], 0)
        self.assertEqual(ring['dead_end_roads'], 0)
        self.assertEqual(ring['one_way_roads'], 1)

    def test_critical_failure_and_no_routing_credit(self):
        report = self.osm('<osm><node id="1" lat="0" lon="0"/><way id="x"><nd ref="1"/><nd ref="missing"/><tag k="highway" v="residential"/></way></osm>')
        compatibility = report['compatibility']
        checks = {c['name']: c for c in compatibility['checks']}
        self.assertEqual(checks['Topology']['status'], 'FAIL')
        self.assertEqual(checks['Routing feasibility']['earned'], 0)
        self.assertTrue(report['status'] == 'FAIL')
        self.assertEqual(compatibility['score'], round(sum(c['earned'] for c in compatibility['checks']), 1))

    def test_directed_graph_and_sampling(self):
        from network_analysis import directed_reachability
        chain = directed_reachability({'a', 'b', 'c'}, [('a', 'b'), ('b', 'c')])
        cycle = directed_reachability({'a', 'b', 'c'}, [('a', 'b'), ('b', 'c'), ('c', 'a')])
        self.assertEqual(chain['reachable_pair_fraction'], .5)
        self.assertEqual(cycle['reachable_pair_fraction'], 1)
        self.assertTrue(directed_reachability(set(map(str, range(129))), [])['sampled'])

    def test_lane_permission_blocks_false_route(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'net.xml'
            path.write_text('''<net><junction id="a"/><junction id="b"/>
              <edge id="1" from="a" to="b"><lane index="0" length="10" speed="10" allow="bus"/><lane index="1" length="10" speed="10"/></edge>
              <edge id="2" from="b" to="a"><lane index="0" length="10" speed="10"/></edge>
              <connection from="1" to="2" fromLane="0" toLane="0"/></net>''')
            report = analyze_network(path)
            self.assertEqual(report['routing']['reachable_pairs'], 0)
            self.assertTrue(report['status'] == 'FAIL')

    def test_typemap_permissions_and_specific_access(self):
        with tempfile.TemporaryDirectory() as directory:
            source, mapping = Path(directory)/'map.osm', Path(directory)/'types.xml'
            mapping.write_text('<types><type id="highway.service" allow="delivery"/></types>')
            source.write_text('''<osm><node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/>
              <way id="a"><nd ref="1"/><nd ref="2"/><tag k="highway" v="service"/></way>
              <way id="b"><nd ref="1"/><nd ref="2"/><tag k="highway" v="service"/><tag k="access" v="no"/><tag k="motorcar" v="yes"/></way></osm>''')
            report = analyze_osm(source, typemap=mapping)
            self.assertEqual(report['drivable_ways'], 1)
            self.assertEqual(report['quality_details']['drivable_ways'], ['b'])
            self.assertEqual(report['unsupported_highway_types'], 0)

    def test_optional_missing_data_costs_less_than_invalid_topology(self):
        nodes = '<node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/>'
        base = '<way id="a"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/>{}</way>'
        complete = self.osm('<osm>'+nodes+base.format('<tag k="lanes" v="2"/><tag k="maxspeed" v="50"/>')+'</osm>')
        missing = self.osm('<osm>'+nodes+base.format('')+'</osm>')
        invalid = self.osm('<osm>'+nodes+base.format('').replace('ref="2"', 'ref="missing"')+'</osm>')
        self.assertEqual(complete['compatibility']['score'] - missing['compatibility']['score'], 5)
        self.assertGreater(missing['compatibility']['score'] - invalid['compatibility']['score'], 25)

    def test_empty_network_never_passes(self):
        report = self.osm('<osm/>')
        self.assertTrue(report['status'] == 'FAIL')
        self.assertLess(report['compatibility']['score'], 20)

    def test_pending_and_final_score_presentation(self):
        from network_quality import check, score
        from network_analysis import save_report
        import contextlib
        import io
        checks = [check('Evaluated checks', 85, 81.1/85, 'Fixture'),
                  check('Routing feasibility', 15, None, 'Pending')]
        pending = score(checks, provisional=True)
        self.assertEqual(pending['evaluated_score'], 81.1)
        self.assertEqual(pending['evaluated_weight'], 85)
        self.assertEqual(pending['normalized_score'], 95.4)
        self.assertEqual(pending['routing_weight_pending'], 15)
        self.assertIsNone(pending['final_score'])
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            save_report(dict(stage='OSM', warnings=[], status=pending['status'], compatibility=pending), Path(directory)/'report.json')
            self.assertIn('Final score                 PENDING', output.getvalue())
            self.assertIn('81.1 / 85', output.getvalue())
        checks[1] = check('Routing feasibility', 15, 14.4/15, 'Verified fixture')
        final = score(checks)
        self.assertEqual(final['final_score'], 95.5)
        self.assertEqual(final['status'], 'PASS')
        self.assertEqual(final['pending_weight'], 0)

    def test_zero_evaluated_weight_is_not_normalized(self):
        from network_quality import check, score
        report = score([check('Routing feasibility', 100, None, 'Pending')])
        self.assertIsNone(report['normalized_score'])
        self.assertIsNone(report['final_score'])

    def test_post_conversion_keeps_fifteen_point_routing(self):
        from network_quality import check
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'net.xml'
            path.write_text('<net><junction id="a"/><junction id="b"/><edge id="1" from="a" to="b"><lane length="10" speed="10"/></edge><edge id="2" from="b" to="a"><lane length="10" speed="10"/></edge><connection from="1" to="2"/><connection from="2" to="1"/></net>')
            standalone = analyze_network(path)['compatibility']
            self.assertIsNone(standalone['final_score'])
            source = dict(vehicle_class='passenger', compatibility=dict(checks=[check('Road-type mapping', 10, 1, 'Mapped'), check('Optional attributes', 5, .2, 'Missing data')]))
            combined = analyze_network(path, osm_report=source)['compatibility']
            self.assertEqual(combined['final_score'], 96)
            self.assertEqual(sum(c['weight'] for c in combined['checks']), 100)
            self.assertEqual(next(c['weight'] for c in combined['checks'] if c['name'] == 'Routing feasibility'), 15)

    def test_selective_repairs_and_inference(self):
        from network_repair import repair_osm, build_repair_plan
        with tempfile.TemporaryDirectory() as directory:
            source, target, mapping = Path(directory)/'input.osm', Path(directory)/'fixed.osm', Path(directory)/'types.xml'
            mapping.write_text('<types><type id="highway.residential" speed="10" numLanes="1" oneway="false"/></types>')
            xml = '<osm><node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/><node id="3" lat="2" lon="2"/><node id="4" lat="3" lon="3"><tag k="amenity" v="bench"/></node><way id="a"><nd ref="1"/><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>'
            source.write_text(xml)
            before = analyze_osm(source, typemap=mapping)
            plan = build_repair_plan(source, before, mapping)
            self.assertEqual(len(plan['isolated-nodes']), 1)
            self.assertEqual(plan['lanes'][0]['value'], '2')
            self.assertEqual(plan['speeds'][0]['value'], '36')
            audit = repair_osm(source, target, ['speeds'], before, mapping)
            self.assertEqual(audit['modified_ways'], 1)
            after = analyze_osm(target, typemap=mapping)
            self.assertEqual(after['ways_missing_maxspeed'], 0)
            self.assertEqual(after['ways_missing_lanes'], 1)
            self.assertEqual(after['zero_length_segment_ways'], 1)
            self.assertEqual(after['nodes'], 4)
            self.assertEqual(source.read_text(), xml)
            self.assertEqual(audit['actions'][0]['rule'], 'SUMO speed in m/s multiplied by 3.6; km/h')
            self.assertEqual(repair_osm(source, target)['changes'], 0)
            with self.assertRaises(ValueError):
                repair_osm(source, source, ['speeds'])

    def test_ambiguous_findings_are_not_eligible(self):
        from network_repair import build_repair_plan
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'input.osm'
            source.write_text('<osm><node id="1" lat="0" lon="0"/><node id="1" lat="2" lon="2"/><node id="2" lat="1" lon="1"/><way id="a"><nd ref="1"/><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>')
            plan = build_repair_plan(source, analyze_osm(source))
            self.assertEqual(plan['geometry'], [])
            self.assertEqual(plan['speeds'], [])

    def test_selected_removals_preserve_largest_and_relations(self):
        from network_repair import build_repair_plan, repair_osm
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'input.osm', Path(directory)/'fixed.osm'
            nodes = ''.join(f'<node id="{i}" lat="0" lon="{i}"/>' for i in range(1,8))
            ways = ''.join(f'<way id="{a}"><nd ref="{a}"/><nd ref="{b}"/><tag k="highway" v="residential"/></way>' for a,b in [(1,2),(2,3),(4,5)])
            source.write_text('<osm>'+nodes+ways+'<relation id="r"><member type="node" ref="6"/></relation></osm>')
            report = analyze_osm(source)
            plan = build_repair_plan(source, report)
            self.assertEqual(plan['small-components'], [{'ways': ['4']}])
            self.assertEqual(plan['isolated-nodes'], [{'id':'7'}])
            audit = repair_osm(source, target, ['small-components'], report)
            self.assertEqual(audit['removed_ways'], 1)
            self.assertEqual(analyze_osm(target)['road_ways'], 2)

    def test_pending_routing_recommends_generation(self):
        from network_repair import recommendations
        report = self.osm('<osm/>')
        routing = next(r for r in recommendations(report) if r['check'] == 'Routing feasibility')
        self.assertEqual(routing['category'], 'generation')
        self.assertIn('--steps network', routing['recommendation'])

    def test_configuration_without_routes(self):
        from osm_to_sumo import write_configuration
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scenario.sumocfg'
            write_configuration(path, False, 0, 100, include_routes=False)
            root = ET.parse(path).getroot()
            self.assertIsNone(root.find('input/route-files'))
            self.assertEqual(root.find('input/net-file').get('value'), 'network.net.xml')

    def test_no_selection_means_no_repair_output(self):
        from unittest.mock import patch
        import contextlib
        import io
        import osm_to_sumo
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'map.osm'
            output = Path(directory)/'output'
            source.write_text('<osm/>')
            with patch('sys.argv', ['osm_to_sumo.py', '--osm', str(source), '--output-dir', str(output), '--repair-only']), patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(osm_to_sumo.main(), 0)
            self.assertTrue((output/'osm-analysis.json').exists())
            self.assertFalse((output/'network.repaired.osm').exists())

    def test_post_repair_status_transitions_and_audit(self):
        from network_repair import repair_and_report
        import contextlib
        import io
        import json
        with tempfile.TemporaryDirectory() as directory:
            source, mapping = Path(directory)/'input.osm', Path(directory)/'types.xml'
            mapping.write_text('<types><type id="highway.residential" speed="10" numLanes="1"/></types>')
            source.write_text('<osm><node id="1" lat="0" lon="0"/><node id="2" lat="1" lon="1"/><way id="a"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>')
            before = analyze_osm(source, typemap=mapping)
            self.assertEqual(before['pre_conversion_status'], 'PASS_WITH_WARNINGS')
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                target, speed = repair_and_report(source, directory, 'passenger', mapping, before, ['speeds'])
            self.assertIn('Post-repair Network Analysis', captured.getvalue())
            self.assertEqual(speed['pre_conversion_status'], 'PASS_WITH_WARNINGS')
            self.assertEqual(speed['final_status'], 'PENDING')
            self.assertEqual(analyze_osm(target, typemap=mapping)['ways_missing_lanes'], 1)
            audit = json.loads(Path(directory, 'network-repair.json').read_text())
            self.assertEqual(audit['comparison']['missing_maxspeed'], {'before':1, 'after':0})
            with contextlib.redirect_stdout(io.StringIO()):
                _, complete = repair_and_report(source, directory, 'passenger', mapping, before, ['speeds', 'lanes'])
            self.assertEqual(complete['pre_conversion_status'], 'PASS')
            self.assertEqual(complete['final_status'], 'PENDING')
            self.assertEqual(complete['compatibility']['normalized_score'], 100)

    def test_preconversion_failure_and_missing_evidence_statuses(self):
        from network_quality import score, check
        failed = score([check('Topology', 85, 0, 'Broken', fail=True), check('Routing feasibility', 15, None, 'Pending')], provisional=True)
        self.assertEqual(failed['pre_conversion_status'], 'FAIL')
        self.assertEqual(failed['final_status'], 'PENDING')
        unknown = score([check('Road-type mapping', 85, None, 'No mapping'), check('Routing feasibility', 15, None, 'Pending')], provisional=True)
        self.assertEqual(unknown['pre_conversion_status'], 'PENDING')


if __name__ == '__main__':
    unittest.main()
