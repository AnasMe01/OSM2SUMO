#!/usr/bin/env python3
"""Build a runnable SUMO scenario from an OpenStreetMap extract."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.sax.saxutils import quoteattr
import xml.etree.ElementTree as ET

import psutil

from network_analysis import analyze_osm, analyze_network, save_report
from network_repair import repair_and_report, build_repair_plan, show_repair_plan, parse_repairs
from scenario_validation import validate_scenario

GENERATION_COMMANDS = []
COMPOSITION_CLASSES = {'passenger_cars': 'passenger', 'buses': 'bus', 'trucks': 'truck',
                       'motorcycles': 'motorcycle', 'emergency': 'emergency'}
DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES = 0
DEMAND_GENERATION_MEMORY_SAMPLE_COUNT = 0


def average_memory_sample_bytes(total_bytes: int, sample_count: int) -> int | None:
    if sample_count <= 0:
        return None
    return int(round(total_bytes / sample_count))


# Traffic-demand defaults (also configurable with command-line options).
SIMULATION_BEGIN = 0.0
SIMULATION_END = 900.0
TRIP_PERIOD = 1.0
VEHICLE_CLASS = "passenger"


def find_sumo_home() -> Path:
    candidates: list[Path] = []
    if os.environ.get("SUMO_HOME"):
        candidates.append(Path(os.environ["SUMO_HOME"]))
    for executable in ("netconvert", "sumo", "sumo-gui"):
        found = shutil.which(executable)
        if found:
            candidates.append(Path(found).resolve().parent.parent)
    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\Eclipse\Sumo"),
            Path(r"C:\Program Files\Eclipse\Sumo"),
            Path(r"C:\Program Files (x86)\SUMO"),
            Path(r"C:\Program Files\SUMO"),
        ]
    )
    for candidate in candidates:
        if (candidate / "bin" / executable_name("netconvert")).is_file():
            return candidate.resolve()
    raise RuntimeError("SUMO installation not found. Set SUMO_HOME or add SUMO/bin to PATH.")


def executable_name(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def find_tool(name: str, sumo_home: Path) -> Path:
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    candidate = sumo_home / "bin" / executable_name(name)
    if candidate.is_file():
        return candidate.resolve()
    raise RuntimeError(f"Required SUMO tool not found: {name}")


def print_command(command: list[str]) -> None:
    print("\n$ " + subprocess.list2cmdline(command), flush=True)


def run(
    command: list[str],
    *,
    critical: bool = True,
    log_path: Path | None = None,
    measure_demand_memory: bool = False,
) -> bool:
    global DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES, DEMAND_GENERATION_MEMORY_SAMPLE_COUNT
    print_command(command)
    if measure_demand_memory:
        process = subprocess.Popen(command)
        monitored_process = psutil.Process(process.pid)
        try:
            memory_sample = monitored_process.memory_info().rss
            DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES += memory_sample
            DEMAND_GENERATION_MEMORY_SAMPLE_COUNT += 1
        except psutil.NoSuchProcess:
            pass
        while process.poll() is None:
            time.sleep(0.05)
            if process.poll() is None:
                try:
                    memory_sample = monitored_process.memory_info().rss
                    DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES += memory_sample
                    DEMAND_GENERATION_MEMORY_SAMPLE_COUNT += 1
                except psutil.NoSuchProcess:
                    pass
        return_code = process.wait()
    else:
        result = subprocess.run(command, text=True, capture_output=log_path is not None)
        return_code = result.returncode
        if log_path is not None:
            log_path.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
            print(result.stdout or "", end="")
            print(result.stderr or "", end="", file=sys.stderr)
    GENERATION_COMMANDS.append({'command': command, 'exit_code': return_code})
    if return_code != 0:
        message = f"Command failed with exit code {return_code}: {command[0]}"
        if critical:
            raise RuntimeError(message)
        print(f"WARNING: {message}", file=sys.stderr)
        return False
    return True


def limit_generated_trips(trip_file: Path, maximum: int) -> int:
    tree = ET.parse(trip_file)
    root = tree.getroot()
    trips = root.findall("trip")
    for trip in trips[maximum:]:
        root.remove(trip)
    if len(trips) > maximum:
        tree.write(trip_file, encoding="UTF-8", xml_declaration=True)
    return min(len(trips), maximum)


def generate_trips(args, sumo_home: Path, network: Path, trips: Path) -> None:
    def generate(target, vehicle_class, period, seed, prefix=''):
        run([
            sys.executable, str(sumo_home / 'tools' / 'randomTrips.py'), '--net-file', str(network),
            '--begin', str(args.begin), '--end', str(args.end), '--period', str(period),
            '--vehicle-class', vehicle_class, '--edge-permission', vehicle_class,
            '--seed', str(seed), '--validate', '--output-trip-file', str(target), '--prefix', prefix,
        ], measure_demand_memory=True)

    if args.composition is None:
        generate(trips, args.vehicle_class, args.period, args.seed)
        if args.max_trips is not None:
            limit_generated_trips(trips, args.max_trips)
        return

    # Assign leftover vehicles to the largest fractional shares after rounding down.
    shares = {key: divmod(args.max_trips * args.composition.get(key, 0), 100) for key in COMPOSITION_CLASSES}
    remaining = args.max_trips - sum(count for count, _ in shares.values())
    extra = set(sorted(shares, key=lambda key: shares[key][1], reverse=True)[:remaining])
    combined = ET.Element('routes')
    departures = []
    with TemporaryDirectory(dir=trips.parent) as temporary:
        for index, (key, vehicle_class) in enumerate(COMPOSITION_CLASSES.items()):
            count = shares[key][0] + (key in extra)
            if not count:
                continue
            target = Path(temporary) / f'{vehicle_class}.trips.xml'
            generate(target, vehicle_class, (args.end - args.begin) / count,
                     args.seed + index, prefix=f'{vehicle_class}_')
            root = ET.parse(target).getroot()
            combined.extend(root.findall('vType'))
            departures.extend(root.findall('trip')[:count])
    combined.extend(sorted(departures, key=lambda trip: float(trip.attrib['depart'])))
    ET.ElementTree(combined).write(trips, encoding='UTF-8', xml_declaration=True)


def write_configuration(path: Path, include_polygons: bool, begin: float, end: float, include_routes: bool = True, seed: int = 42) -> None:
    additional = '        <additional-files value="network.poly.xml"/>\n' if include_polygons else ""
    route_input = '        <route-files value="network.rou.xml"/>\n' if include_routes else ""
    content = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<configuration>\n"
        "    <input>\n"
        "        <net-file value=\"network.net.xml\"/>\n"
        f"{route_input}"
        f"{additional}"
        "    </input>\n"
        "    <time>\n"
        f"        <begin value={quoteattr(str(begin))}/>\n"
        f"        <end value={quoteattr(str(end))}/>\n"
        "    </time>\n"
        f'    <random_number><seed value="{seed}"/></random_number>\n'
        "</configuration>\n"
    )
    path.write_text(content, encoding="utf-8")
    print(f"\nWrote {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, help="input .osm file; auto-detected if omitted")
    parser.add_argument("--output-dir", type=Path, default=Path("sumo_scenario"))
    parser.add_argument("--begin", type=float, default=SIMULATION_BEGIN)
    parser.add_argument("--end", type=float, default=SIMULATION_END)
    parser.add_argument("--period", type=float, default=TRIP_PERIOD)
    parser.add_argument("--max-trips", type=int, help="cap generated trips at this count")
    parser.add_argument("--vehicle-class", default=VEHICLE_CLASS)
    parser.add_argument('--composition', type=json.loads, help='vehicle percentages as a JSON object; requires --max-trips')
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-gui", action="store_true", help="generate files without opening SUMO-GUI")
    parser.add_argument("--validate-scenario", action="store_true", help="explicitly run scenario validation after generation")
    parser.add_argument("--analyze", action="store_true", help="analyze before/after conversion; ask which stages to generate when interactive")
    parser.add_argument("--analyze-only", action="store_true", help="report OSM and any existing network without generating SUMO files")
    parser.add_argument("--repair", action="store_true", help="open selective repair workflow; no repairs are selected by default")
    parser.add_argument("--repair-only", action="store_true", help="analyze, repair a copy, and report without generating SUMO files")
    parser.add_argument("--repairs", nargs="+", help="selected repair keys (e.g. speeds lanes); default none")
    parser.add_argument("--component-max-ways", type=int, default=2, help="small-component removal threshold (default 2 ways); largest retained")
    parser.add_argument("--steps", nargs="+", choices=("network", "polygons", "routes", "config", "all"), help="generation stages; omitted dependencies must already exist")
    return parser.parse_args()


def detect_osm(requested: Path | None) -> Path:
    if requested:
        path = requested.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"OSM file not found: {path}")
        return path
    matches = sorted(Path.cwd().glob("*.osm"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one .osm file, found {len(matches)}; pass --osm explicitly.")
    return matches[0].resolve()


def main() -> int:
    global DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES, DEMAND_GENERATION_MEMORY_SAMPLE_COUNT
    GENERATION_COMMANDS.clear()
    DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES = 0
    DEMAND_GENERATION_MEMORY_SAMPLE_COUNT = 0
    execution_times = {'analysis': 0.0, 'repair': 0.0, 'netconvert': 0.0, 'routing': 0.0, 'validation': 0.0}
    args = parse_args()
    if args.end <= args.begin or args.period <= 0:
        raise ValueError("--end must exceed --begin and --period must be positive")
    if args.max_trips is not None and args.max_trips <= 0:
        raise ValueError("--max-trips must be positive")
    if args.composition is not None:
        if (not isinstance(args.composition, dict) or args.composition.keys() - COMPOSITION_CLASSES.keys()
                or any(type(value) is not int or value < 0 for value in args.composition.values())
                or sum(args.composition.values()) != 100 or args.max_trips is None):
            raise ValueError('--composition requires non-negative vehicle percentages totaling 100 and --max-trips')

    osm = detect_osm(args.osm)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    analysis = args.analyze or args.analyze_only or args.repair or args.repair_only or args.repairs is not None
    if args.component_max_ways < 1:
        raise ValueError("--component-max-ways must be positive")
    if args.analyze_only and (args.repairs or args.repair_only):
        raise ValueError("--analyze-only cannot be combined with repair selections")
    if args.repair_only and args.steps:
        raise ValueError("--repair-only cannot be combined with --steps")
    if args.analyze_only and args.repair:
        raise ValueError("--repair cannot be combined with --analyze-only")
    if analysis:
        try:
            analysis_typemap = find_sumo_home() / "data" / "typemap" / "osmNetconvert.typ.xml"
            if not analysis_typemap.is_file():
                analysis_typemap = None
        except RuntimeError:
            analysis_typemap = None
        osm_report = analyze_osm(osm, vehicle_class=args.vehicle_class, typemap=analysis_typemap)
        execution_times['analysis'] = osm_report['execution_times']['analysis']
        save_report(osm_report, output / "osm-analysis.json")
        plan = build_repair_plan(osm, osm_report, analysis_typemap, args.component_max_ways)
        show_repair_plan(plan)
        selected_repairs = parse_repairs(args.repairs)
        if args.repairs is None and not args.analyze_only and sys.stdin.isatty():
            selected_repairs = parse_repairs(input("Select repairs [none]: ").strip().lower())
        wants_repair = bool(selected_repairs)
        if wants_repair:
            osm, osm_report = repair_and_report(osm, output, args.vehicle_class, analysis_typemap, osm_report, selected_repairs, args.component_max_ways)
            repair_report = json.loads((output / 'network-repair.json').read_text(encoding='utf-8'))
            execution_times['repair'] = repair_report['execution_times']['repair']
        if args.repair_only:
            return 0
    if args.analyze_only:
        existing = output / "network.net.xml"
        if existing.is_file():
            print(f"Analyzing existing network (may not correspond to input OSM): {existing}")
            save_report(analyze_network(existing, args.vehicle_class), output / "sumo-analysis.json")
        return 0
    selected = args.steps
    if analysis and selected is None and sys.stdin.isatty():
        print("\nChoose generation stages:\n[1] Network only\n[2] Network + polygons\n[3] Network + routes\n[4] Network + configuration (no traffic unless routes selected)\n[5] All\n[6] None (analysis/repair only)")
        answer = input("Generation stages [none]: ").strip().lower()
        if not answer or answer in ("none", "6"):
            return 0
        selected = {"1": ["network"], "2": ["network", "polygons"], "3": ["network", "routes"], "4": ["network", "config"], "5": ["all"]}.get(answer, answer.replace(",", " ").split())
        if not set(selected) <= {"network", "polygons", "routes", "config", "all"}:
            raise ValueError("Unknown generation stage")
    if analysis and selected is None:
        print("Analysis complete. Use --steps to select generation stages in non-interactive mode.")
        return 0
    steps = set(selected or ["all"])
    if "all" in steps:
        steps = {"network", "polygons", "routes", "config"}
    if analysis and wants_repair and "network" not in steps:
        raise ValueError("Generating from repaired OSM requires network in the selected stages")
    network = output / "network.net.xml"
    polygons = output / "network.poly.xml"
    trips = output / "trips.trips.xml"
    routes = output / "network.rou.xml"
    config = output / "scenario.sumocfg"

    if "network" not in steps and not network.is_file():
        raise FileNotFoundError("Selected stages require network.net.xml; include --steps network or use an existing output directory")
    sumo_home = find_sumo_home() if steps - {"config"} or not args.no_gui else None
    if sumo_home:
        os.environ["SUMO_HOME"] = str(sumo_home)
    print(f"Detected OSM: {osm}")
    if "network" in steps:
        netconvert_start = time.perf_counter()
        run([
            str(find_tool("netconvert", sumo_home)), "--osm-files", str(osm),
            "--type-files", str(sumo_home / "data" / "typemap" / "osmNetconvert.typ.xml"),
            "--geometry.remove", "--roundabouts.guess", "--ramps.guess",
            "--junctions.join", "--tls.guess-signals", "--tls.discard-simple",
            "--output-file", str(network),
        ], log_path=output / "netconvert.log")
        execution_times['netconvert'] = time.perf_counter() - netconvert_start
    if analysis:
        report = analyze_network(network, args.vehicle_class, osm_report if "network" in steps else None)
        if "network" in steps:
            report["conversion_warnings"] = [line for line in (output / "netconvert.log").read_text(encoding="utf-8").splitlines() if line.startswith("Warning:")]
            if report["conversion_warnings"]:
                report["warnings"].append(f'{len(report["conversion_warnings"])} netconvert warnings; see conversion_warnings and netconvert.log.')
                if report["status"] == "PASS":
                    report["status"] = "WARNING"
                report["compatibility"]["status"] = report["status"]
                if report["compatibility"]["final_score"] is not None:
                    report["final_status"] = report["status"]
                    report["compatibility"]["final_status"] = report["status"]
                report["compatibility"]["conversion_review_required"] = True
        save_report(report, output / "sumo-analysis.json")
    polygon_ok = polygons.is_file()
    if "polygons" in steps:
        polygon_ok = run([
            str(find_tool("polyconvert", sumo_home)), "--net-file", str(network), "--osm-files", str(osm),
            "--type-file", str(sumo_home / "data" / "typemap" / "osmPolyconvert.typ.xml"),
            "--output-file", str(polygons),
        ], critical=False)
        if not polygon_ok and polygons.exists():
            polygons.unlink()
    if "routes" in steps:
        generate_trips(args, sumo_home, network, trips)
        routing_start = time.perf_counter()
        run([
            str(find_tool("duarouter", sumo_home)), "--net-file", str(network), "--route-files", str(trips),
            "--output-file", str(routes),
        ], measure_demand_memory=True)
        execution_times['routing'] = time.perf_counter() - routing_start

    if "config" in steps:
        write_configuration(config, polygon_ok, args.begin, args.end, "routes" in steps or ("network" not in steps and routes.is_file()), args.seed)
        (output / 'generation-commands.json').write_text(json.dumps(GENERATION_COMMANDS, indent=2), encoding='utf-8')
    if args.validate_scenario and "config" in steps:
        try:
            sumo = find_tool('sumo', sumo_home or find_sumo_home())
        except RuntimeError:
            sumo = None
        evidence = validate_scenario(output, {'source': str(osm), 'begin': args.begin, 'end': args.end,
            'period': args.period, 'vehicle_class': args.vehicle_class, 'composition': args.composition,
            'seed': args.seed, 'steps': sorted(steps),
            'execution_times': execution_times}, sumo)
        execution_times = evidence['execution_times']
        print('Scenario validation: ' + evidence['validation']['status'])
    execution_times['total'] = sum(execution_times[key] for key in ('analysis', 'repair', 'netconvert', 'routing', 'validation'))
    execution_times['demand_generation_average_memory_bytes'] = average_memory_sample_bytes(
        DEMAND_GENERATION_MEMORY_SAMPLE_TOTAL_BYTES,
        DEMAND_GENERATION_MEMORY_SAMPLE_COUNT,
    )
    (output / 'execution-times.json').write_text(json.dumps(execution_times, indent=2), encoding='utf-8')
    print("\nSelected generation stages completed successfully.")
    if not args.no_gui and "config" in steps:
        command = [str(find_tool("sumo-gui", sumo_home)), "-c", str(config)]
        print_command(command)
        subprocess.Popen(command, cwd=output)
        print("SUMO-GUI launched. Press Play manually when ready.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
