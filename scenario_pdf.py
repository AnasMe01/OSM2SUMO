"""Publication-friendly scenario report with vector charts and paginated tables."""
from io import BytesIO
from textwrap import wrap

from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure


def scenario_pdf(report):
    metadata = report.get('input', {})
    validation = report.get('validation', {})
    checks = [check for check in validation.get('checks', []) if check.get('name') != 'Simulation startup']
    output = BytesIO()
    page_number = 0
    colors = {'PASS': '#168575', 'FAIL': '#c84646', 'PENDING': '#bc850c',
              'NOT_CHECKED': '#718096', 'SKIP': '#9aa8b5'}

    def page(title):
        nonlocal page_number
        page_number += 1
        figure = Figure(figsize=(8.27, 11.69), facecolor='white')
        figure.text(.08, .955, 'OSM NETWORK STUDIO', fontsize=10, color='#168575', weight='bold')
        figure.text(.08, .92, title, fontsize=21, color='#203349', weight='bold')
        source = metadata.get('source_name', metadata.get('source', 'Scenario'))
        figure.text(.08, .875, '\n'.join(wrap(str(source), 85)), fontsize=9, color='#526477', va='top')
        figure.text(.08, .035, 'Scenario validation and reproducibility', fontsize=8, color='#526477')
        figure.text(.92, .035, str(page_number), fontsize=8, color='#526477', ha='right')
        return figure

    def text(value):
        return 'Not recorded' if value is None else str(value)

    with PdfPages(output, metadata={'Title': 'Scenario validation and reproducibility report',
                                    'Author': 'OSM Network Studio'}) as pdf:
        figure = page('Scenario report')
        status = validation.get('status', 'PENDING')
        figure.text(.08, .81, 'Overall status: ' + status, fontsize=17, weight='bold', color=colors.get(status, '#526477'))
        figure.text(.08, .775, 'Validation date: ' + text(report.get('generated_at')), fontsize=9)
        ax = figure.add_axes([.24, .49, .65, .22])
        statuses = ['PASS', 'FAIL', 'NOT_CHECKED', 'SKIP']
        counts = [sum(c.get('status') == s for c in checks) for s in statuses]
        ax.barh(statuses, counts, color=[colors[s] for s in statuses], height=.6)
        ax.invert_yaxis()
        ax.set_xlim(0, max(counts + [1]) * 1.2)
        ax.set_xlabel('Number of checks', fontsize=9)
        ax.set_xticks(range(max(counts + [1]) + 1))
        for i, count in enumerate(counts):
            ax.text(count + .08, i, str(count), va='center', fontsize=10)
        ax.spines[['top', 'right']].set_visible(False)
        figure.text(.08, .735, 'Figure 1. Validation outcomes', fontsize=12, weight='bold')
        figure.text(.08, .415, 'Figure 2. Traffic generation results', fontsize=12, weight='bold')
        values = [metadata.get(k) for k in ('requested', 'valid_routes', 'rejected')]
        if all(isinstance(v, (float, int)) for v in values):
            ax = figure.add_axes([.24, .20, .65, .17])
            ax.barh(['Requested', 'Routed', 'Rejected'], values, color=['#447ca6', '#168575', '#c84646'], height=.55)
            ax.invert_yaxis()
            ax.set_xlim(0, max(values + [1]) * 1.22)
            for i, value in enumerate(values):
                ax.text(value + max(values + [1]) * .02, i, f'{value:g}', va='center', fontsize=10)
            ax.set_xlabel('Vehicles', fontsize=9)
            ax.spines[['top', 'right']].set_visible(False)
        else:
            figure.text(.08, .35, 'Demand totals were not recorded for this run.', fontsize=10)
        figure.text(.08, .105, 'Scope: a short SUMO run checks startup and vehicle insertion.\n'
                    'A passing result does not certify completion of every trip.', fontsize=10, color='#526477', linespacing=1.5)
        pdf.savefig(figure)

        def table_pages(title, headers, rows, widths, characters):
            rows = rows or [('Not recorded',) * len(headers)]
            prepared = []
            for row in rows:
                cells = ['\n'.join(wrap(text(value), width=limit, break_long_words=True)) for value, limit in zip(row, characters)]
                prepared.append((cells, max(cell.count('\n') + 1 for cell in cells) * .014 + .018))
            batches, batch, height = [], [], .04
            for item in prepared:
                if batch and height + item[1] > .74:
                    batches.append(batch)
                    batch, height = [], .04
                batch.append(item)
                height += item[1]
            batches.append(batch)
            for index, batch in enumerate(batches):
                fig = page(title + (' (continued)' if index else ''))
                height = .04 + sum(item[1] for item in batch)
                ax = fig.add_axes([.08, .08, .84, .76])
                ax.axis('off')
                table = ax.table(cellText=[item[0] for item in batch] or [['Not recorded'] * len(headers)],
                                 colLabels=headers, colWidths=widths, cellLoc='left', colLoc='left',
                                 bbox=[0, 1 - height / .76, 1, height / .76])
                table.auto_set_font_size(False)
                table.set_fontsize(8)
                for (row, col), cell in table.get_celld().items():
                    cell.set_edgecolor('#dfe8f0')
                    cell.PAD = .045
                    cell.set_height((.04 if row == 0 else batch[row - 1][1]) / height)
                    if row == 0:
                        cell.set_facecolor('#203349')
                        cell.set_text_props(color='white', weight='bold')
                    else:
                        cell.set_facecolor('#f0f6f8' if row % 2 else 'white')
                        if cell.get_text().get_text() in colors:
                            cell.set_text_props(color=colors[cell.get_text().get_text()], weight='bold')
                pdf.savefig(fig)

        duration = metadata.get('duration')
        if duration is None and 'end' in metadata and 'begin' in metadata:
            duration = metadata['end'] - metadata['begin']
        rows = [('Vehicle class', metadata.get('effective_vehicle_class', metadata.get('vehicle_class'))),
                ('Traffic profile', metadata.get('profile')), ('Requested vehicles', metadata.get('requested')),
                ('Routed vehicles', metadata.get('valid_routes')), ('Rejected vehicles', metadata.get('rejected')),
                ('Routing success (%)', metadata.get('routing_success')), ('Simulation duration (s)', duration),
                ('Trip period (s)', metadata.get('period')), ('Random seed', metadata.get('seed')),
                ('Source revision', metadata.get('source_revision'))]
        for label, value in (metadata.get('composition') or {}).items():
            rows.append((label.replace('_', ' ').title() + ' (%)', value))
        environment = report.get('environment', {})
        rows.extend([('Tool version', environment.get('tool_version')), ('SUMO version', environment.get('sumo')),
                     ('Python version', environment.get('python')), ('Platform', environment.get('platform'))])
        table_pages('Experiment parameters', ['Parameter', 'Value'], rows, [.38, .62], [31, 57])
        table_pages('Validation evidence', ['Check', 'Status', 'Evidence'],
                    [(c.get('name'), c.get('status'), c.get('detail')) for c in checks], [.27, .16, .57], [24, 13, 51])
        processing = metadata.get('network_processing') or {}
        analysis = processing.get('analysis') or {}
        if analysis:
            table_pages('Network processing', ['Measure', 'Value'],
                        [(label, analysis.get(key)) for key, label in [('nodes', 'Nodes'), ('road_ways', 'Road ways'),
                          ('drivable_ways', 'Drivable ways'), ('components', 'Components'), ('ways_missing_maxspeed', 'Ways missing speed'),
                          ('ways_missing_lanes', 'Ways missing lanes'), ('pre_conversion_status', 'Pre-conversion status')]] +
                        [('Repair audit', name) for name in processing.get('repair_audits', [])], [.4, .6], [34, 54])
        table_pages('Scenario artifacts', ['File', 'Size (bytes)'],
                    [(name, f"{entry.get('bytes', 0):,}") for name, entry in report.get('artifacts', {}).items()], [.76, .24], [66, 19])
    return output.getvalue()
