"""Task-table import: a sheet of work items becomes tickets, owners, projects and blockers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from company_brain.graph import FalkorDBStore
from company_brain.tables import TaskMapping, TaskTableImport, import_task_table, parse_due
from tests.conftest import FakeStore

HEADER = ["Task ID", "Task", "Owner", "Status", "Due", "Project", "Waiting on"]
ROWS: list[list[Any]] = [
    ["T-1", "Sign supplier contract", "Asha Rao", "Done", "2026-10-01", "Launch", ""],
    ["T-2", "Configure payment gateway", "Vikram", "Blocked", "25/10/2026", "Launch", "T-3"],
    ["T-3", "Get gateway API keys", "", "In progress", "05/06/2026", "Launch", "T-1, T-9"],
    ["T-4", "Write onboarding emails", "Asha Rao", "Someday", "12 Nov 2026", "Marketing", ""],
    ["", "Row without an id", "Vikram", "Open", "", "Launch", ""],
    ["T-2", "Duplicate of T-2", "Vikram", "Open", "", "Launch", ""],
    [],
]
MAPPING = TaskMapping(
    id="Task ID",
    name="Task",
    owner="Owner",
    status="Status",
    due="Due",
    project="Project",
    blocked_by="Waiting on",
)


def _request(**overrides: Any) -> TaskTableImport:
    fields: dict[str, Any] = {
        "source_system": "google_sheets",
        "document_id": "1AbCdEf",
        "document_name": "Launch tracker",
        "table": "Tasks",
        "uri": "https://docs.google.com/spreadsheets/d/1AbCdEf",
        "header": HEADER,
        "rows": ROWS,
        "mapping": MAPPING,
        "observed_at": datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    }
    return TaskTableImport(**(fields | overrides))


def test_dates_are_read_only_when_unambiguous() -> None:
    assert parse_due("2026-10-01") == date(2026, 10, 1)
    assert parse_due("25/10/2026") == date(2026, 10, 25)
    assert parse_due("10/25/2026") == date(2026, 10, 25)
    assert parse_due("12 Nov 2026") == date(2026, 11, 12)
    assert parse_due("Nov 12, 2026") == date(2026, 11, 12)
    assert parse_due("05/06/2026") is None  # May 6 or 5 June: not guessed
    assert parse_due("31/02/2026") is None
    assert parse_due("next week") is None


def test_dry_run_reports_gaps_instead_of_guessing() -> None:
    report = import_task_table(FakeStore(), "x", _request(dry_run=True))
    assert not report.written
    assert (report.tasks, report.people, report.projects) == (4, 2, 2)
    assert report.skipped_rows == 2
    text = "\n".join(report.warnings)
    assert "status 'Someday' not recognised" in text
    assert "due date '05/06/2026' is ambiguous" in text
    assert "blocked by 'T-9', which is not an id in this table" in text
    assert "no id in 'Task ID'" in text
    assert "'T-2' appears more than once" in text
    # links: 3 owners (T-3 has none), 4 project memberships, 2 blockers (T-2->T-3, T-3->T-1)
    assert report.links == 9


def test_unknown_mapped_column_is_rejected() -> None:
    with pytest.raises(ValueError, match="not in the header"):
        import_task_table(FakeStore(), "x", _request(mapping=TaskMapping(id="Ticket", name="Task")))


def test_import_builds_a_traversable_graph_and_is_idempotent(
    real_store: FalkorDBStore, company: str, raw_graph: Any
) -> None:
    first = import_task_table(real_store, company, _request())
    assert first.written
    g = raw_graph(company)

    def counts() -> tuple[int, int]:
        nodes = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
        edges = g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
        return nodes, edges

    before = counts()
    import_task_table(real_store, company, _request())
    assert counts() == before  # same ids, nothing duplicated

    chain = g.ro_query(
        "MATCH (a:Ticket {id: 'ticket:t-2'})-[:BLOCKED_BY*1..2]->(b:Ticket) "
        "RETURN b.id ORDER BY b.id"
    ).result_set
    assert [r[0] for r in chain] == ["ticket:t-1", "ticket:t-3"]
    owner = g.ro_query(
        "MATCH (p:Person)-[o:OWNS]->(:Ticket {id: 'ticket:t-2'}) RETURN p.name, o.source_id"
    ).result_set
    assert owner[0][0] == "Vikram" and owner[0][1].startswith("source:")
    assert not g.ro_query(
        "MATCH (:Person)-[:OWNS]->(:Ticket {id: 'ticket:t-3'}) RETURN 1"
    ).result_set
    row = g.ro_query(
        "MATCH (:Ticket {id: 'ticket:t-2'})-[:SUPPORTED_BY]->(s:Source) "
        "RETURN s.source_record_id, s.uri, s.excerpt"
    ).result_set[0]
    assert row[0] == "Tasks!3" and row[1].startswith("https://docs.google.com/")
    assert "Status: Blocked" in row[2]
    status = g.ro_query(
        "MATCH (t:Ticket) RETURN t.id, t.status, t.due_date ORDER BY t.id"
    ).result_set
    assert status == [
        ["ticket:t-1", "done", "2026-10-01"],
        ["ticket:t-2", "blocked", "2026-10-25"],
        ["ticket:t-3", "in_progress", None],
        ["ticket:t-4", None, "2026-11-12"],
    ]
