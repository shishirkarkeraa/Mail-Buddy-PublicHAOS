#!/usr/bin/env python3
"""Generate a deterministic SPDX 2.3 inventory from the pinned runtime lock."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

PROJECT_NAME = "mail-buddy"
PROJECT_VERSION = "0.1.0"
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


def spdx_id(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.-]", "-", name)
    return f"SPDXRef-Package-{normalized}"


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: generate-python-sbom.py REQUIREMENTS_LOCK OUTPUT_JSON",
            file=sys.stderr,
        )
        return 2
    lock_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    lock_bytes = lock_path.read_bytes()
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    dependencies: list[tuple[str, str]] = []
    for raw_line in lock_bytes.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-r ")):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"Unpinned runtime requirement: {line}")
        dependencies.append((match.group(1), match.group(2)))

    root_id = spdx_id(PROJECT_NAME)
    packages = [
        {
            "SPDXID": root_id,
            "name": PROJECT_NAME,
            "versionInfo": PROJECT_VERSION,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": "NOASSERTION",
        }
    ]
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_id,
        }
    ]
    for name, version in sorted(dependencies, key=lambda item: item[0].lower()):
        package_id = spdx_id(name)
        normalized = name.lower().replace("_", "-")
        packages.append(
            {
                "SPDXID": package_id,
                "name": name,
                "versionInfo": version,
                "downloadLocation": (
                    f"https://pypi.org/project/{quote(normalized)}/{quote(version)}/"
                ),
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{normalized}@{version}",
                    }
                ],
            }
        )
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": package_id,
            }
        )

    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{PROJECT_NAME}-python-runtime-{PROJECT_VERSION}",
        "documentNamespace": (
            f"https://mail-buddy.local/spdx/{PROJECT_NAME}/{PROJECT_VERSION}/{lock_sha256}"
        ),
        "creationInfo": {
            "created": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            ),
            "creators": ["Tool: Mail-Buddy generate-python-sbom.py"],
        },
        "documentDescribes": [root_id],
        "packages": packages,
        "relationships": relationships,
        "annotations": [
            {
                "annotationDate": datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "annotationType": "OTHER",
                "annotator": "Tool: Mail-Buddy generate-python-sbom.py",
                "comment": f"requirements.lock SHA-256: {lock_sha256}",
            }
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
