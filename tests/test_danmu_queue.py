import csv
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from danmu_queue import (
    OP_MESSAGE,
    PROTO_JSON,
    PROTO_ZLIB,
    DanmuMessage,
    GuardBuyEvent,
    QueueRecorder,
    build_auth_payload,
    build_packet,
    decode_json_body,
    danmu_fingerprint,
    extract_danmu,
    extract_danmu_guard_level,
    extract_danmu_medal_level,
    extract_guard_buy,
    extract_history_danmu,
    history_danmu_key,
    iter_packets,
    normalize_unix_timestamp,
    parse_cookie,
    sign_wbi_params,
)
from app import LocalDanmuQueueApp, QueueStore, parse_danmu_time


class DanmuQueueTests(unittest.TestCase):
    def test_extracts_danmu_from_zlib_packet(self) -> None:
        payload = {
            "cmd": "DANMU_MSG",
            "info": [[0, 1, 25, 16777215, 1786419000], "排队", [123456, "测试用户"], [], [], [], 3],
        }
        inner_packet = build_packet(json.dumps(payload, ensure_ascii=False), OP_MESSAGE, PROTO_JSON)
        outer_packet = build_packet(zlib.compress(inner_packet), OP_MESSAGE, PROTO_ZLIB)

        packets = list(iter_packets(outer_packet))
        self.assertEqual(len(packets), 1)

        danmu = extract_danmu(decode_json_body(packets[0].body))
        self.assertEqual(danmu, DanmuMessage(123456, "测试用户", "排队", 1786419000, 3))

    def test_extracts_guard_level_from_fan_medal_block(self) -> None:
        fan_medal = [0, "牌子", "主播", 1, 0, 0, 0, 0, 0, 0, 3]
        payload = {
            "cmd": "DANMU_MSG",
            "info": [[0, 1, 25, 16777215, 1786419000], "准备街霸！", [3546903218227797, "今天不许后跳"], fan_medal],
        }

        self.assertEqual(extract_danmu_guard_level(payload["info"]), 3)
        danmu = extract_danmu(payload)
        self.assertEqual(danmu, DanmuMessage(3546903218227797, "今天不许后跳", "准备街霸！", 1786419000, 3))

    def test_extracts_medal_level_from_fan_medal_block(self) -> None:
        fan_medal = [32, "漂亮刘", "主播", 11113452, 0, "", 0, 0, 0, 0, 0]
        payload = {
            "cmd": "DANMU_MSG",
            "info": [[0, 1, 25, 16777215, 1786419000], "排队", [535118771, "前舰长用户"], fan_medal],
        }

        self.assertEqual(extract_danmu_medal_level(payload["info"]), 32)
        danmu = extract_danmu(payload)
        self.assertEqual(danmu, DanmuMessage(535118771, "前舰长用户", "排队", 1786419000, 0, 32))

    def test_extracts_guard_buy_event(self) -> None:
        payload = {
            "cmd": "GUARD_BUY",
            "data": {
                "uid": 123456,
                "username": "测试用户",
                "guard_level": 2,
                "num": 1,
                "price": 1998000,
            },
        }

        self.assertEqual(extract_guard_buy(payload), GuardBuyEvent(123456, "测试用户", 2, 1, 1998000))

    def test_extracts_history_danmu(self) -> None:
        item = {
            "text": "排队",
            "uid": 123456,
            "nickname": "测试用户",
            "timeline": "2026-08-11 21:47:41",
            "guard_level": 3,
            "medal": [34, "漂亮刘", "主播", 11113452, 0, "", 0, 0, 0, 0, 3],
            "id_str": "abc123",
            "check_info": {"ts": 1786456061, "ct": "C174BE58"},
        }

        danmu = extract_history_danmu(item)

        self.assertEqual(danmu, DanmuMessage(123456, "测试用户", "排队", 1786456061, 3, 34))
        self.assertEqual(history_danmu_key(item, danmu), "id:abc123")
        self.assertEqual(danmu_fingerprint(danmu), "123456|测试用户|排队|1786456061")

    def test_signs_wbi_params(self) -> None:
        signed = sign_wbi_params(
            {"id": 1881089677, "type": 0, "web_location": "444.8"},
            "0123456789abcdef0123456789abcdef",
            wts=1786419000,
        )

        self.assertEqual(signed["wts"], "1786419000")
        self.assertEqual(len(signed["w_rid"]), 32)
        self.assertEqual(
            signed,
            sign_wbi_params(
                {"web_location": "444.8", "type": 0, "id": 1881089677},
                "0123456789abcdef0123456789abcdef",
                wts=1786419000,
            ),
        )

    def test_builds_auth_payload_from_cookie(self) -> None:
        cookie = "SESSDATA=abc; DedeUserID=123456; buvid3=BV123; other=x"

        self.assertEqual(parse_cookie(cookie)["DedeUserID"], "123456")
        payload = build_auth_payload(1881089677, "token", cookie)
        self.assertEqual(payload["uid"], 123456)
        self.assertEqual(payload["buvid"], "BV123")
        self.assertEqual(payload["roomid"], 1881089677)
        self.assertEqual(payload["key"], "token")

    def test_normalizes_millisecond_danmu_timestamp(self) -> None:
        self.assertEqual(normalize_unix_timestamp(1786419000000), 1786419000)
        self.assertIn("2026", parse_danmu_time(1786419000000))

    def test_records_matching_danmu_once_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "queue.csv"
            recorder = QueueRecorder(output, allow_repeat=False)
            danmu = DanmuMessage(123456, "测试用户", "我要排队", 1786419000)

            self.assertTrue(recorder.should_record(danmu, "排队"))
            self.assertEqual(recorder.append(danmu), (True, 1))
            self.assertEqual(recorder.append(danmu), (False, None))

            with output.open("r", encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["queue_no"], "1")
            self.assertEqual(rows[0]["uid"], "123456")
            self.assertEqual(rows[0]["message"], "我要排队")

    def test_store_allows_historical_guard_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = QueueStore(Path(tmpdir) / "queue.db")
            settings = store.update_settings(
                {
                    "keyword": "排队",
                    "eligibility_mode": "historical",
                    "required_guard_level": 3,
                    "allow_repeat": False,
                }
            )
            danmu = DanmuMessage(123456, "测试用户", "我要排队", 1786419000, 0)

            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (False, None, "not_eligible"))
            store.upsert_guard(123456, "测试用户", 3, "guard_buy")
            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (True, 1, "queued"))
            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (False, None, "duplicate"))

    def test_historical_mode_ignores_required_guard_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = QueueStore(Path(tmpdir) / "queue.db")
            settings = store.update_settings(
                {
                    "keyword": "排队",
                    "eligibility_mode": "historical",
                    "required_guard_level": 1,
                    "allow_repeat": False,
                }
            )
            danmu = DanmuMessage(123456, "舰长用户", "排队", None, 0)

            store.upsert_guard(123456, "舰长用户", 3, "guard_buy")
            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (True, 1, "queued"))

    def test_former_captain_medal_record_allows_historical_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = QueueStore(Path(tmpdir) / "queue.db")
            settings = store.update_settings(
                {
                    "keyword": "排队",
                    "eligibility_mode": "historical",
                    "required_guard_level": 3,
                    "allow_repeat": False,
                }
            )
            danmu = DanmuMessage(535118771, "前舰长用户", "排队", None, 0, 32)

            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (False, None, "not_eligible"))
            store.upsert_guard(danmu.uid, danmu.uname, 3, "former_captain_medal")
            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (True, 1, "queued"))

    def test_high_medal_level_danmu_is_recorded_as_former_captain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = LocalDanmuQueueApp(Path(tmpdir) / "queue.db")
            app.store.update_settings(
                {
                    "keyword": "排队",
                    "eligibility_mode": "historical",
                    "required_guard_level": 3,
                    "allow_repeat": False,
                }
            )
            danmu = DanmuMessage(535118771, "前舰长用户", "排队", None, 0, 32)

            self.assertTrue(app._record_danmu(danmu, "danmu"))
            guard = app.store.list_guards(limit=1)[0]
            self.assertEqual(guard["uid"], 535118771)
            self.assertEqual(guard["best_guard_level"], 3)
            self.assertEqual(guard["source"], "former_captain_medal")
            self.assertEqual(app.store.list_queue()[0]["uid"], 535118771)

    def test_overlay_hiding_does_not_remove_export_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = QueueStore(Path(tmpdir) / "queue.db")
            settings = store.update_settings(
                {
                    "keyword": "排队",
                    "eligibility_mode": "all",
                    "allow_repeat": False,
                }
            )
            danmu = DanmuMessage(123456, "测试用户", "排队", None, 0)

            self.assertEqual(store.enqueue_if_allowed(danmu, settings), (True, 1, "queued"))
            self.assertEqual(len(store.list_overlay_queue()), 1)
            self.assertTrue(store.update_queue_note(1, "3-2"))
            self.assertEqual(store.list_queue()[0]["note"], "3-2")

            self.assertTrue(store.hide_overlay_queue_item(1))
            self.assertEqual(store.list_overlay_queue(), [])
            self.assertEqual(len(store.list_queue()), 1)
            self.assertIn("测试用户", store.export_queue_csv())
            self.assertIn("3-2", store.export_queue_csv())

            self.assertEqual(store.reset_overlay_queue(), 1)
            self.assertEqual(len(store.list_overlay_queue()), 1)

    def test_store_can_require_tidu_or_above(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = QueueStore(Path(tmpdir) / "queue.db")
            settings = store.update_settings(
                {
                    "keyword": "排队",
                    "eligibility_mode": "current",
                    "required_guard_level": 2,
                    "allow_repeat": False,
                }
            )

            captain = DanmuMessage(1001, "舰长用户", "排队", None, 3)
            admiral = DanmuMessage(1002, "提督用户", "排队", None, 2)
            self.assertEqual(store.enqueue_if_allowed(captain, settings), (False, None, "not_eligible"))
            self.assertEqual(store.enqueue_if_allowed(admiral, settings), (True, 1, "queued"))


if __name__ == "__main__":
    unittest.main()
