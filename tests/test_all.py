"""
tests/test_all.py — full test suite for decision-provenance v1.1.0

Run with: python -m pytest tests/ -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from decision_provenance import (
    ProvenanceLogger, ValidationError, MerkleChain,
    LabelRegistry, ConfigChain, GenesisChain,
)
from decision_provenance.record import build_record, _canonical, _sha256, _compute_record_hash
from decision_provenance.chain import GENESIS_ROOT
from decision_provenance.genesis import CURRENT_SCHEMA


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(tmp_db):
    c = sqlite3.connect(tmp_db, check_same_thread=False)
    yield c
    c.close()


@pytest.fixture
def logger(tmp_db):
    lg = ProvenanceLogger(model_id="test_model", model_version="1.0.0", db_path=tmp_db)
    lg.init_chain(changed_by="test_suite", reason="test initialisation")
    lg.set_config(
        threshold=0.6, above_label="approved", below_label="denied",
        changed_by="test_suite", change_reason="initial test config",
    )
    yield lg
    lg.close()


def _make_record(conn, suffix=""):
    labels  = LabelRegistry(conn)
    configs = ConfigChain(conn)
    genesis = GenesisChain(conn)
    lid = labels.register("ok" + suffix)
    cfg = configs.register(
        model_id="mc" + suffix, config_version="1",
        threshold=0.5, threshold_label_id=lid,
        changed_by="t", change_reason="t",
    )
    try:
        gen = genesis.init(model_id="mc" + suffix, created_by="t", reason="t")
    except RuntimeError:
        gen = genesis.current("mc" + suffix)
    return build_record(
        model_id="mc" + suffix, model_version="1.0", model_hash="d" * 64,
        input_features={"x": 1}, output={"s": 0.9},
        label_id=lid, label_display="ok",
        config_id=cfg.config_id,
        genesis_id=gen.genesis_id,
    )


# ===========================================================================
# 1. Genesis chain
# ===========================================================================

class TestGenesisChain:

    def test_init_creates_genesis(self, conn):
        g = GenesisChain(conn)
        rec = g.init(model_id="m", created_by="ops", reason="first deploy")
        assert rec.genesis_id
        assert rec.schema_version == CURRENT_SCHEMA
        assert rec.created_by == "ops"
        assert rec.migrated_from == ""

    def test_init_twice_raises(self, conn):
        g = GenesisChain(conn)
        g.init(model_id="m", created_by="ops", reason="first")
        with pytest.raises(RuntimeError, match="already exists"):
            g.init(model_id="m", created_by="ops", reason="second")

    def test_migrate_creates_new_segment(self, conn):
        g = GenesisChain(conn)
        g1 = g.init(model_id="m", created_by="ops", reason="first")
        g2 = g.migrate(model_id="m", changed_by="ops", reason="upgrade to v1.2")
        assert g2.migrated_from == g1.genesis_id
        assert g2.genesis_id != g1.genesis_id
        assert g.current("m").genesis_id == g2.genesis_id

    def test_empty_created_by_raises(self, conn):
        g = GenesisChain(conn)
        with pytest.raises(ValueError, match="created_by"):
            g.init(model_id="m", created_by="", reason="reason")

    def test_empty_reason_raises(self, conn):
        g = GenesisChain(conn)
        with pytest.raises(ValueError, match="reason"):
            g.init(model_id="m", created_by="ops", reason="")

    def test_genesis_hash_is_deterministic(self, conn):
        from decision_provenance.genesis import GenesisRecord, _compute_genesis_hash
        import time as t
        ts = "2026-01-01T00:00:00Z"
        rec = GenesisRecord(
            genesis_id="abc", schema_version="1.1", model_id="m",
            created_at=ts, created_by="ops", reason="test",
            migrated_from="", genesis_hash=""
        )
        h1 = _compute_genesis_hash(rec)
        h2 = _compute_genesis_hash(rec)
        assert h1 == h2
        assert len(h1) == 64


# ===========================================================================
# 2. Hash determinism (v1.1 — includes schema_version + genesis_id)
# ===========================================================================

class TestHashDeterminism:

    def test_canonical_is_sorted(self):
        assert _canonical({"z": 1, "a": 2}) == _canonical({"a": 2, "z": 1})

    def test_sha256_stable(self):
        assert _sha256("hello") == _sha256("hello")
        assert len(_sha256("hello")) == 64

    def test_schema_version_in_hash(self, conn):
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        genesis = GenesisChain(conn)
        lid = labels.register("ok")
        cfg = configs.register(model_id="m", config_version="1",
                               threshold=0.5, threshold_label_id=lid,
                               changed_by="t", change_reason="t")
        gen = genesis.init(model_id="m", created_by="t", reason="t")
        base = dict(model_id="m", model_version="1.0", model_hash="a"*64,
                    input_features={"x":1}, output={"s":0.9},
                    label_id=lid, label_display="ok",
                    config_id=cfg.config_id, genesis_id=gen.genesis_id)
        r1 = build_record(**base, schema_version="1.0")
        r2 = build_record(**base, schema_version="1.1")
        assert r1.record_hash != r2.record_hash, \
            "Different schema_versions must produce different hashes"

    def test_genesis_id_in_hash(self, conn):
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        genesis = GenesisChain(conn)
        lid = labels.register("ok2")
        cfg = configs.register(model_id="m2", config_version="1",
                               threshold=0.5, threshold_label_id=lid,
                               changed_by="t", change_reason="t")
        gen = genesis.init(model_id="m2", created_by="t", reason="t")
        base = dict(model_id="m2", model_version="1.0", model_hash="b"*64,
                    input_features={"x":1}, output={"s":0.9},
                    label_id=lid, label_display="ok",
                    config_id=cfg.config_id, schema_version=CURRENT_SCHEMA)
        r1 = build_record(**base, genesis_id=gen.genesis_id)
        r2 = build_record(**base, genesis_id="different-genesis-id-here-xxxx")
        assert r1.record_hash != r2.record_hash, \
            "Different genesis_ids must produce different hashes"

    def test_label_display_not_in_hash(self, conn):
        import inspect
        from decision_provenance.record import _compute_record_hash
        source = inspect.getsource(_compute_record_hash)
        payload_block = source[:source.index("return _sha256")]
        assert '"label_display"' not in payload_block

    def test_different_inputs_different_hash(self, conn):
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        genesis = GenesisChain(conn)
        lid = labels.register("x")
        cfg = configs.register(model_id="m3", config_version="1",
                               threshold=0.5, threshold_label_id=lid,
                               changed_by="t", change_reason="t")
        gen = genesis.init(model_id="m3", created_by="t", reason="t")
        base = dict(model_id="m3", model_version="1.0", model_hash="c"*64,
                    output={"s":0.9}, label_id=lid, label_display="x",
                    config_id=cfg.config_id, genesis_id=gen.genesis_id)
        r1 = build_record(**base, input_features={"x":1})
        r2 = build_record(**base, input_features={"x":2})
        assert r1.input_hash != r2.input_hash


# ===========================================================================
# 3. Label registry
# ===========================================================================

class TestLabelRegistry:

    def test_register_and_retrieve(self, conn):
        reg = LabelRegistry(conn)
        assert reg.register("approved") == "L001"
        assert reg.get_display("L001") == "approved"

    def test_idempotent_case_insensitive(self, conn):
        reg = LabelRegistry(conn)
        assert reg.register("Approved") == reg.register("APPROVED")

    def test_sequential_ids(self, conn):
        reg = LabelRegistry(conn)
        assert reg.register("approved") == "L001"
        assert reg.register("denied")   == "L002"
        assert reg.register("referred") == "L003"


# ===========================================================================
# 4. Config chain
# ===========================================================================

class TestConfigChain:

    def test_register_config(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("approved")
        configs = ConfigChain(conn)
        cfg = configs.register(model_id="m", config_version="1.0",
                               threshold=0.6, threshold_label_id=lid,
                               changed_by="ops", change_reason="initial")
        assert cfg.threshold == 0.6

    def test_current_returns_latest(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("ok")
        configs = ConfigChain(conn)
        configs.register(model_id="m", config_version="1",
                         threshold=0.5, threshold_label_id=lid,
                         changed_by="t", change_reason="first")
        cfg2 = configs.register(model_id="m", config_version="2",
                                threshold=0.7, threshold_label_id=lid,
                                changed_by="t", change_reason="second")
        assert configs.current_config("m").threshold == 0.7
        assert configs.current_config("m").config_id == cfg2.config_id

    def test_empty_change_reason_raises(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("ok")
        configs = ConfigChain(conn)
        with pytest.raises(ValueError, match="change_reason"):
            configs.register(model_id="m", config_version="1",
                             threshold=0.5, threshold_label_id=lid,
                             changed_by="t", change_reason="")

    def test_invalid_threshold_raises(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("ok")
        configs = ConfigChain(conn)
        with pytest.raises(ValueError, match="Threshold"):
            configs.register(model_id="m", config_version="1",
                             threshold=1.5, threshold_label_id=lid,
                             changed_by="t", change_reason="test")


# ===========================================================================
# 5. Merkle chain integrity
# ===========================================================================

class TestMerkleChain:

    def test_genesis_root(self, conn):
        chain = MerkleChain(conn)
        assert chain.current_root == GENESIS_ROOT
        assert chain.record_count == 0

    def test_append_updates_root(self, conn):
        chain = MerkleChain(conn)
        rec = _make_record(conn)
        root = chain.append(rec)
        assert root != GENESIS_ROOT
        assert chain.record_count == 1

    def test_verify_empty_chain(self, conn):
        ok, msg = MerkleChain(conn).verify()
        assert ok
        assert "0 records" in msg

    def test_verify_valid_chain(self, conn):
        chain = MerkleChain(conn)
        for i in range(5):
            chain.append(_make_record(conn, suffix=str(i)))
        ok, msg = chain.verify()
        assert ok, msg

    def test_tamper_record_hash_detected(self, conn):
        chain = MerkleChain(conn)
        chain.append(_make_record(conn))
        conn.execute("UPDATE records SET record_hash='deadbeef00' WHERE seq=1")
        conn.commit()
        ok, msg = chain.verify()
        assert not ok
        assert "seq=1" in msg

    def test_tamper_prev_root_detected(self, conn):
        chain = MerkleChain(conn)
        chain.append(_make_record(conn))
        conn.execute("UPDATE records SET prev_root='badbad' WHERE seq=1")
        conn.commit()
        ok, msg = chain.verify()
        assert not ok

    def test_chain_auto_sequences(self, conn):
        chain = MerkleChain(conn)
        rec1 = _make_record(conn, suffix="a")
        rec2 = _make_record(conn, suffix="b")
        root1 = chain.append(rec1)
        root2 = chain.append(rec2)
        assert rec1.prev_root == GENESIS_ROOT
        assert rec2.prev_root == root1
        assert chain.current_root == root2
        ok, msg = chain.verify()
        assert ok, msg


# ===========================================================================
# 6. Search and count
# ===========================================================================

class TestSearchAndCount:

    def test_search_by_label(self, logger):
        for score in [0.8, 0.3, 0.9, 0.2, 0.7]:
            logger.record(input_features={"x": score},
                          output={"score": score}, score=score)
        approved = logger.search(label_display="approved")
        denied   = logger.search(label_display="denied")
        assert len(approved) == 3
        assert len(denied)   == 2

    def test_count_matches_search(self, logger):
        for score in [0.8, 0.3, 0.9]:
            logger.record(input_features={"x": score},
                          output={"score": score}, score=score)
        assert logger.count() == 3
        assert logger.count(label_display="approved") == 2
        assert logger.count(label_display="denied")   == 1

    def test_search_pagination(self, logger):
        for i in range(10):
            logger.record(input_features={"x": i},
                          output={"score": 0.8}, score=0.8)
        page1 = logger.search(limit=4, offset=0)
        page2 = logger.search(limit=4, offset=4)
        page3 = logger.search(limit=4, offset=8)
        assert len(page1) == 4
        assert len(page2) == 4
        assert len(page3) == 2

    def test_search_by_genesis_id(self, logger):
        g = logger.genesis.current(logger.model_id)
        for i in range(3):
            logger.record(input_features={"x": i},
                          output={"score": 0.8}, score=0.8)
        results = logger.search(genesis_id=g.genesis_id)
        assert len(results) == 3
        results_wrong = logger.search(genesis_id="nonexistent-id")
        assert len(results_wrong) == 0


# ===========================================================================
# 7. Input validation
# ===========================================================================

class TestValidation:

    def test_empty_model_id_raises(self, tmp_db):
        with pytest.raises(ValidationError):
            ProvenanceLogger(model_id="", model_version="1.0", db_path=tmp_db)

    def test_record_without_init_chain_raises(self, tmp_db):
        lg = ProvenanceLogger(model_id="x", model_version="1", db_path=tmp_db)
        lg.set_config(threshold=0.5, above_label="ok", below_label="no",
                      changed_by="t", change_reason="t")
        with pytest.raises(RuntimeError, match="No genesis record"):
            lg.record(input_features={"x": 1}, output={"s": 0.5}, score=0.5)
        lg.close()

    def test_non_dict_features_raises(self, logger):
        with pytest.raises(ValidationError, match="must be a dict"):
            logger.record(input_features=["not", "a", "dict"],
                          output={"score": 0.9}, score=0.9)

    def test_empty_features_raises(self, logger):
        with pytest.raises(ValidationError, match="must not be empty"):
            logger.record(input_features={}, output={"s": 0.9}, score=0.9)

    def test_non_serialisable_raises(self, logger):
        with pytest.raises(ValidationError, match="non-serialisable"):
            logger.record(input_features={"x": object()},
                          output={"s": 0.9}, score=0.9)

    def test_no_config_raises(self, tmp_db):
        lg = ProvenanceLogger(model_id="y", model_version="1", db_path=tmp_db)
        lg.init_chain(changed_by="t", reason="t")
        with pytest.raises(RuntimeError, match="No config"):
            lg.record(input_features={"x": 1}, output={"s": 0.5}, score=0.5)
        lg.close()


# ===========================================================================
# 8. Logger end-to-end
# ===========================================================================

class TestLoggerEndToEnd:

    def test_decorator_logs_record(self, logger):
        @logger.log(score_fn=lambda out: out["score"])
        def predict(features):
            return {"score": 0.9}
        predict({"x": 1})
        assert logger.chain.record_count == 1

    def test_record_contains_genesis_id(self, logger):
        result = logger.record(input_features={"x": 1},
                               output={"s": 0.8}, score=0.8)
        g = logger.genesis.current(logger.model_id)
        assert result["genesis_id"] == g.genesis_id
        assert result["schema_version"] == CURRENT_SCHEMA

    def test_context_manager(self, tmp_db):
        with ProvenanceLogger(model_id="cm", model_version="1",
                              db_path=tmp_db) as lg:
            lg.init_chain(changed_by="t", reason="t")
            lg.set_config(threshold=0.5, above_label="yes", below_label="no",
                          changed_by="t", change_reason="t")
            lg.record(input_features={"x": 1}, output={"s": 0.9}, score=0.9)

    def test_anonymise_fn_applied(self, tmp_db):
        def anonymise(f):
            return {k: v for k, v in f.items() if k != "ssn"}
        lg = ProvenanceLogger(model_id="anon", model_version="1",
                              db_path=tmp_db, anonymise_fn=anonymise)
        lg.init_chain(changed_by="t", reason="t")
        lg.set_config(threshold=0.5, above_label="ok", below_label="no",
                      changed_by="t", change_reason="t")
        lg.record(input_features={"income": 50000, "ssn": "123-45-6789"},
                  output={"score": 0.8}, score=0.8)
        rec = lg.get_record(
            lg._conn.execute("SELECT record_id FROM records WHERE seq=1").fetchone()[0]
        )
        assert "123-45-6789" not in json.dumps(rec)
        lg.close()

    def test_eu_ai_act_export_includes_genesis(self, logger, tmp_path):
        for score in [0.8, 0.3, 0.9]:
            logger.record(input_features={"x": score},
                          output={"score": score}, score=score)
        out = tmp_path / "report.json"
        report = logger.export_eu_ai_act(str(out))
        assert out.exists()
        assert "genesis_history" in report
        assert len(report["genesis_history"]) == 1
        assert report["genesis_history"][0]["schema_version"] == CURRENT_SCHEMA
        assert report["audit_summary"]["chain_integrity"]["valid"]

    def test_audit_log_jsonl(self, logger, tmp_path):
        for i in range(3):
            logger.record(input_features={"x": i},
                          output={"score": 0.8}, score=0.8)
        out = tmp_path / "audit.jsonl"
        n = logger.export_audit_log(str(out))
        assert n == 3
        for line in out.read_text().strip().split("\n"):
            json.loads(line)

    def test_verify_chain(self, logger):
        for i in range(5):
            logger.record(input_features={"x": i},
                          output={"score": 0.8}, score=0.8)
        ok, msg = logger.verify()
        assert ok, msg


# ===========================================================================
# 9. Async support
# ===========================================================================

class TestAsyncSupport:

    def test_record_async(self, logger):
        async def run():
            return await logger.record_async(
                input_features={"x": 1},
                output={"score": 0.9},
                score=0.9,
            )
        result = asyncio.run(run())
        assert result["label_display"] == "approved"
        assert logger.chain.record_count == 1

    def test_log_async_decorator(self, logger):
        @logger.log_async(score_fn=lambda out: out["score"])
        async def predict_async(features):
            return {"score": 0.8}

        async def run():
            return await predict_async({"x": 1})

        result = asyncio.run(run())
        assert result["score"] == 0.8
        assert logger.chain.record_count == 1

    def test_multiple_async_records(self, logger):
        async def run():
            results = []
            for i in range(5):
                r = await logger.record_async(
                    input_features={"x": i},
                    output={"score": 0.8},
                    score=0.8,
                )
                results.append(r)
            return results

        results = asyncio.run(run())
        assert len(results) == 5
        assert logger.chain.record_count == 5
        ok, msg = logger.verify()
        assert ok, msg


# ===========================================================================
# 10. Concurrency
# ===========================================================================

class TestConcurrency:

    def test_concurrent_writes_no_corruption(self, tmp_db):
        import time as _time
        lg = ProvenanceLogger(model_id="concurrent", model_version="1",
                              db_path=tmp_db)
        lg.init_chain(changed_by="t", reason="t")
        lg.set_config(threshold=0.5, above_label="yes", below_label="no",
                      changed_by="t", change_reason="t")
        _time.sleep(0.05)

        # Pre-fetch both config and genesis so threads never hit the DB for lookups
        cfg = lg.current_config()
        gen = lg.genesis.current(lg.model_id)
        assert cfg is not None
        assert gen is not None

        # Pre-register labels so threads don't race on label registration
        yes_id = lg.labels.get_id("yes")
        no_id  = lg.labels.get_id("no")
        assert yes_id and no_id

        errors = []
        lock = threading.Lock()

        def worker(i):
            try:
                lg.record(
                    input_features={"worker": i, "value": round(i * 0.1, 2)},
                    output={"score": round(i * 0.05, 2)},
                    score=round(i * 0.05, 2),
                    config=cfg,
                )
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(1, 21)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Concurrent write errors: {errors}"
        assert lg.chain.record_count == 20
        ok, msg = lg.verify()
        assert ok, msg
        lg.close()


# ===========================================================================
# 11. Threshold change audit trail
# ===========================================================================

class TestThresholdChangeAudit:

    def test_config_history_in_report(self, tmp_db):
        lg = ProvenanceLogger(model_id="audit", model_version="1",
                              db_path=tmp_db)
        lg.init_chain(changed_by="ops", reason="production deployment")
        lg.set_config(threshold=0.5, above_label="approved",
                      below_label="denied", changed_by="ops",
                      change_reason="initial deployment")
        for score in [0.8, 0.3]:
            lg.record(input_features={"x": score},
                      output={"s": score}, score=score)
        lg.set_config(threshold=0.7, above_label="approved",
                      below_label="denied", changed_by="ops",
                      change_reason="Q3 review: reduce false positive rate")
        for score in [0.8, 0.3]:
            lg.record(input_features={"x": score},
                      output={"s": score}, score=score)

        import tempfile as tf, os
        with tf.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        report = lg.export_eu_ai_act(path)
        os.unlink(path)

        configs = report["config_history"]
        assert len(configs) == 2
        assert configs[0]["threshold"] == 0.5
        assert configs[1]["threshold"] == 0.7
        assert "Q3 review" in configs[1]["change_reason"]
        lg.close()


# ===========================================================================
# 12. Schema migration
# ===========================================================================

class TestSchemaMigration:

    def test_migrate_creates_new_genesis_segment(self, tmp_db):
        lg = ProvenanceLogger(model_id="migtest", model_version="1",
                              db_path=tmp_db)
        g1 = lg.init_chain(changed_by="ops", reason="v1.1 deploy")
        lg.set_config(threshold=0.5, above_label="ok", below_label="no",
                      changed_by="ops", change_reason="init")
        for i in range(3):
            lg.record(input_features={"x": i},
                      output={"s": 0.8}, score=0.8)
        g2 = lg.migrate_chain(changed_by="ops",
                               reason="upgrade to v1.2.0")
        assert g2.migrated_from == g1.genesis_id
        assert g2.genesis_id != g1.genesis_id
        assert len(lg.genesis.all_for_model("migtest")) == 2
        lg.close()

    def test_records_after_migration_use_new_genesis(self, tmp_db):
        lg = ProvenanceLogger(model_id="migtest2", model_version="1",
                              db_path=tmp_db)
        lg.init_chain(changed_by="ops", reason="initial")
        lg.set_config(threshold=0.5, above_label="ok", below_label="no",
                      changed_by="ops", change_reason="init")
        lg.record(input_features={"x": 1}, output={"s": 0.8}, score=0.8)

        g2 = lg.migrate_chain(changed_by="ops", reason="schema upgrade")
        lg.record(input_features={"x": 2}, output={"s": 0.8}, score=0.8)

        all_records = lg.search()
        assert all_records[0]["genesis_id"] != all_records[1]["genesis_id"]
        assert all_records[1]["genesis_id"] == g2.genesis_id

        ok, msg = lg.verify()
        assert ok, msg
        lg.close()
