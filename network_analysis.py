"""Dependency-free OSM and SUMO network diagnostics."""
import json
import math
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from network_quality import osm_quality, permitted, check, score


def components(vertices, links):
    adjacency = {v: set() for v in vertices}
    for a, b in links:
        if a in adjacency and b in adjacency:
            adjacency[a].add(b)
            adjacency[b].add(a)
    groups = []
    unseen = set(adjacency)
    while unseen:
        todo = [unseen.pop()]
        group = set(todo)
        while todo:
            for neighbor in adjacency[todo.pop()]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    group.add(neighbor)
                    todo.append(neighbor)
        groups.append(group)
    return sorted(groups, key=lambda g: (-len(g), sorted(g)))


def analyze_osm(path, repaired_path=None, vehicle_class="passenger", typemap=None):
    analysis_start = time.perf_counter()
    tree = ET.parse(path)
    root = tree.getroot()
    nodes = {n.get('id'): n for n in root.findall('node')}
    valid_nodes = set()
    for key, node in nodes.items():
        try:
            lat, lon = float(node.get('lat')), float(node.get('lon'))
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                valid_nodes.add(key)
        except (TypeError, ValueError):
            pass
    ways = [w for w in root.findall('way') if any(t.get('k') == 'highway' for t in w.findall('tag'))]
    invalid, restricted, links, used = [], [], [], set()
    types = Counter()
    for way in ways:
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        types[tags['highway']] += 1
        refs = [n.get('ref') for n in way.findall('nd')]
        if len(set(refs)) < 2 or any(r not in valid_nodes for r in refs):
            invalid.append(way.get('id'))
            if repaired_path:
                root.remove(way)
            continue
        used.update(refs)
        links.extend(zip(refs, refs[1:]))
        if any(tags.get(k) in ('no', 'private') for k in ('access', 'vehicle', 'motor_vehicle')):
            restricted.append(way.get('id'))
    groups = components(used, links)
    warnings = []
    if invalid:
        warnings.append(f'{len(invalid)} road ways have missing/invalid coordinates or fewer than two distinct nodes.')
    if len(groups) > 1:
        warnings.append(f'{len(groups)} weak road components; disconnected roads are preserved.')
    if restricted:
        warnings.append('Access restrictions detected; final vehicle permissions are checked after conversion.')
    if not ways:
        warnings.append('No highway ways found.')
    original_root = ET.fromstring(ET.tostring(root)) if not repaired_path else ET.parse(path).getroot()
    if repaired_path:
        # Remove references to removed ways, preserving relation metadata.
        for relation in root.findall('relation'):
            for member in list(relation.findall('member')):
                if member.get('type') == 'way' and member.get('ref') in invalid:
                    relation.remove(member)
        tree.write(repaired_path, encoding='utf-8', xml_declaration=True)
    report = dict(stage='OSM', nodes=len(nodes), road_ways=len(ways), road_types=dict(types),
                invalid_ways=invalid, removed_invalid_ways=len(invalid) if repaired_path else 0,
                restricted_ways=restricted, components=len(groups), component_sizes=[len(g) for g in groups],
                traffic_signal_nodes=sum(any(t.get('k') == 'highway' and t.get('v') == 'traffic_signals' for t in n.findall('tag')) for n in nodes.values()),
                warnings=warnings, status='OSM analyzed; SUMO compatibility requires conversion and validation')
    osm_quality(original_root, report, components, vehicle_class, typemap)
    report['execution_times'] = {
        'analysis': time.perf_counter() - analysis_start,
        'repair': 0.0,
        'netconvert': 0.0,
        'routing': 0.0,
        'validation': 0.0,
    }
    report['execution_times']['total'] = report['execution_times']['analysis']
    return report


def analyze_network(path, vehicle_class='passenger', osm_report=None):
    root = ET.parse(path).getroot()
    edges = {e.get('id'): e for e in root.findall('edge') if e.get('function') != 'internal'}
    junctions = {j.get('id') for j in root.findall('junction')}
    invalid, forbidden = [], []
    allowed_lanes = {}
    for key, edge in edges.items():
        lanes = edge.findall('lane')
        bad = not lanes or edge.get('from') not in junctions or edge.get('to') not in junctions
        edge_permitted = False
        for lane in lanes:
            try:
                length, speed = float(lane.get('length')), float(lane.get('speed'))
                bad |= not math.isfinite(length) or not math.isfinite(speed) or length <= 0 or speed <= 0
            except (TypeError, ValueError):
                bad = True
            lane_allowed = permitted(lane, vehicle_class)
            allowed_lanes[(key, lane.get('index', str(lanes.index(lane))))] = lane_allowed
            edge_permitted |= lane_allowed
        if bad:
            invalid.append(key)
        if not edge_permitted:
            forbidden.append(key)
    connections = root.findall('connection')
    all_edge_ids = {e.get('id') for e in root.findall('edge')}
    lane_by_id = {lane.get('id'): lane for edge in root.findall('edge') for lane in edge.findall('lane')}
    invalid_connections = [dict(c.attrib) for c in connections
                           if c.get('from') not in all_edge_ids or c.get('to') not in all_edge_ids
                           or (c.get('from') in edges and (c.get('from'), c.get('fromLane', '0')) not in allowed_lanes)
                           or (c.get('to') in edges and (c.get('to'), c.get('toLane', '0')) not in allowed_lanes)
                           or (c.get('via') and c.get('via') not in lane_by_id)]
    eligible = set(edges) - set(forbidden) - set(invalid)
    links = [(c.get('from'), c.get('to')) for c in connections
             if c.get('from') in eligible and c.get('to') in eligible
             and allowed_lanes.get((c.get('from'), c.get('fromLane', '0')), False)
             and allowed_lanes.get((c.get('to'), c.get('toLane', '0')), False)
             and (not c.get('via') or (c.get('via') in lane_by_id and permitted(lane_by_id[c.get('via')], vehicle_class)))]
    groups = components(eligible, links)
    tls = {t.get('id'): t for t in root.findall('tlLogic')}
    tls_issues = []
    for c in connections:
        if c.get('tl'):
            program = tls.get(c.get('tl'))
            try:
                index = int(c.get('linkIndex', '-1'))
                if program is None or not program.findall('phase') or index < 0 or any(index >= len(p.get('state', '')) for p in program.findall('phase')):
                    tls_issues.append(dict(c.attrib))
            except ValueError:
                tls_issues.append(dict(c.attrib))
    warnings = []
    if invalid:
        warnings.append(f'{len(invalid)} structurally invalid edges.')
    if len(groups) > 1:
        warnings.append(f'{len(groups)} weak components for {vehicle_class}; this does not guarantee directed route reachability.')
    if tls_issues:
        warnings.append(f'{len(tls_issues)} invalid traffic-light connections.')
    if not eligible:
        warnings.append(f'No usable edges for {vehicle_class}.')
    report = dict(stage='SUMO', edges=len(edges), junctions=len(junctions), traffic_lights=len(tls),
                vehicle_class=vehicle_class, forbidden_edges=forbidden, invalid_edges=invalid,
                components=len(groups), disconnected_edges=sum(len(g) for g in groups[1:]),
                isolated_components=[sorted(g) for g in groups[1:]], tls_issues=tls_issues,
                warnings=warnings, status='Checks passed (routing not guaranteed)' if not warnings else 'Review required')
    routing = directed_reachability(eligible, links)
    report['routing'] = routing
    largest = len(groups[0])/len(eligible) if groups else 0
    report['invalid_connections'] = invalid_connections
    topology_ok = bool(edges) and not invalid and not tls_issues and not invalid_connections
    source_checks = {c['name']: dict(c) for c in (osm_report or {}).get('compatibility', {}).get('checks', [])}
    if osm_report is not None and osm_report.get('vehicle_class') != vehicle_class:
        raise ValueError('OSM and SUMO reports must use the same vehicle class')
    report['compatibility'] = score([
        check('Topology', 30, 1 if topology_ok else 0, f'{len(invalid)} invalid edges; {len(tls_issues)} TLS issues; {len(invalid_connections)} invalid connections.', fail=not topology_ok),
        check('Road connectivity', 25, largest, f'{len(groups)} usable weak components; largest edge share {largest:.1%}.', fail=not eligible or largest < .5),
        check('Vehicle permissions', 15, 1 if eligible else 0, f'{len(eligible)} usable edges for {vehicle_class}; forbidden edges are not inherently errors.', fail=not eligible),
        source_checks.get('Road-type mapping', check('Road-type mapping', 10, None, 'Matching OSM analysis unavailable; source mapping remains pending.')),
        source_checks.get('Optional attributes', check('Optional attributes', 5, None, 'Matching OSM analysis unavailable; source attributes remain pending.')),
        check('Traffic-signal information', 0, 0 if tls_issues else 1, f'{len(tls)} signal controllers; {len(tls_issues)} invalid connections.', fail=bool(tls_issues), info=not tls_issues),
        check('Routing feasibility', 15, routing['reachable_pair_fraction'], routing['scope'], fail=routing['reachable_pairs'] == 0),
    ])
    if osm_report is not None:
        report['compatibility']['pre_conversion_status'] = osm_report['compatibility'].get('pre_conversion_status', report['compatibility']['pre_conversion_status'])
    report['status'] = report['compatibility']['status']
    report['pre_conversion_status'] = report['compatibility']['pre_conversion_status']
    report['final_status'] = report['compatibility']['final_status']
    return report


def directed_reachability(vertices, links):
    """Deterministic bounded all-destination traversal; no demand is assumed."""
    outgoing = {v: set() for v in vertices}
    for a, b in links:
        outgoing[a].add(b)
    ordered = sorted(vertices)
    # At most 128 origins on larger graphs. All destinations are tested per origin.
    origins = ordered if len(ordered) <= 128 else [ordered[i*len(ordered)//128] for i in range(128)]
    reached_pairs = 0
    for origin in origins:
        seen, todo = {origin}, [origin]
        while todo:
            for neighbor in outgoing[todo.pop()]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    todo.append(neighbor)
        reached_pairs += len(seen)-1
    possible = len(origins)*max(0, len(vertices)-1)
    fraction = reached_pairs/possible if possible else 0
    return dict(origins_checked=origins, sampled=len(origins) < len(vertices), reachable_pairs=reached_pairs,
                tested_pairs=possible, reachable_pair_fraction=fraction,
                scope=f'{reached_pairs}/{possible} directed edge pairs reachable from {len(origins)} deterministic origins using permitted lane connections; graph check only, not a duarouter demand test.')


def save_report(report, path):
    from network_repair import recommendations
    report["recommendations"] = recommendations(report)
    Path(path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    heading = 'Post-repair Network Analysis' if report.get('analysis_phase') == 'post_repair' else report['stage'] + ' Network Analysis'
    print('\n' + heading + '\n' + '-' * 28)
    for key, value in report.items():
        if not isinstance(value, (list, dict)) and key not in ('stage', 'status', 'pre_conversion_status', 'final_status', 'analysis_phase'):
            print(f'{key.replace("_", " "):26} {value}')
    for name in ('road_types', 'lane_count_distribution', 'speed_limit_distribution'):
        if name in report:
            print(f'{name.replace("_", " ")}: {report[name]}')
    compatibility = report.get('compatibility')
    if compatibility:
        title = 'Pre-conversion compatibility' if report['stage'] == 'OSM' else 'Post-conversion compatibility'
        print('\n' + title + '\n' + '-' * 32)
        for item in compatibility['checks']:
            print(f'{item["name"]:28} {item["status"]:12} {item["earned"]:g}/{item["weight"]}')
            print('  ' + item['reason'])
        print(f'Evaluated score             {compatibility["evaluated_score"]:g} / {compatibility["evaluated_weight"]}')
        normalized = compatibility['normalized_score']
        print('Normalized score            ' + (f'{normalized:.1f}%' if normalized is not None else 'N/A'))
        print(f'Routing weight pending      {compatibility["routing_weight_pending"]}')
        other_pending = compatibility['pending_weight'] - compatibility['routing_weight_pending']
        if other_pending:
            print(f'Other weight pending        {other_pending}')
        routing = next((c for c in compatibility['checks'] if c['name'] == 'Routing feasibility'), None)
        if routing and routing['status'] != 'NOT_CHECKED':
            print(f'Routing score               {routing["earned"]:g} / {routing["weight"]}')
        final = compatibility['final_score']
        if final is None:
            print('Final score                 PENDING')
        else:
            print(f'Final compatibility score   {final:g} / 100')
        print('Pre-conversion status       ' + compatibility['pre_conversion_status'])
        print('Final SUMO status           ' + compatibility['final_status'])
        if compatibility['routing_weight_pending']:
            print('Final compatibility pending: routing validation has not yet been performed.')
    for warning in report['warnings']:
        print('WARNING: ' + warning)
    if report['recommendations']:
        print('\nDiagnosis and recommendations')
        for item in report['recommendations']:
            print(f'- {item["check"]} [{item["status"]}]: {item["recommendation"]}')
    print(f'Report: {path}')
