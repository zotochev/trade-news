"""Forced flows: dated events when someone must buy or sell by rule, margin, calendar or mandate
(option expiry, month end, index rebalancing, auctions, lock-ups, token unlocks...).

- rules.py: dates computed from exchange / Russian calendars, no external data;
- calendar.py: reading the merged calendar (our table + token unlocks + central bank meetings);
- notify.py: the Monday digest and the eve reminders in Telegram.
The events are collected by collectors/flow_rules.py, finnhub_calendar.py, treasury_auctions.py.
"""
