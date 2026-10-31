"""Project hygiene: files, documentation and configuration agree with each other."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["README.md", "requirements.txt", ".env.example", ".gitignore", "docker-compose.yml",
            "pytest.ini", "app/streamlit_app.py", "cypher/constraints.cypher", "cypher/queries.cypher",
            "docker/mysql/00_app_user.sh", "sql/01_schema.sql", "sql/02_migration_improvements.sql",
            "sql/03_seed_data.sql", "sql/04_views_procedures.sql", "src/db.py", "src/utils.py",
            "src/generator.py", "src/neo4j_etl.py", "src/neo4j_queries.py", "tools/make_seed.py"]


def test_required_files_are_present():
    missing = [p for p in REQUIRED if not (ROOT / p).is_file()]
    assert not missing, f"missing files (see README 'Project structure'): {missing}"


def test_env_example_documents_every_setting_the_code_reads():
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    code = "".join(p.read_text() for p in (ROOT / "src").glob("*.py")) + (ROOT / "docker-compose.yml").read_text()
    used = set(re.findall(r"""["'$\{]((?:MYSQL|NEO4J|ACADEMIC)_[A-Z_]+)""", code))
    assert used <= documented, f"not in .env.example: {sorted(used - documented)}"


def test_secrets_are_git_ignored_but_the_template_is_not():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines and "!.env.example" in lines and ".venv/" in lines


def test_requirements_cover_the_imports():
    reqs = (ROOT / "requirements.txt").read_text().lower()
    for pkg in ("mysql-connector-python", "neo4j", "pandas", "streamlit"):
        assert pkg in reqs


def test_seed_file_is_exactly_what_make_seed_generates(tmp_path):
    out = tmp_path / "seed.sql"
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_seed.py"), str(out)], check=True, capture_output=True)
    assert out.read_bytes() == (ROOT / "sql" / "03_seed_data.sql").read_bytes()


def test_readme_matches_the_code():
    text = (ROOT / "README.md").read_text()
    code = (ROOT / "src" / "generator.py").read_text() + (ROOT / "src" / "neo4j_etl.py").read_text()
    flags = set(re.findall(r"--[a-z][a-z-]+", text)) - {"--now", "--user", "--basedir", "--datadir",
                                                          "--python", "--silent", "--env-only"}
    assert {f for f in flags if f not in code} == set()                 # every documented option exists
    headings = {re.sub(r"[^\w\- ]", "", h.lower()).strip().replace(" ", "-")
                for h in re.findall(r"^#{1,4} (.+)$", text, re.M)}
    assert {l for l in re.findall(r"\]\(#([^)]+)\)", text) if l not in headings} == set()   # no dead links
    assert text.count("<details>") == text.count("</details>") and text.count("```") % 2 == 0
    for rel in re.findall(r"`((?:src|sql|cypher|app|tools|docker)/[\w./]+\.\w+)`", text):
        if "..." in rel:                       # placeholders like sql/05_...sql are examples, not files
            continue
        assert (ROOT / rel).exists(), f"README mentions missing file {rel}"
