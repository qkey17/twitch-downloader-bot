"""Conservative presentation hints for the supplied VOD text templates.

These are text matches, NOT verified Twitch event types. Keep source rows intact.
Unknown wording, invalid counts or unsafe fragment boundaries use the old viewer.
"""
import re

NAME = r"\w{1,25}"
NUMBER = r"[1-9][0-9]{0,6}"
SUB = re.compile(rf"^(?P<actor>{NAME}) subscribed (?:(?:at (?P<plan>Tier [12]))|(?P<prime>with Prime))\.")
TENURE = re.compile(rf" They've subscribed for (?P<months>{NUMBER}) (?P<unit>months?)(?:, currently on a (?P<streak>{NUMBER}) month streak)?!")
TEMPLATES = (
    ('watch_streak', re.compile(rf"^(?P<actor>{NAME}) watched (?P<count>{NUMBER}) consecutive streams and sparked a watch streak!")),
    ('community_gift', re.compile(rf"^(?P<actor>{NAME}) is gifting (?P<count>{NUMBER}) Tier 1 Subs to (?P<channel>{NAME})'s community! They've gifted a total of (?P<total>{NUMBER}) in the channel!")),
    ('gift', re.compile(rf"^(?P<actor>{NAME}) gifted a Tier 1 sub to (?P<recipient>{NAME})!")),
)


def recognize_notice(text):
    """Return a small, explicit hint or None; never guess missing parameters."""
    if not isinstance(text, str):
        return None
    match = SUB.match(text)
    if match:
        kind = 'subscription'
        params = dict(actor=match['actor'], plan=match['plan'] or 'Prime')
        end = match.end()
        tenure = TENURE.match(text, end)
        if tenure:
            months = int(tenure['months'])
            streak = int(tenure['streak']) if tenure['streak'] else None
            if months > 1200 or (tenure['unit'] == 'month') != (months == 1):
                return None
            if streak is not None and not 1 <= streak <= months:
                return None
            params['months'] = months
            if streak is not None:
                params['streak'] = streak
            end = tenure.end()
        elif text[end:].lstrip().startswith("They've subscribed"):
            # A truncated/malformed tenure is not a first subscription comment.
            return None
    else:
        for kind, pattern in TEMPLATES:
            match = pattern.match(text)
            if match:
                break
        else:
            return None
        params = match.groupdict()
        for key in ('count', 'total'):
            if key in params:
                value = int(params[key])
                if value > (100000 if kind == 'watch_streak' else 1000000):
                    return None
                params[key] = value
        if kind in ('community_gift', 'gift'):
            params['plan'] = 'Tier 1'
        if kind == 'community_gift' and params['total'] < params['count']:
            return None
        end = match.end()
    # A complete phrase must end or be followed by a whitespace-delimited comment.
    if end < len(text) and not text[end].isspace():
        return None
    return dict(v=1, kind=kind, **params, system=text[:end], comment=text[end:])


def _is_emote(fragment):
    return bool(fragment.get('emoteID') or fragment.get('emote'))


def _comment_fragments(text, fragments, boundary):
    """Slice code-point ranges in Python; no UTF-16 offset reaches JavaScript."""
    if not isinstance(fragments, list) or any(not isinstance(f, dict) or not isinstance(f.get('text'), str) for f in fragments):
        return None
    combined = ''.join(f['text'] for f in fragments)
    if combined == text:
        leading = 0
    elif combined.strip() == text:
        # Existing render_message strips the joined text, but not source fragments.
        leading = len(combined) - len(combined.lstrip())
    else:
        return None
    start, stop = leading + boundary, leading + len(text)
    result, offset = [], 0
    for fragment in fragments:
        end = offset + len(fragment['text'])
        if _is_emote(fragment) and offset < start:
            return None  # A system prefix/emote boundary is ambiguous.
        left, right = max(start, offset), min(stop, end)
        if left < right:
            if _is_emote(fragment) and (left != offset or right != end):
                return None
            result.append({**fragment, 'text': fragment['text'][left-offset:right-offset]})
        offset = end
    return result


def prepare_notice(row):
    """Annotate only the HTML projection; spool, original text/fragments unchanged."""
    if 'notice' in row:
        return row  # Preserve existing extra fields; never overwrite source metadata.
    notice = recognize_notice(row.get('text'))
    if notice is None:
        return row
    fragments = row.get('fragments')
    if fragments:
        sliced = _comment_fragments(row['text'], fragments, len(notice['system']))
        if sliced is None:
            return row
        notice['comment_fragments'] = sliced
    return {**row, 'notice': notice}
