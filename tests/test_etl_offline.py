"""
The MySQL -> Neo4j export, tested WITHOUT a Neo4j server: a recording stand-in
for the Neo4j session captures every statement the ETL would send.
"""
import re
from types import SimpleNamespace

import neo4j_etl as E
import neo4j_queries as NQ
import utils


class Tx:
    def __init__(self, log):
        self.log = log

    def run(self, cypher, params=None, **kw):
        self.log.append((cypher.strip(), dict(params or {}, **kw)))
        return self

    def consume(self):
        return None


class RecordingSession:
    def __init__(self):
        self.log = []

    def execute_write(self, fn):
        return fn(Tx(self.log))


def quiet(*_):
    pass


def run_all(conn, year):
    s = RecordingSession()
    E.apply_schema(s, quiet)
    E.load_nodes(s, conn, quiet)
    E.load_static_relationships(s, conn, quiet)
    E.load_timetable(s, conn, [year], all_years=False, log=quiet)
    E.derive_conflicts(s, quiet)
    return s.log


def rows_sent(log, query):
    return sum(len(p["rows"]) for q, p in log if q == query.strip() and "rows" in p)


def test_constraints_come_first_and_conflicts_last(conn, year):
    log = run_all(conn, year)
    kinds = ["schema" if q.startswith("CREATE") else "data" for q, _ in log]
    first_data = kinds.index("data")
    assert all(k == "schema" for k in kinds[:first_data]) and first_data >= 15
    assert "schema" not in kinds[first_data:]                 # nothing schema-related after data starts
    assert "CONFLICTS_WITH" in log[-1][0]                     # derived relationship is built last

    texts = [q for q, _ in log]
    last_node = max(i for i, q in enumerate(texts) if re.match(r"UNWIND \$rows AS row\s+MERGE \(n:", q))
    first_rel = min(i for i, q in enumerate(texts) if re.match(r"UNWIND \$rows AS row\s+MATCH", q))
    assert last_node < first_rel                              # nodes exist before relationships reference them


def test_rows_sent_match_mysql_counts(conn, year):
    log = run_all(conn, year)
    exp = E.expected_counts(conn, [year])
    for label, query in E.Q_NODES.items():
        assert rows_sent(log, query) == exp["nodes"][label], label
    for rel, query in E.Q_RELS.items():
        assert rows_sent(log, query) == exp["relationships"][rel], rel
    assert rows_sent(log, E.Q_SCHEDULED) == exp["relationships"]["SCHEDULED_IN"]


def test_deleted_mysql_rows_are_pruned_from_every_static_relationship(conn, year):
    """MERGE alone never deletes: each static type is followed by a prune keyed on MySQL's current rows."""
    log = run_all(conn, year)
    texts = [q for q, _ in log]
    exp = E.expected_counts(conn, [year])["relationships"]
    for rel, query in E.Q_RELS.items():
        last_batch = max(i for i, q in enumerate(texts) if q == query.strip())
        prune = texts.index(E.Q_PRUNE[rel].strip())
        assert prune == last_batch + 1, rel                    # right after its own (batched) load
        keys = log[prune][1]["keys"]
        assert len(keys) == exp[rel] and all(len(k) == 2 for k in keys), rel


def test_batches_never_exceed_the_batch_size(conn, year):
    assert max(len(p["rows"]) for _, p in run_all(conn, year) if "rows" in p) <= E.BATCH_SIZE
    s = RecordingSession()
    E._batched(s, "UNWIND $rows AS row RETURN row", [{"i": i} for i in range(1200)], "x", lambda *_: None)
    assert [len(p["rows"]) for _, p in s.log] == [500, 500, 200]


def test_old_timetable_is_deleted_before_reload(conn, year):
    texts = [q for q, _ in run_all(conn, year)]
    delete = next(i for i, q in enumerate(texts) if "DELETE s" in q)
    load = next(i for i, q in enumerate(texts) if "SCHEDULED_IN {timetable_id" in q)
    assert delete < load                                      # no ghost classes after a regeneration


def test_expected_conflict_pairs_follow_the_sql_definition(conn):
    graph = utils.build_conflict_graph(conn)
    exp = E.expected_counts(conn, [])
    assert exp["relationships"]["CONFLICTS_WITH"] == sum(len(v) for v in graph.values()) // 2 == 91
    assert graph["CS301"]["CS302"] == 25                      # 24 shared students + 1 shared faculty
    assert all(graph[a][b] == graph[b][a] for a in graph for b in graph[a])    # symmetric


def test_compare_counts_reports_each_difference():
    exp = {"nodes": {"Course": 30, "Faculty": 20}, "relationships": {"TEACHES": 62}}
    act = {"nodes": {"Course": 30, "Faculty": 19}, "relationships": {}}
    problems = E.compare_counts(exp, act)
    assert len(problems) == 2 and any("Faculty" in p for p in problems) and any("TEACHES" in p for p in problems)
    assert E.compare_counts(exp, {"nodes": exp["nodes"], "relationships": exp["relationships"]}) == []


def test_helpers():
    assert E.dept_code("Electronics and Communication Engineering") == "ECE"
    assert len(E.read_statements(E.CONSTRAINTS_FILE)) >= 15
    assert all(s.startswith("CREATE") for s in E.read_statements(E.CONSTRAINTS_FILE))


def test_single_year_run_verifies_every_year_the_graph_holds(monkeypatch, year):
    """
    Regression: `neo4j_etl.py --year Y` used to compare the whole graph with MySQL's
    counts for Y alone, so a second academic year in the graph caused a false MISMATCH.
    """
    class Session(RecordingSession):
        def execute_read(self, fn):            # graph_years() asks which years are in the graph
            rows = [{"y": "2001-2002"}, {"y": year}]
            return fn(SimpleNamespace(run=lambda *a, **k: SimpleNamespace(data=lambda: rows)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    driver = SimpleNamespace(verify_connectivity=lambda: None, session=lambda **kw: Session())
    monkeypatch.setattr(NQ, "get_driver", lambda: driver)
    seen = []
    empty = {"nodes": {}, "relationships": {}}
    monkeypatch.setattr(E, "expected_counts", lambda conn, years: seen.append(years) or empty)
    monkeypatch.setattr(E, "actual_counts", lambda session: empty)
    result = E.run_etl(year, log=quiet)
    assert result["ok"] and seen == [sorted({"2001-2002", year})]


def test_cli_explains_an_unreachable_neo4j(monkeypatch, capsys):
    NQ.close_driver()                           # drop any driver cached by earlier tests
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_PASSWORD", "irrelevant")
    assert E.main(["--year", "2001-2002"]) == 2
    assert "Could not use Neo4j" in capsys.readouterr().err
