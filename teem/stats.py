"""How Runs have gone over time: the numbers behind "is this setup doing good work?"."""

import html

from .common import STATUS_LABELS
from .telegram import last_error

WINDOW = "30 days"
STOPPED = ("failed", "blocked", "checks_failed", "uncertain")


def percent(part, whole):
    return f"{100 * part // whole}% ({part}/{whole})" if whole else "–"


def collect(conn):
    runs = conn.execute(f"""
        SELECT r.id,r.status,r.stop_reason,r.pr_state,r.created_at,c.body->>'objective' AS objective,
               (SELECT a.usage->>'model' FROM attempts a JOIN tasks t ON t.id=a.task_id
                WHERE t.run_id=r.id AND t.kind='code_and_check' ORDER BY a.started_at LIMIT 1) AS implementer,
               (SELECT payload->>'decider_model' FROM events WHERE run_id=r.id AND kind='proposal_created'
                LIMIT 1) AS decider,
               (SELECT max(revision_number) FROM tasks WHERE run_id=r.id AND kind='code_and_check') AS revisions,
               (SELECT v.result->>'verdict' FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                JOIN tasks t ON t.id=a.task_id WHERE t.run_id=r.id AND t.revision_number=0
                AND v.disposition='accepted' ORDER BY v.created_at LIMIT 1) AS first_verdict
        FROM runs r JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
        WHERE r.created_at > now()-interval '{WINDOW}' ORDER BY r.created_at DESC""").fetchall()
    started = [r for r in runs if r["status"] not in ("awaiting_approval", "denied")]
    reviewed = [r for r in runs if r["first_verdict"]]
    published = [r for r in runs if r["pr_state"]]
    stopped = [r for r in runs if r["status"] in STOPPED]
    errors = {}
    for run in stopped:
        reason = run["stop_reason"] or last_error(conn, run["id"]) or run["status"]
        errors.setdefault(reason[:100], []).append(run)

    def by(key):
        groups = {}
        for run in started:
            groups.setdefault(run[key] or "unrecorded", []).append(run)
        return [(name, len(group), percent(sum(r["first_verdict"] == "pass" for r in group),
                                           sum(bool(r["first_verdict"]) for r in group)),
                 percent(sum(r["pr_state"] == "merged" for r in group), sum(bool(r["pr_state"]) for r in group)))
                for name, group in sorted(groups.items())]

    return {
        "outcomes": sorted(((STATUS_LABELS.get(s, s), sum(r["status"] == s for r in runs))
                            for s in {r["status"] for r in runs}), key=lambda item: -item[1]),
        "started": len(started),
        "first_pass": percent(sum(r["first_verdict"] == "pass" for r in reviewed), len(reviewed)),
        "revisions": (f"{sum(r['revisions'] or 0 for r in started) / len(started):.1f}" if started else "–"),
        "merged": percent(sum(r["pr_state"] == "merged" for r in published), len(published)),
        "errors": sorted(((reason, len(group)) for reason, group in errors.items()), key=lambda item: -item[1])[:8],
        "implementers": by("implementer"),
        "deciders": by("decider"),
        "recent_stops": [(r["id"], r["objective"], STATUS_LABELS.get(r["status"], r["status"]),
                          r["stop_reason"] or last_error(conn, r["id"]) or "") for r in stopped[:10]],
    }


def render(stats):
    def table(headers, rows):
        head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        return f"<table><tr>{head}</tr>{body or '<tr><td>None</td></tr>'}</table>"

    esc = html.escape
    return (
        f"<h1>Last {WINDOW}</h1>"
        f"<p>Runs started: {stats['started']} · first review passed: {stats['first_pass']} · "
        f"revisions per Run: {stats['revisions']} · pull requests merged: {stats['merged']}</p>"
        "<h2>Outcomes</h2>" + table(["Status", "Runs"], [(esc(s), n) for s, n in stats["outcomes"]]) +
        "<h2>By implementer model</h2>" +
        table(["Model", "Runs", "First review passed", "Merged"], [tuple(esc(str(c)) for c in row)
                                                                  for row in stats["implementers"]]) +
        "<h2>By decider model</h2>" +
        table(["Model", "Runs", "First review passed", "Merged"], [tuple(esc(str(c)) for c in row)
                                                                  for row in stats["deciders"]]) +
        "<h2>Why Runs stopped</h2>" + table(["Reason", "Runs"], [(esc(r), n) for r, n in stats["errors"]]) +
        "<h2>Recent stops</h2>" +
        table(["Run", "Status", "Reason"], [(f"<a href='/runs/{i}'>{esc(o[:80])}</a>", esc(s), esc(r[:160]))
                                            for i, o, s, r in stats["recent_stops"]])
    )
