import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from smart_cleaner import SmartCleaner, classify_combined


class Chat(SimpleNamespace):
    pass


class Channel(SimpleNamespace):
    pass


class FloodWaitError(Exception):
    seconds = 30


class FakeClient:
    def __init__(self, dialogs, fail_once=False): self.dialogs = dialogs; self.left = []; self.fail_once = fail_once; self.message_calls = 0
    async def iter_dialogs(self):
        for d in self.dialogs: yield d
    async def iter_messages(self, entity, limit=5):
        self.message_calls += 1
        for s in getattr(entity, 'samples', [])[:limit]:
            yield SimpleNamespace(message=s)
    async def delete_dialog(self, entity):
        if self.fail_once:
            self.fail_once = False
            raise FloodWaitError('FLOOD_WAIT_30')
        self.left.append(entity.id)


class SmartCleanerTests(unittest.TestCase):
    def test_catalog_query_indexes_and_worker_thread_read_connection(self):
        async def run():
            import threading
            with tempfile.TemporaryDirectory() as td:
                cleaner = SmartCleaner(td)
                try:
                    names = {row[1] for row in cleaner.db.execute("PRAGMA index_list(catalog)")}
                    self.assertIn("idx_catalog_sort", names)
                    self.assertIn("idx_catalog_peer_sort", names)
                    self.assertIn("idx_catalog_category_sort", names)
                    self.assertIn("idx_catalog_category_peer_sort", names)

                    sort_plan = cleaner.db.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM catalog "
                        "ORDER BY is_archived ASC, unread_count DESC, title COLLATE NOCASE "
                        "LIMIT ? OFFSET ?", (100, 0),
                    ).fetchall()
                    filtered_plan = cleaner.db.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM catalog "
                        "WHERE category=? AND peer_type=? "
                        "ORDER BY is_archived ASC, unread_count DESC, title COLLATE NOCASE "
                        "LIMIT ? OFFSET ?", ("crypto", "group", 100, 0),
                    ).fetchall()
                    sort_details = " ".join(str(row[3]) for row in sort_plan)
                    filtered_details = " ".join(str(row[3]) for row in filtered_plan)
                    self.assertIn("idx_catalog_sort", sort_details)
                    self.assertIn("idx_catalog_category_peer_sort", filtered_details)

                    result = await asyncio.to_thread(cleaner.list_catalog, category="crypto", peer_type="group")
                    self.assertEqual(result, {"total": 0, "items": []})
                    stats = await asyncio.to_thread(cleaner.catalog_stats)
                    self.assertEqual(stats["total"], 0)
                finally:
                    cleaner.close()
        asyncio.run(run())

    def test_compact_catalog_omits_full_text_but_keeps_panel_fields(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=81, title="Bitcoin crypto traders", username="btc_chat",
                              creator=False, admin_rights=None, broadcast=False, samples=[])
                dialog = SimpleNamespace(id=-81, entity=entity, name="Bitcoin crypto traders",
                                          unread_count=2, date=None, archived=False)
                cleaner = SmartCleaner(td)
                try:
                    await cleaner.scan(FakeClient([dialog]))
                    full = cleaner.list_catalog()["items"][0]
                    compact = cleaner.list_catalog(compact=True)["items"][0]
                    self.assertIn("about", full)
                    self.assertIn("sample_text", full)
                    self.assertIn("scores", full)
                    self.assertNotIn("about", compact)
                    self.assertNotIn("sample_text", compact)
                    self.assertNotIn("scores", compact)
                    self.assertEqual(compact["chat_id"], -81)
                    self.assertEqual(compact["title"], "Bitcoin crypto traders")
                    self.assertEqual(compact["username"], "btc_chat")
                    self.assertIn("protected", compact)
                    self.assertIn("is_scam", compact)
                finally:
                    cleaner.close()
        asyncio.run(run())

    def test_scan_reuses_about_and_message_samples_for_unchanged_dialogs(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=82, title="General discussion", username="general_chat",
                              creator=False, admin_rights=None, broadcast=False,
                              samples=["Bitcoin BTC wallet USDT"])
                dialog = SimpleNamespace(id=-82, entity=entity, name="General discussion",
                                          unread_count=0, date=None, archived=False)
                client = FakeClient([dialog])
                about_calls = 0

                async def get_full_chat_about(_entity):
                    nonlocal about_calls
                    about_calls += 1
                    return ""

                client.get_full_chat_about = get_full_chat_about
                cleaner = SmartCleaner(td)
                try:
                    await cleaner.scan(client)
                    second = await cleaner.scan(client)
                    self.assertEqual(about_calls, 1)
                    self.assertEqual(client.message_calls, 1)
                    self.assertGreaterEqual(second["cache_hits"], 2)
                    self.assertEqual(cleaner.list_catalog()["items"][0]["category"], "crypto")
                finally:
                    cleaner.close()
        asyncio.run(run())

    def test_scan_progress_callback_reports_seen_and_saved_counts(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                dialogs = []
                for chat_id in (91, 92):
                    entity = Chat(id=chat_id, title=f"Crypto traders {chat_id}", username="",
                                  creator=False, admin_rights=None, broadcast=False, samples=[])
                    dialogs.append(SimpleNamespace(id=-chat_id, entity=entity,
                                                    name=entity.title, unread_count=0,
                                                    date=None, archived=False))
                updates = []
                cleaner = SmartCleaner(td)
                try:
                    result = await cleaner.scan(FakeClient(dialogs),
                                                progress=lambda seen, saved: updates.append((seen, saved)))
                    self.assertEqual(result["dialogs_seen"], 2)
                    self.assertEqual(result["scanned"], 2)
                    self.assertEqual(updates[-1], (2, 2))
                finally:
                    cleaner.close()
        asyncio.run(run())

    def test_combined_classifier_uses_title_and_messages(self):
        category, confidence, scores = classify_combined('Обсуждение', '', '', ['Bitcoin BTC кошелёк USDT'])
        self.assertEqual(category, 'crypto')
        self.assertGreater(confidence, 0)
        self.assertIn('crypto', scores)


    def test_short_ton_substring_does_not_trigger_crypto(self):
        self.assertNotEqual(classify_combined('Button updates', '', '', [])[0], 'crypto')

    def test_ambiguous_title_uses_full_description_before_messages(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=51, title='General discussion', username='', creator=False,
                              admin_rights=None, broadcast=False, samples=['ordinary message'])
                dialog = SimpleNamespace(id=-51, entity=entity, name='General discussion', unread_count=0, date=None, archived=False)
                client = FakeClient([dialog])
                async def get_full_chat_about(_entity): return 'Bitcoin blockchain wallet and crypto news'
                client.get_full_chat_about = get_full_chat_about
                cleaner = SmartCleaner(td)
                await cleaner.scan(client)
                item = cleaner.list_catalog()['items'][0]
                self.assertEqual(item['category'], 'crypto')
                self.assertEqual(client.message_calls, 0)
                cleaner.close()
        asyncio.run(run())

    def test_floodwait_during_scan_returns_partial_catalog(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=52, title='General discussion', username='', creator=False,
                              admin_rights=None, broadcast=False, samples=[])
                dialog = SimpleNamespace(id=-52, entity=entity, name='General discussion', unread_count=0, date=None, archived=False)
                client = FakeClient([dialog])
                async def get_full_chat_about(_entity): raise FloodWaitError('FLOOD_WAIT_10')
                client.get_full_chat_about = get_full_chat_about
                cleaner = SmartCleaner(td)
                result = await cleaner.scan(client)
                self.assertTrue(result['partial'])
                self.assertIn('FloodWaitError', result['warning'])
                cleaner.close()
        asyncio.run(run())

    def test_uncertain_for_weak_evidence(self):
        self.assertEqual(classify_combined('Привет', '', '', [])[0], 'uncertain')

    def test_scan_protects_admin_and_whitelist(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                admin = Chat(id=10, title='Crypto Admin', username='admin', creator=False,
                                        admin_rights=object(), broadcast=False, samples=['Bitcoin BTC'])
                white = Chat(id=11, title='Family', username='', creator=False,
                                        admin_rights=None, broadcast=False, samples=[])
                other = Chat(id=12, title='Bitcoin traders', username='', creator=False,
                                        admin_rights=None, broadcast=False, samples=['USDT wallet'])
                dialogs = [SimpleNamespace(id=-10, entity=admin, name='Crypto Admin', unread_count=0, date=None, archived=False),
                           SimpleNamespace(id=-11, entity=white, name='Family', unread_count=0, date=None, archived=False),
                           SimpleNamespace(id=-12, entity=other, name='Bitcoin traders', unread_count=0, date=None, archived=False)]
                cleaner = SmartCleaner(td, whitelist=['Family'])
                result = await cleaner.scan(FakeClient(dialogs))
                self.assertEqual(result['scanned'], 3)
                rows = cleaner.list_catalog()['items']
                protected = {x['chat_id']: x['protected_reason'] for x in rows}
                self.assertEqual(protected[-10], 'admin')
                self.assertEqual(protected[-11], 'whitelist')
                self.assertEqual(next(x for x in rows if x['chat_id'] == -12)['category'], 'crypto')
                cleaner.close()
        asyncio.run(run())

    def test_preview_and_explicit_confirmation(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=21, title='Bitcoin traders', username='', creator=False,
                                         admin_rights=None, broadcast=False, samples=['BTC USDT crypto'])
                dialog = SimpleNamespace(id=-21, entity=entity, name='Bitcoin traders', unread_count=0, date=None, archived=False)
                client = FakeClient([dialog])
                cleaner = SmartCleaner(td)
                await cleaner.scan(client)
                preview = cleaner.preview(category='crypto')
                with self.assertRaises(ValueError):
                    await cleaner.execute(client, preview_id=preview['preview_id'], confirmation='YES')
                result = await cleaner.execute(client, preview_id=preview['preview_id'], confirmation='LEAVE 1')
                self.assertEqual(result['completed'], 1)
                self.assertEqual(client.left, [21])
                cleaner.close()
        asyncio.run(run())


    def test_floodwait_pauses_and_resume_reuses_existing_session(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=41, title='Bitcoin traders', username='', creator=False,
                              admin_rights=None, broadcast=False, samples=['BTC USDT crypto'])
                dialog = SimpleNamespace(id=-41, entity=entity, name='Bitcoin traders', unread_count=0, date=None, archived=False)
                client = FakeClient([dialog], fail_once=True)
                cleaner = SmartCleaner(td)
                await cleaner.scan(client)
                preview = cleaner.preview(category='crypto')
                first = await cleaner.execute(client, preview_id=preview['preview_id'], confirmation='LEAVE 1')
                self.assertEqual(first['status'], 'paused')
                self.assertEqual(first['remaining'], 1)
                resumed = cleaner.resume(first['job_id'])
                second = await cleaner.execute(client, preview_id=resumed['preview_id'], confirmation='LEAVE 1')
                self.assertEqual(second['completed'], 1)
                self.assertEqual(client.left, [41])
                cleaner.close()
        asyncio.run(run())

    def test_no_owned_group_is_previewable(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                entity = Chat(id=31, title='News', username='', creator=True,
                                         admin_rights=None, broadcast=False, samples=['news'])
                dialog = SimpleNamespace(id=-31, entity=entity, name='News', unread_count=0, date=None, archived=False)
                cleaner = SmartCleaner(td)
                await cleaner.scan(FakeClient([dialog]))
                with self.assertRaises(ValueError): cleaner.preview(category='all')
                cleaner.close()
        asyncio.run(run())


if __name__ == '__main__': unittest.main()