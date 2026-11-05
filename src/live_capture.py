"""Historical capture facts from the generation, independent of current Twitch state."""
import json

WARNING = '🔴 Чат получен во время LIVE-трансляции и может быть неполным'


def captured_live(meta):
    return isinstance(meta, dict) and meta.get('captured_during_live') is True


def generation_capture(ready):
    return captured_live(_generation_meta(ready))


def generation_incomplete(ready):
    meta = _generation_meta(ready)
    return captured_live(meta) or (isinstance(meta, dict) and meta.get('capture_complete') is False)


def _generation_meta(ready):
    if not isinstance(ready, dict):
        return None
    meta = ready.get('meta')
    if not isinstance(meta, dict):
        try:
            meta = json.loads(ready.get('meta_json') or '{}')
        except (TypeError, ValueError):
            return None
    return meta
