"""Local Flask interface for OSM diagnosis and selective repair."""
import json
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from network_analysis import analyze_osm
from network_repair import REPAIRS, build_repair_plan, recommendations, repair_osm
from osm_to_sumo import COMPOSITION_CLASSES, find_sumo_home, find_tool
from scenario_validation import validate_scenario, export_package
from scenario_pdf import scenario_pdf
from pdf_export import structured_pdf

SIMULATION_FILES = ('network.net.xml', 'network.poly.xml', 'network.rou.xml', 'trips.trips.xml', 'scenario.sumocfg')
EXECUTION_TIME_KEYS = ('analysis', 'repair', 'netconvert', 'routing', 'validation')


def execution_times(values=None):
    result = {key: 0.0 for key in EXECUTION_TIME_KEYS}
    result.update({key: float(value) for key, value in (values or {}).items() if key in result})
    if values and values.get('demand_generation_average_memory_bytes') is not None:
        result['demand_generation_average_memory_bytes'] = int(values['demand_generation_average_memory_bytes'])
    result['total'] = sum(result[key] for key in EXECUTION_TIME_KEYS)
    return result


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secrets.token_hex(32), TEMPLATES_AUTO_RELOAD=True, MAX_CONTENT_LENGTH=100 * 1024 * 1024,
                      DATA_DIR=Path(app.instance_path) / 'uploads')
    if test_config:
        app.config.update(test_config)
    data = Path(app.config['DATA_DIR'])
    data.mkdir(parents=True, exist_ok=True)
    for stale_item in data.iterdir():
        if stale_item.is_dir():
            shutil.rmtree(stale_item)
        else:
            stale_item.unlink()
    try:
        typemap = find_sumo_home() / 'data' / 'typemap' / 'osmNetconvert.typ.xml'
        typemap = typemap if typemap.is_file() else None
    except RuntimeError:
        typemap = None

    def current():
        identifier = session.get('upload', '')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            return None, None
        folder = data / identifier
        metadata = folder / 'state.json'
        return (folder, json.loads(metadata.read_text(encoding='utf-8'))) if metadata.exists() else (None, None)

    def save(folder, state):
        (folder / 'state.json').write_text(json.dumps(state), encoding='utf-8')

    @app.before_request
    def csrf():
        session.setdefault('csrf', secrets.token_hex(32))
        if request.method == 'POST' and not secrets.compare_digest(request.form.get('csrf', ''), session['csrf']):
            abort(400, 'Invalid form token. Reload the page and try again.')

    @app.get('/')
    def index():
        folder, state = current()
        report = state.get('report') if state else None
        return render_template('index.html', state=state, report=report, repairs=REPAIRS,
                               mapping=bool(typemap), advice=recommendations(report) if report else [],
                               simulation_files=SIMULATION_FILES)

    @app.post('/import')
    def import_osm():
        upload = request.files.get('osm')
        if not upload or not upload.filename or not upload.filename.lower().endswith('.osm'):
            flash('Choose an .osm XML file.', 'error')
            return redirect(url_for('index'))
        content = upload.read()
        try:
            # OSM extracts do not require DTDs or entity declarations.
            if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper() or b'\x00' in content:
                raise ValueError('Use an OSM XML file without a DTD or entity declarations.')
            if ET.fromstring(content).tag != 'osm':
                raise ValueError('The XML root must be <osm>.')
        except (ET.ParseError, ValueError) as exc:
            flash(f'Cannot import file: {exc}', 'error')
            return redirect(url_for('index'))
        identifier = uuid.uuid4().hex
        folder = data / identifier
        folder.mkdir()
        (folder / 'original.osm').write_bytes(content)
        save(folder, dict(name=upload.filename.replace('\\', '/').split('/')[-1], active='original.osm', revision=0))
        session['upload'] = identifier
        flash('File imported. Click Analyze to see its results.', 'success')
        return redirect(url_for('index'))

    @app.post('/analyze')
    def analyze():
        folder, state = current()
        if state is None:
            flash('Import an OSM file first.', 'error')
        else:
            source = folder / state['active']
            report = analyze_osm(source, typemap=typemap)
            previous_times = state.get('execution_times', {})
            state['execution_times'] = execution_times({**previous_times, **report.get('execution_times', {})})
            before = state.pop('pre_repair_report', None)
            audit = state.get('audit')
            if before and audit:
                report['analysis_phase'] = 'post_repair'
                report['removed_invalid_ways'] = audit['removed_invalid_ways']
                audit['before_compatibility'] = before['compatibility']
                audit['after_compatibility'] = report['compatibility']
                audit['remaining_recommendations'] = recommendations(report)
                audit['comparison'] = {
                    'missing_maxspeed': {'before': before['ways_missing_maxspeed'], 'after': report['ways_missing_maxspeed']},
                    'missing_lanes': {'before': before['ways_missing_lanes'], 'after': report['ways_missing_lanes']},
                    'normalized_score': {'before': before['compatibility']['normalized_score'], 'after': report['compatibility']['normalized_score']},
                    'pre_conversion_status': {'before': before['compatibility']['pre_conversion_status'], 'after': report['compatibility']['pre_conversion_status']},
                    'final_status': {'before': before['compatibility']['final_status'], 'after': report['compatibility']['final_status']},
                }
                (folder / Path(state['active']).parent / 'network-repair.json').write_text(
                    json.dumps(audit, indent=2), encoding='utf-8')
            state['report'] = report
            state['counts'] = {k: len(v) for k, v in build_repair_plan(source, report, typemap).items()}
            save(folder, state)
            flash('Analysis complete.' if not state['revision'] else 'Re-analysis of the repaired file complete.', 'success')
        return redirect(url_for('index'))

    @app.post('/repair')
    def repair():
        folder, state = current()
        selected = request.form.getlist('repairs')
        if not state or not state.get('report'):
            flash('Analyze your file before choosing repairs.', 'error')
        elif not selected or any(key not in REPAIRS for key in selected):
            flash('Select at least one available repair.', 'error')
        elif request.form.get('revision') != str(state['revision']):
            flash('This repair form is outdated. Review the current results and try again.', 'error')
        else:
            revision = state['revision'] + 1
            output = folder / f'repair-{revision}'
            target = output / 'network.repaired.osm'
            before = state['report']
            repair_start = time.perf_counter()
            audit = repair_osm(folder / state['active'], target, selected, before, typemap)
            audit.setdefault('execution_times', {})['repair'] = audit['execution_times'].get('repair', time.perf_counter() - repair_start)
            state.setdefault('execution_times', execution_times())['repair'] = audit['execution_times']['repair']
            state['execution_times']['total'] = sum(state['execution_times'][key] for key in EXECUTION_TIME_KEYS)
            (output / 'network-repair.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
            state.update(active=str(target.relative_to(folder)), revision=revision, audit=audit,
                         pre_repair_report=before)
            state.pop('report', None)
            state.pop('counts', None)
            state.pop('traffic', None)
            save(folder, state)
            flash('Selected repairs applied to a new copy. Click Analyze when you want updated results.', 'success')
        return redirect(url_for('index'))

    @app.post('/traffic')
    def traffic():
        folder, state = current()
        try:
            if not state:
                raise ValueError('Import an OSM file first.')
            duration = int(request.form.get('duration', ''))
            requested = int(request.form.get('demand', ''))
            seed = int(request.form.get('seed', ''))
            if duration <= 0 or requested <= 0 or seed < 0:
                raise ValueError('Duration and demand must be positive, and the seed cannot be negative.')
            profile = request.form.get('profile', 'medium')
            vehicle_class = request.form.get('vehicle_class', 'passenger')
            if profile not in ('low', 'medium', 'high') or vehicle_class not in ('passenger', 'custom'):
                raise ValueError('Choose a valid traffic profile and vehicle composition.')
            composition = {key: int(request.form.get(key, '0')) for key in COMPOSITION_CLASSES} if vehicle_class == 'custom' else dict.fromkeys(COMPOSITION_CLASSES, 0)
            if vehicle_class == 'passenger':
                composition['passenger_cars'] = 100
            if any(value < 0 for value in composition.values()) or sum(composition.values()) != 100:
                raise ValueError('Vehicle composition percentages must be non-negative and total 100%.')

            period = duration / requested
            output = folder / 'traffic'
            state.pop('traffic', None)
            save(folder, state)
            if output.exists():
                shutil.rmtree(output)
            output.mkdir()
            cli_vehicle_class = 'passenger'
            composition_args = ['--composition', json.dumps(composition)] if vehicle_class == 'custom' else []
            command = [
                sys.executable, str(Path(__file__).with_name('osm_to_sumo.py')),
                '--osm', str(folder / state['active']), '--output-dir', str(output),
                '--begin', '0', '--end', str(duration), '--period', str(period),
                '--max-trips', str(requested), '--vehicle-class', cli_vehicle_class,
                '--seed', str(seed), '--steps', 'all', '--no-gui', *composition_args,
            ]
            result = subprocess.run(command, cwd=folder, text=True, capture_output=True, timeout=600)
            (output / 'generation.log').write_text(result.stdout + result.stderr, encoding='utf-8')
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()
                raise RuntimeError(detail[-1] if detail else 'SUMO traffic generation failed.')
            generated_times = execution_times()
            times_file = output / 'execution-times.json'
            if times_file.is_file():
                generated_times = execution_times(json.loads(times_file.read_text(encoding='utf-8')))

            route_file = output / 'network.rou.xml'
            valid_routes = sum(1 for element in ET.parse(route_file).getroot().findall('vehicle'))
            rejected = max(0, requested - valid_routes)
            report = {
                'profile': profile, 'duration': duration, 'requested': requested,
                'valid_routes': valid_routes, 'rejected': rejected,
                'routing_success': round(valid_routes / requested * 100, 1),
                'vehicle_class': vehicle_class, 'composition': composition, 'seed': seed,
                'effective_vehicle_class': vehicle_class, 'period': period,
                'profile_note': 'The requested demand and duration determine the insertion interval; the profile is descriptive.',
                'source_name': state['name'], 'source_revision': state['revision'], 'generation_command': command,
                'execution_times': generated_times,
                'demand_generation_average_memory_bytes': generated_times.get('demand_generation_average_memory_bytes'),
            }
            shutil.copy2(folder / 'original.osm', output / 'original.osm')
            shutil.copy2(folder / state['active'], output / 'generation-input.osm')
            if state['revision']:
                shutil.copy2(folder / state['active'], output / 'repaired.osm')
            for revision in range(1, state['revision'] + 1):
                shutil.copy2(folder / f'repair-{revision}' / 'network-repair.json', output / f'repair-report-{revision}.json')
            if state.get('report'):
                (output / 'analysis.json').write_text(json.dumps(state['report'], indent=2), encoding='utf-8')
            report['network_processing'] = {
                'analysis': state.get('report'),
                'repair_audits': [f'repair-report-{revision}.json' for revision in range(1, state['revision'] + 1)],
                'generation_input': 'generation-input.osm',
            }
            sumo_home = find_sumo_home()
            report['sumo_resources'] = {}
            for resource in (sumo_home / 'tools' / 'randomTrips.py',
                             sumo_home / 'data' / 'typemap' / 'osmNetconvert.typ.xml',
                             sumo_home / 'data' / 'typemap' / 'osmPolyconvert.typ.xml'):
                if resource.is_file():
                    shutil.copy2(resource, output / resource.name)
                    report['sumo_resources'][resource.name] = str(resource)
            for filename in ('osm_to_sumo.py', 'network_analysis.py', 'network_quality.py', 'network_repair.py', 'scenario_validation.py', 'scenario_pdf.py', 'requirements.txt'):
                shutil.copy2(Path(__file__).with_name(filename), output / filename)
            replay = ['python', 'osm_to_sumo.py', '--osm', 'generation-input.osm', '--output-dir', 'reproduced',
                      '--begin', '0', '--end', str(duration), '--period', str(period),
                      '--max-trips', str(requested), '--vehicle-class', cli_vehicle_class,
                      '--seed', str(seed), '--steps', 'all', '--no-gui', *composition_args]
            report['reproduction_command'] = replay
            (output / 'REPRODUCE.txt').write_text('Install the SUMO version recorded in scenario-report.json and set SUMO_HOME.\n'
                'Install Python requirements: python -m pip install -r requirements.txt\n'
                'From this folder run:\n' + subprocess.list2cmdline(replay) + '\n'
                'Launch the packaged scenario: sumo-gui -c scenario.sumocfg\n', encoding='utf-8')
            (output / 'traffic-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            generated = [name for name in SIMULATION_FILES if (output / name).is_file()]
            state['traffic'] = {'report': report, 'files': generated}
            save(folder, state)
            flash('Traffic generation completed.', 'success')
        except (OSError, ValueError, ET.ParseError, RuntimeError, subprocess.TimeoutExpired) as exc:
            flash(f'Could not generate traffic: {exc}', 'error')
        return redirect(url_for('index') + '#traffic')

    def run_validation(output, report):
        try:
            sumo = find_tool('sumo', find_sumo_home())
        except RuntimeError:
            sumo = None
        return validate_scenario(output, report, sumo)

    @app.post('/validate')
    def validate():
        folder, state = current()
        if not state or not state.get('traffic'):
            abort(404)
        metadata = dict(state['traffic']['report'])
        metadata['execution_times'] = execution_times(metadata.get('execution_times'))
        result = run_validation(folder / 'traffic', metadata)
        state['traffic']['validation'] = result['validation']
        state['traffic']['report']['execution_times'] = execution_times(result.get('execution_times', metadata['execution_times']))
        save(folder, state)
        return redirect(url_for('index') + '#validation')

    @app.post('/scenario/run')
    def run_simulation():
        folder, state = current()
        if not state or not state.get('traffic'):
            abort(404)
        destination = url_for('index') + '#validation'
        validation = state['traffic'].get('validation') or {}
        output = (folder / 'traffic').resolve()
        config = output / 'scenario.sumocfg'
        if validation.get('status') != 'PASS' or not config.is_file():
            flash('Validate the current scenario successfully before running it.', 'error')
            return redirect(destination)
        try:
            gui = find_tool('sumo-gui', find_sumo_home())
            subprocess.Popen([str(gui), '-c', str(config), '--start'], cwd=output)
        except (OSError, RuntimeError) as exc:
            flash(f'Could not launch SUMO-GUI: {exc}', 'error')
        else:
            flash('SUMO-GUI launched with automatic playback.', 'success')
        return redirect(destination)

    @app.post('/scenario/package')
    def package():
        folder, state = current()
        if not state or not state.get('traffic'):
            abort(404)
        try:
            path = export_package(folder / 'traffic')
        except (OSError, ValueError) as exc:
            flash(str(exc), 'error')
            return redirect(url_for('index') + '#validation')
        return send_file(path, as_attachment=True, download_name='Scenario_001.zip')

    @app.get('/scenario/report.pdf')
    def scenario_report():
        folder, state = current()
        if not state or not state.get('traffic'):
            abort(404)
        path = folder / 'traffic' / 'scenario-report.json'
        if not path.is_file():
            abort(404)
        report = json.loads(path.read_text(encoding='utf-8'))
        response = send_file(BytesIO(scenario_pdf(report)), mimetype='application/pdf',
                             as_attachment=True, download_name='scenario-report.pdf')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/download/<kind>')
    def download(kind):
        folder, state = current()
        if not state:
            abort(404)
        if kind == 'osm':
            return send_file(folder / state['active'], as_attachment=True, download_name='network.repaired.osm' if state['revision'] else state['name'])
        if kind not in ('report', 'audit') or kind not in state:
            abort(404)
        title = 'OSM Network Analysis Results' if kind == 'report' else 'OSM Network Repair Audit'
        pdf = structured_pdf(title, state['name'], state[kind])
        response = send_file(BytesIO(pdf), mimetype='application/pdf', as_attachment=True,
                             download_name=f'{kind}.pdf')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/download/<kind>.pdf')
    def download_pdf(kind):
        return download(kind)

    @app.get('/download/traffic/<path:filename>')
    def download_traffic(filename):
        if filename in ('scenario-report.json', 'scenario-report.html'):
            abort(404)
        folder, state = current()
        traffic_state = state.get('traffic') if state else None
        if not traffic_state or filename not in traffic_state['files']:
            abort(404)
        path = (folder / 'traffic' / filename).resolve()
        if path.parent != (folder / 'traffic').resolve() or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.errorhandler(413)
    def too_large(error):
        flash('The file is too large. Maximum upload size is 100 MB.', 'error')
        return redirect(url_for('index'))

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5000)
