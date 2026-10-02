"""Event adapters: sources of events and programs rather than jobs.

Configured per company under ``event_sources`` in the registry and polled
alongside the job board (:meth:`Company.sources`). Each returns ``Job`` records
that the pipeline labels as events by the source's category.

================== ======================================================
``event_page``     a server-rendered events/programs page (links or headings)
``avature_events`` an Avature events portal (Bloomberg, Two Sigma)
``luma``           a public lu.ma calendar
``sitemap_watch``  new program pages in a published sitemap (Citadel)
================== ======================================================
"""

from jobmonitor.scrapers.events.avature_events import AvatureEventsSource
from jobmonitor.scrapers.events.event_page import EventPageSource
from jobmonitor.scrapers.events.luma import LumaSource
from jobmonitor.scrapers.events.sitemap_watch import SitemapWatchSource

#: Provider keys of the event adapters.
EVENT_PROVIDERS: tuple[str, ...] = ("event_page", "avature_events", "luma", "sitemap_watch")

__all__ = [
    "EVENT_PROVIDERS",
    "AvatureEventsSource",
    "EventPageSource",
    "LumaSource",
    "SitemapWatchSource",
]
