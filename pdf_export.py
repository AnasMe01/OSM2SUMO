from textwrap import wrap


def _display(value):
    if value is None:
        return 'Not available'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def _lines(value, label=None, depth=0):
    indent = '  ' * depth
    heading = str(label).replace('_', ' ').title() if label is not None else None
    if isinstance(value, dict):
        if heading:
            yield indent + heading
        for key, item in value.items():
            yield from _lines(item, key, depth + (1 if heading else 0))
    elif isinstance(value, list):
        if heading:
            yield f'{indent}{heading} ({len(value)})'
        if not value:
            yield indent + '  None'
        for index, item in enumerate(value, 1):
            item_label = f'Item {index}' if isinstance(item, (dict, list)) else None
            if item_label:
                yield from _lines(item, item_label, depth + 1)
            else:
                yield f'{indent}  - {_display(item)}'
    else:
        yield f'{indent}{heading}: {_display(value)}' if heading else indent + _display(value)


def _escape(text):
    text = text.encode('latin-1', 'replace').decode('latin-1')
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def structured_pdf(title, source_name, data):
    """Return a readable PDF byte string without requiring a third-party package."""
    body = []
    for line in _lines(data):
        body.extend(wrap(line, width=92, subsequent_indent='    ', replace_whitespace=True) or [''])

    lines_per_page = 54
    pages = [body[i:i + lines_per_page] for i in range(0, len(body), lines_per_page)] or [[]]
    objects = [None, None, b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    page_ids = []
    for page_number, page_lines in enumerate(pages, 1):
        commands = ['BT', '/F1 16 Tf', '50 794 Td', f'({_escape(title)}) Tj',
                    '/F1 9 Tf', '0 -20 Td', f'(Source: {_escape(source_name)}) Tj',
                    '0 -18 Td', f'(Page {page_number} of {len(pages)}) Tj', '0 -18 Td']
        for line in page_lines:
            commands.extend([f'({_escape(line)}) Tj', '0 -13 Td'])
        commands.append('ET')
        stream = '\n'.join(commands).encode('latin-1')
        content_id = len(objects) + 1
        objects.append(b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream')
        page_id = len(objects) + 1
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
                       f'/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>'.encode())
        page_ids.append(page_id)

    objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
    kids = ' '.join(f'{page_id} 0 R' for page_id in page_ids)
    objects[1] = f'<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>'.encode()

    pdf = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for object_id, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f'{object_id} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(pdf)
    pdf.extend(f'xref\n0 {len(objects) + 1}\n'.encode())
    pdf.extend(b'0000000000 65535 f \n')
    for offset in offsets[1:]:
        pdf.extend(f'{offset:010d} 00000 n \n'.encode())
    pdf.extend(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(pdf)
