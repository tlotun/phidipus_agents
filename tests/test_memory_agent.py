# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tests/test_memory_agent.py — Memory Agent unit tests (no Ollama needed)

Run:  ./venv/bin/python -m unittest tests.test_memory_agent -v
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.memory_agent import MemoryAgent  # noqa: E402
from memory.memory_store import MemoryRecord, MemoryStore, fold  # noqa: E402
from memory.secret_filter import scrub  # noqa: E402

# Fake credentials for the secret-filter tests, assembled at runtime so the source never
# holds a provider-format token (GitHub secret scanning flags those even when fake).
FAKE_GOOGLE_KEY = "AIza" + "Sy" + "x" * 33
FAKE_GITHUB_TOKEN = "ghp" + "_" + "x" * 36
FAKE_OPENAI_KEY = "sk" + "-" + "x" * 32
FAKE_TELEGRAM_TOKEN = "123456789" + ":AA" + "x" * 33
FAKE_PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n-----END RSA " + "PRIVATE KEY-----"
FAKE_URL_CREDENTIALS = "postgres://admin" + ":hunter2" + "@db.local/prod"


class FakeEmbedder:
    """Deterministic bag-of-words embedding: cosine ≈ token overlap."""

    model = "fake-embed"

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(256, dtype=np.float32)
            for tok in re.findall(r"[0-9a-z]+", fold(t)):
                v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 256] += 1.0
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return out


class FakeLLM:
    def __init__(self, decision=None):
        self.decision = decision
        self.calls = 0

    def chat_json(self, system, user, max_tokens=300):
        self.calls += 1
        if callable(self.decision):
            return self.decision(user)
        return self.decision

    def chat(self, system, user, as_json=False, max_tokens=400):
        return "- " + user.splitlines()[0][:80] if user else None


def run(coro):
    return asyncio.run(coro)


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "mem.db")
        self.agent = MemoryAgent(self.store, embedder=FakeEmbedder(), llm=None,
                                 config={"llm_reconcile": False})
        self.agent.llm = None

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class StoreTests(MemoryTestCase):
    def test_fts_is_accent_insensitive(self):
        self.store.add(MemoryRecord(content="Báo cáo doanh số gửi sếp Minh mỗi sáng thứ 2"))
        hits = self.store.fts_search("bao cao doanh so")
        self.assertEqual(len(hits), 1)
        self.assertIn("Báo cáo", hits[0][0].content)

    def test_file_permissions_are_private(self):
        mode = (Path(self.tmp.name) / "mem.db").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_blocks_are_limited(self):
        text = self.store.set_block("user_profile", "x" * 5000)
        self.assertEqual(len(text), 1200)


class WritePathTests(MemoryTestCase):
    def test_add_and_recall(self):
        run(self.agent.remember("Sếp tên là Minh, làm giám đốc kinh doanh"))
        run(self.agent.remember("Báo cáo doanh số gửi qua email mỗi thứ Hai"))
        run(self.agent.remember("Tôi dùng Chrome profile 00Fujin cho Facebook"))
        hits = run(self.agent.recall("sep ten gi", k=2))
        self.assertTrue(hits)
        self.assertIn("Minh", hits[0].content)

    def test_duplicate_is_noop(self):
        a = run(self.agent.remember("Văn phòng ở quận 1"))
        b = run(self.agent.remember("văn phòng ở Quận 1!"))
        self.assertEqual(a["op"], "ADD")
        self.assertEqual(b["op"], "NOOP")
        self.assertEqual(self.store.count(), 1)

    def test_more_detail_updates_in_place(self):
        a = run(self.agent.remember("Sếp tên là Minh"))
        b = run(self.agent.remember("Sếp tên là Minh, làm giám đốc kinh doanh ở Hà Nội"))
        self.assertEqual(b["op"], "UPDATE")
        self.assertEqual(b["id"], a["id"])
        self.assertEqual(self.store.count(), 1)

    def test_changed_number_supersedes_with_history(self):
        a = run(self.agent.remember("Văn phòng ở tầng 5 tòa nhà ABC"))
        b = run(self.agent.remember("Văn phòng ở tầng 7 tòa nhà ABC"))
        self.assertEqual(b["op"], "INVALIDATE")
        self.assertEqual(b["replaced_id"], a["id"])
        current = run(self.agent.recall("văn phòng tầng mấy", k=3))
        self.assertEqual([r.content for r in current], ["Văn phòng ở tầng 7 tòa nhà ABC"])
        old = self.store.get(a["id"])
        self.assertFalse(old.is_valid)
        self.assertEqual(old.superseded_by, b["id"])
        history = run(self.agent.recall("văn phòng tầng", k=5, include_history=True))
        self.assertEqual(len(history), 2)

    def test_change_wording_supersedes_without_embeddings(self):
        self.agent.embedder = None
        a = run(self.agent.remember("Kho hàng chính ở Bình Dương"))
        b = run(self.agent.remember("Kho hàng chính chuyển về Long An"))
        self.assertEqual(b["op"], "INVALIDATE")
        self.assertEqual(b["replaced_id"], a["id"])

    def test_different_preferences_coexist(self):
        run(self.agent.remember("Tôi thích uống trà xanh"))
        b = run(self.agent.remember("Tôi thích cà phê sữa đá"))
        self.assertEqual(b["op"], "ADD")
        self.assertEqual(self.store.count(kind="preference"), 2)

    def test_kind_classification(self):
        self.assertEqual(MemoryAgent.classify_kind("Tên tôi là Lan"), "profile")
        self.assertEqual(MemoryAgent.classify_kind("Luôn gửi báo cáo dạng PDF"), "preference")
        self.assertEqual(MemoryAgent.classify_kind("Server backup nằm ở NAS phòng họp"), "fact")

    def test_secrets_are_rejected_or_redacted(self):
        r1 = run(self.agent.remember(FAKE_GOOGLE_KEY))
        self.assertEqual(r1["op"], "REJECT")
        r2 = run(self.agent.remember("mật khẩu wifi: abc12345"))
        self.assertEqual(r2["op"], "REJECT")
        r3 = run(self.agent.remember(
            "Gửi báo cáo tuần cho anh Minh qua Telegram, token: " + FAKE_GITHUB_TOKEN))
        self.assertEqual(r3["op"], "ADD")
        self.assertNotIn("ghp_", r3["content"])
        self.assertIn("github_token", r3["redacted"])

    def test_llm_decision_is_validated(self):
        a = run(self.agent.remember("Kho hàng chính của công ty ở Bình Dương"))
        self.agent.llm = FakeLLM({"op": "INVALIDATE", "target": a["id"], "content": "x"})
        b = run(self.agent.remember("Kho hàng chính của công ty chuyển về Long An"))
        self.assertEqual(b["op"], "INVALIDATE")
        self.assertEqual(b["by"], "llm")
        # hallucinated target id → ignored, heuristic used instead
        self.agent.llm = FakeLLM({"op": "UPDATE", "target": "deadbeef0000", "content": "y"})
        c = run(self.agent.remember("Kho hàng chính của công ty mở cửa từ 8h sáng"))
        self.assertNotEqual(c.get("by"), "llm")


class ObservationTests(MemoryTestCase):
    def test_episode_and_procedure(self):
        async def scenario():
            for _ in range(2):
                self.agent.observe_task_nowait(goal="tổng hợp tin tức về AI", success=True,
                                               method="workflow:wf_news_digest", duration_s=12)
            await self.agent._worker
        run(scenario())
        self.assertEqual(self.store.count(kind="episode"), 2)
        procs = self.store.list(kind="procedure")
        self.assertEqual(len(procs), 1)
        self.assertIn("thành công 2/2", procs[0].content)
        sug = self.agent.suggest_method("Tổng hợp tin tức về AI")
        self.assertEqual(sug["method"], "workflow:wf_news_digest")

    def test_observe_off(self):
        self.agent.set_observing(False)

        async def scenario():
            self.agent.observe_task_nowait(goal="mở chrome", success=True)
            await asyncio.sleep(0)
        run(scenario())
        self.assertEqual(self.store.count(kind="episode"), 0)

    def test_secret_goal_not_recorded(self):
        async def scenario():
            self.agent.observe_task_nowait(goal=FAKE_OPENAI_KEY, success=True)
            if self.agent._worker:
                await self.agent._worker
        run(scenario())
        self.assertEqual(self.store.count(kind="episode"), 0)


class ContextAndForgetTests(MemoryTestCase):
    def test_context_contains_blocks_and_memories(self):
        run(self.agent.remember("Tên tôi là Lan, làm kế toán"))
        run(self.agent.remember("Báo cáo doanh số luôn xuất ra file Excel"))
        self.store.set_block("user_profile", "- Lan, kế toán")
        ctx = run(self.agent.build_context("làm báo cáo doanh số"))
        self.assertIn("Lan, kế toán", ctx)
        self.assertIn("Excel", ctx)
        self.assertEqual(run(self.agent.build_context("báo cáo", for_cloud=True)), "")

    def test_forget_by_query_and_id(self):
        a = run(self.agent.remember("Mã khách hàng ABC là VIP"))
        run(self.agent.remember("Khách hàng XYZ thanh toán chậm"))
        res = run(self.agent.forget("khách hàng XYZ"))
        self.assertEqual(len(res["deleted"]), 1)
        self.assertEqual(self.store.count(), 1)
        res2 = run(self.agent.forget(a["id"]))
        self.assertEqual(res2["deleted"], [a["id"]])
        self.assertEqual(self.store.count(), 0)


class ConsolidationTests(MemoryTestCase):
    def test_prune_and_dedupe(self):
        old = time.time() - 200 * 86400
        for i in range(5):
            self.store.add(MemoryRecord(content=f"Thành công: việc cũ {i}", kind="episode",
                                        created_at=old, updated_at=old, valid_from=old))
        emb = FakeEmbedder()
        v = emb.embed(["Máy in ở phòng họp tầng 3"])[0]
        self.store.add(MemoryRecord(content="Máy in ở phòng họp tầng 3", embed_model="fake-embed"), v)
        self.store.add(MemoryRecord(content="Máy in: phòng họp, tầng 3", embed_model="fake-embed"), v)
        report = run(self.agent.consolidate())
        self.assertEqual(report["pruned"], 5)
        self.assertEqual(report["deduplicated"], 1)
        self.assertEqual(self.store.count(kind="fact"), 1)

    def test_core_block_refresh_without_llm(self):
        run(self.agent.remember("Tên tôi là Lan"))
        run(self.agent.remember("Tôi làm kế toán ở công ty Phượng Hoàng"))
        report = run(self.agent.consolidate())
        self.assertIn("user_profile", report["blocks"])
        self.assertIn("Lan", self.store.get_block("user_profile"))


class MCPServerTests(unittest.TestCase):
    def test_stdio_protocol(self):
        import json
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PHIDIPUS_MEMORY_DB=str(Path(tmp) / "mcp.db"),
                       PHIDIPUS_OLLAMA_URL="http://127.0.0.1:9")   # closed port → lexical only
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "memory_add", "arguments": {"content": "Kho hàng chính ở Bình Dương"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "memory_search", "arguments": {"query": "kho hang o dau"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "nope"},
            ]
            proc = subprocess.run(
                [sys.executable, "-m", "memory.mcp_server"],
                input="\n".join(json.dumps(m) for m in msgs) + "\n",
                capture_output=True, text=True, timeout=60, env=env,
                cwd=str(Path(__file__).resolve().parent.parent))
            lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
            by_id = {m["id"]: m for m in lines}
            self.assertEqual(len(lines), 5, proc.stderr[-500:])   # notification gets no reply
            self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("memory_search", [t["name"] for t in by_id[2]["result"]["tools"]])
            self.assertFalse(by_id[3]["result"]["isError"])
            self.assertIn("Bình Dương", by_id[4]["result"]["content"][0]["text"])
            self.assertEqual(by_id[5]["error"]["code"], -32601)


class SecretFilterTests(unittest.TestCase):
    def test_patterns(self):
        cases = {
            "thẻ 4111 1111 1111 1111 hết hạn 12/28": "card_number",
            "mã OTP là 482913": "otp",
            "bot " + FAKE_TELEGRAM_TOKEN: "telegram_token",
            FAKE_PRIVATE_KEY: "private_key",
            FAKE_URL_CREDENTIALS: "url_credentials",
        }
        for text, label in cases.items():
            clean, found = scrub(text)
            self.assertIn(label, found, text)
            self.assertIn("[ĐÃ ẨN]", clean)

    def test_normal_text_untouched(self):
        text = "Gọi cho anh Minh số 0901234567 lúc 9 giờ"   # phone numbers are not secrets
        self.assertEqual(scrub(text), (text, []))


if __name__ == "__main__":
    unittest.main()
