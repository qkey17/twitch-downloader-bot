"""Bounded message-local navigation. Jobs and persistent results stay separate."""
import time
from collections import OrderedDict
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from functools import wraps

KEY = 'tdb_card_contexts'
LIMIT = 128
TTL = 900
FIELDS = frozenset(('chat_request', 'chat_choice', 'vod_pending', 'vod_link_lookup',
                    'ready_preview_request', 'vod_result_choice', 'legacy_open',
                    'history_view', 'history_file_card', 'text_input_route',
                    'image_surface', 'rejected_image'))


@dataclass
class Card:
    owner: int
    chat: int
    message: int
    expires: float
    data: dict = field(default_factory=dict)


class CardData(MutableMapping):
    def __init__(self, root, card):
        self.root, self.card = root, card

    def __getitem__(self, key):
        return (self.card.data if key in FIELDS else self.root)[key]

    def __setitem__(self, key, value):
        if key in FIELDS:
            self.card.data[key] = value
        # Compatibility mirror for legacy internal readers, never callback authority.
        self.root[key] = value

    def __delitem__(self, key):
        if key in FIELDS:
            value = self.card.data.pop(key)
            if self.root.get(key) is value:
                self.root.pop(key, None)
        else:
            del self.root[key]

    def __iter__(self):
        return iter((set(self.root) - FIELDS) | set(self.card.data))

    def __len__(self):
        return sum(1 for _ in self)


class CardContext:
    def __init__(self, base, card):
        self.base, self.card = base, card
        self.user_data = CardData(base.user_data, card)

    def __getattr__(self, key):
        return getattr(self.base, key)


def for_message(context, owner, chat, message, *, fresh=False):
    if getattr(context, 'card', None) is not None:
        if not fresh:
            return context
        while isinstance(context, CardContext):
            context = context.base
    store = context.application.bot_data.setdefault(KEY, OrderedDict())
    now = time.monotonic()
    for key, card in list(store.items()):
        if card.expires <= now:
            store.pop(key)
    key = (owner, chat, message)
    card = store.get(key)
    if card is None:
        if len(store) >= LIMIT:
            store.popitem(last=False)
        card = store[key] = Card(owner, chat, message, now + TTL)
        # Recover a legacy internal view only when its exact message is bound.
        for field in FIELDS:
            value = context.user_data.get(field)
            if isinstance(value, dict):
                bound = (value.get('chat', value.get('chat_id')),
                         value.get('message', value.get('message_id')))
                if bound == (chat, message):
                    card.data[field] = value
    store.move_to_end(key)
    card.expires = now + TTL
    return CardContext(context, card)


def bind(context, chat, message):
    card = getattr(context, 'card', None)
    if card is None:
        return
    store = context.application.bot_data[KEY]
    for key, value in list(store.items()):
        if value is card:
            store.pop(key)
    card.chat, card.message = chat, message
    card.expires = time.monotonic() + TTL
    if len(store) >= LIMIT:
        store.popitem(last=False)
    store[(card.owner, chat, message)] = card


def callback_context(function):
    @wraps(function)
    async def scoped(update, context, *args, **kwargs):
        q = update.callback_query
        if q is not None and q.message is not None:
            context = for_message(context, update.effective_user.id, q.message.chat_id, q.message.message_id)
        return await function(update, context, *args, **kwargs)
    return scoped
