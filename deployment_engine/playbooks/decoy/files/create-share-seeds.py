#!/usr/bin/env python3
"""Create the three fixed Samba seed documents from the shared resource profile."""

import argparse
import html
import json
import zipfile
from pathlib import Path


MAPPING = {
    "document_team_meeting_notes": "Team/meeting-notes.odt",
    "spreadsheet_inventory_tracker": "Operations/inventory.ods",
    "document_project_status": "Projects/project-status.odt",
}
MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
<manifest:file-entry manifest:full-path="/" manifest:media-type="{media_type}"/>
<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>"""


def content_xml(body_tag, body):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'office:version="1.3"><office:body>'
        f'<{body_tag}>{body}</{body_tag}>'
        '</office:body></office:document-content>'
    )


def write_resource(resource, destination):
    if resource["kind"] == "document":
        media_type = "application/vnd.oasis.opendocument.text"
        parts = [f"<text:h>{html.escape(str(resource['title']))}</text:h>"]
        for heading, values in resource["sections"].items():
            parts.append(f"<text:h>{html.escape(str(heading))}</text:h>")
            parts.extend(
                f"<text:p>{html.escape(str(value))}</text:p>" for value in values
            )
        body = content_xml("office:text", "".join(parts))
    elif resource["kind"] == "spreadsheet":
        media_type = "application/vnd.oasis.opendocument.spreadsheet"
        rows = []
        for row in [resource["columns"], *resource["rows"]]:
            cells = "".join(
                '<table:table-cell office:value-type="string">'
                f"<text:p>{html.escape(str(value))}</text:p>"
                "</table:table-cell>"
                for value in row
            )
            rows.append(f"<table:table-row>{cells}</table:table-row>")
        body = content_xml(
            "office:spreadsheet",
            '<table:table table:name="Sheet1">' + "".join(rows) + "</table:table>",
        )
    else:
        raise RuntimeError(f"unsupported seed resource kind: {resource['kind']}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("mimetype", media_type, compress_type=zipfile.ZIP_STORED)
        archive.writestr("content.xml", body)
        archive.writestr("META-INF/manifest.xml", MANIFEST.format(media_type=media_type))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("share_root", type=Path)
    args = parser.parse_args()
    resources = json.loads(args.profile.read_text(encoding="utf-8"))["resources"]
    for resource_id, relative_path in MAPPING.items():
        write_resource(resources[resource_id], args.share_root / relative_path)


if __name__ == "__main__":
    main()
