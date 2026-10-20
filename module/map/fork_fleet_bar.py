"""
module/map/fleet_bar.py 포크 보정.

원작자 FleetBarDetector 재작성(2026-09-23) 이후 1함대 목록을 연 첫 화면에서 y~308 줄을 놓쳐 5줄만 감지,
번호가 밀려 엉뚱한 함대를 고르던 문제. 감지된 줄 사이 간격이 줄 간격의 정수배면 빠진 줄을 채워 넣음.
원작자 대기 로직(map_fleet_preparation.py, 옵션 수가 2·6이 아니면 1초 대기)보다 먼저 6줄로 채우므로 그 대기는 건너뜀.

발생 조건: 2함대 해제(`@ FLEET_2_CLEAR`) 후 1초 안에 1함대 목록(`@ FLEET_1_CHOOSE`)을 열 때.
2026-10-05~19 로그에서 이 경우 58회 모두 보정 발생, 잠수함 화면을 거쳐 늦게 연 42회는 0회.

제거 조건: 원작자 2026-10-19 수정(Fix: half-loaded infobar covers fleet option) 반영 후
위 발생 조건이 20회 이상 쌓이는 동안 `FleetOption filled missing row` 0회면 이 파일과 fleet_bar.py 의 훅 2줄 제거.
"""
from module.logger import logger

# Normal distance between option tops
OPTION_PITCH = 42
OPTION_PITCH_RANGE = (38, 46)
MAX_OPTIONS = 6


def _pitch(tops):
    gaps = [b - a for a, b in zip(tops, tops[1:]) if b - a >= 33]
    pitch = min(gaps) if gaps else OPTION_PITCH
    if not OPTION_PITCH_RANGE[0] <= pitch <= OPTION_PITCH_RANGE[1]:
        pitch = OPTION_PITCH
    return pitch


def fill_option_gaps(options):
    """
    Args:
        options (list[FleetOption]): Sorted by area[1]

    Returns:
        list[FleetOption]:
    """
    if len(options) < 2:
        return options
    pitch = _pitch([button.area[1] for button in options])

    result = [options[0]]
    inserted = 0
    for button in options[1:]:
        prev = result[-1]
        gap = button.area[1] - prev.area[1]
        k = int(gap / pitch + 0.5)
        if k >= 2 and abs(gap - k * pitch) < pitch * 0.25 \
                and len(options) + inserted + k - 1 <= MAX_OPTIONS:
            inserted += k - 1
            step = gap / k
            for i in range(1, k):
                dy = int(step * i + 0.5)
                x1, y1, x2, y2 = prev.area
                filled = type(prev)((x1, y1 + dy, x2, y2 + dy))
                logger.info(f'FleetOption filled missing row at y={y1 + dy}')
                result.append(filled)
        result.append(button)
    return result
