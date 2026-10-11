"""Dependency-free SQL checks; do not substitute for rusqlite integration tests."""
from pathlib import Path
import re
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript((ROOT / "crates/iarchive-store/src/schema.sql").read_text())
        self.db.executescript((ROOT / "crates/iarchive-store/src/replica.sql").read_text())

    def tearDown(self):
        self.db.close()

    def test_embedded_sql_compiles(self):
        source = (ROOT / "crates/iarchive-store/src/lib.rs").read_text()
        queries = re.findall(r'"((?:SELECT|INSERT|UPDATE|DELETE) [^"\n]*)"', source)
        self.assertTrue(queries, "No embedded SQL was extracted")
        for query in queries:
            with self.subTest(query=query):
                count = max((int(n) for n in re.findall(r"\?(\d+)", query)), default=0)
                self.db.execute("EXPLAIN " + query, [None] * count)

    def test_schema_constraints(self):
        self.db.execute("INSERT INTO archive_cycle(id,source_root) VALUES('x','/synthetic')")
        self.db.execute("INSERT INTO file_item(id,cycle_id,relative_path,path_key,kind,byte_count,mtime_ns) VALUES(1,'x','same.jpg','same.jpg','image',10,'0')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO file_item(cycle_id,relative_path,path_key,kind,byte_count,mtime_ns) VALUES('x','same.jpg','same.jpg','image',10,'0')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO archive_cycle(id,source_root) VALUES('x','/different')")
        self.db.execute("INSERT INTO candidate_group(id,cycle_id,type,algorithm_version,explanation) VALUES(1,'x','exact','v1','review')")
        self.db.execute("INSERT INTO candidate_member(group_id,file_id) VALUES(1,1)")
        self.db.execute("DELETE FROM candidate_group WHERE id=1")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM candidate_member").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
