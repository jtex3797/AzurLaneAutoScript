import html
import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
import yaml

from module.config.deep import deep_get
from module.config.server import VALID_SERVER_LIST
from module.config.utils import alas_instance, filepath_config, read_file
from module.logger import logger
from module.notify import handle_notify

# Official JP news API behind the JS-rendered https://www.azurlane.jp/news page.
# type 3 is the maintenance category; rows come newest first.
NEWS_API = "https://www.azurlane.jp/api/news/list"
NEWS_PARAMS = {"index": 1, "size": 3, "type": 3}
NEWS_URL = "https://www.azurlane.jp/news/{id}"
USER_AGENT = "ALAS-fork-maintenance-checker"
JST = timezone(timedelta(hours=9))
JP_PACKAGE = "com.YoStarJP.AzurLane"
# Seconds. check() fetches the notice, observe() derives the status and pushes.
FETCH_INTERVAL = 900
OBSERVE_INTERVAL = 60
SOON_SECONDS = 3600
# A notice without the "finished" title suffix stays in_progress this long
# past its announced end before it counts as finished (the suffix is edited
# by hand and extensions are announced late).
END_GRACE_SECONDS = 7200
FINISHED_SHOW_SECONDS = 86400
RECHECK_SECONDS = 60
STATE_FILE = "./log/fork_maintenance.json"

STATUSES = ("unsupported", "unknown", "none", "scheduled", "in_progress", "finished")

# After NFKC: full-width wave dash and digits become ASCII; U+301C does not.
WINDOW_RE = re.compile(
    r"(\d{1,2})月(\d{1,2})日(?:\s*\([^)]{1,3}\))?\s*(\d{1,2}):(\d{2})"
    r"\s*[~〜\-–—]\s*"
    r"(?:(\d{1,2})月(\d{1,2})日(?:\s*\([^)]{1,3}\))?\s*)?(\d{1,2}):(\d{2})"
)
TITLE_NOTE_RE = re.compile(r"\(([^()]*)\)\s*$")
FINISHED_RE = re.compile(r"完了|終了")
WEEKDAYS_KO = "월화수목금토일"


def html_to_text(content):
    """
    Args:
        content (str): HTML body of a notice.

    Returns:
        str: Plain text, NFKC normalised, tags turned into line breaks.
    """
    text = re.sub(r"<[^>]+>", "\n", content or "")
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    return text


def infer_year(month, day, ref):
    """
    The notice text has no year. Pick the year that puts (month, day)
    closest to `ref`, so a December post about 1月1日 lands in the next year
    and a January correction of a December window lands in the previous one.

    Args:
        month (int), day (int):
        ref (datetime): aware reference date.

    Returns:
        int: year
    """
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            candidate = datetime(year, month, day, tzinfo=ref.tzinfo)
        except ValueError:
            continue
        distance = abs((candidate - ref).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, year)
    return best[1] if best else ref.year


def _combine(year, month, day, hour, minute, tz):
    # "25:00" means 01:00 of the next day, so hours are added, not set
    return datetime(year, month, day, tzinfo=tz) + timedelta(hours=hour, minutes=minute)


def parse_window(text, publish):
    """
    Args:
        text (str): output of html_to_text()
        publish (datetime): aware publish time, the year reference.

    Returns:
        tuple[datetime, datetime] | None: (start, end) in JST.
    """
    pos = text.find("実施時間")
    m = None
    if pos >= 0:
        m = WINDOW_RE.search(text, pos, pos + 200)
    if m is None:
        m = WINDOW_RE.search(text)
    if m is None:
        return None
    m1, d1, h1, mi1, m2, d2, h2, mi2 = (int(x) if x is not None else None for x in m.groups())
    start = _combine(infer_year(m1, d1, publish), m1, d1, h1, mi1, JST)
    if m2 is not None:
        end = _combine(infer_year(m2, d2, start), m2, d2, h2, mi2, JST)
    else:
        end = _combine(start.year, start.month, start.day, h2, mi2, JST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def parse_title(title):
    """
    Returns:
        dict: text (NFKC title), note (last parenthesis content or ''),
            finished (bool), extended (bool)
    """
    text = unicodedata.normalize("NFKC", title or "")
    m = TITLE_NOTE_RE.search(text)
    return {
        "text": text,
        "note": m.group(1).strip() if m else "",
        "finished": bool(FINISHED_RE.search(text)),
        "extended": "延長" in text,
    }


def parse_notice(item):
    """
    Args:
        item (dict): one row of the news API.

    Returns:
        dict | None: id, title, note, url, publish, start, end,
            finished_by_title, extended. None if the window is missing.
    """
    try:
        title = parse_title(item.get("title"))
        publish = datetime.fromtimestamp(int(item["publishTime"]) / 1000, JST)
        text = html_to_text(item.get("content"))
        window = parse_window(text, publish)
        if window is None:
            return None
        pos = text.find("実施時間")
        near = text[pos:pos + 200] if pos >= 0 else ""
        return {
            "id": int(item["id"]),
            "title": title["text"],
            "note": title["note"],
            "url": NEWS_URL.format(id=int(item["id"])),
            "publish": publish,
            "start": window[0],
            "end": window[1],
            "finished_by_title": title["finished"],
            "extended": title["extended"] or "延長" in near,
        }
    except Exception:
        return None


def derive(notice, now, fetched, supported=True):
    """
    Pure status rule, see the plan. Markers refine the label and colour.

    Returns:
        tuple[str, str]: (status, marker)
    """
    if not supported:
        return "unsupported", "unsupported"
    if notice is None:
        return "unknown", ("unknown" if fetched else "loading")
    start, end = notice["start"], notice["end"]
    if now > end + timedelta(seconds=FINISHED_SHOW_SECONDS):
        return "none", "none"
    if notice["finished_by_title"] or now > end + timedelta(seconds=END_GRACE_SECONDS):
        return "finished", "finished"
    if now < start:
        if start - now <= timedelta(seconds=SOON_SECONDS):
            return "scheduled", "soon"
        return "scheduled", "scheduled"
    if now > end:
        return "in_progress", "overdue"
    return "in_progress", "in_progress"


def _local(dt, fmt):
    return dt.astimezone().strftime(fmt)


def _day(dt):
    local = dt.astimezone()
    return f"{local.month}/{local.day}({WEEKDAYS_KO[local.weekday()]})"


def _hm(dt):
    return _local(dt, "%H:%M")


def aside_label(status, marker, notice, now):
    """
    Two-line label of the aside entry. alas-fork.css renders the line break
    with `white-space: pre-line`.
    """
    if status == "unsupported":
        return ""
    if status == "unknown":
        return "공지 확인 중" if marker == "loading" else "공지 확인 실패"
    if status == "none":
        return "점검 없음"
    start, end = notice["start"], notice["end"]
    if status == "scheduled":
        if marker == "soon":
            return f"곧 점검\n{_hm(start)}~{_hm(end)}"
        return f"점검 예정\n{_local(start, '%m/%d %H:%M')}"
    if status == "in_progress":
        if marker == "overdue":
            return f"점검 중\n{_hm(end)} 지남"
        return f"점검 중\n~{_hm(end)} 대기"
    # finished
    if now.astimezone().date() != end.astimezone().date():
        return f"{_local(end, '%m/%d')} 점검 종료"
    if notice["finished_by_title"]:
        return "점검 종료\n완료 공지"
    return f"점검 종료\n(예정 {_hm(end)})"


def window_text(notice):
    """
    '10/8(목) 14:00 ~ 20:00' or with the end date when it differs.
    """
    start, end = notice["start"], notice["end"]
    if start.astimezone().date() == end.astimezone().date():
        return f"{_day(start)} {_hm(start)} ~ {_hm(end)}"
    return f"{_day(start)} {_hm(start)} ~ {_day(end)} {_hm(end)}"


def toast_plan(prev, state):
    """
    Which toast a session shows when its polled state changes.

    Args:
        prev: the state the session saw last, None on the first poll.
        state: (notice id, status, end iso) from toast_state(), or -1 when
            the Switch reports "unchanged".

    Returns:
        tuple[str, str] | None: (kind, end iso); kind is start, extended
            or finish. None means no toast.
    """
    if state == -1 or prev is None or prev == -1:
        return None
    notice_id, status, end_iso = state
    prev_id, prev_status, _ = prev
    same = prev_id == notice_id
    if status == "in_progress" and not (same and prev_status == "in_progress"):
        if same and prev_status == "finished":
            return "extended", end_iso
        return "start", end_iso
    if status == "finished" and same and prev_status == "in_progress":
        return "finish", end_iso
    return None


class MaintenanceChecker:
    """
    Fork module: tells the GUI about the official JP maintenance window.

    check() runs every FETCH_INTERVAL on the global TaskHandler and fetches
    the newest maintenance notice; observe() runs every OBSERVE_INTERVAL,
    derives the status from the clock, persists transitions and sends the
    push notifications. Both must never raise: TaskHandler.loop() removes a
    task that raises, for good.

    Only JP instances are supported (the notice source is azurlane.jp).
    """

    def __init__(self, state_file=STATE_FILE):
        self.notice = None
        self.fetched_at = None
        self.error = None
        self.latest_unparsed = None
        self.instances = []
        self._state_file = state_file
        self._record = self._state_load()
        self._lock = threading.Lock()
        self._force = False
        self._config_mtimes = {}
        self._config_cache = {}
        self._error_logged = False

    # Clock, replaced by the smoke test wrapper
    def _now(self):
        return datetime.now(JST)

    @property
    def supported(self):
        return bool(self.instances)

    # ---------- periodic entry points ----------

    def check(self):
        if not self._lock.acquire(False):
            return
        try:
            self._check()
        except Exception as e:
            logger.warning(f"maintenance_checker: check failed, {e!r}")
        finally:
            self._lock.release()

    def observe(self):
        with self._lock:
            try:
                self._observe(self._now())
            except Exception as e:
                logger.warning(f"maintenance_checker: observe failed, {e!r}")

    def recheck(self):
        """
        Manual re-check from the popup.

        Returns:
            bool: False if throttled, nothing was checked.
        """
        if self.fetched_at is not None:
            age = (self._now() - self.fetched_at).total_seconds()
            if age < RECHECK_SECONDS:
                return False
        self._force = True
        self.check()
        self.observe()
        return True

    def _check(self):
        self._scan_instances()
        if not self.supported:
            return
        self._force = False
        self._fetch_notice()

    # ---------- instances ----------

    def _scan_instances(self):
        found = []
        for name in alas_instance():
            try:
                path = filepath_config(name)
                mtime = os.path.getmtime(path)
                if self._config_mtimes.get(name) != mtime:
                    self._config_cache[name] = read_file(path)
                    self._config_mtimes[name] = mtime
                data = self._config_cache.get(name) or {}
                server = str(deep_get(data, "Alas.Emulator.ServerName", "") or "")
                package = str(deep_get(data, "Alas.Emulator.PackageName", "") or "")
                if package != JP_PACKAGE and not server.startswith("jp-"):
                    continue
                found.append({
                    "name": name,
                    "server": self._server_label(server),
                    "push": deep_get(data, "Alas.Error.OnePushConfig", "") or "",
                })
            except Exception as e:
                logger.warning(f"maintenance_checker: skip instance {name}, {e!r}")
        self.instances = found

    @staticmethod
    def _server_label(server):
        try:
            return VALID_SERVER_LIST["jp"][int(server.split("-")[-1])]
        except Exception:
            return "JP"

    # ---------- notice ----------

    def _fetch_notice(self):
        rows = self._get_rows()
        self.fetched_at = self._now()
        if rows is None:
            return
        self.error = None
        self.latest_unparsed = None
        for index, item in enumerate(rows[:NEWS_PARAMS["size"]]):
            notice = parse_notice(item)
            if notice is not None:
                self.notice = notice
                self._error_logged = False
                return
            title = parse_title(item.get("title"))
            if index == 0 and not title["finished"]:
                # The newest notice is live but unreadable: showing last
                # week's notice instead would hide a running maintenance.
                self.notice = None
                self.latest_unparsed = {
                    "id": item.get("id"),
                    "url": NEWS_URL.format(id=item.get("id")),
                }
                self.error = "latest notice unparsable"
                self._warn_once(f"notice {item.get('id')} unparsable: {ascii(title['text'])}")
                return
        self.notice = None
        self.error = "no parseable notice"
        self._warn_once("no parseable notice in the newest rows")

    def _get_rows(self):
        try:
            resp = requests.get(
                NEWS_API,
                params=NEWS_PARAMS,
                headers={"User-Agent": USER_AGENT},
                timeout=(5, 10),
            )
            if resp.status_code != 200:
                raise ValueError(f"status {resp.status_code}")
            data = resp.json()
            if not deep_get(data, "meta.ok", False):
                raise ValueError("meta.ok is false")
            rows = deep_get(data, "data.rows", None)
            if not isinstance(rows, list):
                raise ValueError("no rows")
            return rows
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self._warn_once(f"fetch failed, {e!r}")
            return None

    def _warn_once(self, text):
        if not self._error_logged:
            logger.warning(f"maintenance_checker: {text}")
            self._error_logged = True

    # ---------- status ----------

    def _derive(self, now):
        return derive(self.notice, now, self.fetched_at is not None, self.supported)

    @property
    def status(self):
        return self._derive(self._now())[0]

    def snapshot(self):
        """
        Polled by the aside Switch. Must not raise.

        Returns:
            tuple[str, str, str]: (status, label, marker)
        """
        try:
            now = self._now()
            status, marker = self._derive(now)
            return status, aside_label(status, marker, self.notice, now), marker
        except Exception as e:
            logger.warning(f"maintenance_checker: snapshot failed, {e!r}")
            return "unknown", "공지 확인 실패", "unknown"

    def toast_state(self):
        """
        Polled by the per-session toast Switch. Must not raise.

        Returns:
            tuple: (notice id, status, end iso)
        """
        try:
            status = self._derive(self._now())[0]
            if self.notice is None:
                return None, status, None
            return self.notice["id"], status, self.notice["end"].isoformat()
        except Exception as e:
            logger.warning(f"maintenance_checker: toast_state failed, {e!r}")
            return None, "unknown", None

    def _observe(self, now):
        status = self._derive(now)[0]
        notice_id = self.notice["id"] if self.notice else None
        end_iso = self.notice["end"].isoformat() if self.notice else None
        record = self._record
        fresh = not record
        same_notice = record.get("notice_id") == notice_id
        prev_status = record.get("status") if same_notice else None
        pushed = list(record.get("pushed", [])) if same_notice else []
        if same_notice and record.get("status") == status and record.get("end") == end_iso:
            return

        kind = None
        if status == "in_progress" and prev_status != "in_progress":
            # scheduled -> in_progress, a notice first seen while running,
            # or finished -> in_progress when the end was moved (extension)
            kind = "extended" if prev_status == "finished" else "start"
        elif status == "finished" and prev_status == "in_progress":
            kind = "finish"
        if fresh:
            # First observation of a GUI without a state file (deployment,
            # first start): record only, so last week's notice stays quiet.
            kind = None
        key = f"{status}@{end_iso}"
        if kind and key not in pushed:
            pushed.append(key)
            self._push(kind)
        self._record = {
            "notice_id": notice_id,
            "status": status,
            "end": end_iso,
            "pushed": pushed,
            "updated_at": now.isoformat(),
        }
        self._state_save()

    # ---------- push ----------

    def _push(self, kind):
        notice = self.notice
        if notice is None:
            return
        end = _hm(notice["end"])
        if kind == "start":
            title = f"벽람 점검 시작, {end}까지 대기"
        elif kind == "extended":
            title = f"벽람 점검 연장, {end}까지 대기"
        else:
            title = "벽람 점검 종료"
        jobs = []
        seen = set()
        for inst in self.instances:
            config = inst.get("push") or ""
            if config in seen or not self._has_provider(config):
                continue
            seen.add(config)
            content = f"[{inst['server']}] {window_text(notice)}\n{notice['url']}"
            jobs.append((config, title, content))
        if not jobs:
            logger.info(f"maintenance_checker: {kind}, no push provider configured")
            return
        threading.Thread(target=self._send, args=(jobs,), daemon=True).start()

    @staticmethod
    def _has_provider(config):
        try:
            merged = {}
            for doc in yaml.safe_load_all(config):
                if isinstance(doc, dict):
                    merged.update(doc)
            return merged.get("provider") is not None
        except Exception:
            return False

    @staticmethod
    def _send(jobs):
        for config, title, content in jobs:
            try:
                handle_notify(config, title=title, content=content)
            except Exception as e:
                logger.warning(f"maintenance_checker: push failed, {e!r}")

    # ---------- state file ----------

    def _state_load(self):
        if not self._state_file:
            return {}
        try:
            with open(self._state_file, mode="r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _state_save(self):
        if not self._state_file:
            return
        try:
            with open(self._state_file, mode="w", encoding="utf-8") as f:
                json.dump(self._record, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"maintenance_checker: failed to save state, {e!r}")


maintenance_checker = MaintenanceChecker()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch the JP maintenance notice once and print it")
    parser.add_argument("--once", action="store_true", help="fetch and print, no state file, no push")
    args = parser.parse_args()
    probe = MaintenanceChecker(state_file=None)
    probe._scan_instances()
    print("instances:", [(i["name"], i["server"], bool(i["push"])) for i in probe.instances])
    probe._fetch_notice()
    print("error:", probe.error)
    if probe.notice:
        n = probe.notice
        print(f"notice {n['id']}: {n['title']}")
        print(f"window: {window_text(n)} JST ({n['start'].isoformat()} ~ {n['end'].isoformat()})")
        print(f"finished_by_title={n['finished_by_title']} extended={n['extended']}")
    status, marker = derive(probe.notice, probe._now(), True, True)
    print(f"status now: {status} ({marker}) label={aside_label(status, marker, probe.notice, probe._now())!r}")
