"""
Fork tool: condense ALAS logs for debugging (stdlib only, ASCII-only source).

    ./toolkit/python.exe dev_tools/fork_log_triage.py            # overview
    ./toolkit/python.exe dev_tools/fork_log_triage.py --task Exercise
    ./toolkit/python.exe dev_tools/fork_log_triage.py --since 23:10
    ./toolkit/python.exe dev_tools/fork_log_triage.py --range 12555:12700

Everything it drops is announced, with line numbers, so the reader can expand.
Log files are picked by mtime, not by the date in the name: a long-running
process keeps writing to the file it opened at start, even past midnight.
"""
import argparse
import datetime
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, 'log')
RE_TS = re.compile(r'^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\.\d+ \| (\w+) +\| ?(.*)$')
RE_RULE0 = re.compile(u'^\u2550{20,}$')
RE_RULE = re.compile(u'^[\u2550\u2500]{10,}')
RE_RUN = re.compile(u'([\u2500\u2550\u2501\u2504])\\1{7,}')
RE_BOX_END = re.compile(u'(?:\\s*\u2502)+$')
RE_BOX_BLANK = re.compile(u'^[\u2502\\s]*$')
ALERT = ('WARNING', 'ERROR', 'CRITICAL')
SEVERE = ('ERROR', 'CRITICAL')

FOOTER = """HOW TO READ (alas.py run()/loop()):
- GameStuckError / GameTooManyClickError / GameBugError: dump saved to log/error/<ms>/, task `Restart` queued.
  So the failing task is NOT the last section; "Function calls:" (INFO) just above the WARNING is the call stack.
- GamePageUnknownError: dump + exit(1) if the game server is up. Bare Exception: traceback + dump + exit(1).
- ScriptError / RequestHumanTakeover: exit(1) with NO dump folder; this log is the only evidence.
- Same task failing 3 times in a row: CRITICAL + exit(1).
- Rich tracebacks: the exception type/message is the LAST line of the box, not the ERROR line.
- Section title -> method of the same name (snake_case) in alas.py -> the module it imports.
- Fork web GUI code (module/webui/) logs to *_gui.txt, not to the scheduler log."""


def newest(pattern, count=1):
    files = glob.glob(os.path.join(LOG_DIR, pattern))
    return sorted(files, key=os.path.getmtime, reverse=True)[:count]


def load(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return [line.rstrip() for line in f]


def compact(line, raw=False):
    if raw:
        return line
    m = RE_TS.match(line)
    if m:
        return '%s %s| %s' % (m.group(2), m.group(3)[0], m.group(4))
    line = RE_BOX_END.sub('', line)
    return RE_RUN.sub(lambda x: x.group(1) * 3, line)


def level_of(line):
    m = RE_TS.match(line)
    return m.group(3) if m else None


def first_ts(lines, start, end):
    for i in range(start, min(end, len(lines))):
        m = RE_TS.match(lines[i])
        if m:
            return m.group(1), m.group(2)
    return '', ''


def find_sections(lines):
    """Level-0 banners: rule / centered title / rule. Returns [start, end, title]."""
    out = []
    n = len(lines)
    i = 0
    while i < n - 2:
        if RE_RULE0.match(lines[i]) and RE_RULE0.match(lines[i + 2]) \
                and lines[i + 1].strip() and not RE_TS.match(lines[i + 1]):
            out.append([i, n, lines[i + 1].strip()])
            i += 3
        else:
            i += 1
    for a, b in zip(out, out[1:]):
        a[1] = b[0]
    return out


def find_incidents(lines, before=12, after=3):
    """Alert line + its untimestamped continuation (wrapped text, traceback box)."""
    wins = []
    n = len(lines)
    i = 0
    while i < n:
        if level_of(lines[i]) in ALERT:
            j = i + 1
            while j < n and not RE_TS.match(lines[j]) and not RE_RULE.match(lines[j]):
                j += 1
            start = max(0, i - before)
            for k in range(i - 1, max(-1, i - 80), -1):
                if lines[k].endswith('| Function calls:'):
                    start = max(0, k - 8)
                    break
                if RE_RULE0.match(lines[k]):
                    break
            # [start, end, is_severe, alert line index, signature]
            sig = re.sub(r'\d+', '#', RE_TS.match(lines[i]).group(4))
            if j - 1 > i:
                sig += ' / ' + lines[j - 1].strip()[:160]
            wins.append([start, min(n, j + after), level_of(lines[i]) in SEVERE, i, sig])
            i = j
        else:
            i += 1
    merged = []
    for w in wins:
        if merged and w[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], w[1])
            merged[-1][2] = merged[-1][2] or w[2]
            merged[-1][3] = w[3]
            merged[-1][4] += ' + ' + w[4]
        else:
            merged.append(w)
    return merged


class Printer(object):
    def __init__(self, lines, raw):
        self.lines = lines
        self.raw = raw
        self.shown = set()

    def segment(self, start, end, cap=0, head=40, skip_shown=False):
        """Print lines[start:end]; if longer than cap keep head + tail."""
        idx = list(range(start, end))
        cut = None
        if cap and len(idx) > cap:
            cap = max(2, cap)
            head = min(head, cap // 2)
            tail = cap - head
            idx = idx[:head] + [None] + idx[-tail:]
            cut = (start + head + 1, end - tail)
        day = first_ts(self.lines, start, end)[0]
        print('@L%d-%d %s' % (start + 1, end, day))
        hidden = 0
        for i in idx:
            if i is None:
                print('  ... [L%d-%d omitted: --range %d:%d]' % (cut[0], cut[1], cut[0], cut[1]))
                continue
            if skip_shown and i in self.shown:
                hidden += 1
                continue
            if hidden:
                print('  ... [%d lines already shown above]' % hidden)
                hidden = 0
            self.shown.add(i)
            if RE_RULE0.match(self.lines[i]) or (self.lines[i] and RE_BOX_BLANK.match(self.lines[i])):
                continue
            if 0 < i < len(self.lines) - 1 and RE_RULE0.match(self.lines[i - 1]) \
                    and RE_RULE0.match(self.lines[i + 1]):
                print('=== %s ===' % self.lines[i].strip())
                continue
            print(compact(self.lines[i], self.raw))
        if hidden:
            print('  ... [%d lines already shown above]' % hidden)


def alert_index(lines, limit=25):
    seen = {}
    for i, line in enumerate(lines):
        m = RE_TS.match(line)
        if m and m.group(3) in ALERT:
            key = (m.group(3), re.sub(r'\d+', '#', m.group(4))[:110])
            cnt = seen.get(key, (0,))[0]
            seen[key] = (cnt + 1, i + 1, m.group(2))
    rows = sorted(seen.items(), key=lambda kv: kv[1][1])
    if len(rows) > limit:
        print('  (%d older distinct alerts not listed)' % (len(rows) - limit))
    for (lvl, msg), (cnt, ln, clock) in rows[-limit:]:
        print('  L%d %s %s x%d | %s' % (ln, clock, lvl[0], cnt, msg))
    return len(rows)


def report(path, args, full):
    lines = load(path)
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    print('##### %s | %d lines | modified %s' % (
        os.path.relpath(path, ROOT).replace(os.sep, '/'), len(lines), mtime.strftime('%Y-%m-%d %H:%M')))
    out = Printer(lines, args.raw)
    sections = find_sections(lines)

    if args.range:
        try:
            a, b = [int(x) for x in args.range.split(':')]
        except ValueError:
            print('--range needs two line numbers like 12555:12700, got %r' % args.range)
            return
        a, b = min(a, b), max(a, b)
        if a > len(lines):
            print('--range starts past the end of the file (%d lines)' % len(lines))
            return
        out.segment(max(0, a - 1), min(len(lines), b))
        return
    if args.task:
        want = args.task.lower().replace('_', '').replace(' ', '')
        hits = [s for s in sections if want in s[2].lower().replace('_', '').replace(' ', '')]
        if not hits:
            print('no section titled like %r; titles seen: %s' % (
                args.task, ', '.join(sorted(set(s[2] for s in sections[-40:])))))
            return
        s = hits[-1]
        print('== last of %d sections matching %r' % (len(hits), args.task))
        out.segment(s[0], s[1], cap=args.lines)
        return
    if args.since:
        day = first_ts(lines, max(0, len(lines) - 400), len(lines))[0]
        for i, line in enumerate(lines):
            m = RE_TS.match(line)
            if m and m.group(1) == day and m.group(2)[:5] >= args.since:
                out.segment(i, len(lines), cap=args.lines)
                return
        print('no line at or after %s on %s' % (args.since, day))
        return

    incidents = find_incidents(lines)
    if full:
        print('== TIMELINE (last %d of %d task sections; E=error/critical W=warning)' % (
            min(15, len(sections)), len(sections)))
        for s in sections[-15:]:
            lv = [level_of(x) for x in lines[s[0]:s[1]]]
            e = sum(1 for x in lv if x in SEVERE)
            w = sum(1 for x in lv if x == 'WARNING')
            flag = (' E%d' % e if e else '') + (' W%d' % w if w else '')
            print('  L%d %s %s (%d lines)%s' % (s[0] + 1, first_ts(lines, s[0], s[1])[1], s[2], s[1] - s[0], flag))

    print('== ALERT INDEX (distinct messages, digits masked, last occurrence)')
    if not alert_index(lines):
        print('  none')

    severe = [w for w in incidents if w[2]][-args.incidents:]
    mild = [w for w in incidents if not w[2]][-3:]
    chosen = sorted(severe + mild)
    print('== INCIDENTS shown in full: %d of %d (last %d with ERROR/CRITICAL, last 3 WARNING-only)' % (
        len(chosen), len(incidents), args.incidents))
    newest_sig = {}
    for w in chosen:
        newest_sig[w[4]] = w[0]
    for w in chosen:
        if newest_sig[w[4]] != w[0]:
            print('@L%d-%d same signature as a later incident, not repeated (--range %d:%d)' % (
                w[0] + 1, w[1], w[0] + 1, w[1]))
            continue
        out.segment(w[0], w[1], cap=180, head=60)

    if full and sections:
        anchor = [w for w in incidents if w[2]] or incidents
        focus = sections[-1]
        why = 'final section, no alerts in this file'
        if anchor:
            pos = anchor[-1][3]
            why = 'holds the last %s' % ('ERROR/CRITICAL' if anchor[-1][2] else 'WARNING')
            holder = [s for s in sections if s[0] <= pos < s[1]]
            if holder:
                focus = holder[-1]
        print('== FOCUS SECTION %r (%s; %d lines)' % (focus[2], why, focus[1] - focus[0]))
        out.segment(focus[0], focus[1], cap=args.lines, skip_shown=True)
        if focus is not sections[-1]:
            last = sections[-1]
            print('== CURRENT STATE: tail of final section %r' % last[2])
            out.segment(max(last[0], last[1] - 25), last[1], skip_shown=True)
    print('== OMITTED: %d of %d lines not shown. Expand with --range A:B, --task NAME, --since HH:MM, '
          '--lines N, --incidents N, --file PATH' % (len(lines) - len(out.shown), len(lines)))


def span(path):
    """(first, last) timestamp strings of a log, reading only its head and tail."""
    found = []
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        for offset in (0, max(0, size - 65536)):
            f.seek(offset)
            text = f.read(65536).decode('utf-8', 'replace')
            stamps = re.findall(r'(?m)^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \|', text)
            if stamps:
                found.append(stamps[0] if offset == 0 else stamps[-1])
    return (found[0], found[-1]) if found else ('', '')


def dumps(logs):
    folders = sorted(glob.glob(os.path.join(LOG_DIR, 'error', '*')), reverse=True)[:3]
    spans = [(os.path.basename(x), span(x)) for x in logs]
    print('##### ERROR DUMPS (newest 3 of log/error/; log.txt ~100 lines, PNG = last screenshots)')
    for d in folders:
        name = os.path.basename(d)
        try:
            when = datetime.datetime.fromtimestamp(int(name) / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            when = '?'
        home = [n for n, (a, b) in spans if a and a <= when <= b]
        where = 'lead-up is in %s (use --file log/%s)' % (home[0], home[0]) if home else 'log file not among newest 3'
        if home and home[0] == spans[0][0]:
            where = 'covered above'
        print('  log/error/%s | %s | %d files | %s' % (name, when, len(os.listdir(d)), where))
    if not folders:
        print('  none')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', default='alas', help='config name in the log file name (default: alas)')
    p.add_argument('--file', help='explicit log file instead of the newest one')
    p.add_argument('--task', help='print the last section whose title contains NAME')
    p.add_argument('--since', metavar='HH:MM', help='print from this time on the last logged day')
    p.add_argument('--range', metavar='A:B', help='print raw line range A..B')
    p.add_argument('--lines', type=int, default=100, help='cap for long segments, 0 = no cap')
    p.add_argument('--incidents', type=int, default=5, help='how many ERROR incidents to print in full')
    p.add_argument('--no-gui', action='store_true', help='skip the web GUI log')
    p.add_argument('--raw', action='store_true', help='keep full timestamps and box borders')
    args = p.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if args.file:
        report(args.file if os.path.isabs(args.file) else os.path.join(ROOT, args.file), args, True)
        return
    main_logs = newest('*_%s.txt' % args.config, 3)
    if not main_logs:
        print('no log/*_%s.txt found' % args.config)
        return
    report(main_logs[0], args, True)
    if len(main_logs) > 1:
        print('   older: ' + ', '.join(os.path.basename(x) for x in main_logs[1:]) + ' (use --file)')
    if args.task or args.since or args.range:
        return
    if not args.no_gui:
        for g in newest('*_gui.txt'):
            print('')
            report(g, args, False)
    print('')
    dumps(main_logs)
    print('')
    print(FOOTER)


if __name__ == '__main__':
    main()
