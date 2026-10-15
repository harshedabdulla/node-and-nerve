"""Static guarantees about the fixed query templates (no database needed)."""

from __future__ import annotations

import re

import pytest

from company_brain import queries as q
from company_brain.graph import (
    FalkorDBStore,
    GraphConflict,
    GraphError,
    GraphTimeout,
    Query,
    _translate,
)
from company_brain.schema import Kind, Rel

ALL = q.all_queries()
LABELS = {k.label for k in Kind} | {"Entity"}
RELS = {r.value for r in Rel}
WRITE_WORDS = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DETACH|DROP|CALL)\b")


def test_query_names_are_unique() -> None:
    names = [query.name for query in ALL]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("query", ALL, ids=lambda x: x.name)
def test_read_queries_never_write(query: Query) -> None:
    if query.read_only:
        assert not WRITE_WORDS.search(query.cypher), query.name


@pytest.mark.parametrize("query", ALL, ids=lambda x: x.name)
def test_labels_and_types_come_from_the_schema(query: Query) -> None:
    for label in re.findall(r"\(\w*:([A-Za-z_:]+)", query.cypher):
        for part in label.split(":"):
            assert part in LABELS, (query.name, part)
    for rel_group in re.findall(r"\[\w*:([A-Z_|]+)", query.cypher):
        for rel in rel_group.split("|"):
            assert rel in RELS, (query.name, rel)


@pytest.mark.parametrize("query", ALL, ids=lambda x: x.name)
def test_variable_length_traversals_are_bounded(query: Query) -> None:
    for match in re.findall(r"\*[^\]]*", query.cypher):
        assert re.fullmatch(r"\*\d\.\.\d", match), (query.name, match)


@pytest.mark.parametrize("query", ALL, ids=lambda x: x.name)
def test_templates_are_fully_rendered(query: Query) -> None:
    # No leftover Python format fields such as {kind} or {rel.value}
    assert not re.search(r"\{[a-z_.]+\}", query.cypher), query.name


def test_unbounded_path_reads_have_limits() -> None:
    for query in q.CONNECTIONS.values():
        assert "LIMIT $limit" in query.cypher
    assert "LIMIT" in q.RESOLVE_ENTITIES.cypher
    assert "LIMIT" in q.TICKETS_IN_SCOPE.cypher


def test_store_refuses_raw_cypher_strings() -> None:
    store = FalkorDBStore.__new__(FalkorDBStore)
    with pytest.raises(TypeError):
        store.run("northstar", "MATCH (n) DETACH DELETE n")  # type: ignore[arg-type]


def test_superseded_decisions_are_excluded_inside_the_query() -> None:
    cypher = q.CURRENT_DECISIONS.cypher
    assert "SUPERSEDES" in cypher and "superseders = 0" in cypher


def test_database_errors_are_translated_without_leaking_details() -> None:
    import redis.exceptions as rex

    query = q.GET_ENTITY
    assert isinstance(_translate(rex.ResponseError("Query timed out"), query, "c"), GraphTimeout)
    assert isinstance(
        _translate(rex.ResponseError("unique constraint violation on node"), query, "c"),
        GraphConflict,
    )
    err = _translate(rex.ResponseError("errMsg: secret internals"), query, "c")
    assert type(err) is GraphError and "secret" not in str(err)
