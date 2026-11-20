"""Scenario evidence, bounded SUMO smoke runs, and portable experiment archives."""
import hashlib
import json
import platform
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
from scenario_pdf import scenario_pdf


def manifest(folder):
    return {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(Path(folder).iterdir()) if p.is_file()
            and p.name not in ('scenario-report.json', 'scenario-report.html', 'scenario-report.pdf', 'scenario.zip')}


def validate_scenario(folder, metadata, sumo=None):
    validation_start = time.perf_counter()
    folder = Path(folder).resolve()
    checks = []
    roots = {}
    def check(name, status, detail):
        checks.append(dict(name=name, status=status, detail=detail))
    command_file = folder / 'generation-commands.json'
    if command_file.exists():
        try:
            commands = json.loads(command_file.read_text(encoding='utf-8'))
            conversion = [c for c in commands if Path(c['command'][0]).stem == 'netconvert']
            check('Network conversion', 'PASS' if conversion and conversion[-1]['exit_code'] == 0 else 'NOT_CHECKED',
                  'Recorded generation command exit status; see generation-commands.json.')
        except (ValueError, KeyError, TypeError, IndexError):
            check('Network conversion', 'FAIL', 'Invalid generation command record.')
    for filename, tag, label in [('network.net.xml', 'net', 'Network XML'),
                                  ('network.poly.xml', 'additional', 'Polygon file'),
                                  ('trips.trips.xml', 'routes', 'Demand XML'),
                                  ('network.rou.xml', 'routes', 'Route XML'),
                                  ('scenario.sumocfg', 'configuration', 'SUMO configuration')]:
        try:
            root = ET.parse(folder / filename).getroot()
            if root.tag != tag:
                raise ValueError('Unexpected XML root: ' + root.tag)
            roots[filename] = root
            check(label, 'PASS', filename)
        except (OSError, ET.ParseError, ValueError) as exc:
            check(label, 'SKIP' if filename == 'network.poly.xml' and not (folder / filename).exists() else 'FAIL', str(exc))
    config = roots.get('scenario.sumocfg')
    if config is not None:
        refs = {key: config.find('input/' + key) for key in ('net-file', 'route-files', 'additional-files')}
        valid = all(refs[k] is not None and refs[k].get('value') == v for k, v in
                    [('net-file', 'network.net.xml'), ('route-files', 'network.rou.xml')])
        if refs['additional-files'] is not None:
            valid = valid and refs['additional-files'].get('value') == 'network.poly.xml' and 'network.poly.xml' in roots
        check('Configuration references', 'PASS' if valid else 'FAIL', 'Expected local scenario artifacts only.')
    routes = roots.get('network.rou.xml')
    vehicles = routes.findall('vehicle') if routes is not None else []
    check('Vehicle demand', 'PASS' if vehicles else 'FAIL', f'{len(vehicles)} routed vehicles')
    version = None
    command = None
    if sumo and not any(c['status'] == 'FAIL' for c in checks):
        try:
            version_result = subprocess.run([str(sumo), '--version'], capture_output=True, text=True, timeout=15)
            version = version_result.stdout.strip()
            begin = float(config.find('time/begin').get('value'))
            end = min(float(config.find('time/end').get('value')), begin + 60)
            summary = folder / 'validation-summary.xml'
            summary.unlink(missing_ok=True)
            command = [str(sumo), '-c', 'scenario.sumocfg', '--begin', str(begin), '--end', str(end),
                       '--seed', str(metadata.get('seed', 42)), '--route-steps', '0',
                       '--ignore-route-errors', 'false', '--summary-output', summary.name, '--no-step-log', 'true']
            result = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=120)
            (folder / 'validation.log').write_text(result.stdout + result.stderr, encoding='utf-8')
            ok = result.returncode == 0
            check('Network loads / vehicle routes / permissions', 'PASS' if ok else 'FAIL',
                  f'SUMO loaded all routes with route checking enabled; exit {result.returncode}. See validation.log.')
            inserted = max((int(s.get('inserted', '0')) for s in ET.parse(summary).getroot()), default=0) if ok and summary.exists() else 0
            check('Simulation startup', 'PASS' if ok and inserted > 0 else 'FAIL',
                  f'{inserted} vehicles inserted during {end - begin:g} seconds. This does not validate the full simulation.')
        except (OSError, subprocess.TimeoutExpired, ET.ParseError, ValueError, AttributeError) as exc:
            check('Simulation startup', 'FAIL', str(exc))
    else:
        check('Simulation startup', 'NOT_CHECKED', 'SUMO unavailable or artifact checks failed.')
    status = 'FAIL' if any(c['status'] == 'FAIL' for c in checks) else ('PENDING' if any(c['status'] == 'NOT_CHECKED' for c in checks) else 'PASS')
    execution_times = dict(metadata.get('execution_times', {}))
    execution_times['validation'] = time.perf_counter() - validation_start
    for key in ('analysis', 'repair', 'netconvert', 'routing', 'validation'):
        execution_times.setdefault(key, 0.0)
    execution_times['total'] = sum(execution_times[key] for key in ('analysis', 'repair', 'netconvert', 'routing', 'validation'))
    report = dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                  input=metadata, environment={'python': platform.python_version(), 'platform': platform.platform(),
                  'sumo': version, 'tool_version': 'scenario-framework-1'}, validation={'status': status, 'checks': checks,
                  'command': command, 'timeout_seconds': 120}, execution_times=execution_times,
                  artifacts=manifest(folder))
    (folder / 'scenario-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (folder / 'scenario-report.pdf').write_bytes(scenario_pdf(report))
    return report


def export_package(folder):
    folder = Path(folder)
    report = json.loads((folder / 'scenario-report.json').read_text(encoding='utf-8'))
    if report['validation']['status'] != 'PASS' or manifest(folder) != report['artifacts']:
        raise ValueError('Validate the current scenario successfully before exporting.')
    target = folder / 'scenario.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('network.net.xml', 'network.poly.xml', 'trips.trips.xml',
                     'network.rou.xml', 'scenario.sumocfg'):
            if name not in report['artifacts']:
                continue
            archive.write(folder / name, 'Scenario_001/' + name)
        archive.writestr('Scenario_001/README.md', '''# Run this SUMO scenario

1. Install Eclipse SUMO, including SUMO-GUI. Ensure its bin directory is on
   your PATH so that sumo-gui and sumo are available in your terminal.
2. Extract the ZIP and keep all files together in the Scenario_001 folder.
3. Open a terminal in Scenario_001 and launch the graphical simulation:

   ```sh
   sumo-gui -c scenario.sumocfg
   ```

4. Click Play in SUMO-GUI to start. Adjust the delay control to change playback
   speed. The configuration supplies the simulation start/end times and inputs.

Alternatively, open SUMO-GUI, choose File > Open Simulation, and select
scenario.sumocfg. To run without a graphical window, use:

```sh
sumo -c scenario.sumocfg
```

## Included files

- scenario.sumocfg: simulation configuration; open this file to run the scenario.
- network.net.xml: road network.
- network.rou.xml: routed vehicles used by the simulation.
- trips.trips.xml: generated origin/destination trips, included for reference.
- network.poly.xml (when present): map polygons loaded by the configuration.

Python and the framework source code are not required to run this scenario.
If a command is not found, use the full path to sumo-gui or sumo in your SUMO
installation's bin folder (quote paths containing spaces). If an input file is
missing, extract the complete ZIP again and keep the files together.
''')
    return target
