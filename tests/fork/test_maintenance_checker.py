"""
Unit tests of module/webui/maintenance_checker.py (fork).

Run from the repository root, no pytest needed:
    ./toolkit/python.exe -m tests.fork.test_maintenance_checker
or, with pytest installed:
    ./toolkit/python.exe -m pytest tests/fork

Importing module.* makes module.logger chdir into the repository root, so
every path below is absolute and the working directory is moved to a fresh
temporary directory right after the imports (CLAUDE.md hard rule 4).
"""
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

from module.webui.maintenance_checker import (
    END_GRACE_SECONDS,
    FINISHED_SHOW_SECONDS,
    JST,
    MaintenanceChecker,
    aside_label,
    derive,
    html_to_text,
    infer_year,
    parse_notice,
    parse_title,
    parse_window,
    toast_plan,
    window_text,
)

REPO_ROOT = os.path.abspath(os.getcwd())
SCRATCH = tempfile.mkdtemp(prefix="alas_fork_maint_test_")
os.chdir(SCRATCH)
SAMPLE = os.path.join(REPO_ROOT, "tests", "fork", "data", "news_sample.json")


def jst(*args):
    return datetime(*args, tzinfo=JST)


def sample_rows():
    with open(SAMPLE, encoding="utf-8") as f:
        return json.load(f)["data"]["rows"]


def fake_item(title, body, publish=jst(2026, 10, 6, 19, 30), item_id=900):
    return {
        "id": item_id,
        "title": title,
        "type": 3,
        "publishTime": int(publish.timestamp() * 1000),
        "content": f"<p>■実施時間</p><p>{body}</p><p>■対応内容</p>",
    }


def live_notice(start, end, finished=False, item_id=767):
    return {
        "id": item_id,
        "title": "メンテナンスのお知らせ",
        "note": "",
        "url": f"https://www.azurlane.jp/news/{item_id}",
        "publish": start - timedelta(days=2),
        "start": start,
        "end": end,
        "finished_by_title": finished,
        "extended": False,
    }


# ---------- parsing ----------

def test_sample_rows_are_finished_with_expected_windows():
    rows = sample_rows()
    assert len(rows) == 3
    expected = {
        767: (jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0)),
        766: (jst(2026, 9, 24, 14, 0), jst(2026, 9, 24, 20, 0)),
        765: (jst(2026, 9, 17, 14, 0), jst(2026, 9, 17, 20, 0)),
    }
    for item in rows:
        notice = parse_notice(item)
        assert notice is not None, item["id"]
        assert notice["finished_by_title"] is True
        assert (notice["start"], notice["end"]) == expected[notice["id"]], notice["id"]
        assert notice["url"].endswith(str(notice["id"]))


def test_title_variants():
    cases = [
        ("メンテナンスのお知らせ（完了）", True, False, "完了"),
        ("メンテナンスのお知らせ（完了 9/8 15:40 修正）", True, False, "完了 9/8 15:40 修正"),
        ("メンテナンスのお知らせ（完了 8/6 14:10 加筆）", True, False, "完了 8/6 14:10 加筆"),
        ("メンテナンスのお知らせ", False, False, ""),
        ("メンテナンスのお知らせ（完了）（修正）", True, False, "修正"),
        ("メンテナンス終了のお知らせ", True, False, ""),
        ("メンテナンスのお知らせ（延長）", False, True, "延長"),
    ]
    for title, finished, extended, note in cases:
        t = parse_title(title)
        assert t["finished"] is finished, title
        assert t["extended"] is extended, title
        assert t["note"] == note, (title, t["note"])


def test_window_regular_and_fullwidth_and_spacing():
    publish = jst(2026, 10, 6, 19, 30)
    bodies = [
        "10月8日(木)14:00 ～ 10月8日(木)20:00",
        "１０月８日(木)１４:００ ～ ２０:００",
        "10月8日 (木)14:00 ～ 20:00",
        "10月8日(木)14:00 – 20:00",
        "10月8日(木)14:00 〜 20:00",
        "10月8日(木)14:00-20:00",
    ]
    for body in bodies:
        window = parse_window(html_to_text(fake_item("x", body)["content"]), publish)
        assert window == (jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0)), body


def test_window_year_rollover_and_midnight_and_25h():
    # December post about a January window -> next year
    publish = jst(2025, 12, 30, 19, 0)
    window = parse_window(html_to_text(fake_item("x", "1月1日(木)14:00 ～ 1月1日(木)20:00")["content"]), publish)
    assert window == (jst(2026, 1, 1, 14, 0), jst(2026, 1, 1, 20, 0))
    # January correction of a window that started in December
    publish = jst(2026, 1, 2, 10, 0)
    window = parse_window(html_to_text(fake_item("x", "12月31日 22:00 ～ 1月1日 02:00")["content"]), publish)
    assert window == (jst(2025, 12, 31, 22, 0), jst(2026, 1, 1, 2, 0))
    # 25:00 = 01:00 next day
    publish = jst(2026, 10, 6, 19, 0)
    window = parse_window(html_to_text(fake_item("x", "10月8日(木)22:00 ～ 25:00")["content"]), publish)
    assert window == (jst(2026, 10, 8, 22, 0), jst(2026, 10, 9, 1, 0))
    # end before start without a date -> next day
    window = parse_window(html_to_text(fake_item("x", "10月8日(木)22:00 ～ 02:00")["content"]), publish)
    assert window == (jst(2026, 10, 8, 22, 0), jst(2026, 10, 9, 2, 0))


def test_window_missing_returns_none():
    text = html_to_text("<p>■実施時間</p><p>終了時間未定</p>")
    assert parse_window(text, jst(2026, 10, 6, 19, 0)) is None
    assert parse_notice(fake_item("メンテナンスのお知らせ", "終了時間未定")) is None
    assert parse_notice({"id": "bad"}) is None


def test_infer_year_prefers_closest():
    assert infer_year(1, 1, jst(2025, 12, 30)) == 2026
    assert infer_year(12, 31, jst(2026, 1, 2)) == 2025
    # No valid Feb 29 among ref.year-1..+1: falls back to ref.year (parse then fails safely)
    assert infer_year(2, 29, jst(2026, 3, 1)) == 2026
    assert infer_year(2, 29, jst(2024, 2, 27)) == 2024
    assert parse_notice(fake_item("x", "2月29日(木)14:00 ～ 20:00", publish=jst(2026, 2, 27, 19, 0))) is None


def test_extended_flag_from_body():
    item = fake_item("メンテナンスのお知らせ", "10月8日(木)14:00 ～ 22:00（延長）")
    assert parse_notice(item)["extended"] is True


# ---------- status ----------

def test_derive_boundaries():
    notice = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0))
    assert derive(notice, jst(2026, 10, 8, 12, 59), True) == ("scheduled", "scheduled")
    assert derive(notice, jst(2026, 10, 8, 13, 0), True) == ("scheduled", "soon")
    assert derive(notice, jst(2026, 10, 8, 14, 0), True) == ("in_progress", "in_progress")
    assert derive(notice, jst(2026, 10, 8, 20, 0), True) == ("in_progress", "in_progress")
    assert derive(notice, jst(2026, 10, 8, 20, 1), True) == ("in_progress", "overdue")
    grace = jst(2026, 10, 8, 20, 0) + timedelta(seconds=END_GRACE_SECONDS)
    assert derive(notice, grace, True) == ("in_progress", "overdue")
    assert derive(notice, grace + timedelta(seconds=1), True) == ("finished", "finished")
    show = jst(2026, 10, 8, 20, 0) + timedelta(seconds=FINISHED_SHOW_SECONDS)
    assert derive(notice, show, True) == ("finished", "finished")
    assert derive(notice, show + timedelta(seconds=1), True) == ("none", "none")
    done = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0), finished=True)
    assert derive(done, jst(2026, 10, 8, 15, 0), True) == ("finished", "finished")
    assert derive(None, jst(2026, 10, 8, 15, 0), False) == ("unknown", "loading")
    assert derive(None, jst(2026, 10, 8, 15, 0), True) == ("unknown", "unknown")
    assert derive(notice, jst(2026, 10, 8, 15, 0), True, supported=False) == ("unsupported", "unsupported")


def test_labels_and_window_text():
    notice = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0))
    now = jst(2026, 10, 8, 15, 0)
    assert aside_label("in_progress", "in_progress", notice, now).startswith("점검 중\n")
    assert "지남" in aside_label("in_progress", "overdue", notice, now)
    assert aside_label("scheduled", "soon", notice, now).startswith("곧 점검\n")
    assert aside_label("scheduled", "scheduled", notice, now).startswith("점검 예정\n")
    assert aside_label("finished", "finished", notice, now).startswith("점검 종료\n")
    assert aside_label("finished", "finished", notice, jst(2026, 10, 9, 9, 0)).endswith("점검 종료")
    assert aside_label("unknown", "loading", None, now) == "공지 확인 중"
    assert aside_label("unknown", "unknown", None, now) == "공지 확인 실패"
    assert aside_label("none", "none", None, now) == "점검 없음"
    assert aside_label("unsupported", "unsupported", None, now) == ""
    assert "(목)" in window_text(notice) and "~" in window_text(notice)


def test_toast_plan():
    e = "2026-10-08T20:00:00+09:00"
    assert toast_plan(None, (767, "in_progress", e)) is None
    assert toast_plan((767, "scheduled", e), -1) is None
    assert toast_plan(-1, (767, "in_progress", e)) is None
    assert toast_plan((767, "scheduled", e), (767, "in_progress", e)) == ("start", e)
    assert toast_plan((None, "unknown", None), (768, "in_progress", e)) == ("start", e)
    assert toast_plan((767, "in_progress", e), (767, "in_progress", "2026-10-08T21:00:00+09:00")) is None
    assert toast_plan((767, "finished", e), (767, "in_progress", "2026-10-08T21:00:00+09:00")) == (
        "extended", "2026-10-08T21:00:00+09:00")
    assert toast_plan((767, "in_progress", e), (767, "finished", e)) == ("finish", e)
    assert toast_plan((766, "finished", e), (767, "finished", e)) is None
    assert toast_plan((767, "scheduled", e), (767, "scheduled", e)) is None


# ---------- fetch selection ----------

class FetchStub(MaintenanceChecker):
    def __init__(self, rows):
        super().__init__(state_file=None)
        self.rows = rows
        self.instances = [{"name": "fake", "server": "若松", "push": "provider: null"}]

    def _get_rows(self):
        return self.rows


def test_latest_unparsable_live_notice_is_unknown_not_last_week():
    live = fake_item("メンテナンスのお知らせ", "終了時間未定", item_id=801)
    done = sample_rows()[0]
    checker = FetchStub([live, done])
    checker._fetch_notice()
    assert checker.notice is None
    assert checker.latest_unparsed["id"] == 801
    assert checker.error
    assert checker._derive(jst(2026, 10, 8, 15, 0)) == ("unknown", "unknown")


def test_latest_unparsable_finished_notice_falls_back():
    broken = fake_item("メンテナンスのお知らせ（完了）", "時間なし", item_id=802)
    done = sample_rows()[0]
    checker = FetchStub([broken, done])
    checker._fetch_notice()
    assert checker.notice is not None and checker.notice["id"] == 767
    assert checker.latest_unparsed is None


def test_fetch_failure_keeps_cache():
    checker = FetchStub([sample_rows()[0]])
    checker._fetch_notice()
    assert checker.notice["id"] == 767
    checker.rows = None
    checker._fetch_notice()
    assert checker.notice["id"] == 767


# ---------- observe / push ----------

class ObserveStub(MaintenanceChecker):
    def __init__(self, state_file):
        super().__init__(state_file=state_file)
        self.pushes = []
        self.instances = [{"name": "fake", "server": "若松", "push": "provider: null"}]
        self.clock = jst(2026, 10, 8, 12, 0)
        self.fetched_at = self.clock

    def _now(self):
        return self.clock

    def _push(self, kind):
        self.pushes.append((kind, self.notice["end"].isoformat() if self.notice else None))


def test_observe_transitions_and_dedup():
    state_file = os.path.join(SCRATCH, "fork_maintenance.json")
    c = ObserveStub(state_file)
    start, end = jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0)
    c.notice = live_notice(start, end)

    # Empty state file + already in progress: record only (deployment case)
    c.clock = jst(2026, 10, 8, 15, 0)
    c.observe()
    assert c.pushes == []
    assert json.load(open(state_file, encoding="utf-8"))["status"] == "in_progress"
    c.observe()
    assert c.pushes == []

    # A new notice first seen while running and already in progress: push
    c.notice = live_notice(jst(2026, 10, 8, 15, 30), jst(2026, 10, 8, 18, 0), item_id=768)
    c.clock = jst(2026, 10, 8, 16, 0)
    c.observe()
    assert c.pushes == [("start", "2026-10-08T18:00:00+09:00")]

    # Past the grace period: finished
    c.clock = jst(2026, 10, 8, 20, 1)
    c.observe()
    assert c.pushes[-1] == ("finish", "2026-10-08T18:00:00+09:00")

    # Extension notice moves the end back: extended, once
    c.notice["end"] = jst(2026, 10, 8, 21, 0)
    c.observe()
    c.observe()
    assert c.pushes[-1] == ("extended", "2026-10-08T21:00:00+09:00")
    assert len(c.pushes) == 3

    # Title gets the finished suffix: finish again for the new end
    c.notice["finished_by_title"] = True
    c.observe()
    c.observe()
    assert c.pushes[-1] == ("finish", "2026-10-08T21:00:00+09:00")
    assert len(c.pushes) == 4

    # The state file survives a restart and is no longer "fresh"
    c2 = ObserveStub(state_file)
    assert c2._record["notice_id"] == 768
    assert "finished@2026-10-08T21:00:00+09:00@0" in c2._record["pushed"]


def test_observe_fresh_then_scheduled_to_in_progress_pushes():
    state_file = os.path.join(SCRATCH, "fork_maintenance_2.json")
    c = ObserveStub(state_file)
    c.notice = live_notice(jst(2026, 10, 15, 14, 0), jst(2026, 10, 15, 20, 0), item_id=770)
    c.clock = jst(2026, 10, 14, 9, 0)
    c.observe()  # fresh: scheduled recorded, nothing pushed
    c.clock = jst(2026, 10, 15, 14, 0, 30)
    c.observe()
    assert c.pushes == [("start", "2026-10-15T20:00:00+09:00")]
    c.notice["finished_by_title"] = True
    c.clock = jst(2026, 10, 15, 19, 50)
    c.observe()
    assert c.pushes[-1][0] == "finish"


def test_has_provider_handles_bad_yaml():
    assert MaintenanceChecker._has_provider("provider: null") is False
    assert MaintenanceChecker._has_provider("") is False
    assert MaintenanceChecker._has_provider("provider: telegram\ntoken: x") is True
    assert MaintenanceChecker._has_provider("provider: [unclosed") is False
    assert MaintenanceChecker._has_provider("---\nprovider: bark\n---\nkey: v\n") is True


def test_observe_never_raises():
    c = ObserveStub(os.path.join(SCRATCH, "fork_maintenance_3.json"))
    c.notice = {"id": 1}  # broken notice
    c.observe()  # swallowed
    assert c.snapshot()[0] == "unknown"
    assert c.toast_state()[1] == "unknown"


# ---------- status API (stage C) ----------

def test_derive_api():
    notice = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0))
    # API says down before the announced start: already in progress
    assert derive(notice, jst(2026, 10, 8, 13, 50), True, api_down=True) == ("in_progress", "in_progress")
    # API still down past the grace period: stays overdue, not finished
    late = jst(2026, 10, 8, 20, 0) + timedelta(seconds=END_GRACE_SECONDS + 600)
    assert derive(notice, late, True, api_down=True) == ("in_progress", "overdue")
    assert derive(notice, late, True) == ("finished", "finished")
    # API saw the server come back: finished whatever the clock says
    assert derive(notice, jst(2026, 10, 8, 19, 56), True, api_finished_at=jst(2026, 10, 8, 19, 54)) == (
        "finished", "finished")
    # Title suffix wins over a down reading
    done = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0), finished=True)
    assert derive(done, jst(2026, 10, 8, 15, 0), True, api_down=True) == ("finished", "finished")


class ApiStub(ObserveStub):
    def __init__(self, state_file, answers=()):
        super().__init__(state_file)
        self.answers = list(answers)
        self.calls = 0
        self.api_server = "若松"
        self.swap_notice_during_http = None

    def _fetch_api_state(self, server):
        self.calls += 1
        if self.swap_notice_during_http is not None:
            self.notice = self.swap_notice_during_http
        return self.answers.pop(0) if self.answers else None


def epoch(dt):
    return int(dt.timestamp())


def test_poll_api_end_detection_extension_and_second_end():
    state_file = os.path.join(SCRATCH, "fork_maintenance_api.json")
    c = ApiStub(state_file)
    start, end = jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0)
    c.notice = live_notice(start, end)

    # Before the window: no HTTP at all
    c.clock = jst(2026, 10, 8, 13, 0)
    c.poll_api()
    assert c.calls == 0

    # First observation while running: recorded only (fresh)
    c.clock = jst(2026, 10, 8, 14, 5)
    c.observe()
    assert c.pushes == []

    # Down reading
    c.answers = [(1, epoch(jst(2026, 10, 8, 14, 4)))]
    c.poll_api()
    assert c.calls == 1 and c.api_state == 1 and c.api_seen_down_at == epoch(jst(2026, 10, 8, 14, 4))
    c.observe()
    assert c.pushes == []

    # Up again at 19:54 with a newer last_update: finished at that minute
    c.clock = jst(2026, 10, 8, 19, 55)
    c.answers = [(3, epoch(jst(2026, 10, 8, 19, 54)))]
    c.poll_api()
    assert c.finished_at == jst(2026, 10, 8, 19, 54)
    assert c._derive(c.clock) == ("finished", "finished")
    c.observe()
    assert c.pushes[-1][0] == "finish"
    assert c.snapshot()[1] == "점검 종료\n19:54"
    saved = json.load(open(state_file, encoding="utf-8"))
    assert saved["finished_at"] == jst(2026, 10, 8, 19, 54).isoformat() and saved["api_round"] == 0

    # Down again at 20:09: extension, new round, extended push
    c.clock = jst(2026, 10, 8, 20, 10)
    c.answers = [(1, epoch(jst(2026, 10, 8, 20, 9)))]
    c.poll_api()
    assert c.finished_at is None and c.api_round == 1
    c.observe()
    assert c.pushes[-1][0] == "extended"
    assert c._derive(c.clock) == ("in_progress", "overdue")

    # Up again at 20:39: second finish push, not blocked by the first key
    c.clock = jst(2026, 10, 8, 20, 40)
    c.answers = [(3, epoch(jst(2026, 10, 8, 20, 39)))]
    c.poll_api()
    c.observe()
    assert [p[0] for p in c.pushes] == ["finish", "extended", "finish"]

    # Past end + grace: polling stops
    c.clock = end + timedelta(seconds=END_GRACE_SECONDS + 60)
    calls = c.calls
    c.poll_api()
    assert c.calls == calls

    # An up reading not newer than the down reading is ignored
    c2 = ApiStub(os.path.join(SCRATCH, "fork_maintenance_api2.json"))
    c2.notice = live_notice(start, end)
    c2.clock = jst(2026, 10, 8, 14, 5)
    c2.answers = [(1, 1000), (3, 1000)]
    c2.poll_api()
    c2.poll_api()
    assert c2.finished_at is None


def test_poll_api_drops_answer_when_notice_changed():
    c = ApiStub(os.path.join(SCRATCH, "fork_maintenance_api3.json"))
    c.notice = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0))
    c.clock = jst(2026, 10, 8, 15, 0)
    c.answers = [(1, epoch(jst(2026, 10, 8, 14, 59)))]
    c.swap_notice_during_http = live_notice(jst(2026, 10, 15, 14, 0), jst(2026, 10, 15, 20, 0), item_id=770)
    c.poll_api()
    assert c.api_seen_down_at is None and c.api_state is None


def test_state_file_api_roundtrip():
    state_file = os.path.join(SCRATCH, "fork_maintenance_api4.json")
    c = ApiStub(state_file)
    start, end = jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0)
    c.notice = live_notice(start, end)
    c.clock = jst(2026, 10, 8, 14, 5)
    c.observe()
    c.answers = [(1, epoch(jst(2026, 10, 8, 14, 4))), (3, epoch(jst(2026, 10, 8, 19, 54)))]
    c.poll_api()
    c.clock = jst(2026, 10, 8, 19, 55)
    c.poll_api()
    assert c.finished_at is not None

    # Same (id, end) after a restart: restored
    c2 = ApiStub(state_file)
    c2._set_notice(live_notice(start, end))
    assert c2.finished_at == jst(2026, 10, 8, 19, 54) and c2.api_seen_down_at == epoch(jst(2026, 10, 8, 14, 4))
    # Moved end: new round, nothing restored
    c3 = ApiStub(state_file)
    c3._set_notice(live_notice(start, jst(2026, 10, 8, 21, 0)))
    assert c3.finished_at is None and c3.api_seen_down_at is None and c3.api_round == 0
    # Old-format file without the API keys
    old = os.path.join(SCRATCH, "fork_maintenance_old.json")
    with open(old, "w", encoding="utf-8") as f:
        json.dump({"notice_id": 767, "status": "finished", "end": end.isoformat(), "pushed": []}, f)
    c4 = ApiStub(old)
    c4._set_notice(live_notice(start, end))
    assert c4.finished_at is None and c4.api_round == 0


def test_aside_label_finished_at():
    notice = live_notice(jst(2026, 10, 8, 14, 0), jst(2026, 10, 8, 20, 0))
    now = jst(2026, 10, 8, 20, 30)
    assert aside_label("finished", "finished", notice, now, finished_at=jst(2026, 10, 8, 19, 54)) == "점검 종료\n19:54"
    assert aside_label("finished", "finished", notice, now) == "점검 종료\n(예정 20:00)"
    assert aside_label("finished", "finished", notice, jst(2026, 10, 9, 9, 0), finished_at=jst(2026, 10, 8, 19, 54)).endswith(
        "점검 종료")


if __name__ == "__main__":
    failures = 0
    names = [n for n in list(globals()) if n.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(names) - failures}/{len(names)} passed, scratch {SCRATCH}")
    sys.exit(1 if failures else 0)
