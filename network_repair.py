"""Conservative OSM repairs and actionable diagnostics; never edit the source."""
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def recommendations(report):
    result = []
    guidance = {
        'Topology': ('repair', 'Select eligible repairs using --repair-only --repairs invalid-ways geometry. Review conflicting IDs, crossings and SUMO junction/TLS defects manually.'),
        'Road connectivity': ('manual', 'Inspect disconnected components and extract boundaries. Expand the OSM extract if needed; do not invent connecting roads.'),
        'Vehicle permissions': ('manual', 'Check --vehicle-class and OSM access tags. Use the intended class; do not remove valid access restrictions.'),
        'Road-type mapping': ('configuration', 'Install/configure SUMO for missing mapping evidence; review unsupported highway tags against the conversion typemap.'),
        'Optional attributes': ('manual', 'Check missing lane/speed tags against authoritative map data. Conversion may use typemap defaults; select speeds/lanes to infer eligible missing values from the installed typemap; these are assumptions, not observed data.'),
        'Routing feasibility': ('generation', 'Run --analyze --steps network --no-gui to evaluate routing. For failed/partial routes, inspect one-way directions, permitted lane connections and disconnected areas.'),
        'Traffic-signal information': ('manual', 'Inspect signal programs and link indices in netedit, then regenerate the network from corrected source data.'),
    }
    for item in report.get('compatibility', {}).get('checks', []):
        if item['status'] in ('FAIL', 'WARNING', 'NOT_CHECKED'):
            kind, action = guidance.get(item['name'], ('manual', 'Inspect the affected elements.'))
            result.append(dict(check=item['name'], status=item['status'], category=kind, evidence=item['reason'], recommendation=action))
    for key in ('duplicate_node_ids', 'duplicate_way_ids', 'zero_length_segment_ways', 'self_intersecting_ways'):
        if report.get(key):
            result.append(dict(check=key, status='WARNING', category='repair' if key != 'self_intersecting_ways' else 'manual', evidence=report.get('quality_details', {}).get(key, []), recommendation='Review eligible selections with --analyze; inspect network-repair.json after selected repairs. Conflicting IDs and ambiguous geometry require manual review.'))
    if report.get('conversion_warnings'):
        result.append(dict(check='Conversion warnings', status='WARNING', category='manual', evidence=report['conversion_warnings'], recommendation='Review netconvert.log; correct source data or conversion settings, then regenerate.'))
    return result


REPAIRS = {
    'speeds': 'Infer missing speed limits',
    'lanes': 'Infer missing lane counts',
    'isolated-nodes': 'Remove untagged, unreferenced nodes',
    'small-components': 'Remove small road components',
    'unsupported': 'Remove unsupported highway ways',
    'geometry': 'Fix consecutive repeated references',
    'invalid-ways': 'Remove structurally invalid road ways',
}


def build_repair_plan(source, report, typemap=None, component_max_ways=2):
    """Build a reviewable plan. No writes; ambiguous cases are excluded."""
    from network_analysis import components
    import math
    root = ET.parse(source).getroot()
    ways = [w for w in root.findall('way') if w.find("tag[@k='highway']") is not None]
    mapping = {} if typemap is None else {t.get('id'): t for t in ET.parse(typemap).getroot().findall('type')}
    plan = {key: [] for key in REPAIRS}
    # Never alter duplicate-ID ways or ways referencing duplicate-ID nodes.
    from collections import Counter
    duplicate_ways = {k for k,n in Counter(w.get('id') for w in root.findall('way')).items() if n > 1}
    duplicate_nodes = {k for k,n in Counter(n.get('id') for n in root.findall('node')).items() if n > 1}
    relation_ways = {m.get('ref') for r in root.findall('relation') for m in r.findall('member') if m.get('type') == 'way'}
    protected_nodes = {m.get('ref') for r in root.findall('relation') for m in r.findall('member') if m.get('type') == 'node'}
    used = {nd.get('ref') for w in root.findall('way') for nd in w.findall('nd')}
    for node in root.findall('node'):
        key = node.get('id')
        if key and key not in used | protected_nodes | duplicate_nodes and not node.findall('tag'):
            plan['isolated-nodes'].append(dict(id=key))
    valid = []
    for way in ways:
        key = way.get('id')
        refs = [nd.get('ref') for nd in way.findall('nd')]
        tags = {t.get('k'): t.get('v','') for t in way.findall('tag')}
        if key is None or key in duplicate_ways or set(refs) & duplicate_nodes:
            continue
        if key in report['invalid_ways']:
            if key not in relation_ways:
                plan['invalid-ways'].append(dict(id=key))
            continue
        valid.append((key, refs))
        typ = mapping.get('highway.' + tags['highway'])
        if mapping and typ is None and key not in relation_ways:
            plan['unsupported'].append(dict(id=key, highway=tags['highway']))
        if any(a == b for a,b in zip(refs, refs[1:])):
            plan['geometry'].append(dict(id=key))
        if typ is None or key not in report.get('quality_details', {}).get('drivable_ways', []):
            continue
        for option, attr, default in [('speeds','maxspeed','speed'), ('lanes','lanes','numLanes')]:
            # Directional/conditional values need interpretation; do not synthesize a base tag.
            if attr in tags or any(k.startswith(attr + ':') for k in tags):
                continue
            try:
                value = float(typ.get(default))
                if not math.isfinite(value) or value <= 0:
                    continue
                if option == 'speeds':
                    inferred = format(value*3.6, '.6g')
                    rule = 'SUMO speed in m/s multiplied by 3.6; km/h'
                else:
                    if not value.is_integer():
                        continue
                    direction = tags.get('oneway', '').lower()
                    if direction not in ('','yes','true','1','-1','no','false','0') or any(k.startswith('oneway:') for k in tags):
                        continue
                    oneway = direction in ('yes','true','1','-1') or (not direction and (tags.get('junction') == 'roundabout' or typ.get('oneway') == 'true'))
                    inferred = str(int(value) * (1 if oneway else 2))
                    rule = 'SUMO lanes per direction; doubled for two-way OSM total'
                plan[option].append(dict(id=key, attribute=attr, value=inferred, source=str(typemap), road_type=typ.get('id'), default=typ.get(default), rule=rule))
            except (TypeError, ValueError):
                continue
    vertices = {r for _, refs in valid for r in refs}
    groups = components(vertices, [(a,b) for _, refs in valid for a,b in zip(refs,refs[1:])])
    # Largest component always retained; ties use deterministic component ordering.
    for group in groups[1:]:
        ids = [key for key,refs in valid if refs and refs[0] in group]
        if ids and len(ids) <= component_max_ways and not set(ids) & relation_ways:
            plan['small-components'].append(dict(ways=ids))
    return plan


def show_repair_plan(plan):
    print('\nDetected / Repairable Issues\n' + '-'*40)
    for key, label in REPAIRS.items():
        unit = 'nodes' if key == 'isolated-nodes' else 'components' if key == 'small-components' else 'ways'
        print(f'[ ] {key}: {label} ? {len(plan[key])} {unit}')
    print('Counts show eligible candidates only. Ambiguous findings remain recommendations.')


def parse_repairs(value):
    selected = value.replace(',', ' ').split() if isinstance(value, str) else list(value or [])
    if selected == ['none']:
        return []
    if not set(selected) <= set(REPAIRS):
        raise ValueError('Unknown repair selection; choose: ' + ', '.join(REPAIRS) + ', or none')
    return list(dict.fromkeys(selected))


def repair_osm(source, target, selected=None, report=None, typemap=None, component_max_ways=2):
    import hashlib
    from network_analysis import analyze_osm
    source, target = Path(source), Path(target)
    selected = parse_repairs(selected)
    if source.resolve() == target.resolve():
        raise ValueError('Repair output must differ from the original OSM')
    if report is None:
        report = analyze_osm(source, typemap=typemap)
    plan = build_repair_plan(source, report, typemap, component_max_ways)
    repair_start = time.perf_counter()
    tree = ET.parse(source)
    root = tree.getroot()
    ways = {w.get('id'): w for w in root.findall('way')}
    nodes = {n.get('id'): n for n in root.findall('node')}
    actions = []
    removed = {item['id'] for key in ('unsupported','invalid-ways') if key in selected for item in plan[key]}
    if 'small-components' in selected:
        removed.update(key for item in plan['small-components'] for key in item['ways'])
    for key in sorted(removed):
        root.remove(ways[key])
        actions.append(dict(action='remove_way', way=key, selected_categories=[k for k in selected if (k in ('unsupported','invalid-ways') and any(i['id']==key for i in plan[k])) or (k=='small-components' and any(key in i['ways'] for i in plan[k]))]))
    for option in selected:
        for item in plan[option]:
            key = item.get('id')
            if option in ('speeds','lanes') and key not in removed:
                ET.SubElement(ways[key], 'tag', k=item['attribute'], v=item['value'])
                actions.append(dict(action='infer_attribute', way=key, **{k:v for k,v in item.items() if k!='id'}))
            elif option == 'geometry' and key not in removed:
                previous = None
                for nd in list(ways[key].findall('nd')):
                    if nd.get('ref') == previous:
                        ways[key].remove(nd)
                    previous = nd.get('ref')
                actions.append(dict(action='remove_consecutive_references', way=key))
            elif option == 'isolated-nodes':
                root.remove(nodes[key])
                actions.append(dict(action='remove_unreferenced_node', node=key))
    target.parent.mkdir(parents=True, exist_ok=True)
    tree.write(target, encoding='utf-8', xml_declaration=True)
    repair_time = time.perf_counter() - repair_start if actions else 0.0
    return dict(feature='Selective OSM Network Diagnosis and Repair', selected_repairs=selected,
                source=str(source.resolve()), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                output=str(target.resolve()), typemap_sha256=hashlib.sha256(Path(typemap).read_bytes()).hexdigest() if typemap else None,
                component_max_ways=component_max_ways, candidate_counts={k:len(v) for k,v in plan.items()},
                actions=actions, changes=len(actions), removed_ways=len(removed),
                removed_invalid_ways=len(removed & set(report['invalid_ways'])),
                modified_ways=len({a['way'] for a in actions if a['action'] in ('infer_attribute','remove_consecutive_references')}),
                execution_times={'repair': repair_time})


def repair_and_report(source, output, vehicle_class, typemap, before, selected=None, component_max_ways=2):
    from network_analysis import analyze_osm, save_report
    target = Path(output) / 'network.repaired.osm'
    audit = repair_osm(source, target, selected, before, typemap, component_max_ways)
    after = analyze_osm(target, vehicle_class=vehicle_class, typemap=typemap)
    after['analysis_phase'] = 'post_repair'
    after['removed_invalid_ways'] = audit['removed_invalid_ways']
    audit['before_compatibility'] = before['compatibility']
    audit['after_compatibility'] = after['compatibility']
    audit['remaining_recommendations'] = recommendations(after)
    audit['comparison'] = {
        'missing_maxspeed': {'before': before['ways_missing_maxspeed'], 'after': after['ways_missing_maxspeed']},
        'missing_lanes': {'before': before['ways_missing_lanes'], 'after': after['ways_missing_lanes']},
        'normalized_score': {'before': before['compatibility']['normalized_score'], 'after': after['compatibility']['normalized_score']},
        'pre_conversion_status': {'before': before['compatibility']['pre_conversion_status'], 'after': after['compatibility']['pre_conversion_status']},
        'final_status': {'before': before['compatibility']['final_status'], 'after': after['compatibility']['final_status']},
    }
    Path(output, 'network-repair.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
    save_report(after, Path(output, 'osm-analysis-repaired.json'))
    print('\nRepair Summary\n' + '-'*32)
    for key in audit['selected_repairs']:
        print('Selected: ' + REPAIRS[key])
    print(f'Modified ways: {audit["modified_ways"]}\nRemoved ways: {audit["removed_ways"]}')
    print(f'{"Metric":26} {"Before":22} After')
    for metric, values in audit['comparison'].items():
        print(f'{metric.replace("_", " "):26} {str(values["before"]):22} {values["after"]}')
    if after['compatibility']['pre_conversion_status'] == 'PASS_WITH_WARNINGS':
        print('Warnings remain; see the post-repair checks and recommendations above.')
    if after['compatibility']['routing_weight_pending']:
        print('Final status: PENDING ROUTING VALIDATION')
    print(f'Repair report: {Path(output, "network-repair.json")}')
    return target, after
