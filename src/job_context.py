"""Scope only operation fields; navigation fences remain shared with input handlers."""
from collections.abc import MutableMapping

FIELDS = {'vod_busy','vod_active_operation','vod_cancel','download_ui','_vod_job','html_file_action'}


class JobData(MutableMapping):
    def __init__(self, shared, job):
        self.shared, self.local = shared, {'_vod_job':job}

    def __getitem__(self, key):
        return (self.local if key in FIELDS else self.shared)[key]

    def __setitem__(self, key, value):
        (self.local if key in FIELDS else self.shared)[key] = value

    def __delitem__(self, key):
        del (self.local if key in FIELDS else self.shared)[key]

    def __iter__(self):
        return iter((set(self.shared)-FIELDS) | set(self.local))

    def __len__(self):
        return sum(1 for _ in self)


class JobContext:
    def __init__(self, context, job):
        self.base = context
        self.shared_user_data = context.user_data
        self.user_data = JobData(context.user_data, job)

    def __getattr__(self, name):
        return getattr(self.base, name)
