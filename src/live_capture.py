"""Historical capture facts from the generation, independent of current Twitch state."""
import json

WARNING = '🔴 Чат получен во время LIVE и может быть неполным: сообщения только до момента обработки.'


def captured_live(meta):
    return isinstance(meta, dict) and meta.get('captured_during_live') is True


def generation_capture(ready):
    if not isinstance(ready, dict):
        return False
    meta = ready.get('meta')
    if not isinstance(meta, dict):
        try:
            meta = json.loads(ready.get('meta_json') or '{}')
        except (TypeError, ValueError):
            return False
    return captured_live(meta)
