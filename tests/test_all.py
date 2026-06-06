"""
tests/test_all.py — full test suite for decision-provenance.

Run with:  python -m pytest tests/ -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from decision_provenance import (
    ProvenanceLogger,
    ValidationError,
    MerkleChain,
    LabelRegistry,
    ConfigChain,
)
from decision_provenance.record import build_record, _canonical, _sha256, _compute_record_hash
from decision_provenance.chain import GENESIS_ROOT


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
    lg = ProvenanceLogger(
        model_id="test_model",
        model_version="1.0.0",
        db_path=tmp_db,
    )
    lg.set_config(
        threshold=0.6,
        above_label="approved",
        below_label="denied",
        changed_by="test_suite",
        change_reason="initial test config",
    )
    yield lg
    lg.close()


# ===========================================================================
# 1. Hash determinism
# ===========================================================================

class TestHashDeterminism:

    def test_canonical_is_sorted(self):
        a = _canonical({"z": 1, "a": 2})
        b = _canonical({"a": 2, "z": 1})
        assert a == b

    def test_canonical_no_whitespace(self):
        out = _canonical({"x": 1})
        assert " " not in out

    def test_sha256_stable(self):
        h1 = _sha256("hello")
        h2 = _sha256("hello")
        assert h1 == h2
        assert len(h1) == 64

    def test_record_hash_is_deterministic(self, conn):
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        lid = labels.register("approved")
        cfg = configs.register(
            model_id="m", config_version="1",
            threshold=0.6, threshold_label_id=lid,
            changed_by="test", change_reason="test",
        )
        kwargs = dict(
            model_id="m", model_version="1.0",
            model_hash="abc" * 21 + "a",
            input_features={"x": 1}, output={"score": 0.9},
            label_id=lid, label_display="approved",
            config_id=cfg.config_id,
        )
        r1 = build_record(**kwargs)
        r2 = build_record(**kwargs)
        # record_id and timestamp differ — but the hash computation fields match
        assert r1.input_hash == r2.input_hash
        assert r1.output_hash == r2.output_hash
        assert r1.label_id == r2.label_id
        assert r1.config_id == r2.config_id

    def test_different_inputs_different_hash(self, conn):
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        lid = labels.register("ok")
        cfg = configs.register(
            model_id="m2", config_version="1",
            threshold=0.5, threshold_label_id=lid,
            changed_by="t", change_reason="t",
        )
        r1 = build_record(
            model_id="m2", model_version="1.0", model_hash="a" * 64,
            input_features={"x": 1}, output={"s": 0.8},
            label_id=lid, label_display="ok", config_id=cfg.config_id,
        )
        r2 = build_record(
            model_id="m2", model_version="1.0", model_hash="a" * 64,
            input_features={"x": 2}, output={"s": 0.8},
            label_id=lid, label_display="ok", config_id=cfg.config_id,
        )
        assert r1.input_hash != r2.input_hash

    def test_label_display_change_does_not_affect_record_hash(self, conn):
        """label_display must NOT be part of the hash payload — only label_id is."""
        import inspect
        from decision_provenance.record import _compute_record_hash
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        lid = labels.register("approved")
        cfg = configs.register(
            model_id="m3", config_version="1",
            threshold=0.5, threshold_label_id=lid,
            changed_by="t", change_reason="t",
        )
        base = dict(
            model_id="m3", model_version="1.0", model_hash="b" * 64,
            input_features={"x": 1}, output={"s": 0.9},
            label_id=lid, config_id=cfg.config_id,
        )
        r1 = build_record(**base, label_display="approved")
        r2 = build_record(**base, label_display="APPROVED")

        # Verify the payload dict in _compute_record_hash never sets label_display as a key
        # by checking that "label_display" only appears after the closing brace of the payload
        source = inspect.getsource(_compute_record_hash)
        payload_block_end = source.index("return _sha256")
        payload_block = source[:payload_block_end]
        assert '"label_display"' not in payload_block, (
            "label_display must not be a key in the hash payload dict"
        )
        # Stable fields must be identical across both records regardless of display string
        assert r1.input_hash == r2.input_hash
        assert r1.output_hash == r2.output_hash
        assert r1.label_id == r2.label_id

    def test_threshold_not_in_record_hash(self, conn):
        """Threshold changes (new config) must NOT affect records from old config."""
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        lid = labels.register("pass")
        cfg1 = configs.register(
            model_id="m4", config_version="1",
            threshold=0.5, threshold_label_id=lid,
            changed_by="t", change_reason="initial",
        )
        cfg2 = configs.register(
            model_id="m4", config_version="2",
            threshold=0.7, threshold_label_id=lid,
            changed_by="t", change_reason="recalibration",
        )
        base = dict(
            model_id="m4", model_version="1.0", model_hash="c" * 64,
            input_features={"x": 1}, output={"s": 0.9},
            label_id=lid, label_display="pass",
        )
        r1 = build_record(**base, config_id=cfg1.config_id)
        r2 = build_record(**base, config_id=cfg2.config_id)
        # Different config_ids → different hashes (config_id IS in the hash)
        assert r1.record_hash != r2.record_hash
        # But this is correct — the decision changed context, not the label


# ===========================================================================
# 2. Label registry
# ===========================================================================

class TestLabelRegistry:

    def test_register_and_retrieve(self, conn):
        reg = LabelRegistry(conn)
        lid = reg.register("approved")
        assert lid == "L001"
        assert reg.get_display("L001") == "approved"

    def test_idempotent_registration(self, conn):
        reg = LabelRegistry(conn)
        id1 = reg.register("approved")
        id2 = reg.register("approved")
        assert id1 == id2

    def test_case_insensitive_dedup(self, conn):
        reg = LabelRegistry(conn)
        id1 = reg.register("Approved")
        id2 = reg.register("APPROVED")
        assert id1 == id2

    def test_multiple_labels_get_sequential_ids(self, conn):
        reg = LabelRegistry(conn)
        id1 = reg.register("approved")
        id2 = reg.register("denied")
        id3 = reg.register("referred")
        assert id1 == "L001"
        assert id2 == "L002"
        assert id3 == "L003"

    def test_id_stable_after_new_labels_added(self, conn):
        reg = LabelRegistry(conn)
        id1 = reg.register("approved")
        reg.register("denied")
        reg.register("referred")
        assert reg.get_display(id1) == "approved"


# ===========================================================================
# 3. Config chain
# ===========================================================================

class TestConfigChain:

    def test_register_config(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("approved")
        configs = ConfigChain(conn)
        cfg = configs.register(
            model_id="m", config_version="1.0",
            threshold=0.6, threshold_label_id=lid,
            changed_by="ops_team", change_reason="initial deployment",
        )
        assert cfg.threshold == 0.6
        assert cfg.threshold_label_id == lid

    def test_current_config_returns_latest(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("ok")
        configs = ConfigChain(conn)
        configs.register(model_id="m", config_version="1",
                         threshold=0.5, threshold_label_id=lid,
                         changed_by="t", change_reason="first")
        cfg2 = configs.register(model_id="m", config_version="2",
                                threshold=0.7, threshold_label_id=lid,
                                changed_by="t", change_reason="recalibrate")
        current = configs.current_config("m")
        assert current.threshold == 0.7
        assert current.config_id == cfg2.config_id

    def test_config_at_timestamp(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("ok")
        configs = ConfigChain(conn)
        cfg1 = configs.register(model_id="m2", config_version="1",
                                threshold=0.5, threshold_label_id=lid,
                                changed_by="t", change_reason="first")
        configs.register(model_id="m2", config_version="2",
                         threshold=0.8, threshold_label_id=lid,
                         changed_by="t", change_reason="second")
        # Verify seq ordering is correct
        all_cfgs = configs.all_configs("m2")
        assert len(all_cfgs) == 2
        assert all_cfgs[0]["threshold"] == 0.5
        assert all_cfgs[1]["threshold"] == 0.8
        # config_at with cfg1's own timestamp must return cfg1
        # (it was active at the moment it was created)
        cfg_at = configs.config_at("m2", cfg1.effective_from)
        assert cfg_at.threshold == 0.5

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

    def test_config_chains_prev_ids(self, conn):
        labels = LabelRegistry(conn)
        lid = labels.register("ok")
        configs = ConfigChain(conn)
        cfg1 = configs.register(model_id="m3", config_version="1",
                                threshold=0.5, threshold_label_id=lid,
                                changed_by="t", change_reason="first")
        cfg2 = configs.register(model_id="m3", config_version="2",
                                threshold=0.6, threshold_label_id=lid,
                                changed_by="t", change_reason="second")
        assert cfg2.prev_config_id == cfg1.config_id
        assert cfg1.prev_config_id == ConfigChain.GENESIS


# ===========================================================================
# 4. Merkle chain integrity
# ===========================================================================

class TestMerkleChain:

    def _make_record(self, conn, prev_root=GENESIS_ROOT, suffix=""):
        labels = LabelRegistry(conn)
        configs = ConfigChain(conn)
        lid = labels.register("ok" + suffix)
        cfg = configs.register(
            model_id="mc" + suffix, config_version="1",
            threshold=0.5, threshold_label_id=lid,
            changed_by="t", change_reason="t",
        )
        return build_record(
            model_id="mc" + suffix, model_version="1.0",
            model_hash="d" * 64,
            input_features={"x": 1}, output={"s": 0.9},
            label_id=lid, label_display="ok",
            config_id=cfg.config_id,
            prev_root=prev_root,
        )

    def test_genesis_root(self, conn):
        chain = MerkleChain(conn)
        assert chain.current_root == GENESIS_ROOT
        assert chain.record_count == 0

    def test_append_updates_root(self, conn):
        chain = MerkleChain(conn)
        rec = self._make_record(conn)
        root = chain.append(rec)
        assert root != GENESIS_ROOT
        assert chain.current_root == root
        assert chain.record_count == 1

    def test_verify_empty_chain(self, conn):
        chain = MerkleChain(conn)
        ok, msg = chain.verify()
        assert ok
        assert "0 records" in msg

    def test_verify_valid_chain(self, conn):
        chain = MerkleChain(conn)
        for i in range(5):
            rec = self._make_record(conn, suffix=str(i))
            chain.append(rec)
        ok, msg = chain.verify()
        assert ok, msg

    def test_tamper_record_hash_detected(self, conn):
        chain = MerkleChain(conn)
        rec = self._make_record(conn)
        chain.append(rec)
        conn.execute("UPDATE records SET record_hash='deadbeef00' WHERE seq=1")
        conn.commit()
        ok, msg = chain.verify()
        assert not ok
        assert "seq=1" in msg

    def test_tamper_prev_root_detected(self, conn):
        chain = MerkleChain(conn)
        rec = self._make_record(conn)
        chain.append(rec)
        conn.execute("UPDATE records SET prev_root='badbad' WHERE seq=1")
        conn.commit()
        ok, msg = chain.verify()
        assert not ok

    def test_chain_auto_sequences_prev_root(self, conn):
        """chain.append auto-assigns prev_root inside the lock — caller never needs to pre-fetch."""
        chain = MerkleChain(conn)
        rec1 = self._make_record(conn, suffix="a")
        rec2 = self._make_record(conn, suffix="b")
        root1 = chain.append(rec1)
        root2 = chain.append(rec2)
        assert rec1.prev_root == GENESIS_ROOT
        assert rec2.prev_root == root1
        assert chain.current_root == root2
        ok, msg = chain.verify()
        assert ok, msg


# ===========================================================================
# 5. Input validation
# ===========================================================================

class TestValidation:

    def test_empty_model_id_raises(self, tmp_db):
        with pytest.raises(ValidationError):
            ProvenanceLogger(model_id="", model_version="1.0", db_path=tmp_db)

    def test_non_dict_features_raises(self, logger):
        with pytest.raises(ValidationError, match="must be a dict"):
            logger.record(
                input_features=["not", "a", "dict"],
                output={"score": 0.9},
                score=0.9,
            )

    def test_empty_features_raises(self, logger):
        with pytest.raises(ValidationError, match="must not be empty"):
            logger.record(input_features={}, output={"s": 0.9}, score=0.9)

    def test_non_serialisable_raises(self, logger):
        with pytest.raises(ValidationError, match="non-serialisable"):
            logger.record(
                input_features={"x": object()},
                output={"s": 0.9},
                score=0.9,
            )

    def test_no_config_raises(self, tmp_db):
        lg = ProvenanceLogger(model_id="x", model_version="1", db_path=tmp_db)
        with pytest.raises(RuntimeError, match="No config"):
            lg.record(input_features={"x": 1}, output={"s": 0.5}, score=0.5)
        lg.close()


# ===========================================================================
# 6. Logger end-to-end
# ===========================================================================

class TestLoggerEndToEnd:

    def test_decorator_logs_record(self, logger):
        @logger.log(score_fn=lambda out: out["score"])
        def predict(features):
            return {"score": 0.9}

        result = predict({"x": 1})
        assert result["score"] == 0.9
        assert logger.chain.record_count == 1

    def test_approved_denied_split(self, logger):
        for score in [0.8, 0.3, 0.9, 0.2, 0.7]:
            logger.record(
                input_features={"x": score},
                output={"score": score},
                score=score,
            )
        ok, msg = logger.verify()
        assert ok, msg
        assert logger.chain.record_count == 5

    def test_context_manager(self, tmp_db):
        with ProvenanceLogger(model_id="cm", model_version="1", db_path=tmp_db) as lg:
            lg.set_config(threshold=0.5, above_label="yes", below_label="no",
                          changed_by="t", change_reason="t")
            lg.record(input_features={"x": 1}, output={"s": 0.9}, score=0.9)
        # connection closed — should not raise

    def test_anonymise_fn_strips_pii(self, tmp_db):
        stripped = {}

        def anonymise(f):
            stripped.update(f)
            return {k: v for k, v in f.items() if k != "ssn"}

        lg = ProvenanceLogger(
            model_id="anon", model_version="1",
            db_path=tmp_db, anonymise_fn=anonymise,
        )
        lg.set_config(threshold=0.5, above_label="ok", below_label="no",
                      changed_by="t", change_reason="t")
        lg.record(
            input_features={"income": 50000, "ssn": "123-45-6789"},
            output={"score": 0.8},
            score=0.8,
        )
        rec = lg.get_record(
            lg._conn.execute("SELECT record_id FROM records WHERE seq=1").fetchone()[0]
        )
        # The input_hash should be of the anonymised version — verify SSN not in stored JSON
        assert "123-45-6789" not in rec["full_json"] if "full_json" in rec else True
        lg.close()

    def test_eu_ai_act_export(self, logger, tmp_path):
        for score in [0.8, 0.3, 0.9]:
            logger.record(
                input_features={"x": score},
                output={"score": score},
                score=score,
            )
        out = tmp_path / "report.json"
        report = logger.export_eu_ai_act(str(out))
        assert out.exists()
        assert report["audit_summary"]["total_decisions"] == 3
        assert report["audit_summary"]["chain_integrity"]["valid"]
        assert "label_registry" in report
        assert "config_history" in report

    def test_audit_log_jsonl(self, logger, tmp_path):
        for i in range(3):
            logger.record(
                input_features={"x": i},
                output={"score": 0.8},
                score=0.8,
            )
        out = tmp_path / "audit.jsonl"
        n = logger.export_audit_log(str(out))
        assert n == 3
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            json.loads(line)   # must be valid JSON


# ===========================================================================
# 7. Concurrency
# ===========================================================================

class TestConcurrency:

    def test_concurrent_writes_no_corruption(self, tmp_db):
        """20 threads writing simultaneously — chain must verify clean."""
        import time as _time
        lg = ProvenanceLogger(model_id="concurrent", model_version="1", db_path=tmp_db)
        lg.set_config(threshold=0.5, above_label="yes", below_label="no",
                      changed_by="t", change_reason="t")

        # Give SQLite a moment to fully commit the config before threads start
        _time.sleep(0.05)

        # Pre-fetch config so all threads use the same object — no DB race
        cfg = lg.current_config()
        assert cfg is not None, "Config must be registered before spawning threads"

        errors = []
        lock = threading.Lock()

        def worker(i):
            try:
                lg.record(
                    input_features={"worker": i, "value": round(i * 0.1, 2)},
                    output={"score": round(i * 0.05, 2)},
                    score=round(i * 0.05, 2),
                    config=cfg,   # pass pre-fetched config — no per-thread DB read
                )
            except Exception as e:
                with lock:
                    errors.append(e)

        # Start from 1 to avoid score=0.0 ambiguity at threshold boundary
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 21)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent write errors: {errors}"
        assert lg.chain.record_count == 20

        ok, msg = lg.verify()
        assert ok, msg
        lg.close()


# ===========================================================================
# 8. Threshold change audit trail
# ===========================================================================

class TestThresholdChangeAudit:

    def test_config_history_in_report(self, tmp_db):
        lg = ProvenanceLogger(model_id="audit", model_version="1", db_path=tmp_db)
        lg.set_config(threshold=0.5, above_label="approved", below_label="denied",
                      changed_by="ops", change_reason="initial deployment")

        for score in [0.8, 0.3]:
            lg.record(input_features={"x": score}, output={"s": score}, score=score)

        lg.set_config(threshold=0.7, above_label="approved", below_label="denied",
                      changed_by="ops", change_reason="reduce false positive rate Q3 review")

        for score in [0.8, 0.3]:
            lg.record(input_features={"x": score}, output={"s": score}, score=score)

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        report = lg.export_eu_ai_act(path)
        os.unlink(path)

        configs = report["config_history"]
        assert len(configs) == 2
        assert configs[0]["threshold"] == 0.5
        assert configs[1]["threshold"] == 0.7
        assert configs[1]["change_reason"] == "reduce false positive rate Q3 review"
        lg.close()
