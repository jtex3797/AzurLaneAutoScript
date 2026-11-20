"""
Fork: War Archives names in three languages for the Korean GUI, plus a searchable picker.

The WarArchives task picks its archive from the `WarArchives.Campaign.Event` dropdown. Upstream has no
Korean names (ko-KR.json is fork-owned and falls back to English), and this account plays on the JP server,
so the English names could not be matched against the titles in the game.

- relabel(): after every lang.reload(), the ko-KR label of each `Campaign.Event.war_archives_*` becomes
  "Japanese / Korean / English" (empty parts skipped). Only war_archives_* keys change, so the Event lists of
  other tasks stay as they are. Installed at import by wrapping module.webui.lang.reload, which app.py calls
  as a module attribute.
- inject(): called once per GUI session from AlasGUI.run(). Sends the names as JSON and
  assets/gui/js/fork-war-archives.js, which widens that dropdown, adds an info line and a side panel with
  search. Styles are in assets/gui/css/alas-fork.css.

KO_NAMES holds the KR server's event names, collected 2026-11-20 from the Namu Wiki Azur Lane event lists
(large, small and other events, which list CN/JP/EN/KR names side by side), matched by the Japanese name or by
the KR schedule (about two weeks after CN). Less certain: 20191010_en (spacing), 20201012_cn, 20201029_cn,
20211014_cn, 20220224_cn, 20220324_cn, 20240725_cn. Archives without a name show as "not collected".
"""
import json

from pywebio.session import run_js

import module.webui.lang as lang
from module.logger import logger

KEY = 'Campaign.Event.'
PREFIX = 'war_archives_'
JS_FILE = './assets/gui/js/fork-war-archives.js'

# Archive id -> KR server event name.
KO_NAMES = {
    'war_archives_20180607_cn': '검게 물든 강철의 벚꽃',
    'war_archives_20180726_cn': '빛과 그림자의 아이리스',
    'war_archives_20181020_en': '노력, 희망과 계획',
    'war_archives_20181026_en': '어둠에 떨어진 푸른 날개',
    'war_archives_20181227_cn': '청홍의 메아리',
    'war_archives_20190221_en': '싸늘한 겨울의 왕관',
    'war_archives_20190314_en': '월하의 서곡',
    'war_archives_20190321_en': '홍염의 방문자',
    'war_archives_20190620_en': '영광스런 최종전',
    'war_archives_20190911_cn': '디바인 트레지 코미디',
    'war_archives_20191010_en': '그라프 슈피 추격전',
    'war_archives_20191031_en': '거울에 비친 이색',
    'war_archives_20200312_cn': '솔로몬의 무사',
    'war_archives_20200507_cn': '어둠 속의 귀로',
    'war_archives_20200603_cn': '피오르드 반격전',
    'war_archives_20200806_cn': '최고의 보물',
    'war_archives_20200820_cn': '메탈 블러드, 음표 & 맹세',
    'war_archives_20200903_cn': '피오르드의 별',
    'war_archives_20200917_cn': '접해몽화',
    'war_archives_20201012_cn': '날카로운 해상의 날개',
    'war_archives_20201029_cn': '격창의 유니버스',
    'war_archives_20201229_cn': '부상한 작전',
    'war_archives_20210225_cn': '새벽녘에 비치는 빙화',
    'war_archives_20210325_cn': '샌드플레이',
    'war_archives_20210422_cn': '부흥의 찬송가',
    'war_archives_20210527_cn': '비추는 나선의 경해',
    'war_archives_20210624_cn': '빛나는 벚꽃의 그림자',
    'war_archives_20210819_cn': '어둠을 쫓는 은익',
    'war_archives_20210916_cn': '맑고 푸른 바다',
    'war_archives_20211014_cn': '격주의 폴라리스',
    'war_archives_20211028_cn': '하늘 아래의 찬송가',
    'war_archives_20211111_cn': '제노바의 화염',
    'war_archives_20211229_cn': '역행하는 무지개 탑',
    'war_archives_20220210_cn': '동절의 북해',
    'war_archives_20220224_cn': '깊게 울리는 메아리',
    'war_archives_20220324_cn': '허상의 탑',
    'war_archives_20220414_cn': '영원한 밤의 환광',
    'war_archives_20220428_cn': '레인보우 피날레',
    'war_archives_20220526_cn': '빛나는 정원의 맹세',
    'war_archives_20220728_cn': '독수리의 서사곡',
    'war_archives_20220818_cn': '결상점 작전',
    'war_archives_20220915_cn': '짙은 자줏빛의 안개',
    'war_archives_20221222_cn': '거듭되는 평행세계',
    'war_archives_20230223_cn': '잿빛 폐허',
    'war_archives_20230525_cn': '오엽각 교차점',
    'war_archives_20230803_cn': '불타는 아이리스 회상곡',
    'war_archives_20230817_cn': '우자의 천칭',
    'war_archives_20231026_cn': '템페스타와 청춘의 샘',
    'war_archives_20240725_cn': '환몽 간주곡',
    'war_archives_20241024_cn': '템페스타와 잠든 바다',
    'war_archives_20251023_cn': '템페스타와 자유 군도',
}


def _strip(text, prefix):
    return text[len(prefix):] if text.startswith(prefix) else text


def names():
    """
    Returns:
        dict[str, list[str]]: Archive id -> [Japanese, Korean, English], '' where unknown.
    """
    en_dic = lang.dic_lang.get('en-US', {})
    ja_dic = lang.dic_lang.get('ja-JP', {})
    out = {}
    for key, en in en_dic.items():
        if not key.startswith(KEY + PREFIX):
            continue
        archive = key[len(KEY):]
        ja = ja_dic.get(key, '')
        # lang.reload() copies English into missing keys of other languages
        if ja == en:
            ja = ''
        out[archive] = [_strip(ja, '檔案 '), KO_NAMES.get(archive, ''), _strip(en, 'archives ')]
    return out


def relabel():
    ko_dic = lang.dic_lang.get('ko-KR')
    if ko_dic is None:
        return
    for archive, parts in names().items():
        ko_dic[KEY + archive] = ' / '.join(part for part in parts if part)


def _install():
    original = lang.reload
    if getattr(original, '_fork_war_archives', False):
        return

    def reload():
        original()
        try:
            relabel()
        except Exception as e:
            logger.warning(f'War archives relabel failed: {e}')

    reload._fork_war_archives = True
    lang.reload = reload


def inject():
    """
    Load the picker into the current GUI session. Never raises, the settings page must keep working.
    """
    try:
        with open(JS_FILE, 'r', encoding='utf-8') as f:
            js = f.read()
        data = json.dumps(names(), ensure_ascii=True)
        run_js(f'window.forkWarArchives = {data};\n{js}')
    except Exception as e:
        logger.warning(f'War archives picker not loaded: {e}')


_install()
