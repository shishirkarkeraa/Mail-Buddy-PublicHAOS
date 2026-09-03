#!/usr/bin/env python3
# ruff: noqa: E501 - prose strings are kept intact for PDF layout content.
"""Generate the Mail-Buddy Pi, failover, and personal-learning operator PDF."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

OUTPUT = Path("output/pdf/mail-buddy-laptop-gpu-pi-fallback-guide.pdf")

NAVY = colors.HexColor("#17324D")
TEAL = colors.HexColor("#117C78")
PALE_TEAL = colors.HexColor("#E8F5F3")
PALE_BLUE = colors.HexColor("#EDF4FA")
INK = colors.HexColor("#243342")
MUTED = colors.HexColor("#607181")
LINE = colors.HexColor("#CFDCE6")
WHITE = colors.white


def footer(canvas, doc):
    canvas.saveState()
    width, _ = A4
    canvas.setStrokeColor(LINE)
    canvas.line(18 * mm, 14 * mm, width - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm, "Mail-Buddy - private, user-controlled inference")
    canvas.drawRightString(width - 18 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def code(text, style):
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Table(
        [[Paragraph(escaped.replace("\n", "<br/>"), style)]],
        colWidths=[168 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F7F9")),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        ),
    )


def bullet(text, style):
    return Paragraph(f"<bullet color='#117C78'>&#8226;</bullet>{text}", style)


def build():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=27,
        leading=32,
        textColor=NAVY,
        alignment=TA_CENTER,
        spaceAfter=12,
    )
    subtitle = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=12,
        leading=18,
        textColor=MUTED,
        alignment=TA_CENTER,
    )
    h1 = ParagraphStyle(
        "H1",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=NAVY,
        spaceBefore=10,
        spaceAfter=8,
    )
    h2 = ParagraphStyle(
        "H2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12.5,
        leading=16,
        textColor=TEAL,
        spaceBefore=9,
        spaceAfter=5,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=14,
        textColor=INK,
        spaceAfter=6,
    )
    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=8.3,
        leading=11,
        spaceAfter=0,
    )
    bullet_style = ParagraphStyle(
        "Bullet",
        parent=body,
        leftIndent=13,
        firstLineIndent=-9,
        bulletIndent=2,
        spaceAfter=4,
    )
    mono = ParagraphStyle(
        "Mono",
        parent=body,
        fontName="Courier",
        fontSize=7.6,
        leading=10.5,
        spaceAfter=0,
    )
    callout = ParagraphStyle(
        "Callout",
        parent=body,
        fontSize=9.5,
        leading=14,
        textColor=NAVY,
        spaceAfter=0,
    )

    doc = BaseDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title="Mail-Buddy Raspberry Pi, Laptop GPU, and Personal Learning Guide",
        author="Mail-Buddy",
        subject="Private Ollama primary and fallback deployment guide",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates(PageTemplate(id="main", frames=frame, onPage=footer))

    story = [Spacer(1, 17 * mm)]
    story += [
        Paragraph(
            "MAIL-BUDDY",
            ParagraphStyle(
                "Brand",
                parent=subtitle,
                fontName="Helvetica-Bold",
                fontSize=11,
                textColor=TEAL,
                spaceAfter=10,
            ),
        ),
        Paragraph("Raspberry Pi + Laptop GPU<br/>Personal Learning Deployment", title),
        Paragraph(
            "Gmail integration, direct labels, failover, Pi learning, MLX QLoRA, rollback, and Home Assistant OS boundaries",
            subtitle,
        ),
        Spacer(1, 16 * mm),
    ]
    node_style = ParagraphStyle(
        "Node",
        parent=body,
        alignment=TA_CENTER,
        fontName="Helvetica-Bold",
        spaceAfter=0,
    )
    route_style = ParagraphStyle("Route", parent=small, alignment=TA_CENTER, textColor=TEAL)
    architecture = Table(
        [
            [Paragraph("Destination Gmail", node_style), ""],
            [Paragraph("|", route_style), ""],
            [Paragraph("Mail-Buddy on Raspberry Pi", node_style), ""],
            [Paragraph("PRIMARY", route_style), Paragraph("FALLBACK", route_style)],
            [
                Paragraph("Laptop Ollama (GPU)", node_style),
                Paragraph("Pi Ollama (CPU)", node_style),
            ],
        ],
        colWidths=[75 * mm, 75 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_TEAL),
                ("BOX", (0, 0), (-1, -1), 1, TEAL),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("SPAN", (0, 0), (1, 0)),
                ("SPAN", (0, 1), (1, 1)),
                ("SPAN", (0, 2), (1, 2)),
                ("LINEABOVE", (0, 4), (0, 4), 0.8, TEAL),
                ("LINEABOVE", (1, 4), (1, 4), 0.8, TEAL),
                ("LINEBEFORE", (1, 3), (1, 4), 0.5, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        ),
    )
    story += [architecture, Spacer(1, 13 * mm)]
    summary = Table(
        [
            [
                Paragraph("Outcome", h2),
                Paragraph(
                    "The laptop handles semantic model requests when reachable. If the request cannot connect or complete, Mail-Buddy retries the same request against the Pi. If both fail, unresolved mail remains in Inbox with Needs Review.",
                    callout,
                ),
            ]
        ],
        colWidths=[29 * mm, 125 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.7, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        ),
    )
    story += [
        summary,
        Spacer(1, 7 * mm),
        Paragraph("Direct Gmail labels", h2),
        Paragraph(
            "Labels are top-level names such as Security OTP, Bank Transactions, "
            "College Notices, Order Updates, and Needs Review. No Mail-Buddy/ "
            "prefix is created. Known legacy labels are renamed in place so their "
            "message membership is preserved.",
            body,
        ),
        Spacer(1, 5 * mm),
        Paragraph("Pinned model", h2),
        code("llama3.2:3b-instruct-q4_K_M", mono),
        Spacer(1, 8 * mm),
        Paragraph("Version 1.0 | September 2026", subtitle),
        PageBreak(),
    ]

    story += [Paragraph("1. Security boundary", h1)]
    story += [
        Paragraph(
            "A redacted prompt derived from the message travels from the Pi to the laptop when the primary is enabled. The data remains on user-controlled devices, but it is no longer confined to the Pi. The prompt can still contain message text.",
            body,
        )
    ]
    for item in [
        "Use only a trusted home LAN or a private Tailscale connection.",
        "Never expose TCP 11434 through router port forwarding.",
        "Restrict the laptop firewall so only the Pi can reach TCP 11434.",
        "Never configure a public Ollama host; the app rejects public addresses.",
        "Update the public privacy policy before enabling this option for users.",
    ]:
        story.append(bullet(item, bullet_style))
    story += [Spacer(1, 5), Paragraph("Accepted primary addresses", h2)]
    address_rows = [
        ["Accepted", "10/8, 172.16/12, 192.168/16, Tailscale 100.64/10, IPv6 ULA fc00::/7"],
        [
            "Rejected",
            "Public IPs, DNS names, HTTPS URLs, URL credentials, query strings, fragments, paths",
        ],
    ]
    table = Table(
        [[Paragraph(a, small), Paragraph(b, small)] for a, b in address_rows],
        colWidths=[30 * mm, 132 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), PALE_TEAL),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story += [
        table,
        Paragraph("2. Prepare the laptop", h1),
        Paragraph(
            "Install Ollama natively. Apple Silicon Macs use Metal acceleration. Windows and Linux can use supported NVIDIA or AMD GPUs. Docker Desktop on macOS does not provide Ollama with GPU passthrough.",
            body,
        ),
        Paragraph("Pull and test the exact model", h2),
        code(
            'ollama pull llama3.2:3b-instruct-q4_K_M\nollama run llama3.2:3b-instruct-q4_K_M "Reply with READY only"\nollama ps',
            mono,
        ),
        Paragraph(
            "The Processor column in ollama ps reports GPU, CPU, or a split. Both laptop and Pi must retain the same pinned tag.",
            body,
        ),
    ]

    story += [
        Paragraph("3. Assign a stable private address", h1),
        Paragraph(
            "Reserve the laptop LAN address in the router, such as 192.168.1.25, or install Tailscale on both devices and use the laptop's stable 100.64/10 address. Use a literal IP in Mail-Buddy; .local names are intentionally not accepted.",
            body,
        ),
        PageBreak(),
        Paragraph("4. Expose Ollama to the Pi", h1),
        Paragraph(
            "Ollama binds to localhost by default. Change its listener and then enforce the source restriction with the laptop firewall.",
            body,
        ),
        Paragraph("macOS", h2),
        code('launchctl setenv OLLAMA_HOST "0.0.0.0:11434"', mono),
        Paragraph("Quit and reopen Ollama after setting the variable.", body),
        KeepTogether(
            [
                Paragraph("Linux systemd override", h2),
                code(
                    '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n\n'
                    "sudo systemctl daemon-reload\n"
                    "sudo systemctl restart ollama",
                    mono,
                ),
            ]
        ),
        Paragraph("Windows", h2),
        Paragraph(
            "Set the user environment variable OLLAMA_HOST to 0.0.0.0:11434, quit Ollama completely, and reopen it.",
            body,
        ),
        Paragraph("Connectivity check from the Pi", h2),
        code("curl --fail --max-time 5 http://192.168.1.25:11434/api/tags", mono),
        Paragraph(
            "The response must list the pinned model. This test does not transmit mail content.",
            body,
        ),
    ]

    story += [
        Paragraph("5. Configure Mail-Buddy on the Pi", h1),
        Paragraph(
            "Edit /opt/mail-buddy/.env. Replace the example address with the laptop's fixed private or Tailscale address.",
            body,
        ),
        code(
            "MAIL_BUDDY_OLLAMA_PRIMARY_URL=http://192.168.1.25:11434\nMAIL_BUDDY_OLLAMA_CONNECT_TIMEOUT_SECONDS=3\nMAIL_BUDDY_OLLAMA_TIMEOUT_SECONDS=120",
            mono,
        ),
        Paragraph(
            "Do not replace the internal fallback URL. Compose supplies MAIL_BUDDY_OLLAMA_URL=http://ollama:11434 to the application. The Pi still downloads the model so fallback remains available.",
            body,
        ),
        Paragraph("Start and inspect", h2),
        code(
            "cd /opt/mail-buddy\n./scripts/start-mail-buddy.sh\ndocker compose ps\ndocker compose logs --tail=100 app ollama model-init",
            mono,
        ),
        Paragraph("6. How automatic failover works", h1),
    ]
    flow_rows = [
        [
            "1",
            "Deterministic rules",
            "Encrypted user rules and high-confidence deterministic evidence run first.",
        ],
        ["2", "Laptop primary", "Ambiguous mail is sent to the configured laptop Ollama endpoint."],
        [
            "3",
            "Pi fallback",
            "Connection, timeout, or HTTP failure triggers one retry against Pi Ollama.",
        ],
        [
            "4",
            "Safe total failure",
            "If both model requests fail, unresolved mail stays in Inbox with Needs Review.",
        ],
    ]
    table = Table(
        [[Paragraph(a, small), Paragraph(b, small), Paragraph(c, small)] for a, b, c in flow_rows],
        colWidths=[10 * mm, 37 * mm, 115 * mm],
        repeatRows=0,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), TEAL),
                ("TEXTCOLOR", (0, 0), (0, -1), WHITE),
                ("BACKGROUND", (1, 0), (1, -1), PALE_TEAL),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story += [table, PageBreak(), Paragraph("7. Continuous personal learning", h1)]
    for item in [
        "Only owner-confirmed Needs Review corrections and answered accuracy questions become ground truth.",
        "The registry encrypts redacted sender domain, subject, and bounded Gmail snippet; complete bodies and attachment text are not retained for training.",
        "The Pi companion becomes eligible at 10 examples across at least two categories and uses deterministic sender-grouped five-fold validation.",
        "For ambiguous mail the companion advises Llama. Confident disagreement stays in Inbox with Needs Review; low confidence never overrides Llama.",
        "Disconnecting Gmail purges training examples, job/model metadata, and companion artifacts from the application database.",
    ]:
        story.append(bullet(item, bullet_style))
    gates = [
        ["Companion readiness", "10 confirmed examples; at least two represented categories"],
        ["Companion promotion", "5+ held-out predictions; accuracy >=65%; no macro-F1 regression"],
        ["QLoRA readiness", "200 confirmed examples; 10+ per included category; sender-isolated train/valid/test"],
        ["QLoRA promotion", "Accuracy >=70% and no production, macro-F1, sensitive-recall, schema, injection, or failover regression"],
    ]
    table = Table(
        [[Paragraph(a, small), Paragraph(b, small)] for a, b in gates],
        colWidths=[43 * mm, 119 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), PALE_TEAL),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story += [
        table,
        Paragraph("Schedule and dashboard", h2),
        Paragraph(
            "Automatic training defaults to every seven days at 02:00 in TZ (Asia/Kolkata by default). The dashboard offers 1, 3, 7, 14, or 30 days and displays readiness, category counts, last/next run, phase, metrics, failures, active versions, rejected versions, manual companion training, and rollback. Internal timestamps remain UTC.",
            body,
        ),
        PageBreak(),
        Paragraph("8. Install the Apple Silicon MLX trainer", h1),
        Paragraph(
            "Use a 16 GB Apple Silicon Mac with native Ollama, FileVault, AC power, and at least 20 GiB free. Install mlx-lm[train] in a private virtual environment. Build llama.cpp, then pin both its exact commit and the SHA-256 of llama-quantize in the mode-0600 trainer configuration.",
            body,
        ),
        code(
            "ssh-keygen -t ed25519 -f ~/.ssh/mail_buddy_trainer_ed25519\n"
            "# Copy the .pub file to the normal Raspberry Pi OS host, then:\n"
            "sudo /opt/mail-buddy/scripts/install-pi-trainer-key.sh /tmp/mail_buddy_trainer_ed25519.pub\n"
            "mkdir -p ~/.config/mail-buddy\n"
            "cp scripts/mail-buddy-trainer.env.example ~/.config/mail-buddy/trainer.env\n"
            "chmod 600 ~/.config/mail-buddy/trainer.env\n"
            "./scripts/install-macos-trainer.sh",
            mono,
        ),
        Paragraph(
            "The launchd agent checks hourly and catches up after sleep. The forced SSH key cannot open a shell: it can request privacy-safe status, acquire a locked due export, transfer candidate files only into the model inbox, record verified installation, promote, fail, or roll back.",
            body,
        ),
        Paragraph("Fixed QLoRA recipe", h2),
    ]
    for item in [
        "MLX-LM QLoRA; seed 42; batch 1; gradient accumulation 8.",
        "Eight trainable layers; rank 8; scale 20; maximum sequence 1024.",
        "Prompt masking and gradient checkpointing; approximately five dataset epochs.",
        "Fuse/dequantize to FP16 GGUF, then checksum-pinned llama.cpp Q4_K_M quantization exactly once.",
        "Evaluate candidate and current production end-to-end through Ollama before any activation.",
    ]:
        story.append(bullet(item, bullet_style))
    story += [PageBreak(), Paragraph("9. Dual-host release and recovery", h1)]
    release_rows = [
        ["1", "Export", "Pi checks enabled, local schedule, 200-example readiness, and unique database lock."],
        ["2", "Train", "Only redacted JSONL crosses SSH; temporary files use a mode-0700 directory."],
        ["3", "Evaluate", "Candidate and production metrics plus application safety tests must pass."],
        ["4", "Install", "Identical GGUF checksum and versioned tag are recorded for laptop and Pi."],
        ["5", "Activate", "Registry promotion is atomic; any partial failure leaves production unchanged."],
        ["6", "Recover", "Dashboard rollback restores a previous promoted tag; two prior versions remain registered."],
    ]
    table = Table(
        [[Paragraph(a, small), Paragraph(b, small), Paragraph(c, small)] for a, b, c in release_rows],
        colWidths=[10 * mm, 30 * mm, 122 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), TEAL),
                ("TEXTCOLOR", (0, 0), (0, -1), WHITE),
                ("BACKGROUND", (1, 0), (1, -1), PALE_TEAL),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story += [
        table,
        Paragraph("Home Assistant OS on Raspberry Pi 5", h2),
        Paragraph(
            "Do not install this Docker Compose stack, trainer SSH account, or Ollama directly into Home Assistant OS. Keep HA OS as the appliance and run Mail-Buddy on a Mac, second Raspberry Pi OS host, or another supported Linux Docker host. Do not mount the Home Assistant Docker socket or disable app protection.",
            body,
        ),
        Paragraph("Privacy disclosure", h2),
        Paragraph(
            "Disclose encrypted redacted training excerpts and optional processing on a user-controlled laptop. Plaintext JSONL exists transiently during training. On SSD/APFS, deletion cannot guarantee physical erasure because of wear levelling; keep FileVault enabled. GGUF artifacts outside the application database must be removed separately during device decommissioning.",
            body,
        ),
        Paragraph("Operator diagnostics", h2),
        code(
            "docker compose exec -T app mail-buddy training-status\n"
            "docker compose exec -T app mail-buddy recover-stale-training --older-than-hours 12\n"
            "docker compose exec -T app mail-buddy rollback-main-model\n"
            "launchctl print gui/$(id -u)/com.mail-buddy.trainer",
            mono,
        ),
        PageBreak(),
        Paragraph("10. Acceptance tests", h1),
        Paragraph("Use only a dedicated Gmail test account and harmless synthetic messages.", body),
    ]
    tests = [
        (
            "Laptop primary",
            "Keep laptop Ollama running, send an ambiguous synthetic message, confirm laptop Ollama logs or ollama ps show activity, and verify the Gmail label.",
        ),
        (
            "Pi fallback",
            "Quit laptop Ollama, send a different synthetic message, inspect docker compose logs --since=5m app ollama, and confirm classification succeeds.",
        ),
        (
            "Both unavailable",
            "Stop both Ollama instances. Confirm unresolved mail stays in Inbox "
            "and receives Needs Review.",
        ),
    ]
    for name, text in tests:
        story.append(KeepTogether([Paragraph(name, h2), Paragraph(text, body)]))

    story += [PageBreak(), Paragraph("11. Routine operations", h1)]
    ops = [
        ["Start Pi stack", "./scripts/start-mail-buddy.sh"],
        ["Stop safely", "./scripts/stop-mail-buddy.sh"],
        ["Container status", "docker compose ps"],
        ["Recent inference logs", "docker compose logs --since=5m app ollama"],
        ["Laptop processor", "ollama ps"],
        ["Disable primary", "Blank MAIL_BUDDY_OLLAMA_PRIMARY_URL and restart"],
    ]
    table = Table(
        [[Paragraph(a, small), Paragraph(b, mono)] for a, b in ops], colWidths=[47 * mm, 115 * mm]
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), PALE_TEAL),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story += [table, Paragraph("12. Troubleshooting", h1)]
    trouble = [
        [
            "Laptop gets no requests",
            "Run the Pi curl test; check OLLAMA_HOST, firewall, address, and exact model tag.",
        ],
        [
            "Primary URL rejected",
            "Use http://PRIVATE-IP:11434 with no path, credentials, query, or hostname.",
        ],
        ["Laptop uses CPU", "Use native Ollama and verify GPU support and drivers with ollama ps."],
        [
            "Fallback is slow",
            "Keep connect timeout at 3 seconds and use a stable LAN or Tailscale address.",
        ],
        [
            "Both models unavailable",
            "Restore either service. Needs Review messages remain in Inbox for correction.",
        ],
    ]
    table = Table(
        [[Paragraph(a, small), Paragraph(b, small)] for a, b in trouble],
        colWidths=[47 * mm, 115 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, PALE_BLUE]),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story += [table, Paragraph("13. Go-live checklist", h1)]
    for item in [
        "Both machines list llama3.2:3b-instruct-q4_K_M.",
        "Laptop has a stable private or Tailscale address.",
        "Laptop firewall allows TCP 11434 only from the Pi.",
        "No router port forwarding exposes Ollama.",
        "Laptop-primary and Pi-fallback synthetic tests both pass.",
        "Both-hosts-down test keeps unresolved mail in Needs Review.",
        "Owner answers, not unanswered predictions, are the only training truth.",
        "Mac trainer refuses battery power, low disk, or an unpinned quantizer.",
        "Candidate checksum/tag is identical on laptop and Pi before promotion.",
        "Rollback restores the previous fine-tuned version.",
        "Home Assistant OS remains unchanged on its dedicated Pi.",
        "Public privacy policy discloses optional trusted-laptop inference.",
    ]:
        story.append(
            Paragraph(
                f"<bullet color='#117C78'>&#8226;</bullet>[ ] {item}",
                ParagraphStyle(
                    "Checklist",
                    parent=small,
                    leftIndent=13,
                    firstLineIndent=-9,
                    bulletIndent=2,
                    spaceAfter=2,
                ),
            )
        )
    story += [PageBreak(), Paragraph("Official references", h1)]
    for text, url in [
        ("Ollama FAQ and network configuration", "https://docs.ollama.com/faq"),
        ("Ollama hardware and GPU support", "https://docs.ollama.com/gpu"),
        ("Ollama macOS support", "https://docs.ollama.com/macos"),
        ("Ollama API introduction", "https://docs.ollama.com/api/introduction"),
        ("MLX-LM LoRA and QLoRA", "https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md"),
        ("Ollama GGUF import", "https://github.com/ollama/ollama/blob/main/docs/import.mdx"),
    ]:
        story.append(
            Paragraph(
                f"<link href='{url}' color='#117C78'>{text}</link><br/><font size='7.5' color='#607181'>{url}</font>",
                small,
            )
        )

    doc.build(story)
    print(OUTPUT)


if __name__ == "__main__":
    build()
