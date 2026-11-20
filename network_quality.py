"""Research quality indicators and transparent, versioned compatibility rubric."""
import math
import re
from collections import Counter, defaultdict
import xml.etree.ElementTree as ET

# Offline baseline only; the CLI supplies the installed conversion typemap when available.
DRIVING = set('motorway motorway_link trunk trunk_link primary primary_link secondary secondary_link tertiary tertiary_link unclassified residential living_street service track unsurfaced road'.split())
KNOWN = DRIVING | set('footway pedestrian path bridleway cycleway step steps stairs bus_guideway busway raceway ford construction'.split())


def permitted(lane, vehicle_class):
    allow = lane.get('allow')
    disallow = lane.get('disallow', '').split()
    return (allow is None or vehicle_class in allow.split() or 'all' in allow.split()) and vehicle_class not in disallow and 'all' not in disallow


def score(checks, provisional=False):
    value = round(sum(c['earned'] for c in checks), 1)
    evaluated = sum(c['weight'] for c in checks if c['status'] != 'NOT_CHECKED')
    pending = sum(c['weight'] for c in checks if c['status'] == 'NOT_CHECKED')
    routing_pending = sum(c['weight'] for c in checks if c['name'] == 'Routing feasibility' and c['status'] == 'NOT_CHECKED')
    failed = any(c['status'] == 'FAIL' for c in checks)
    final = None if pending or provisional else value
    status = 'FAIL' if failed else 'PENDING' if final is None else 'PASS' if final >= 80 else 'WARNING'
    pre_checks = [c for c in checks if c['name'] != 'Routing feasibility']
    pre_status = ('FAIL' if any(c['status'] == 'FAIL' for c in pre_checks)
                  else 'PENDING' if any(c['status'] == 'NOT_CHECKED' and c['weight'] for c in pre_checks)
                  else 'PASS_WITH_WARNINGS' if any(c['status'] == 'WARNING' for c in pre_checks)
                  else 'PASS')
    final_status = 'PENDING' if final is None else status
    return dict(pre_conversion_status=pre_status, final_status=final_status, rubric_version='1.2', score=value, evaluated_score=value, maximum=100,
                evaluated_weight=evaluated, normalized_score=round(value / evaluated * 100, 1) if evaluated else None,
                pending_weight=pending, routing_weight_pending=routing_pending, final_score=final,
                provisional=provisional or bool(pending), checks=checks, status=status,
                interpretation='Diagnostic rubric, not an empirical probability or SUMO certification.')


def check(name, weight, fraction, reason, fail=False, info=False):
    status = 'NOT_CHECKED' if fraction is None else 'FAIL' if fail else 'INFO' if info else 'PASS' if fraction == 1 else 'WARNING'
    earned = round(weight * max(0, min(1, fraction or 0)), 2)
    return dict(name=name, status=status, weight=weight, earned=earned,
                deduction=round(weight-earned, 2), reason=reason)


def speed_value(raw):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kph|knots)?\s*', raw or '')
    if not match:
        return None
    return float(match[1]) * {'mph': 1.609344, 'knots': 1.852}.get(match[2], 1)


def crosses(a, b, c, d):
    def orient(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    return orient(a,b,c)*orient(a,b,d) < 0 and orient(c,d,a)*orient(c,d,b) < 0


def osm_quality(root, report, components, vehicle_class='passenger', typemap=None):
    node_elements = root.findall('node')
    ways = [w for w in root.findall('way') if w.find("tag[@k='highway']") is not None]
    nodes = {n.get('id'): n for n in node_elements}
    coords, same_coords = {}, defaultdict(list)
    for key, node in nodes.items():
        try:
            lat, lon = float(node.get('lat')), float(node.get('lon'))
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                coords[key] = (lon, lat)
                same_coords[(lon, lat)].append(key)
        except (TypeError, ValueError):
            pass
    mapping = None
    if typemap:
        mapping = {t.get('id')[8:]: t for t in ET.parse(typemap).getroot().findall('type') if t.get('id', '').startswith('highway.')}
    details = defaultdict(list)
    geometry = defaultdict(list)
    lanes, speeds, directions = Counter(), Counter(), Counter()
    adjacency = defaultdict(set)
    drive_refs = []
    used_all = {n.get('ref') for w in root.findall('way') for n in w.findall('nd')}
    used_roads = set()
    for way in ways:
        key = way.get('id')
        tags = {t.get('k'): t.get('v', '') for t in way.findall('tag')}
        refs = [n.get('ref') for n in way.findall('nd')]
        used_roads.update(refs)
        road = tags['highway']
        mapped = mapping.get(road) if mapping is not None else None
        if road not in (mapping if mapping is not None else KNOWN):
            details['unsupported_highway_types'].append(key)
        if mapping is not None:
            drive = mapped is not None and mapped.get('discard') != 'true' and permitted(mapped, vehicle_class)
        else:
            drive = road in DRIVING if vehicle_class == 'passenger' else False
        access_keys = ['access', 'vehicle']
        if vehicle_class not in ('pedestrian', 'bicycle'):
            access_keys += ['motor_vehicle']
        access_keys += [{'passenger': 'motorcar', 'pedestrian': 'foot'}.get(vehicle_class, vehicle_class)]
        for access_key in access_keys:
            if tags.get(access_key) in ('no', 'private'):
                drive = False
            elif tags.get(access_key) in ('yes', 'designated', 'permissive', 'destination', 'delivery'):
                drive = road != 'construction'
        details['drivable_ways' if drive else 'non_drivable_ways'].append(key)
        direction = tags.get('oneway', '')
        if not direction:
            direction = 'yes' if tags.get('junction') == 'roundabout' or road in ('motorway', 'motorway_link') else 'no'
        directions[direction] += 1
        if direction in ('yes', 'true', '1', '-1'):
            details['one_way_roads'].append(key)
        elif direction not in ('no', 'false', '0', 'reversible', 'alternating'):
            details['suspicious_attributes'].append({'way': key, 'attribute': 'oneway', 'value': direction})
        for attr in ('lanes', 'lanes:forward', 'lanes:backward'):
            if attr in tags:
                value = tags[attr]
                if attr == 'lanes':
                    lanes[value] += 1
                if not value.isdigit() or int(value) < (1 if attr == 'lanes' else 0) or int(value) > 20:
                    details['suspicious_attributes'].append({'way': key, 'attribute': attr, 'value': value})
        if all(tags.get(k, '').isdigit() for k in ('lanes', 'lanes:forward', 'lanes:backward')):
            if int(tags['lanes:forward']) + int(tags['lanes:backward']) > int(tags['lanes']):
                details['suspicious_attributes'].append({'way': key, 'attribute': 'lanes', 'value': 'directional sum exceeds total'})
        for attr in ('maxspeed', 'maxspeed:forward', 'maxspeed:backward'):
            if attr in tags:
                raw = tags[attr]
                value = speed_value(raw)
                if attr == 'maxspeed':
                    speeds[raw] += 1
                if value is None:
                    details['non_numeric_speed_values'].append({'way': key, 'attribute': attr, 'value': raw})
                    if raw not in ('none', 'signals', 'walk', 'variable', 'national', 'urban', 'rural') and not re.fullmatch(r'[A-Za-z]{2}:[A-Za-z0-9:_-]+', raw):
                        details['suspicious_attributes'].append({'way': key, 'attribute': attr, 'value': raw})
                elif value <= 0 or value > 200:
                    details['suspicious_attributes'].append({'way': key, 'attribute': attr, 'value': raw})
        if drive:
            for attr in ('lanes', 'maxspeed'):
                if not tags.get(attr):
                    details['ways_missing_' + attr].append(key)
        if any('conditional' in k for k in tags):
            details['conditional_attribute_ways'].append(key)
        if refs:
            geometry[min(tuple(refs), tuple(reversed(refs)))].append(key)
        if key in report['invalid_ways']:
            continue
        if drive:
            drive_refs.append((key, refs))
            for a, b in zip(refs, refs[1:]):
                if a != b:
                    adjacency[a].add(b)
                    adjacency[b].add(a)
        if any(a == b or coords[a] == coords[b] for a, b in zip(refs, refs[1:])):
            details['zero_length_segment_ways'].append(key)
        core = refs[:-1] if refs[0] == refs[-1] else refs
        if len(core) != len(set(core)):
            details['repeated_node_ways'].append(key)
        # Proper crossings within a single way, excluding adjacent segments.
        # Cap quadratic work and explicitly report omitted long geometries.
        if len(refs) > 2000:
            details['self_intersection_unchecked_ways'].append(key)
        else:
            segments = list(zip(refs, refs[1:]))
            found = False
            for i, (a, b) in enumerate(segments):
                for c, d in segments[i+2:]:
                    if len({a,b,c,d}) == 4 and crosses(coords[a], coords[b], coords[c], coords[d]):
                        found = True
                        break
                if found:
                    break
            if found:
                details['self_intersecting_ways'].append(key)
    groups = components(adjacency, [(a,b) for a in adjacency for b in adjacency[a]])
    details['dead_end_roads'] = [key for key, refs in drive_refs if len(adjacency[refs[0]]) == 1 or len(adjacency[refs[-1]]) == 1]
    details['isolated_nodes'] = sorted(set(nodes)-used_all)
    details['nodes_outside_road_network'] = sorted(set(nodes)-used_roads)
    details['duplicate_node_ids'] = [key for key, n in Counter(n.get('id') for n in node_elements).items() if n > 1]
    details['duplicate_way_ids'] = [key for key, n in Counter(w.get('id') for w in root.findall('way')).items() if n > 1]
    details['coincident_node_groups'] = [ids for ids in same_coords.values() if len(ids) > 1]
    details['duplicate_way_geometry_groups'] = [ids for ids in geometry.values() if len(ids) > 1]
    fields = ('drivable_ways non_drivable_ways one_way_roads dead_end_roads isolated_nodes nodes_outside_road_network duplicate_node_ids duplicate_way_ids coincident_node_groups duplicate_way_geometry_groups self_intersecting_ways self_intersection_unchecked_ways repeated_node_ways zero_length_segment_ways ways_missing_maxspeed ways_missing_lanes unsupported_highway_types suspicious_attributes non_numeric_speed_values conditional_attribute_ways').split()
    for field in fields:
        report[field] = len(details[field])
    report.update(node_elements=len(node_elements), total_ways=len(root.findall('way')), quality_details=dict(details), lane_count_distribution=dict(lanes), speed_limit_distribution=dict(speeds),
                  oneway_distribution=dict(directions), drivable_components=len(groups),
                  largest_drivable_component_percent=round(100*len(groups[0])/len(adjacency), 2) if groups else 0,
                  vehicle_class=vehicle_class, mapping_source=str(typemap) if typemap else 'offline baseline; mapping and non-passenger permissions unverified')
    total = max(1, len(ways))
    drive_total = max(1, report['drivable_ways'])
    topology_bad = len(report['invalid_ways']) + report['duplicate_node_ids'] + report['duplicate_way_ids']
    topology_suspect = report['self_intersecting_ways'] + report['zero_length_segment_ways'] + report['duplicate_way_geometry_groups']
    largest = report['largest_drivable_component_percent']/100
    checks = [
        check('Topology', 30, 0 if topology_bad or not ways else max(0, 1-topology_suspect/total), f'{topology_bad} invalid ways/duplicate IDs; {topology_suspect} geometry findings.', fail=bool(topology_bad or not ways)),
        check('Road connectivity', 25, largest, f'{len(groups)} drivable weak components; largest covers {largest:.1%} of drivable nodes.', fail=not groups or largest < .5),
        check('Vehicle permissions', 15, (1 if drive_refs else 0) if mapping is not None or vehicle_class == 'passenger' else None, f'{len(drive_refs)} valid ways estimated usable for {vehicle_class}; conditional access and turn restrictions are not resolved.', fail=not drive_refs),
        check('Road-type mapping', 10, 1-report['unsupported_highway_types']/total if mapping is not None else None, f'{report["unsupported_highway_types"]} ways absent from mapping; source: {report["mapping_source"]}.'),
        check('Optional attributes', 5, max(0, 1-(report['ways_missing_maxspeed']+report['ways_missing_lanes'])/(2*drive_total)-report['suspicious_attributes']/total), 'Missing maxspeed/lanes each cost up to 2.5 points in proportion to drivable ways; suspicious attributes add proportional deductions.'),
        check('Traffic-signal information', 0, 1, f'{report["traffic_signal_nodes"]} tagged signal nodes; absence alone is not an error.', info=True),
        check('Routing feasibility', 15, None, 'Requires converted lane connections; no pre-conversion routing PASS is claimed.'),
    ]
    report['compatibility'] = score(checks, provisional=True)
    report['status'] = report['compatibility']['status']
    report['pre_conversion_status'] = report['compatibility']['pre_conversion_status']
    report['final_status'] = report['compatibility']['final_status']
    for field in ('suspicious_attributes', 'self_intersecting_ways', 'duplicate_node_ids', 'duplicate_way_ids', 'unsupported_highway_types', 'self_intersection_unchecked_ways'):
        if report[field]:
            report['warnings'].append(f'{report[field]} {field.replace("_", " ")}; see quality_details.')
