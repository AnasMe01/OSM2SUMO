# OSM to SUMO

## Contribution 3: scenario validation and reproducibility

Traffic generation saves simulation files without running scenario validation.
Click **Validate Scenario** in section 4 to run the checks and create the internal
`scenario-report.json` and `scenario-report.pdf`. Section 4 provides one **Export Report** button
for the PDF, and a ZIP scenario export. The PDF contains validation and demand figures,
parameter tables, validation evidence, and an artifact inventory, without raw JSON.
Reports can be exported for failed or pending validation too. ZIP export requires a passing validation
and unchanged artifact SHA-256 hashes. Repairing the source invalidates the current
traffic result. Export before restarting the server: startup clears stored sessions.

Validation checks network, polygon (optional), demand, route, and configuration XML,
local configuration references, and nonempty vehicle demand. It runs SUMO with all
routes loaded (`--route-steps 0`) and route error checking enabled, for at most 60
simulation seconds with a 120-second wall-clock timeout. A passing startup requires
at least one inserted vehicle. Missing SUMO produces PENDING; malformed files,
failed processes, timeouts, or no inserted vehicles cannot produce PASS. This is a
startup test, not proof that every vehicle completes its trip or traffic is realistic.
See the [SUMO options](https://sumo.dlr.de/docs/sumo.html) and
[summary output definitions](https://sumo.dlr.de/docs/Simulation/Output/Summary.html).

The scenario ZIP includes only the SUMO network, optional polygons, trips, routes,
configuration, and a `README.md` with installation and simulation instructions.
Extract the ZIP, open a terminal in `Scenario_001`, and run
`sumo-gui -c scenario.sumocfg`, then click Play. For a command-line run, use
`sumo -c scenario.sumocfg`. Framework source code, logs, and reports are excluded;
the PDF report is available separately through **Export Report**.
The explicit demand now determines the trip period (duration / demand); the profile
is descriptive. Generated trips are capped at the requested count to prevent an
extra trip caused by floating-point departure increments. Unsupported mixed
composition is rejected rather than reported as generated passenger traffic.
Routing may reject some requested vehicles.

The generated-file list shows only network, polygons, routes, trips, and configuration,
in three columns (one on small screens). The Simulation startup row is omitted from
the displayed results and PDF; its underlying check still contributes to readiness.

CLI runs validate only when `--validate-scenario` is explicitly supplied. Partial-stage
or demand-free runs may correctly report FAIL/PENDING and are not certified as
ready traffic scenarios. Run tests with
`python -m unittest -q test_app.py test_network_analysis.py test_scenario_validation.py`.

## Flask interface

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. Import an `.osm` XML file, click **Analyze**, then **Repair…** to choose the repairs to apply. Available options show eligible item counts. Each repair batch creates a separate copy and displays post-repair analysis and before/after metrics. Click **Re-analyze** to check the current repaired file again or select further repairs. Download the current OSM, analysis JSON, or repair audit from the results.

The interface uses passenger-vehicle analysis and the existing repair engine. It discovers the installed SUMO typemap when available; without it, speed/lane inference and unsupported-way removal are unavailable. Conversion and routing validation remain separate CLI steps, so the interface does not report a completed SUMO compatibility score.

Uploads and repair revisions are stored under `instance/uploads/`; original files are preserved. This is a local interface bound to localhost, with a 100 MB upload limit. Browser sessions reset when the server restarts; stored files remain on disk. Run interface and engine checks with `python -m unittest -v test_app.py test_network_analysis.py`.

Analyze first, then choose stages at the terminal prompt:

```powershell
python osm_to_sumo.py --analyze
```

Choose `network`, `polygons`, `routes`, `config`, any combination, `all`, or `none`.
Without an interactive terminal, `--analyze` stops after OSM analysis unless `--steps` is supplied.

```powershell
python osm_to_sumo.py --analyze-only
python osm_to_sumo.py --analyze --steps network --no-gui
python osm_to_sumo.py --steps routes config --no-gui
python osm_to_sumo.py --repair --repairs invalid-ways geometry --steps all --no-gui
```

Use `--osm FILE` and `--output-dir DIRECTORY` to select input and output. Omitted stages require their prerequisite files to already exist in that directory. Regenerate routes and configuration after changing a network; existing files are not checked for provenance. The original invocation without these options still generates all stages.

Reports are saved as `osm-analysis.json` and `sumo-analysis.json`. Analysis-only requires no SUMO installation and also inspects an existing network when present. Conversion warnings are saved in `netconvert.log` and included in the post-conversion report.

OSM checks cover road types, invalid references/coordinates, weak connectivity, access restrictions, and signal nodes. SUMO checks cover edge/lane structure, selected vehicle permissions, weak connectivity through edge connections, junction references, and traffic-light link indices. Disconnected edge counts mean eligible edges outside the largest weak component. These checks do not prove directed reachability, signal safety, or successful routing; OSM restriction checks are advisory and routing uses permitted source, destination, and immediate via lanes, with the limitations below.

`--repairs invalid-ways` removes eligible structurally invalid road ways in `network.repaired.osm`, preserving the input and relation-member ways. Disconnected roads and vehicle restrictions are preserved because they may be intentional. SUMO conversion applies its geometry and junction processing; arbitrary topology and traffic-light problems require review rather than invented repairs.

Run checks with `python -m unittest -v test_network_analysis.py`.


## Research quality report

`--analyze` and `--analyze-only` now include drivable/non-drivable way counts for the selected vehicle class, road-class distribution, explicit and implied one-way counts, raw lane-count and speed-limit distributions, missing attributes, suspicious lane/speed/direction values, duplicate IDs, coincident coordinates, repeated/reversed way geometry, and detectable self-crossings. JSON `quality_details` contains the affected IDs and attribute values.

The installed `osmNetconvert.typ.xml` used by generation supplies road mapping and default permissions. Specific OSM access tags override broader access tags in the estimate. Without SUMO, a documented offline baseline is used for passenger vehicles; mapping and other vehicle-class permissions remain unverified. Conditional access, country-specific defaults, turn restrictions, and every OSM subtype combination are not resolved. Drivability is an estimate, separate from structural validity. Unknown highway types count as ways absent from the mapping, not as distinct type names; `road_types` provides the class distribution.

Definitions:

- Isolated nodes: nodes referenced by no OSM way, potentially legitimate POIs or relation members. `nodes_outside_road_network` separately counts nodes unused by highway ways.
- Dead-end roads: valid drivable ways with at least one endpoint of degree one in the undirected drivable graph. Each way is counted once. Extract boundaries and cul-de-sacs can legitimately produce these.
- Largest drivable component: percentage of valid drivable graph nodes in the largest weak component. The original `components` metric still covers all structurally valid highway ways.
- Duplicate IDs are structural failures. Coincident node coordinates and identical/reversed node-reference sequences are findings, not automatic merge instructions.
- Self-crossings: proper planar intersections between non-adjacent segments of the same way. Closed rings are not flagged solely for repeating their start node. Collinear overlaps, crossings between separate ways, and grade separation are not resolved. Ways over 2,000 references are explicitly listed as unchecked to bound quadratic geometry work.
- Missing lane/speed counts apply to estimated drivable ways and base `lanes`/`maxspeed` tags. Directional values are checked separately. Raw distributions preserve original units; numeric speed checks convert mph and knots to km/h. Lane counts over 20 or speeds outside (0, 200] km/h are review thresholds, not definitive errors. Symbolic speed limits are reported separately.
- Repair reports describe the original input and show the removal count; they do not silently score the repaired copy as the original.

## Compatibility rubric version 1.2

This is a reproducible diagnostic score, not a statistically calibrated probability or official SUMO certification. Every check records its status, weight, earned points, deduction, and reason. Unchecked categories remain pending and are excluded from the evaluated denominator. Both stages use the same 100-point rubric. Fresh conversion carries forward source mapping and optional-attribute checks, updates topology/connectivity/permissions with converted evidence, and evaluates routing.

| Check | Before conversion | After conversion |
| --- | ---: | ---: |
| Topology | 30 | 30 |
| Road connectivity | 25 | 25 |
| Vehicle permissions | 15 | 15 |
| Road-type mapping | 10 | 10 (retained from source) |
| Optional attributes | 5 | 5 (retained from source) |
| Routing feasibility | 15 (pending) | 15 |
| Traffic-signal information | 0 | 0 (invalid connections fail topology) |

Topology loses all 30 points for invalid ways/duplicate IDs or an empty road network before conversion; otherwise suspect way geometry causes proportional deductions. After conversion invalid edges, invalid lane/via references, or invalid TLS references fail topology. Connectivity earns 25 times the largest usable weak-component fraction; no usable component or a fraction below 50% is a critical failure. Permissions earn 15 when at least one valid usable way/edge exists; intentional forbidden roads do not reduce the score. Road mapping earns 10 times the mapped highway-way fraction when an installed mapping is available.

Missing base lane and speed attributes each deduct up to 2.5 points, proportional to drivable ways; suspicious attributes additionally deduct 5 times their count divided by highway-way count, with the category floored at zero. No tagged traffic signals is informational and incurs no penalty.

Pre-conversion routing is `NOT_CHECKED`: report earned points out of the evaluated 85, their normalized percentage, routing weight pending (15), and final score PENDING. For example, 81.1 / 85 normalizes to 95.4%. Post-conversion routing earns 15 times the reachable directed edge-pair fraction: all distinct origin/destination pairs for up to 128 eligible edges, or 128 evenly spaced origins in sorted edge-ID order against all destinations for larger networks. The JSON discloses the origins, sample flag, and pair counts. This follows permitted lane connections, but does not simulate lane-changing feasibility, full internal-junction traversal, departure constraints, closures, or demand. Zero reachable pairs fails routing; partial reachability is a warning. A PASS means all tested pairs were graph-reachable, not that arbitrary demand is validated by duarouter.

A completed score of at least 80 with no critical failure yields `PASS`; lower scores yield `WARNING`. Critical failures yield `FAIL` regardless of the total. Until every weighted category is evaluated, `final_score` is null and the display says `Final score PENDING` (a known critical failure can still show status FAIL). `normalized_score` is the displayed evaluated score divided by evaluated weight, as a percentage; zero evaluated weight displays N/A. JSON retains `score` as an alias for earned `evaluated_score`, not as a final total. Use `final_score` for completed results.

For example, 81.1 evaluated points plus 14.4 / 15 for routing produces a final 95.5 / 100 if the other checks remain unchanged. When analyzing an existing network independently, source mapping and attributes remain pending: the CLI does not assume that the current OSM matches that network. Fresh `--analyze --steps network` conversion provides matching source evidence. Conversion warnings can change an otherwise PASS status to WARNING without altering numeric points. The final score evaluates graph reachability, not a completed demand simulation.

Reference: [SUMO OpenStreetMap import documentation](https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html).


## Selective OSM Network Diagnosis and Repair

`python osm_to_sumo.py --analyze` shows analysis, eligible repair counts, a `Select repairs [none]` prompt, re-analysis after selected changes, then the numbered generation menu. Enter repair keys separated by spaces or commas. Enter nothing to change nothing. Ambiguous findings remain recommendations. `--repair` and `--repair-only` do not select any repairs implicitly.

```powershell
# Analysis only
python osm_to_sumo.py --analyze-only
# Infer only selected attributes and stop after re-analysis
python osm_to_sumo.py --repair-only --repairs speeds lanes
# Selected repairs then chosen generation stages
python osm_to_sumo.py --analyze --repairs speeds lanes --steps network routes --no-gui
```

Selections: `speeds`, `lanes`, `isolated-nodes`, `small-components`, `unsupported`, `geometry`, `invalid-ways`, or `none`. Noninteractive invocations default to no repairs and no generation unless `--steps` is provided. Without analysis/repair options, the original full-generation invocation remains available.

Speed inference converts installed typemap speed defaults from m/s to km/h. Lane inference converts typemap lanes per direction to total OSM lanes, doubling on two-way roads. Existing, directional, conditional, ambiguous, invalid, and duplicate-ID cases are excluded. Inferred values are modeling assumptions, not measurements: each action records the typemap, original default, conversion rule, and resulting value. Scores may improve through added assumptions; that does not demonstrate improved real-world data accuracy.

Node removal only includes untagged nodes referenced by neither ways nor relations. Small-component removal uses weak components of valid highway ways, retains the largest component, and offers only components of at most `--component-max-ways` (default 2). Relation-member ways are excluded from removal options. Unsupported-way removal requires an installed typemap. Geometry repair only collapses consecutive identical node references; crossings and coincident coordinates are left for review. Invalid-way removal never bridges missing nodes. No option automatically merges conflicting duplicate IDs or changes permissions.

Artifacts: `osm-analysis.json`, `network.repaired.osm`, `osm-analysis-repaired.json`, and `network-repair.json`. The repair audit records selections, candidate counts, actual changes, distinct modified/removed way counts, source and typemap hashes, parameters, before/after compatibility, and remaining recommendations. Original input is preserved; named output artifacts may be overwritten on another run.

Generation menu: 1 network; 2 network + polygons; 3 network + routes; 4 network + configuration; 5 all; 6 none. Option 4 creates a configuration without traffic demand. Existing routes may be used for configuration-only CLI generation, but freshly generated networks do not silently reuse old routes. Routing checks remain pending until conversion. A final score or successful conversion alone does not certify a ready demand scenario.


### Post-repair validation and separate statuses

Every selected repair batch is saved, then the complete OSM analysis runs again on that saved file. Its report is labeled **Post-repair Network Analysis**. `network-repair.json` now includes a `comparison` object for missing maxspeed, missing lanes, normalized compatibility, and both statuses. The printed repair summary displays those before/after transitions.

- `pre_conversion_status`: FAIL for a failed non-routing check; PENDING for a weighted non-routing check lacking evidence; PASS_WITH_WARNINGS for warning checks; otherwise PASS. Optional attribute warnings therefore do not obscure readiness for conversion.
- `final_status`: PENDING while any weighted evidence is missing or the analysis is pre-conversion. Once fully evaluated, FAIL for failed checks, PASS for the existing score threshold of 80, otherwise WARNING. Conversion warnings can downgrade a completed PASS to WARNING. Partial graph reachability remains a warning on the routing check; it is not relabeled as a perfect routing PASS.

Both fields appear at report top level and inside `compatibility`. The older `status` field remains for backward compatibility and may show an immediate FAIL even while final_status awaits routing. Post-conversion reports preserve the matching source's pre-conversion status when available.

Typical transitions: missing speeds and lanes ? PASS_WITH_WARNINGS / PENDING; infer speeds only ? PASS_WITH_WARNINGS / PENDING; infer both ? PASS / PENDING if all other source checks pass. Inferred data can improve this rubric without proving the assumptions match real-world conditions.
