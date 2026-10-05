"""
Fork patch for module/map/fleet_bar.py.

Since the FleetBarDetector rewrite (upstream 2026-09-23), FLEET_1 dropdown misses one row at y~308,
so option indexes shift and the wrong fleet is chosen. Re-insert rows missing between detected ones.
Remove this file and its hook once upstream fixes FleetBarDetector.
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
